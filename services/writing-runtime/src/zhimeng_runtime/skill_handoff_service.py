from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from zhimeng_runtime.context_models import canonical_json, canonical_json_hash
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.knowledge_models import KnowledgeScopedCardList
from zhimeng_runtime.knowledge_service import KnowledgeService, KnowledgeServiceError
from zhimeng_runtime.managed_skill_service import ManagedSkillError
from zhimeng_runtime.skill_executor import SkillExecutor
from zhimeng_runtime.skill_handoff_models import (
    SkillHandoffActivation,
    SkillHandoffApproval,
    SkillHandoffCardRef,
    SkillHandoffDecisionRequest,
    SkillHandoffDownstream,
    SkillHandoffPermissions,
    SkillHandoffPrepareRequest,
    SkillHandoffProjectRef,
    SkillHandoffResponse,
    SkillHandoffRunRecord,
    SkillHandoffUpstream,
    SkillHandoffWorkflowRef,
)

HANDOFF_KEY = "novel-distillation-to-kb-manager"
HANDOFF_SKILL_ID = "novel-distillation.kb-handoff"
REQUEST_SCHEMA = "zhimeng.novel-kb-handoff-request.v1"


class SkillHandoffError(RuntimeError):
    code = "skill_handoff_error"
    status_code = 409
    retryable = False

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class SkillHandoffNotFound(SkillHandoffError):
    code = "skill_handoff_not_found"
    status_code = 404


class SkillHandoffUnavailable(SkillHandoffError):
    code = "skill_handoff_unavailable"
    status_code = 503
    retryable = True


class SkillHandoffInvalid(SkillHandoffError):
    code = "skill_handoff_invalid"
    status_code = 422


class SkillHandoffConflict(SkillHandoffError):
    code = "skill_handoff_conflict"
    status_code = 409


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return f"{prefix}{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


def _compact_list(value: object, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value:
        text = " ".join(str(item).split())[:160]
        if text and text not in seen:
            seen.append(text)
        if len(seen) >= limit:
            break
    return seen


class SkillHandoffService:
    def __init__(
        self,
        store: SqliteStore,
        knowledge_service: KnowledgeService,
        skill_executor: SkillExecutor | None,
        *,
        runtime_state_root: Path,
    ) -> None:
        self.store = store
        self.knowledge_service = knowledge_service
        self.skill_executor = skill_executor
        self.runtime_state_root = runtime_state_root
        self.handoff_root = runtime_state_root / "skill-handoffs"

    def get(self, project_id: str) -> SkillHandoffResponse:
        binding = self._require_binding(project_id)
        row = self._latest_row(project_id)
        if row is not None:
            return SkillHandoffResponse(record=self._record_from_row(row))
        prepared = self._awaiting_upstream_record(binding)
        return SkillHandoffResponse(record=prepared)

    def prepare(self, project_id: str, request: SkillHandoffPrepareRequest) -> SkillHandoffResponse:
        binding = self._require_fresh_binding(project_id, request.expected_project_revision, request.expected_project_fingerprint)
        activation = self._active_activation(project_id)
        upstream = self._build_upstream(project_id, source_import_id=request.source_import_id)
        if upstream is None:
            raise SkillHandoffInvalid(
                "本项目还没有可交接的正式蒸馏批次：需要先完成 work/voice/validator 三类知识正式化。"
            )
        workflow = self._workflow_ref(project_id, upstream.source_import_id, upstream.formalization_receipt_hash)
        handoff_id = _digest_id(
            "skill-handoff-",
            project_id,
            HANDOFF_KEY,
            upstream.source_import_id,
            upstream.formalization_receipt_hash,
        )
        now = _utcnow()
        record = SkillHandoffRunRecord(
            handoff_id=handoff_id,
            handoff_key=HANDOFF_KEY,
            status="prepared",
            project=SkillHandoffProjectRef(
                project_id=project_id,
                revision=binding.revision,
                fingerprint=binding.fingerprint,
            ),
            workflow=workflow,
            upstream=upstream,
            downstream=SkillHandoffDownstream(),
            activation=activation,
            approval=SkillHandoffApproval(approved=False),
            permissions=SkillHandoffPermissions(
                read=("bound_project_snapshot",),
                write=("run_staging",),
                network=False,
                secrets=(),
            ),
            formal_draft_allowed=False,
            created_at=now,
            updated_at=now,
        )
        self._upsert_record(record)
        return SkillHandoffResponse(record=record)

    def approve(self, project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        record = self._require_row_record(project_id, request.expected_handoff_id)
        if record.status in {"disabled", "running"}:
            raise SkillHandoffConflict("当前交接状态不能批准。")
        self._assert_not_stale(record)
        now = _utcnow()
        record = record.model_copy(update={
            "status": "approved",
            "approval": SkillHandoffApproval(
                approved=True,
                approved_by="author",
                approved_at=now,
                risk_acknowledged=True,
            ),
            "error_code": None,
            "error_message": None,
            "updated_at": now,
        })
        self._update_record(record)
        return SkillHandoffResponse(record=record)

    def reject(self, project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        record = self._require_row_record(project_id, request.expected_handoff_id)
        if record.status in {"running", "succeeded"}:
            raise SkillHandoffConflict("运行中或已成功的交接不能改成拒绝。")
        now = _utcnow()
        record = record.model_copy(update={
            "status": "rejected",
            "approval": SkillHandoffApproval(approved=False),
            "updated_at": now,
        })
        self._update_record(record)
        return SkillHandoffResponse(record=record)

    def disable(self, project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        record = self._require_row_record(project_id, request.expected_handoff_id)
        if record.status == "running":
            raise SkillHandoffConflict("运行中的交接不能禁用。")
        now = _utcnow()
        record = record.model_copy(update={
            "status": "disabled",
            "approval": SkillHandoffApproval(approved=False),
            "updated_at": now,
        })
        self._update_record(record)
        return SkillHandoffResponse(record=record)

    async def run(self, project_id: str, request: SkillHandoffDecisionRequest) -> SkillHandoffResponse:
        if self.skill_executor is None:
            raise SkillHandoffUnavailable("Managed Skill executor is unavailable")
        record = self._require_row_record(project_id, request.expected_handoff_id)
        if record.status == "succeeded":
            return SkillHandoffResponse(record=record)
        if record.status == "disabled":
            raise SkillHandoffConflict("作者已禁用该相邻 Skill 交接，不能运行。")
        if record.status != "approved" or not record.approval.approved:
            raise SkillHandoffConflict("必须先由作者批准并确认风险后才能运行相邻 Skill 交接。")
        self._assert_not_stale(record)
        try:
            current_upstream = self._build_upstream(
                project_id,
                source_import_id=record.upstream.source_import_id if record.upstream else None,
            )
        except SkillHandoffConflict as exc:
            failed = self._failed_record(
                record,
                "upstream_invalid",
                "正式知识批次或 receipt 校验失败，需要重新准备并重新批准。",
            )
            self._update_record(failed)
            raise SkillHandoffConflict(failed.error_message or str(exc)) from exc
        if current_upstream is None or record.upstream is None:
            raise SkillHandoffConflict("正式知识批次不可用，不能运行交接。")
        if current_upstream.cards_hash != record.upstream.cards_hash or current_upstream.formalization_receipt_hash != record.upstream.formalization_receipt_hash:
            failed = self._failed_record(record, "upstream_stale", "正式知识批次或 receipt 已变化，需要重新准备并重新批准。")
            self._update_record(failed)
            raise SkillHandoffConflict(failed.error_message or "upstream stale")
        now = _utcnow()
        running = record.model_copy(update={"status": "running", "updated_at": now})
        self._update_record(running)
        try:
            outcome_record = await self._execute_approved(project_id, running)
        except Exception as exc:
            failed = self._failed_record(running, "execution_failed", str(exc)[:4000])
            self._update_record(failed)
            raise
        self._update_record(outcome_record)
        return SkillHandoffResponse(record=outcome_record)

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.store.get_project_binding(project_id)
        if binding is None:
            raise SkillHandoffNotFound("Project binding was not found")
        return binding

    def _require_fresh_binding(self, project_id: str, revision: int, fingerprint: str) -> ProjectBindingRecord:
        binding = self._require_binding(project_id)
        if binding.revision != revision or binding.fingerprint != fingerprint:
            raise SkillHandoffConflict("项目快照已变化，请刷新后重新准备相邻 Skill 交接。")
        return binding

    def _assert_not_stale(self, record: SkillHandoffRunRecord) -> None:
        binding = self._require_binding(record.project.project_id)
        if binding.revision != record.project.revision or binding.fingerprint != record.project.fingerprint:
            raise SkillHandoffConflict("项目 revision/fingerprint 已变化，旧交接不能继续；请重新准备并重新批准。")

    def _active_activation(self, project_id: str) -> SkillHandoffActivation:
        if self.skill_executor is None:
            raise SkillHandoffUnavailable("Managed Skill executor is unavailable")
        try:
            skill = self.skill_executor.active_skill_declaration(project_id, HANDOFF_SKILL_ID)
        except ManagedSkillError as exc:
            raise SkillHandoffConflict("相邻 Skill adapter 尚未导入/启用，不能路由。") from exc
        return SkillHandoffActivation(
            skill_version_id=skill.skill_version_id,
            skill_version=skill.manifest.version,
            manifest_fingerprint=skill.manifest_fingerprint,
            package_fingerprint=skill.package_fingerprint,
            effective_scope="project",
        )

    def _scope_id_for_project(self, project_id: str) -> str | None:
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT id FROM knowledge_scope WHERE project_binding_id = ?",
                (project_id,),
            ).fetchone()
        return None if row is None else str(row["id"])

    def _latest_formalization_payload(self, project_id: str, source_import_id: str | None = None) -> dict[str, Any] | None:
        scope_id = self._scope_id_for_project(project_id)
        if scope_id is None:
            return None
        sql = (
            "SELECT payload_json FROM knowledge_formalization_receipt "
            "WHERE scope_id = ? "
        )
        params: list[object] = [scope_id]
        if source_import_id is not None:
            sql += "AND source_import_id = ? "
            params.append(source_import_id)
        sql += "ORDER BY created_at DESC LIMIT 1"
        with self.store.connection() as connection:
            row = connection.execute(sql, params).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return payload if isinstance(payload, dict) else None

    def _build_upstream(self, project_id: str, *, source_import_id: str | None = None) -> SkillHandoffUpstream | None:
        formalization = self._latest_formalization_payload(project_id, source_import_id)
        if formalization is None:
            return None
        try:
            cards_snapshot = self.knowledge_service.list_cards(project_id=project_id)
        except Exception as exc:
            raise SkillHandoffConflict("项目级知识快照不可用或正式化 receipt 校验失败。") from exc
        if not isinstance(cards_snapshot, KnowledgeScopedCardList):
            raise SkillHandoffConflict("项目级知识快照不可用。")
        receipt_id = str(formalization.get("formalization_receipt_id") or "")
        receipt_hash = str(formalization.get("receipt_hash") or "")
        source_id = str(formalization.get("source_import_id") or "")
        by_type: dict[str, SkillHandoffCardRef] = {}
        for card in cards_snapshot.cards:
            version = card.active_version
            lineage = version.lineage
            formal_ref = version.formalization
            if lineage is None or formal_ref is None:
                continue
            if lineage.source_import_id != source_id:
                continue
            if formal_ref.formalization_receipt_id != receipt_id or formal_ref.formalization_receipt_hash != receipt_hash:
                continue
            payload = version.card
            card_type = str(payload.get("card_type") or "").strip().lower()
            if card_type not in {"work", "voice", "validator"}:
                continue
            route_hints = payload.get("nlcl_route_hints") if isinstance(payload.get("nlcl_route_hints"), dict) else {}
            safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
            by_type[card_type] = SkillHandoffCardRef(
                card_id=card.card_id,
                card_type=card_type,  # type: ignore[arg-type]
                version_id=version.version_id,
                content_hash=version.content_hash,
                title=str(payload.get("title") or card.card_id)[:512],
                summary=str(payload.get("summary") or payload.get("reader_promise") or card.card_id)[:2000],
                route_terms=_compact_list(route_hints.get("route_terms")),
                tags=_compact_list(payload.get("tags")),
                safety_summary=str(route_hints.get("safety_summary") or safety.get("copyright_boundary") or "只交接抽象机制入口，不复制来源正文。")[:2000],
            )
        ordered = [by_type.get("work"), by_type.get("voice"), by_type.get("validator")]
        if any(item is None for item in ordered):
            raise SkillHandoffConflict("正式批次必须同时包含 work / voice / validator 三类 active 卡。")
        card_refs = tuple(item for item in ordered if item is not None)
        cards_hash = canonical_json_hash([item.model_dump(mode="json") for item in card_refs])
        return SkillHandoffUpstream(
            source_import_id=source_id,
            formalization_receipt_id=receipt_id,
            formalization_receipt_hash=receipt_hash,
            knowledge_fingerprint=cards_snapshot.fingerprint,
            cards_hash=cards_hash,
            cards=card_refs,  # type: ignore[arg-type]
        )

    def _workflow_ref(self, project_id: str, source_import_id: str, receipt_hash: str) -> SkillHandoffWorkflowRef:
        plan_id = _digest_id("workflow-plan-", project_id, HANDOFF_KEY, source_import_id, receipt_hash)
        checkpoint_id = _digest_id("workflow-checkpoint-", plan_id, "step-1")
        tool_call_id = "skill_handoff_" + hashlib.sha256(f"{plan_id}:tool".encode()).hexdigest()[:24]
        skill_call_id = "skill_call_" + hashlib.sha256(f"{plan_id}:skill".encode()).hexdigest()[:24]
        plan_hash = canonical_json_hash({
            "schema": "zhimeng.skill-handoff-workflow-plan.v1",
            "workflow_plan_id": plan_id,
            "handoff_key": HANDOFF_KEY,
            "source_import_id": source_import_id,
            "formalization_receipt_hash": receipt_hash,
            "formal_draft_allowed": False,
        })
        checkpoint_hash = canonical_json_hash({
            "schema": "zhimeng.skill-handoff-checkpoint.v1",
            "workflow_plan_id": plan_id,
            "checkpoint_id": checkpoint_id,
            "tool_call_id": tool_call_id,
            "skill_call_id": skill_call_id,
            "plan_hash": plan_hash,
        })
        return SkillHandoffWorkflowRef(
            workflow_plan_id=plan_id,
            workflow_plan_hash=plan_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_hash=checkpoint_hash,
            tool_call_id=tool_call_id,
            skill_call_id=skill_call_id,
        )

    async def _execute_approved(self, project_id: str, record: SkillHandoffRunRecord) -> SkillHandoffRunRecord:
        assert self.skill_executor is not None
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise SkillHandoffConflict("交接记录缺少 upstream/workflow/activation。")
        root = self.handoff_root / record.handoff_id
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
        (snapshot / "formal_knowledge_index.json").write_text(
            canonical_json(record.upstream.model_dump(mode="json")),
            encoding="utf-8",
        )
        request_payload = {
            "schema": REQUEST_SCHEMA,
            "handoff_id": record.handoff_id,
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
            "downstream": record.downstream.model_dump(mode="json"),
            "activation": record.activation.model_dump(mode="json"),
            "approval": record.approval.model_dump(mode="json"),
            "permissions": record.permissions.model_dump(mode="json"),
            "formal_draft_allowed": False,
        }
        outcome = await self.skill_executor.execute(
            project_id=project_id,
            project_revision=record.project.revision,
            project_fingerprint=record.project.fingerprint,
            run_id=record.workflow.workflow_plan_id,
            tool_call_id=record.workflow.tool_call_id,
            skill_id=HANDOFF_SKILL_ID,
            input_payload=request_payload,
            project_snapshot=snapshot,
            run_staging=staging,
        )
        now = _utcnow()
        result_hash = canonical_json_hash(outcome.result) if outcome.result is not None else None
        artifact_hash = None
        artifact_path: str | None = None
        artifact = next((item for item in outcome.artifacts if item.artifact_type == "distillation_index_handoff"), None)
        if artifact is not None:
            if artifact.payload is not None:
                artifact_hash = canonical_json_hash(artifact.payload)
            if artifact.relative_path:
                artifact_path = str((staging / artifact.relative_path).resolve())
        if outcome.receipt.execution_status != "succeeded" or outcome.receipt.domain_status == "blocked":
            return record.model_copy(update={
                "status": "failed",
                "skill_receipt_id": outcome.receipt.receipt_id,
                "skill_receipt_hash": outcome.receipt_hash,
                "result_hash": result_hash,
                "artifact_hash": artifact_hash,
                "output_artifact_path": artifact_path,
                "result": outcome.result if isinstance(outcome.result, dict) else None,
                "error_code": outcome.receipt.error.code if outcome.receipt.error else "domain_blocked",
                "error_message": outcome.receipt.error.message if outcome.receipt.error else "Skill returned a blocked domain result",
                "updated_at": now,
            })
        if not self._result_is_valid_downstream_ready(outcome.result):
            return record.model_copy(update={
                "status": "failed",
                "skill_receipt_id": outcome.receipt.receipt_id,
                "skill_receipt_hash": outcome.receipt_hash,
                "result_hash": result_hash,
                "artifact_hash": artifact_hash,
                "output_artifact_path": artifact_path,
                "result": outcome.result if isinstance(outcome.result, dict) else None,
                "error_code": "invalid_handoff_result",
                "error_message": (
                    "受管 adapter 没有返回 work/voice/validator 三类下游入口，"
                    "本次交接按失败关闭。"
                ),
                "updated_at": now,
            })
        return record.model_copy(update={
            "status": "succeeded",
            "skill_receipt_id": outcome.receipt.receipt_id,
            "skill_receipt_hash": outcome.receipt_hash,
            "result_hash": result_hash,
            "artifact_hash": artifact_hash,
            "output_artifact_path": artifact_path,
            "result": outcome.result if isinstance(outcome.result, dict) else None,
            "error_code": None,
            "error_message": None,
            "updated_at": now,
        })

    def _result_is_valid_downstream_ready(self, result: object) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("status") != "ready_for_downstream":
            return False
        entries = result.get("entries")
        if not isinstance(entries, list) or len(entries) != 3:
            return False
        return [
            item.get("card_type")
            for item in entries
            if isinstance(item, dict)
        ] == ["work", "voice", "validator"]

    def _awaiting_upstream_record(self, binding: ProjectBindingRecord) -> SkillHandoffRunRecord:
        now = _utcnow()
        return SkillHandoffRunRecord(
            handoff_id=_digest_id("skill-handoff-", binding.project_id, HANDOFF_KEY, "awaiting"),
            handoff_key=HANDOFF_KEY,
            status="awaiting_upstream",
            project=SkillHandoffProjectRef(project_id=binding.project_id, revision=binding.revision, fingerprint=binding.fingerprint),
            workflow=None,
            upstream=None,
            downstream=SkillHandoffDownstream(),
            activation=None,
            approval=SkillHandoffApproval(approved=False),
            permissions=SkillHandoffPermissions(read=("bound_project_snapshot",), write=("run_staging",), network=False, secrets=()),
            formal_draft_allowed=False,
            error_code="upstream_missing",
            error_message="先完成 novel-distillation 的 work/voice/validator 正式化，才可交接给 novel-kb-manager。",
            created_at=now,
            updated_at=now,
        )

    def _failed_record(self, record: SkillHandoffRunRecord, code: str, message: str) -> SkillHandoffRunRecord:
        return record.model_copy(update={
            "status": "failed",
            "error_code": code,
            "error_message": message[:4096],
            "updated_at": _utcnow(),
        })

    def _row_payload(self, record: SkillHandoffRunRecord) -> str:
        return canonical_json(record.model_dump(mode="json", by_alias=True))

    def _upsert_record(self, record: SkillHandoffRunRecord) -> None:
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise ValueError("persisted handoff record must be prepared")
        payload = self._row_payload(record)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO skill_handoff_run(
                    id, handoff_key, project_binding_id, status, project_revision,
                    project_fingerprint, source_import_id, formalization_receipt_id,
                    formalization_receipt_hash, knowledge_fingerprint, upstream_cards_hash,
                    workflow_plan_id, workflow_plan_hash, checkpoint_id, checkpoint_hash,
                    skill_version_id, skill_receipt_id, skill_receipt_hash, result_hash,
                    artifact_hash, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    project_revision=excluded.project_revision,
                    project_fingerprint=excluded.project_fingerprint,
                    knowledge_fingerprint=excluded.knowledge_fingerprint,
                    upstream_cards_hash=excluded.upstream_cards_hash,
                    workflow_plan_hash=excluded.workflow_plan_hash,
                    checkpoint_hash=excluded.checkpoint_hash,
                    skill_version_id=excluded.skill_version_id,
                    skill_receipt_id=excluded.skill_receipt_id,
                    skill_receipt_hash=excluded.skill_receipt_hash,
                    result_hash=excluded.result_hash,
                    artifact_hash=excluded.artifact_hash,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                self._row_values(record, payload),
            )
            connection.commit()

    def _update_record(self, record: SkillHandoffRunRecord) -> None:
        if record.upstream is None or record.workflow is None or record.activation is None:
            raise ValueError("persisted handoff record must be prepared")
        payload = self._row_payload(record)
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE skill_handoff_run
                SET status = ?, skill_receipt_id = ?, skill_receipt_hash = ?,
                    result_hash = ?, artifact_hash = ?, payload_json = ?, updated_at = ?
                WHERE id = ? AND project_binding_id = ?
                """,
                (
                    record.status,
                    record.skill_receipt_id,
                    record.skill_receipt_hash,
                    record.result_hash,
                    record.artifact_hash,
                    payload,
                    record.updated_at.isoformat(),
                    record.handoff_id,
                    record.project.project_id,
                ),
            )
            connection.commit()

    def _row_values(self, record: SkillHandoffRunRecord, payload: str) -> tuple[object, ...]:
        assert record.upstream is not None and record.workflow is not None and record.activation is not None
        return (
            record.handoff_id,
            record.handoff_key,
            record.project.project_id,
            record.status,
            record.project.revision,
            record.project.fingerprint,
            record.upstream.source_import_id,
            record.upstream.formalization_receipt_id,
            record.upstream.formalization_receipt_hash,
            record.upstream.knowledge_fingerprint,
            record.upstream.cards_hash,
            record.workflow.workflow_plan_id,
            record.workflow.workflow_plan_hash,
            record.workflow.checkpoint_id,
            record.workflow.checkpoint_hash,
            record.activation.skill_version_id,
            record.skill_receipt_id,
            record.skill_receipt_hash,
            record.result_hash,
            record.artifact_hash,
            payload,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        )

    def _latest_row(self, project_id: str):
        with self.store.connection() as connection:
            return connection.execute(
                """
                SELECT * FROM skill_handoff_run
                WHERE project_binding_id = ? AND handoff_key = ?
                ORDER BY updated_at DESC, created_at DESC LIMIT 1
                """,
                (project_id, HANDOFF_KEY),
            ).fetchone()

    def _row_by_id(self, project_id: str, handoff_id: str):
        with self.store.connection() as connection:
            return connection.execute(
                "SELECT * FROM skill_handoff_run WHERE project_binding_id = ? AND id = ?",
                (project_id, handoff_id),
            ).fetchone()

    def _record_from_row(self, row) -> SkillHandoffRunRecord:
        return SkillHandoffRunRecord.model_validate_json(str(row["payload_json"]))

    def _require_row_record(self, project_id: str, handoff_id: str) -> SkillHandoffRunRecord:
        row = self._row_by_id(project_id, handoff_id)
        if row is None:
            raise SkillHandoffNotFound("Skill handoff was not found")
        return self._record_from_row(row)


def raise_skill_handoff_error(exc: SkillHandoffError) -> None:
    raise ApiError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
        details=exc.details,
        retryable=exc.retryable,
    ) from exc


__all__ = [
    "SkillHandoffService",
    "SkillHandoffError",
    "raise_skill_handoff_error",
]
