from __future__ import annotations

import asyncio
import io
import json
import threading
import urllib.error
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

import zhimeng_runtime.project_api as project_api
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_reader import (
    SECTION_LABELS,
    ProjectChangedDuringReadError,
    ProjectReader,
)


def _create_project(root: Path) -> Path:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷文件夹"
    volume.mkdir()
    prose = volume / "CH001.md"
    prose.write_text("可编辑的第一章", encoding="utf-8")
    return prose


def _client(tmp_path: Path, trusted_root: Path) -> TestClient:
    return TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(trusted_root,),
                allowed_origins=(),
            )
        )
    )


def _provider_app(tmp_path: Path, trusted_root: Path):
    return create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(trusted_root,),
            allowed_origins=(),
            agent_provider_base_url="https://provider.test/v1",
            agent_provider_model="claude-test",
            agent_provider_api_key="test-secret",
            agent_provider_credential_reference="test:provider-key",
        )
    )


class _FakeProviderResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback

    def read(self) -> bytes:
        return self._body


def _fake_provider_chain(monkeypatch, reply_text: str) -> list[list[dict]]:
    """把规范 Provider 链路打桩到内存：真 resolver 照跑，只换最终的连接构造。

    2026-09-12 收编后，简介/开书企划/资料提取不再走 project_api 里的
    urllib（旧打桩点），改在这里钉住「调用必须经过 provider 链路」。
    """
    import zhimeng_runtime.app as app_module
    from zhimeng_runtime.agent_provider import ProviderTextDelta

    captured: list[list[dict]] = []

    class _FakeProvider:
        def stream_chat(self, messages, cancel_event=None, tools=()):
            captured.append([dict(item) for item in messages])

            async def gen():
                yield ProviderTextDelta(text=reply_text)

            return gen()

    def fake_build(runtime_settings, **kwargs):
        del runtime_settings, kwargs
        return _FakeProvider()

    monkeypatch.setattr(app_module, "build_agent_provider_from_connection", fake_build)
    return captured


def test_project_api_exposes_tree_and_etagged_text_content(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200
        payload = inspected.json()
        assert payload["schema"] == "zhimeng.project-inspection.v1"
        assert payload["project"]["display_name"] == "book"
        assert payload["project"]["revision"] == 1
        assert payload["volumes"][0]["chapter_ids"] == ["VOL001-CH001"]
        prose_record = next(item for item in payload["files"] if item["kind"] == "prose")
        project_id = payload["project"]["project_id"]

        bindings = client.get("/api/v1/projects")
        rebound = client.get(f"/api/v1/projects/{project_id}/inspect")

        content = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}"
        )
        etag = content.headers["etag"]
        not_modified = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}",
            headers={"If-None-Match": f"W/{etag}"},
        )
        missing_file = client.get(
            f"/api/v1/projects/{project_id}/files/file-{'0' * 32}"
        )

    assert bindings.status_code == 200
    assert bindings.json()["schema"] == "zhimeng.project-bindings.v1"
    assert bindings.json()["projects"][0]["project_id"] == project_id
    assert rebound.status_code == 200
    assert rebound.json()["project"]["revision"] == 1
    assert content.status_code == 200
    assert content.json()["content"] == prose.read_text(encoding="utf-8")
    assert content.json()["snapshot_revision"] == 1
    assert len(content.json()["etag"]) == 64
    assert content.headers["etag"] == f'"{content.json()["etag"]}"'
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == content.headers["etag"]
    assert missing_file.status_code == 404
    assert missing_file.json()["schema"] == "zhimeng.error.v1"


def test_project_revision_survives_runtime_restart_and_same_size_change(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as first_client:
        first = first_client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()

    original = prose.read_text(encoding="utf-8")
    replacement = "已改写的第一章"
    assert len(replacement.encode("utf-8")) == len(original.encode("utf-8"))
    prose.write_text(replacement, encoding="utf-8")

    project_id = first["project"]["project_id"]
    with _client(tmp_path, library) as restarted_client:
        before_refresh = restarted_client.get("/api/v1/projects").json()
        refreshed = restarted_client.get(
            f"/api/v1/projects/{project_id}/inspect"
        ).json()
        after_refresh = restarted_client.get("/api/v1/projects").json()

    assert before_refresh["projects"][0]["revision"] == 1
    assert refreshed["project"]["revision"] == 2
    assert refreshed["project"]["fingerprint"] != first["project"]["fingerprint"]
    assert after_refresh["projects"][0]["revision"] == 2


def test_file_etag_changes_when_snapshot_metadata_changes(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = snapshot["project"]["project_id"]
        prose_record = next(item for item in snapshot["files"] if item["kind"] == "prose")
        first = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}"
        )
        old_etag = first.headers["etag"]

        prose.rename(prose.with_name("CH001_定稿.md"))
        renamed = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}",
            headers={"If-None-Match": old_etag},
        )

    assert renamed.status_code == 200
    assert renamed.headers["etag"] != old_etag
    assert renamed.json()["snapshot_revision"] == 2
    assert renamed.json()["file"]["relative_path"].endswith("CH001_定稿.md")


def test_project_api_rejects_outside_root_and_requires_configuration(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    _create_project(outside)

    with _client(tmp_path, trusted) as client:
        rejected = client.post("/api/v1/projects/inspect", json={"root_path": str(outside)})
    assert rejected.status_code == 403
    assert rejected.json()["schema"] == "zhimeng.error.v1"

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "unconfigured.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        unavailable = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(outside)},
        )
    assert unavailable.status_code == 403


def test_text_import_creates_managed_project_and_splits_chapters(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    content = (
        "作品说明\n\n"
        "第一章 山门来客\n沈青云推开山门。\n\n"
        "第2章 青云玉\n柳青烟发现玉佩不见了。\n"
    )

    with TestClient(app) as client:
        imported = client.post(
            "/api/v1/projects/import-text",
            json={
                "schema": "zhimeng.text-project-import-request.v1",
                "file_name": "宗门新篇.txt",
                "content": content,
            },
        )
        assert imported.status_code == 201, imported.text
        payload = imported.json()
        inspection = payload["inspection"]
        listed = client.get("/api/v1/projects")

    assert payload["schema"] == "zhimeng.text-project-import-response.v1"
    assert payload["title"] == "宗门新篇"
    assert payload["chapter_count"] == 2
    assert inspection["project"]["display_name"] == "宗门新篇"
    assert inspection["project"]["root_path"].startswith(
        str((tmp_path / "author-projects").resolve())
    )
    assert len(inspection["chapters"]) == 2
    assert [chapter["number"] for chapter in inspection["chapters"]] == [1, 2]
    assert listed.status_code == 200
    assert [item["display_name"] for item in listed.json()["projects"]] == ["宗门新篇"]
    chapter_paths = [
        Path(item["absolute_path"])
        for item in inspection["files"]
        if item["kind"] == "prose"
    ]
    assert len(chapter_paths) == 2
    assert "作品说明" in chapter_paths[0].read_text(encoding="utf-8")
    assert "柳青烟发现玉佩不见了" in chapter_paths[1].read_text(encoding="utf-8")


def test_delete_keeps_shelf_binding_when_recycle_move_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = inspected["project"]["project_id"]

        def fail_move(*args, **kwargs):
            del args, kwargs
            raise OSError("模拟文件被占用")

        monkeypatch.setattr(project_api.shutil, "move", fail_move)
        response = client.delete(
            f"/api/v1/projects/{project_id}?delete_disk_files=true"
        )
        listed = client.get("/api/v1/projects")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["files_moved"] is False
    assert "仍保留在书架上" in payload["message"]
    assert project.is_dir()
    assert listed.status_code == 200
    assert [item["project_id"] for item in listed.json()["projects"]] == [project_id]


def test_create_asset_never_overwrites_an_existing_author_file(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = inspected["project"]["project_id"]
        first = client.post(
            f"/api/v1/projects/{project_id}/create-asset",
            json={"category": "characters", "name": "柳青烟", "content": "作者原文"},
        )
        second = client.post(
            f"/api/v1/projects/{project_id}/create-asset",
            json={"category": "characters", "name": "柳青烟", "content": "不该覆盖"},
        )
        unsupported = client.post(
            f"/api/v1/projects/{project_id}/create-asset",
            json={"category": "unknown", "name": "其他", "content": ""},
        )

    target = project / "03_人物设定" / "柳青烟.md"
    assert first.status_code == 200
    assert second.status_code == 409
    assert unsupported.status_code == 400
    assert target.read_text(encoding="utf-8") == "作者原文"


def test_style_extraction_uses_author_choices_without_hidden_short_style(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    captured = _fake_provider_chain(
        monkeypatch,
        json.dumps(
            {
                "assets": [{
                    "name": "这本书怎么写",
                    "content": "# 这本书怎么写\n\n段落按场景自然分。",
                }]
            },
            ensure_ascii=False,
        ),
    )
    app = _provider_app(tmp_path, library)
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        response = client.post(
            f"/api/v1/projects/{inspected['project']['project_id']}/ai-extract-assets",
            json={"category": "style", "style_note": "段落正常分，不要一行一句"},
        )

    assert response.status_code == 200
    assert response.json()["assets"][0]["relative_path"] == "06_写作规范/这本书怎么写.md"
    assert not (project / "06_写作规范" / "这本书怎么写.md").exists()
    provider_messages = captured[0]
    system_prompt = provider_messages[0]["content"]
    user_prompt = provider_messages[1]["content"]
    assert "不要默认短句" in system_prompt
    assert "番茄短句爽文" not in system_prompt
    assert "作者选择的可选方向：未选择" in user_prompt
    assert "段落正常分，不要一行一句" in user_prompt


def test_ai_asset_edit_returns_candidate_without_writing_the_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    target = project / "03_人物设定" / "柳青烟.md"
    target.write_text("作者原文", encoding="utf-8")

    _fake_provider_chain(
        monkeypatch,
        "修改候选：她不喜欢别人替她做决定，也不愿再重复过去的错误。",
    )
    app = _provider_app(tmp_path, library)
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        response = client.post(
            f"/api/v1/projects/{inspected['project']['project_id']}/ai-optimize-asset",
            json={
                "category": "characters",
                "relative_path": "03_人物设定/柳青烟.md",
                "content": "作者原文",
                "instruction": "补清楚她为什么拒绝",
            },
        )

    assert response.status_code == 200
    assert response.json()["content"] == "修改候选：她不喜欢别人替她做决定，也不愿再重复过去的错误。"
    assert "原文件还没有变化" in response.json()["message"]
    assert target.read_text(encoding="utf-8") == "作者原文"


def test_text_import_rejects_non_txt_and_empty_content(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        wrong_type = client.post(
            "/api/v1/projects/import-text",
            json={"file_name": "作品.docx", "content": "第一章"},
        )
        empty = client.post(
            "/api/v1/projects/import-text",
            json={"file_name": "作品.txt", "content": "   "},
        )

    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "text_project_import_invalid"
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "text_project_import_invalid"


def test_invalid_configured_root_does_not_kill_runtime_health(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-library"
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(missing_root,),
            allowed_origins=(),
        )
    )

    with TestClient(app) as client:
        live = client.get("/health/live")
        project = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(missing_root / "book")},
        )

    assert live.status_code == 200
    assert project.status_code == 503
    assert "does not resolve" in project.json()["error"]["message"]


def test_legacy_binding_without_fingerprint_is_listed_as_pending(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO project_binding(
                id, root_path, display_name, fingerprint, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, '', 4, ?, ?)
            """,
            (
                "project-0123456789abcdef01234567",
                "C:\\Novels\\Legacy",
                "Legacy",
                "2026-07-17T00:00:00Z",
                "2026-07-17T00:00:00Z",
            ),
        )
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=database_path,
            allowed_origins=(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 200
    assert response.json()["projects"][0]["fingerprint"] is None
    assert response.json()["projects"][0]["snapshot_ready"] is False
    assert response.json()["projects"][0]["revision"] == 4


def test_snapshot_change_returns_typed_retryable_conflict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    def changed(*args, **kwargs):
        del args, kwargs
        raise ProjectChangedDuringReadError("project changed during scan")

    monkeypatch.setattr(ProjectReader, "inspect", changed)
    with _client(tmp_path, library) as client:
        response = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_snapshot_changed"
    assert response.json()["error"]["retryable"] is True


def test_export_check_duplicate_and_scan_job(tmp_path: Path) -> None:
    """完稿体检、试验副本、扫描进度三个新端点在真实小书上一次跑通。"""
    trusted_root = tmp_path / "library"
    trusted_root.mkdir(parents=True)
    book_root = trusted_root / "测试书"
    project = _create_project(book_root)
    (trusted_root / "测试书" / "05_正文内容" / "第一卷文件夹" / "CH002.md").write_text(
        "第二章正文", encoding="utf-8"
    )
    client = _client(tmp_path, trusted_root)

    with client:
        inspected = client.post(
            "/api/v1/projects/inspect", json={"root_path": str(book_root)}
        )
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]

        # 体检：CH001 有正文、CH002 有正文；无缺章时 issues 为空。
        check = client.get(f"/api/v1/projects/{project_id}/export-check")
        assert check.status_code == 200, check.text
        payload = check.json()
        assert payload["schema"] == "zhimeng.export-check.v1"
        assert payload["chapter_count"] >= 1
        assert payload["total_chars"] > 0
        assert isinstance(payload["issues"], list)

        # 缺章可见：只导了部分章时，中间断档要报 gap。
        (trusted_root / "测试书" / "05_正文内容" / "第一卷文件夹" / "CH005.md").write_text(
            "第五章", encoding="utf-8"
        )
        check2 = client.get(f"/api/v1/projects/{project_id}/export-check").json()
        kinds = {issue["kind"] for issue in check2["issues"]}
        assert "gap" in kinds or "skipped" in kinds

        # 试验副本：原书不动，副本立刻可扫描上架。
        duplicate = client.post(f"/api/v1/projects/{project_id}/duplicate")
        assert duplicate.status_code == 200, duplicate.text
        duplicate_project = duplicate.json()["project"]
        assert duplicate_project["root_path"] != str(project)
        assert (trusted_root / "测试书-副本").is_dir()
        assert (book_root / "05_正文内容" / "第一卷文件夹" / "CH001.md").read_text(
            encoding="utf-8"
        ) == "可编辑的第一章"

        # 扫描任务：queued/running → done，进度字段如实。
        job = client.post(
            "/api/v1/projects/inspect-jobs", json={"root_path": str(book_root)}
        )
        assert job.status_code == 200, job.text
        job_id = job.json()["job_id"]
        import time as _time

        final = None
        for _ in range(100):
            state = client.get(f"/api/v1/projects/inspect-jobs/{job_id}").json()
            if state["status"] in {"done", "failed"}:
                final = state
                break
            _time.sleep(0.05)
        assert final is not None and final["status"] == "done", final
        assert final["result"]["project"]["project_id"] == project_id
        assert final["total"] >= final["scanned"] > 0


def test_cover_endpoint_returns_204_when_no_cover_file(tmp_path: Path) -> None:
    """没设封面就回 204 而不是 404：让 <img> onError 走生成色块，
    同时控制台不再刷 4 条 404 噪音挡住真正错误。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "no-cover-book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        bound = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = bound["project"]["project_id"]

        missing = client.get(f"/api/v1/projects/{project_id}/cover")
        assert missing.status_code == 204
        assert missing.content == b""

    # 设了封面就正常返回图片字节，不再是 204
    (project / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    with _client(tmp_path, library) as client:
        present = client.get(f"/api/v1/projects/{project_id}/cover")
        assert present.status_code == 200
        assert present.headers["content-type"] == "image/png"
        assert present.content.startswith(b"\x89PNG")



def test_run_provider_chat_text_collects_stream_and_translates_errors() -> None:
    """共享助手：收齐流式文本；Provider 错误翻成 RuntimeError 给调用方转人话。"""
    from zhimeng_runtime.agent_provider import AgentProviderError, ProviderTextDelta

    class _OkProvider:
        def stream_chat(self, messages, cancel_event=None, tools=()):
            del messages, cancel_event, tools

            async def gen():
                yield ProviderTextDelta(text="你好")
                yield ProviderTextDelta(text="，世界")

            return gen()

    text = project_api._run_provider_chat_text(_OkProvider(), [{"role": "user", "content": "hi"}])
    assert text == "你好，世界"

    class _FailProvider:
        def stream_chat(self, messages, cancel_event=None, tools=()):
            del messages, cancel_event, tools

            async def gen():
                raise AgentProviderError("上游断了", code="http_error", retryable=False)
                yield  # pragma: no cover

            return gen()

    try:
        project_api._run_provider_chat_text(_FailProvider(), [])
    except RuntimeError as exc:
        assert "模型通道" in str(exc)
    else:
        raise AssertionError("Provider 错误必须翻成 RuntimeError，不能吞掉")


def test_master_library_browse_filter_and_full_card(tmp_path: Path, monkeypatch) -> None:
    """写法参考浏览：空词按库序列卡、按类型筛、按 card_id 看全文——同一个 cards.jsonl 源。"""
    import zhimeng_runtime.skill_bundle as skill_bundle

    cards_root = tmp_path / "skills" / "novel-distillation" / "library"
    cards_root.mkdir(parents=True)
    cards_file = cards_root / "cards.jsonl"
    cards_file.write_text(
        "\n".join([
            json.dumps({
                "card_id": "card-trope-1",
                "title": "当众退婚",
                "card_type": "trope",
                "summary": "开局被退婚，当众立誓",
                "rule": "先抑后扬",
                "retrieval_tags": ["退婚", "打脸"],
            }, ensure_ascii=False),
            json.dumps({
                "card_id": "card-voice-1",
                "title": "白描短句声纹",
                "card_type": "voice",
                "summary": "一句一段，动词优先",
                "retrieval_tags": ["短句"],
            }, ensure_ascii=False),
            json.dumps({
                "card_id": "card-noise",
                "title": "不该出现的类型",
                "card_type": "unknown_type",
                "summary": "不在白名单里",
            }, ensure_ascii=False),
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_bundle, "repo_root", lambda: tmp_path)

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        # 空词浏览：按库序直接给（unknown_type 不在白名单，被滤掉）
        browse = client.get("/api/v1/projects/distillation/cards/master-library?limit=10").json()
        assert browse["ok"] is True
        assert [c["card_id"] for c in browse["matched_cards"]] == ["card-trope-1", "card-voice-1"]

        # 类型筛选
        voices = client.get("/api/v1/projects/distillation/cards/master-library?card_type=voice").json()
        assert [c["card_id"] for c in voices["matched_cards"]] == ["card-voice-1"]

        # card_id 看全文：完整字段原样返回
        detail = client.get("/api/v1/projects/distillation/cards/master-library?card_id=card-trope-1").json()
        assert detail["ok"] is True
        assert detail["card"]["rule"] == "先抑后扬"
        assert detail["card"]["retrieval_tags"] == ["退婚", "打脸"]

        # 不存在的卡：人话，不炸
        missing = client.get("/api/v1/projects/distillation/cards/master-library?card_id=nope").json()
        assert missing["ok"] is False
        assert "没有找到" in missing["message"]


def test_new_book_is_one_folder_and_one_card(tmp_path: Path) -> None:
    """2026-09-24 新默认书夹：只建《这本书.md》，正文 / 设定 / 大纲 / 素材谁用谁建，
    不再预铺 01–09 编号夹与九份模板文件（作者 2026-09-23「要更简单的书夹结构」）。"""
    library = tmp_path / "library"
    library.mkdir()

    with _client(tmp_path, library) as client:
        created = client.post(
            "/api/v1/projects/create",
            json={
                "title": "只剩七个人的破宗门",
                "summary": "我想写一个只剩七个人的破宗门。",
                "creation_mode": "agent",
            },
        )

    assert created.status_code == 201, created.text
    payload = created.json()
    root = Path(payload["project"]["root_path"])
    # 新书建在作者的书目根里，不是运行时托管目录
    assert root.resolve().is_relative_to(library.resolve())
    assert sorted(item.name for item in root.iterdir()) == ["这本书.md"]
    card = (root / "这本书.md").read_text(encoding="utf-8")
    assert card.startswith("# 《只剩七个人的破宗门》")
    assert "我想写一个只剩七个人的破宗门" in card
    assert "## 已经定下的事" in card and "## 这本书怎么写" in card
    assert "玄幻修仙" not in card and "短句爽文" not in card
    assert payload["project"]["creation_mode"] == "agent"

    with _client(tmp_path, library) as client:
        reopened = client.get(
            f"/api/v1/projects/{payload['project']['project_id']}/inspect"
        )
    assert reopened.status_code == 200
    assert reopened.json()["project"]["creation_mode"] == "agent"


def test_new_book_manual_mode_writes_style_choices_into_the_card(tmp_path: Path) -> None:
    """手动模式选的基调与写法写进《这本书.md》的「这本书怎么写」，不再另建文风文件与机器三表。"""
    library = tmp_path / "library"
    library.mkdir()

    with _client(tmp_path, library) as client:
        created = client.post(
            "/api/v1/projects/create",
            json={
                "title": "自定义文风的书",
                "creation_mode": "manual",
                "author": "某人",
                "style_preset": "克制、细密、留白多",
                "style_rules": ["人物情绪藏在动作里", "允许长短句自然交替"],
            },
        )

    assert created.status_code == 201, created.text
    root = Path(created.json()["project"]["root_path"])
    assert sorted(item.name for item in root.iterdir()) == ["这本书.md"]
    card = (root / "这本书.md").read_text(encoding="utf-8")
    assert "克制、细密、留白多" in card
    assert "人物情绪藏在动作里" in card
    assert "允许长短句自然交替" in card
    assert "- 作者：某人" in card
    assert "平均句长不超过" not in card
    assert "每章结尾必须" not in card


def test_unbinding_a_book_with_conversations_removes_it_from_the_shelf(tmp_path: Path) -> None:
    """书下面挂着对话（session → run → …多层外键）时，从书架移除以前会 500；
    现在顺着外键图递归清掉，文件夹一个字节不动（2026-09-24）。"""
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = inspected["project"]["project_id"]
        # 直接往库里建一条会话 + 一次 run（session → run 两层外键）；这个测试 app 没配模型，走不了接口。
        from zhimeng_runtime.agent_store import AgentStore
        from zhimeng_runtime.execution_protocol import (
            CapabilityName,
            CapabilityProfile,
            ExecutorIdentity,
            ExecutorKind,
        )

        agent_store = AgentStore(client.app.state.runtime.store)
        session = agent_store.create_session(
            project_id=project_id,
            title="会被一起清掉的对话",
            executor=ExecutorIdentity(
                executor_id="executor.unbind-test",
                kind=ExecutorKind.EMBEDDED,
                implementation="unbind-test",
                version="1",
                protocol_version="1",
                instance_id=None,
            ),
            capabilities=CapabilityProfile(
                protocol_version="1",
                names=[CapabilityName.STREAMING, CapabilityName.TOOL_CALLS],
                extensions={},
            ),
        )
        agent_store.create_run(
            session_id=session.session_id,
            message="随便说一句",
            expected_project_revision=inspected["project"]["revision"],
            expected_project_fingerprint=inspected["project"]["fingerprint"],
            active_chapter_id=None,
            idempotency_key="unbind-test-turn-0001",
        )
        response = client.delete(f"/api/v1/projects/{project_id}")
        listed = client.get("/api/v1/projects")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert [item["project_id"] for item in listed.json()["projects"]] == []
    assert project.is_dir()
