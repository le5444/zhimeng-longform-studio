from __future__ import annotations

from pathlib import Path
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from zhimeng_runtime import __version__
from zhimeng_runtime.agent_api import create_agent_router
from zhimeng_runtime.agent_broker import AgentEventBroker
from zhimeng_runtime.agent_provider import (
    AgentProvider,
    build_agent_provider_from_connection,
)
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore, AgentStoreNotFound
from zhimeng_runtime.agent_tools import AgentToolExecutor
from zhimeng_runtime.change_set_api import create_change_set_router
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings
from zhimeng_runtime.context_api import create_context_router
from zhimeng_runtime.adjacent_chain_api import create_adjacent_chain_router
from zhimeng_runtime.adjacent_chain_service import AdjacentChainService
from zhimeng_runtime.managed_skill_chain_api import create_managed_skill_chain_router
from zhimeng_runtime.managed_skill_chain_service import ManagedSkillChainService
from zhimeng_runtime.skill_adaptation_api import create_skill_adaptation_router
from zhimeng_runtime.skill_adaptation_service import SkillAdaptationService
from zhimeng_runtime.skill_rule_evolution_api import create_skill_rule_evolution_router
from zhimeng_runtime.skill_rule_evolution_service import SkillRuleEvolutionService
from zhimeng_runtime.context_handoff_api import create_context_handoff_router
from zhimeng_runtime.context_handoff_service import ContextHandoffService
from zhimeng_runtime.context_service import ContextService
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
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader, ProjectReaderError
from zhimeng_runtime.text_project_import_service import TextProjectImportService
from zhimeng_runtime.formal_draft_api import create_formal_draft_router
from zhimeng_runtime.four_skills_continuous_api import create_four_skills_continuous_router
from zhimeng_runtime.four_skills_raw_adaptation_api import create_four_skills_raw_adaptation_router
from zhimeng_runtime.four_skills_raw_adaptation_service import FourSkillsRawAdaptationService
from zhimeng_runtime.eval_suite_api import create_eval_suite_router
from zhimeng_runtime.eval_suite_service import EvalSuiteService
from zhimeng_runtime.evaluation_scenario_api import create_evaluation_scenario_router
from zhimeng_runtime.external_blind_review_matrix_api import (
    create_external_blind_review_matrix_router,
)
from zhimeng_runtime.evaluation_scenario_service import EvaluationScenarioService
from zhimeng_runtime.privacy_outbound_api import create_privacy_outbound_router
from zhimeng_runtime.privacy_outbound_service import PrivacyOutboundService
from zhimeng_runtime.chapter_draft_api import create_chapter_draft_router
from zhimeng_runtime.ai_smell_service import AiSmellService
from zhimeng_runtime.knowledge_context_service import KnowledgeContextService
from zhimeng_runtime.provider_settings_api import create_provider_settings_router
from zhimeng_runtime.provider_settings_service import (
    ProviderSettings,
    ProviderSettingsService,
)
from zhimeng_runtime.chapter_draft_service import ChapterDraftService
from zhimeng_runtime.project_asset_api import create_project_asset_router
from zhimeng_runtime.project_asset_service import ProjectAssetService
from zhimeng_runtime.four_skills_continuous_service import FourSkillsContinuousService
from zhimeng_runtime.formal_draft_service import FormalDraftAuthorizationService
from zhimeng_runtime.search_api import create_search_router
from zhimeng_runtime.search_service import SearchService
from zhimeng_runtime.skill_api import create_skill_router
from zhimeng_runtime.skill_handoff_api import create_skill_handoff_router
from zhimeng_runtime.skill_handoff_service import SkillHandoffService
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.skill_registry import SkillRegistry, SkillRegistryError
from zhimeng_runtime.source_import_api import create_source_import_router
from zhimeng_runtime.source_import_service import SourceImportService
from zhimeng_runtime.tomato_skill_adapter import (
    TomatoAdapterError,
    TomatoSourceAdapter,
    build_tomato_skill_adapter,
)
from zhimeng_runtime.workflow_runner import WorkflowRunner


SERVICE_NAME = "zhimeng-writing-runtime"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
BUILTIN_MANAGED_SKILL_PACKAGES = (
    "creation-suite-project-audit",
    "creation-suite-prewrite-gate",
    "creation-suite-handoff",
    "creation-suite-draft-gate",
    "creation-suite-writer-instruction",
    "novel-distillation-deep-instruction",
    "novel-distillation-kb-handoff",
    "novel-kb-context-handoff",
    "four-skills-family-source-ingest",
    "four-skills-family-distillation",
    "four-skills-family-knowledge-context",
    "four-skills-family-creation-suite",
)


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
        raise RuntimeError(
            "Managed Skill executor is unavailable for workflow startup recovery"
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
    context = RuntimeContext(
        settings=runtime_settings,
        store=SqliteStore(runtime_settings.database_path),
        started_at=datetime.now(UTC),
        started_monotonic=monotonic(),
        database_health=initial_health,
    )
    provider_settings_service = ProviderSettingsService(
        runtime_settings.database_path.parent / "provider-settings.json",
        fallback=ProviderSettings(
            base_url=runtime_settings.agent_provider_base_url,
            model=runtime_settings.agent_provider_model,
            api_key=runtime_settings.agent_provider_api_key,
        ),
        fallback_credential_reference=(
            runtime_settings.agent_provider_credential_reference
        ),
    )
    if runtime_settings.managed_skill_root is not None:
        try:
            managed_skill_service = ManagedSkillService(
                # Bundled packages are sources. Executable snapshots live beside
                # Runtime SQLite so execution never depends on the source checkout.
                context.store,
                (runtime_settings.database_path.parent / "managed-skills").resolve(),
                trusted_import_roots=runtime_settings.trusted_skill_roots,
            )
            managed_skill_executor = SkillExecutor(managed_skill_service)
        except (ManagedSkillError, OSError, ValueError) as exc:
            managed_skill_service_error = str(exc)
    agent_provider_resolver = None
    if agent_provider is None:

        def resolve_agent_provider() -> AgentProvider | None:
            effective, credential_reference = (
                provider_settings_service.resolve_agent_connection()
            )
            return build_agent_provider_from_connection(
                runtime_settings,
                base_url=effective.base_url,
                model=effective.model,
                api_key=effective.api_key,
                credential_reference=credential_reference,
            )

        agent_provider_resolver = resolve_agent_provider
        agent_provider = resolve_agent_provider()
    agent_store = AgentStore(context.store)
    distillation_invocation_store = DistillationInvocationStore(context.store)
    distillation_invocation_service = DistillationInvocationService(
        invocation_store=distillation_invocation_store,
        provider=agent_provider,
        provider_resolver=agent_provider_resolver,
    )
    source_import_state_root = runtime_settings.database_path.parent / "source-imports"
    if source_import_adapter is None and runtime_settings.tomato_skill_root is not None:
        try:
            source_import_adapter = build_tomato_skill_adapter(
                runtime_settings,
                state_root=source_import_state_root,
            )
        except (OSError, TomatoAdapterError) as exc:
            source_import_error = str(exc)
    if local_source_adapter is None and runtime_settings.trusted_source_roots:
        try:
            local_source_adapter = ManagedLocalSourceAdapter(
                snapshot_root=(
                    DEFAULT_MANAGED_SKILL_ROOT / "local-source-acceptance"
                ),
                state_root=source_import_state_root,
                trusted_source_roots=runtime_settings.trusted_source_roots,
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
                runtime_settings.database_path.parent / "project-kb",
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
    formal_draft_service = FormalDraftAuthorizationService(
        context.store,
        change_set_state_root=runtime_settings.database_path.parent / "change-sets",
        runtime_state_root=runtime_settings.database_path.parent,
    )
    four_skills_continuous_service = FourSkillsContinuousService(
        context.store,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    change_set_service = (
        ChangeSetService(
            project_reader,
            context.store,
            runtime_settings.database_path.parent / "change-sets",
            post_draft_gate_executor=managed_skill_executor,
            knowledge_fingerprint_probe=knowledge_fingerprint_probe,
            knowledge_validator_resolver=knowledge_validator_resolver,
            formal_draft_authorization_service=formal_draft_service,
        )
        if project_reader is not None
        else None
    )
    search_service = (
        SearchService(
            context.store,
            project_reader,
            native_knowledge_service,
            runtime_settings.database_path.parent / "project-search",
        )
        if project_reader is not None
        else None
    )
    base_tool_executor = (
        AgentToolExecutor(
            project_reader,
            context.store,
            project_locks,
            change_set_service,
            search_service=search_service,
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
    )
    context_service = (
        ContextService(
            context_store,
            agent_store,
            project_reader,
            project_locks,
            distillation_adapter,
            native_knowledge_service,
            project_kb_service,
        )
        if project_reader is not None
        else None
    )
    skill_handoff_service = SkillHandoffService(
        context.store,
        native_knowledge_service,
        managed_skill_executor,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    context_handoff_service = ContextHandoffService(
        context.store,
        context_store,
        managed_skill_executor,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    adjacent_chain_service = AdjacentChainService(
        context.store,
        skill_handoff_service,
        context_handoff_service,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    managed_skill_chain_service = ManagedSkillChainService(
        context.store,
        managed_skill_executor,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    skill_adaptation_service = SkillAdaptationService(
        managed_skill_service,
        trusted_skill_roots=runtime_settings.trusted_skill_roots,
        runtime_state_root=runtime_settings.database_path.parent,
    )
    four_skills_raw_adaptation_service = (
        FourSkillsRawAdaptationService(
            skill_adaptation_service,
            state_root=runtime_settings.database_path.parent / "four-skills-raw-adaptations",
            skill_executor=managed_skill_executor,
            store=context.store,
        )
        if skill_adaptation_service is not None
        else None
    )
    skill_rule_evolution_service = (
        SkillRuleEvolutionService(
            managed_skill_service,
            state_root=runtime_settings.database_path.parent / "skill-rule-evolutions",
            change_set_service=change_set_service,
        )
        if managed_skill_service is not None
        else None
    )
    eval_suite_service = (
        EvalSuiteService(
            state_root=runtime_settings.database_path.parent / "eval-suites",
            change_set_service=change_set_service,
            change_set_state_root=runtime_settings.database_path.parent / "change-sets",
            agent_store=agent_store if change_set_service is not None else None,
        )
        if change_set_service is not None
        else None
    )
    _repo_evaluation_root = Path(__file__).resolve().parents[4] / "evaluation"
    evaluation_scenario_service = EvaluationScenarioService(
        packs_root=(
            _repo_evaluation_root
            if _repo_evaluation_root.is_dir()
            else runtime_settings.database_path.parent / "evaluation"
        ),
        state_root=runtime_settings.database_path.parent / "evaluation-scenario-executions",
    )
    privacy_outbound_service = PrivacyOutboundService(
        state_root=runtime_settings.database_path.parent / "privacy",
        provider_base_url=runtime_settings.agent_provider_base_url,
        provider_model=runtime_settings.agent_provider_model,
        telemetry_enabled=False,
    )
    release_readiness_service = ReleaseReadinessService(
        state_root=runtime_settings.database_path.parent / "release-readiness",
    )
    project_asset_service = ProjectAssetService(
        state_root=runtime_settings.database_path.parent / "project-assets",
    )

    def resolve_project_root_for_assets(project_id: str):
        binding = context.store.get_project_binding(project_id)
        if binding is None:
            raise FileNotFoundError(f"project binding not found: {project_id}")
        return Path(binding.root_path)

    ai_smell_service = AiSmellService()
    knowledge_context_service = KnowledgeContextService()
    chapter_draft_service = (
        ChapterDraftService(
            change_set_service,
            provider_settings=provider_settings_service,
            project_reader=project_reader,
            ai_smell=ai_smell_service,
            knowledge=knowledge_context_service,
        )
        if change_set_service is not None and project_reader is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal managed_skill_service_error
        database_initialized = False
        try:
            context.database_health = context.store.initialize()
            database_initialized = True
            agent_service.tool_executor = base_tool_executor
            app.state.skill_executor = None
            if (
                managed_skill_service is not None
                and managed_skill_executor is not None
            ):
                try:
                    managed_skill_service.recover_imports()
                    for package_name in BUILTIN_MANAGED_SKILL_PACKAGES:
                        assert runtime_settings.managed_skill_root is not None
                        package_root = runtime_settings.managed_skill_root / package_name
                        # The post-draft Gate is additive for existing development
                        # bundles. A custom root may still contain only the two
                        # M3a/M3e packages; an explicitly present but invalid Gate
                        # package must still fail closed.
                        if (
                            package_name
                            in {
                                "creation-suite-draft-gate",
                                "creation-suite-writer-instruction",
                            }
                            and not package_root.exists()
                        ):
                            continue
                        managed_skill_service.register_builtin(
                            package_root
                        )
                except (ManagedSkillError, OSError, ValueError) as exc:
                    managed_skill_service_error = str(exc)
                else:
                    managed_skill_service_error = None
                    app.state.skill_executor = managed_skill_executor
                    if change_set_service is not None:
                        change_set_service.set_post_draft_gate_executor(
                            managed_skill_executor
                        )
                    if project_reader is not None and change_set_service is not None:
                        agent_service.tool_executor = AgentToolExecutor(
                            project_reader,
                            context.store,
                            project_locks,
                            change_set_service,
                            search_service=search_service,
                            skill_executor=managed_skill_executor,
                        )
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
            context.store.recover_interrupted_source_imports()
            context.startup_error = None
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
        try:
            yield
        finally:
            if database_initialized:
                await agent_service.shutdown()
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
    app.state.context_service = context_service
    app.state.distillation_invocation_store = distillation_invocation_store
    app.state.distillation_invocation_service = distillation_invocation_service
    app.state.distillation_prepare_service = distillation_prepare_service
    app.state.project_kb_service = project_kb_service
    app.state.managed_skill_service = managed_skill_service
    app.state.managed_skill_service_error = managed_skill_service_error
    app.state.skill_executor = None

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
            managed_service=managed_skill_service,
            managed_service_error=lambda: managed_skill_service_error,
        )
    )
    app.include_router(
        create_source_import_router(
            source_import_service,
            store=context.store,
            service_error=source_import_error,
            distillation_prepare_service=distillation_prepare_service,
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
    app.include_router(create_formal_draft_router(formal_draft_service))
    app.include_router(create_four_skills_continuous_router(four_skills_continuous_service))
    app.include_router(create_four_skills_raw_adaptation_router(four_skills_raw_adaptation_service))
    app.include_router(create_skill_handoff_router(skill_handoff_service))
    app.include_router(create_context_handoff_router(context_handoff_service))
    app.include_router(create_adjacent_chain_router(adjacent_chain_service))
    app.include_router(create_managed_skill_chain_router(managed_skill_chain_service))
    app.include_router(create_skill_adaptation_router(skill_adaptation_service))
    app.include_router(create_skill_rule_evolution_router(skill_rule_evolution_service))
    app.include_router(create_eval_suite_router(eval_suite_service))
    app.include_router(create_evaluation_scenario_router(evaluation_scenario_service))
    app.include_router(create_external_blind_review_matrix_router())
    app.include_router(create_privacy_outbound_router(privacy_outbound_service))
    app.include_router(
        create_project_asset_router(
            project_asset_service,
            project_root_resolver=resolve_project_root_for_assets,
        )
    )
    app.include_router(
        create_chapter_draft_router(
            chapter_draft_service,
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
        )
    )

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
        service = BackupService(
            context.store,
            runtime_settings,
            product_version=__version__,
        )
        return service.list_backups()

    @app.post(
        "/api/v1/runtime/backups",
        response_model=RuntimeBackupResponse,
        tags=["runtime"],
    )
    async def create_runtime_backup() -> RuntimeBackupResponse:
        service = BackupService(
            context.store,
            runtime_settings,
            product_version=__version__,
        )
        try:
            return service.create_backup()
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
        service = BackupService(
            context.store,
            runtime_settings,
            product_version=__version__,
        )
        try:
            return service.restore_backup(request)
        except BackupError as exc:
            _raise_backup_error(exc)
            raise


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
