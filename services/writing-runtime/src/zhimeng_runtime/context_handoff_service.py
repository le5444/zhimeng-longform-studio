from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhimeng_runtime import __version__
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    canonical_json,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import ContextStore, ContextStoreError
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.managed_skill_service import ManagedSkillError
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.skill_handoff_models import SkillHandoffRunRecord

from zhimeng_runtime.context_handoff_models import (
    ContextHandoffActivation,
    ContextHandoffApproval,
    ContextHandoffDecisionRequest,
    ContextHandoffDownstream,
    ContextHandoffIndexEntry,
    ContextHandoffPermissions,
    ContextHandoffPrepareRequest,
    ContextHandoffProjectRef,
    ContextHandoffResponse,
    ContextHandoffRouteRequest,
    ContextHandoffRunRecord,
    ContextHandoffUpstream,
    ContextHandoffWorkflowRef,
)


CONTEXT_HANDOFF_KEY = "novel-kb-import-index-to-assemble-context"
CONTEXT_HANDOFF_SKILL_ID = "novel-kb.context-handoff"
REQUEST_SCHEMA = "zhimeng.novel-kb-context-handoff-request.v1"
O2D_HANDOFF_KEY = "novel-distillation-to-kb-manager"


class ContextHandoffError(RuntimeError):
    code = "context_handoff_error"
    status_code = 409
    retryable = False

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class ContextHandoffNotFound(ContextHandoffError):
    code = "context_handoff_not_found"
    status_code = 404


class ContextHandoffUnavailable(ContextHandoffError):
    code = "context_handoff_unavailable"
    status_code = 503
    retryable = True


class ContextHandoffInvalid(ContextHandoffError):
    code = "context_handoff_invalid"
    status_code = 422


class ContextHandoffConflict(ContextHandoffError):
    code = "context_handoff_conflict"
    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _compact_unique(values: list[object] | tuple[object, ...], *, limit: int = 64) -> list[str]:
    seen: list[str] = []
    for value in values:
        text = " ".join(str(value).split())[:160]
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


class ContextHandoffService:
    def __init__(
        self,
        store: SqliteStore,
        context_store: ContextStore,
        skill_executor: SkillExecutor | None,
        *,
        runtime_state_root: Path,
    ) -> None:
        self.store = store
        self.context_store = context_store
        self.skill_executor = skill_executor
        self.runtime_state_root = runtime_state_root
        self.handoff_root = runtime_state_root / "context-handoffs"

    def get(self, project_id: str) -> ContextHandoffResponse:
        binding = self._require_binding(project_id)
        row = self._latest_row(project_id)
        if row is not None:
            return ContextHandoffResponse(record=self._record_from_row(row))
        return ContextHandoffResponse(record=self._awaiting_upstream_record(binding))

    def prepare(
        self,
        project_id: str,
        request: ContextHandoffPrepareRequest,
    ) -> ContextHandoffResponse:
        binding = self._require_fresh_binding(
            project_id,
            request.expected_project_revision,
            request.expected_project_fingerprint,
        )
        activation = self._active_activation(project_id)
        upstream = self._build_upstream(project_id)
        if upstream is None:
            raise ContextHandoffInvalid(
                "本项目还没有 O2-D 受控 distillation_index 入口：需要先完成 "
                "novel-distillation -> novel-kb-manager handoff。"
            )
        route_terms = _compact_unique(
            [
                request.profile,
                request.stage,
                request.chapter_id,
                *[term for entry in upstream.entries for term in entry.route_terms],
                *[tag for entry in upstream.entries for tag in entry.tags],
            ]
        )
        route = ContextHandoffRouteRequest(
            profile=request.profile,
            stage=request.stage,
            chapter_id=request.chapter_id,
            task_intent=request.task_intent,
            route_terms=route_terms,
        )
        workflow = self._workflow_ref(
            project_id,
            upstream.source_handoff_id,
            upstream.distillation_index_hash,
            route,
        )
        context_handoff_id = _digest_id(
            "context-handoff-",
            project_id,
            CONTEXT_HANDOFF_KEY,
            upstream.source_handoff_id,
            upstream.distillation_index_hash,
            binding.fingerprint,
            route.profile,
            route.stage,
            route.chapter_id,
            route.task_intent,
        )
        now = _utcnow()
        record = ContextHandoffRunRecord(
            context_handoff_id=context_handoff_id,
            handoff_key=CONTEXT_HANDOFF_KEY,
            status="prepared",
            project=ContextHandoffProjectRef(
                project_id=project_id,
                revision=binding.revision,
                fingerprint=binding.fingerprint,
            ),
            workflow=workflow,
            upstream=upstream,
            downstream=ContextHandoffDownstream(),
            route=route,
            activation=activation,
            approval=ContextHandoffApproval(approved=False),
            permissions=ContextHandoffPermissions(
                read=("bound_project_snapshot", "run_staging"),
                write=("run_staging", "runtime_context_store"),
                network=False,
                secrets=(),
            ),
            constraints=self._constraints(),
            formal_draft_allowed=False,
            created_at=now,
            updated_at=now,
        )
        self._upsert_record(record)
        return ContextHandoffResponse(record=record)

    def approve(
        self,
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        record = self._require_row_record(project_id, request.expected_context_handoff_id)
        if record.status in {"disabled", "running"}:
            raise ContextHandoffConflict("当前 Context handoff 状态不能批准。")
        self._assert_not_stale(record)
        now = _utcnow()
        record = record.model_copy(
            update={
                "status": "approved",
                "approval": ContextHandoffApproval(
                    approved=True,
                    approved_by="author",
                    approved_at=now,
                    risk_acknowledged=True,
                ),
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
        )
        self._update_record(record)
        return ContextHandoffResponse(record=record)

    def reject(
        self,
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        record = self._require_row_record(project_id, request.expected_context_handoff_id)
        if record.status in {"running", "succeeded"}:
            raise ContextHandoffConflict("运行中或已成功的 Context handoff 不能改成拒绝。")
        record = record.model_copy(
            update={
                "status": "rejected",
                "approval": ContextHandoffApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._update_record(record)
        return ContextHandoffResponse(record=record)

    def disable(
        self,
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        record = self._require_row_record(project_id, request.expected_context_handoff_id)
        if record.status == "running":
            raise ContextHandoffConflict("运行中的 Context handoff 不能禁用。")
        record = record.model_copy(
            update={
                "status": "disabled",
                "approval": ContextHandoffApproval(approved=False),
                "updated_at": _utcnow(),
            }
        )
        self._update_record(record)
        return ContextHandoffResponse(record=record)

    async def run(
        self,
        project_id: str,
        request: ContextHandoffDecisionRequest,
    ) -> ContextHandoffResponse:
        if self.skill_executor is None:
            raise ContextHandoffUnavailable("Managed Skill executor is unavailable")
        record = self._require_row_record(project_id, request.expected_context_handoff_id)
        if record.status == "succeeded":
            return ContextHandoffResponse(record=record)
        if record.status == "disabled":
            raise ContextHandoffConflict("作者已禁用该 Context handoff，不能运行。")
        if record.status != "approved" or not record.approval.approved:
            raise ContextHandoffConflict("必须先由作者批准并确认风险后才能生成 Context Pack。")
        self._assert_not_stale(record)
        try:
            current_upstream = self._build_upstream(project_id)
        except ContextHandoffError as exc:
            failed = self._failed_record(
                record,
                "upstream_invalid",
                f"O2-D 受控入口不可用或已被篡改：{exc}",
            )
            self._update_record(failed)
            raise ContextHandoffConflict(failed.error_message or "upstream invalid") from exc
        if current_upstream is None or record.upstream is None:
            failed = self._failed_record(
                record,
                "upstream_missing",
                "O2-D 受控入口不可用，不能生成 Context Pack。",
            )
            self._update_record(failed)
            raise ContextHandoffConflict(failed.error_message or "upstream missing")
        if (
            current_upstream.source_handoff_id != record.upstream.source_handoff_id
            or current_upstream.source_skill_receipt_hash
            != record.upstream.source_skill_receipt_hash
            or current_upstream.distillation_index_hash
            != record.upstream.distillation_index_hash
            or current_upstream.distillation_index_artifact_file_hash
            != record.upstream.distillation_index_artifact_file_hash
        ):
            failed = self._failed_record(
                record,
                "upstream_stale",
                "O2-D distillation_index 入口已变化或被篡改，需要重新准备并重新批准。",
            )
            self._update_record(failed)
            raise ContextHandoffConflict(failed.error_message or "upstream stale")
        running = record.model_copy(update={"status": "running", "updated_at": _utcnow()})
        self._update_record(running)
        try:
            outcome_record = await self._execute_approved(project_id, running)
        except Exception as exc:
            failed = self._failed_record(running, "execution_failed", str(exc)[:4000])
            self._update_record(failed)
            raise
        self._update_record(outcome_record)
        return ContextHandoffResponse(record=outcome_record)

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.store.get_project_binding(project_id)
        if binding is None:
            raise ContextHandoffNotFound("Project binding was not found")
        return binding

    def _require_fresh_binding(
        self,
        project_id: str,
        revision: int,
        fingerprint: str,
    ) -> ProjectBindingRecord:
        binding = self._require_binding(project_id)
        if binding.revision != revision or binding.fingerprint != fingerprint:
            raise ContextHandoffConflict("项目快照已变化，请刷新后重新准备 Context Pack。")
        return binding

    def _assert_not_stale(self, record: ContextHandoffRunRecord) -> None:
        binding = self._require_binding(record.project.project_id)
        if (
            binding.revision != record.project.revision
            or binding.fingerprint != record.project.fingerprint
        ):
            raise ContextHandoffConflict(
                "项目 revision/fingerprint 已变化，旧 Context handoff 不能继续；"
                "请重新准备并重新批准。"
            )

    def _active_activation(self, project_id: str) -> ContextHandoffActivation:
        if self.skill_executor is None:
            raise ContextHandoffUnavailable("Managed Skill executor is unavailable")
        try:
            skill = self.skill_executor.active_skill_declaration(
                project_id,
                CONTEXT_HANDOFF_SKILL_ID,
            )
        except ManagedSkillError as exc:
            raise ContextHandoffConflict(
                "assemble-context 受管 adapter 尚未导入/启用，不能路由。"
            ) from exc
        return ContextHandoffActivation(
            skill_version_id=skill.skill_version_id,
            skill_version=skill.manifest.version,
            manifest_fingerprint=skill.manifest_fingerprint,
            package_fingerprint=skill.package_fingerprint,
            effective_scope="project",
        )

    def _build_upstream(self, project_id: str) -> ContextHandoffUpstream | None:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM skill_handoff_run
                WHERE project_binding_id = ? AND handoff_key = ? AND status = 'succeeded'
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (project_id, O2D_HANDOFF_KEY),
            ).fetchone()
        if row is None:
            return None
        source = SkillHandoffRunRecord.model_validate_json(str(row["payload_json"]))
        if source.result is None or source.upstream is None or source.activation is None:
            raise ContextHandoffConflict("O2-D handoff 记录缺少 result/upstream/activation。")
        result = source.result
        distillation_index_hash = str(result.get("distillation_index_hash") or "")
        artifact_path = source.output_artifact_path or ""
        if not artifact_path:
            raise ContextHandoffConflict("O2-D handoff 缺少 distillation_index artifact 路径。")
        artifact_file = Path(artifact_path)
        try:
            artifact_text = artifact_file.read_text(encoding="utf-8")
            artifact = json.loads(artifact_text)
        except (OSError, json.JSONDecodeError) as exc:
            raise ContextHandoffConflict("O2-D distillation_index artifact 不可读取。") from exc
        if not isinstance(artifact, dict):
            raise ContextHandoffConflict("O2-D distillation_index artifact 结构不合法。")
        artifact_hash = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
        canonical_artifact_hash = canonical_json_hash(artifact)
        if artifact_hash != distillation_index_hash or canonical_artifact_hash != distillation_index_hash:
            raise ContextHandoffConflict("O2-D distillation_index artifact hash 与 receipt 不一致。")
        if artifact.get("no_full_card_text_copied") is not True:
            raise ContextHandoffConflict(
                "O2-D distillation_index 必须证明 no_full_card_text_copied=true。"
            )
        entries = artifact.get("entries")
        if not isinstance(entries, list) or len(entries) != 3:
            raise ContextHandoffConflict("O2-D distillation_index 必须包含三类入口。")
        forbidden_fields = {"summary", "reader_promise", "type_payload", "content"}
        parsed_entries: list[ContextHandoffIndexEntry] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ContextHandoffConflict("O2-D distillation_index entry 不是对象。")
            if forbidden_fields.intersection(entry):
                raise ContextHandoffConflict(
                    "O2-D distillation_index entry 复制了不允许的卡全文字段。"
                )
            parsed_entries.append(ContextHandoffIndexEntry.model_validate(entry))
        if [entry.card_type for entry in parsed_entries] != ["work", "voice", "validator"]:
            raise ContextHandoffConflict(
                "O2-D distillation_index entry 顺序必须是 work -> voice -> validator。"
            )
        if (
            source.skill_receipt_id is None
            or source.skill_receipt_hash is None
            or source.result_hash is None
        ):
            raise ContextHandoffConflict("O2-D handoff 缺少 skill receipt/result hash。")
        return ContextHandoffUpstream(
            source_handoff_id=source.handoff_id,
            source_skill_receipt_id=source.skill_receipt_id,
            source_skill_receipt_hash=source.skill_receipt_hash,
            source_result_hash=source.result_hash,
            distillation_index_hash=distillation_index_hash,
            distillation_index_artifact_path=str(artifact_file.resolve()),
            distillation_index_artifact_file_hash=artifact_hash,
            source_project_revision=source.project.revision,
            source_project_fingerprint=source.project.fingerprint,
            upstream_activation_skill_version_id=source.activation.skill_version_id,
            entries=(parsed_entries[0], parsed_entries[1], parsed_entries[2]),
            no_full_card_text_copied=True,
        )

    def _workflow_ref(
        self,
        project_id: str,
        source_handoff_id: str,
        distillation_index_hash: str,
        route: ContextHandoffRouteRequest,
    ) -> ContextHandoffWorkflowRef:
        plan_id = _digest_id(
            "workflow-plan-",
            project_id,
            CONTEXT_HANDOFF_KEY,
            source_handoff_id,
            distillation_index_hash,
            route.profile,
            route.stage,
            route.chapter_id,
        )
        checkpoint_id = _digest_id("workflow-checkpoint-", plan_id, "assemble-context")
        tool_call_id = "context_handoff_" + hashlib.sha256(
            f"{plan_id}:tool".encode()
        ).hexdigest()[:24]
        skill_call_id = "skill_call_" + hashlib.sha256(
            f"{plan_id}:skill".encode()
        ).hexdigest()[:24]
        plan_hash = canonical_json_hash(
            {
                "schema": "zhimeng.context-handoff-workflow-plan.v1",
                "workflow_plan_id": plan_id,
                "handoff_key": CONTEXT_HANDOFF_KEY,
                "source_handoff_id": source_handoff_id,
                "distillation_index_hash": distillation_index_hash,
                "route": route.model_dump(mode="json"),
                "formal_draft_allowed": False,
            }
        )
        checkpoint_hash = canonical_json_hash(
            {
                "schema": "zhimeng.context-handoff-checkpoint.v1",
                "workflow_plan_id": plan_id,
                "checkpoint_id": checkpoint_id,
                "tool_call_id": tool_call_id,
                "skill_call_id": skill_call_id,
                "plan_hash": plan_hash,
            }
        )
        return ContextHandoffWorkflowRef(
            workflow_plan_id=plan_id,
            workflow_plan_hash=plan_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            tool_call_id=tool_call_id,
            skill_call_id=skill_call_id,
        )

    async def _execute_approved(
        self,
        project_id: str,
        record: ContextHandoffRunRecord,
    ) -> ContextHandoffRunRecord:
        assert self.skill_executor is not None
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise ContextHandoffConflict("Context handoff 记录缺少 upstream/workflow/activation。")
        root = self.handoff_root / record.context_handoff_id
        snapshot = root / "snapshot"
        staging = root / "staging"
        for target in (snapshot, staging):
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
        (snapshot / "project_snapshot.json").write_text(
            canonical_json(record.project.model_dump(mode="json")),
            encoding="utf-8",
        )
        upstream_artifact = json.loads(
            Path(record.upstream.distillation_index_artifact_path).read_text(encoding="utf-8")
        )
        (snapshot / "distillation_index_handoff.json").write_text(
            canonical_json(upstream_artifact),
            encoding="utf-8",
        )
        request_payload = {
            "schema": REQUEST_SCHEMA,
            "context_handoff_id": record.context_handoff_id,
            "handoff_key": record.handoff_key,
            "project": record.project.model_dump(mode="json"),
            "workflow": {
                "workflow_plan_id": record.workflow.workflow_plan_id,
                "checkpoint_id": record.workflow.checkpoint_id,
                "tool_call_id": record.workflow.tool_call_id,
                "skill_call_id": record.workflow.skill_call_id,
                "plan_hash": record.workflow.workflow_plan_hash,
                "checkpoint_hash": record.workflow.checkpoint_hash,
            },
            "upstream": record.upstream.model_dump(mode="json"),
            "distillation_index": upstream_artifact,
            "downstream": record.downstream.model_dump(mode="json"),
            "route": record.route.model_dump(mode="json"),
            "activation": record.activation.model_dump(mode="json"),
            "approval": record.approval.model_dump(mode="json"),
            "permissions": record.permissions.model_dump(mode="json"),
            "constraints": record.constraints,
            "formal_draft_allowed": False,
        }
        outcome = await self.skill_executor.execute(
            project_id=project_id,
            project_revision=record.project.revision,
            project_fingerprint=record.project.fingerprint,
            run_id=record.workflow.workflow_plan_id,
            tool_call_id=record.workflow.tool_call_id,
            skill_id=CONTEXT_HANDOFF_SKILL_ID,
            input_payload=request_payload,
            project_snapshot=snapshot,
            run_staging=staging,
        )
        now = _utcnow()
        adapter_result_hash = canonical_json_hash(outcome.result) if outcome.result is not None else None
        adapter_artifact_hash = None
        adapter_artifact_path: str | None = None
        artifact = next(
            (item for item in outcome.artifacts if item.artifact_type == "context_pack_intent"),
            None,
        )
        if artifact is not None:
            if artifact.payload is not None:
                adapter_artifact_hash = canonical_json_hash(artifact.payload)
            if artifact.relative_path:
                adapter_artifact_path = str((staging / artifact.relative_path).resolve())
        if outcome.receipt.execution_status != "succeeded" or outcome.receipt.domain_status == "blocked":
            return record.model_copy(
                update={
                    "status": "failed",
                    "skill_receipt_id": outcome.receipt.receipt_id,
                    "skill_receipt_hash": outcome.receipt_hash,
                    "result_hash": adapter_result_hash,
                    "artifact_hash": adapter_artifact_hash,
                    "output_artifact_path": adapter_artifact_path,
                    "result": outcome.result if isinstance(outcome.result, dict) else None,
                    "error_code": outcome.receipt.error.code
                    if outcome.receipt.error
                    else "domain_blocked",
                    "error_message": outcome.receipt.error.message
                    if outcome.receipt.error
                    else "adapter blocked assemble-context",
                    "updated_at": now,
                }
            )
        if not self._adapter_result_valid(outcome.result, record):
            return record.model_copy(
                update={
                    "status": "failed",
                    "skill_receipt_id": outcome.receipt.receipt_id,
                    "skill_receipt_hash": outcome.receipt_hash,
                    "result_hash": adapter_result_hash,
                    "artifact_hash": adapter_artifact_hash,
                    "output_artifact_path": adapter_artifact_path,
                    "result": outcome.result if isinstance(outcome.result, dict) else None,
                    "error_code": "invalid_context_handoff_result",
                    "error_message": (
                        "受管 adapter 没有返回可校验 Context Pack intent，"
                        "本次 assemble-context 按失败关闭。"
                    ),
                    "updated_at": now,
                }
            )
        try:
            pack, route_receipt = self._persist_context_pack(record)
        except (ContextStoreError, ValueError) as exc:
            return record.model_copy(
                update={
                    "status": "failed",
                    "skill_receipt_id": outcome.receipt.receipt_id,
                    "skill_receipt_hash": outcome.receipt_hash,
                    "result_hash": adapter_result_hash,
                    "artifact_hash": adapter_artifact_hash,
                    "output_artifact_path": adapter_artifact_path,
                    "result": outcome.result if isinstance(outcome.result, dict) else None,
                    "error_code": "context_pack_persist_failed",
                    "error_message": str(exc)[:4096],
                    "updated_at": now,
                }
            )
        final_result = self._final_result(record, outcome.result, pack, route_receipt)
        return record.model_copy(
            update={
                "status": "succeeded",
                "skill_receipt_id": outcome.receipt.receipt_id,
                "skill_receipt_hash": outcome.receipt_hash,
                "result_hash": canonical_json_hash(final_result),
                "artifact_hash": adapter_artifact_hash,
                "output_artifact_path": adapter_artifact_path,
                "context_pack_id": pack.context_pack_id,
                "context_pack_hash": pack.pack_hash,
                "route_receipt_id": route_receipt.route_receipt_id,
                "route_receipt_hash": route_receipt.receipt_hash,
                "result": final_result,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
        )

    def _adapter_result_valid(
        self,
        result: object,
        record: ContextHandoffRunRecord,
    ) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("status") != "context_ready":
            return False
        if record.upstream is None:
            return False
        if result.get("distillation_index_hash") != record.upstream.distillation_index_hash:
            return False
        if result.get("downstream_target") != "novel-kb-manager.assemble-context":
            return False
        selected = result.get("selected_entries")
        if not isinstance(selected, list) or len(selected) != 3:
            return False
        return [item.get("card_type") for item in selected if isinstance(item, dict)] == [
            "work",
            "voice",
            "validator",
        ]

    def _persist_context_pack(self, record: ContextHandoffRunRecord):
        assert record.upstream is not None
        session_id = _digest_id("session-o2e-", record.context_handoff_id)
        self._ensure_session(record, session_id)
        created_at = _utcnow()
        project_kb_state_id = _digest_id(
            "project-kb-state-",
            record.project.project_id,
            record.upstream.distillation_index_hash,
        )
        request = ContextCreateRequest(
            objective=record.route.task_intent,
            profile=record.route.profile,
            active_chapter_id=record.route.chapter_id,
            expected_project_revision=record.project.revision,
            expected_project_fingerprint=record.project.fingerprint,
            project_kb_state_id=project_kb_state_id,
            project_kb_manifest_hash=record.upstream.distillation_index_hash,
            formal_knowledge_selector=None,
        )
        candidates = tuple(
            self._candidate_fragment(record, entry, priority=850 - index * 40)
            for index, entry in enumerate(record.upstream.entries)
        )
        pack = compile_context_pack(
            context_pack_id=_digest_id("context-pack-o2e-", record.context_handoff_id),
            session_id=session_id,
            project_id=record.project.project_id,
            history_through_ordinal=0,
            request=request,
            history=(),
            candidates=candidates,
            max_characters=64_000,
            max_tokens=64_000,
            knowledge_fingerprint=record.upstream.distillation_index_hash,
            created_at=created_at,
        )
        route_cards = tuple(
            NlclSelectedCard(
                card_id=entry.card_id,
                card_type=entry.card_type,
                title=entry.title,
                version_id=entry.version_id,
                content_hash=entry.content_hash,
                route_terms=tuple(entry.route_terms[:16]),
                recommended_profiles=(record.route.profile,),
                negative_routes=(),
                safety_summary=entry.safety_summary
                or "仅使用 distillation_index 入口，不复制正式卡全文。",
                feedback_layer=(
                    "L3_DISTILLATION_RETRIEVAL"
                    if entry.card_type == "work"
                    else "L5_DEAI_STYLE"
                    if entry.card_type == "voice"
                    else "L6_REVIEW_ACCEPTANCE"
                ),
            )
            for entry in record.upstream.entries
        )
        route_receipt = build_nlcl_route_receipt(
            pack,
            source=NlclRouteSource.EXTERNAL_DISTILLATION,
            route_cards=route_cards,
            extra_warnings=(
                "route_health_ready_not_validated",
                "project_truth_not_written",
            ),
        )
        self.context_store.create_pack(pack, nlcl_route=route_receipt)
        return pack, route_receipt

    def _ensure_session(self, record: ContextHandoffRunRecord, session_id: str) -> None:
        now = _utcnow().isoformat()
        executor_json = canonical_json(
            {
                "executor_id": "executor.context-handoff-runtime",
                "kind": "embedded",
                "implementation": "runtime-context-handoff-compiler",
                "version": __version__,
                "protocol_version": "1",
                "instance_id": "runtime.context-handoff",
            }
        )
        capabilities_json = canonical_json(
            {
                "protocol_version": "1",
                "names": ["checkpoints", "skills", "artifacts"],
                "extensions": {},
            }
        )
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT id, project_binding_id, last_message_ordinal
                FROM execution_session WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO execution_session(
                        id, project_binding_id, title, status, revision,
                        latest_project_revision, latest_project_fingerprint,
                        executor_json, capabilities_json, correlation_id,
                        last_run_ordinal, last_message_ordinal, created_at, updated_at
                    ) VALUES (?, ?, ?, 'idle', 1, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        session_id,
                        record.project.project_id,
                        "O2-E assemble-context",
                        record.project.revision,
                        record.project.fingerprint,
                        executor_json,
                        capabilities_json,
                        record.context_handoff_id,
                        now,
                        now,
                    ),
                )
            elif (
                str(existing["project_binding_id"]) != record.project.project_id
                or int(existing["last_message_ordinal"]) != 0
            ):
                raise ContextHandoffConflict(
                    "Context handoff session binding is stale or corrupted"
                )
            connection.commit()

    def _candidate_fragment(
        self,
        record: ContextHandoffRunRecord,
        entry: ContextHandoffIndexEntry,
        *,
        priority: int,
    ) -> ContextCandidateFragment:
        assert record.upstream is not None
        content = canonical_json(
            {
                "schema": "zhimeng.o2e-context-fragment-from-distillation-index.v1",
                "source_handoff_id": record.upstream.source_handoff_id,
                "distillation_index_hash": record.upstream.distillation_index_hash,
                "entry": {
                    "card_id": entry.card_id,
                    "card_type": entry.card_type,
                    "version_id": entry.version_id,
                    "content_hash": entry.content_hash,
                    "title": entry.title,
                    "route_terms": entry.route_terms,
                    "tags": entry.tags,
                    "safety_summary": entry.safety_summary,
                },
                "selected_reason": (
                    f"{entry.card_type} 入口匹配 "
                    f"{record.route.profile}/{record.route.stage}/{record.route.chapter_id} "
                    "写前任务。"
                ),
                "constraints": self._constraints(),
            }
        )
        return ContextCandidateFragment(
            source_id=f"knowledge-card:{entry.card_id}",
            source_type=f"distillation_index_entry:{entry.card_type}",
            title=entry.title,
            authority=ContextAuthority.FORMAL_KNOWLEDGE,
            version=entry.version_id,
            source_hash=entry.content_hash,
            content=content,
            priority=priority,
            mandatory=True,
            eligible=True,
            exclusion_reason=None,
            knowledge_provenance=None,
        )

    def _final_result(
        self,
        record: ContextHandoffRunRecord,
        adapter_result: object,
        pack: Any,
        route_receipt: Any,
    ) -> dict[str, object]:
        assert record.upstream is not None
        return {
            "schema": "zhimeng.novel-kb-context-handoff-result.v1",
            "context_handoff_id": record.context_handoff_id,
            "status": "context_ready",
            "source_handoff_id": record.upstream.source_handoff_id,
            "distillation_index_hash": record.upstream.distillation_index_hash,
            "downstream_target": "novel-kb-manager.assemble-context",
            "context_pack_id": pack.context_pack_id,
            "context_pack_hash": pack.pack_hash,
            "route_receipt_id": route_receipt.route_receipt_id,
            "route_receipt_hash": route_receipt.receipt_hash,
            "route_health": route_receipt.status.value,
            "route_evidence_status": "not_validated",
            "context_health": pack.readiness.value,
            "readiness": pack.readiness.value,
            "route_terms": record.route.route_terms,
            "profile": record.route.profile,
            "stage": record.route.stage,
            "chapter_id": record.route.chapter_id,
            "task_intent": record.route.task_intent,
            "selected_entries": [entry.model_dump(mode="json") for entry in record.upstream.entries],
            "selection_reasons": [
                f"{entry.card_type} 入口作为 {record.route.profile}/{record.route.stage} 的写前上下文证据。"
                for entry in record.upstream.entries
            ],
            "constraints": self._constraints(),
            "adapter_result": adapter_result if isinstance(adapter_result, dict) else None,
        }

    @staticmethod
    def _constraints() -> dict[str, object]:
        return {
            "no_full_card_text_copied": True,
            "formal_draft_allowed": False,
            "project_truth_not_written": True,
            "route_health_ready_is_not_validated": True,
            "writes_formal_prose": False,
        }

    def _awaiting_upstream_record(
        self,
        binding: ProjectBindingRecord,
    ) -> ContextHandoffRunRecord:
        now = _utcnow()
        return ContextHandoffRunRecord(
            context_handoff_id=_digest_id(
                "context-handoff-",
                binding.project_id,
                CONTEXT_HANDOFF_KEY,
                "awaiting",
            ),
            handoff_key=CONTEXT_HANDOFF_KEY,
            status="awaiting_upstream",
            project=ContextHandoffProjectRef(
                project_id=binding.project_id,
                revision=binding.revision,
                fingerprint=binding.fingerprint,
            ),
            workflow=None,
            upstream=None,
            downstream=ContextHandoffDownstream(),
            route=ContextHandoffRouteRequest(
                profile="chapter",
                stage="draft",
                chapter_id="CH001",
                task_intent="等待受控 distillation_index 入口后再装配 Context Pack。",
            ),
            activation=None,
            approval=ContextHandoffApproval(approved=False),
            permissions=ContextHandoffPermissions(
                read=("bound_project_snapshot", "run_staging"),
                write=("run_staging", "runtime_context_store"),
                network=False,
                secrets=(),
            ),
            constraints=self._constraints(),
            formal_draft_allowed=False,
            error_code="upstream_missing",
            error_message="先完成 O2-D distillation_index 交接，才可装配 assemble-context / Context Pack。",
            created_at=now,
            updated_at=now,
        )

    def _failed_record(
        self,
        record: ContextHandoffRunRecord,
        code: str,
        message: str,
    ) -> ContextHandoffRunRecord:
        return record.model_copy(
            update={
                "status": "failed",
                "error_code": code,
                "error_message": message[:4096],
                "updated_at": _utcnow(),
            }
        )

    def _row_payload(self, record: ContextHandoffRunRecord) -> str:
        return canonical_json(record.model_dump(mode="json", by_alias=True))

    def _upsert_record(self, record: ContextHandoffRunRecord) -> None:
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise ValueError("persisted context handoff record must be prepared")
        payload = self._row_payload(record)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO context_handoff_run(
                    id, handoff_key, project_binding_id, status, project_revision,
                    project_fingerprint, source_handoff_id, distillation_index_hash,
                    workflow_plan_id, workflow_plan_hash, checkpoint_id, checkpoint_hash,
                    skill_version_id, skill_receipt_id, skill_receipt_hash, result_hash,
                    artifact_hash, context_pack_id, context_pack_hash,
                    route_receipt_id, route_receipt_hash, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    project_revision=excluded.project_revision,
                    project_fingerprint=excluded.project_fingerprint,
                    distillation_index_hash=excluded.distillation_index_hash,
                    workflow_plan_hash=excluded.workflow_plan_hash,
                    checkpoint_hash=excluded.checkpoint_hash,
                    skill_version_id=excluded.skill_version_id,
                    skill_receipt_id=excluded.skill_receipt_id,
                    skill_receipt_hash=excluded.skill_receipt_hash,
                    result_hash=excluded.result_hash,
                    artifact_hash=excluded.artifact_hash,
                    context_pack_id=excluded.context_pack_id,
                    context_pack_hash=excluded.context_pack_hash,
                    route_receipt_id=excluded.route_receipt_id,
                    route_receipt_hash=excluded.route_receipt_hash,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                self._row_values(record, payload),
            )
            connection.commit()

    def _update_record(self, record: ContextHandoffRunRecord) -> None:
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise ValueError("persisted context handoff record must be prepared")
        payload = self._row_payload(record)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE context_handoff_run
                SET status = ?, skill_receipt_id = ?, skill_receipt_hash = ?,
                    result_hash = ?, artifact_hash = ?, context_pack_id = ?,
                    context_pack_hash = ?, route_receipt_id = ?, route_receipt_hash = ?,
                    payload_json = ?, updated_at = ?
                WHERE id = ? AND project_binding_id = ?
                """,
                (
                    record.status,
                    record.skill_receipt_id,
                    record.skill_receipt_hash,
                    record.result_hash,
                    record.artifact_hash,
                    record.context_pack_id,
                    record.context_pack_hash,
                    record.route_receipt_id,
                    record.route_receipt_hash,
                    payload,
                    record.updated_at.isoformat(),
                    record.context_handoff_id,
                    record.project.project_id,
                ),
            )
            connection.commit()

    def _row_values(
        self,
        record: ContextHandoffRunRecord,
        payload: str,
    ) -> tuple[object, ...]:
        assert record.upstream is not None and record.workflow is not None and record.activation is not None
        return (
            record.context_handoff_id,
            record.handoff_key,
            record.project.project_id,
            record.status,
            record.project.revision,
            record.project.fingerprint,
            record.upstream.source_handoff_id,
            record.upstream.distillation_index_hash,
            record.workflow.workflow_plan_id,
            record.workflow.workflow_plan_hash,
            record.workflow.checkpoint_id,
            record.workflow.checkpoint_hash,
            record.activation.skill_version_id,
            record.skill_receipt_id,
            record.skill_receipt_hash,
            record.result_hash,
            record.artifact_hash,
            record.context_pack_id,
            record.context_pack_hash,
            record.route_receipt_id,
            record.route_receipt_hash,
            payload,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        )

    def _latest_row(self, project_id: str):
        with self.store.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM context_handoff_run
                WHERE project_binding_id = ? AND handoff_key = ?
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (project_id, CONTEXT_HANDOFF_KEY),
            ).fetchone()

    def _row_by_id(self, project_id: str, handoff_id: str):
        with self.store.connection() as connection:
            return connection.execute(
                "SELECT * FROM context_handoff_run WHERE project_binding_id = ? AND id = ?",
                (project_id, handoff_id),
            ).fetchone()

    def _record_from_row(self, row) -> ContextHandoffRunRecord:
        return ContextHandoffRunRecord.model_validate_json(str(row["payload_json"]))

    def _require_row_record(
        self,
        project_id: str,
        handoff_id: str,
    ) -> ContextHandoffRunRecord:
        row = self._row_by_id(project_id, handoff_id)
        if row is None:
            raise ContextHandoffNotFound("Context handoff was not found")
        return self._record_from_row(row)


def raise_context_handoff_error(exc: ContextHandoffError) -> None:
    raise ApiError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
        details=exc.details,
        retryable=exc.retryable,
    ) from exc


__all__ = [
    "ContextHandoffService",
    "ContextHandoffError",
    "raise_context_handoff_error",
]
