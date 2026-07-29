from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette import status

from zhimeng_runtime.agent_provider import AgentProvider
from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.distillation_anchor_models import (
    DistillationAnchor,
    DistillationAnchorCatalog,
    DistillationAnchorLevel,
    build_distillation_anchor,
    build_distillation_anchor_catalog,
)
from zhimeng_runtime.distillation_invocation_models import (
    DISTILLATION_ALLOWED_TOOL_NAMES,
    DISTILLATION_INSTRUCTION_SKILL_ID,
    DistillationInstructionBinding,
    DistillationKnowledgeBinding,
    DistillationProjectBinding,
    DistillationProviderBinding,
    DistillationSourceBinding,
    distillation_invocation_id,
    distillation_invocation_request_hash,
)
from zhimeng_runtime.distillation_invocation_service import (
    build_distillation_tool_definitions,
    distillation_provider_tool_definitions_hash,
)
from zhimeng_runtime.distillation_invocation_store import (
    DistillationInvocationSnapshot,
    DistillationInvocationStore,
    DistillationInvocationStoreError,
    DistillationInvocationStoreNotFound,
)
from zhimeng_runtime.distillation_receipt_models import (
    DistillationPreparedPayload,
    build_distillation_receipt,
)
from zhimeng_runtime.knowledge_service import KnowledgeService, KnowledgeServiceError
from zhimeng_runtime.knowledge_store import KnowledgeStoreConflict, KnowledgeStoreError
from zhimeng_runtime.project_kb_models import ProjectKbStatus
from zhimeng_runtime.project_kb_service import (
    ProjectKbService,
    ProjectKbServiceError,
    ProjectKbTamperedError,
)
from zhimeng_runtime.source_import_models import (
    AnySourceImportJob,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_receipt_models import (
    AnySourceImportExecutionReceipt,
    parse_source_import_execution_receipt,
    validate_source_import_receipt_job,
)


MAX_ANCHOR_TEXT_CHARS = 24_000


class SourceImportDistillationPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    expected_project_revision: int | None = Field(default=None, ge=1)
    expected_project_fingerprint: Sha256 | None = None
    idempotency_key: str | None = Field(
        default=None,
        pattern=r"^distillation-idempotency-[0-9a-f]{32}$",
    )


@dataclass(frozen=True, slots=True)
class SourceImportDistillationPrepareResult:
    snapshot: DistillationInvocationSnapshot
    anchor_catalog: DistillationAnchorCatalog
    text_by_anchor_id: Mapping[str, str]


class SourceImportDistillationPrepareError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int = status.HTTP_409_CONFLICT,
        retryable: bool = False,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.details = details


class SourceImportDistillationPrepareService:
    """Prepare a Runtime-owned deep distillation invocation from a verified source.

    The legacy source-import receipt remains immutable: its
    ``distillation_invocation_id`` is a historical ``source-import-invocation-*``
    draft identifier.  This service creates a new Runtime-owned
    ``distillation-invocation-*`` root receipt that binds the verified source
    receipt, current project binding, current project knowledge scope, active
    managed instruction Skill, Provider identity/config, and source anchors.
    """

    def __init__(
        self,
        *,
        database: SqliteStore,
        source_state_root: Path,
        invocation_store: DistillationInvocationStore,
        knowledge_service: KnowledgeService,
        managed_skill_service: Any | None,
        provider: AgentProvider | None,
        provider_resolver: Callable[[], AgentProvider | None] | None = None,
        project_kb_service: ProjectKbService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.source_state_root = source_state_root.resolve(strict=False)
        self.invocation_store = invocation_store
        self.knowledge_service = knowledge_service
        self.managed_skill_service = managed_skill_service
        self.provider = provider
        self._provider_resolver = provider_resolver
        self.project_kb_service = project_kb_service
        self._clock = clock or (lambda: datetime.now(UTC))
        self.tool_definitions = build_distillation_tool_definitions()
        self.tool_definitions_hash = distillation_provider_tool_definitions_hash(
            self.tool_definitions
        )

    def prepare(
        self,
        source_import_id: str,
        request: SourceImportDistillationPrepareRequest,
    ) -> SourceImportDistillationPrepareResult:
        provider = self._resolve_provider()
        if provider is None:
            raise SourceImportDistillationPrepareError(
                "Agent Provider is not configured for Runtime deep distillation",
                code="distillation_provider_unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if self.managed_skill_service is None:
            raise SourceImportDistillationPrepareError(
                "Managed Skill lifecycle is not configured",
                code="managed_skill_lifecycle_unavailable",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        job = self._read_verified_job(source_import_id)
        source_receipt = self._read_verified_source_receipt(source_import_id, job)
        project = self._read_project_binding(request)
        text = self._read_source_text(job, source_receipt)

        scope = self._ensure_project_scope_and_source_binding(
            source_import_id=source_import_id,
            project_id=project.project_id,
        )
        knowledge_fingerprint = self.knowledge_service.observe_knowledge_fingerprint(
            project.project_id
        )
        project_kb_state_id, project_kb_manifest_hash = self._current_project_kb_binding(
            project.project_id,
        )
        instruction = self._active_instruction_binding(project.project_id)
        provider_binding = self._provider_binding(provider)
        idempotency_key = request.idempotency_key or self._default_idempotency_key(
            source_receipt=source_receipt,
            project=project,
            scope_id=scope.scope_id,
            scope_state_revision=scope.state_revision,
            knowledge_fingerprint=knowledge_fingerprint,
            instruction=instruction,
            provider=provider_binding,
        )
        source = DistillationSourceBinding(
            source_import_id=source_receipt.source_import_id,
            source_receipt_id=source_receipt.receipt_id,
            source_receipt_hash=source_receipt.receipt_hash,
            source_packet_fingerprint=source_receipt.source_packet_fingerprint,
            packet_text_hash=source_receipt.packet_text_hash,
        )
        project_binding = DistillationProjectBinding(
            project_id=project.project_id,
            project_revision=project.revision,
            project_fingerprint=project.fingerprint,
        )
        knowledge = DistillationKnowledgeBinding(
            scope_id=scope.scope_id,
            scope_state_revision=scope.state_revision,
            knowledge_fingerprint=knowledge_fingerprint,
            project_kb_state_id=project_kb_state_id,
            project_kb_manifest_hash=project_kb_manifest_hash,
        )
        invocation_id = distillation_invocation_id(
            source_receipt_id=source.source_receipt_id,
            project_id=project_binding.project_id,
            idempotency_key=idempotency_key,
        )
        anchors, text_by_anchor_id = self._build_anchor_set(
            source_receipt=source_receipt,
            text=text,
        )
        # The prepare endpoint is author-clickable and must be idempotent.
        # Bind the root materialization time to the immutable source receipt so
        # repeated prepares reconstruct the same catalog/request/root receipt
        # instead of turning wall-clock time into a false conflict.
        now = source_receipt.created_at.astimezone(UTC)
        catalog = build_distillation_anchor_catalog(
            invocation_id=invocation_id,
            source_import_id=source.source_import_id,
            source_receipt_id=source.source_receipt_id,
            source_receipt_hash=source.source_receipt_hash,
            packet_text_hash=source.packet_text_hash,
            packet_char_count=len(text),
            anchors=anchors,
            created_at=now,
        )
        request_hash = distillation_invocation_request_hash(
            source=source,
            project=project_binding,
            knowledge=knowledge,
            instruction=instruction,
            provider=provider_binding,
            anchor_catalog_hash=catalog.catalog_hash,
            retry_of=None,
        )
        payload = DistillationPreparedPayload(
            source=source,
            project=project_binding,
            knowledge=knowledge,
            instruction=instruction,
            provider=provider,
            anchor_catalog_id=catalog.catalog_id,
            anchor_catalog_hash=catalog.catalog_hash,
            anchor_count=catalog.anchor_count,
            output_contract_hash=self._output_contract_hash(instruction),
            allowed_tools=DISTILLATION_ALLOWED_TOOL_NAMES,
            request_hash=request_hash,
            retry_of=None,
        )
        receipt = build_distillation_receipt(
            invocation_id=invocation_id,
            ordinal=1,
            parent_receipt_id=None,
            expected_invocation_revision=0,
            payload=payload,
            created_at=now,
        )

        try:
            snapshot = self.invocation_store.create_prepared(
                receipt=receipt,
                idempotency_key=idempotency_key,
            )
        except DistillationInvocationStoreNotFound as exc:
            raise SourceImportDistillationPrepareError(
                str(exc),
                code="distillation_prepare_binding_missing",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from exc
        except DistillationInvocationStoreError as exc:
            try:
                snapshot = self.invocation_store.get(invocation_id)
            except DistillationInvocationStoreError:
                raise SourceImportDistillationPrepareError(
                    str(exc),
                    code="distillation_prepare_conflict",
                    status_code=status.HTTP_409_CONFLICT,
                ) from exc
            self._assert_existing_snapshot_matches(
                snapshot=snapshot,
                source=source,
                project=project_binding,
                knowledge=knowledge,
                instruction=instruction,
                provider=provider,
                anchor_catalog=catalog,
            )

        return SourceImportDistillationPrepareResult(
            snapshot=snapshot,
            anchor_catalog=catalog,
            text_by_anchor_id=text_by_anchor_id,
        )

    def _read_verified_job(self, source_import_id: str) -> AnySourceImportJob:
        payload = self.database.get_source_import_job(source_import_id)
        if payload is None:
            raise SourceImportDistillationPrepareError(
                "Source import was not found",
                code="source_import_not_found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        try:
            return parse_source_import_job(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise SourceImportDistillationPrepareError(
                "Stored source import job failed its contract",
                code="source_import_invalid",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc

    def _read_verified_source_receipt(
        self,
        source_import_id: str,
        job: AnySourceImportJob,
    ) -> AnySourceImportExecutionReceipt:
        payload = self.database.get_source_import_execution_receipt(source_import_id)
        if payload is None:
            raise SourceImportDistillationPrepareError(
                "Source import has no verified execution receipt",
                code="source_import_receipt_missing",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        try:
            receipt = parse_source_import_execution_receipt(payload)
            validate_source_import_receipt_job(receipt, job)
        except (TypeError, ValueError, ValidationError) as exc:
            raise SourceImportDistillationPrepareError(
                "Source import execution receipt failed its contract",
                code="source_import_receipt_invalid",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not receipt.distillation_invocation_id.startswith(
            "source-import-invocation-"
        ):
            raise SourceImportDistillationPrepareError(
                "Source import receipt must remain an immutable legacy draft receipt",
                code="source_import_distillation_identity_mutated",
                status_code=status.HTTP_409_CONFLICT,
            )
        return receipt

    def _read_project_binding(
        self,
        request: SourceImportDistillationPrepareRequest,
    ) -> ProjectBindingRecord:
        project = self.database.get_project_binding(request.project_id)
        if project is None:
            raise SourceImportDistillationPrepareError(
                "Project binding was not found",
                code="project_binding_not_found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != project.revision
        ):
            raise SourceImportDistillationPrepareError(
                "Project binding revision changed before distillation prepare",
                code="project_binding_stale",
                status_code=status.HTTP_409_CONFLICT,
                retryable=True,
                details={
                    "expected_project_revision": request.expected_project_revision,
                    "current_project_revision": project.revision,
                },
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != project.fingerprint
        ):
            raise SourceImportDistillationPrepareError(
                "Project binding fingerprint changed before distillation prepare",
                code="project_binding_stale",
                status_code=status.HTTP_409_CONFLICT,
                retryable=True,
                details={
                    "expected_project_fingerprint": request.expected_project_fingerprint,
                    "current_project_fingerprint": project.fingerprint,
                },
            )
        return project

    def _read_source_text(
        self,
        job: AnySourceImportJob,
        receipt: AnySourceImportExecutionReceipt,
    ) -> str:
        if job.source_packet is None:
            raise SourceImportDistillationPrepareError(
                "Source import has no prepared source packet",
                code="source_packet_missing",
                status_code=status.HTTP_409_CONFLICT,
            )
        path = Path(job.source_packet.text_path)
        if _is_link_or_junction(path):
            raise SourceImportDistillationPrepareError(
                "Source packet text is a link",
                code="source_packet_tampered",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SourceImportDistillationPrepareError(
                "Source packet text is missing",
                code="source_packet_missing",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from exc
        if not resolved.is_file() or _is_link_or_junction(resolved):
            raise SourceImportDistillationPrepareError(
                "Source packet text is not a regular file",
                code="source_packet_tampered",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            resolved.relative_to(self.source_state_root)
        except ValueError as exc:
            raise SourceImportDistillationPrepareError(
                "Source packet text escaped Runtime source state",
                code="source_packet_escaped_runtime_state",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        raw = resolved.read_bytes()
        observed_hash = hashlib.sha256(raw).hexdigest()
        if observed_hash != receipt.packet_text_hash:
            raise SourceImportDistillationPrepareError(
                "Source packet text changed after source receipt publication",
                code="source_packet_tampered",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceImportDistillationPrepareError(
                "Source packet text is not valid UTF-8",
                code="source_packet_invalid_text",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not text:
            raise SourceImportDistillationPrepareError(
                "Source packet text is empty",
                code="source_packet_empty",
                status_code=status.HTTP_409_CONFLICT,
            )
        return text

    def _ensure_project_scope_and_source_binding(
        self,
        *,
        source_import_id: str,
        project_id: str,
    ):
        try:
            scope = self.knowledge_service.get_scope(project_id=project_id)
            self.knowledge_service.store.bind_source_import_scope(
                source_import_id,
                scope.scope_id,
            )
            return self.knowledge_service.get_scope(project_id=project_id)
        except KnowledgeStoreConflict as exc:
            raise SourceImportDistillationPrepareError(
                "Source import is already bound to another knowledge scope",
                code="knowledge_scope_conflict",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        except (KnowledgeStoreError, KnowledgeServiceError) as exc:
            raise SourceImportDistillationPrepareError(
                str(exc),
                code="knowledge_scope_unavailable",
                status_code=status.HTTP_409_CONFLICT,
                retryable=True,
            ) from exc
        except Exception as exc:
            raise SourceImportDistillationPrepareError(
                str(exc),
                code="knowledge_scope_unavailable",
                status_code=status.HTTP_409_CONFLICT,
                retryable=True,
            ) from exc

    def _current_project_kb_binding(
        self,
        project_id: str,
    ) -> tuple[str | None, str | None]:
        if self.project_kb_service is None:
            return None, None
        try:
            record = self.project_kb_service.get_state(project_id)
        except ProjectKbTamperedError as exc:
            raise SourceImportDistillationPrepareError(
                str(exc),
                code="project_kb_tampered",
                status_code=status.HTTP_409_CONFLICT,
                details=exc.details,
            ) from exc
        except ProjectKbServiceError:
            return None, None
        if record.status is not ProjectKbStatus.CURRENT:
            return None, None
        if record.manifest is None:
            return None, None
        return record.manifest.state_id, record.manifest.manifest_hash

    def _active_instruction_binding(self, project_id: str) -> DistillationInstructionBinding:
        assert self.managed_skill_service is not None
        try:
            version = self.managed_skill_service.get_active_declaration(
                project_id,
                DISTILLATION_INSTRUCTION_SKILL_ID,
            )
        except Exception as exc:
            raise SourceImportDistillationPrepareError(
                "novel-distillation.deep-instruction is not active for this project",
                code="distillation_instruction_skill_inactive",
                status_code=status.HTTP_409_CONFLICT,
                retryable=True,
            ) from exc
        activation = self._effective_activation(
            project_id=project_id,
            skill_version_id=version.skill_version_id,
        )
        instruction_map = self._read_instruction_map(version.instruction_map_path)
        materialization = {
            "schema": "zhimeng.distillation-instruction-materialization.v1",
            "skill_id": DISTILLATION_INSTRUCTION_SKILL_ID,
            "skill_version_id": version.skill_version_id,
            "skill_version": version.manifest.version,
            "manifest_fingerprint": version.manifest_fingerprint,
            "package_fingerprint": version.package_fingerprint,
            "activation_receipt_id": activation["receipt_id"],
            "activation_receipt_hash": activation["receipt_hash"],
            "activation_state_revision": activation["state_revision"],
            "effective_scope": activation["effective_scope"],
            "instruction_map": instruction_map,
        }
        instruction_hash = canonical_json_hash(
            {
                "schema": "zhimeng.distillation-managed-instruction.v1",
                "instruction_map": instruction_map,
            }
        )
        materialization_hash = canonical_json_hash(
            {**materialization, "instruction_hash": instruction_hash}
        )
        return DistillationInstructionBinding(
            skill_id=DISTILLATION_INSTRUCTION_SKILL_ID,
            skill_version_id=version.skill_version_id,
            skill_version=version.manifest.version,
            manifest_fingerprint=version.manifest_fingerprint,
            package_fingerprint=version.package_fingerprint,
            activation_receipt_id=str(activation["receipt_id"]),
            activation_receipt_hash=str(activation["receipt_hash"]),
            activation_state_revision=int(activation["state_revision"]),
            effective_scope=activation["effective_scope"],  # type: ignore[arg-type]
            instruction_materialization_id=(
                f"distillation-instruction-materialization-{materialization_hash[:32]}"
            ),
            instruction_materialization_hash=materialization_hash,
            instruction_hash=instruction_hash,
        )

    def _resolve_provider(self) -> AgentProvider | None:
        if self._provider_resolver is not None:
            return self._provider_resolver()
        return self.provider

    def _provider_binding(
        self,
        provider: AgentProvider,
    ) -> DistillationProviderBinding:
        return DistillationProviderBinding(
            executor=provider.identity,
            capabilities=provider.capabilities,
            requested_model=_optional_string(getattr(provider, "model", None)),
            accepted_reported_models=tuple(
                sorted(
                    {
                        str(value).strip()
                        for value in getattr(
                            provider,
                            "accepted_reported_models",
                            (),
                        )
                        if str(value).strip()
                    }
                )
            ),
            dispatch_config_fingerprint=provider.dispatch_config_fingerprint,
            credential_reference=_optional_string(
                getattr(provider, "credential_reference", None)
            ),
            tool_definitions_hash=self.tool_definitions_hash,
        )

    def _build_anchor_set(
        self,
        *,
        source_receipt: AnySourceImportExecutionReceipt,
        text: str,
    ) -> tuple[tuple[DistillationAnchor, ...], dict[str, str]]:
        anchors: list[DistillationAnchor] = []
        text_by_anchor_id: dict[str, str] = {}
        ordinal = 1
        for start in range(0, len(text), MAX_ANCHOR_TEXT_CHARS):
            chunk = text[start : start + MAX_ANCHOR_TEXT_CHARS]
            if not chunk:
                continue
            anchor = build_distillation_anchor(
                source_receipt_id=source_receipt.receipt_id,
                packet_text_hash=source_receipt.packet_text_hash,
                level=DistillationAnchorLevel.SMALL,
                ordinal=ordinal,
                char_start=start,
                text=chunk,
            )
            anchors.append(anchor)
            text_by_anchor_id[anchor.anchor_id] = chunk
            ordinal += 1
        if not anchors:
            raise SourceImportDistillationPrepareError(
                "Source packet produced no readable anchors",
                code="distillation_anchor_catalog_empty",
                status_code=status.HTTP_409_CONFLICT,
            )
        return tuple(anchors), text_by_anchor_id

    def _default_idempotency_key(
        self,
        *,
        source_receipt: AnySourceImportExecutionReceipt,
        project: ProjectBindingRecord,
        scope_id: str,
        scope_state_revision: int,
        knowledge_fingerprint: str,
        instruction: DistillationInstructionBinding,
        provider: DistillationProviderBinding,
    ) -> str:
        digest = canonical_json_hash(
            {
                "schema": "zhimeng.distillation-prepare-idempotency.v1",
                "source_receipt_id": source_receipt.receipt_id,
                "source_receipt_hash": source_receipt.receipt_hash,
                "source_packet_fingerprint": source_receipt.source_packet_fingerprint,
                "packet_text_hash": source_receipt.packet_text_hash,
                "project_id": project.project_id,
                "project_revision": project.revision,
                "project_fingerprint": project.fingerprint,
                "scope_id": scope_id,
                "scope_state_revision": scope_state_revision,
                "knowledge_fingerprint": knowledge_fingerprint,
                "instruction": instruction.model_dump(mode="json"),
                "provider": provider.model_dump(mode="json"),
            }
        )
        return f"distillation-idempotency-{digest[:32]}"

    def _effective_activation(
        self,
        *,
        project_id: str,
        skill_version_id: str,
    ) -> dict[str, object]:
        project_scope_key = f"project:{project_id}"
        with self.database.connection() as connection:
            local = connection.execute(
                """
                SELECT * FROM skill_activation
                WHERE scope_key = ? AND skill_id = ?
                """,
                (project_scope_key, DISTILLATION_INSTRUCTION_SKILL_ID),
            ).fetchone()
            if local is not None and str(local["state"]) == "enabled":
                row = local
                scope_key = project_scope_key
                effective_scope = "project"
            else:
                row = connection.execute(
                    """
                    SELECT * FROM skill_activation
                    WHERE scope_key = 'application' AND skill_id = ?
                    """,
                    (DISTILLATION_INSTRUCTION_SKILL_ID,),
                ).fetchone()
                scope_key = "application"
                effective_scope = "application"
            if (
                row is None
                or str(row["state"]) != "enabled"
                or str(row["active_version_id"]) != skill_version_id
            ):
                raise SourceImportDistillationPrepareError(
                    "Active distillation instruction Skill changed before prepare",
                    code="distillation_instruction_skill_stale",
                    status_code=status.HTTP_409_CONFLICT,
                    retryable=True,
                )
            receipt = connection.execute(
                """
                SELECT * FROM skill_activation_receipt
                WHERE scope_key = ? AND skill_id = ? AND state_revision = ?
                """,
                (
                    scope_key,
                    DISTILLATION_INSTRUCTION_SKILL_ID,
                    int(row["state_revision"]),
                ),
            ).fetchone()
            if receipt is None:
                raise SourceImportDistillationPrepareError(
                    "Active distillation instruction Skill has no activation receipt",
                    code="distillation_instruction_receipt_missing",
                    status_code=status.HTTP_409_CONFLICT,
                )
            return {
                "receipt_id": str(receipt["id"]),
                "receipt_hash": str(receipt["receipt_hash"]),
                "state_revision": int(row["state_revision"]),
                "effective_scope": effective_scope,
            }

    def _read_instruction_map(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            raise SourceImportDistillationPrepareError(
                "Active distillation instruction Skill has no instruction map",
                code="distillation_instruction_map_missing",
                status_code=status.HTTP_409_CONFLICT,
            )
        if _is_link_or_junction(path):
            raise SourceImportDistillationPrepareError(
                "Active distillation instruction map is a link",
                code="distillation_instruction_map_tampered",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            raw = path.resolve(strict=True).read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceImportDistillationPrepareError(
                "Active distillation instruction map is unreadable",
                code="distillation_instruction_map_invalid",
                status_code=status.HTTP_409_CONFLICT,
            ) from exc
        if not isinstance(payload, dict):
            raise SourceImportDistillationPrepareError(
                "Active distillation instruction map must be an object",
                code="distillation_instruction_map_invalid",
                status_code=status.HTTP_409_CONFLICT,
            )
        return payload

    def _output_contract_hash(self, instruction: DistillationInstructionBinding) -> str:
        return canonical_json_hash(
            {
                "schema": "zhimeng.distillation-output-contract.v1",
                "instruction_materialization_id": (
                    instruction.instruction_materialization_id
                ),
                "instruction_materialization_hash": (
                    instruction.instruction_materialization_hash
                ),
                "instruction_hash": instruction.instruction_hash,
                "required_card_types": ["work", "voice", "validator"],
                "allowed_tools": list(DISTILLATION_ALLOWED_TOOL_NAMES),
                "formal_draft_allowed": False,
                "requires_author_review": True,
            }
        )

    def _assert_existing_snapshot_matches(
        self,
        *,
        snapshot: DistillationInvocationSnapshot,
        source: DistillationSourceBinding,
        project: DistillationProjectBinding,
        knowledge: DistillationKnowledgeBinding,
        instruction: DistillationInstructionBinding,
        provider: DistillationProviderBinding,
        anchor_catalog: DistillationAnchorCatalog,
    ) -> None:
        invocation = snapshot.invocation
        if (
            invocation.source != source
            or invocation.project != project
            or invocation.knowledge != knowledge
            or invocation.instruction != instruction
            or invocation.provider != provider
            or invocation.anchor_catalog_id != anchor_catalog.catalog_id
            or invocation.anchor_catalog_hash != anchor_catalog.catalog_hash
        ):
            raise SourceImportDistillationPrepareError(
                "Existing distillation invocation does not match current prepare bindings",
                code="distillation_invocation_idempotency_conflict",
                status_code=status.HTTP_409_CONFLICT,
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("distillation prepare clock must be timezone-aware")
        return now.astimezone(UTC)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        checker = getattr(Path, "is_junction", None)
        return bool(checker is not None and checker(path))
    except OSError:
        return False


__all__ = [
    "SourceImportDistillationPrepareError",
    "SourceImportDistillationPrepareRequest",
    "SourceImportDistillationPrepareResult",
    "SourceImportDistillationPrepareService",
]
