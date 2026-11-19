from __future__ import annotations

import asyncio

import json
import os
from pathlib import Path
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import tempfile
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from zhimeng_runtime import skill_bundle
from zhimeng_runtime import __version__
from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_broker import AgentEventBroker
from zhimeng_runtime.agent_provider import (
    AgentProvider,
    build_agent_provider_from_connection,
)
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_queue import AgentQueueService
from zhimeng_runtime.agent_queue_scheduler import AgentQueueScheduler
from zhimeng_runtime.agent_store import AgentStore, AgentStoreNotFound
from zhimeng_runtime.agent_tools import AgentToolExecutor
from zhimeng_runtime.runtime_maintenance import RuntimeMaintenance, RuntimeMaintenanceMiddleware
from zhimeng_runtime.mcp_api import create_mcp_router
from zhimeng_runtime.mcp_client_service import McpClientService
from zhimeng_runtime.mcp_settings import McpSettingsService
from zhimeng_runtime.mcp_tool_adapter import McpToolAdapter
from zhimeng_runtime.change_set_api import create_change_set_router
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.file_changes_api import create_file_changes_router
from zhimeng_runtime.context_state_api import create_context_state_router
from zhimeng_runtime.file_journal import FileChangeJournal
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings
from zhimeng_runtime.context_api import create_context_router
from zhimeng_runtime.context_service import (
    DEFAULT_MAX_CHARACTERS,
    DEFAULT_MAX_TOKENS,
    ContextService,
)
from zhimeng_runtime.model_context import resolve_context_window_spec
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import DatabaseHealth, SqliteStore
from zhimeng_runtime.distillation_adapter import (
    DistillationAdapterError,
    DistillationPreviewAdapter,
    build_distillation_adapter,
)
from zhimeng_runtime.distillation_api import create_distillation_router
from zhimeng_runtime.distillation_invocation_prepare import (
    SourceImportDistillationPrepareService,
)
from zhimeng_runtime.distillation_invocation_service import DistillationInvocationService
from zhimeng_runtime.distillation_invocation_store import DistillationInvocationStore
from zhimeng_runtime.errors import ApiError, ServiceUnavailableError, install_exception_handlers
from zhimeng_runtime.events import iter_runtime_events
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.knowledge_api import create_knowledge_router
from zhimeng_runtime.knowledge_service import KnowledgeService
from zhimeng_runtime.local_source_adapter import (
    LocalSourceAdapter,
    LocalSourceAdapterError,
    ManagedLocalSourceAdapter,
)
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillError,
    ManagedSkillService,
)
from zhimeng_runtime.backup_models import (
    RuntimeBackupListResponse,
    RuntimeBackupResponse,
    RuntimeRestoreRequest,
    RuntimeRestoreResponse,
)
from zhimeng_runtime.backup_service import (
    AutoBackupWorker,
    BackupConflictError,
    BackupError,
    BackupNotFoundError,
    BackupService,
    BackupStateError,
)
from zhimeng_runtime.diagnostics_models import DiagnosticsPackageResponse
from zhimeng_runtime.diagnostics_service import DiagnosticsService
from zhimeng_runtime.release_readiness_service import (
    ReleaseReadinessError,
    ReleaseReadinessService,
    ReleaseReadinessStateError,
)
from zhimeng_runtime.models import (
    DatabaseStatus,
    LiveResponse,
    ReadyResponse,
    RuntimeStatusResponse,
)
from zhimeng_runtime.project_api import create_project_router
from zhimeng_runtime.author_phrases_api import create_author_phrases_router
from zhimeng_runtime.user_memory_api import create_user_memory_router
from zhimeng_runtime.fs_browse_api import create_fs_router
from zhimeng_runtime.local_file_api import create_local_file_router
from zhimeng_runtime.user_skill_api import create_user_skill_router
from zhimeng_runtime.user_skill_folder_service import KNOWN_SKILL_SOURCES, UserSkillFolderService
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_locks import ProjectLockRegistry
from pydantic import BaseModel, Field

from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.project_reader import ProjectReader, ProjectReaderError
from zhimeng_runtime.text_project_import_service import TextProjectImportService
from zhimeng_runtime.ai_smell_service import AiSmellService
from zhimeng_runtime.knowledge_context_service import KnowledgeContextService
from zhimeng_runtime.provider_settings_api import create_provider_settings_router
from zhimeng_runtime.provider_settings_service import (
    ProviderSettings,
    ProviderSettingsReadError,
    ProviderSettingsService,
    resolve_model_connection,
)
from zhimeng_runtime.project_asset_api import create_project_asset_router
from zhimeng_runtime.longform_check_api import create_longform_check_router
from zhimeng_runtime.project_asset_service import ProjectAssetService
from zhimeng_runtime.search_api import create_search_router
from zhimeng_runtime.search_service import SearchService
from zhimeng_runtime.skill_api import create_skill_router
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.skill_registry import SkillRegistry, SkillRegistryError
from zhimeng_runtime.source_import_api import create_source_import_router
from zhimeng_runtime.source_import_service import SourceImportService
from zhimeng_runtime.source_upload_service import SourceUploadError, SourceUploadService
from zhimeng_runtime.tomato_skill_adapter import (
    TomatoAdapterError,
    TomatoSourceAdapter,
    build_tomato_skill_adapter,
)
from zhimeng_runtime.workflow_runner import WorkflowRunner


SERVICE_NAME = "zhimeng-writing-runtime"
_LOGGER = logging.getLogger(__name__)
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
BUILTIN_MANAGED_SKILL_PACKAGES = (
    "creation-suite-project-audit",
    "creation-suite-prewrite-gate",
    "creation-suite-handoff",
    "creation-suite-draft-gate",
    "creation-suite-writer-instruction",
    "novel-distillation-kb-handoff",
    "novel-kb-context-handoff",
    "four-skills-family-source-ingest",
    "four-skills-family-distillation",
    "four-skills-family-knowledge-context",
    "four-skills-family-creation-suite",
)


class RuntimeTrustedRootRequest(BaseModel):
    """POST /api/v1/runtime/trusted-roots 的请求体。

    必须放在模块顶层：本文件开头有 ``from __future__ import annotations``，
    路由函数的参数注解是字符串，FastAPI 只在模块全局里找它。定义在
    create_app 里面时它找不到，就把 ``request`` 当成了一个查询参数——
    2026-09-20 实测 POST 直接 422「query.request 缺失」。
    """

    path: str = Field(min_length=1, max_length=1024)


class RuntimeTrustedRootResponse(BaseModel):
    trusted_roots: list[str]
    persisted_roots: list[str]


@dataclass(slots=True)
class RuntimeContext:
    settings: RuntimeSettings
    store: SqliteStore
    started_at: datetime
    started_monotonic: float
    database_health: DatabaseHealth
    startup_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.startup_error is None and self.database_health.ready


def _recover_workflows_at_startup(
    agent_store: AgentStore,
    context_store: ContextStore,
    skill_executor: SkillExecutor | None,
) -> None:
    runner: WorkflowRunner | None = None
    executor_unavailable = False
    for run_id in agent_store.list_nonterminal_workflow_run_ids():
        try:
            agent_store.get_provider_io_boundary(run_id)
        except AgentStoreNotFound:
            pass
        else:
            agent_store.mark_interrupted(
                run_id,
                detail="Runtime restarted after Provider dispatch began",
            )
            continue

        run = agent_store.get_run(run_id)
        if run.status not in {
            RunStatus.QUEUED,
            RunStatus.STARTING,
            RunStatus.RUNNING,
            RunStatus.PAUSED,
        }:
            # Approval/cancellation states are not part of the fixed pre-Provider
            # workflow and remain owned by the generic interrupted-run pass.
            continue
        if skill_executor is None:
            if run.status is not RunStatus.PAUSED:
                agent_store.pause_workflow_for_restart(run_id)
            executor_unavailable = True
            continue

        if runner is None:
            runner = WorkflowRunner(agent_store, context_store, skill_executor)
        runner.reconcile_startup(run_id)
        if run.status is not RunStatus.PAUSED:
            agent_store.pause_workflow_for_restart(run_id)
    if executor_unavailable:
        _LOGGER.warning(
            "Managed Skill executor is unavailable; legacy workflows remain paused"
        )


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    source_import_adapter: TomatoSourceAdapter | None = None,
    local_source_adapter: LocalSourceAdapter | None = None,
    distillation_adapter: DistillationPreviewAdapter | None = None,
    agent_provider: AgentProvider | None = None,
) -> FastAPI:
    runtime_settings = settings or RuntimeSettings.from_environment()

    def _trusted_roots_state_path() -> Path:
        return runtime_settings.database_path.parent / "trusted-roots.json"

    def _load_persisted_trusted_roots() -> list[str]:
        """作者自助添加的受信根列表（信任端点落盘，重启后仍生效）。"""
        path = _trusted_roots_state_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LOGGER.warning("受信根持久化文件读取失败 %s: %s", path, exc)
            return []
        roots = data.get("trusted_roots") if isinstance(data, dict) else None
        if not isinstance(roots, list):
            return []
        return [str(root) for root in roots if str(root).strip()]

    def _persist_trusted_roots(roots: list[str]) -> None:
        """把作者自助添加的受信根写回 trusted-roots.json（临时文件 + 原子替换）。

        2026-09-19 端点上线时这个函数根本没写，POST 一到落盘就 NameError；
        `_load_persisted_trusted_roots` 读的正是这份文件，两边格式必须一致。
        """
        path = _trusted_roots_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema": "zhimeng.trusted-roots.v1", "trusted_roots": list(roots)},
            ensure_ascii=False,
            indent=2,
        )
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(payload + "\n", encoding="utf-8")
        try:
            replace_with_retry(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

    project_reader: ProjectReader | None = None
    project_reader_error: str | None = None
    project_kb_service: ProjectKbService | None = None
    project_kb_service_error: str | None = None
    skill_registry: SkillRegistry | None = None
    skill_registry_error: str | None = None
    managed_skill_service: ManagedSkillService | None = None
    managed_skill_service_error: str | None = None
    managed_skill_executor: SkillExecutor | None = None
    source_import_error: str | None = None
    distillation_error: str | None = None
    startup_configuration_error: str | None = None
    source_upload_service: SourceUploadService | None = None
    managed_project_root = (
        runtime_settings.database_path.parent / "author-projects"
    ).resolve(strict=False)
    text_import_service: TextProjectImportService | None = None
    try:
        managed_project_root.mkdir(parents=True, exist_ok=True)
        trusted_project_roots = list(runtime_settings.trusted_project_roots)
        if all(
            root.resolve(strict=False) != managed_project_root
            for root in trusted_project_roots
        ):
            trusted_project_roots.append(managed_project_root)
        project_reader = ProjectReader(tuple(trusted_project_roots))
        for persisted_root in _load_persisted_trusted_roots():
            try:
                project_reader.trust_root(persisted_root)
            except OSError as exc:
                _LOGGER.warning(
                    "加载持久化受信根失败 %s: %s", persisted_root, exc
                )
        text_import_service = TextProjectImportService(managed_project_root)
    except (OSError, ProjectReaderError, ValueError) as exc:
        project_reader_error = str(exc)
    if runtime_settings.trusted_skill_roots:
        try:
            skill_registry = SkillRegistry(runtime_settings.trusted_skill_roots)
        except SkillRegistryError as exc:
            skill_registry_error = str(exc)
    initial_health = DatabaseHealth(
        ready=False,
        journal_mode="unknown",
        schema_version=0,
    )
    runtime_state_root = runtime_settings.database_path.parent.resolve(strict=False)
    try:
        runtime_state_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Keep diagnostics and health endpoints available when the configured
        # database parent is not writable (for example, a file was supplied as
        # the parent). State services use an isolated temporary root only while
        # the database itself remains unavailable and readiness stays failed.
        startup_configuration_error = (
            f"Runtime state root is unavailable: {exc}"
        )
        runtime_state_root = (
            Path(tempfile.gettempdir())
            / f"zhimeng-runtime-unavailable-{uuid4().hex}"
        )
        runtime_state_root.mkdir(parents=True, exist_ok=True)
    context = RuntimeContext(
        settings=runtime_settings,
        store=SqliteStore(runtime_settings.database_path),
        started_at=datetime.now(UTC),
        started_monotonic=monotonic(),
        database_health=initial_health,
        startup_error=startup_configuration_error,
    )
    if project_reader is not None:
        def _pretrust_bound_project_roots() -> None:
            """把已经在书架上的项目目录预先授信。

            两个原来的毛病：
            1. 方法名写成 `store.list_bindings()`，SqliteStore 上并不存在这个方法，
               AttributeError 被外层 `except Exception: pass` 吞掉，整段一次都没执行过；
            2. 这段原本写在 create_app 里，而建表/迁移是在 lifespan 的
               `store.initialize()` 才做的 —— 顺序反了，新装或迁移过的库读不到表。
            现在放到 initialize() 之后调用。
            """
            try:
                bindings = context.store.list_project_bindings()
            except Exception as error:
                _LOGGER.warning(
                    "读取已绑定项目失败，本次没有预授信任何项目目录：%s: %s",
                    type(error).__name__,
                    error,
                )
                return
            for binding in bindings:
                try:
                    project_reader.trust_root(binding.root_path)
                except Exception as error:
                    # 单本书授信失败不该拖垮启动，但不能一声不响。
                    _LOGGER.warning(
                        "项目目录预授信失败 %s：%s: %s",
                        binding.root_path,
                        type(error).__name__,
                        error,
                    )
    else:
        def _pretrust_bound_project_roots() -> None:
            return

    provider_settings_service = ProviderSettingsService(
        runtime_state_root / "provider-settings.json",
        fallback=ProviderSettings(
            base_url=runtime_settings.agent_provider_base_url,
            model=runtime_settings.agent_provider_model,
            api_key=runtime_settings.agent_provider_api_key,
        ),
        fallback_credential_reference=(
            runtime_settings.agent_provider_credential_reference
        ),
    )
    # 技能文件夹：像 Codex/Claude 一样往文件夹里丢 SKILL.md 就上架，skill_* 工具翻文件，
    # 技能目录挂在 skill_read_file 的说明里。除了本软件自己的 skills/，默认也读本机别的 agent
    # 读的那几处（~/.codex/skills、~/.agents/skills、~/.claude/skills）——作者 9-26：「Claude 还有
    # 其他 agents 都会读取我 codex 的 skills，我们这个也可以的话…」。同名的以 skills/ 为准。
    # 测试和隔离实例用 ZHIMENG_USER_SKILL_EXTRA_ROOTS 换掉共享文件夹（分号分隔，设成空就不读）。
    user_skills_root_env = os.environ.get("ZHIMENG_USER_SKILLS_ROOT", "").strip()
    user_skills_root = (
        Path(user_skills_root_env).expanduser()
        if user_skills_root_env
        else skill_bundle.repo_root() / "skills"
    )
    # 9-28 起本机各家 agent 的技能文件夹都认（KNOWN_SKILL_SOURCES），设置页「技能库」一家一个开关；
    # Codex、通用 .agents、Claude 三家默认开（和 9-26 一样），别的默认关。给了环境变量就只读环境变量那几处。
    extra_roots_env = os.environ.get("ZHIMENG_USER_SKILL_EXTRA_ROOTS")
    user_skill_folder_service = (
        UserSkillFolderService(
            user_skills_root,
            runtime_state_root / "user-skills-state",
            sources=KNOWN_SKILL_SOURCES,
        )
        if extra_roots_env is None
        else UserSkillFolderService(
            user_skills_root,
            runtime_state_root / "user-skills-state",
            extra_roots=[Path(part.strip()).expanduser() for part in extra_roots_env.split(";") if part.strip()],
        )
    )
    four_skills_bundle_root = skill_bundle.execution_bundle_root(runtime_state_root)
    if runtime_settings.managed_skill_root is not None:
        try:
            managed_skill_service = ManagedSkillService(
                # Bundled packages are sources. Executable snapshots live beside
                # Runtime SQLite so execution never depends on the source checkout.
                context.store,
                (runtime_state_root / "managed-skills").resolve(),
                trusted_import_roots=runtime_settings.trusted_skill_roots,
                # Without this the contract registry falls back to guessing
                # <package>/../../../../packages/contracts, which only holds in a
                # source checkout; a portable bundle would fail startup with
                # "contracts_root must be an accessible directory".
                contracts_root=runtime_settings.skill_contracts_root,
            )
            managed_skill_executor = SkillExecutor(
                managed_skill_service,
                four_skills_bundle_root=four_skills_bundle_root,
                # Same reason as above: the fallback resolves relative to the
                # source checkout layout and misses inside a portable bundle.
                contracts_root=runtime_settings.skill_contracts_root,
            )
        except (ManagedSkillError, OSError, ValueError) as exc:
            managed_skill_service_error = str(exc)
    agent_provider_resolver = None
    if agent_provider is None:

        def resolve_agent_provider(
            provider_id: str | None = None,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> AgentProvider | None:
            try:
                profile, _profile_reference = provider_settings_service.resolve_profile(provider_id)
            except ProviderSettingsReadError as exc:
                raise ServiceUnavailableError(str(exc)) from None
            if profile is not None:
                # 模型级覆盖（base_url / 密钥 / 协议 / 输出上限等）在这里生效，
                # 模型级优先于渠道级。
                effective = resolve_model_connection(profile, model)
                # 模型可能使用独立密钥；运行身份必须属于实际发出的那把，而不是父服务的。
                credential_reference = provider_settings_service._credential_reference_from(
                    {"api_key": effective.api_key}, effective
                )
            else:
                try:
                    effective, credential_reference = provider_settings_service.resolve_agent_profile(provider_id)
                except ProviderSettingsReadError as exc:
                    raise ServiceUnavailableError(str(exc)) from None
                if model:
                    effective = replace(effective, model=model)
            return build_agent_provider_from_connection(
                runtime_settings,
                base_url=effective.base_url,
                model=effective.model,
                provider_id=effective.provider_id,
                api_key=effective.api_key,
                credential_reference=credential_reference,
                reasoning_effort=reasoning_effort or effective.reasoning_effort,
                wire_format=effective.wire_format,
                accepted_reported_models=effective.accepted_reported_models,
                auth_type=effective.auth_type,
                extra_headers=effective.extra_headers,
                request_timeout_seconds=effective.request_timeout_seconds,
                max_output_tokens=effective.max_output_tokens,
                max_retries=effective.max_retries,
                stream_idle_timeout_seconds=effective.stream_idle_timeout_seconds,
                max_concurrent_requests=effective.max_concurrent_requests,
                circuit_breaker_threshold=effective.circuit_breaker_threshold,
                circuit_breaker_cooldown_seconds=(
                    effective.circuit_breaker_cooldown_seconds
                ),
                context_window=effective.context_window,
            )

        agent_provider_resolver = resolve_agent_provider
        try:
            agent_provider = resolve_agent_provider()
        except ServiceUnavailableError as exc:
            # Keep history and settings reachable when this one resource is
            # unreadable. Dispatch re-reads it and reports the actual failure.
            _LOGGER.warning("模型配置未载入：%s", exc)
            agent_provider = None
    else:
        # 环境变量直配 provider 时上面那个分支不执行，resolve_agent_provider 名字不存在——
        # 2026-09-12 起路由构造处也会引用这个名字（project_api 收编），未定义会直接
        # UnboundLocalError 炸掉整个 create_app。这里补一个返回静态实例的 resolver，
        # 语义与原设计一致：env 直配模式下所有旁路调用都用这同一个实例。
        def resolve_agent_provider(
            provider_id: str | None = None,
            model: str | None = None,
            reasoning_effort: str | None = None,
        ) -> AgentProvider | None:
            del provider_id, model, reasoning_effort
            return agent_provider

        agent_provider_resolver = resolve_agent_provider
    agent_store = AgentStore(context.store)
    distillation_invocation_store = DistillationInvocationStore(context.store)
    distillation_invocation_service = DistillationInvocationService(
        invocation_store=distillation_invocation_store,
        provider=agent_provider,
        provider_resolver=agent_provider_resolver,
    )
    source_import_state_root = runtime_state_root / "source-imports"
    try:
        source_upload_service = SourceUploadService(
            runtime_state_root / "source-uploads"
        )
    except (OSError, SourceUploadError) as exc:
        source_import_error = str(exc)
    if source_import_adapter is None and runtime_settings.tomato_skill_root is not None:
        try:
            source_import_adapter = build_tomato_skill_adapter(
                runtime_settings,
                state_root=source_import_state_root,
            )
        except (OSError, TomatoAdapterError) as exc:
            source_import_error = str(exc)
    trusted_source_roots = list(runtime_settings.trusted_source_roots)
    if (
        source_upload_service is not None
        and source_upload_service.root not in trusted_source_roots
    ):
        trusted_source_roots.append(source_upload_service.root)
    if local_source_adapter is None and trusted_source_roots:
        try:
            local_source_adapter = ManagedLocalSourceAdapter(
                snapshot_root=(
                    DEFAULT_MANAGED_SKILL_ROOT / "local-source-acceptance"
                ),
                state_root=source_import_state_root,
                trusted_source_roots=tuple(trusted_source_roots),
                powershell_executable=runtime_settings.powershell_executable,
            )
        except (OSError, LocalSourceAdapterError) as exc:
            local_error = str(exc)
            source_import_error = (
                f"{source_import_error}; {local_error}"
                if source_import_error
                else local_error
            )
    if (
        distillation_adapter is None
        and runtime_settings.distillation_skill_root is not None
    ):
        try:
            distillation_adapter = build_distillation_adapter(runtime_settings)
        except (OSError, DistillationAdapterError) as exc:
            distillation_error = str(exc)
    external_knowledge_fingerprint_probe = (
        getattr(distillation_adapter, "observe_knowledge_fingerprint", None)
        if distillation_adapter is not None
        else None
    )
    native_knowledge_service = KnowledgeService(
        context.store,
        source_import_state_root,
    )
    if project_reader is not None:
        try:
            project_kb_service = ProjectKbService(
                context.store,
                project_reader,
                runtime_state_root / "project-kb",
            )
        except (OSError, ValueError) as exc:
            project_kb_service_error = str(exc)
    else:
        project_kb_service_error = project_reader_error or "Project Reader is disabled"

    def knowledge_fingerprint_probe(project_id: str | None = None) -> str:
        if native_knowledge_service.has_active_cards(project_id=project_id):
            return native_knowledge_service.observe_knowledge_fingerprint(project_id)
        if callable(external_knowledge_fingerprint_probe):
            return external_knowledge_fingerprint_probe()
        return native_knowledge_service.observe_knowledge_fingerprint(project_id)

    source_import_service = (
        SourceImportService(
            context.store,
            source_import_state_root,
            source_import_adapter,
            local_adapter=local_source_adapter,
        )
        if source_import_adapter is not None or local_source_adapter is not None
        else None
    )
    distillation_prepare_service = SourceImportDistillationPrepareService(
        database=context.store,
        source_state_root=source_import_state_root,
        invocation_store=distillation_invocation_store,
        knowledge_service=native_knowledge_service,
        managed_skill_service=managed_skill_service,
        provider=agent_provider,
        provider_resolver=agent_provider_resolver,
        project_kb_service=project_kb_service,
    )
    context_store = ContextStore(context.store)

    def knowledge_validator_resolver(
        project_id: str,
        context_pack_id: str,
        context_pack_hash: str,
        knowledge_fingerprint: str | None,
    ):
        route = context_store.get_nlcl_route(context_pack_id)
        if (
            route.project_id != project_id
            or route.context_pack_id != context_pack_id
            or route.context_pack_hash != context_pack_hash
            or route.knowledge_fingerprint != knowledge_fingerprint
        ):
            raise RuntimeError(
                "NLCL route does not match the reviewed post-draft Gate context"
            )
        return (
            route.route_receipt_id,
            route.receipt_hash,
            native_knowledge_service.build_gate_validator_bundle(project_id, route),
        )

    project_locks = ProjectLockRegistry()
    change_set_service = (
        ChangeSetService(
            project_reader,
            context.store,
            runtime_state_root / "change-sets",
            post_draft_gate_executor=managed_skill_executor,
            knowledge_fingerprint_probe=knowledge_fingerprint_probe,
            knowledge_validator_resolver=knowledge_validator_resolver,
        )
        if project_reader is not None
        else None
    )
    search_service = (
        SearchService(
            context.store,
            project_reader,
            native_knowledge_service,
            runtime_state_root / "project-search",
        )
        if project_reader is not None
        else None
    )
    # AI 味检测在这之前创建：Agent 提案和写章候选共用同一个实例。
    ai_smell_service = AiSmellService(runtime_state_root=runtime_state_root)
    mcp_settings = McpSettingsService(runtime_state_root / "mcp-servers.json")
    mcp_service = McpClientService(mcp_settings, runtime_state_root)
    mcp_adapter = McpToolAdapter(mcp_service)
    base_tool_executor = (
        AgentToolExecutor(
            project_reader,
            context.store,
            project_locks,
            change_set_service,
            search_service=search_service,
            ai_smell=ai_smell_service,
            user_skills=user_skill_folder_service,
            mcp_adapter=mcp_adapter,
        )
        if project_reader is not None and change_set_service is not None
        else None
    )
    agent_service = AgentService(
        agent_store,
        agent_provider,
        AgentEventBroker(),
        knowledge_fingerprint_probe=(
            knowledge_fingerprint_probe
            if callable(knowledge_fingerprint_probe)
            else None
        ),
        tool_executor=base_tool_executor,
        context_store=context_store,
        project_kb_service=project_kb_service,
        project_locks=project_locks,
        provider_resolver=agent_provider_resolver,
        provider_selector=agent_provider_resolver,
        user_skills=user_skill_folder_service,
        auto_memory=True,
    )
    agent_queue_service = AgentQueueService(agent_service)
    agent_queue_scheduler = AgentQueueScheduler(agent_queue_service)

    def _context_pack_budget() -> tuple[int, int]:
        # 资料包预算跟当前线路的上下文窗口档位走（设置页改了立刻生效）。
        try:
            effective, _ = provider_settings_service.resolve_agent_profile(None)
            spec = resolve_context_window_spec(getattr(effective, "context_window", None))
            return spec.pack_max_characters, spec.pack_max_tokens
        except Exception:
            return DEFAULT_MAX_CHARACTERS, DEFAULT_MAX_TOKENS

    context_service = (
        ContextService(
            context_store,
            agent_store,
            project_reader,
            project_locks,
            distillation_adapter,
            native_knowledge_service,
            project_kb_service,
            budget_resolver=_context_pack_budget,
        )
        if project_reader is not None
        else None
    )
    release_readiness_service = ReleaseReadinessService(
        state_root=runtime_state_root / "release-readiness",
    )
    project_asset_service = ProjectAssetService(
        state_root=runtime_state_root / "project-assets",
    )

    def resolve_project_root_for_assets(project_id: str):
        binding = context.store.get_project_binding(project_id)
        if binding is None:
            raise FileNotFoundError(f"project binding not found: {project_id}")
        return Path(binding.root_path)

    def resolve_project_revision(project_id: str) -> int | None:
        binding = context.store.get_project_binding(project_id)
        return binding.revision if binding is not None else None

    # 两个服务都通过 skill_bundle 解析随包脚本/知识库，必须拿到同一个 runtime 状态根，
    # 否则安装后释放到用户数据区的那份快照找不到，会静默掉回本机 .codex。
    # ai_smell_service 已在 Agent 工具执行器组装前创建（共用一个实例）。
    knowledge_context_service = KnowledgeContextService(
        runtime_state_root=runtime_state_root
    )
    maintenance = RuntimeMaintenance()
    auto_backup: AutoBackupWorker | None = None
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal managed_skill_service_error, auto_backup
        database_initialized = False
        try:
            context.database_health = context.store.initialize()
            database_initialized = True
            # 建表/迁移完成后才能读 project_binding，顺序不能反。
            _pretrust_bound_project_roots()
            agent_service.tool_executor = base_tool_executor
            app.state.skill_executor = None
            if (
                managed_skill_service is not None
                and managed_skill_executor is not None
            ):
                try:
                    managed_skill_service.recover_imports()
                    configured_package_names = set(BUILTIN_MANAGED_SKILL_PACKAGES)
                    dynamic_package_names = {
                        child.name
                        for child in runtime_settings.managed_skill_root.iterdir()
                        if child.is_dir()
                        and child.name not in {".staging", "snapshots", "local-source-acceptance"}
                        and (child / "skill.manifest.json").is_file()
                    }
                    # Keep the declared builtin order stable because startup
                    # recovery observes registration order. Custom packages
                    # discovered in the managed root are deterministic after
                    # the builtins, without allowing directory enumeration to
                    # reorder the recovery contract.
                    package_names = [
                        package_name
                        for package_name in BUILTIN_MANAGED_SKILL_PACKAGES
                        if package_name in configured_package_names
                    ]
                    package_names.extend(
                        sorted(dynamic_package_names - configured_package_names)
                    )
                    for package_name in package_names:
                        assert runtime_settings.managed_skill_root is not None
                        package_root = runtime_settings.managed_skill_root / package_name
                        # A managed root may intentionally contain only a subset
                        # of the bundled packages (for example, a test or a
                        # staged upgrade). Missing packages are optional; an
                        # explicitly present package is still validated strictly
                        # by register_builtin and fails closed when malformed.
                        if not package_root.exists():
                            continue
                        managed_skill_service.register_builtin(
                            package_root
                        )
                except (ManagedSkillError, OSError, ValueError) as exc:
                    managed_skill_service_error = str(exc)
                else:
                    managed_skill_service_error = None
                    # 受管技能包只留给旧的诊断路由；对话 Agent 不再挂
                    # skill_execute（四族受管子进程链），修改落盘也不再接
                    # 写前门禁执行器——作者要的是普通 Agent，不是审批流水线。
                    app.state.skill_executor = managed_skill_executor
            app.state.managed_skill_service_error = managed_skill_service_error
            if change_set_service is not None:
                change_set_service.recover_interrupted()
            try:
                _recover_workflows_at_startup(
                    agent_store,
                    context_store,
                    app.state.skill_executor,
                )
            finally:
                agent_store.recover_interrupted()
            agent_queue_service.store.recover_dispatches()
            context.store.recover_interrupted_source_imports()
            context.startup_error = startup_configuration_error
        except Exception as exc:  # Runtime remains live so readiness can explain failure.
            context.startup_error = str(exc)
            context.database_health = initial_health
            # A failed startup must not leave a previously prepared Skill
            # executor advertised through the Agent or the diagnostic state.
            agent_service.tool_executor = base_tool_executor
            app.state.skill_executor = None
            if change_set_service is not None:
                change_set_service.set_post_draft_gate_executor(None)
            if managed_skill_service is not None:
                managed_skill_service_error = (
                    f"Runtime startup failed before Managed Skill readiness: {exc}"
                )
            app.state.managed_skill_service_error = managed_skill_service_error
        if database_initialized:
            # 库一丢，对话、修改记录和「撤回」凭据全没了，所以自动备份不能靠作者想起来。
            auto_backup = AutoBackupWorker(
                BackupService(
                    context.store,
                    runtime_settings,
                    product_version=__version__,
                )
            )
            auto_backup.start()
        if context.startup_error is None and database_initialized:
            if base_tool_executor is not None:
                await asyncio.to_thread(base_tool_executor._command_session_manager)
            await mcp_service.start()
            agent_queue_scheduler.start()
        try:
            yield
        finally:
            try:
                await agent_queue_scheduler.stop()
                await maintenance.drain_background()
                if auto_backup is not None:
                    auto_backup.stop()
                if database_initialized:
                    await agent_service.shutdown()
            finally:
                await mcp_service.shutdown()
                if source_import_service is not None:
                    source_import_service.close()

    app = FastAPI(
        title="Zhimeng Writing Runtime",
        version=__version__,
        description="Local execution runtime for the Zhimeng author workbench.",
        lifespan=lifespan,
    )
    app.state.runtime = context
    app.state.agent_service = agent_service
    app.state.agent_queue_service = agent_queue_service
    app.state.agent_queue_scheduler = agent_queue_scheduler
    app.state.mcp_service = mcp_service
    app.state.mcp_adapter = mcp_adapter
    app.state.context_service = context_service
    app.state.distillation_invocation_store = distillation_invocation_store
    app.state.distillation_invocation_service = distillation_invocation_service
    app.state.distillation_prepare_service = distillation_prepare_service
    app.state.project_kb_service = project_kb_service
    app.state.managed_skill_service = managed_skill_service
    app.state.managed_skill_service_error = managed_skill_service_error
    app.state.skill_executor = None
    app.state.runtime_maintenance = maintenance
    app.add_middleware(RuntimeMaintenanceMiddleware, maintenance=maintenance)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "If-None-Match",
            "X-Request-ID",
        ],
        expose_headers=["ETag", "X-Request-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid4())
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    install_exception_handlers(app)
    app.include_router(
        create_project_router(
            project_reader,
            context.store,
            reader_error=project_reader_error,
            locks=project_locks,
            text_import_service=text_import_service,
            provider_settings=provider_settings_service,
            provider_resolver=resolve_agent_provider,
            maintenance=maintenance,
        )
    )
    app.include_router(
        create_change_set_router(
            change_set_service,
            context.store,
            project_locks,
            service_error=project_reader_error,
        )
    )
    app.include_router(
        create_skill_router(
            skill_registry,
            registry_error=skill_registry_error,
        )
    )
    app.include_router(
        create_source_import_router(
            source_import_service,
            store=context.store,
            service_error=source_import_error,
            distillation_prepare_service=distillation_prepare_service,
            source_upload_service=source_upload_service,
        )
    )
    app.include_router(
        create_distillation_router(
            distillation_adapter,
            adapter_error=distillation_error,
            invocation_store=distillation_invocation_store,
            invocation_service=distillation_invocation_service,
        )
    )
    app.include_router(
        create_knowledge_router(
            native_knowledge_service,
            project_kb_service,
            project_kb_service_error=project_kb_service_error,
        )
    )
    app.include_router(
        create_search_router(
            search_service,
            service_error=project_reader_error,
        )
    )
    app.include_router(create_user_skill_router(user_skill_folder_service))
    app.include_router(create_mcp_router(mcp_service, mcp_adapter))
    app.include_router(create_author_phrases_router(runtime_state_root))
    app.include_router(create_user_memory_router(runtime_state_root))
    app.include_router(create_local_file_router())
    # 选文件夹的框（纠偏 P2）：常用位置、列一层文件夹、新建文件夹；打开成项目仍走受信根 + 读项目两个老接口。
    app.include_router(
        create_fs_router(
            trusted_roots=lambda: sorted(project_reader.trusted_roots) if project_reader is not None else [],
            project_roots=lambda: [binding.root_path for binding in context.store.list_project_bindings()],
        )
    )
    app.include_router(
        create_project_asset_router(
            project_asset_service,
            project_root_resolver=resolve_project_root_for_assets,
        )
    )
    app.include_router(
        create_longform_check_router(
            project_root_resolver=resolve_project_root_for_assets,
        )
    )
    app.include_router(create_provider_settings_router(provider_settings_service))
    app.include_router(
        create_context_router(
            context_service,
            service_error=project_reader_error,
        )
    )
    app.include_router(
        create_agent_router(
            agent_service,
            heartbeat_seconds=runtime_settings.sse_heartbeat_seconds,
            queue_service=agent_queue_service,
        )
    )
    file_change_journal = base_tool_executor.file_journal if base_tool_executor else FileChangeJournal(context.store, runtime_state_root)
    app.state.file_change_journal = file_change_journal
    app.include_router(create_file_changes_router(file_change_journal, agent_store))
    app.include_router(create_context_state_router(agent_service))

    @app.get("/health/live", response_model=LiveResponse, tags=["health"])
    async def health_live() -> LiveResponse:
        return LiveResponse(
            service=SERVICE_NAME,
            version=__version__,
            status="live",
            timestamp=datetime.now(UTC),
        )

    @app.get(
        "/health/ready",
        response_model=ReadyResponse,
        responses={503: {"description": "Runtime dependencies are not ready"}},
        tags=["health"],
    )
    async def health_ready() -> ReadyResponse:
        health = context.store.probe() if context.ready else context.database_health
        context.database_health = health
        if not context.ready:
            raise ServiceUnavailableError(
                "Writing runtime is not ready",
                details={
                    "database_ready": health.ready,
                    "startup_error": context.startup_error,
                },
            )
        return ReadyResponse(
            service=SERVICE_NAME,
            version=__version__,
            status="ready",
            database_schema_version=health.schema_version,
            timestamp=datetime.now(UTC),
        )

    @app.get(
        "/api/v1/runtime/status",
        response_model=RuntimeStatusResponse,
        tags=["runtime"],
    )
    async def runtime_status() -> RuntimeStatusResponse:
        health = context.store.probe() if context.ready else context.database_health
        context.database_health = health
        if context.startup_error is not None:
            state = "degraded"
        elif health.ready:
            state = "ready"
        else:
            state = "starting"
        return RuntimeStatusResponse(
            service=SERVICE_NAME,
            version=__version__,
            environment=runtime_settings.environment,
            state=state,
            started_at=context.started_at,
            uptime_seconds=max(0.0, monotonic() - context.started_monotonic),
            database=DatabaseStatus(
                ready=health.ready,
                journal_mode=health.journal_mode,
                schema_version=health.schema_version,
            ),
        )


    def _runtime_state_and_health() -> tuple[str, DatabaseHealth]:
        health = context.store.probe() if context.ready else context.database_health
        context.database_health = health
        if context.startup_error is not None:
            state = "degraded"
        elif health.ready:
            state = "ready"
        else:
            state = "starting"
        return state, health

    def _raise_backup_error(exc: BackupError) -> None:
        if isinstance(exc, BackupNotFoundError):
            raise ApiError(str(exc), status_code=404, code="runtime_backup_not_found") from exc
        if isinstance(exc, BackupConflictError):
            raise ApiError(str(exc), status_code=409, code="runtime_backup_conflict") from exc
        if isinstance(exc, BackupStateError):
            raise ApiError(str(exc), status_code=409, code="runtime_backup_state") from exc
        raise ApiError(str(exc), status_code=500, code="runtime_backup_failed") from exc

    def _backup_service() -> BackupService:
        return BackupService(
            context.store,
            runtime_settings,
            product_version=__version__,
        )

    @app.post(
        "/api/v1/runtime/diagnostics-package",
        response_model=DiagnosticsPackageResponse,
        tags=["runtime"],
    )
    async def export_diagnostics_package() -> DiagnosticsPackageResponse:
        state, health = _runtime_state_and_health()
        service = DiagnosticsService(
            context.store,
            runtime_settings,
            product_version=__version__,
        )
        return service.export_package(
            runtime_state=state,
            health=health,
            startup_error=context.startup_error,
        )

    @app.get(
        "/api/v1/runtime/backups",
        response_model=RuntimeBackupListResponse,
        tags=["runtime"],
    )
    async def list_runtime_backups() -> RuntimeBackupListResponse:
        return await asyncio.to_thread(_backup_service().list_backups)

    @app.post(
        "/api/v1/runtime/backups",
        response_model=RuntimeBackupResponse,
        tags=["runtime"],
    )
    async def create_runtime_backup() -> RuntimeBackupResponse:
        service = _backup_service()
        try:
            worker = maintenance.start_background(
                lambda: asyncio.to_thread(service.create_backup), name="runtime-backup",
            )
            return await asyncio.shield(worker)
        except BackupError as exc:
            _raise_backup_error(exc)
            raise

    @app.post(
        "/api/v1/runtime/backups/restore",
        response_model=RuntimeRestoreResponse,
        tags=["runtime"],
    )
    async def restore_runtime_backup(
        request: RuntimeRestoreRequest,
    ) -> RuntimeRestoreResponse:
        service = _backup_service()
        try:
            await asyncio.to_thread(service.validate_restore, request)
            async def perform_restore() -> RuntimeRestoreResponse:
                await agent_queue_scheduler.stop()
                if auto_backup is not None:
                    await asyncio.to_thread(auto_backup.stop, None)
                await agent_service.shutdown()
                if source_import_service is not None:
                    await asyncio.to_thread(source_import_service.close)
                try:
                    return await asyncio.to_thread(service.restore_backup, request)
                finally:
                    # Reconcile restored durable state before admitting new work.
                    # A failed restore also reopens the intact original database.
                    context.database_health = await asyncio.to_thread(context.store.initialize)
                    await asyncio.to_thread(_pretrust_bound_project_roots)
                    if change_set_service is not None:
                        await asyncio.to_thread(change_set_service.recover_interrupted)
                    await asyncio.to_thread(agent_store.recover_interrupted)
                    await asyncio.to_thread(agent_queue_service.store.recover_dispatches)
                    await asyncio.to_thread(context.store.recover_interrupted_source_imports)
                    if source_import_service is not None:
                        source_import_service.reopen()
                    agent_service.reopen_after_restore()
                    if base_tool_executor is not None:
                        await asyncio.to_thread(base_tool_executor._command_session_manager)
                    if auto_backup is not None:
                        auto_backup.start()
                    agent_queue_scheduler.start()
            return await maintenance.run(perform_restore)
        except BackupError as exc:
            _raise_backup_error(exc)
            raise

    @app.get(
        "/api/v1/runtime/trusted-roots",
        response_model=RuntimeTrustedRootResponse,
        tags=["runtime"],
    )
    async def list_trusted_roots() -> RuntimeTrustedRootResponse:
        if project_reader is None:
            raise ApiError(
                "Project Reader is disabled",
                status_code=503,
                code="project_reader_unavailable",
            )
        return RuntimeTrustedRootResponse(
            trusted_roots=sorted(
                str(root) for root in project_reader.trusted_roots
            ),
            persisted_roots=_load_persisted_trusted_roots(),
        )

    @app.post(
        "/api/v1/runtime/trusted-roots",
        response_model=RuntimeTrustedRootResponse,
        tags=["runtime"],
    )
    async def add_trusted_root(
        request: RuntimeTrustedRootRequest,
    ) -> RuntimeTrustedRootResponse:
        """把一个书目录当场加进受信根（挪盘/新盘自救），并落盘记住。

        书挪了位置后，旧绑定全部 403；以前只能改启动配置重启。现在作者
        或 Agent 用一条命令就能自助，重启后仍然生效。
        """
        if project_reader is None:
            raise ApiError(
                "Project Reader is disabled",
                status_code=503,
                code="project_reader_unavailable",
            )
        candidate = Path(request.path).expanduser()
        if not candidate.is_absolute():
            raise ApiError(
                f"受信根必须是绝对路径：{request.path}",
                status_code=422,
                code="trusted_root_not_absolute",
            )
        resolved = candidate.resolve(strict=False)
        if not resolved.is_dir():
            raise ApiError(
                f"受信根必须是一个已存在的目录：{resolved}",
                status_code=422,
                code="trusted_root_not_found",
            )
        # 选文件夹框里打开书库中的一本书时，它已经在书库这个受信根里面：
        # 不再单独记一条，免得设置页的「可信文件夹」越记越长（打开过的项目重启后会按绑定自己补上信任）。
        already_trusted = any(
            resolved == root or root in resolved.parents
            for root in project_reader.trusted_roots
        )
        try:
            project_reader.trust_root(resolved)
        except OSError as exc:
            raise ApiError(
                f"受信根添加失败：{exc}",
                status_code=500,
                code="trusted_root_add_failed",
            ) from exc
        persisted = _load_persisted_trusted_roots()
        if not already_trusted and str(resolved) not in persisted:
            persisted.append(str(resolved))
            _persist_trusted_roots(persisted)
        return RuntimeTrustedRootResponse(
            trusted_roots=sorted(
                str(root) for root in project_reader.trusted_roots
            ),
            persisted_roots=persisted,
        )

    @app.delete(
        "/api/v1/runtime/trusted-roots",
        response_model=RuntimeTrustedRootResponse,
        tags=["runtime"],
    )
    async def remove_trusted_root(
        request: RuntimeTrustedRootRequest,
    ) -> RuntimeTrustedRootResponse:
        """设置页移除一个可信文件夹：只认「自己加过的」（persisted），
        启动配置里带的根不在这里删（删了重启也会回来，那是改启动配置的事）。"""
        if project_reader is None:
            raise ApiError(
                "Project Reader is disabled",
                status_code=503,
                code="project_reader_unavailable",
            )
        candidate = Path(request.path).expanduser().resolve(strict=False)
        persisted = _load_persisted_trusted_roots()
        if str(candidate) not in persisted:
            raise ApiError(
                "这个文件夹不是后来添加的，不能在这里移除。",
                status_code=409,
                code="trusted_root_not_removable",
            )
        persisted.remove(str(candidate))
        _persist_trusted_roots(persisted)
        project_reader.untrust_root(candidate)
        return RuntimeTrustedRootResponse(
            trusted_roots=sorted(
                str(root) for root in project_reader.trusted_roots
            ),
            persisted_roots=persisted,
        )

    def _raise_release_readiness_error(exc: ReleaseReadinessError) -> None:
        if isinstance(exc, ReleaseReadinessStateError):
            raise ApiError(
                str(exc),
                status_code=409,
                code=exc.code,
                retryable=False,
            ) from exc
        raise ApiError(
            str(exc),
            status_code=500,
            code=exc.code,
            retryable=False,
        ) from exc

    @app.get(
        "/api/v1/runtime/release-readiness",
        tags=["runtime"],
    )
    async def get_runtime_release_readiness() -> dict:
        try:
            latest = release_readiness_service.latest()
            if latest is not None:
                return latest
            return release_readiness_service.evaluate()
        except ReleaseReadinessError as exc:
            _raise_release_readiness_error(exc)
            raise

    @app.post(
        "/api/v1/runtime/release-readiness/evaluate",
        tags=["runtime"],
    )
    async def evaluate_runtime_release_readiness() -> dict:
        try:
            return release_readiness_service.evaluate()
        except ReleaseReadinessError as exc:
            _raise_release_readiness_error(exc)
            raise

    @app.get("/api/v1/events/stream", tags=["events"])
    async def event_stream(request: Request) -> StreamingResponse:
        return StreamingResponse(
            iter_runtime_events(
                request,
                service_name=SERVICE_NAME,
                version=__version__,
                ready=context.ready,
                heartbeat_seconds=runtime_settings.sse_heartbeat_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
