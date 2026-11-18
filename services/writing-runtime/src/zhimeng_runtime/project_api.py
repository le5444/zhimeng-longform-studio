from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore

_logger = logging.getLogger(__name__)
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime import novel_export_service
from zhimeng_runtime import skill_bundle
from zhimeng_runtime.project_models import (
    ProjectBindingListResponse,
    ProjectBindingSummary,
    ProjectCreateRequest,
    ProjectCoverUploadRequest,
    ProjectFileContentResponse,
    ProjectFileReadRequest,
    ProjectGenerateCoverRequest,
    ProjectGenerateCoverResponse,
    ProjectGenerateInspirationRequest,
    ProjectGenerateInspirationResponse,
    ProjectGenerateSynopsisCandidate,
    ProjectGenerateSynopsisRequest,
    ProjectGenerateSynopsisResponse,
    MatchedDistillationCard,
    ProjectInspectRequest,
    ProjectInspectionResponse,
    ProjectUpdateRequest,
    TextProjectImportRequest,
    TextProjectImportResponse,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import (
    InvalidNovelProjectError,
    ProjectChangedDuringReadError,
    ProjectFileNotFoundError,
    ProjectReader,
    ProjectReaderError,
    ProjectScanLimitError,
    SECTION_LABELS,
    UntrustedProjectPathError,
    UnreadableProjectFileError,
)
from zhimeng_runtime.provider_settings_service import ProviderSettingsService
from zhimeng_runtime.runtime_maintenance import RuntimeMaintenance
from zhimeng_runtime.text_project_import_service import (
    TextProjectImportError,
    TextProjectImportService,
    _safe_name,
)


def _run_provider_chat_text(provider: Any, messages: list[dict[str, str]]) -> str:
    """走规范 Provider 链路发一轮非流式 chat，返回模型文本。

    供简介/开书企划/书名优化等一次性模型调用共用——以前它们各自拼
    `/chat/completions` + Bearer，作者配的协议、认证方式、接受上报名、
    重试与熔断对这些调用一律不生效。失败抛 RuntimeError（带人话），
    空回复返回空串，由调用方决定怎么向作者解释。
    """
    from zhimeng_runtime.agent_provider import (
        AgentProviderCancelled,
        AgentProviderError,
        ProviderTextDelta,
        run_provider_blocking,
    )

    pieces: list[str] = []

    async def collect() -> None:
        cancel_event = asyncio.Event()
        stream = provider.stream_chat(
            messages,
            cancel_event=cancel_event,
            tools=(),
        )
        async for event in stream:
            if isinstance(event, ProviderTextDelta) and event.text:
                pieces.append(event.text)

    try:
        run_provider_blocking(collect())
    except AgentProviderError as exc:
        raise RuntimeError(f"模型通道没有走通（{exc}）") from exc
    except AgentProviderCancelled as exc:
        raise RuntimeError("模型通道被取消") from exc
    return "".join(pieces).strip()


def reveal_folder(folder: Path) -> None:
    """在系统自己的文件管理器里打开一个文件夹（Windows 资源管理器 / 访达 / Linux 默认文件管理器）。

    只给文件夹用：交给 explorer.exe 的是文件夹路径，不会顺手打开或运行什么文件。测试里换成假的。
    """
    if os.name == "nt":
        subprocess.Popen(["explorer.exe", str(folder)])  # noqa: S603,S607 - 固定程序 + 已核过的文件夹
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603,S607


def create_project_router(
    reader: ProjectReader | None,
    store: SqliteStore,
    *,
    reader_error: str | None = None,
    locks: ProjectLockRegistry | None = None,
    text_import_service: TextProjectImportService | None = None,
    provider_settings: ProviderSettingsService | None = None,
    provider_resolver: Callable[[], Any | None] | None = None,
    maintenance: RuntimeMaintenance | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects", tags=["projects"])
    project_locks = locks or ProjectLockRegistry()
    background = maintenance or RuntimeMaintenance()

    def require_reader() -> ProjectReader:
        if reader is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=reader_error or "No trusted project roots are configured",
            )
        return reader

    def binding_payload(record: ProjectBindingRecord) -> ProjectBindingSummary:
        return ProjectBindingSummary(
            project_id=record.project_id,
            display_name=record.display_name,
            root_path=record.root_path,
            revision=record.revision,
            fingerprint=record.fingerprint or None,
            snapshot_ready=bool(record.fingerprint and record.revision >= 1),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def require_binding(project_id: str) -> ProjectBindingRecord:
        record = store.get_project_binding(project_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project binding was not found",
            )
        if reader is not None:
            try:
                reader.trust_root(record.root_path)
            except Exception as exc:  # 以前静默吞掉，挪盘后全靠猜
                _logger.warning("项目根授信失败 %s: %s", record.root_path, exc)
        return record

    def raise_project_error(exc: ProjectReaderError) -> NoReturn:
        detail_text: str | None = None
        if isinstance(exc, UntrustedProjectPathError):
            status_code = status.HTTP_403_FORBIDDEN
            code = "project_path_untrusted"
            retryable = False
            detail_text = str(exc) + (
                " 自救办法（挪盘/新盘后旧路径 403 时）：让 Agent 执行一条命令当场信任"
                "新位置，重启后仍生效——curl -X POST "
                "http://127.0.0.1:8787/api/v1/runtime/trusted-roots "
                '-H "Content-Type: application/json" '
                '-d "{\"path\": \"书的新位置\"}"；'
                "或把新位置写进启动配置（小说目录.txt / ZHIMENG_TRUSTED_PROJECT_ROOTS）"
                "后重启写作台。"
            )
        elif isinstance(exc, ProjectFileNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "project_file_not_found"
            retryable = False
        elif isinstance(exc, (InvalidNovelProjectError, UnreadableProjectFileError)):
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            code = "project_file_unreadable" if isinstance(exc, UnreadableProjectFileError) else "invalid_novel_project"
            retryable = False
        elif isinstance(exc, ProjectScanLimitError):
            status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            code = "project_scan_limit"
            retryable = False
        elif isinstance(exc, ProjectChangedDuringReadError):
            status_code = status.HTTP_409_CONFLICT
            code = "project_snapshot_changed"
            retryable = True
        else:
            status_code = status.HTTP_409_CONFLICT
            code = "project_read_conflict"
            retryable = True
        raise ApiError(
            detail_text or str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    def etag_matches(header: str | None, etag: str) -> bool:
        if not header:
            return False
        for candidate in header.split(","):
            normalized = candidate.strip()
            if normalized == "*":
                return True
            if normalized.startswith("W/"):
                normalized = normalized[2:].strip()
            if normalized.strip('"') == etag:
                return True
        return False

    def representation_etag(payload: dict[str, object]) -> str:
        representation = {
            "project_id": payload["project_id"],
            "snapshot_revision": payload["snapshot_revision"],
            "project_fingerprint": payload["project_fingerprint"],
            "file": payload["file"],
        }
        return hashlib.sha256(
            json.dumps(
                representation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    async def inspect_root(root_path: str) -> ProjectInspectionResponse:
        active_reader = require_reader()
        try:
            resolved_root = await run_in_threadpool(active_reader.resolve, root_path)
        except ProjectReaderError as exc:
            raise_project_error(exc)
        async with project_locks.for_path(resolved_root):
            observed_at = datetime.now(UTC)
            try:
                payload = await run_in_threadpool(active_reader.inspect, resolved_root)
            except ProjectReaderError as exc:
                raise_project_error(exc)

            project = payload["project"]
            if not isinstance(project, dict):  # pragma: no cover - internal contract guard.
                raise RuntimeError("Project Reader returned an invalid project payload")
            record = await run_in_threadpool(
                store.sync_project_binding,
                project_id=str(project["project_id"]),
                root_path=str(project["root_path"]),
                display_name=str(project["display_name"]),
                fingerprint=str(project["fingerprint"]),
                observed_at=observed_at,
            )
            if record.fingerprint != project["fingerprint"]:
                raise_project_error(
                    ProjectChangedDuringReadError(
                        "a newer project snapshot was committed while this scan was running"
                    )
                )
            project["revision"] = record.revision
            return ProjectInspectionResponse.model_validate(payload)

    async def read_root_file(
        root_path: str,
        file_id: str,
    ) -> ProjectFileContentResponse:
        active_reader = require_reader()
        try:
            resolved_root = await run_in_threadpool(active_reader.resolve, root_path)
        except ProjectReaderError as exc:
            raise_project_error(exc)
        async with project_locks.for_path(resolved_root):
            observed_at = datetime.now(UTC)
            try:
                payload = await run_in_threadpool(
                    active_reader.read_text_file,
                    resolved_root,
                    file_id,
                )
            except ProjectReaderError as exc:
                raise_project_error(exc)

            project_id = str(payload["project_id"])
            record = await run_in_threadpool(
                store.sync_project_binding,
                project_id=project_id,
                root_path=str(resolved_root),
                display_name=resolved_root.name,
                fingerprint=str(payload["project_fingerprint"]),
                observed_at=observed_at,
            )
            if record.fingerprint != payload["project_fingerprint"]:
                raise_project_error(
                    ProjectChangedDuringReadError(
                        "a newer project snapshot was committed while this file was read"
                    )
                )
            payload["snapshot_revision"] = record.revision
            payload["etag"] = representation_etag(payload)
            return ProjectFileContentResponse.model_validate(payload)

    @router.get("", response_model=ProjectBindingListResponse)
    async def list_projects() -> ProjectBindingListResponse:
        records = await run_in_threadpool(store.list_project_bindings)
        return ProjectBindingListResponse(
            projects=[binding_payload(record) for record in records]
        )

    @router.post("/inspect", response_model=ProjectInspectionResponse)
    async def inspect_project(request: ProjectInspectRequest) -> ProjectInspectionResponse:
        return await inspect_root(request.root_path)

    # ------------------------------------------------------------------
    # 大书扫描进度：inspect 是逐文件哈希的慢活（974 章 12 秒以上），
    # 后台任务 + 轮询让作者看到「扫到第几个文件」，而不是白转圈。
    # ------------------------------------------------------------------
    scan_jobs: dict[str, dict[str, object]] = {}
    scan_jobs_lock = threading.Lock()

    @router.post("/inspect-jobs")
    async def create_inspect_job(request: ProjectInspectRequest) -> dict[str, object]:
        job_id = f"scanjob-{uuid4().hex}"
        job: dict[str, object] = {
            "status": "queued",
            "scanned": 0,
            "total": 0,
            "result": None,
            "error": None,
            "generation": background.generation,
        }
        with scan_jobs_lock:
            scan_jobs[job_id] = job
            # 防泄漏：只留最近 20 个任务。
            for stale in list(scan_jobs)[:-20]:
                if scan_jobs[stale].get("status") in {"done", "failed"}:
                    scan_jobs.pop(stale, None)

        async def run_job() -> None:
            job["status"] = "running"
            observed_at = datetime.now(UTC)

            def progress(done: int, total: int) -> None:
                job["scanned"] = done
                job["total"] = total

            try:
                active_reader = require_reader()
                resolved_root = await run_in_threadpool(active_reader.resolve, request.root_path)
                async with project_locks.for_path(resolved_root):
                    payload = await run_in_threadpool(
                        active_reader.inspect,
                        resolved_root,
                        progress,
                    )
                    project = payload["project"]
                    record = await run_in_threadpool(
                        store.sync_project_binding,
                        project_id=str(project["project_id"]),
                        root_path=str(project["root_path"]),
                        display_name=str(project["display_name"]),
                        fingerprint=str(project["fingerprint"]),
                        observed_at=observed_at,
                    )
                if record.fingerprint != project["fingerprint"]:
                    raise ProjectChangedDuringReadError(
                        "a newer project snapshot was committed while this scan was running"
                    )
                project["revision"] = record.revision
                job["result"] = payload
                job["status"] = "done"
            except Exception as exc:  # noqa: BLE001 - 任务失败要如实回报给前端
                job["status"] = "failed"
                job["error"] = str(exc)

        background.start_background(run_job, name=f"zhimeng-{job_id}")
        return {"job_id": job_id}

    @router.get("/inspect-jobs/{job_id}")
    async def get_inspect_job(job_id: str) -> dict[str, object]:
        with scan_jobs_lock:
            job = scan_jobs.get(job_id)
            if job is None:
                raise ApiError(
                    "扫描任务不存在或已被清理",
                    status_code=status.HTTP_404_NOT_FOUND,
                    code="inspect_job_not_found",
                )
            if job["generation"] != background.generation:
                return {"status": "failed", "scanned": job["scanned"], "total": job["total"],
                        "result": None, "error": "运行时已恢复备份，请重新扫描这个文件夹。"}
            return {
                "status": job["status"],
                "scanned": job["scanned"],
                "total": job["total"],
                "result": job["result"],
                "error": job["error"],
            }

    @router.post(
        "/create",
        response_model=ProjectInspectionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_novel_project(
        request: ProjectCreateRequest,
    ) -> ProjectInspectionResponse:
        """根据书名、作者、题材与简介，在受管根目录下创建全新作品标准目录结构。"""
        title = _safe_name(request.title.strip(), fallback="新长篇作品")
        author = (request.author or "作者").strip()
        genre = (request.genre or "未指定").strip()
        summary = (request.summary or "").strip()

        # 确定存放根目录：优先作者自己的书目根，而不是运行时托管目录。
        # 新书要建在作者找得到、备份得到的地方（2026-09-05 演练发现新书
        # 落在 .runtime/author-projects 里，作者在自己的书根里找不到它）。
        managed_root = (
            text_import_service.managed_root
            if text_import_service is not None
            else None
        )
        managed_root_resolved = managed_root.resolve() if managed_root else None
        books_root = next(
            (
                Path(root)
                for root in (reader.trusted_roots or [])
                if managed_root_resolved is None
                or Path(root).resolve() != managed_root_resolved
            ),
            None,
        )
        create_root = books_root or managed_root or (
            Path(reader.trusted_roots[0])
            if reader and reader.trusted_roots
            else Path.home() / ".zhimeng-novels"
        )
        create_root.mkdir(parents=True, exist_ok=True)

        target_dir = create_root / title
        if target_dir.exists():
            for index in range(2, 1000):
                candidate = create_root / f"{title} ({index})"
                if not candidate.exists():
                    target_dir = candidate
                    break

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            # 新书只建一份《这本书.md》：书名、作者那句想法、两个空标题。
            # 正文 / 设定 / 大纲 / 素材这些夹子谁用谁建（Agent 或作者自己），
            # 不再预铺 01–09 编号夹和九份模板文件——那是旧流水线给机器看的摆法
            # （作者 2026-09-23：「要更简单的书夹结构」；借鉴 NovelCrafter / Scrivener /
            # Obsidian 写作库的共同点：一页纸说明，其余按需长出来）。老书一个字节不动。
            style_preset = (request.style_preset or "").strip()
            rules_list = [rule.strip() for rule in request.style_rules if rule.strip()]
            how_lines = ([f"- 基调：{style_preset}"] if style_preset else []) + [
                f"- {rule}" for rule in rules_list
            ]
            how_section = (
                "\n".join(how_lines)
                if how_lines
                else "（还没有。想要的语气、节奏和不想出现的写法，记在这里。）"
            )
            facts = []
            if (request.author or "").strip():
                facts.append(f"- 作者：{author}")
            if (request.genre or "").strip():
                facts.append(f"- 题材：{genre}")
            facts_section = (
                "\n".join(facts)
                if facts
                else "（还没有。聊到并确认一项，就记在这里。）"
            )
            book_card = (
                f"# 《{title}》\n\n"
                f"## 现在的想法\n\n{summary or '还没有写。'}\n\n"
                f"## 已经定下的事\n\n{facts_section}\n\n"
                f"## 这本书怎么写\n\n{how_section}\n"
            )
            (target_dir / "这本书.md").write_text(book_card, encoding="utf-8")
        except Exception as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise ApiError(
                f"创建新作品失败: {exc}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="project_create_failed",
            ) from exc

        return await inspect_root(str(target_dir))

    @router.post("/{project_id}/duplicate", response_model=ProjectInspectionResponse)
    async def duplicate_project(project_id: str) -> ProjectInspectionResponse:
        """复制一本书成为试验副本（同目录旁新建「书名-副本」文件夹）。

        大重构前先抄一份放心改：副本逐字节拷贝，原书一个字节不动；
        拷贝完成后立刻扫描上架，项目身份来自新路径（project_id 由路径
        派生，天然不会和原书冲突）。
        """
        binding = await run_in_threadpool(require_binding, project_id)
        source_root = Path(binding.root_path) if binding.root_path else None
        if source_root is None or not source_root.is_dir():
            raise ApiError(
                "这本书绑定的是外部文件夹但找不到目录，没法复制",
                status_code=status.HTTP_409_CONFLICT,
                code="project_duplicate_missing_root",
            )
        base_name = source_root.name
        candidate = source_root.parent / f"{base_name}-副本"
        suffix = 2
        while candidate.exists():
            candidate = source_root.parent / f"{base_name}-副本{suffix}"
            suffix += 1
        try:
            await run_in_threadpool(
                shutil.copytree,
                source_root,
                candidate,
            )
        except OSError as exc:
            raise ApiError(
                f"复制作品失败：{exc}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="project_duplicate_failed",
            ) from exc
        return await inspect_root(str(candidate))

    @router.patch("/{project_id}", response_model=ProjectBindingSummary)
    async def update_project(
        project_id: str,
        request: ProjectUpdateRequest,
    ) -> ProjectBindingSummary:
        """重命名作品显示名称。"""
        record = await run_in_threadpool(
            store.update_project_binding_display_name,
            project_id,
            request.display_name,
        )
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project binding not found",
            )
        return binding_payload(record)

    @router.post("/{project_id}/reveal")
    async def reveal_project(project_id: str) -> dict[str, object]:
        """左栏项目的「在资源管理器里打开」（专题《项目即文件夹》第 4 节）：只开这个项目自己的文件夹。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root = Path(binding.root_path)
        if not root.is_dir():
            raise ApiError(
                f"这个项目的文件夹不在原来的位置了：{root}",
                status_code=status.HTTP_404_NOT_FOUND,
                code="project_root_missing",
            )
        try:
            await run_in_threadpool(reveal_folder, root)
        except OSError as exc:
            raise ApiError(
                f"资源管理器没打开：{exc}",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="project_reveal_failed",
            ) from exc
        return {"ok": True, "path": str(root)}

    @router.delete("/{project_id}")
    async def delete_project(
        project_id: str,
        delete_disk_files: bool = False,
    ) -> dict[str, object]:
        """从书架删除/解绑作品，可选择是否一并移入回收站。"""
        binding = await run_in_threadpool(require_binding, project_id)
        # 移入回收站成不成功必须如实回报。原来失败就 pass，接口照样返回 ok=true，
        # 于是「书从书架消失了、文件夹还原地躺着、回收站里什么都没有」被说成删除成功，
        # 作者既找不回也确认不了。
        files_moved: bool | None = None
        trash_error: str | None = None
        trash_path: str | None = None
        if delete_disk_files and binding.root_path:
            root_path = Path(binding.root_path)
            if root_path.exists() and root_path.is_dir():
                files_moved = False
                trash_dir = root_path.parent / ".trash"
                timestamp = int(time.time())
                trash_dest = trash_dir / f"{root_path.name}_{timestamp}"
                try:
                    trash_dir.mkdir(exist_ok=True)
                    shutil.move(str(root_path), str(trash_dest))
                    files_moved = True
                    trash_path = str(trash_dest)
                except OSError as error:
                    trash_error = f"{type(error).__name__}: {error}"

        # 选择了“同时移动文件夹”时，移动失败就不能继续解绑书架。
        # 否则作者会看到书不见了，文件却还在原处，之后很难判断到底发生了什么。
        if trash_error:
            return {
                "ok": False,
                "project_id": project_id,
                "files_moved": False,
                "message": (
                    "文件夹没有移进回收目录，所以作品仍保留在书架上。"
                    f"原因：{trash_error}"
                ),
                "trash_error": trash_error,
            }

        deleted = await run_in_threadpool(store.delete_project_binding, project_id)
        result: dict[str, object] = {"ok": deleted, "project_id": project_id}
        if files_moved is not None:
            result["files_moved"] = files_moved
            if trash_path:
                result["trash_path"] = trash_path
            if trash_error:
                result["message"] = (
                    "作品已从书架移除，但文件夹没能移进回收站，仍在原处："
                    f"{trash_error}"
                )
                result["trash_error"] = trash_error
        return result

    @router.post("/{project_id}/cover")
    async def upload_project_cover(
        project_id: str,
        request: ProjectCoverUploadRequest,
    ) -> dict[str, object]:
        """为作品上传或保存自定义封面图片（支持 base64 data URL）。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        if not root_path.exists():
            raise HTTPException(status_code=404, detail="作品根目录不存在")

        raw_data = request.image_data.strip()
        if "," in raw_data and "base64" in raw_data[:50]:
            raw_data = raw_data.split(",", 1)[1]

        try:
            image_bytes = base64.b64decode(raw_data)
        except Exception as exc:
            raise ApiError("图片 Base64 格式无效", status_code=400, code="invalid_image") from exc

        cover_file = root_path / "cover.png"
        cover_file.write_bytes(image_bytes)

        # 同时也向 01_项目定位 写入一份作为资产
        sec_dir = root_path / "01_项目定位"
        if sec_dir.exists():
            try:
                (sec_dir / "cover.png").write_bytes(image_bytes)
            except Exception:
                pass

        return {
            "ok": True,
            "cover_url": f"/api/v1/projects/{project_id}/cover?t={int(time.time())}",
        }

    @router.get("/{project_id}/cover")
    async def get_project_cover(project_id: str) -> Response:
        """获取作品的封面图片文件；没设封面就返回 204，让 <img> 走 onError 走生成色块，不再每次刷 4 条 404 噪音。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        for name in ("cover.png", "cover.jpg", "cover.jpeg", "01_项目定位/cover.png"):
            candidate = root_path / name
            if candidate.exists() and candidate.is_file():
                media_type = "image/jpeg" if candidate.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                return FileResponse(candidate, media_type=media_type)

        return Response(status_code=204)

    @router.post(
        "/{project_id}/generate-cover",
        response_model=ProjectGenerateCoverResponse,
    )
    async def generate_project_cover(
        project_id: str,
        request: ProjectGenerateCoverRequest,
    ) -> ProjectGenerateCoverResponse:
        """根据书名、题材和简介，调用已配置的模型/生图 API 一键生成精美封面。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)

        # 自动提取书名与题材，拼装适合生图的提示词
        title = binding.display_name
        prompt = (request.prompt or "").strip()
        if not prompt:
            prompt = (
                f"东方玄幻仙侠风格网文小说封面，书名《{title}》，"
                f"唯美意境，仙气缭绕，云海山巅，持剑主角立绘，光影细腻，电影级质感插画，8k高清，无水印无乱码"
            )

        # 检查 provider 设置
        effective = provider_settings.effective() if provider_settings else None
        if not effective or not effective.api_key or not effective.base_url:
            return ProjectGenerateCoverResponse(
                ok=False,
                prompt=prompt,
                message="尚未在「偏好设置」中配置 API Key 与接口地址。已为你自动生成封面提示词，你可以复制提示词到外部绘画工具生成后上传，或在设置中配置 API Key。",
            )

        # 尝试调用 OpenAI 兼容的 /images/generations 接口
        base_url = effective.base_url.rstrip("/")
        image_url_endpoint = f"{base_url}/images/generations"
        req_payload = {
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }

        try:
            req = urllib.request.Request(
                image_url_endpoint,
                data=json.dumps(req_payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {effective.api_key}",
                    "User-Agent": "ZhimengRuntime/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))

            b64_data = None
            if "data" in resp_data and isinstance(resp_data["data"], list) and len(resp_data["data"]) > 0:
                first = resp_data["data"][0]
                if "b64_json" in first:
                    b64_data = first["b64_json"]
                elif "url" in first:
                    # 下载远程 url
                    img_req = urllib.request.Request(
                        first["url"],
                        headers={"User-Agent": "ZhimengRuntime/1.0"},
                    )
                    with urllib.request.urlopen(img_req, timeout=30) as img_resp:
                        img_bytes = img_resp.read()
                        (root_path / "cover.png").write_bytes(img_bytes)
                        return ProjectGenerateCoverResponse(
                            ok=True,
                            prompt=prompt,
                            cover_url=f"/api/v1/projects/{project_id}/cover?t={int(time.time())}",
                            message="封面生成成功！",
                        )

            if b64_data:
                img_bytes = base64.b64decode(b64_data)
                (root_path / "cover.png").write_bytes(img_bytes)
                return ProjectGenerateCoverResponse(
                    ok=True,
                    prompt=prompt,
                    cover_url=f"/api/v1/projects/{project_id}/cover?t={int(time.time())}",
                    message="封面生成成功！",
                )

            return ProjectGenerateCoverResponse(
                ok=False,
                prompt=prompt,
                message="接口已响应，但未返回可用的图片数据。已生成提示词供复制使用。",
            )
        except Exception as exc:
            return ProjectGenerateCoverResponse(
                ok=False,
                prompt=prompt,
                message=f"生图接口调用失败（{exc}）。已生成提示词，你可以复制到外部 AI 绘图工具生成后上传。",
            )

    def _generate_synopsis_candidates(
        title: str,
        genre: str,
        keywords: str | None = None,
    ) -> tuple[list[ProjectGenerateSynopsisCandidate], str | None]:
        # 收编进规范 Provider 链路（2026-09-12）：不再自己拼 /chat/completions + Bearer，
        # 作者配的协议（Responses/Anthropic）、认证方式、接受上报名、重试与熔断对简介生成同样生效。
        # 封面生成（/images/generations）不在 chat 链路覆盖范围内，维持原实现。
        if provider_resolver is None:
            return [], "模型通道还没就绪，请稍后再试。"
        try:
            provider = provider_resolver()
        except Exception as exc:  # noqa: BLE001 - 配置问题翻成人话，不炸请求
            return [], f"模型通道配置有问题（{exc}）。请检查「设置 → 写作模型」。"
        if provider is None:
            return [], "还没配置写作模型。请先在「设置 → 写作模型」里填好接口地址和 API Key，再来生成爆款简介。"

        prompt_system = (
            "你是一位拥有十年经验的网文爆款主编与金牌策划，精通番茄、起点、飞卢等头部平台的爆款网文简介法则。"
            "请根据用户提供的书名、题材和核心关键词，量身打造3组风格各异、点击转化率极高的爆款网文简介。\n"
            "写作规范：\n"
            "1. 短句断句，节奏明快，多用一两句话成段；\n"
            "2. 开局黄金钩子，制造强烈身份/剧情反差；\n"
            "3. 明确亮出核心金手指与爽点期待；\n"
            "4. 必须严格输出为合法 JSON 格式，包含 candidates 数组，每项包含 tag(风格名称字符串), content(完整简介文案字符串), highlights(包含3个亮点词的字符串数组)。不要包含任何 Markdown 格式或多余前缀后缀。"
        )
        user_input = f"作品书名：《{title}》\n题材类型：{genre or '玄幻修仙'}\n核心要素/关键词：{keywords or '无'}"

        try:
            raw_text = _run_provider_chat_text(
                provider,
                [
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": user_input},
                ],
            )
        except RuntimeError as exc:
            return [], f"简介生成失败（{exc}）。请检查模型是否可用，然后重试。"
        if not raw_text:
            return [], "模型没有返回可用的简介，请重试一次。"

        # 清理可能的 markdown 代码块包裹
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            return [], "模型没有返回可用的简介，请重试一次。"
        candidates_data = parsed.get("candidates") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else None)
        if isinstance(candidates_data, list) and len(candidates_data) > 0:
            results = []
            for item in candidates_data[:3]:
                if isinstance(item, dict) and "content" in item:
                    results.append(
                        ProjectGenerateSynopsisCandidate(
                            tag=str(item.get("tag") or "爆款推荐风"),
                            content=str(item["content"]).strip(),
                            highlights=[str(h) for h in item.get("highlights", [])][:3],
                        )
                    )
            if results:
                return results, None
        return [], "模型没有返回可用的简介，请重试一次。"

    def _find_master_cards_path() -> Path | None:
        candidates = [
            # 项目内 skills/ 是真库（3.62 起 cards.jsonl 进版本管理）
            skill_bundle.repo_root() / "skills/novel-distillation/library/cards.jsonl",
            # 旧本机路径做兜底
            Path.home() / ".codex/skills/novel-distillation/library/cards.jsonl",
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    def _read_master_card_lines() -> list[str]:
        """读一次卡库的行列表。8.4MB 的 cards.jsonl，一次请求里只该读一遍。"""
        p = _find_master_cards_path()
        if not p or not p.exists():
            return []
        try:
            return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        except OSError:
            return []

    def _match_master_cards(
        query: str,
        genre: str | None = None,
        limit: int = 5,
        card_type: str | None = None,
        card_lines: list[str] | None = None,
    ) -> list[MatchedDistillationCard]:
        lines = card_lines if card_lines is not None else _read_master_card_lines()
        if not lines:
            return []

        cleaned = re.sub(r"[^\w\u4e00-\u9fa5]+", " ", query).strip()
        tokens = [t.lower() for t in cleaned.split() if t]

        chinese_chars = "".join([c for c in query if "\u4e00" <= c <= "\u9fa5"])
        for i in range(len(chinese_chars) - 1):
            tokens.append(chinese_chars[i:i+2].lower())
        for i in range(len(chinese_chars) - 2):
            tokens.append(chinese_chars[i:i+3].lower())

        if genre:
            tokens.append(genre.lower())

        browse_mode = not tokens
        results: list[tuple[int, MatchedDistillationCard]] = []
        try:
            for line in lines:
                try:
                    card = json.loads(line)
                except Exception:
                    continue

                card_type_value = str(card.get("card_type") or "")
                if card_type:
                    # 指定了类型就按它筛（不受白名单限制，浏览模式要看全部类型）
                    if card_type_value != card_type:
                        continue
                elif card_type_value not in ("trope", "genre", "voice", "scene", "packaging", "validator", "work"):
                    continue

                title = str(card.get("title") or "")
                summary = str(card.get("summary") or "")
                rule = str(card.get("rule") or "")
                tags = [str(t) for t in card.get("retrieval_tags") or card.get("tags") or []]

                searchable_text = f"{title} {summary} {rule} {' '.join(tags)}".lower()

                score = 0
                if browse_mode:
                    # 没有检索词：浏览模式，按库里的顺序直接给
                    score = 1
                else:
                    for t in tokens:
                        if len(t) >= 2 and t in searchable_text:
                            score += 5 if t in title.lower() else 1

                card_obj = MatchedDistillationCard(
                    card_id=str(card.get("card_id") or ""),
                    title=title,
                    card_type=card_type_value,
                    summary=summary[:200] if summary else (rule[:200] if rule else "核心网文套路与爆款技巧"),
                    rule=rule[:300] if rule else None,
                    tags=tags[:5],
                )

                if score > 0:
                    results.append((score, card_obj))

            results.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in results[:limit]]
        except Exception:
            return []


    def _read_master_card_by_id(
        card_id: str,
        card_lines: list[str] | None = None,
    ) -> dict[str, object] | None:
        """按 card_id 从总库捞一张卡的完整字段（点开看全文用，不新造存储）。"""
        lines = card_lines if card_lines is not None else _read_master_card_lines()
        for line in lines:
            try:
                card = json.loads(line)
            except Exception:
                continue
            if isinstance(card, dict) and str(card.get("card_id") or "") == card_id:
                return card
        return None

    def _generate_inspiration_candidates(
        idea: str,
        genre: str | None = None,
    ) -> ProjectGenerateInspirationResponse:
        idea_clean = idea.strip()
        matched_cards = _match_master_cards(idea_clean, genre, limit=4)
        cards_context = "\n".join([f"- 【{c.card_type}】{c.title}：{c.summary}" for c in matched_cards]) if matched_cards else "暂无匹配的特定技巧卡"

        # 收编进规范 Provider 链路（2026-09-12）：协议/认证/接受名单/重试与作者设置一致。
        if provider_resolver is None:
            return ProjectGenerateInspirationResponse(
                ok=False,
                idea=idea_clean,
                matched_cards=matched_cards,
                message="模型通道还没就绪，请稍后再试。",
            )
        try:
            provider = provider_resolver()
        except Exception as exc:  # noqa: BLE001 - 配置问题翻成人话
            return ProjectGenerateInspirationResponse(
                ok=False,
                idea=idea_clean,
                matched_cards=matched_cards,
                message=f"模型通道配置有问题（{exc}）。请检查「设置 → 写作模型」。",
            )
        if provider is None:
            return ProjectGenerateInspirationResponse(
                ok=False,
                idea=idea_clean,
                matched_cards=matched_cards,
                message="还没配置写作模型。请先在「设置 → 写作模型」里填好接口地址和密钥，再生成开书草案。",
            )

        prompt_system = (
            "你是小说作者的开书搭档。根据作者的一句话想法，提出几份可继续修改的草案，"
            "不要套固定平台公式，不要擅自把所有小说改成短句爽文，也不要把建议说成唯一答案。\n"
            "返回 JSON 对象，包含：\n"
            "1. recommended_titles: 3个贴合故事的备选书名；\n"
            "2. recommended_genre: 题材建议，不确定可以留空；\n"
            "3. recommended_style: 用普通中文概括可能合适的文风，不确定可以留空；\n"
            "4. recommended_style_rules: 0~5条只针对这本书的具体写法；\n"
            "5. synopsis_candidates: 3份不同方向的简介，每份包含 tag、content、highlights；\n"
            "6. golden_opening_hook: 开篇三章可以怎样展开。\n"
            "只输出 JSON，不要添加 Markdown 围栏或解释。"
        )
        user_input = f"作者的想法：{idea_clean}\n题材偏好：{genre or '还没定'}\n可参考的写作经验：\n{cards_context}"

        try:
            raw_text = _run_provider_chat_text(
                provider,
                [
                    {"role": "system", "content": prompt_system},
                    {"role": "user", "content": user_input},
                ],
            )
            if raw_text.startswith("```"):
                lines = raw_text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip().startswith("```"):
                    lines = lines[:-1]
                raw_text = "\n".join(lines).strip()

            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                titles = [str(t) for t in parsed.get("recommended_titles", [])][:3]
                rec_genre = str(parsed.get("recommended_genre") or genre or "")
                rec_style = str(parsed.get("recommended_style") or "")
                rec_rules = [str(r) for r in parsed.get("recommended_style_rules", [])][:5]
                hook = str(parsed.get("golden_opening_hook") or "")

                synopses: list[ProjectGenerateSynopsisCandidate] = []
                for s in parsed.get("synopsis_candidates", []):
                    if isinstance(s, dict) and "content" in s:
                        synopses.append(ProjectGenerateSynopsisCandidate(
                            tag=str(s.get("tag") or "爆款爽点风"),
                            content=str(s["content"]).strip(),
                            highlights=[str(h) for h in s.get("highlights", [])][:3],
                        ))

                return ProjectGenerateInspirationResponse(
                    ok=True,
                    idea=idea_clean,
                    recommended_titles=titles,
                    recommended_genre=rec_genre,
                    recommended_style=rec_style,
                    recommended_style_rules=rec_rules,
                    synopsis_candidates=synopses,
                    golden_opening_hook=hook or None,
                    matched_cards=matched_cards,
                )
        except Exception as exc:
            return ProjectGenerateInspirationResponse(
                ok=False,
                idea=idea_clean,
                matched_cards=matched_cards,
                message=f"开书企划生成失败（{exc}）。请检查模型是否可用，然后重试。",
            )

        return ProjectGenerateInspirationResponse(
            ok=False,
            idea=idea_clean,
            matched_cards=matched_cards,
            message="模型没有返回可用的企划，请重试一次。",
        )

    @router.post(
        "/generate-inspiration",
        response_model=ProjectGenerateInspirationResponse,
    )
    async def generate_inspiration_api(
        request: ProjectGenerateInspirationRequest,
    ) -> ProjectGenerateInspirationResponse:
        """根据用户输入的一句话脑洞/灵感，智能匹配蒸馏总库中的爆款卡片，并一键生成全套开书企划。"""
        return await run_in_threadpool(
            _generate_inspiration_candidates,
            request.idea,
            request.genre,
        )

    @router.get(
        "/distillation/cards/master-library",
    )
    async def get_master_distillation_cards(
        q: str | None = None,
        genre: str | None = None,
        limit: int = 20,
        card_type: str | None = None,
        card_id: str | None = None,
    ) -> dict[str, object]:
        """浏览或检索蒸馏总库中的网文技巧卡片。

        card_id 给出时返回那张卡的完整字段（点开看全文用）；
        否则按检索词 + 题材 + 类型浏览，没有检索词时按库里顺序直接列。
        """
        lines = await run_in_threadpool(_read_master_card_lines)
        total_count = len(lines)

        if card_id:
            full_card = await run_in_threadpool(_read_master_card_by_id, card_id, lines)
            if full_card is None:
                return {"ok": False, "total_cards": total_count, "card": None,
                        "message": "知识库里没有找到这张卡，可能已被更新或移除。"}
            return {"ok": True, "total_cards": total_count, "card": full_card}

        cards = await run_in_threadpool(
            _match_master_cards, q or "", genre, limit, card_type, lines
        )
        return {
            "ok": True,
            "total_cards": total_count,
            "matched_cards": [c.model_dump() for c in cards],
        }

    @router.post(
        "/generate-synopsis",
        response_model=ProjectGenerateSynopsisResponse,
    )
    async def generate_synopsis_free(
        request: ProjectGenerateSynopsisRequest,
    ) -> ProjectGenerateSynopsisResponse:
        """根据书名、题材和核心关键词，一键生成 3 套风格各异的爆款网文简介。"""
        title = request.title.strip()
        genre = (request.genre or "玄幻修仙").strip()
        candidates, generate_message = await run_in_threadpool(
            _generate_synopsis_candidates,
            title,
            genre,
            request.keywords,
        )
        if not candidates:
            return ProjectGenerateSynopsisResponse(
                ok=False,
                title=title,
                genre=genre,
                candidates=[],
                message=generate_message or "简介生成失败，请重试。",
            )
        return ProjectGenerateSynopsisResponse(
            ok=True,
            title=title,
            genre=genre,
            candidates=candidates,
            message=None,
        )

    @router.post(
        "/{project_id}/generate-synopsis",
        response_model=ProjectGenerateSynopsisResponse,
    )
    async def generate_synopsis_for_project(
        project_id: str,
        request: ProjectGenerateSynopsisRequest,
    ) -> ProjectGenerateSynopsisResponse:
        """为已绑定的作品生成爆款简介。"""
        binding = await run_in_threadpool(require_binding, project_id)
        title = request.title.strip() if request.title.strip() else binding.display_name
        genre = (request.genre or "玄幻修仙").strip()
        candidates, generate_message = await run_in_threadpool(
            _generate_synopsis_candidates,
            title,
            genre,
            request.keywords,
        )
        if not candidates:
            return ProjectGenerateSynopsisResponse(
                ok=False,
                title=title,
                genre=genre,
                candidates=[],
                message=generate_message or "简介生成失败，请重试。",
            )
        return ProjectGenerateSynopsisResponse(
            ok=True,
            title=title,
            genre=genre,
            candidates=candidates,
            message=None,
        )

    @router.post("/{project_id}/save-synopsis")
    async def save_project_synopsis(
        project_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """将选定的爆款简介保存进这本书的说明：新书是《这本书.md》的「现在的想法」，
        老书仍是 01_项目定位/01_项目卡片.md 的「核心立意与简介」。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        card_file = root_path / "01_项目定位" / "01_项目卡片.md"
        synopsis_text = str(payload.get("synopsis", "") or "").strip()
        book_card = root_path / "这本书.md"

        if book_card.exists() and not card_file.exists():
            old_content = book_card.read_text(encoding="utf-8")
            heading = "## 现在的想法"
            if heading in old_content:
                head, rest = old_content.split(heading, 1)
                following = re.split(r"\n(?=#{1,2} )", rest.lstrip("\n"), maxsplit=1)
                tail = ("\n" + following[1]) if len(following) > 1 else "\n"
                new_content = f"{head}{heading}\n\n{synopsis_text}\n{tail}"
            else:
                new_content = f"{old_content.rstrip()}\n\n{heading}\n\n{synopsis_text}\n"
            book_card.write_text(new_content, encoding="utf-8")
            return {"ok": True, "project_id": project_id}

        if not card_file.exists():
            card_file.parent.mkdir(parents=True, exist_ok=True)
            card_file.write_text(
                f"# 《{binding.display_name}》项目卡片\n\n## 核心立意与简介\n{synopsis_text}\n",
                encoding="utf-8",
            )
        else:
            old_content = card_file.read_text(encoding="utf-8")
            if "## 核心立意与简介" in old_content:
                parts = old_content.split("## 核心立意与简介", 1)
                header = parts[0]
                new_content = f"{header}## 核心立意与简介\n\n{synopsis_text}\n"
            else:
                new_content = f"{old_content}\n\n## 核心立意与简介\n\n{synopsis_text}\n"
            card_file.write_text(new_content, encoding="utf-8")

        return {"ok": True, "project_id": project_id}

    _EXPORT_FORMATS: dict[str, tuple[str, str]] = {
        "txt": ("txt", "text/plain; charset=utf-8"),
        "epub": ("epub", "application/epub+zip"),
        "docx": (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
    }

    @router.get("/{project_id}/export-check")
    async def export_check(project_id: str) -> dict[str, object]:
        """导出前的完稿体检 + 全书简明总览。

        和导出共用 collect_book 这条取章路径：空章、读不出、不是正文的
        章节进 issues；另外补上断号（缺章）和重号的检查。总览给作者看
        每卷每章的字数，不是工程报告。
        """
        binding = await run_in_threadpool(require_binding, project_id)
        inspection = await inspect_root(binding.root_path)
        book = await run_in_threadpool(
            novel_export_service.collect_book,
            inspection,
            Path(binding.root_path),
            binding.display_name,
        )

        issues: list[dict[str, str]] = [
            {"kind": "skipped", "detail": item} for item in book.skipped
        ]
        chapters_payload: list[dict[str, object]] = []
        numbers: list[int] = []
        total_chars = 0
        for volume in book.volumes:
            for chapter in volume.chapters:
                chars = sum(len(text) for text in chapter.paragraphs)
                numbers.append(chapter.number)
                total_chars += chars
                chapters_payload.append(
                    {
                        "number": chapter.number,
                        "title": chapter.title,
                        "chars": chars,
                        "volume": volume.heading,
                    }
                )
        duplicates = sorted(
            {number for number in numbers if numbers.count(number) > 1}
        )
        for number in duplicates:
            issues.append(
                {"kind": "duplicate", "detail": f"第{number}章出现了不止一次"}
            )
        if numbers:
            expected = set(range(min(numbers), max(numbers) + 1))
            for missing in sorted(expected - set(numbers)):
                issues.append(
                    {"kind": "gap", "detail": f"第{missing}章缺失（{min(numbers)}–{max(numbers)} 中间断档）"}
                )
        short_chapters = [
            item for item in chapters_payload if int(item["chars"]) < 50
        ]
        for item in short_chapters[:20]:
            issues.append(
                {
                    "kind": "tiny",
                    "detail": f"第{item['number']}章只有 {item['chars']} 字，可能是占位空章",
                }
            )

        return {
            "schema": "zhimeng.export-check.v1",
            "title": binding.display_name,
            "chapter_count": len(chapters_payload),
            "total_chars": total_chars,
            "volume_count": len(book.volumes),
            "has_volumes": book.has_real_volumes,
            "chapters": chapters_payload,
            "issues": issues,
        }

    async def _build_export(project_id: str, fmt: str) -> Response:
        """三种格式共用一条取章路径，见 novel_export_service。

        旧实现直接读 ch.title / vol_obj.title，而 ProjectChapter/ProjectVolume
        都没有 title 字段（章名只在文件名里），所以任何有分卷的书导出都 500。
        """
        binding = await run_in_threadpool(require_binding, project_id)
        inspection = await inspect_root(binding.root_path)
        book = await run_in_threadpool(
            novel_export_service.collect_book,
            inspection,
            Path(binding.root_path),
            binding.display_name,
        )
        if not book.chapters:
            raise HTTPException(status_code=409, detail="这本书还没有正文，导不出东西来")

        suffix, media_type = _EXPORT_FORMATS[fmt]
        if fmt == "txt":
            payload = novel_export_service.render_txt(book).encode("utf-8")
        elif fmt == "epub":
            payload = novel_export_service.render_epub(book, project_id)
        else:
            payload = novel_export_service.render_docx(book)

        filename = f"{binding.display_name}.{suffix}"
        encoded_filename = urllib.parse.quote(filename)
        headers = {
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{encoded_filename}"
            ),
            "X-Zhimeng-Chapters-Exported": str(len(book.chapters)),
        }
        if book.skipped:
            # 缺章不能只靠作者翻成品发现。断章要等贴到平台才看出来就太晚了。
            headers["X-Zhimeng-Chapters-Skipped"] = str(len(book.skipped))
            headers["X-Zhimeng-Skipped-Detail"] = urllib.parse.quote(
                "; ".join(book.skipped[:20])
            )
        return Response(
            content=payload,
            media_type=media_type,
            headers=headers,
        )

    @router.get("/{project_id}/export-txt")
    async def export_project_txt(project_id: str) -> Response:
        """导出整部小说为纯文本，适合直接贴到网文平台。"""
        return await _build_export(project_id, "txt")

    @router.get("/{project_id}/export-epub")
    async def export_project_epub(project_id: str) -> Response:
        """导出 EPUB：带目录，手机阅读器直接能看。"""
        return await _build_export(project_id, "epub")

    @router.get("/{project_id}/export-docx")
    async def export_project_docx(project_id: str) -> Response:
        """导出 Word：标题用 Heading 1，目录是可点击的内部链接，不用按 F9。"""
        return await _build_export(project_id, "docx")

    @router.post("/{project_id}/create-asset")
    async def create_project_asset(
        project_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """在作品资料库中新建人物卡、世界观设定或大纲文件。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        category = str(payload.get("category", "characters"))
        raw_name = str(payload.get("name", "") or "").strip()
        if not raw_name:
            raise HTTPException(status_code=400, detail="请先填写资料名称")
        safe_name = re.sub(r'[\\/:*?"<>|]+', "_", raw_name).strip(" ._")
        if not safe_name:
            raise HTTPException(status_code=400, detail="资料名称不能只包含特殊符号")
        content = str(payload.get("content", "") or "")

        if category == "characters":
            folder = root_path / "03_人物设定"
        elif category == "world":
            folder = root_path / "02_世界观设定"
        elif category == "outlines":
            folder = root_path / "04_剧情大纲"
        else:
            raise HTTPException(status_code=400, detail="不支持这个资料分类")

        folder.mkdir(parents=True, exist_ok=True)
        target_file = folder / f"{safe_name}.md"
        if target_file.exists():
            raise HTTPException(status_code=409, detail=f"已经有一份同名资料：{safe_name}")
        target_file.write_text(content, encoding="utf-8")
        return {"ok": True, "relative_path": str(target_file.relative_to(root_path))}

    @router.put("/{project_id}/update-asset")
    async def update_project_asset(
        project_id: str,
        payload: dict[str, str],
    ) -> dict[str, object]:
        """更新已有设定文件正文。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        relative_path = payload.get("relative_path", "").strip()
        content = payload.get("content", "")

        if not relative_path:
            raise HTTPException(status_code=400, detail="relative_path is required")

        target_file = (root_path / relative_path).resolve()
        if not target_file.is_relative_to(root_path.resolve()):
            raise HTTPException(status_code=403, detail="Invalid file path")

        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text(content, encoding="utf-8")
        return {"ok": True, "relative_path": relative_path}

    @router.get("/{project_id}/read-asset")
    async def read_project_asset(
        project_id: str,
        relative_path: str,
    ) -> dict[str, object]:
        """读取指定相对路径的设定或文件内容。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        if not relative_path:
            raise HTTPException(status_code=400, detail="relative_path is required")

        target_file = (root_path / relative_path).resolve()
        if not target_file.is_relative_to(root_path.resolve()):
            raise HTTPException(status_code=403, detail="Invalid file path")

        if not target_file.exists() or not target_file.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        content = target_file.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "relative_path": relative_path,
            "content": content,
        }

    def _call_llm_chat(
        system_prompt: str,
        user_prompt: str,
        timeout: float = 12.0,
        diag: dict[str, object] | None = None,
    ) -> str | None:
        """调模型取文本。

        失败原因一律写进 diag，由上层如实转告作者。
        以前这里所有失败都只 return None，界面统一显示「模型超时或返回为空」，
        把「没配 Key」「中转站 500」「模型没按格式答」三件完全不同的事说成同一件，
        作者照提示去换模型永远修不好。
        2026-09-12 收编进规范 Provider 链路：协议/认证/接受名单/重试/熔断与作者设置一致。
        per-call timeout 保留在签名里（诊断口径不变），实际请求超时走渠道配置。
        """
        def _note(reason: str, **extra: object) -> None:
            if diag is not None:
                diag["reason"] = reason
                diag.update(extra)

        if provider_resolver is None:
            _note("no_provider")
            return None
        try:
            provider = provider_resolver()
        except Exception as exc:  # noqa: BLE001 - 配置问题记进诊断
            _note("provider_config_error", detail=f"{type(exc).__name__}: {exc}"[:300])
            return None
        if provider is None:
            _note("no_provider")
            return None
        effective = provider_settings.effective() if provider_settings else None
        _note("pending", model=(effective.model if effective else "") or "", timeout_seconds=timeout)

        try:
            raw_text = _run_provider_chat_text(
                provider,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except RuntimeError as exc:
            _note("provider_error", detail=str(exc)[:300])
            return None
        if raw_text.startswith("```"):
            lines = raw_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
        if not raw_text:
            _note("model_returned_empty")
            return None
        _note("model_replied", raw_len=len(raw_text), raw_head=raw_text[:200])
        return raw_text

    def _lenient_json_object(text: str) -> object | None:
        """模型经常在 JSON 前后加一句解释或 ``` 围栏，逐层退让地试着解析。

        原来只处理「围栏正好在第一个字符」这一种，模型写「好的，结果如下：```json」就全盘失败。
        """
        candidate = text.strip()
        if not candidate:
            return None
        attempts = [candidate]
        fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.S)
        if fence:
            attempts.append(fence.group(1).strip())
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            attempts.append(candidate[start : end + 1])
        for attempt in attempts:
            try:
                return json.loads(attempt)
            except Exception:
                continue
        return None

    def _collect_llm_assets(
        raw_llm: str | None,
        diag: dict[str, object] | None = None,
    ) -> list[dict[str, str]]:
        """把模型输出解析成资料候选。四类资料（人物/世界观/文风/大纲）共用这一份，不再复制四遍。"""
        if not raw_llm:
            return []
        parsed = _lenient_json_object(raw_llm)
        if parsed is None:
            if diag is not None:
                diag["reason"] = "model_output_not_json"
            return []
        if not isinstance(parsed, dict) or not isinstance(parsed.get("assets"), list):
            if diag is not None:
                diag["reason"] = "model_output_missing_assets"
            return []
        collected: list[dict[str, str]] = []
        for item in parsed["assets"]:
            if isinstance(item, dict) and "name" in item and "content" in item:
                name = str(item["name"]).strip()
                content = str(item["content"]).strip()
                if name and content:
                    collected.append({"name": name, "content": content})
        if not collected and diag is not None:
            diag["reason"] = "model_output_assets_empty"
        return collected

    def _asset_failure_message(diag: dict[str, object]) -> str:
        """把机器原因翻成作者能照着动手的一句话。别再让作者去『换个模型』试运气。"""
        reason = str(diag.get("reason") or "unknown")
        head = str(diag.get("raw_head") or "").replace("\n", " ")[:120]
        fed = (
            f"（系统这次喂给模型 {diag.get('chapters_with_text')}/{diag.get('chapters_scanned')} 章正文，"
            f"共 {diag.get('prose_chars')} 字）"
            if "prose_chars" in diag else ""
        )
        if diag.get("chapters_with_text") == 0:
            return f"系统一章正文都没读到，所以模型无从整理。请确认这本书的正文文件在不在。{fed}"
        if reason == "no_provider":
            return "还没配好模型。请点左边「设置」，填写地址和 Key 并选一个模型，再回来重试。"
        if reason == "http_error":
            return (
                f"模型服务返回错误 HTTP {diag.get('status')}，不是超时。"
                f"服务器原话：{str(diag.get('detail') or '')[:120]}"
            )
        if reason == "timeout":
            return f"等了 {diag.get('timeout_seconds')} 秒，模型服务没回话。多半是网络或中转站慢，可以再试一次。"
        if reason == "network_error":
            return f"连不上模型服务：{str(diag.get('detail') or '')[:120]}"
        if reason == "bad_response_shape":
            return f"模型服务回了内容，但结构不对（取不到回答正文）。原始返回：{str(diag.get('detail') or '')[:120]}"
        if reason == "model_returned_empty":
            return "模型接通了，但回了空内容。可以再试一次；连续为空说明这个模型不适合做资料整理。"
        if reason == "model_output_not_json":
            return (
                f"模型答了 {diag.get('raw_len')} 个字，但没按要求给 JSON，所以整理不出资料。"
                f"它开头这样写的：{head}"
            )
        if reason == "model_output_missing_assets":
            return f"模型给了 JSON，但里面没有 assets 列表。它开头这样写的：{head}"
        if reason == "model_output_assets_empty":
            return f"模型读完正文后没提出任何人物，返回了空列表。{fed} 它的原话：{head}"
        return "整理没有产出内容，原因未能识别。请把这句话连同刚才的操作告诉开发者。"

    @router.post("/{project_id}/ai-extract-assets")
    async def ai_extract_assets(
        project_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """AI 根据全书正文提取人物档案、世界观设定或主线大纲。不直接落盘：内容作为待审阅候选返回，由前端走 ChangeSet 审阅通道，作者接受后才写入。"""
        binding = await run_in_threadpool(require_binding, project_id)
        inspection = await inspect_root(binding.root_path)
        root_path = Path(binding.root_path)
        category = str(payload.get("category", "characters"))

        # 完整遍历章节文件，再为模型压成每章的首尾证据，避免只读前六章。
        sorted_chapters = sorted(inspection.chapters, key=lambda c: c.number)
        sample_prose_parts: list[str] = []
        # 每一章为什么被跳过都要留痕。原来这里是 except Exception: pass，
        # 结果 ch.title 这个根本不存在的字段抛 AttributeError，被一口吞掉，
        # 于是每章都跳过、正文永远为空、模型永远回空列表，界面却说是「模型超时」。
        prose_skips: list[str] = []
        for ch in sorted_chapters:
            # 只认正文文件：file_ids 里还混着章节审稿报告和章纲控制卡，
            # 取 [0] 是靠运气（第 4 章正好正文在前，第 5 章就会把控制卡当正文喂进去）。
            prose_ids = list(getattr(ch, "prose_file_ids", None) or [])
            if not prose_ids:
                fallback = getattr(ch, "file_ids", None) or []
                prose_ids = list(fallback[:1]) if fallback else (
                    [getattr(ch, "file_id")] if getattr(ch, "file_id", None) else []
                )
            ch_file_id = prose_ids[0] if prose_ids else None
            file_obj = next((f for f in inspection.files if f.file_id == ch_file_id), None) if ch_file_id else None
            if file_obj is None:
                prose_skips.append(f"第{ch.number}章：没有对应的正文文件")
                continue
            if getattr(file_obj, "kind", "prose") != "prose":
                # 第 5 章只有章纲控制卡、还没写正文。把控制卡当正文喂给模型，
                # 它会「整理」出大纲里计划好、但正文里根本还没出场的人物。
                prose_skips.append(f"第{ch.number}章：只有 {file_obj.relative_path}，还没有正文")
                continue
            ch_path = root_path / file_obj.relative_path
            if not ch_path.exists():
                prose_skips.append(f"第{ch.number}章：文件不在磁盘上 {file_obj.relative_path}")
                continue
            try:
                text = ch_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                prose_skips.append(f"第{ch.number}章：读不出来 {type(exc).__name__}: {exc}")
                continue
            if not text:
                prose_skips.append(f"第{ch.number}章：正文是空的")
                continue
            if len(text) > 1400:
                text = f"{text[:700]}\n……本章中段省略，已读到本章结尾……\n{text[-700:]}"
            chapter_title = getattr(ch, "title", None) or Path(file_obj.relative_path).stem
            sample_prose_parts.append(f"第{ch.number}章 {chapter_title}\n{text}")

        if len(sample_prose_parts) > 40:
            # 长篇按全书位置均匀取 40 章，避免 60k 字预算被开头章节独占。
            selected_indexes = {
                int(index * (len(sample_prose_parts) - 1) / 39)
                for index in range(40)
            }
            sample_prose_parts = [
                part for index, part in enumerate(sample_prose_parts)
                if index in selected_indexes
            ]
        combined_prose = "\n\n".join(sample_prose_parts)[:60000]
        if not combined_prose:
            combined_prose = f"作品名：《{binding.display_name}》，暂无正文。"
        evidence_note = (
            f"已扫描全书 {len(sorted_chapters)} 章；从开头、中段到结尾均匀选取章节首尾作为对照证据。"
            if sorted_chapters else "目前没有可读取的正文。"
        )
        existing_reference_parts: list[str] = []
        if category == "outlines":
            for file_obj in inspection.files:
                relative = str(file_obj.relative_path).replace("\\", "/")
                if not (
                    relative.startswith("04_剧情大纲/")
                    or "章纲" in relative
                    or "伏笔" in relative
                ):
                    continue
                reference_path = root_path / file_obj.relative_path
                if not reference_path.exists() or not reference_path.is_file():
                    continue
                try:
                    reference_text = reference_path.read_text(encoding="utf-8", errors="replace").strip()
                except Exception:
                    continue
                if reference_text:
                    existing_reference_parts.append(
                        f"【已有资料：{relative}】\n{reference_text[:5000]}"
                    )
        existing_reference = "\n\n".join(existing_reference_parts)[:30000]
        if existing_reference:
            evidence_note += " 已同时读取现有大纲、章纲和伏笔文件，会优先对照并补缺。"
        elif category == "outlines":
            evidence_note += " 当前没有可对照的现有大纲、章纲或伏笔文件。"

        generated_assets: list[dict[str, str]] = []
        # 模型这一趟到底怎么了，如实记下来，失败时原样告诉作者。
        llm_diag: dict[str, object] = {
            "chapters_scanned": len(sorted_chapters),
            "chapters_with_text": len(sample_prose_parts),
            "prose_chars": len(combined_prose),
        }
        if prose_skips:
            llm_diag["prose_skips"] = prose_skips[:20]
        # 正文对照最长 60000 字，30 秒必然把长篇误判成超时；
        # 前端这个请求给了 120 秒，后端留到 100 秒仍在其内。
        llm_timeout = 100.0

        if category == "characters":
            folder = root_path / "03_人物设定"
            system_prompt = (
                "你是作者的小说资料整理助手。只根据正文中已经出现的信息，整理关键人物；不确定的内容标成待确认，不要擅自补修为、能力、关系或性格。\n"
                "输出格式必须为合法 JSON，不要包含任何 markdown 代码块标记：\n"
                '{\n  "assets": [\n    {\n      "name": "人物姓名",\n      "content": "# 人物姓名\\n\\n正文已经写明的身份、性格、目标、关系和当前状态。\\n\\n待确认：正文没有说清的地方。\\n"\n    }\n  ]\n}'
            )
            raw_llm = _call_llm_chat(system_prompt, f"作品名：《{binding.display_name}》\n{evidence_note}\n\n正文对照：\n{combined_prose}", timeout=llm_timeout, diag=llm_diag)
            generated_assets = _collect_llm_assets(raw_llm, llm_diag)

        elif category == "world":
            folder = root_path / "02_世界观设定"
            system_prompt = (
                "你是作者的小说资料整理助手。只根据正文中已经出现的信息，整理地点、势力、能力、物品和世界规则；不要把所有题材套成玄幻或修仙，也不要补正文没有的等级。\n"
                "输出格式必须为合法 JSON，不要包含任何 markdown 代码块标记：\n"
                '{\n  "assets": [\n    {\n      "name": "设定名称",\n      "content": "# 设定名称\\n\\n正文已经写明的规则、作用、限制和相关人物。\\n\\n待确认：正文没有说清的地方。\\n"\n    }\n  ]\n}'
            )
            raw_llm = _call_llm_chat(system_prompt, f"作品名：《{binding.display_name}》\n{evidence_note}\n\n正文对照：\n{combined_prose}", timeout=llm_timeout, diag=llm_diag)
            generated_assets = _collect_llm_assets(raw_llm, llm_diag)

        elif category == "style":
            folder = root_path / "06_写作规范"
            style_preset = str(payload.get("style_preset") or "").strip()
            raw_rules = payload.get("style_rules")
            style_rules = [str(rule).strip() for rule in raw_rules if str(rule).strip()] if isinstance(raw_rules, list) else []
            style_note = str(payload.get("style_note") or "").strip()
            requested_rules = "\n".join(f"- {rule}" for rule in style_rules[:12]) or "作者没有勾选固定规则"
            system_prompt = (
                "你是作者的小说文风整理助手。先尊重作者明确说过的要求，再从已有正文归纳实际写法。不要默认短句、爽文、平台套路、每章钩子或固定平均句长；长短句和段落应服从场景。\n"
                "只生成一份可编辑的《这本书怎么写》。区分作者已经确定的写法、从正文观察到的倾向和仍待作者决定的地方，不要把观察当成硬性禁令。\n"
                "输出格式必须为合法 JSON，不要包含任何 markdown 代码块标记：\n"
                '{\n  "assets": [\n    {\n      "name": "这本书怎么写",\n      "content": "# 这本书怎么写\\n\\n## 作者已经确定\\n按作者输入的要求原样整理。\\n\\n## 从正文看到的写法\\n用正常段落说明语气、叙述距离、对白和节奏，不下武断结论。\\n\\n## 还要决定\\n列出真正影响后续写作、但目前没有答案的问题。\\n"\n    }\n  ]\n}'
            )
            raw_llm = _call_llm_chat(
                system_prompt,
                f"作品名：《{binding.display_name}》\n{evidence_note}\n作者选择的可选方向：{style_preset or '未选择'}\n作者勾选的写法：\n{requested_rules}\n作者自己写的要求：{style_note or '未填写'}\n\n正文对照：\n{combined_prose}",
                timeout=llm_timeout,
                diag=llm_diag,
            )
            generated_assets = _collect_llm_assets(raw_llm, llm_diag)

        else:  # outlines
            folder = root_path / "04_剧情大纲"
            system_prompt = (
                "你是作者的小说剧情整理助手。对照已有大纲、章纲、伏笔和正文，整理已经发生的主线、尚未解决的目标、转折和伏笔。不要把所有故事套成穿越、升级、打脸或黄金三章。\n"
                "输出格式必须为合法 JSON，不要包含任何 markdown 代码块标记：\n"
                '{\n  "assets": [\n    {\n      "name": "故事大纲",\n      "content": "# 故事大纲\\n\\n## 已经发生\\n按正文顺序整理关键事件。\\n\\n## 正在推进\\n写清人物当前目标和阻力。\\n\\n## 还没有解决\\n列出伏笔、承诺和待作者决定的方向。\\n"\n    }\n  ]\n}'
            )
            raw_llm = _call_llm_chat(
                system_prompt,
                f"作品名：《{binding.display_name}》\n{evidence_note}\n\n已有规划资料：\n{existing_reference or '（无）'}\n\n正文对照：\n{combined_prose}",
                timeout=llm_timeout,
                diag=llm_diag,
            )
            generated_assets = _collect_llm_assets(raw_llm, llm_diag)

        if not generated_assets:
            return {
                "ok": False,
                "category": category,
                "created_count": 0,
                "assets": [],
                "message": _asset_failure_message(llm_diag),
                "diagnostic": llm_diag,
            }

        # 不在这里建目录：审阅接受后 apply 会自己建；没接受就一个字节也不碰书。
        created_list = []
        skipped = []
        for item in generated_assets:
            safe_name = re.sub(r"[\/:*?\"<>|\.\.]", "_", item["name"].strip())[:40] or "未命名"
            stem = safe_name[:-3] if safe_name.endswith(".md") else safe_name
            file_path = folder / f"{stem}.md"
            # 不覆盖作者已有文件：同名时换后缀；再撞就跳过。
            # 不直接落盘：内容作为待审阅候选返回，由前端走 ChangeSet 审阅通道。
            if file_path.exists():
                alt = folder / f"{stem}_AI提取.md"
                if alt.exists():
                    skipped.append(stem)
                    continue
                file_path = alt
            created_list.append({
                "name": safe_name,
                "relative_path": file_path.relative_to(root_path).as_posix(),
                "content": item["content"],
            })

        message = f"AI 已扫描全书并整理出 {len(created_list)} 份档案，先给你预览；你确认后才会进入待审阅，接受后才会写入你的书。"
        if skipped:
            message += f" 另有 {len(skipped)} 份与现有文件重名，已跳过：{'、'.join(skipped)}。"
        return {
            "ok": True,
            "category": category,
            "created_count": len(created_list),
            "assets": created_list,
            "message": message,
        }

    @router.post("/{project_id}/ai-optimize-asset")
    async def ai_optimize_asset(
        project_id: str,
        payload: dict[str, str],
    ) -> dict[str, object]:
        """AI 针对单张人物卡、世界观设定或大纲进行智能润色、细节扩写与人设立体化优化。"""
        binding = await run_in_threadpool(require_binding, project_id)
        root_path = Path(binding.root_path)
        content = payload.get("content", "").strip()
        category = payload.get("category", "characters")
        instruction = payload.get("instruction", "按作者的要求修改这份资料").strip()

        system_prompt = (
            "你是作者的小说资料修改助手。只按作者提出的要求修改，不要擅自套用短句爽文、升级、打脸、修仙或其他题材模板。\n"
            f"作者的修改要求：【{instruction}】。\n"
            "直接输出修改后的完整资料正文，不要解释过程，不要用代码块包裹。"
        )
        user_prompt = f"作品名：《{binding.display_name}》\n类别：{category}\n\n当前档案内容：\n{content}"

        # 这是 _call_llm_chat 唯一一个不带 diag 的调用点，于是「没配 Key」「中转站 500」
        # 「超时」「模型只回了几个字」四件事都显示同一句话，作者无从判断该去设置页
        # 还是查网络还是重试。复用已经写好的诊断翻译。
        optimize_diag: dict[str, object] = {}
        optimized = _call_llm_chat(system_prompt, user_prompt, timeout=60.0, diag=optimize_diag)
        if not optimized or len(optimized) < 20:
            if optimized and len(optimized) < 20:
                optimize_diag["reason"] = "model_returned_too_short"
                optimize_diag["raw_head"] = optimized
            return {
                "ok": False,
                "content": "",
                "message": (
                    f"模型只回了 {len(optimized)} 个字，短得没法当成修改后的资料。可以再试一次。"
                    if optimized
                    else _asset_failure_message(optimize_diag)
                ),
                "diagnostic": optimize_diag,
            }

        return {
            "ok": True,
            "content": optimized,
            "message": "修改已经生成，原文件还没有变化。",
        }

    @router.post(
        "/import-text",
        response_model=TextProjectImportResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_text_project(
        request: TextProjectImportRequest,
    ) -> TextProjectImportResponse:
        if text_import_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TXT project import is unavailable",
            )
        try:
            imported = await run_in_threadpool(
                text_import_service.import_text,
                file_name=request.file_name,
                content=request.content,
            )
        except TextProjectImportError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="text_project_import_invalid",
                retryable=False,
            ) from exc
        except OSError as exc:
            raise ApiError(
                "The imported work could not be saved",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="text_project_import_unavailable",
                retryable=True,
            ) from exc

        inspection = await inspect_root(str(imported.root_path))
        return TextProjectImportResponse(
            source_file_name=imported.source_file_name,
            title=imported.title,
            chapter_count=imported.chapter_count,
            inspection=inspection,
        )

    @router.get("/{project_id}/inspect", response_model=ProjectInspectionResponse)
    async def inspect_bound_project(project_id: str) -> ProjectInspectionResponse:
        binding = await run_in_threadpool(require_binding, project_id)
        return await inspect_root(binding.root_path)

    @router.post("/files/read", response_model=ProjectFileContentResponse)
    async def read_project_file(
        request: ProjectFileReadRequest,
        response: Response,
    ) -> ProjectFileContentResponse:
        result = await read_root_file(request.root_path, request.file_id)
        response.headers["ETag"] = f'"{result.etag}"'
        return result

    @router.get(
        "/{project_id}/files/{file_id}",
        response_model=ProjectFileContentResponse,
    )
    async def read_bound_project_file(
        project_id: str,
        file_id: str,
        request: Request,
        response: Response,
    ) -> ProjectFileContentResponse | Response:
        binding = await run_in_threadpool(require_binding, project_id)
        result = await read_root_file(binding.root_path, file_id)
        quoted_etag = f'"{result.etag}"'
        if etag_matches(request.headers.get("If-None-Match"), result.etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": quoted_etag},
            )
        response.headers["ETag"] = quoted_etag
        return result

    return router
