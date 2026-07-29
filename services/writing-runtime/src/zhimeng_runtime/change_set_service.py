from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
import re
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, NoReturn, TYPE_CHECKING
from uuid import uuid4

from zhimeng_runtime.change_set_models import (
    CandidateFileChange,
    ChangeHunk,
    ChangeOperation,
    ChangeSetApplyRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetListResponse,
    ChangeSetOrigin,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
    ChangeSetStatus,
    ChangeSetSummary,
    HunkDecision,
    NlclCallUsageContinuityReceipt,
    NlclCallUsageContinuityRequest,
    NlclCallUsageContinuityResponse,
    NlclOriginalCallUsageExportReceipt,
    NlclOriginalCallUsageExportRequest,
    NlclOriginalCallUsageExportResponse,
    NlclOriginalChapterOpsUsageExportReceipt,
    NlclOriginalChapterOpsUsageExportRequest,
    NlclOriginalChapterOpsUsageExportResponse,
    NlclOriginalNovelStateBridgeReceipt,
    NlclOriginalNovelStateBridgeRequest,
    NlclOriginalNovelStateBridgeResponse,
    NlclOriginalNovelStateSyncReceipt,
    NlclOriginalNovelStateSyncRequest,
    NlclOriginalNovelStateSyncResponse,
    NlclOriginalPrewriteContextReportReceipt,
    NlclOriginalPrewriteContextReportRequest,
    NlclOriginalPrewriteContextReportResponse,
    NlclOriginalPrewriteContextReportWrittenFile,
    NlclOriginalTruthContentExtractReceipt,
    NlclOriginalTruthContentExtractRequest,
    NlclOriginalTruthContentExtractResponse,
    NlclOriginalTruthContentExtractSourceFile,
    NlclOriginalTruthContentExtractWrittenFile,
    NlclOriginalMandatoryUsagePlanItem,
    NlclOriginalMandatoryUsagePlanReceipt,
    NlclOriginalMandatoryUsagePlanRequest,
    NlclOriginalMandatoryUsagePlanResponse,
    NlclOriginalMandatoryUsagePlanWrittenFile,
    NlclOriginalMandatoryUsageClosureDecision,
    NlclOriginalMandatoryUsageClosureItem,
    NlclOriginalMandatoryUsageClosureReceipt,
    NlclOriginalMandatoryUsageClosureRequest,
    NlclOriginalMandatoryUsageClosureResponse,
    NlclOriginalMandatoryUsageClosureWrittenFile,
    NlclOriginalOperationPackageLikeReceipt,
    NlclOriginalOperationPackageLikeRequest,
    NlclOriginalOperationPackageLikeResponse,
    NlclOriginalOperationPackageLikeWrittenFile,
    NlclOriginalLibraryLayoutInventoryReceipt,
    NlclOriginalLibraryLayoutInventoryRequest,
    NlclOriginalLibraryLayoutInventoryResponse,
    NlclOriginalLibraryLayoutSlot,
    NlclOriginalLibraryLayoutInventoryWrittenFile,
    NlclOriginalLibraryLayoutCompletenessReceipt,
    NlclOriginalLibraryLayoutCompletenessRequest,
    NlclOriginalLibraryLayoutCompletenessResponse,
    NlclOriginalLibraryLayoutCompletenessWrittenFile,
    NlclOriginalBoundedLibraryActivationReceipt,
    NlclOriginalBoundedLibraryActivationRequest,
    NlclOriginalBoundedLibraryActivationResponse,
    NlclOriginalBoundedLibraryActivationWrittenFile,
    NlclOriginalBoundedLibraryActivationSource,
    NlclOriginalNovelStateWrittenFile,
    NlclNovelStateFileRef,
    FeedbackCandidate,
    FeedbackCandidateBundle,
    FeedbackCandidatePromotionDecision,
    FeedbackCandidatePromotionReceipt,
    FeedbackCandidatePromotionRequest,
    FeedbackCandidatePromotionResponse,
    FeedbackCandidateResponse,
    ReaderFinding,
    ReaderFindingDecision,
    ReaderReviewDecisionReceipt,
    ReaderReviewDecisionRequest,
    ReaderReviewDecisionResponse,
    ReaderReviewReceipt,
    ReaderReviewRequest,
    ReaderReviewResponse,
    CreateFileChangeInput,
    UpdateFileChangeInput,
    NlclWritebackSyncReceipt,
    NlclWritebackSyncRequest,
    NlclWritebackSyncResponse,
    WritebackEvidenceReceipt,
    WritebackEvidenceResponse,
    WritebackFileResult,
    WritebackManifest,
    change_set_persisted_payload,
)
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.formal_draft_models import (
    FormalDraftAuthorizationReceipt,
    FormalDraftDecision,
    FormalDraftEvaluateRequest,
)
from zhimeng_runtime.formal_knowledge_promotion_models import (
    FormalKnowledgePromotionAction,
    FormalKnowledgePromotionActionRequest,
    FormalKnowledgePromotionCheck,
    FormalKnowledgePromotionCheckCode,
    FormalKnowledgePromotionDecision,
    FormalKnowledgePromotionEvaluateRequest,
    FormalKnowledgePromotionItem,
    FormalKnowledgePromotionPackage,
    FormalKnowledgePromotionReceipt,
    FormalKnowledgePromotionResponse,
    FormalKnowledgePromotionStatus,
    formal_knowledge_promotion_package_hash,
    formal_knowledge_promotion_receipt_hash,
)
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeLineageStatus,
    KnowledgeReviewIssue,
    KnowledgeVersionFormalizationStatus,
)
from zhimeng_runtime.knowledge_store import KnowledgeStore, KnowledgeStoreError
from zhimeng_runtime.source_import_models import (
    FeedbackExperienceCandidate,
    FeedbackSourceImportJob,
    SourceImportProgress,
    SourceImportStatus,
)
from zhimeng_runtime.context_models import canonical_json_hash as knowledge_card_hash
from zhimeng_runtime.eval_corpus_export_models import (
    EvalCorpusArtifactRef,
    EvalCorpusExportReceipt,
    EvalCorpusExportRequest,
    EvalCorpusExportResponse,
    eval_corpus_export_receipt_hash,
)
from zhimeng_runtime.eval_corpus_regression_models import (
    EvalCorpusBaselineFreezeRequest,
    EvalCorpusBaselineReceipt,
    EvalCorpusBaselineResponse,
    EvalCorpusRegressionCheck,
    EvalCorpusRegressionCheckCode,
    EvalCorpusRegressionEvaluateRequest,
    EvalCorpusRegressionReceipt,
    EvalCorpusRegressionResponse,
    EvalCorpusRegressionStatus,
    eval_corpus_baseline_receipt_hash,
    eval_corpus_regression_receipt_hash,
)
from zhimeng_runtime.gate_pipeline_models import (
    GatePipelineEvaluateRequest,
    GatePipelineReceipt,
    GatePipelineResponse,
    GatePipelineStatus,
    GatePipelineStep,
    GatePipelineStepCode,
    gate_pipeline_receipt_hash,
)
from zhimeng_runtime.formal_draft_service import (
    FormalDraftAuthorizationError,
    FormalDraftAuthorizationService,
)
from zhimeng_runtime.hard_fact_models import (
    HardFactEvaluateRequest,
    HardFactGateReceipt,
    HardFactGateResponse,
    HardFactSignal,
    HardFactSignalCode,
    HardFactStatus,
    hard_fact_gate_receipt_hash,
)
from zhimeng_runtime.literary_quality_models import (
    LiteraryQualityDecision,
    LiteraryQualityDimension,
    LiteraryQualityDimensionCode,
    LiteraryQualityDimensionStatus,
    LiteraryQualityEvaluateRequest,
    LiteraryQualityFinding,
    LiteraryQualityGateReceipt,
    LiteraryQualityGateResponse,
    LiteraryRubricScore,
    literary_quality_gate_receipt_hash,
)
from zhimeng_runtime.external_blind_review_matrix import (
    evaluate_external_blind_review_matrix,
)
from zhimeng_runtime.llm_literary_score_models import (
    LlmLiteraryScoreAxis,
    LlmLiteraryScoreEvaluateRequest,
    LlmLiteraryScoreReceipt,
    LlmLiteraryScoreResponse,
    LlmLiteraryScorerMode,
    llm_literary_score_receipt_hash,
)
from zhimeng_runtime.managed_skill_service import ManagedSkillPackageDriftError
from zhimeng_runtime.project_models import ProjectFile, ProjectInspectionResponse
from zhimeng_runtime.project_reader import (
    MAX_TEXT_FILE_BYTES,
    ProjectReader,
    classify_project_file_kind,
)
from zhimeng_runtime.post_draft_gate_models import (
    KnowledgeValidatorBundle,
    PostDraftGateReceipt,
    PostDraftGateRequest,
    PostDraftGateResponse,
    PostDraftGateResult,
    canonical_json_hash,
    post_draft_gate_receipt_hash,
)

if TYPE_CHECKING:
    from zhimeng_runtime.skill_executor import SkillExecutor, SkillExecutionOutcome


UTF8_BOM = b"\xef\xbb\xbf"
REPARSE_POINT_ATTRIBUTE = 0x400
TEXT_EXTENSIONS = frozenset({".json", ".md", ".txt", ".yaml", ".yml"})
MAX_WRITEBACK_JOURNAL_BYTES = 2 * 1024 * 1024
KNOWLEDGE_VALIDATOR_BUNDLE_CAPABILITY = "knowledge_validator_bundle.v1"


class ChangeSetError(RuntimeError):
    pass


class ChangeSetNotFoundError(ChangeSetError):
    pass


class ChangeSetConflictError(ChangeSetError):
    pass


class ChangeSetStateError(ChangeSetError):
    pass


class ChangeSetInputError(ChangeSetError):
    pass


class ChangeSetWritebackError(ChangeSetError):
    pass


class ChangeSetGateError(ChangeSetError):
    code = "post_draft_gate_invalid"


class ChangeSetGateUnavailableError(ChangeSetGateError):
    code = "post_draft_gate_unavailable"


class ChangeSetGateUpgradeRequiredError(ChangeSetGateError):
    code = "post_draft_gate_upgrade_required"


class ChangeSetGateMissingError(ChangeSetGateError):
    code = "post_draft_gate_missing"


class ChangeSetGateStaleError(ChangeSetGateError):
    code = "post_draft_gate_stale"


class ChangeSetGateBlockedError(ChangeSetGateError):
    code = "post_draft_gate_blocked"


class ChangeSetGatePipelineBlockedError(ChangeSetGateError):
    code = "gate_pipeline_blocked"


@dataclass(slots=True)
class _PreparedWrite:
    change: CandidateFileChange
    root: Path
    target: Path
    before_bytes: bytes | None
    after_bytes: bytes | None
    backup: Path | None
    temporary: Path | None
    tombstone: Path | None
    accepted_hunks: list[str]
    rejected_hunks: list[str]
    applied: bool = False


@dataclass(slots=True)
class _RecoveryWrite:
    relative_path: str
    target: Path
    desired_bytes: bytes | None
    observed_sha256: str | None
    cleanup_paths: tuple[tuple[Path, str | None], ...]


@dataclass(frozen=True, slots=True)
class _CandidateFile:
    change_id: str
    operation: ChangeOperation
    relative_path: str
    before_sha256: str | None
    after_bytes: bytes | None
    post_draft_gate_scope: Literal["none", "prose_body"]
    accepted_hunk_ids: tuple[str, ...]
    rejected_hunk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ManagedValidatorIdentity:
    skill_id: str
    version: str
    manifest_fingerprint: str
    package_fingerprint: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _GateBinding:
    context_pack_id: str | None
    context_pack_hash: str | None
    knowledge_fingerprint: str | None
    route_receipt_id: str | None
    route_receipt_hash: str | None
    knowledge_validator_bundle: KnowledgeValidatorBundle | None
    managed_validator: _ManagedValidatorIdentity

    @property
    def knowledge_validator_bundle_hash(self) -> str | None:
        bundle = self.knowledge_validator_bundle
        return None if bundle is None else bundle.bundle_hash

    @property
    def knowledge_validator_card_ids(self) -> tuple[str, ...]:
        bundle = self.knowledge_validator_bundle
        return () if bundle is None else tuple(item.card_id for item in bundle.validators)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _text_sha256(content: str) -> str:
    return _sha256(content.encode("utf-8"))


def _now() -> datetime:
    return datetime.now(UTC)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _encode_text(content: str, *, preserve_utf8_bom: bool) -> bytes:
    encoded = content.encode("utf-8")
    return UTF8_BOM + encoded if preserve_utf8_bom else encoded


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ChangeSetService:
    def __init__(
        self,
        reader: ProjectReader,
        store: SqliteStore,
        state_root: Path,
        *,
        post_draft_gate_executor: "SkillExecutor | None" = None,
        knowledge_fingerprint_probe: Callable[[str], str] | None = None,
        knowledge_validator_resolver: Callable[
            [str, str, str, str | None],
            tuple[str, str, KnowledgeValidatorBundle | None],
        ]
        | None = None,
        formal_draft_authorization_service: FormalDraftAuthorizationService | None = None,
    ) -> None:
        self.reader = reader
        self.store = store
        self.state_root = state_root.resolve(strict=False)
        self.post_draft_gate_executor = post_draft_gate_executor
        self.knowledge_fingerprint_probe = knowledge_fingerprint_probe
        self.knowledge_validator_resolver = knowledge_validator_resolver
        self.formal_draft_authorization_service = (
            formal_draft_authorization_service
            or FormalDraftAuthorizationService(store)
        )

    def set_post_draft_gate_executor(
        self,
        executor: "SkillExecutor | None",
    ) -> None:
        self.post_draft_gate_executor = executor

    def get(self, change_set_id: str) -> ChangeSetResponse:
        payload = self.store.get_change_set(change_set_id)
        if payload is None:
            raise ChangeSetNotFoundError("Change set was not found")
        record = ChangeSetResponse.model_validate(payload)
        try:
            receipt_payload = self.store.get_latest_post_draft_gate_receipt(change_set_id)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ChangeSetGateStaleError(
                "stored post-draft Gate receipt failed integrity validation: "
                + str(exc)
            ) from exc
        if receipt_payload is None:
            return record
        receipt = self._validate_stored_gate_receipt(receipt_payload)
        if (
            receipt.review_revision != record.review_revision
            or receipt.base_revision != record.base_revision
            or receipt.base_fingerprint != record.base_fingerprint
        ):
            return record
        if record.status is ChangeSetStatus.READY:
            if not self._has_accepted_post_draft_body(record):
                return record
            try:
                binding = self._current_gate_binding(record)
                self._require_current_gate_binding(receipt, binding)
            except ChangeSetGateError:
                # Historical evidence remains in the receipt table, while the
                # public current-state view must fail closed.
                return record
            except Exception:
                # A transient Context/knowledge observation failure cannot make
                # an old passing receipt current.
                return record
        return record.model_copy(update={"post_draft_gate": receipt})

    def list_for_project(self, project_id: str) -> ChangeSetListResponse:
        if self.store.get_project_binding(project_id) is None:
            raise ChangeSetNotFoundError("Project binding was not found")
        summaries: list[ChangeSetSummary] = []
        for payload in self.store.list_change_sets(project_id):
            record = ChangeSetResponse.model_validate(payload)
            decisions = [
                hunk.decision
                for change in record.changes
                for hunk in change.hunks
            ]
            summaries.append(
                ChangeSetSummary(
                    change_set_id=record.change_set_id,
                    project_id=record.project_id,
                    title=record.title,
                    origin=record.origin,
                    source_run_id=record.source_run_id,
                    source_tool_call_id=record.source_tool_call_id,
                    gate_required=record.gate_required,
                    status=record.status,
                    review_revision=record.review_revision,
                    file_count=len(record.changes),
                    hunk_count=len(decisions),
                    pending_hunk_count=decisions.count(HunkDecision.PENDING),
                    accepted_hunk_count=decisions.count(HunkDecision.ACCEPTED),
                    rejected_hunk_count=decisions.count(HunkDecision.REJECTED),
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
        return ChangeSetListResponse(change_sets=summaries)

    def recover_interrupted(self) -> None:
        failures: list[str] = []
        payloads = self.store.list_change_sets_with_status(
            (ChangeSetStatus.APPLYING.value, ChangeSetStatus.ROLLING_BACK.value)
        )
        for payload in payloads:
            change_set_id = str(payload.get("change_set_id", "unknown"))
            try:
                record = ChangeSetResponse.model_validate(payload)
                transaction = (
                    "apply"
                    if record.status is ChangeSetStatus.APPLYING
                    else "rollback"
                )
                self._recover_interrupted_transaction(record, transaction=transaction)
            except Exception as exc:
                failures.append(f"{change_set_id}: {exc}")
        if failures:
            raise ChangeSetWritebackError(
                "interrupted change set recovery failed: " + "; ".join(failures)
            )

    def _recover_interrupted_transaction(
        self,
        record: ChangeSetResponse,
        *,
        transaction: str,
    ) -> None:
        artifact_root = self._artifact_root(record.change_set_id)
        journal_path = artifact_root / "writeback-journal.json"
        journal = self._read_recovery_journal(journal_path, record, transaction)
        binding = self._require_binding(record.project_id)
        root = self.reader.resolve(binding.root_path)
        writes = self._preflight_recovery(
            record,
            transaction=transaction,
            journal=journal,
            artifact_root=artifact_root,
            root=root,
        )

        if transaction == "rollback" and record.writeback is not None:
            self._restore_recorded_directories(
                root,
                record.writeback.created_directories,
            )

        for item in writes:
            self._restore_recovery_write(item, root)
        for item in writes:
            for cleanup_path, expected_sha256 in item.cleanup_paths:
                if not cleanup_path.exists():
                    continue
                actual = self._read_recovery_file(cleanup_path, root)
                if actual is None or _sha256(actual) != expected_sha256:
                    raise ChangeSetWritebackError(
                        f"recovery artifact changed: {item.relative_path}"
                    )
                cleanup_path.unlink()

        created_directories = journal.get("created_directories", [])
        if transaction == "apply":
            if not isinstance(created_directories, list) or not all(
                isinstance(item, str) for item in created_directories
            ):
                raise ChangeSetWritebackError(
                    "writeback journal has invalid created_directories"
                )
            self._remove_recorded_directories(root, created_directories)

        self._snapshot(binding)
        message = (
            "interrupted apply was restored to the pre-write project state"
            if transaction == "apply"
            else "interrupted rollback was restored to the applied project state"
        )
        if transaction == "apply":
            (artifact_root / "writeback-manifest.json").unlink(missing_ok=True)
            recovered = record.model_copy(
                update={
                    "status": ChangeSetStatus.FAILED,
                    "writeback": None,
                    "rollback": None,
                    "error": message,
                    "updated_at": _now(),
                }
            )
            journal_status = "recovered_to_base"
        else:
            (artifact_root / "rollback-manifest.json").unlink(missing_ok=True)
            recovered = record.model_copy(
                update={
                    "status": ChangeSetStatus.APPLIED,
                    "rollback": None,
                    "error": message,
                    "updated_at": _now(),
                }
            )
            journal_status = "recovered_to_applied"
        recovered = ChangeSetResponse.model_validate(recovered.model_dump())
        self.store.save_change_set(self._payload(recovered))
        journal["status"] = journal_status
        journal["recovered_at"] = _now().isoformat()
        _atomic_write_json(journal_path, journal)

    def _read_recovery_journal(
        self,
        path: Path,
        record: ChangeSetResponse,
        transaction: str,
    ) -> dict[str, Any]:
        try:
            if _is_link_or_reparse(path):
                raise ChangeSetWritebackError("writeback journal is a link")
            size = path.stat().st_size
            if size > MAX_WRITEBACK_JOURNAL_BYTES:
                raise ChangeSetWritebackError("writeback journal exceeds its size limit")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ChangeSetWritebackError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChangeSetWritebackError(
                "interrupted change set has no readable writeback journal"
            ) from exc
        if not isinstance(payload, dict):
            raise ChangeSetWritebackError("writeback journal must be an object")
        expected = {
            "schema": "zhimeng.writeback-journal.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "base_revision": record.base_revision,
            "base_fingerprint": record.base_fingerprint,
            "transaction": transaction,
        }
        for key, value in expected.items():
            if payload.get(key) != value:
                raise ChangeSetWritebackError(
                    f"writeback journal {key} does not match the interrupted transaction"
                )
        if payload.get("status") not in {
            "prepared",
            "files_applied",
            "recovery_required",
        }:
            raise ChangeSetWritebackError(
                "writeback journal is not at a recoverable checkpoint"
            )
        return payload

    def _preflight_recovery(
        self,
        record: ChangeSetResponse,
        *,
        transaction: str,
        journal: dict[str, Any],
        artifact_root: Path,
        root: Path,
    ) -> list[_RecoveryWrite]:
        raw_files = journal.get("files")
        if not isinstance(raw_files, list) or not raw_files:
            raise ChangeSetWritebackError("writeback journal has no file records")
        if not all(isinstance(item, dict) for item in raw_files):
            raise ChangeSetWritebackError("writeback journal file records are invalid")

        changes = {item.change_id: item for item in record.changes}
        writeback_results = (
            {}
            if record.writeback is None
            else {item.change_id: item for item in record.writeback.files}
        )
        expected_change_ids = (
            {
                change.change_id
                for change in record.changes
                if any(
                    hunk.decision is HunkDecision.ACCEPTED
                    for hunk in change.hunks
                )
            }
            if transaction == "apply"
            else set(writeback_results)
        )
        seen: set[str] = set()
        writes: list[_RecoveryWrite] = []
        for raw in raw_files:
            change_id = raw.get("change_id")
            relative_path = raw.get("relative_path")
            before_sha256 = raw.get("before_sha256")
            after_sha256 = raw.get("after_sha256")
            if not isinstance(change_id, str) or change_id in seen:
                raise ChangeSetWritebackError(
                    "writeback journal change IDs must be unique strings"
                )
            seen.add(change_id)
            change = changes.get(change_id)
            if change is None or relative_path != change.relative_path:
                raise ChangeSetWritebackError(
                    "writeback journal file identity does not match the change set"
                )
            if raw.get("operation") != change.operation.value:
                raise ChangeSetWritebackError(
                    "writeback journal operation does not match the change set"
                )
            if before_sha256 is not None and (
                not isinstance(before_sha256, str)
                or len(before_sha256) != 64
            ):
                raise ChangeSetWritebackError("writeback journal has an invalid before hash")
            if after_sha256 is not None and (
                not isinstance(after_sha256, str)
                or len(after_sha256) != 64
            ):
                raise ChangeSetWritebackError("writeback journal has an invalid after hash")

            if transaction == "apply":
                expected_before = change.before_sha256
                if before_sha256 != expected_before:
                    raise ChangeSetWritebackError(
                        "apply recovery hash does not match the proposed base"
                    )
                if change.operation is ChangeOperation.CREATE:
                    expected_after = change.proposed_after_sha256
                elif change.operation is ChangeOperation.DELETE:
                    expected_after = None
                else:
                    backup = artifact_root / "backups" / Path(change.relative_path)
                    base_bytes = self._read_verified_backup(
                        backup,
                        expected_before,
                        artifact_root,
                    )
                    try:
                        base_text = base_bytes.decode("utf-8-sig")
                    except UnicodeDecodeError as exc:
                        raise ChangeSetWritebackError(
                            f"recovery backup is not UTF-8: {change.relative_path}"
                        ) from exc
                    expected_after = _sha256(
                        _encode_text(
                            self._apply_hunk_decisions(base_text, change),
                            preserve_utf8_bom=change.preserve_utf8_bom,
                        )
                    )
            else:
                result = writeback_results.get(change_id)
                if result is None:
                    raise ChangeSetWritebackError(
                        "rollback journal references an unknown writeback result"
                    )
                expected_before = result.after_sha256
                expected_after = result.before_sha256
            if after_sha256 != expected_after:
                raise ChangeSetWritebackError(
                    "writeback journal after hash does not match the transaction"
                )

            target = self._recovery_project_path(root, relative_path)
            current_bytes = self._read_recovery_file(target, root)
            current_sha256 = None if current_bytes is None else _sha256(current_bytes)
            if current_sha256 not in {before_sha256, after_sha256}:
                raise ChangeSetConflictError(
                    f"project file changed after interruption: {relative_path}"
                )

            if before_sha256 is None:
                desired_bytes = None
            else:
                backup_root = (
                    artifact_root / "backups"
                    if transaction == "apply"
                    else artifact_root / "rollback-current"
                )
                desired_bytes = self._read_verified_backup(
                    backup_root / Path(relative_path),
                    before_sha256,
                    artifact_root,
                )

            cleanup: list[tuple[Path, str | None]] = []
            for key, expected_hash in (
                ("temporary_path", after_sha256),
                ("tombstone_path", before_sha256),
            ):
                raw_path = raw.get(key)
                if raw_path is None:
                    continue
                if not isinstance(raw_path, str) or expected_hash is None:
                    raise ChangeSetWritebackError(
                        f"writeback journal has an invalid {key}"
                    )
                cleanup_path = self._recovery_project_path(root, raw_path)
                if cleanup_path == target or cleanup_path.parent != target.parent:
                    raise ChangeSetWritebackError(
                        f"writeback journal {key} is not a target sibling"
                    )
                cleanup.append((cleanup_path, expected_hash))

            writes.append(
                _RecoveryWrite(
                    relative_path=relative_path,
                    target=target,
                    desired_bytes=desired_bytes,
                    observed_sha256=current_sha256,
                    cleanup_paths=tuple(cleanup),
                )
            )
        if seen != expected_change_ids:
            raise ChangeSetWritebackError(
                "writeback journal file set does not match the interrupted transaction"
            )
        return writes

    def _read_verified_backup(
        self,
        path: Path,
        expected_sha256: str | None,
        artifact_root: Path,
    ) -> bytes:
        if expected_sha256 is None or not _is_within(
            path.resolve(strict=False), artifact_root
        ):
            raise ChangeSetWritebackError("recovery backup path is invalid")
        try:
            if _is_link_or_reparse(path) or not path.is_file():
                raise ChangeSetWritebackError("recovery backup is not a regular file")
            if int(getattr(path.stat(), "st_nlink", 1)) > 1:
                raise ChangeSetWritebackError("recovery backup is hardlinked")
            content = path.read_bytes()
        except ChangeSetWritebackError:
            raise
        except OSError as exc:
            raise ChangeSetWritebackError("recovery backup cannot be read") from exc
        if len(content) > MAX_TEXT_FILE_BYTES or _sha256(content) != expected_sha256:
            raise ChangeSetWritebackError("recovery backup hash mismatch")
        return content

    @staticmethod
    def _recovery_project_path(root: Path, raw: str) -> Path:
        relative = PurePosixPath(raw)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ChangeSetWritebackError("writeback journal contains an unsafe path")
        path = root / Path(relative.as_posix())
        if not _is_within(path.resolve(strict=False), root):
            raise ChangeSetWritebackError("writeback journal path escaped the project")
        return path

    @staticmethod
    def _read_recovery_file(path: Path, root: Path) -> bytes | None:
        if not os.path.lexists(path):
            return None
        try:
            ChangeSetService._validate_existing_parent_chain(root, path.parent)
            metadata = path.lstat()
            if _is_link_or_reparse(path) or not path.is_file():
                raise ChangeSetConflictError("recovery target is not a regular file")
            if int(getattr(metadata, "st_nlink", 1)) > 1:
                raise ChangeSetConflictError("recovery target is hardlinked")
            if metadata.st_size > MAX_TEXT_FILE_BYTES:
                raise ChangeSetConflictError("recovery target exceeds its size limit")
            content = path.read_bytes()
        except (ChangeSetConflictError, ChangeSetWritebackError):
            raise
        except OSError as exc:
            raise ChangeSetConflictError("recovery target cannot be read") from exc
        return content

    @staticmethod
    def _restore_recovery_write(item: _RecoveryWrite, root: Path) -> None:
        current = ChangeSetService._read_recovery_file(item.target, root)
        current_sha256 = None if current is None else _sha256(current)
        if current_sha256 != item.observed_sha256:
            raise ChangeSetConflictError(
                f"project file changed during recovery: {item.relative_path}"
            )
        if item.desired_bytes is None:
            if os.path.lexists(item.target):
                item.target.unlink()
            return
        if current is not None and _sha256(current) == _sha256(item.desired_bytes):
            return
        temporary = ChangeSetService._write_temporary_sibling(
            item.target,
            item.desired_bytes,
        )
        os.replace(temporary, item.target)

    def create(
        self,
        project_id: str,
        request: ChangeSetCreateRequest,
    ) -> ChangeSetResponse:
        if request.origin is ChangeSetOrigin.AGENT:
            raise ChangeSetInputError(
                "agent-origin change sets can only be created by the Runtime tool executor"
            )
        return self._create(
            project_id,
            request,
            change_set_id=f"changeset-{uuid4().hex}",
            provenance=None,
        )

    def create_agent_candidate(
        self,
        project_id: str,
        request: ChangeSetCreateRequest,
    ) -> ChangeSetResponse:
        if request.origin is not ChangeSetOrigin.AGENT:
            raise ChangeSetInputError("agent candidate requires origin=agent")
        if (
            request.source_run_id is None
            or request.source_tool_call_id is None
            or request.source_request_hash is None
        ):
            raise ChangeSetInputError("agent candidate is missing Runtime provenance")

        change_set_id = self._agent_change_set_id(
            request.source_run_id,
            request.source_tool_call_id,
        )
        existing_payload = self.store.get_change_set(change_set_id)
        existing_by_tool = self.store.get_change_set_by_tool_call(
            request.source_tool_call_id
        )
        if existing_payload is not None or existing_by_tool is not None:
            if existing_payload is None or existing_by_tool is None:
                raise ChangeSetConflictError(
                    "agent change proposal is missing its atomic provenance record"
                )
            tool_payload, stored_provenance = existing_by_tool
            if tool_payload.get("change_set_id") != change_set_id:
                raise ChangeSetConflictError(
                    "ToolCall is already bound to another change proposal"
                )
            existing = ChangeSetResponse.model_validate(existing_payload)
            if (
                existing.project_id != project_id
                or existing.source_run_id != request.source_run_id
                or existing.source_tool_call_id != request.source_tool_call_id
                or existing.source_request_hash != request.source_request_hash
                or stored_provenance.get("run_id") != request.source_run_id
                or stored_provenance.get("request_hash") != request.source_request_hash
            ):
                raise ChangeSetConflictError(
                    "tool call identity was already used for a different change proposal"
                )
            return existing

        provenance = self._require_agent_provenance(project_id, request)
        return self._create(
            project_id,
            request,
            change_set_id=change_set_id,
            provenance=provenance,
        )

    def _create(
        self,
        project_id: str,
        request: ChangeSetCreateRequest,
        *,
        change_set_id: str,
        provenance: dict[str, object] | None,
    ) -> ChangeSetResponse:
        binding = self._require_binding(project_id)
        root, snapshot, current = self._snapshot(binding)
        self._require_base(request.base_revision, request.base_fingerprint, current)

        files_by_id = {item.file_id: item for item in snapshot.files}
        paths_casefold = {item.relative_path.casefold() for item in snapshot.files}
        targeted: set[str] = set()
        changes: list[CandidateFileChange] = []
        for item in request.changes:
            if item.operation is ChangeOperation.CREATE:
                relative_path = self._normalize_new_path(item.relative_path or "")
                file_kind = classify_project_file_kind(relative_path)
                target_key = relative_path.casefold()
                if target_key in paths_casefold or os.path.lexists(
                    root / Path(relative_path)
                ):
                    raise ChangeSetInputError(
                        f"create target already exists: {relative_path}"
                    )
                before_text = ""
                before_hash = None
                preserve_bom = False
                after_text = item.after_content or ""
                self._validate_text(relative_path, after_text)
                proposed_after_hash = _sha256(after_text.encode("utf-8"))
                file_id = None
            else:
                if item.file_id not in files_by_id:
                    raise ChangeSetInputError(
                        f"file_id is not present in the current snapshot: {item.file_id}"
                    )
                file_record = files_by_id[item.file_id]
                relative_path = file_record.relative_path
                file_kind = file_record.kind
                target_key = relative_path.casefold()
                before_bytes, before_text, preserve_bom = self._read_snapshot_file(
                    root,
                    file_record,
                )
                before_hash = _sha256(before_bytes)
                if before_hash != file_record.sha256:
                    raise ChangeSetConflictError(
                        f"project file changed while creating the change set: {relative_path}"
                    )
                file_id = file_record.file_id
                if item.operation is ChangeOperation.UPDATE:
                    after_text = self._preserve_newline_style(
                        item.after_content or "",
                        before_text,
                    )
                    self._validate_text(relative_path, after_text)
                    proposed_after_hash = _sha256(
                        _encode_text(after_text, preserve_utf8_bom=preserve_bom)
                    )
                    if proposed_after_hash == before_hash:
                        raise ChangeSetInputError(
                            f"update does not change file content: {relative_path}"
                        )
                else:
                    after_text = ""
                    proposed_after_hash = None

            if target_key in targeted:
                raise ChangeSetInputError(
                    f"a change set cannot target one path more than once: {relative_path}"
                )
            targeted.add(target_key)

            change_id = self._change_id(
                item.operation,
                relative_path,
                before_hash,
                proposed_after_hash,
            )
            hunks = self._build_hunks(
                change_id,
                before_text,
                after_text,
                operation=item.operation,
            )
            changes.append(
                CandidateFileChange(
                    change_id=change_id,
                    operation=item.operation,
                    file_id=file_id,
                    relative_path=relative_path,
                    rationale=item.rationale,
                    before_sha256=before_hash,
                    proposed_after_sha256=proposed_after_hash,
                    preserve_utf8_bom=preserve_bom,
                    post_draft_gate_scope=(
                        "prose_body"
                        if item.operation is not ChangeOperation.DELETE
                        and file_kind == "prose"
                        else "none"
                    ),
                    hunks=hunks,
                )
            )

        created_at = _now()
        record = ChangeSetResponse(
            change_set_id=change_set_id,
            project_id=project_id,
            base_revision=current.revision,
            base_fingerprint=current.fingerprint,
            title=request.title,
            summary=request.summary,
            origin=request.origin,
            source_run_id=request.source_run_id,
            source_tool_call_id=request.source_tool_call_id,
            source_request_hash=request.source_request_hash,
            source_skill_ids=request.source_skill_ids,
            gate_required=(
                bool(request.gate_required)
                or request.origin in {
                    ChangeSetOrigin.AGENT,
                    ChangeSetOrigin.SKILL_WORKFLOW,
                }
            ),
            status=ChangeSetStatus.REVIEWING,
            review_revision=0,
            changes=changes,
            created_at=created_at,
            updated_at=created_at,
        )
        payload = self._payload(record)
        if provenance is None:
            self.store.create_change_set(payload)
        else:
            provenance = {
                **provenance,
                "change_set_id": record.change_set_id,
                "created_at": created_at.isoformat(),
            }
            self.store.create_agent_change_set(payload, provenance)
        return record

    @staticmethod
    def _agent_change_set_id(run_id: str, tool_call_id: str) -> str:
        material = f"{run_id}\0{tool_call_id}".encode("utf-8")
        return f"changeset-{_sha256(material)[:32]}"

    def _require_agent_provenance(
        self,
        project_id: str,
        request: ChangeSetCreateRequest,
    ) -> dict[str, object]:
        run_id = request.source_run_id
        tool_call_id = request.source_tool_call_id
        request_hash = request.source_request_hash
        if run_id is None or tool_call_id is None or request_hash is None:
            raise ChangeSetInputError("agent candidate is missing Runtime provenance")
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    r.id AS run_id,
                    r.project_binding_id,
                    r.context_pack_id,
                    r.project_revision,
                    r.project_fingerprint,
                    r.status AS run_status,
                    tc.id AS tool_call_id,
                    tc.tool_name,
                    tc.status AS tool_status,
                    tc.request_hash
                FROM run AS r
                JOIN tool_call AS tc ON tc.run_id = r.id
                WHERE r.id = ? AND tc.id = ?
                """,
                (run_id, tool_call_id),
            ).fetchone()
        if row is None:
            raise ChangeSetInputError("agent Run or ToolCall provenance was not found")
        if str(row["project_binding_id"]) != project_id:
            raise ChangeSetInputError("agent ToolCall belongs to another project")
        if row["context_pack_id"] is None:
            raise ChangeSetInputError("agent change proposal requires a bound Context Pack")
        if (
            int(row["project_revision"]) != request.base_revision
            or str(row["project_fingerprint"]) != request.base_fingerprint
        ):
            raise ChangeSetConflictError(
                "agent proposal base does not match its Run project snapshot"
            )
        if str(row["run_status"]) != "running":
            raise ChangeSetStateError("agent proposal Run is not running")
        if str(row["tool_name"]) != "change_set_propose":
            raise ChangeSetInputError("ToolCall is not a change-set proposal")
        if str(row["tool_status"]) not in {"pending", "running"}:
            raise ChangeSetStateError("change-set ToolCall is not executable")
        if str(row["request_hash"]) != request_hash:
            raise ChangeSetConflictError(
                "agent proposal request hash does not match its ToolCall"
            )
        return {
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "context_pack_id": str(row["context_pack_id"]),
            "project_id": project_id,
            "project_revision": request.base_revision,
            "project_fingerprint": request.base_fingerprint,
            "request_hash": request_hash,
        }

    def decide(
        self,
        change_set_id: str,
        request: ChangeSetDecisionRequest,
    ) -> ChangeSetResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.REVIEWING, ChangeSetStatus.READY}:
            raise ChangeSetStateError(
                f"change set decisions are not allowed in state {record.status.value}"
            )
        self._require_review_revision(record, request.expected_review_revision)
        updates = {item.hunk_id: item.decision for item in request.decisions}
        known = {
            hunk.hunk_id
            for change in record.changes
            for hunk in change.hunks
        }
        unknown = sorted(set(updates) - known)
        if unknown:
            raise ChangeSetInputError(f"unknown hunk IDs: {', '.join(unknown)}")

        changes: list[CandidateFileChange] = []
        for change in record.changes:
            changes.append(
                change.model_copy(
                    update={
                        "hunks": [
                            hunk.model_copy(
                                update={"decision": updates.get(hunk.hunk_id, hunk.decision)}
                            )
                            for hunk in change.hunks
                        ]
                    }
                )
            )
        pending = any(
            hunk.decision is HunkDecision.PENDING
            for change in changes
            for hunk in change.hunks
        )
        updated = record.model_copy(
            update={
                "changes": changes,
                "status": (
                    ChangeSetStatus.REVIEWING if pending else ChangeSetStatus.READY
                ),
                "post_draft_gate": None,
                "error": None,
                "review_revision": record.review_revision + 1,
                "updated_at": _now(),
            }
        )
        updated = ChangeSetResponse.model_validate(updated.model_dump())
        self.store.save_change_set(self._payload(updated))
        return updated

    @staticmethod
    def _has_accepted_post_draft_body(record: ChangeSetResponse) -> bool:
        return any(
            change.post_draft_gate_scope == "prose_body"
            and change.operation is not ChangeOperation.DELETE
            and any(
                hunk.decision is HunkDecision.ACCEPTED for hunk in change.hunks
            )
            for change in record.changes
        )

    @classmethod
    def _post_draft_gate_required(cls, record: ChangeSetResponse) -> bool:
        return record.gate_required and cls._has_accepted_post_draft_body(record)

    def post_draft_gate(
        self,
        change_set_id: str,
        request: PostDraftGateRequest,
    ) -> ChangeSetResponse:
        """Materialize the reviewed candidate and run the managed post-draft Gate.

        The candidate is reconstructed from the current project snapshot and
        hunk decisions. No temporary project file, backup, or writeback journal
        is created until a later ``apply`` call has an exact passing receipt.
        """

        record = self.get(change_set_id)
        if record.status is not ChangeSetStatus.READY:
            raise ChangeSetStateError(
                f"post-draft Gate requires a ready change set; current state is {record.status.value}"
            )
        self._require_review_revision(record, request.expected_review_revision)
        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)
        if (
            current.revision != record.base_revision
            or current.fingerprint != record.base_fingerprint
        ):
            self._conflict(record, "project revision changed before the post-draft Gate")

        try:
            candidate_files, candidate_hash = self._materialize_candidate(
                root,
                snapshot,
                record,
            )
        except ChangeSetConflictError as exc:
            self._conflict(record, str(exc))

        if not candidate_files:
            raise ChangeSetGateError(
                "post-draft Gate requires at least one materialized candidate file"
            )
        gate_files = [
            item
            for item in candidate_files
            if item.post_draft_gate_scope == "prose_body"
            and item.operation is not ChangeOperation.DELETE
            and item.after_bytes is not None
        ]
        if not gate_files:
            raise ChangeSetGateError(
                "post-draft body Gate is not applicable because no accepted prose body remains"
            )
        binding = self._current_gate_binding(record)
        try:
            existing = self.store.find_post_draft_gate_receipt(
                change_set_id=record.change_set_id,
                review_revision=record.review_revision,
                candidate_hash=candidate_hash,
                base_revision=record.base_revision,
                base_fingerprint=record.base_fingerprint,
                context_pack_id=binding.context_pack_id,
                context_pack_hash=binding.context_pack_hash,
                knowledge_fingerprint=binding.knowledge_fingerprint,
                validator_package_fingerprint=(
                    binding.managed_validator.package_fingerprint
                ),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ChangeSetGateStaleError(
                "stored post-draft Gate receipt failed integrity validation: "
                + str(exc)
            ) from exc
        if existing is not None:
            receipt = self._validate_stored_gate_receipt(existing)
            self._require_current_gate_binding(receipt, binding)
            if receipt.status in {"passed", "blocked"}:
                return record.model_copy(update={"post_draft_gate": receipt})
            receipt_nonce = uuid4().hex
        else:
            receipt_nonce = None

        try:
            receipt = self._execute_post_draft_gate(
                record,
                root=root,
                snapshot=snapshot,
                candidate_files=gate_files,
                candidate_hash=candidate_hash,
                binding=binding,
                receipt_nonce=receipt_nonce,
            )
        except ChangeSetError:
            raise
        except Exception as exc:
            raise ChangeSetGateUnavailableError(
                "managed post-draft Gate failed before an immutable attempt could be recorded"
            ) from exc
        try:
            self.store.save_post_draft_gate_receipt(
                receipt.model_dump(mode="json", by_alias=True)
            )
        except Exception as exc:
            raise ChangeSetGateUnavailableError(
                "post-draft Gate attempt could not be persisted"
            ) from exc
        return record.model_copy(update={"post_draft_gate": receipt})

    def get_post_draft_gate(self, change_set_id: str) -> PostDraftGateResponse:
        record = self.get(change_set_id)
        return PostDraftGateResponse(
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            gate_required=record.gate_required,
            review_revision=record.review_revision,
            receipt=record.post_draft_gate,
        )

    def _materialize_candidate(
        self,
        root: Path,
        snapshot: ProjectInspectionResponse,
        record: ChangeSetResponse,
    ) -> tuple[list[_CandidateFile], str]:
        files_by_id = {item.file_id: item for item in snapshot.files}
        materialized: list[_CandidateFile] = []
        for change in record.changes:
            accepted = tuple(
                hunk.hunk_id
                for hunk in change.hunks
                if hunk.decision is HunkDecision.ACCEPTED
            )
            rejected = tuple(
                hunk.hunk_id
                for hunk in change.hunks
                if hunk.decision is HunkDecision.REJECTED
            )
            if not accepted:
                continue

            before_bytes: bytes | None = None
            before_text = ""
            if change.operation is not ChangeOperation.CREATE:
                file_record = files_by_id.get(change.file_id or "")
                if file_record is None or file_record.relative_path != change.relative_path:
                    raise ChangeSetConflictError(
                        f"target file identity changed: {change.relative_path}"
                    )
                before_bytes, before_text, preserve_bom = self._read_snapshot_file(
                    root,
                    file_record,
                )
                if preserve_bom != change.preserve_utf8_bom:
                    raise ChangeSetConflictError(
                        f"target encoding changed: {change.relative_path}"
                    )
                if _sha256(before_bytes) != change.before_sha256:
                    raise ChangeSetConflictError(
                        f"target hash changed: {change.relative_path}"
                    )
            elif os.path.lexists(root / Path(change.relative_path)):
                raise ChangeSetConflictError(
                    f"create target now exists: {change.relative_path}"
                )

            if change.operation is ChangeOperation.DELETE:
                after_bytes = None
            else:
                after_text = self._apply_hunk_decisions(before_text, change)
                self._validate_text(change.relative_path, after_text)
                after_bytes = _encode_text(
                    after_text,
                    preserve_utf8_bom=change.preserve_utf8_bom,
                )
            materialized.append(
                _CandidateFile(
                    change_id=change.change_id,
                    operation=change.operation,
                    relative_path=change.relative_path,
                    before_sha256=change.before_sha256,
                    after_bytes=after_bytes,
                    post_draft_gate_scope=change.post_draft_gate_scope,
                    accepted_hunk_ids=accepted,
                    rejected_hunk_ids=rejected,
                )
            )

        hash_material = [
            {
                "change_id": item.change_id,
                "operation": item.operation.value,
                "relative_path": item.relative_path,
                "before_sha256": item.before_sha256,
                "after_sha256": (
                    None if item.after_bytes is None else _sha256(item.after_bytes)
                ),
                "byte_length": (
                    None if item.after_bytes is None else len(item.after_bytes)
                ),
                "post_draft_gate_scope": item.post_draft_gate_scope,
                "accepted_hunk_ids": list(item.accepted_hunk_ids),
                "rejected_hunk_ids": list(item.rejected_hunk_ids),
            }
            for item in materialized
        ]
        return materialized, canonical_json_hash(
            {
                "schema": "zhimeng.post-draft-candidate.v1",
                "change_set_id": record.change_set_id,
                "review_revision": record.review_revision,
                "base_revision": record.base_revision,
                "base_fingerprint": record.base_fingerprint,
                "files": hash_material,
            }
        )

    def _context_binding(
        self,
        record: ChangeSetResponse,
    ) -> tuple[str | None, str | None, str | None]:
        provenance = self.store.get_change_set_provenance(record.change_set_id)
        if provenance is None:
            return None, None, None
        context_pack_id = str(provenance["context_pack_id"])
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM context_pack WHERE id = ?",
                (context_pack_id,),
            ).fetchone()
        if row is None:
            raise ChangeSetGateStaleError("bound Context Pack is no longer available")
        try:
            from zhimeng_runtime.context_models import ContextPack

            pack = ContextPack.model_validate_json(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise ChangeSetGateStaleError("bound Context Pack is invalid") from exc
        if (
            pack.context_pack_id != context_pack_id
            or pack.project_id != record.project_id
            or pack.project_revision != record.base_revision
            or pack.project_fingerprint != record.base_fingerprint
            or str(row["pack_hash"]) != pack.pack_hash
        ):
            raise ChangeSetGateStaleError("bound Context Pack identity changed")
        knowledge = pack.knowledge_fingerprint
        return context_pack_id, pack.pack_hash, knowledge

    def _is_managed_o2e_context_pack(self, context_pack_id: str | None) -> bool:
        """O2-E packs close over distillation-index identity, not live native knowledge."""
        if context_pack_id is None or not context_pack_id.startswith("context-pack-o2e-"):
            return False
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT source
                FROM nlcl_route_receipt
                WHERE context_pack_id = ?
                """,
                (context_pack_id,),
            ).fetchone()
        return row is not None and str(row["source"]) == "external_distillation"

    def _require_current_knowledge(
        self,
        project_id: str,
        expected: str | None,
        *,
        context_pack_id: str | None = None,
    ) -> str | None:
        if expected is None:
            return expected
        # Mirror Agent O2-E observation: distillation-index identity is closed over
        # at assemble time and must not be re-probed against native formal knowledge.
        if self._is_managed_o2e_context_pack(context_pack_id):
            return expected
        if self.knowledge_fingerprint_probe is None:
            return expected
        try:
            current = self.knowledge_fingerprint_probe(project_id)
        except Exception as exc:
            raise ChangeSetGateStaleError(
                "current knowledge fingerprint could not be observed"
            ) from exc
        if current != expected:
            raise ChangeSetGateStaleError(
                "knowledge fingerprint changed after the Context Pack was reviewed"
            )
        return current

    def _validator_identity(
        self,
        project_id: str,
        *,
        required_capability: str | None = None,
    ) -> _ManagedValidatorIdentity:
        executor = self.post_draft_gate_executor
        if executor is None:
            raise ChangeSetGateUnavailableError(
                "managed post-draft Gate is not configured"
            )
        if required_capability is not None:
            try:
                declaration_probe = getattr(
                    executor,
                    "active_skill_declaration",
                    None,
                )
                if declaration_probe is not None:
                    declaration = declaration_probe(
                        project_id,
                        "novel-creation-suite.draft-gate",
                    )
                else:
                    declaration = executor._service.get_active_declaration(  # type: ignore[attr-defined]
                        project_id,
                        "novel-creation-suite.draft-gate",
                    )
            except ManagedSkillPackageDriftError as exc:
                raise ChangeSetGateStaleError(
                    "managed post-draft Gate declaration changed after registration"
                ) from exc
            except Exception as exc:
                raise ChangeSetGateUnavailableError(
                    "managed post-draft Gate declaration is not available"
                ) from exc
            if required_capability not in declaration.manifest.capabilities:
                raise ChangeSetGateUpgradeRequiredError(
                    "active post-draft Gate does not declare "
                    f"{required_capability}; the author must explicitly upgrade it "
                    "in the Skill lifecycle controls"
                )
        try:
            skill = executor.active_skill(
                project_id,
                "novel-creation-suite.draft-gate",
            )
        except AttributeError:
            skill = executor._service.get_active(  # type: ignore[attr-defined]
                project_id,
                "novel-creation-suite.draft-gate",
            )
        except ManagedSkillPackageDriftError as exc:
            raise ChangeSetGateStaleError(
                "managed post-draft Gate package changed after registration"
            ) from exc
        except Exception as exc:
            raise ChangeSetGateUnavailableError(
                "managed post-draft Gate package is not active"
            ) from exc
        if (
            required_capability is not None
            and required_capability not in skill.manifest.capabilities
        ):
            raise ChangeSetGateUpgradeRequiredError(
                "active post-draft Gate does not declare "
                f"{required_capability}; the author must explicitly upgrade it "
                "in the Skill lifecycle controls"
            )
        return _ManagedValidatorIdentity(
            skill_id=skill.manifest.id,
            version=skill.manifest.version,
            manifest_fingerprint=skill.manifest_fingerprint,
            package_fingerprint=skill.package_fingerprint,
            capabilities=tuple(skill.manifest.capabilities),
        )

    def _current_gate_binding(
        self,
        record: ChangeSetResponse,
    ) -> _GateBinding:
        context_pack_id, context_pack_hash, knowledge_fingerprint = self._context_binding(
            record
        )
        knowledge_fingerprint = self._require_current_knowledge(
            record.project_id,
            knowledge_fingerprint,
            context_pack_id=context_pack_id,
        )
        route_receipt_id: str | None = None
        route_receipt_hash: str | None = None
        knowledge_validator_bundle: KnowledgeValidatorBundle | None = None
        if context_pack_id is not None:
            resolver = self.knowledge_validator_resolver
            if resolver is None:
                raise ChangeSetGateUnavailableError(
                    "Context-bound post-draft Gate has no knowledge validator resolver"
                )
            if context_pack_hash is None:  # pragma: no cover - Context invariant.
                raise ChangeSetGateStaleError("bound Context Pack hash is missing")
            try:
                (
                    route_receipt_id,
                    route_receipt_hash,
                    knowledge_validator_bundle,
                ) = resolver(
                    record.project_id,
                    context_pack_id,
                    context_pack_hash,
                    knowledge_fingerprint,
                )
            except ChangeSetGateError:
                raise
            except Exception as exc:
                raise ChangeSetGateStaleError(
                    "bound NLCL knowledge validators could not be resolved"
                ) from exc
            if knowledge_validator_bundle is not None and (
                knowledge_validator_bundle.context_pack_id != context_pack_id
                or knowledge_validator_bundle.context_pack_hash != context_pack_hash
                or knowledge_validator_bundle.route_receipt_id != route_receipt_id
                or knowledge_validator_bundle.route_receipt_hash != route_receipt_hash
                or knowledge_validator_bundle.knowledge_fingerprint
                != knowledge_fingerprint
            ):
                raise ChangeSetGateStaleError(
                    "knowledge validator bundle does not match its reviewed Context/NLCL route"
                )
        return _GateBinding(
            context_pack_id=context_pack_id,
            context_pack_hash=context_pack_hash,
            knowledge_fingerprint=knowledge_fingerprint,
            route_receipt_id=route_receipt_id,
            route_receipt_hash=route_receipt_hash,
            knowledge_validator_bundle=knowledge_validator_bundle,
            managed_validator=self._validator_identity(
                record.project_id,
                required_capability=(
                    KNOWLEDGE_VALIDATOR_BUNDLE_CAPABILITY
                    if knowledge_validator_bundle is not None
                    else None
                ),
            ),
        )

    def _execute_post_draft_gate(
        self,
        record: ChangeSetResponse,
        *,
        root: Path,
        snapshot: ProjectInspectionResponse,
        candidate_files: list[_CandidateFile],
        candidate_hash: str,
        binding: _GateBinding,
        receipt_nonce: str | None = None,
    ) -> PostDraftGateReceipt:
        executor = self.post_draft_gate_executor
        if executor is None:  # pragma: no cover - guarded by _validator_identity
            raise ChangeSetGateUnavailableError("managed post-draft Gate is not configured")
        workspace_root = self.state_root / "_post-draft-gates"
        workspace_root.mkdir(parents=True, exist_ok=True)
        workspace = Path(
            tempfile.mkdtemp(
                prefix=f"{record.change_set_id}-",
                dir=str(workspace_root),
            )
        )
        project_snapshot = workspace / "project-snapshot"
        run_staging = workspace / "run-staging"
        project_snapshot.mkdir()
        run_staging.mkdir()
        try:
            self._copy_snapshot_for_gate(root, snapshot, project_snapshot)
            request_payload = {
                "schema": "zhimeng.creation-draft-gate-request.v1",
                "candidate_id": record.change_set_id,
                "candidate_manifest": {
                    "schema": "zhimeng.candidate-draft-manifest.v1",
                    "manifest_id": f"{record.change_set_id}:{record.review_revision}",
                    "files": [
                        {
                            "path": item.relative_path,
                            "after_sha256": _sha256(item.after_bytes or b""),
                            "byte_length": len(item.after_bytes or b""),
                        }
                        for item in candidate_files
                        if item.after_bytes is not None
                    ],
                },
                "candidate_files": [
                    {
                        "path": item.relative_path,
                        "after_bytes_base64": base64.b64encode(
                            item.after_bytes or b""
                        ).decode("ascii"),
                        "after_sha256": _sha256(item.after_bytes or b""),
                    }
                    for item in candidate_files
                    if item.after_bytes is not None
                ],
                "knowledge_validator_bundle": (
                    None
                    if binding.knowledge_validator_bundle is None
                    else binding.knowledge_validator_bundle.model_dump(
                        mode="json",
                        by_alias=True,
                    )
                ),
            }
            identity = canonical_json_hash(
                {
                    "change_set_id": record.change_set_id,
                    "review_revision": record.review_revision,
                    "candidate_hash": candidate_hash,
                    "validator_package_fingerprint": (
                        binding.managed_validator.package_fingerprint
                    ),
                    "knowledge_validator_bundle_hash": (
                        binding.knowledge_validator_bundle_hash
                    ),
                }
            )[:32]
            execution_identity = (
                identity
                if receipt_nonce is None
                else canonical_json_hash(
                    {
                        "gate_identity": identity,
                        "attempt_nonce": receipt_nonce,
                    }
                )[:32]
            )
            run_id = f"post-draft-gate-run-{execution_identity}"
            tool_call_id = f"post-draft-gate-call-{execution_identity}"
            request_input_hash = canonical_json_hash(request_payload)
            candidate_manifest_hash = canonical_json_hash(
                request_payload["candidate_manifest"]
            )
            try:
                outcome = asyncio.run(
                    executor.execute(
                        project_id=record.project_id,
                        project_revision=record.base_revision,
                        project_fingerprint=record.base_fingerprint,
                        run_id=run_id,
                        tool_call_id=tool_call_id,
                        skill_id=binding.managed_validator.skill_id,
                        input_payload=request_payload,
                        project_snapshot=project_snapshot,
                        run_staging=run_staging,
                    )
                )
            except Exception as exc:
                raise ChangeSetGateUnavailableError(
                    "managed post-draft Gate execution failed before it produced a receipt"
                ) from exc
            return self._gate_receipt_from_outcome(
                record,
                outcome,
                candidate_hash=candidate_hash,
                binding=binding,
                receipt_nonce=receipt_nonce,
                request_input_hash=request_input_hash,
                candidate_manifest_hash=candidate_manifest_hash,
                expected_run_id=run_id,
                expected_tool_call_id=tool_call_id,
                candidate_file_count=sum(
                    item.after_bytes is not None for item in candidate_files
                ),
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _copy_snapshot_for_gate(
        self,
        root: Path,
        snapshot: ProjectInspectionResponse,
        destination: Path,
    ) -> None:
        for file_record in snapshot.files:
            source = Path(file_record.absolute_path)
            if _is_link_or_reparse(source) or int(getattr(source.lstat(), "st_nlink", 1)) > 1:
                raise ChangeSetConflictError(
                    f"project file became unsafe during Gate snapshot: {file_record.relative_path}"
                )
            resolved = source.resolve(strict=True)
            if not _is_within(resolved, root):
                raise ChangeSetConflictError(
                    f"project file escaped its root during Gate snapshot: {file_record.relative_path}"
                )
            content = source.read_bytes()
            if _sha256(content) != file_record.sha256:
                raise ChangeSetConflictError(
                    f"project file changed during Gate snapshot: {file_record.relative_path}"
                )
            target = destination / Path(file_record.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def _gate_receipt_from_outcome(
        self,
        record: ChangeSetResponse,
        outcome: "SkillExecutionOutcome",
        *,
        candidate_hash: str,
        binding: _GateBinding,
        candidate_file_count: int,
        request_input_hash: str,
        candidate_manifest_hash: str,
        expected_run_id: str,
        expected_tool_call_id: str,
        receipt_nonce: str | None = None,
    ) -> PostDraftGateReceipt:
        skill_receipt = outcome.receipt
        actual_skill_receipt_hash = canonical_json_hash(
            skill_receipt.model_dump(mode="json", by_alias=True)
        )
        binding_errors: list[str] = []
        if outcome.receipt_hash != actual_skill_receipt_hash:
            binding_errors.append("Skill receipt hash does not match its payload")
        if not isinstance(outcome.output_bytes, bytes):
            binding_errors.append("Skill outcome does not expose immutable output bytes")
        elif _sha256(outcome.output_bytes) != skill_receipt.output_hash:
            binding_errors.append("Skill output hash does not match its raw output")
        if skill_receipt.execution_status == "succeeded" and isinstance(
            outcome.output_bytes, bytes
        ):
            try:
                raw_output = json.loads(outcome.output_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                binding_errors.append("Skill raw output is not a UTF-8 JSON envelope")
            else:
                if not isinstance(raw_output, dict):
                    binding_errors.append("Skill raw output is not an object envelope")
                else:
                    if canonical_json_hash(raw_output.get("result")) != canonical_json_hash(
                        outcome.result
                    ):
                        binding_errors.append(
                            "Skill stdout result does not match the parsed outcome"
                        )
                    if raw_output.get("summary") != outcome.result_summary:
                        binding_errors.append(
                            "Skill stdout summary does not match the parsed outcome"
                        )
                    if raw_output.get("health") != skill_receipt.domain_status:
                        binding_errors.append(
                            "Skill stdout health does not match its receipt"
                        )
        expected_receipt_fields = {
            "run_id": expected_run_id,
            "tool_call_id": expected_tool_call_id,
            "project_id": record.project_id,
            "project_revision": record.base_revision,
            "project_fingerprint": record.base_fingerprint,
            "skill_id": binding.managed_validator.skill_id,
            "skill_version": binding.managed_validator.version,
            "manifest_fingerprint": binding.managed_validator.manifest_fingerprint,
            "package_fingerprint": binding.managed_validator.package_fingerprint,
            "input_hash": request_input_hash,
        }
        for field, expected in expected_receipt_fields.items():
            if getattr(skill_receipt, field) != expected:
                binding_errors.append(f"Skill receipt {field} does not match the Gate request")
        result: PostDraftGateResult | None = None
        error_code: str | None = None
        error_message: str | None = None
        if skill_receipt.execution_status == "succeeded":
            try:
                result = PostDraftGateResult.model_validate_json(
                    json.dumps(outcome.result, ensure_ascii=False),
                    strict=True,
                )
            except (TypeError, ValueError) as exc:
                error_code = "invalid_gate_result"
                error_message = (
                    "managed post-draft Gate output failed its contract: "
                    + str(exc)
                )[:4096]
            if result is not None:
                if result.candidate_id != record.change_set_id:
                    binding_errors.append(
                        "Gate result candidate_id does not match the ChangeSet"
                    )
                if result.candidate_manifest_hash != candidate_manifest_hash:
                    binding_errors.append(
                        "Gate result manifest hash does not match the request"
                    )
                if outcome.result_summary != result.summary:
                    binding_errors.append(
                        "Gate result summary does not match the Skill envelope"
                    )
                if (
                    result.knowledge_validator_bundle_hash
                    != binding.knowledge_validator_bundle_hash
                ):
                    binding_errors.append(
                        "Gate result knowledge validator bundle does not match the request"
                    )
                expected_validator_bindings = (
                    ()
                    if binding.knowledge_validator_bundle is None
                    else binding.knowledge_validator_bundle.validators
                )
                actual_validator_results = result.knowledge_validator_results
                if len(actual_validator_results) != len(expected_validator_bindings):
                    binding_errors.append(
                        "Gate result knowledge validator count does not match the request"
                    )
                else:
                    for expected, actual in zip(
                        expected_validator_bindings,
                        actual_validator_results,
                    ):
                        if (
                            actual.card_id != expected.card_id
                            or actual.version_id != expected.version_id
                            or actual.content_hash != expected.content_hash
                            or actual.rules_hash != expected.rules_hash
                        ):
                            binding_errors.append(
                                "Gate result knowledge validator identity does not match the request"
                            )
                            break
                if result.status == "blocked":
                    if skill_receipt.domain_status != "blocked" or skill_receipt.exit_code != 1:
                        binding_errors.append(
                            "blocked Gate result does not match Skill domain status"
                        )
                elif (
                    skill_receipt.domain_status not in {"healthy", "degraded"}
                    or skill_receipt.exit_code != 0
                ):
                    binding_errors.append(
                        "passing Gate result does not match Skill domain status"
                    )
        else:
            error = skill_receipt.error
            error_code = error.code if error is not None else "gate_execution_failed"
            error_message = (
                error.message
                if error is not None
                else "managed post-draft Gate did not produce a result"
            )
        if binding_errors:
            result = None
            error_code = "invalid_gate_outcome"
            error_message = ("; ".join(binding_errors))[:4096]
        execution_status = "succeeded" if result is not None else "failed"
        status = result.status if result is not None else "failed"
        created_at = skill_receipt.finished_at
        material = {
            "schema_id": "zhimeng.post-draft-gate-receipt.v1",
            "gate_receipt_id": "post-draft-gate-"
            + hashlib.sha256(
                f"{record.change_set_id}\0{record.review_revision}\0{candidate_hash}\0{binding.managed_validator.package_fingerprint}\0{binding.knowledge_validator_bundle_hash or ''}\0{receipt_nonce or ''}".encode(
                    "utf-8"
                )
            ).hexdigest()[:32],
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "review_revision": record.review_revision,
            "base_revision": record.base_revision,
            "base_fingerprint": record.base_fingerprint,
            "candidate_hash": candidate_hash,
            "validator_id": binding.managed_validator.skill_id,
            "validator_version": binding.managed_validator.version,
            "validator_manifest_fingerprint": (
                binding.managed_validator.manifest_fingerprint
            ),
            "validator_package_fingerprint": (
                binding.managed_validator.package_fingerprint
            ),
            "context_pack_id": binding.context_pack_id,
            "context_pack_hash": binding.context_pack_hash,
            "knowledge_fingerprint": binding.knowledge_fingerprint,
            "route_receipt_id": binding.route_receipt_id,
            "route_receipt_hash": binding.route_receipt_hash,
            "knowledge_validator_bundle_hash": (
                binding.knowledge_validator_bundle_hash
            ),
            "knowledge_validator_count": len(
                binding.knowledge_validator_card_ids
            ),
            "knowledge_validator_card_ids": binding.knowledge_validator_card_ids,
            "candidate_file_count": candidate_file_count,
            "skill_receipt_id": skill_receipt.receipt_id,
            "skill_receipt_hash": actual_skill_receipt_hash,
            "skill_output_hash": skill_receipt.output_hash,
            "execution_status": execution_status,
            "status": status,
            "result": result,
            "error_code": error_code,
            "error_message": error_message,
            "quality_assessed": False,
            "formal_draft_allowed": False,
            "created_at": created_at,
        }
        draft = PostDraftGateReceipt.model_construct(**material, receipt_hash="0" * 64)
        return PostDraftGateReceipt(
            **material,
            receipt_hash=post_draft_gate_receipt_hash(draft),
        )

    @staticmethod
    def _validate_stored_gate_receipt(payload: dict[str, object]) -> PostDraftGateReceipt:
        try:
            return PostDraftGateReceipt.model_validate_json(
                json.dumps(payload, ensure_ascii=False),
                strict=True,
            )
        except (TypeError, ValueError) as exc:
            raise ChangeSetGateStaleError(
                "stored post-draft Gate receipt failed integrity validation: "
                + str(exc)
            ) from exc

    @staticmethod
    def _require_current_validator(
        receipt: PostDraftGateReceipt,
        validator: _ManagedValidatorIdentity,
    ) -> None:
        receipt_validator = (
            receipt.validator_id,
            receipt.validator_version,
            receipt.validator_manifest_fingerprint,
            receipt.validator_package_fingerprint,
        )
        if receipt_validator != (
            validator.skill_id,
            validator.version,
            validator.manifest_fingerprint,
            validator.package_fingerprint,
        ):
            raise ChangeSetGateStaleError(
                "post-draft Gate validator changed after the receipt was issued"
            )

    @classmethod
    def _require_current_gate_binding(
        cls,
        receipt: PostDraftGateReceipt,
        binding: _GateBinding,
    ) -> None:
        if (
            receipt.context_pack_id != binding.context_pack_id
            or receipt.context_pack_hash != binding.context_pack_hash
            or receipt.knowledge_fingerprint != binding.knowledge_fingerprint
            or receipt.route_receipt_id != binding.route_receipt_id
            or receipt.route_receipt_hash != binding.route_receipt_hash
            or receipt.knowledge_validator_bundle_hash
            != binding.knowledge_validator_bundle_hash
            or receipt.knowledge_validator_count
            != len(binding.knowledge_validator_card_ids)
            or receipt.knowledge_validator_card_ids
            != binding.knowledge_validator_card_ids
        ):
            raise ChangeSetGateStaleError(
                "post-draft Gate knowledge validator binding changed after the receipt"
            )
        cls._require_current_validator(receipt, binding.managed_validator)

    def _require_gate_receipt(
        self,
        record: ChangeSetResponse,
        *,
        candidate_hash: str,
    ) -> PostDraftGateReceipt:
        binding = self._current_gate_binding(record)
        try:
            payload = self.store.find_post_draft_gate_receipt(
                change_set_id=record.change_set_id,
                review_revision=record.review_revision,
                candidate_hash=candidate_hash,
                base_revision=record.base_revision,
                base_fingerprint=record.base_fingerprint,
                context_pack_id=binding.context_pack_id,
                context_pack_hash=binding.context_pack_hash,
                knowledge_fingerprint=binding.knowledge_fingerprint,
                validator_package_fingerprint=(
                    binding.managed_validator.package_fingerprint
                ),
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ChangeSetGateStaleError(
                "stored post-draft Gate receipt failed integrity validation: "
                + str(exc)
            ) from exc
        if payload is None:
            try:
                latest_payload = self.store.get_latest_post_draft_gate_receipt(
                    record.change_set_id
                )
            except (TypeError, ValueError, RuntimeError) as exc:
                raise ChangeSetGateStaleError(
                    "stored post-draft Gate receipt failed integrity validation: "
                    + str(exc)
                ) from exc
            if latest_payload is not None:
                latest = self._validate_stored_gate_receipt(latest_payload)
                same_candidate_binding = (
                    latest.change_set_id == record.change_set_id
                    and latest.project_id == record.project_id
                    and latest.review_revision == record.review_revision
                    and latest.candidate_hash == candidate_hash
                    and latest.base_revision == record.base_revision
                    and latest.base_fingerprint == record.base_fingerprint
                    and latest.context_pack_id == binding.context_pack_id
                    and latest.context_pack_hash == binding.context_pack_hash
                    and latest.knowledge_fingerprint == binding.knowledge_fingerprint
                )
                if same_candidate_binding:
                    self._require_current_gate_binding(latest, binding)
            raise ChangeSetGateMissingError(
                "a current passing post-draft Gate receipt is required before apply"
            )
        receipt = self._validate_stored_gate_receipt(payload)
        self._require_current_gate_binding(receipt, binding)
        if receipt.status == "blocked":
            raise ChangeSetGateBlockedError(
                "post-draft Gate blocked the candidate; revise it and run the Gate again"
            )
        if receipt.status != "passed":
            raise ChangeSetGateStaleError(
                "post-draft Gate receipt is not a successful passing receipt"
            )
        return receipt

    def apply(
        self,
        change_set_id: str,
        request: ChangeSetApplyRequest,
    ) -> ChangeSetResponse:
        record = self.get(change_set_id)
        if record.status is not ChangeSetStatus.READY:
            raise ChangeSetStateError(
                f"only ready change sets can be applied; current state is {record.status.value}"
            )
        self._require_review_revision(record, request.expected_review_revision)
        accepted = [
            hunk
            for change in record.changes
            for hunk in change.hunks
            if hunk.decision is HunkDecision.ACCEPTED
        ]
        if not accepted:
            discarded = record.model_copy(
                update={
                    "status": ChangeSetStatus.DISCARDED,
                    "updated_at": _now(),
                }
            )
            self.store.save_change_set(self._payload(discarded))
            return discarded

        # Author-reviewed apply remains allowed. Formal-draft authorization is
        # evaluated and fail-closed against any grant of formal prose rights.
        formal_draft_authorization = self._require_denied_formal_draft_authorization(
            project_id=record.project_id,
            change_set_id=record.change_set_id,
            run_id=record.source_run_id,
        )

        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)
        if (
            current.revision != record.base_revision
            or current.fingerprint != record.base_fingerprint
        ):
            self._conflict(record, "project revision changed after the proposal was created")

        if self._post_draft_gate_required(record):
            try:
                _, candidate_hash = self._materialize_candidate(
                    root,
                    snapshot,
                    record,
                )
                self._require_gate_receipt(
                    record,
                    candidate_hash=candidate_hash,
                )
            except ChangeSetConflictError as exc:
                self._conflict(record, str(exc))
            # Ordered pipeline is fail-closed on apply when post-draft prose Gate is required.
            pipeline = self.evaluate_gate_pipeline(
                record.change_set_id,
                GatePipelineEvaluateRequest(
                    expected_review_revision=record.review_revision,
                ),
            )
            pipeline_receipt = pipeline.receipt
            if pipeline_receipt is None:
                raise ChangeSetGatePipelineBlockedError(
                    "ordered gate pipeline receipt is required before apply when post-draft Gate is required"
                )
            pipeline_status = getattr(
                pipeline_receipt.status, "value", str(pipeline_receipt.status)
            )
            if pipeline_status != "complete" or not pipeline_receipt.authorization_input_ready:
                raise ChangeSetGatePipelineBlockedError(
                    "ordered gate pipeline blocked or incomplete for this candidate; "
                    f"status={pipeline_status}"
                )
            if (
                pipeline_receipt.formal_draft_allowed is not False
                or pipeline_receipt.quality_assessed is not False
                or pipeline_receipt.authorization_unlock is not False
            ):
                raise ChangeSetWritebackError(
                    "ordered gate pipeline attempted to unlock formal draft or claim quality assessed"
                )

        files_by_id = {item.file_id: item for item in snapshot.files}
        artifact_root = self._artifact_root(record.change_set_id)
        prepared: list[_PreparedWrite] = []
        created_directories: list[Path] = []
        applying = record.model_copy(
            update={"status": ChangeSetStatus.APPLYING, "updated_at": _now()}
        )
        try:
            for change in record.changes:
                item = self._prepare_change(
                    root,
                    change,
                    files_by_id,
                    artifact_root,
                    created_directories,
                )
                if item is not None:
                    prepared.append(item)
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="apply",
                status="prepared",
                created_directories=created_directories,
            )
            self.store.save_change_set(self._payload(applying))
            for item in prepared:
                self._apply_prepared(item)
            self._finalize_prepared(prepared)
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="apply",
                status="files_applied",
                created_directories=created_directories,
            )

            result_root, result_snapshot, result_binding = self._snapshot(binding)
            if result_root != root:
                raise ChangeSetWritebackError("project root changed during writeback")
            self._verify_prepared_result(root, prepared, result_snapshot)
            manifest = self._writeback_manifest(
                record,
                prepared,
                result_snapshot,
                result_binding,
                outcome="applied",
                created_directories=created_directories,
            )
            _atomic_write_json(
                artifact_root / "writeback-manifest.json",
                manifest.model_dump(mode="json", by_alias=True),
            )
            self._record_writeback_evidence(
                record,
                manifest,
                artifact_root=artifact_root,
                formal_draft_authorization=formal_draft_authorization,
            )
            applied = record.model_copy(
                update={
                    "status": ChangeSetStatus.APPLIED,
                    "writeback": manifest,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            applied = ChangeSetResponse.model_validate(applied.model_dump())
            self.store.save_change_set(self._payload(applied))
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="apply",
                status="committed",
                created_directories=created_directories,
            )
            return applied
        except Exception as exc:
            had_applied = any(item.applied for item in prepared)
            rollback_error = self._restore_prepared(prepared)
            self._remove_empty_directories(created_directories)
            if rollback_error is None and had_applied:
                try:
                    self._snapshot(binding)
                except Exception as sync_exc:
                    rollback_error = f"restored project rescan failed: {sync_exc}"
            message = str(exc) or exc.__class__.__name__
            if rollback_error is not None:
                message = f"{message}; rollback also failed: {rollback_error}"
            (artifact_root / "writeback-manifest.json").unlink(missing_ok=True)
            try:
                self._write_journal(
                    record,
                    artifact_root,
                    prepared,
                    transaction="apply",
                    status=(
                        "recovery_required"
                        if rollback_error is not None
                        else "rolled_back_after_failure"
                    ),
                    created_directories=created_directories,
                )
            except Exception:
                pass
            if (
                isinstance(exc, ChangeSetInputError)
                and rollback_error is None
                and not any(item.applied for item in prepared)
            ):
                (artifact_root / "writeback-journal.json").unlink(missing_ok=True)
                retryable = record.model_copy(
                    update={
                        "status": ChangeSetStatus.READY,
                        "error": message,
                        "updated_at": _now(),
                    }
                )
                self.store.save_change_set(self._payload(retryable))
                raise exc
            if rollback_error is not None:
                pending = applying.model_copy(
                    update={
                        "error": message,
                        "updated_at": _now(),
                    }
                )
                self.store.save_change_set(self._payload(pending))
                raise ChangeSetWritebackError(
                    f"{message}; recovery is required before this transaction can be finalized"
                ) from exc
            failed = applying.model_copy(
                update={
                    "status": (
                        ChangeSetStatus.CONFLICTED
                        if isinstance(exc, ChangeSetConflictError)
                        else ChangeSetStatus.FAILED
                    ),
                    "error": message,
                    "updated_at": _now(),
                }
            )
            self.store.save_change_set(self._payload(failed))
            if isinstance(exc, ChangeSetConflictError) and rollback_error is None:
                raise exc
            raise ChangeSetWritebackError(message) from exc

    def rollback(
        self,
        change_set_id: str,
        request: ChangeSetRollbackRequest,
    ) -> ChangeSetResponse:
        record = self.get(change_set_id)
        if record.status is not ChangeSetStatus.APPLIED or record.writeback is None:
            raise ChangeSetStateError("only applied change sets can be rolled back")
        self._require_review_revision(record, request.expected_review_revision)
        formal_draft_authorization = self._require_denied_formal_draft_authorization(
            project_id=record.project_id,
            change_set_id=record.change_set_id,
            run_id=record.source_run_id,
        )
        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)
        if current.fingerprint != record.writeback.result_fingerprint:
            self._conflict(record, "project changed after this change set was applied")

        files_by_path = {item.relative_path.casefold(): item for item in snapshot.files}
        artifact_root = self._artifact_root(record.change_set_id)
        prepared: list[_PreparedWrite] = []
        created_directories: list[Path] = []
        rolling_back = record.model_copy(
            update={
                "status": ChangeSetStatus.ROLLING_BACK,
                "error": None,
                "updated_at": _now(),
            }
        )
        try:
            for result in record.writeback.files:
                target = root / Path(result.relative_path)
                backup = (
                    artifact_root / Path(result.backup_ref)
                    if result.backup_ref is not None
                    else None
                )
                current_file = files_by_path.get(result.relative_path.casefold())
                current_bytes: bytes | None = None
                if current_file is not None:
                    current_bytes, _, _ = self._read_snapshot_file(root, current_file)
                if result.after_sha256 is not None:
                    if current_bytes is None or _sha256(current_bytes) != result.after_sha256:
                        raise ChangeSetConflictError(
                            f"writeback target changed before rollback: {result.relative_path}"
                        )
                if result.operation is ChangeOperation.CREATE:
                    after_bytes = None
                else:
                    if backup is None:
                        raise ChangeSetWritebackError(
                            f"rollback backup is missing: {result.relative_path}"
                        )
                    after_bytes = self._read_verified_backup(
                        backup,
                        result.before_sha256,
                        artifact_root,
                    )
                temporary = (
                    self._write_temporary_sibling(target, after_bytes)
                    if after_bytes is not None
                    else None
                )
                rollback_backup = artifact_root / "rollback-current" / Path(
                    result.relative_path
                )
                if current_bytes is not None:
                    rollback_backup.parent.mkdir(parents=True, exist_ok=True)
                    rollback_backup.write_bytes(current_bytes)
                prepared.append(
                    _PreparedWrite(
                        change=next(
                            change
                            for change in record.changes
                            if change.change_id == result.change_id
                        ),
                        root=root,
                        target=target,
                        before_bytes=current_bytes,
                        after_bytes=after_bytes,
                        backup=rollback_backup if current_bytes is not None else None,
                        temporary=temporary,
                        tombstone=(
                            target.with_name(
                                f".{target.name}.zhimeng-{uuid4().hex}.deleted"
                            )
                            if after_bytes is None
                            else None
                        ),
                        accepted_hunks=result.accepted_hunk_ids,
                        rejected_hunks=result.rejected_hunk_ids,
                    )
                )
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="rollback",
                status="prepared",
            )
            self.store.save_change_set(self._payload(rolling_back))
            for item in prepared:
                self._apply_prepared(item)
            self._finalize_prepared(prepared)
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="rollback",
                status="files_applied",
            )
            self._remove_recorded_directories(
                root,
                record.writeback.created_directories,
            )
            result_root, result_snapshot, result_binding = self._snapshot(binding)
            if result_root != root:
                raise ChangeSetWritebackError("project root changed during rollback")
            self._verify_prepared_result(root, prepared, result_snapshot)
            rollback_manifest = self._writeback_manifest(
                record,
                prepared,
                result_snapshot,
                result_binding,
                outcome="rolled_back",
                base_revision=record.writeback.result_revision,
                base_fingerprint=record.writeback.result_fingerprint,
                created_directories=[],
            )
            _atomic_write_json(
                artifact_root / "rollback-manifest.json",
                rollback_manifest.model_dump(mode="json", by_alias=True),
            )
            self._record_writeback_evidence(
                record,
                rollback_manifest,
                artifact_root=artifact_root,
                formal_draft_authorization=formal_draft_authorization,
            )
            rolled_back = record.model_copy(
                update={
                    "status": ChangeSetStatus.ROLLED_BACK,
                    "rollback": rollback_manifest,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            rolled_back = ChangeSetResponse.model_validate(rolled_back.model_dump())
            self.store.save_change_set(self._payload(rolled_back))
            self._write_journal(
                record,
                artifact_root,
                prepared,
                transaction="rollback",
                status="committed",
            )
            return rolled_back
        except Exception as exc:
            had_applied = any(item.applied for item in prepared)
            rollback_error = self._restore_prepared(prepared)
            self._remove_empty_directories(created_directories)
            if rollback_error is None and had_applied:
                try:
                    self._snapshot(binding)
                except Exception as sync_exc:
                    rollback_error = f"rollback recovery rescan failed: {sync_exc}"
            message = str(exc) or exc.__class__.__name__
            if rollback_error is not None:
                message = f"{message}; rollback recovery also failed: {rollback_error}"
            (artifact_root / "rollback-manifest.json").unlink(missing_ok=True)
            try:
                self._write_journal(
                    record,
                    artifact_root,
                    prepared,
                    transaction="rollback",
                    status=(
                        "recovery_required"
                        if rollback_error is not None
                        else "restored_after_failure"
                    ),
                )
            except Exception:
                pass
            if rollback_error is not None:
                pending = rolling_back.model_copy(
                    update={
                        "error": message,
                        "updated_at": _now(),
                    }
                )
                self.store.save_change_set(self._payload(pending))
                raise ChangeSetWritebackError(
                    f"{message}; recovery is required before this transaction can be finalized"
                ) from exc
            restored = record.model_copy(
                update={"status": ChangeSetStatus.APPLIED, "error": message, "updated_at": _now()}
            )
            self.store.save_change_set(self._payload(restored))
            raise ChangeSetWritebackError(message) from exc

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.store.get_project_binding(project_id)
        if binding is None:
            raise ChangeSetNotFoundError("Project binding was not found")
        return binding

    def _snapshot(
        self,
        binding: ProjectBindingRecord,
    ) -> tuple[Path, ProjectInspectionResponse, ProjectBindingRecord]:
        root = self.reader.resolve(binding.root_path)
        observed_at = _now()
        payload = self.reader.inspect(root)
        project = payload.get("project")
        if not isinstance(project, dict):
            raise ChangeSetWritebackError("Project Reader returned an invalid snapshot")
        current = self.store.sync_project_binding(
            project_id=str(project["project_id"]),
            root_path=str(project["root_path"]),
            display_name=str(project["display_name"]),
            fingerprint=str(project["fingerprint"]),
            observed_at=observed_at,
        )
        if current.fingerprint != project["fingerprint"]:
            raise ChangeSetConflictError(
                "a newer project snapshot was committed during inspection"
            )
        project["revision"] = current.revision
        return root, ProjectInspectionResponse.model_validate(payload), current

    @staticmethod
    def _require_base(
        revision: int,
        fingerprint: str,
        current: ProjectBindingRecord,
    ) -> None:
        if revision != current.revision or fingerprint != current.fingerprint:
            raise ChangeSetConflictError(
                "change set base does not match the current project revision"
            )

    @staticmethod
    def _require_review_revision(
        record: ChangeSetResponse,
        expected: int,
    ) -> None:
        if record.review_revision != expected:
            raise ChangeSetConflictError(
                "change set review revision changed; refresh before continuing"
            )

    def _conflict(self, record: ChangeSetResponse, message: str) -> NoReturn:
        conflicted = record.model_copy(
            update={
                "status": ChangeSetStatus.CONFLICTED,
                "error": message,
                "updated_at": _now(),
            }
        )
        self.store.save_change_set(self._payload(conflicted))
        raise ChangeSetConflictError(message)

    @staticmethod
    def _normalize_new_path(raw: str) -> str:
        if "\\" in raw or "\x00" in raw or ":" in raw:
            raise ChangeSetInputError("relative_path must use safe POSIX separators")
        candidate = PurePosixPath(raw)
        if candidate.is_absolute() or not candidate.parts:
            raise ChangeSetInputError("relative_path must be a non-empty relative path")
        if any(part in {"", ".", ".."} for part in candidate.parts):
            raise ChangeSetInputError("relative_path cannot contain dot segments")
        if not candidate.parts[0][:3].endswith("_") or candidate.parts[0][:2] not in {
            f"{index:02d}" for index in range(1, 10)
        }:
            raise ChangeSetInputError("new files must be created inside a 01-09 section")
        normalized = candidate.as_posix()
        if Path(normalized).suffix.casefold() not in TEXT_EXTENSIONS:
            raise ChangeSetInputError("new project files must use a supported text extension")
        return normalized

    @staticmethod
    def _validate_text(relative_path: str, content: str) -> None:
        if "\x00" in content:
            raise ChangeSetInputError("project text cannot contain NUL characters")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ChangeSetInputError("project text must be valid UTF-8") from exc
        if len(encoded) > MAX_TEXT_FILE_BYTES:
            raise ChangeSetInputError(
                f"project text exceeds the {MAX_TEXT_FILE_BYTES} byte limit"
            )
        if Path(relative_path).suffix.casefold() == ".json":
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                raise ChangeSetInputError(
                    f"structured project JSON is invalid: {relative_path}"
                ) from exc

    @staticmethod
    def _preserve_newline_style(candidate: str, current: str) -> str:
        normalized = candidate.replace("\r\n", "\n").replace("\r", "\n")
        if "\r\n" in current:
            return normalized.replace("\n", "\r\n")
        if "\r" in current and "\n" not in current:
            return normalized.replace("\n", "\r")
        return normalized

    def _read_snapshot_file(
        self,
        root: Path,
        file_record: ProjectFile,
    ) -> tuple[bytes, str, bool]:
        if Path(file_record.relative_path).suffix.casefold() not in TEXT_EXTENSIONS:
            raise ChangeSetInputError(
                f"change sets only support text project files: {file_record.relative_path}"
            )
        path = Path(file_record.absolute_path)
        try:
            before = path.lstat()
            if _is_link_or_reparse(path):
                raise ChangeSetConflictError(
                    f"project file became a link or reparse point: {file_record.relative_path}"
                )
            if int(getattr(before, "st_nlink", 1)) > 1:
                raise ChangeSetConflictError(
                    f"project file became hardlinked: {file_record.relative_path}"
                )
            resolved = path.resolve(strict=True)
            if not _is_within(resolved, root):
                raise ChangeSetConflictError(
                    f"project file escaped its root: {file_record.relative_path}"
                )
            if before.st_size > MAX_TEXT_FILE_BYTES:
                raise ChangeSetInputError(
                    f"project text exceeds the {MAX_TEXT_FILE_BYTES} byte limit"
                )
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                content = handle.read(MAX_TEXT_FILE_BYTES + 1)
                finished = os.fstat(handle.fileno())
            after = path.lstat()
        except OSError as exc:
            raise ChangeSetConflictError(
                f"cannot read current project file: {file_record.relative_path}"
            ) from exc
        if len(content) > MAX_TEXT_FILE_BYTES:
            raise ChangeSetInputError(
                f"project text exceeds the {MAX_TEXT_FILE_BYTES} byte limit"
            )
        if not (
            _stat_key(before)
            == _stat_key(opened)
            == _stat_key(finished)
            == _stat_key(after)
        ):
            raise ChangeSetConflictError(
                f"project file changed while it was read: {file_record.relative_path}"
            )
        if _sha256(content) != file_record.sha256:
            raise ChangeSetConflictError(
                f"project file hash changed after inspection: {file_record.relative_path}"
            )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ChangeSetInputError(
                f"project text is not UTF-8: {file_record.relative_path}"
            ) from exc
        if "\x00" in text:
            raise ChangeSetInputError(
                f"project text contains NUL characters: {file_record.relative_path}"
            )
        return content, text, content.startswith(UTF8_BOM)

    @staticmethod
    def _change_id(
        operation: ChangeOperation,
        relative_path: str,
        before_hash: str | None,
        after_hash: str | None,
    ) -> str:
        material = json.dumps(
            [operation.value, relative_path, before_hash, after_hash],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"change-{_sha256(material)[:32]}"

    @staticmethod
    def _build_hunks(
        change_id: str,
        before: str,
        after: str,
        *,
        operation: ChangeOperation,
    ) -> list[ChangeHunk]:
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        if operation is ChangeOperation.CREATE:
            opcodes = [("insert", 0, 0, 0, len(after_lines))]
        elif operation is ChangeOperation.DELETE:
            opcodes = [("delete", 0, len(before_lines), 0, 0)]
        else:
            opcodes = [
                item
                for item in difflib.SequenceMatcher(
                    None,
                    before_lines,
                    after_lines,
                    autojunk=False,
                ).get_opcodes()
                if item[0] != "equal"
            ]
        if not opcodes:
            raise ChangeSetInputError("file change contains no textual differences")

        hunks: list[ChangeHunk] = []
        for index, (_, before_start, before_end, after_start, after_end) in enumerate(
            opcodes
        ):
            before_text = "".join(before_lines[before_start:before_end])
            after_text = "".join(after_lines[after_start:after_end])
            before_hash = _text_sha256(before_text)
            after_hash = _text_sha256(after_text)
            material = (
                f"{change_id}\0{index}\0{before_start}\0{before_end}\0"
                f"{after_start}\0{after_end}\0{before_hash}\0{after_hash}"
            ).encode("utf-8")
            hunks.append(
                ChangeHunk(
                    hunk_id=f"hunk-{_sha256(material)[:32]}",
                    index=index,
                    before_start=before_start,
                    before_end=before_end,
                    after_start=after_start,
                    after_end=after_end,
                    before_text=before_text,
                    after_text=after_text,
                    before_sha256=before_hash,
                    after_sha256=after_hash,
                )
            )
        return hunks

    def _prepare_change(
        self,
        root: Path,
        change: CandidateFileChange,
        files_by_id: dict[str, ProjectFile],
        artifact_root: Path,
        created_directories: list[Path],
    ) -> _PreparedWrite | None:
        accepted_hunks = [
            hunk.hunk_id
            for hunk in change.hunks
            if hunk.decision is HunkDecision.ACCEPTED
        ]
        rejected_hunks = [
            hunk.hunk_id
            for hunk in change.hunks
            if hunk.decision is HunkDecision.REJECTED
        ]
        if not accepted_hunks:
            return None
        target = root / Path(change.relative_path)
        before_bytes: bytes | None = None
        before_text = ""
        if change.operation is not ChangeOperation.CREATE:
            file_record = files_by_id.get(change.file_id or "")
            if file_record is None or file_record.relative_path != change.relative_path:
                raise ChangeSetConflictError(
                    f"target file identity changed: {change.relative_path}"
                )
            before_bytes, before_text, preserve_bom = self._read_snapshot_file(
                root,
                file_record,
            )
            if preserve_bom != change.preserve_utf8_bom:
                raise ChangeSetConflictError(
                    f"target encoding changed: {change.relative_path}"
                )
            if _sha256(before_bytes) != change.before_sha256:
                raise ChangeSetConflictError(
                    f"target hash changed: {change.relative_path}"
                )
        elif os.path.lexists(target):
            raise ChangeSetConflictError(
                f"create target now exists: {change.relative_path}"
            )

        if change.operation is ChangeOperation.DELETE:
            after_bytes = None
        elif change.operation is ChangeOperation.CREATE:
            after_bytes = change.hunks[0].after_text.encode("utf-8")
        else:
            after_text = self._apply_hunk_decisions(before_text, change)
            self._validate_text(change.relative_path, after_text)
            after_bytes = _encode_text(
                after_text,
                preserve_utf8_bom=change.preserve_utf8_bom,
            )
        if after_bytes == before_bytes:
            return None

        parent = target.parent
        missing: list[Path] = []
        cursor = parent
        while cursor != root and not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        self._validate_existing_parent_chain(root, cursor)
        for directory in reversed(missing):
            directory.mkdir()
            created_directories.append(directory)
        self._validate_existing_parent_chain(root, parent)

        backup = None
        if before_bytes is not None:
            backup = artifact_root / "backups" / Path(change.relative_path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before_bytes)
            if _sha256(backup.read_bytes()) != change.before_sha256:
                raise ChangeSetWritebackError(
                    f"backup verification failed: {change.relative_path}"
                )
        temporary = (
            self._write_temporary_sibling(target, after_bytes)
            if after_bytes is not None
            else None
        )
        tombstone = (
            target.with_name(f".{target.name}.zhimeng-{uuid4().hex}.deleted")
            if after_bytes is None
            else None
        )
        return _PreparedWrite(
            change=change,
            root=root,
            target=target,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            backup=backup,
            temporary=temporary,
            tombstone=tombstone,
            accepted_hunks=accepted_hunks,
            rejected_hunks=rejected_hunks,
        )

    @staticmethod
    def _apply_hunk_decisions(before: str, change: CandidateFileChange) -> str:
        lines = before.splitlines(keepends=True)
        output: list[str] = []
        cursor = 0
        for hunk in change.hunks:
            if hunk.before_start < cursor or hunk.before_end > len(lines):
                raise ChangeSetConflictError(
                    f"hunk range no longer matches: {hunk.hunk_id}"
                )
            output.extend(lines[cursor : hunk.before_start])
            actual = "".join(lines[hunk.before_start : hunk.before_end])
            if _text_sha256(actual) != hunk.before_sha256:
                raise ChangeSetConflictError(
                    f"hunk base no longer matches: {hunk.hunk_id}"
                )
            if hunk.decision is HunkDecision.ACCEPTED:
                output.append(hunk.after_text)
            elif hunk.decision is HunkDecision.REJECTED:
                output.append(actual)
            else:
                raise ChangeSetStateError(
                    f"pending hunk cannot be applied: {hunk.hunk_id}"
                )
            cursor = hunk.before_end
        output.extend(lines[cursor:])
        return "".join(output)

    @staticmethod
    def _validate_existing_parent_chain(root: Path, leaf: Path) -> None:
        resolved_root = root.resolve(strict=True)
        try:
            relative = leaf.relative_to(root)
        except ValueError as exc:
            raise ChangeSetConflictError("write target escaped the project root") from exc
        current = root
        if _is_link_or_reparse(current):
            raise ChangeSetConflictError("project root became a link or reparse point")
        for part in relative.parts:
            current = current / part
            if not current.exists() or not current.is_dir():
                raise ChangeSetConflictError(
                    f"write target parent is not a directory: {current}"
                )
            if _is_link_or_reparse(current):
                raise ChangeSetConflictError(
                    f"write target parent is a link or reparse point: {current}"
                )
            if not _is_within(current.resolve(strict=True), resolved_root):
                raise ChangeSetConflictError("write target parent escaped the project root")

    @staticmethod
    def _write_temporary_sibling(target: Path, content: bytes) -> Path:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{target.name}.zhimeng-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    @staticmethod
    def _apply_prepared(item: _PreparedWrite) -> None:
        ChangeSetService._validate_existing_parent_chain(item.root, item.target.parent)
        if item.before_bytes is None:
            if os.path.lexists(item.target):
                raise ChangeSetConflictError(
                    f"create target appeared during writeback: {item.change.relative_path}"
                )
        else:
            try:
                if _is_link_or_reparse(item.target):
                    raise ChangeSetConflictError(
                        f"write target became a link: {item.change.relative_path}"
                    )
                if int(getattr(item.target.lstat(), "st_nlink", 1)) > 1:
                    raise ChangeSetConflictError(
                        f"write target became hardlinked: {item.change.relative_path}"
                    )
                current = item.target.read_bytes()
            except OSError as exc:
                raise ChangeSetConflictError(
                    f"write target became unreadable: {item.change.relative_path}"
                ) from exc
            if _sha256(current) != _sha256(item.before_bytes):
                raise ChangeSetConflictError(
                    f"write target changed during writeback: {item.change.relative_path}"
                )
        if item.after_bytes is None:
            if item.tombstone is None:
                raise ChangeSetWritebackError("delete operation has no tombstone")
            os.replace(item.target, item.tombstone)
        else:
            if item.temporary is None:
                raise ChangeSetWritebackError("write operation has no temporary file")
            os.replace(item.temporary, item.target)
            item.temporary = None
        item.applied = True

    @staticmethod
    def _restore_prepared(items: list[_PreparedWrite]) -> str | None:
        errors: list[str] = []
        for item in reversed(items):
            try:
                if item.applied:
                    current = ChangeSetService._read_recovery_file(
                        item.target,
                        item.root,
                    )
                    current_hash = None if current is None else _sha256(current)
                    before_hash = (
                        None
                        if item.before_bytes is None
                        else _sha256(item.before_bytes)
                    )
                    after_hash = (
                        None
                        if item.after_bytes is None
                        else _sha256(item.after_bytes)
                    )
                    if current_hash != before_hash:
                        if current_hash != after_hash:
                            raise ChangeSetConflictError(
                                "write target changed before compensation"
                            )
                        if item.before_bytes is None:
                            item.target.unlink(missing_ok=True)
                        elif (
                            item.tombstone is not None
                            and os.path.lexists(item.tombstone)
                        ):
                            tombstone = ChangeSetService._read_recovery_file(
                                item.tombstone,
                                item.root,
                            )
                            if tombstone is None or _sha256(tombstone) != before_hash:
                                raise ChangeSetConflictError(
                                    "delete tombstone changed before compensation"
                                )
                            os.replace(item.tombstone, item.target)
                        else:
                            temporary = ChangeSetService._write_temporary_sibling(
                                item.target,
                                item.before_bytes,
                            )
                            os.replace(temporary, item.target)
                    item.applied = False
                if item.temporary is not None and os.path.lexists(item.temporary):
                    temporary_bytes = ChangeSetService._read_recovery_file(
                        item.temporary,
                        item.root,
                    )
                    expected_temporary_hash = (
                        None
                        if item.after_bytes is None
                        else _sha256(item.after_bytes)
                    )
                    if (
                        temporary_bytes is None
                        or _sha256(temporary_bytes) != expected_temporary_hash
                    ):
                        raise ChangeSetConflictError(
                            "temporary write changed before compensation"
                        )
                    item.temporary.unlink()
                if item.tombstone is not None and os.path.lexists(item.tombstone):
                    tombstone_bytes = ChangeSetService._read_recovery_file(
                        item.tombstone,
                        item.root,
                    )
                    expected_tombstone_hash = (
                        None
                        if item.before_bytes is None
                        else _sha256(item.before_bytes)
                    )
                    if (
                        tombstone_bytes is None
                        or _sha256(tombstone_bytes) != expected_tombstone_hash
                    ):
                        raise ChangeSetConflictError(
                            "delete tombstone changed before compensation"
                        )
                    item.tombstone.unlink()
            except Exception as exc:  # Best effort must report every rollback failure.
                errors.append(f"{item.change.relative_path}: {exc}")
        return "; ".join(errors) if errors else None

    @staticmethod
    def _finalize_prepared(items: list[_PreparedWrite]) -> None:
        for item in items:
            if item.temporary is not None:
                item.temporary.unlink(missing_ok=True)
            if item.tombstone is not None:
                item.tombstone.unlink(missing_ok=True)

    @staticmethod
    def _remove_empty_directories(directories: list[Path]) -> None:
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _remove_recorded_directories(root: Path, relative_paths: list[str]) -> None:
        if not relative_paths:
            return
        directories: list[Path] = []
        for raw in relative_paths:
            relative = PurePosixPath(raw)
            if relative.is_absolute() or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                raise ChangeSetWritebackError(
                    f"writeback recorded an unsafe directory: {raw}"
                )
            directory = root / Path(relative.as_posix())
            if not _is_within(directory, root):
                raise ChangeSetWritebackError(
                    f"writeback directory escaped the project root: {raw}"
                )
            if directory.exists():
                if not directory.is_dir() or _is_link_or_reparse(directory):
                    raise ChangeSetConflictError(
                        f"created directory changed before rollback: {raw}"
                    )
                directories.append(directory)

        recorded = {directory.resolve(strict=True) for directory in directories}
        for directory in sorted(
            directories,
            key=lambda item: len(item.relative_to(root).parts),
            reverse=True,
        ):
            unexpected = [
                child
                for child in directory.iterdir()
                if child.resolve(strict=True) not in recorded
            ]
            if unexpected:
                raise ChangeSetConflictError(
                    f"created directory is no longer empty: {directory.relative_to(root)}"
                )
        for directory in sorted(
            directories,
            key=lambda item: len(item.relative_to(root).parts),
            reverse=True,
        ):
            directory.rmdir()

    @staticmethod
    def _restore_recorded_directories(root: Path, relative_paths: list[str]) -> None:
        directories: list[Path] = []
        for raw in relative_paths:
            directory = ChangeSetService._recovery_project_path(root, raw)
            if os.path.lexists(directory):
                if not directory.exists() or not directory.is_dir() or _is_link_or_reparse(directory):
                    raise ChangeSetConflictError(
                        f"created directory changed during rollback recovery: {raw}"
                    )
            directories.append(directory)
        for directory in sorted(
            directories,
            key=lambda item: len(item.relative_to(root).parts),
        ):
            if os.path.lexists(directory):
                continue
            ChangeSetService._validate_existing_parent_chain(root, directory.parent)
            directory.mkdir()
            if _is_link_or_reparse(directory):
                raise ChangeSetConflictError(
                    "rollback recovery created an unsafe directory"
                )


    def get_writeback_evidence(self, change_set_id: str) -> WritebackEvidenceResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_writeback_evidence(
            artifact_root / "writeback-evidence-receipt.json"
        )
        history = self._read_writeback_evidence_history(
            artifact_root / "writeback-evidence-history.json"
        )
        return WritebackEvidenceResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def get_nlcl_writeback_sync(self, change_set_id: str) -> NlclWritebackSyncResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_nlcl_writeback_sync(
            artifact_root / "nlcl-writeback-sync-receipt.json"
        )
        history = self._read_nlcl_writeback_sync_history(
            artifact_root / "nlcl-writeback-sync-history.json"
        )
        return NlclWritebackSyncResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def sync_nlcl_writeback_evidence(
        self,
        change_set_id: str,
        request: NlclWritebackSyncRequest,
    ) -> NlclWritebackSyncResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "NLCL writeback sync requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        evidence = self._read_writeback_evidence(
            artifact_root / "writeback-evidence-receipt.json"
        )
        if evidence is None:
            raise ChangeSetStateError(
                "writeback evidence receipt is required before NLCL writeback sync"
            )
        if evidence.receipt_hash != request.expected_writeback_evidence_receipt_hash:
            raise ChangeSetConflictError(
                "writeback evidence receipt hash does not match the expected value"
            )
        if evidence.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "writeback evidence receipt is not bound to this ChangeSet"
            )
        if evidence.outcome != (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        ):
            raise ChangeSetStateError(
                "writeback evidence outcome does not match ChangeSet status"
            )

        existing = self._read_nlcl_writeback_sync(
            artifact_root / "nlcl-writeback-sync-receipt.json"
        )
        if (
            existing is not None
            and existing.writeback_evidence_receipt_hash == evidence.receipt_hash
            and existing.outcome == evidence.outcome
            and existing.result_revision == evidence.result_revision
            and existing.result_fingerprint == evidence.result_fingerprint
        ):
            history = self._read_nlcl_writeback_sync_history(
                artifact_root / "nlcl-writeback-sync-history.json"
            )
            return NlclWritebackSyncResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        affected_paths = sorted(
            {
                change.relative_path
                for change in record.changes
                if any(hunk.decision is HunkDecision.ACCEPTED for hunk in change.hunks)
            },
            key=lambda value: value.casefold(),
        )
        chapter_paths = [
            path
            for path in affected_paths
            if path.startswith("05_正文内容/") and path.endswith(".md")
        ]
        material = {
            "schema": "zhimeng.nlcl-writeback-sync-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "writeback_evidence_receipt_id": evidence.receipt_id,
            "writeback_evidence_receipt_hash": evidence.receipt_hash,
            "outcome": evidence.outcome,
            "result_revision": evidence.result_revision,
            "result_fingerprint": evidence.result_fingerprint,
            "affected_relative_paths": affected_paths,
            "chapter_relative_paths": chapter_paths,
            "context_pack_id": evidence.context_pack_id,
            "context_pack_hash": evidence.context_pack_hash,
            "route_receipt_id": evidence.route_receipt_id,
            "route_receipt_hash": evidence.route_receipt_hash,
            "knowledge_fingerprint": evidence.knowledge_fingerprint,
            "selected_card_ids": list(evidence.selected_card_ids),
            "source_run_id": evidence.source_run_id,
            "sync_scope": "product_native_evidence_continuity",
            "original_nlcl_chapter_ops_usage_synced": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-writeback-sync-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclWritebackSyncReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-writeback-sync-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-writeback-sync-history.json"
        history = self._read_nlcl_writeback_sync_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclWritebackSyncResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _require_denied_formal_draft_authorization(
        self,
        *,
        project_id: str,
        change_set_id: str | None = None,
        context_pack_id: str | None = None,
        run_id: str | None = None,
    ) -> FormalDraftAuthorizationReceipt:
        """Evaluate formal-draft rights and fail closed unless denied.

        Author-reviewed ChangeSet apply/rollback is not formal prose grant.
        This keeps the write path bound to an auditable denial receipt.
        """

        service = (
            self.formal_draft_authorization_service
            or FormalDraftAuthorizationService(self.store)
        )
        try:
            response = service.evaluate(
                project_id,
                FormalDraftEvaluateRequest(
                    context_pack_id=context_pack_id,
                    run_id=run_id,
                    change_set_id=change_set_id,
                ),
            )
        except FormalDraftAuthorizationError as exc:
            raise ChangeSetWritebackError(
                f"formal draft authorization evaluation failed: {exc}"
            ) from exc
        receipt = response.receipt
        if (
            receipt.decision is not FormalDraftDecision.DENIED
            or receipt.formal_draft_allowed is not False
            or receipt.writes_project_truth is not False
        ):
            raise ChangeSetWritebackError(
                "formal draft authorization did not deny formal prose write rights"
            )
        return receipt

    def _record_writeback_evidence(
        self,
        record: ChangeSetResponse,
        manifest: WritebackManifest,
        *,
        artifact_root: Path,
        formal_draft_authorization: FormalDraftAuthorizationReceipt | None = None,
    ) -> WritebackEvidenceReceipt:
        context_pack_id: str | None = None
        context_pack_hash: str | None = None
        knowledge_fingerprint: str | None = None
        route_receipt_id: str | None = None
        route_receipt_hash: str | None = None
        selected_card_ids: list[str] = []
        try:
            context_pack_id, context_pack_hash, knowledge_fingerprint = (
                self._context_binding(record)
            )
        except ChangeSetGateStaleError:
            provenance = self.store.get_change_set_provenance(record.change_set_id)
            if provenance is not None and provenance.get("context_pack_id") is not None:
                context_pack_id = str(provenance["context_pack_id"])
        if context_pack_id is not None:
            with self.store.connection() as connection:
                route = connection.execute(
                    """
                    SELECT id, receipt_hash, payload_json
                    FROM nlcl_route_receipt
                    WHERE context_pack_id = ?
                    """,
                    (context_pack_id,),
                ).fetchone()
            if route is not None:
                route_receipt_id = str(route["id"])
                route_receipt_hash = str(route["receipt_hash"])
                try:
                    payload = json.loads(str(route["payload_json"]))
                    cards = (
                        payload.get("selected_cards")
                        if isinstance(payload, dict)
                        else None
                    )
                    if isinstance(cards, list):
                        for card in cards:
                            if (
                                isinstance(card, dict)
                                and isinstance(card.get("card_id"), str)
                            ):
                                selected_card_ids.append(str(card["card_id"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    selected_card_ids = []
        # Preserve stable order while de-duplicating.
        selected_card_ids = list(dict.fromkeys(selected_card_ids))
        if formal_draft_authorization is None:
            formal_draft_authorization = (
                self._require_denied_formal_draft_authorization(
                    project_id=record.project_id,
                    change_set_id=record.change_set_id,
                    context_pack_id=context_pack_id,
                    run_id=record.source_run_id,
                )
            )
        elif (
            formal_draft_authorization.decision is not FormalDraftDecision.DENIED
            or formal_draft_authorization.formal_draft_allowed is not False
            or formal_draft_authorization.writes_project_truth is not False
        ):
            raise ChangeSetWritebackError(
                "formal draft authorization did not deny formal prose write rights"
            )
        if formal_draft_authorization.project_id != record.project_id:
            raise ChangeSetWritebackError(
                "formal draft authorization project does not match the change set"
            )
        if (
            formal_draft_authorization.change_set_id is not None
            and formal_draft_authorization.change_set_id != record.change_set_id
        ):
            raise ChangeSetWritebackError(
                "formal draft authorization change set does not match writeback"
            )
        _atomic_write_json(
            artifact_root / "formal-draft-authorization-receipt.json",
            formal_draft_authorization.model_dump(mode="json", by_alias=True),
        )
        manifest_hash = canonical_json_hash(
            manifest.model_dump(mode="json", by_alias=True)
        )
        material = {
            "schema": "zhimeng.writeback-evidence-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "outcome": manifest.outcome,
            "writeback_manifest_hash": manifest_hash,
            "base_revision": manifest.base_revision,
            "base_fingerprint": manifest.base_fingerprint,
            "result_revision": manifest.result_revision,
            "result_fingerprint": manifest.result_fingerprint,
            "context_pack_id": context_pack_id,
            "context_pack_hash": context_pack_hash,
            "route_receipt_id": route_receipt_id,
            "route_receipt_hash": route_receipt_hash,
            "knowledge_fingerprint": knowledge_fingerprint,
            "selected_card_ids": selected_card_ids,
            "source_run_id": record.source_run_id,
            "formal_draft_authorization_receipt_id": (
                formal_draft_authorization.receipt_id
            ),
            "formal_draft_authorization_receipt_hash": (
                formal_draft_authorization.receipt_hash
            ),
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"writeback-evidence-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = WritebackEvidenceReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(artifact_root / "writeback-evidence-receipt.json", payload)
        history_path = artifact_root / "writeback-evidence-history.json"
        history = self._read_writeback_evidence_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return receipt

    def _read_writeback_evidence(self, path: Path) -> WritebackEvidenceReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return WritebackEvidenceReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_writeback_evidence_history(
        self,
        path: Path,
    ) -> list[WritebackEvidenceReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[WritebackEvidenceReceipt] = []
        for item in payload:
            try:
                history.append(WritebackEvidenceReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history


    def get_nlcl_call_usage_continuity(
        self,
        change_set_id: str,
    ) -> NlclCallUsageContinuityResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_nlcl_call_usage_continuity(
            artifact_root / "nlcl-call-usage-continuity-receipt.json"
        )
        history = self._read_nlcl_call_usage_continuity_history(
            artifact_root / "nlcl-call-usage-continuity-history.json"
        )
        return NlclCallUsageContinuityResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def record_nlcl_call_usage_continuity(
        self,
        change_set_id: str,
        request: NlclCallUsageContinuityRequest,
    ) -> NlclCallUsageContinuityResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "NLCL call_usage continuity requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        sync = self._read_nlcl_writeback_sync(
            artifact_root / "nlcl-writeback-sync-receipt.json"
        )
        if sync is None:
            raise ChangeSetStateError(
                "NLCL writeback sync receipt is required before call_usage continuity"
            )
        if sync.receipt_hash != request.expected_nlcl_writeback_sync_receipt_hash:
            raise ChangeSetConflictError(
                "NLCL writeback sync receipt hash does not match the expected value"
            )
        if sync.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL writeback sync receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        if sync.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL writeback sync outcome does not match ChangeSet status"
            )

        existing = self._read_nlcl_call_usage_continuity(
            artifact_root / "nlcl-call-usage-continuity-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_writeback_sync_receipt_hash == sync.receipt_hash
            and existing.outcome == sync.outcome
            and existing.result_revision == sync.result_revision
            and existing.result_fingerprint == sync.result_fingerprint
        ):
            history = self._read_nlcl_call_usage_continuity_history(
                artifact_root / "nlcl-call-usage-continuity-history.json"
            )
            return NlclCallUsageContinuityResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        selected_card_ids = list(sync.selected_card_ids)
        chapter_paths = list(sync.chapter_relative_paths)
        # Product-native rule: selected cards bound into an applied chapter write
        # are recorded as used. Rollback voids the used set for this outcome.
        # unused/missed stay empty because this path does not invent original
        # kb-manager mandatory-usage-closure audits.
        if sync.outcome == "applied" and chapter_paths:
            used_card_ids = list(selected_card_ids)
        else:
            used_card_ids = []
        unused_card_ids: list[str] = []
        missed_card_ids: list[str] = []

        material = {
            "schema": "zhimeng.nlcl-call-usage-continuity-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_writeback_sync_receipt_id": sync.receipt_id,
            "nlcl_writeback_sync_receipt_hash": sync.receipt_hash,
            "writeback_evidence_receipt_id": sync.writeback_evidence_receipt_id,
            "writeback_evidence_receipt_hash": sync.writeback_evidence_receipt_hash,
            "outcome": sync.outcome,
            "result_revision": sync.result_revision,
            "result_fingerprint": sync.result_fingerprint,
            "chapter_relative_paths": chapter_paths,
            "context_pack_id": sync.context_pack_id,
            "context_pack_hash": sync.context_pack_hash,
            "route_receipt_id": sync.route_receipt_id,
            "route_receipt_hash": sync.route_receipt_hash,
            "knowledge_fingerprint": sync.knowledge_fingerprint,
            "selected_card_ids": selected_card_ids,
            "used_card_ids": used_card_ids,
            "unused_card_ids": unused_card_ids,
            "missed_card_ids": missed_card_ids,
            "source_run_id": sync.source_run_id,
            "usage_scope": "product_native_selected_card_continuity",
            "record_mode": "product_native_writeback_bound",
            "original_nlcl_call_usage_recorded": False,
            "original_nlcl_chapter_ops_usage_synced": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-call-usage-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclCallUsageContinuityReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-call-usage-continuity-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-call-usage-continuity-history.json"
        history = self._read_nlcl_call_usage_continuity_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclCallUsageContinuityResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )


    def get_original_nlcl_call_usage_export(
        self,
        change_set_id: str,
    ) -> NlclOriginalCallUsageExportResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_call_usage_export(
            artifact_root / "nlcl-original-call-usage-export-receipt.json"
        )
        history = self._read_original_nlcl_call_usage_export_history(
            artifact_root / "nlcl-original-call-usage-export-history.json"
        )
        return NlclOriginalCallUsageExportResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_call_usage(
        self,
        change_set_id: str,
        request: NlclOriginalCallUsageExportRequest,
    ) -> NlclOriginalCallUsageExportResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL call_usage export requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        continuity = self._read_nlcl_call_usage_continuity(
            artifact_root / "nlcl-call-usage-continuity-receipt.json"
        )
        if continuity is None:
            raise ChangeSetStateError(
                "product-native NLCL call_usage continuity receipt is required before original export"
            )
        if (
            continuity.receipt_hash
            != request.expected_nlcl_call_usage_continuity_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL call_usage continuity receipt hash does not match the expected value"
            )
        if continuity.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL call_usage continuity receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        if continuity.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL call_usage continuity outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_call_usage_export(
            artifact_root / "nlcl-original-call-usage-export-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_call_usage_continuity_receipt_hash
            == continuity.receipt_hash
            and existing.outcome == continuity.outcome
        ):
            history = self._read_original_nlcl_call_usage_export_history(
                artifact_root / "nlcl-original-call-usage-export-history.json"
            )
            return NlclOriginalCallUsageExportResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        export_relative = "07_运营数据/nlcl_feedback/call_usage.jsonl"
        export_path = project_root.joinpath(*PurePosixPath(export_relative).parts)
        if not _is_within(export_path.parent, project_root):
            raise ChangeSetStateError("export path escapes project root")
        export_path.parent.mkdir(parents=True, exist_ok=True)

        chapter_id = self._chapter_id_from_paths(list(continuity.chapter_relative_paths))
        canonical_chapter_key = self._canonical_chapter_key(chapter_id)
        row = {
            "bridge_schema": "zhimeng.original-nlcl-call-usage-bridge-row.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": canonical_chapter_key,
            "recorded_at": _now().isoformat().replace("+00:00", "Z"),
            "used": list(continuity.used_card_ids),
            "unused": list(continuity.unused_card_ids),
            "missed": list(continuity.missed_card_ids),
            "record_mode": "product_bridge_from_continuity",
            "source_run_id": continuity.source_run_id,
            "change_set_id": record.change_set_id,
            "product_continuity_receipt_id": continuity.receipt_id,
            "product_continuity_receipt_hash": continuity.receipt_hash,
            "outcome": continuity.outcome,
            "chapter_relative_paths": list(continuity.chapter_relative_paths),
            "formal_draft_allowed": False,
        }
        row_text = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        row_bytes = (row_text + "\n").encode("utf-8")
        row_sha = hashlib.sha256(row_bytes).hexdigest()

        # Append-only write with exclusive create/open.
        if export_path.exists():
            value = export_path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ChangeSetStateError("call_usage.jsonl must be a regular file")
            if int(getattr(value, "st_nlink", 1)) > 1:
                raise ChangeSetStateError("call_usage.jsonl cannot be a hardlink")
            byte_offset = int(value.st_size)
            with export_path.open("ab") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            byte_offset = 0
            with export_path.open("xb") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        export_sha = hashlib.sha256(export_path.read_bytes()).hexdigest()

        material = {
            "schema": "zhimeng.nlcl-original-call-usage-export-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_call_usage_continuity_receipt_id": continuity.receipt_id,
            "nlcl_call_usage_continuity_receipt_hash": continuity.receipt_hash,
            "outcome": continuity.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": canonical_chapter_key,
            "chapter_relative_paths": list(continuity.chapter_relative_paths),
            "used_card_ids": list(continuity.used_card_ids),
            "unused_card_ids": list(continuity.unused_card_ids),
            "missed_card_ids": list(continuity.missed_card_ids),
            "source_run_id": continuity.source_run_id,
            "export_relative_path": export_relative,
            "export_absolute_path": str(export_path),
            "export_file_sha256": export_sha,
            "export_byte_offset": byte_offset,
            "export_row_sha256": row_sha,
            "record_mode": "product_bridge_from_continuity",
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-call-usage-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalCallUsageExportReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-call-usage-export-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-call-usage-export-history.json"
        history = self._read_original_nlcl_call_usage_export_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalCallUsageExportResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )


    def get_original_nlcl_chapter_ops_usage_export(
        self,
        change_set_id: str,
    ) -> NlclOriginalChapterOpsUsageExportResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_chapter_ops_usage_export(
            artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json"
        )
        history = self._read_original_nlcl_chapter_ops_usage_export_history(
            artifact_root / "nlcl-original-chapter-ops-usage-export-history.json"
        )
        return NlclOriginalChapterOpsUsageExportResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_chapter_ops_usage(
        self,
        change_set_id: str,
        request: NlclOriginalChapterOpsUsageExportRequest,
    ) -> NlclOriginalChapterOpsUsageExportResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL chapter/ops usage export requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        call_usage_export = self._read_original_nlcl_call_usage_export(
            artifact_root / "nlcl-original-call-usage-export-receipt.json"
        )
        if call_usage_export is None:
            raise ChangeSetStateError(
                "original-compatible NLCL call_usage export receipt is required before chapter/ops usage export"
            )
        if (
            call_usage_export.receipt_hash
            != request.expected_nlcl_original_call_usage_export_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original call_usage export receipt hash does not match the expected value"
            )
        if call_usage_export.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original call_usage export receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        if call_usage_export.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original call_usage export outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_chapter_ops_usage_export(
            artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_call_usage_export_receipt_hash
            == call_usage_export.receipt_hash
            and existing.outcome == call_usage_export.outcome
        ):
            history = self._read_original_nlcl_chapter_ops_usage_export_history(
                artifact_root / "nlcl-original-chapter-ops-usage-export-history.json"
            )
            return NlclOriginalChapterOpsUsageExportResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        export_relative = "07_运营数据/nlcl_feedback/chapter_ops_usage.jsonl"
        export_path = project_root.joinpath(*PurePosixPath(export_relative).parts)
        if not _is_within(export_path.parent, project_root):
            raise ChangeSetStateError("export path escapes project root")
        export_path.parent.mkdir(parents=True, exist_ok=True)

        row = {
            "bridge_schema": "zhimeng.original-nlcl-chapter-ops-usage-bridge-row.v1",
            "chapter_id": call_usage_export.chapter_id,
            "canonical_chapter_key": call_usage_export.canonical_chapter_key,
            "recorded_at": _now().isoformat().replace("+00:00", "Z"),
            "used": list(call_usage_export.used_card_ids),
            "unused": list(call_usage_export.unused_card_ids),
            "missed": list(call_usage_export.missed_card_ids),
            "record_mode": "product_bridge_chapter_ops_from_call_usage_export",
            "source_run_id": call_usage_export.source_run_id,
            "change_set_id": record.change_set_id,
            "product_call_usage_export_receipt_id": call_usage_export.receipt_id,
            "product_call_usage_export_receipt_hash": call_usage_export.receipt_hash,
            "product_continuity_receipt_id": call_usage_export.nlcl_call_usage_continuity_receipt_id,
            "product_continuity_receipt_hash": call_usage_export.nlcl_call_usage_continuity_receipt_hash,
            "outcome": call_usage_export.outcome,
            "chapter_relative_paths": list(call_usage_export.chapter_relative_paths),
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": True,
            "formal_draft_allowed": False,
            "novel_state_synced": False,
        }
        row_text = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        row_bytes = (row_text + "\n").encode("utf-8")
        row_sha = hashlib.sha256(row_bytes).hexdigest()

        if export_path.exists():
            value = export_path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ChangeSetStateError("chapter_ops_usage.jsonl must be a regular file")
            if int(getattr(value, "st_nlink", 1)) > 1:
                raise ChangeSetStateError("chapter_ops_usage.jsonl cannot be a hardlink")
            byte_offset = int(value.st_size)
            with export_path.open("ab") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            byte_offset = 0
            with export_path.open("xb") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        export_sha = hashlib.sha256(export_path.read_bytes()).hexdigest()

        material = {
            "schema": "zhimeng.nlcl-original-chapter-ops-usage-export-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_call_usage_export_receipt_id": call_usage_export.receipt_id,
            "nlcl_original_call_usage_export_receipt_hash": call_usage_export.receipt_hash,
            "nlcl_call_usage_continuity_receipt_id": call_usage_export.nlcl_call_usage_continuity_receipt_id,
            "nlcl_call_usage_continuity_receipt_hash": call_usage_export.nlcl_call_usage_continuity_receipt_hash,
            "outcome": call_usage_export.outcome,
            "chapter_id": call_usage_export.chapter_id,
            "canonical_chapter_key": call_usage_export.canonical_chapter_key,
            "chapter_relative_paths": list(call_usage_export.chapter_relative_paths),
            "used_card_ids": list(call_usage_export.used_card_ids),
            "unused_card_ids": list(call_usage_export.unused_card_ids),
            "missed_card_ids": list(call_usage_export.missed_card_ids),
            "source_run_id": call_usage_export.source_run_id,
            "export_relative_path": export_relative,
            "export_absolute_path": str(export_path),
            "export_file_sha256": export_sha,
            "export_byte_offset": byte_offset,
            "export_row_sha256": row_sha,
            "record_mode": "product_bridge_chapter_ops_from_call_usage_export",
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-chapter-ops-usage-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalChapterOpsUsageExportReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json",
            payload,
        )
        history_path = (
            artifact_root / "nlcl-original-chapter-ops-usage-export-history.json"
        )
        history = self._read_original_nlcl_chapter_ops_usage_export_history(
            history_path
        )
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalChapterOpsUsageExportResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_chapter_ops_usage_export(
        self,
        path: Path,
    ) -> NlclOriginalChapterOpsUsageExportReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalChapterOpsUsageExportReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_chapter_ops_usage_export_history(
        self,
        path: Path,
    ) -> list[NlclOriginalChapterOpsUsageExportReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalChapterOpsUsageExportReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalChapterOpsUsageExportReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history




    def get_original_nlcl_novel_state_bridge(
        self,
        change_set_id: str,
    ) -> NlclOriginalNovelStateBridgeResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_novel_state_bridge(
            artifact_root / "nlcl-original-novel-state-bridge-receipt.json"
        )
        history = self._read_original_nlcl_novel_state_bridge_history(
            artifact_root / "nlcl-original-novel-state-bridge-history.json"
        )
        return NlclOriginalNovelStateBridgeResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_novel_state_bridge(
        self,
        change_set_id: str,
        request: NlclOriginalNovelStateBridgeRequest,
    ) -> NlclOriginalNovelStateBridgeResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL novel-state bridge requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        chapter_ops = self._read_original_nlcl_chapter_ops_usage_export(
            artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json"
        )
        if chapter_ops is None:
            raise ChangeSetStateError(
                "original-compatible NLCL chapter/ops usage export receipt is required before novel-state bridge"
            )
        if (
            chapter_ops.receipt_hash
            != request.expected_nlcl_original_chapter_ops_usage_export_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original chapter/ops usage export receipt hash does not match the expected value"
            )
        if chapter_ops.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original chapter/ops usage export receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        if chapter_ops.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original chapter/ops usage export outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_novel_state_bridge(
            artifact_root / "nlcl-original-novel-state-bridge-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_chapter_ops_usage_export_receipt_hash
            == chapter_ops.receipt_hash
            and existing.outcome == chapter_ops.outcome
        ):
            history = self._read_original_nlcl_novel_state_bridge_history(
                artifact_root / "nlcl-original-novel-state-bridge-history.json"
            )
            return NlclOriginalNovelStateBridgeResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        state_refs = self._inventory_novel_state_refs(project_root)
        character_refs = [item for item in state_refs if item.domain == "character"]
        plot_refs = [item for item in state_refs if item.domain == "plot"]
        foreshadow_refs = [
            item for item in state_refs if item.domain == "foreshadowing"
        ]
        project_status_refs = [
            item for item in state_refs if item.domain == "project_status"
        ]

        export_relative = "07_运营数据/nlcl_feedback/novel_state_bridge.jsonl"
        export_path = project_root.joinpath(*PurePosixPath(export_relative).parts)
        if not _is_within(export_path.parent, project_root):
            raise ChangeSetStateError("export path escapes project root")
        export_path.parent.mkdir(parents=True, exist_ok=True)

        row = {
            "bridge_schema": "zhimeng.original-nlcl-novel-state-bridge-row.v1",
            "chapter_id": chapter_ops.chapter_id,
            "canonical_chapter_key": chapter_ops.canonical_chapter_key,
            "recorded_at": _now().isoformat().replace("+00:00", "Z"),
            "record_mode": "product_bridge_novel_state_inventory_from_chapter_ops",
            "source_run_id": chapter_ops.source_run_id,
            "change_set_id": record.change_set_id,
            "product_chapter_ops_export_receipt_id": chapter_ops.receipt_id,
            "product_chapter_ops_export_receipt_hash": chapter_ops.receipt_hash,
            "product_call_usage_export_receipt_id": chapter_ops.nlcl_original_call_usage_export_receipt_id,
            "product_call_usage_export_receipt_hash": chapter_ops.nlcl_original_call_usage_export_receipt_hash,
            "outcome": chapter_ops.outcome,
            "chapter_relative_paths": list(chapter_ops.chapter_relative_paths),
            "character_state_refs": [
                item.model_dump(mode="json") for item in character_refs
            ],
            "plot_state_refs": [item.model_dump(mode="json") for item in plot_refs],
            "foreshadowing_state_refs": [
                item.model_dump(mode="json") for item in foreshadow_refs
            ],
            "project_status_refs": [
                item.model_dump(mode="json") for item in project_status_refs
            ],
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": True,
            "original_nlcl_novel_state_bridge_recorded": True,
            "formal_draft_allowed": False,
            "novel_state_synced": False,
        }
        row_text = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        row_bytes = (row_text + "\n").encode("utf-8")
        row_sha = hashlib.sha256(row_bytes).hexdigest()

        if export_path.exists():
            value = export_path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ChangeSetStateError("novel_state_bridge.jsonl must be a regular file")
            if int(getattr(value, "st_nlink", 1)) > 1:
                raise ChangeSetStateError("novel_state_bridge.jsonl cannot be a hardlink")
            byte_offset = int(value.st_size)
            with export_path.open("ab") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            byte_offset = 0
            with export_path.open("xb") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        export_sha = hashlib.sha256(export_path.read_bytes()).hexdigest()

        material = {
            "schema": "zhimeng.nlcl-original-novel-state-bridge-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_chapter_ops_usage_export_receipt_id": chapter_ops.receipt_id,
            "nlcl_original_chapter_ops_usage_export_receipt_hash": chapter_ops.receipt_hash,
            "nlcl_original_call_usage_export_receipt_id": chapter_ops.nlcl_original_call_usage_export_receipt_id,
            "nlcl_original_call_usage_export_receipt_hash": chapter_ops.nlcl_original_call_usage_export_receipt_hash,
            "outcome": chapter_ops.outcome,
            "chapter_id": chapter_ops.chapter_id,
            "canonical_chapter_key": chapter_ops.canonical_chapter_key,
            "chapter_relative_paths": list(chapter_ops.chapter_relative_paths),
            "character_state_refs": [
                item.model_dump(mode="json") for item in character_refs
            ],
            "plot_state_refs": [item.model_dump(mode="json") for item in plot_refs],
            "foreshadowing_state_refs": [
                item.model_dump(mode="json") for item in foreshadow_refs
            ],
            "project_status_refs": [
                item.model_dump(mode="json") for item in project_status_refs
            ],
            "source_run_id": chapter_ops.source_run_id,
            "export_relative_path": export_relative,
            "export_absolute_path": str(export_path),
            "export_file_sha256": export_sha,
            "export_byte_offset": byte_offset,
            "export_row_sha256": row_sha,
            "record_mode": "product_bridge_novel_state_inventory_from_chapter_ops",
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": True,
            "original_nlcl_novel_state_bridge_recorded": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-novel-state-bridge-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalNovelStateBridgeReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-novel-state-bridge-receipt.json",
            payload,
        )
        history_path = (
            artifact_root / "nlcl-original-novel-state-bridge-history.json"
        )
        history = self._read_original_nlcl_novel_state_bridge_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalNovelStateBridgeResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _inventory_novel_state_refs(self, project_root: Path) -> list[NlclNovelStateFileRef]:
        try:
            inspection = self.reader.inspect(project_root)
        except Exception as exc:  # ProjectReaderError and friends
            raise ChangeSetStateError(f"project inspection failed for novel-state bridge: {exc}") from exc
        files = inspection.get("files")
        if not isinstance(files, list):
            return []
        refs: list[NlclNovelStateFileRef] = []
        for raw in files:
            if not isinstance(raw, dict):
                continue
            relative_path = str(raw.get("relative_path") or "").strip().replace("\\", "/")
            source_hash = str(raw.get("sha256") or "").strip()
            if not relative_path or len(source_hash) != 64:
                continue
            domain = self._classify_novel_state_domain(relative_path)
            if domain is None:
                continue
            refs.append(
                NlclNovelStateFileRef(
                    relative_path=relative_path,
                    source_hash=source_hash,
                    domain=domain,
                )
            )
        refs.sort(key=lambda item: (item.domain, item.relative_path))
        # Keep deterministic bounded inventory.
        by_domain: dict[str, list[NlclNovelStateFileRef]] = {
            "character": [],
            "plot": [],
            "foreshadowing": [],
            "project_status": [],
        }
        for item in refs:
            bucket = by_domain[item.domain]
            if len(bucket) < 64:
                bucket.append(item)
        ordered: list[NlclNovelStateFileRef] = []
        for key in ("character", "plot", "foreshadowing", "project_status"):
            ordered.extend(by_domain[key])
        return ordered

    @staticmethod
    def _classify_novel_state_domain(
        relative_path: str,
    ) -> str | None:
        lowered = relative_path.casefold()
        name = PurePosixPath(relative_path).name.casefold()
        if (
            "project_status" in lowered
            or name in {"project_status.json", "project-status.json"}
            or lowered.endswith("/state/project_status.json")
        ):
            return "project_status"
        if any(
            marker in lowered
            for marker in (
                "03_",
                "人物",
                "角色",
                "character",
                "character_state",
            )
        ):
            return "character"
        if any(
            marker in lowered
            for marker in (
                "伏笔",
                "foreshadow",
                "foreshadowing",
                "foreshadowing_state",
            )
        ):
            return "foreshadowing"
        if any(
            marker in lowered
            for marker in (
                "04_",
                "剧情",
                "大纲",
                "plot",
                "plot_state",
            )
        ):
            return "plot"
        return None

    def _read_original_nlcl_novel_state_bridge(
        self,
        path: Path,
    ) -> NlclOriginalNovelStateBridgeReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalNovelStateBridgeReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_novel_state_bridge_history(
        self,
        path: Path,
    ) -> list[NlclOriginalNovelStateBridgeReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalNovelStateBridgeReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalNovelStateBridgeReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history



    def get_original_nlcl_novel_state_sync(
        self,
        change_set_id: str,
    ) -> NlclOriginalNovelStateSyncResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_novel_state_sync(
            artifact_root / "nlcl-original-novel-state-sync-receipt.json"
        )
        history = self._read_original_nlcl_novel_state_sync_history(
            artifact_root / "nlcl-original-novel-state-sync-history.json"
        )
        return NlclOriginalNovelStateSyncResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_novel_state_sync(
        self,
        change_set_id: str,
        request: NlclOriginalNovelStateSyncRequest,
    ) -> NlclOriginalNovelStateSyncResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL novel-state sync requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        bridge = self._read_original_nlcl_novel_state_bridge(
            artifact_root / "nlcl-original-novel-state-bridge-receipt.json"
        )
        if bridge is None:
            raise ChangeSetStateError(
                "original-compatible NLCL novel-state inventory bridge is required before state sync"
            )
        if (
            bridge.receipt_hash
            != request.expected_nlcl_original_novel_state_bridge_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original novel-state bridge receipt hash does not match the expected value"
            )
        if bridge.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original novel-state bridge receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        if bridge.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original novel-state bridge outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_novel_state_sync(
            artifact_root / "nlcl-original-novel-state-sync-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_novel_state_bridge_receipt_hash
            == bridge.receipt_hash
            and existing.outcome == bridge.outcome
        ):
            history = self._read_original_nlcl_novel_state_sync_history(
                artifact_root / "nlcl-original-novel-state-sync-history.json"
            )
            return NlclOriginalNovelStateSyncResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        state_root = project_root / "state"
        if state_root.exists() and not state_root.is_dir():
            raise ChangeSetStateError("project state path must be a directory")
        if state_root.exists() and _is_link_or_reparse(state_root):
            raise ChangeSetStateError("project state path cannot be a link or reparse point")
        state_root.mkdir(parents=True, exist_ok=True)

        ts = _now().isoformat().replace("+00:00", "Z")
        used_cards = []
        # pull used cards from chapter_ops receipt if available
        chapter_ops = self._read_original_nlcl_chapter_ops_usage_export(
            artifact_root / "nlcl-original-chapter-ops-usage-export-receipt.json"
        )
        if chapter_ops is not None:
            used_cards = list(chapter_ops.used_card_ids)

        written: list[NlclOriginalNovelStateWrittenFile] = []
        payloads = {
            "project_status": self._build_project_status_state(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "character": self._build_character_state(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "plot": self._build_plot_state(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "foreshadowing": self._build_foreshadowing_state(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "truth_character_matrix": self._build_truth_character_matrix(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "truth_foreshadow_lifecycle": self._build_truth_foreshadow_lifecycle(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
            "truth_info_boundary": self._build_truth_info_boundary(
                bridge=bridge,
                used_card_ids=used_cards,
                timestamp=ts,
            ),
        }
        path_map = {
            "project_status": state_root / "project_status.json",
            "character": state_root / "character_state.json",
            "plot": state_root / "plot_state.json",
            "foreshadowing": state_root / "foreshadowing_state.json",
            "truth_character_matrix": state_root / "truth_character_matrix.json",
            "truth_foreshadow_lifecycle": state_root / "truth_foreshadow_lifecycle.json",
            "truth_info_boundary": state_root / "truth_info_boundary.json",
        }
        relative_map = {
            "project_status": "state/project_status.json",
            "character": "state/character_state.json",
            "plot": "state/plot_state.json",
            "foreshadowing": "state/foreshadowing_state.json",
            "truth_character_matrix": "state/truth_character_matrix.json",
            "truth_foreshadow_lifecycle": "state/truth_foreshadow_lifecycle.json",
            "truth_info_boundary": "state/truth_info_boundary.json",
        }
        for domain, path in path_map.items():
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"state path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"state file must be a regular file: {path}")
            created = not path.exists()
            existing_payload: dict[str, object] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing_payload = loaded
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    existing_payload = {}
            merged = self._merge_state_payload(existing_payload, payloads[domain])
            _atomic_write_json(path, merged)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalNovelStateWrittenFile(
                    relative_path=relative_map[domain],
                    source_hash=digest,
                    domain=domain,  # type: ignore[arg-type]
                    created=created,
                )
            )

        written.sort(key=lambda item: item.domain)
        material = {
            "schema": "zhimeng.nlcl-original-novel-state-sync-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_novel_state_bridge_receipt_id": bridge.receipt_id,
            "nlcl_original_novel_state_bridge_receipt_hash": bridge.receipt_hash,
            "nlcl_original_chapter_ops_usage_export_receipt_id": (
                bridge.nlcl_original_chapter_ops_usage_export_receipt_id
            ),
            "nlcl_original_chapter_ops_usage_export_receipt_hash": (
                bridge.nlcl_original_chapter_ops_usage_export_receipt_hash
            ),
            "outcome": bridge.outcome,
            "chapter_id": bridge.chapter_id,
            "canonical_chapter_key": bridge.canonical_chapter_key,
            "chapter_relative_paths": list(bridge.chapter_relative_paths),
            "written_files": [item.model_dump(mode="json") for item in written],
            "state_root_relative_path": "state",
            "record_mode": "product_bridge_original_state_json_writeback",
            "original_nlcl_call_usage_recorded": True,
            "original_nlcl_chapter_ops_usage_synced": True,
            "original_nlcl_novel_state_bridge_recorded": True,
            "original_nlcl_novel_state_synced": True,
            "original_nlcl_truth_matrix_synced": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-novel-state-sync-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalNovelStateSyncReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-novel-state-sync-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-novel-state-sync-history.json"
        history = self._read_original_nlcl_novel_state_sync_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalNovelStateSyncResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    @staticmethod
    def _merge_state_payload(
        existing: dict[str, object],
        update: dict[str, object],
    ) -> dict[str, object]:
        merged = dict(existing)
        for key, value in update.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
                and key in {"_meta", "writeback_current", "current_task", "product_bridge"}
            ):
                nested = dict(merged[key])  # type: ignore[arg-type]
                nested.update(value)  # type: ignore[arg-type]
                merged[key] = nested
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _build_project_status_state(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        applied = bridge.outcome == "applied"
        return {
            "status": "active" if applied else "rolled_back_checkpoint",
            "last_completed_chapter": bridge.chapter_id if applied else "",
            "last_updated_from_chapter": bridge.chapter_id,
            "state_sync_status": "product_bridge_writeback_synced",
            "current_task": {
                "chapter_id": bridge.chapter_id,
                "stage": "post_writeback" if applied else "rolled_back",
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "chapter_relative_paths": list(bridge.chapter_relative_paths),
            },
            "product_bridge": {
                "change_set_id": bridge.change_set_id,
                "novel_state_bridge_receipt_id": bridge.receipt_id,
                "novel_state_bridge_receipt_hash": bridge.receipt_hash,
                "used_card_ids": list(used_card_ids),
                "outcome": bridge.outcome,
            },
            "_meta": {
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": "product writeback + NLCL original state sync bridge",
                "record_mode": "product_bridge_original_state_json_writeback",
            },
        }

    @staticmethod
    def _build_character_state(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": "product writeback + inventory refs; does not rewrite 03 truth",
                "phase": "post_writeback",
            },
            "writeback_current": {
                "chapter_id": bridge.chapter_id,
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "outcome": bridge.outcome,
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json") for item in bridge.character_state_refs
                ],
            },
            "characters": {},
        }

    @staticmethod
    def _build_plot_state(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": "product writeback + inventory refs; does not rewrite 04 truth",
            },
            "chapter_track": {
                "chapter_id": bridge.chapter_id,
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "chapter_relative_paths": list(bridge.chapter_relative_paths),
                "outcome": bridge.outcome,
            },
            "writeback_current": {
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json") for item in bridge.plot_state_refs
                ],
            },
            "open_threads": [],
        }

    @staticmethod
    def _build_foreshadowing_state(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": "product writeback + inventory refs; does not rewrite foreshadow truth",
            },
            "writeback_current": {
                "chapter_id": bridge.chapter_id,
                "outcome": bridge.outcome,
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json")
                    for item in bridge.foreshadowing_state_refs
                ],
            },
            "open_debts": [],
            "available_evidence": [],
        }

    @staticmethod
    def _build_truth_character_matrix(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": (
                    "product writeback + character inventory refs; "
                    "compressed support only, never rewrites 03 truth"
                ),
                "external_alignment": "真相文件_角色状态矩阵",
                "record_mode": "product_bridge_original_state_json_writeback",
            },
            "characters": {},
            "relationship_edges": [],
            "writeback_current": {
                "chapter_id": bridge.chapter_id,
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "outcome": bridge.outcome,
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json") for item in bridge.character_state_refs
                ],
            },
            "writer_must_check": [
                "角色当前状态优先读已定稿正文和 character_state.writeback_current",
                "敌友转向必须由事件、证据、代价或第三方压力挣来",
                "承重角色不得只当剧情推手",
            ],
        }

    @staticmethod
    def _build_truth_foreshadow_lifecycle(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": (
                    "product writeback + foreshadow inventory refs; "
                    "compressed support only, never rewrites 04 truth"
                ),
                "external_alignment": "真相文件_伏笔生命周期",
                "record_mode": "product_bridge_original_state_json_writeback",
            },
            "open_debts": [],
            "paid_debts": [],
            "retired_debts": [],
            "available_evidence": [],
            "writeback_current": {
                "chapter_id": bridge.chapter_id,
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "outcome": bridge.outcome,
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json")
                    for item in bridge.foreshadowing_state_refs
                ],
            },
            "writer_must_check": [
                "每章至少推进一个旧债、伏笔或关系代价",
                "禁提前项只能在满足证据后兑现",
                "新谜团必须写清下一次触碰接口",
            ],
        }

    @staticmethod
    def _build_truth_info_boundary(
        *,
        bridge: NlclOriginalNovelStateBridgeReceipt,
        used_card_ids: list[str],
        timestamp: str,
    ) -> dict[str, object]:
        return {
            "_meta": {
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_updated_from_chapter": bridge.chapter_id,
                "source_of_truth": (
                    "product writeback + inventory refs; "
                    "compressed support only, never rewrites 01-09 truth"
                ),
                "external_alignment": "真相文件_信息边界图",
                "record_mode": "product_bridge_original_state_json_writeback",
            },
            "entities": {},
            "shared_unknowns": [],
            "forbidden_cross_knowledge": [],
            "writeback_current": {
                "chapter_id": bridge.chapter_id,
                "canonical_chapter_key": bridge.canonical_chapter_key,
                "outcome": bridge.outcome,
                "used_card_ids": list(used_card_ids),
                "source_refs": [
                    item.model_dump(mode="json")
                    for item in (
                        list(bridge.character_state_refs)
                        + list(bridge.plot_state_refs)
                        + list(bridge.foreshadowing_state_refs)
                        + list(bridge.project_status_refs)
                    )
                ],
            },
            "writer_must_check": [
                "角色只能基于自己已知信息行动、误判或保留",
                "不要把读者知道的信息直接塞给角色",
                "关键真相揭露前必须复核 forbidden_cross_knowledge",
            ],
        }

    def _read_original_nlcl_novel_state_sync(
        self,
        path: Path,
    ) -> NlclOriginalNovelStateSyncReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalNovelStateSyncReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_novel_state_sync_history(
        self,
        path: Path,
    ) -> list[NlclOriginalNovelStateSyncReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalNovelStateSyncReceipt] = []
        for item in payload:
            try:
                history.append(NlclOriginalNovelStateSyncReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history



    def get_original_nlcl_prewrite_context_report(
        self,
        change_set_id: str,
    ) -> NlclOriginalPrewriteContextReportResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_prewrite_context_report(
            artifact_root / "nlcl-original-prewrite-context-report-receipt.json"
        )
        history = self._read_original_nlcl_prewrite_context_report_history(
            artifact_root / "nlcl-original-prewrite-context-report-history.json"
        )
        return NlclOriginalPrewriteContextReportResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_prewrite_context_report(
        self,
        change_set_id: str,
        request: NlclOriginalPrewriteContextReportRequest,
    ) -> NlclOriginalPrewriteContextReportResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL prewrite context report requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        sync = self._read_original_nlcl_novel_state_sync(
            artifact_root / "nlcl-original-novel-state-sync-receipt.json"
        )
        if sync is None:
            raise ChangeSetStateError(
                "original-compatible NLCL novel-state sync is required before prewrite context report"
            )
        if sync.receipt_hash != request.expected_nlcl_original_novel_state_sync_receipt_hash:
            raise ChangeSetConflictError(
                "NLCL original novel-state sync receipt hash does not match the expected value"
            )
        if sync.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original novel-state sync receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if sync.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original novel-state sync outcome does not match ChangeSet status"
            )

        activation = self._read_original_nlcl_bounded_library_activation(
            artifact_root / "nlcl-original-bounded-library-activation-receipt.json"
        )
        if (
            activation is not None
            and (
                activation.change_set_id != record.change_set_id
                or activation.outcome
                != (
                    "applied"
                    if record.status == ChangeSetStatus.APPLIED
                    else "rolled_back"
                )
            )
        ):
            activation = None

        existing = self._read_original_nlcl_prewrite_context_report(
            artifact_root / "nlcl-original-prewrite-context-report-receipt.json"
        )
        existing_activation_hash = (
            existing.nlcl_original_bounded_library_activation_receipt_hash
            if existing is not None
            else None
        )
        current_activation_hash = activation.receipt_hash if activation is not None else None
        if (
            existing is not None
            and existing.nlcl_original_novel_state_sync_receipt_hash == sync.receipt_hash
            and existing.outcome == sync.outcome
            and existing_activation_hash == current_activation_hash
        ):
            history = self._read_original_nlcl_prewrite_context_report_history(
                artifact_root / "nlcl-original-prewrite-context-report-history.json"
            )
            return NlclOriginalPrewriteContextReportResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        # Load state files written by novel-state sync for report content.
        def _load_state(name: str) -> dict[str, object]:
            path = project_root / "state" / name
            if not path.is_file():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        project_status = _load_state("project_status.json")
        character_state = _load_state("character_state.json")
        foreshadowing_state = _load_state("foreshadowing_state.json")
        truth_character = _load_state("truth_character_matrix.json")
        truth_foreshadow = _load_state("truth_foreshadow_lifecycle.json")
        truth_info = _load_state("truth_info_boundary.json")
        ts = _now().isoformat().replace("+00:00", "Z")
        applied = sync.outcome == "applied"
        report = {
            "project": Path(binding.root_path).name,
            "chapter_id": sync.chapter_id,
            "stage": "post_writeback" if applied else "rolled_back",
            "generated_at": ts,
            "route_health": "product_bridge_ready",
            "context_health": "product_bridge_state_synced",
            "context_degraded": False,
            "project_status": project_status,
            "project_status_contract": {
                "last_completed_chapter": project_status.get("last_completed_chapter", ""),
                "current_phase": project_status.get("current_phase", ""),
                "current_arc": project_status.get("current_arc", ""),
                "state_sync_status": project_status.get(
                    "state_sync_status", "product_bridge_writeback_synced"
                ),
                "current_task": project_status.get("current_task", {}),
            },
            "chapter_mission": {
                "task_goal": f"continue from {sync.chapter_id} product writeback state",
                "reader_promise": "",
                "scene_goal": "",
                "visible_conflict": "",
                "core_promise": "",
            },
            "kickoff_pack": {
                "protagonist_carry_in": "read character_state.writeback_current and truth_character_matrix",
                "supporting_weight_rule": "承重角色不得只当剧情推手",
                "unresolved_threat_must_handle": "优先推进 open_debts / foreshadow lifecycle",
                "old_setting_use_mode": "use_not_recite",
            },
            "seam_check": {
                "previous_chapter": sync.chapter_id,
                "outcome": sync.outcome,
                "canonical_chapter_key": sync.canonical_chapter_key,
            },
            "continuation_state_contract": {
                "required_previous_chapter": sync.chapter_id if applied else "",
                "writing_layer_must_use": [
                    "state/project_status.json",
                    "state/character_state.json",
                    "state/plot_state.json",
                    "state/foreshadowing_state.json",
                    "state/truth_character_matrix.json",
                    "state/truth_foreshadow_lifecycle.json",
                    "state/truth_info_boundary.json",
                    f"chapter_context/{sync.chapter_id}_prewrite_context_report_latest.md",
                ],
                "next_chapter_interface": {
                    "from_chapter": sync.chapter_id,
                    "chapter_relative_paths": list(sync.chapter_relative_paths),
                    "outcome": sync.outcome,
                },
            },
            "current_character_state": character_state.get("characters", {})
            if isinstance(character_state.get("characters"), dict)
            else {},
            "open_debts": foreshadowing_state.get("open_debts", [])
            if isinstance(foreshadowing_state.get("open_debts"), list)
            else [],
            "available_evidence": foreshadowing_state.get("available_evidence", [])
            if isinstance(foreshadowing_state.get("available_evidence"), list)
            else [],
            "truth_files": {
                "truth_character_matrix": {
                    "relative_path": "state/truth_character_matrix.json",
                    "external_alignment": (
                        (truth_character.get("_meta") or {}).get("external_alignment")
                        if isinstance(truth_character.get("_meta"), dict)
                        else "真相文件_角色状态矩阵"
                    ),
                },
                "truth_foreshadow_lifecycle": {
                    "relative_path": "state/truth_foreshadow_lifecycle.json",
                    "external_alignment": (
                        (truth_foreshadow.get("_meta") or {}).get("external_alignment")
                        if isinstance(truth_foreshadow.get("_meta"), dict)
                        else "真相文件_伏笔生命周期"
                    ),
                },
                "truth_info_boundary": {
                    "relative_path": "state/truth_info_boundary.json",
                    "external_alignment": (
                        (truth_info.get("_meta") or {}).get("external_alignment")
                        if isinstance(truth_info.get("_meta"), dict)
                        else "真相文件_信息边界图"
                    ),
                },
            },
            "block_context_bundle": {
                "macro_story_bible_digest": "use project 01-09 truth files as authority",
                "recent_1_2_chapter_recaps": list(sync.chapter_relative_paths),
                "open_debts_and_foreshadowing": foreshadowing_state.get("open_debts", [])
                if isinstance(foreshadowing_state.get("open_debts"), list)
                else [],
                "whole_novel_context_forbidden": True,
            },
            "library_activation": (
                {
                    "bounded_library_activated": True,
                    "whole_library_load": False,
                    "whole_library_load_forbidden": True,
                    "activation_receipt_id": activation.receipt_id,
                    "activation_receipt_hash": activation.receipt_hash,
                    "activated_count": activation.activated_count,
                    "max_sources": activation.max_sources,
                    "activated_source_ids": list(activation.activated_source_ids),
                    "used_characters": activation.used_characters,
                    "max_characters": activation.max_characters,
                    "used_tokens": activation.used_tokens,
                    "max_tokens": activation.max_tokens,
                }
                if activation is not None
                else {
                    "bounded_library_activated": False,
                    "whole_library_load": False,
                    "whole_library_load_forbidden": True,
                    "activated_count": 0,
                    "activated_source_ids": [],
                    "note": "bounded activation not bound; prewrite may re-export after activation",
                }
            ),
            "do_not_replay": [
                "上一章结尾只当状态来源，不要在本章开头重演或重新发现",
                "旧设定只许拿来用，不许背诵式复述",
                "product bridge report 不是 formal draft 授权",
            ],
            "prewrite_p0_gate": {
                "status": "product_bridge_ready_for_next_draft_planning"
                if applied
                else "rolled_back_checkpoint",
                "allowed_output": "risk_draft_or_planning_only",
                "blocking_failure_signatures": [],
                "required_before_full_draft": [
                    "author review of next chapter plan",
                    "fresh state freshness check for previous chapter",
                    "formal draft remains denied until release gates pass",
                ],
            },
            "product_bridge": {
                "change_set_id": record.change_set_id,
                "novel_state_sync_receipt_id": sync.receipt_id,
                "novel_state_sync_receipt_hash": sync.receipt_hash,
                "outcome": sync.outcome,
            },
        }
        markdown = self._render_prewrite_context_report_markdown(report)
        stage_slug = "post_writeback"
        chapter_id = sync.chapter_id
        path_specs = {
            "json_stage": (
                chapter_context_root
                / f"{chapter_id}_prewrite_context_report_{stage_slug}.json",
                f"chapter_context/{chapter_id}_prewrite_context_report_{stage_slug}.json",
                {"prewrite_context_report": report},
                True,
            ),
            "json_latest": (
                chapter_context_root
                / f"{chapter_id}_prewrite_context_report_latest.json",
                f"chapter_context/{chapter_id}_prewrite_context_report_latest.json",
                {"prewrite_context_report": report},
                True,
            ),
            "md_stage": (
                chapter_context_root
                / f"{chapter_id}_prewrite_context_report_{stage_slug}.md",
                f"chapter_context/{chapter_id}_prewrite_context_report_{stage_slug}.md",
                markdown,
                False,
            ),
            "md_latest": (
                chapter_context_root
                / f"{chapter_id}_prewrite_context_report_latest.md",
                f"chapter_context/{chapter_id}_prewrite_context_report_latest.md",
                markdown,
                False,
            ),
        }
        written: list[NlclOriginalPrewriteContextReportWrittenFile] = []
        for kind, (path, relative, payload, is_json) in path_specs.items():
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"prewrite report path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"prewrite report path must be a regular file: {path}")
            created = not path.exists()
            if is_json:
                _atomic_write_json(path, payload)  # type: ignore[arg-type]
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(str(payload), encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalPrewriteContextReportWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )
        written.sort(key=lambda item: item.kind)
        material = {
            "schema": "zhimeng.nlcl-original-prewrite-context-report-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_novel_state_sync_receipt_id": sync.receipt_id,
            "nlcl_original_novel_state_sync_receipt_hash": sync.receipt_hash,
            "outcome": sync.outcome,
            "chapter_id": sync.chapter_id,
            "canonical_chapter_key": sync.canonical_chapter_key,
            "written_files": [item.model_dump(mode="json") for item in written],
            "report_relative_path": f"chapter_context/{chapter_id}_prewrite_context_report_latest.json",
            "record_mode": "product_bridge_prewrite_context_report",
            "original_nlcl_novel_state_synced": True,
            "original_nlcl_truth_matrix_synced": True,
            "prewrite_context_report_synced": True,
            "bounded_library_activated": activation is not None,
            "nlcl_original_bounded_library_activation_receipt_id": (
                activation.receipt_id if activation is not None else None
            ),
            "nlcl_original_bounded_library_activation_receipt_hash": (
                activation.receipt_hash if activation is not None else None
            ),
            "activated_source_count": (
                activation.activated_count if activation is not None else 0
            ),
            "whole_library_load_forbidden": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-prewrite-context-report-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalPrewriteContextReportReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-prewrite-context-report-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-prewrite-context-report-history.json"
        history = self._read_original_nlcl_prewrite_context_report_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalPrewriteContextReportResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    @staticmethod
    def _render_prewrite_context_report_markdown(report: dict[str, object]) -> str:
        mission = report.get("chapter_mission") if isinstance(report.get("chapter_mission"), dict) else {}
        project_status = (
            report.get("project_status_contract")
            if isinstance(report.get("project_status_contract"), dict)
            else {}
        )
        kickoff = report.get("kickoff_pack") if isinstance(report.get("kickoff_pack"), dict) else {}
        continuation = (
            report.get("continuation_state_contract")
            if isinstance(report.get("continuation_state_contract"), dict)
            else {}
        )
        p0 = report.get("prewrite_p0_gate") if isinstance(report.get("prewrite_p0_gate"), dict) else {}
        lines = [
            "# 前情写作上下文报告",
            "",
            f"- project: `{report.get('project', '')}`",
            f"- chapter_id: `{report.get('chapter_id', '')}`",
            f"- stage: `{report.get('stage', '')}`",
            f"- generated_at: `{report.get('generated_at', '')}`",
            f"- route_health: `{report.get('route_health', '')}`",
            f"- context_health: `{report.get('context_health', '')}`",
            f"- context_degraded: `{report.get('context_degraded', False)}`",
            "",
            "## 项目状态中枢",
            "",
            f"- last_completed_chapter: `{project_status.get('last_completed_chapter', '')}`",
            f"- current_phase: `{project_status.get('current_phase', '')}`",
            f"- current_arc: `{project_status.get('current_arc', '')}`",
            f"- state_sync_status: `{project_status.get('state_sync_status', '')}`",
            "",
            "## 本章使命",
            "",
            f"- task_goal: {mission.get('task_goal', '')}",
            f"- reader_promise: {mission.get('reader_promise', '')}",
            f"- scene_goal: {mission.get('scene_goal', '')}",
            f"- visible_conflict: {mission.get('visible_conflict', '')}",
            f"- core_promise: {mission.get('core_promise', '')}",
            "",
            "## 开工包",
            "",
            f"- protagonist_carry_in: {kickoff.get('protagonist_carry_in', '')}",
            f"- supporting_weight_rule: {kickoff.get('supporting_weight_rule', '')}",
            f"- unresolved_threat_must_handle: {kickoff.get('unresolved_threat_must_handle', '')}",
            f"- old_setting_use_mode: {kickoff.get('old_setting_use_mode', '')}",
            "",
            "## 长篇续写合同",
            "",
            f"- required_previous_chapter: `{continuation.get('required_previous_chapter', '')}`",
            "",
            "### writing_layer_must_use",
            "",
        ]
        must_use = continuation.get("writing_layer_must_use", [])
        if isinstance(must_use, list):
            for item in must_use:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## 不得重演 / 不得误用",
                "",
            ]
        )
        do_not = report.get("do_not_replay", [])
        if isinstance(do_not, list) and do_not:
            for item in do_not:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.extend(
            [
                "",
                "## P0 起稿门",
                "",
                f"- status: `{p0.get('status', '')}`",
                f"- allowed_output: `{p0.get('allowed_output', '')}`",
                "",
                "### required_before_full_draft",
                "",
            ]
        )
        required = p0.get("required_before_full_draft", [])
        if isinstance(required, list) and required:
            for item in required:
                lines.append(f"- {item}")
        else:
            lines.append("- none")
        lines.append("")
        return "\n".join(lines)

    def _read_original_nlcl_prewrite_context_report(
        self,
        path: Path,
    ) -> NlclOriginalPrewriteContextReportReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalPrewriteContextReportReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_prewrite_context_report_history(
        self,
        path: Path,
    ) -> list[NlclOriginalPrewriteContextReportReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalPrewriteContextReportReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalPrewriteContextReportReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history


    def get_original_nlcl_truth_content_extract(
        self,
        change_set_id: str,
    ) -> NlclOriginalTruthContentExtractResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_truth_content_extract(
            artifact_root / "nlcl-original-truth-content-extract-receipt.json"
        )
        history = self._read_original_nlcl_truth_content_extract_history(
            artifact_root / "nlcl-original-truth-content-extract-history.json"
        )
        return NlclOriginalTruthContentExtractResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_truth_content_extract(
        self,
        change_set_id: str,
        request: NlclOriginalTruthContentExtractRequest,
    ) -> NlclOriginalTruthContentExtractResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL truth content extract requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        sync = self._read_original_nlcl_novel_state_sync(
            artifact_root / "nlcl-original-novel-state-sync-receipt.json"
        )
        if sync is None:
            raise ChangeSetStateError(
                "original-compatible NLCL novel-state sync is required before truth content extract"
            )
        if sync.receipt_hash != request.expected_nlcl_original_novel_state_sync_receipt_hash:
            raise ChangeSetConflictError(
                "NLCL original novel-state sync receipt hash does not match the expected value"
            )
        if sync.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original novel-state sync receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if sync.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original novel-state sync outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_truth_content_extract(
            artifact_root / "nlcl-original-truth-content-extract-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_novel_state_sync_receipt_hash == sync.receipt_hash
            and existing.outcome == sync.outcome
        ):
            history = self._read_original_nlcl_truth_content_extract_history(
                artifact_root / "nlcl-original-truth-content-extract-history.json"
            )
            return NlclOriginalTruthContentExtractResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        state_root = project_root / "state"
        if state_root.exists() and not state_root.is_dir():
            raise ChangeSetStateError("project state path must be a directory")
        if state_root.exists() and _is_link_or_reparse(state_root):
            raise ChangeSetStateError("project state path cannot be a link or reparse point")
        state_root.mkdir(parents=True, exist_ok=True)

        extracted = self._extract_truth_content_from_project(
            project_root=project_root,
            chapter_id=sync.chapter_id,
            outcome=sync.outcome,
        )
        source_files = extracted["source_files"]
        characters = extracted["characters"]
        foreshadows = extracted["foreshadows"]
        plot_threads = extracted["plot_threads"]
        info_entities = extracted["info_entities"]
        ts = _now().isoformat().replace("+00:00", "Z")

        payloads = {
            "character": {
                "_meta": {
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 03_人物设定 + writeback",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "writeback_current": {
                    "chapter_id": sync.chapter_id,
                    "canonical_chapter_key": sync.canonical_chapter_key,
                    "outcome": sync.outcome,
                },
                "characters": characters,
            },
            "plot": {
                "_meta": {
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 04_剧情大纲 + writeback",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "chapter_track": {
                    "chapter_id": sync.chapter_id,
                    "canonical_chapter_key": sync.canonical_chapter_key,
                    "chapter_relative_paths": list(sync.chapter_relative_paths),
                    "outcome": sync.outcome,
                },
                "open_threads": plot_threads,
            },
            "foreshadowing": {
                "_meta": {
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 04_剧情大纲 foreshadow markers",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "writeback_current": {
                    "chapter_id": sync.chapter_id,
                    "outcome": sync.outcome,
                },
                "open_debts": foreshadows,
                "available_evidence": [],
            },
            "truth_character_matrix": {
                "_meta": {
                    "created_at": ts,
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 03 truth markdown",
                    "external_alignment": "真相文件_角色状态矩阵",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "characters": characters,
                "relationship_edges": [],
                "writer_must_check": [
                    "角色当前状态优先读已定稿正文和 character_state.writeback_current",
                    "敌友转向必须由事件、证据、代价或第三方压力挣来",
                    "承重角色不得只当剧情推手",
                ],
            },
            "truth_foreshadow_lifecycle": {
                "_meta": {
                    "created_at": ts,
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 04 foreshadow markers",
                    "external_alignment": "真相文件_伏笔生命周期",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "open_debts": foreshadows,
                "paid_debts": [],
                "retired_debts": [],
                "available_evidence": [],
                "writer_must_check": [
                    "每章至少推进一个旧债、伏笔或关系代价",
                    "禁提前项只能在满足证据后兑现",
                    "新谜团必须写清下一次触碰接口",
                ],
            },
            "truth_info_boundary": {
                "_meta": {
                    "created_at": ts,
                    "updated_at": ts,
                    "last_updated_from_chapter": sync.chapter_id,
                    "source_of_truth": "deterministic extract from 01/02/06 knowledge markers",
                    "external_alignment": "真相文件_信息边界图",
                    "record_mode": "product_bridge_truth_content_extract_from_01_09",
                },
                "entities": info_entities,
                "shared_unknowns": [],
                "forbidden_cross_knowledge": [],
                "writer_must_check": [
                    "角色只能基于自己已知信息行动、误判或保留",
                    "不要把读者知道的信息直接塞给角色",
                    "关键真相揭露前必须复核 forbidden_cross_knowledge",
                ],
            },
        }
        path_map = {
            "character": state_root / "character_state.json",
            "plot": state_root / "plot_state.json",
            "foreshadowing": state_root / "foreshadowing_state.json",
            "truth_character_matrix": state_root / "truth_character_matrix.json",
            "truth_foreshadow_lifecycle": state_root / "truth_foreshadow_lifecycle.json",
            "truth_info_boundary": state_root / "truth_info_boundary.json",
        }
        relative_map = {
            "character": "state/character_state.json",
            "plot": "state/plot_state.json",
            "foreshadowing": "state/foreshadowing_state.json",
            "truth_character_matrix": "state/truth_character_matrix.json",
            "truth_foreshadow_lifecycle": "state/truth_foreshadow_lifecycle.json",
            "truth_info_boundary": "state/truth_info_boundary.json",
        }
        count_map = {
            "character": len(characters),
            "plot": len(plot_threads),
            "foreshadowing": len(foreshadows),
            "truth_character_matrix": len(characters),
            "truth_foreshadow_lifecycle": len(foreshadows),
            "truth_info_boundary": len(info_entities),
        }
        written: list[NlclOriginalTruthContentExtractWrittenFile] = []
        for domain, path in path_map.items():
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"state path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"state file must be a regular file: {path}")
            created = not path.exists()
            existing_payload: dict[str, object] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing_payload = loaded
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    existing_payload = {}
            merged = self._merge_state_payload(existing_payload, payloads[domain])
            # Prefer extracted content maps over empty scaffolds.
            for key in ("characters", "entities"):
                if key in payloads[domain] and isinstance(payloads[domain][key], dict):
                    merged[key] = payloads[domain][key]
            for key in ("open_threads", "open_debts", "available_evidence", "paid_debts", "retired_debts"):
                if key in payloads[domain] and isinstance(payloads[domain][key], list):
                    merged[key] = payloads[domain][key]
            _atomic_write_json(path, merged)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalTruthContentExtractWrittenFile(
                    relative_path=relative_map[domain],
                    source_hash=digest,
                    domain=domain,  # type: ignore[arg-type]
                    created=created,
                    item_count=count_map[domain],
                )
            )
        written.sort(key=lambda item: item.domain)
        material = {
            "schema": "zhimeng.nlcl-original-truth-content-extract-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_novel_state_sync_receipt_id": sync.receipt_id,
            "nlcl_original_novel_state_sync_receipt_hash": sync.receipt_hash,
            "outcome": sync.outcome,
            "chapter_id": sync.chapter_id,
            "canonical_chapter_key": sync.canonical_chapter_key,
            "source_files": source_files,
            "written_files": [item.model_dump(mode="json") for item in written],
            "extracted_character_count": len(characters),
            "extracted_foreshadow_count": len(foreshadows),
            "extracted_info_boundary_count": len(info_entities),
            "record_mode": "product_bridge_truth_content_extract_from_01_09",
            "original_nlcl_novel_state_synced": True,
            "original_nlcl_truth_matrix_synced": True,
            "truth_content_extracted": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-truth-content-extract-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalTruthContentExtractReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-truth-content-extract-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-truth-content-extract-history.json"
        history = self._read_original_nlcl_truth_content_extract_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalTruthContentExtractResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    @staticmethod
    def _extract_truth_content_from_project(
        *,
        project_root: Path,
        chapter_id: str,
        outcome: str,
    ) -> dict[str, object]:
        import re

        section_dirs: list[tuple[int, Path]] = []
        for child in project_root.iterdir() if project_root.is_dir() else []:
            if not child.is_dir() or _is_link_or_reparse(child):
                continue
            match = re.match(r"^0([1-9])_", child.name)
            if not match:
                continue
            index = int(match.group(1))
            if index in {1, 2, 3, 4, 6}:
                section_dirs.append((index, child))
        section_dirs.sort(key=lambda item: item[0])

        source_files: list[dict[str, object]] = []
        characters: dict[str, object] = {}
        foreshadows: list[dict[str, object]] = []
        plot_threads: list[dict[str, object]] = []
        info_entities: dict[str, object] = {}
        heading_re = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*$")
        bullet_re = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
        foreshadow_markers = ("伏笔", "悬念", "未偿", "欠账", "谜团", "禁提前", "债务")
        info_markers = ("信息边界", "已知", "未知", "禁提前", "世界观", "规则", "设定")

        def slug(value: str) -> str:
            cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "_", value.strip(), flags=re.UNICODE)
            cleaned = cleaned.strip("_")
            return cleaned[:48] or "item"

        for section_index, section_dir in section_dirs:
            files = sorted(
                [
                    path
                    for path in section_dir.rglob("*")
                    if path.is_file()
                    and not _is_link_or_reparse(path)
                    and path.suffix.lower() in {".md", ".markdown", ".txt"}
                ],
                key=lambda path: str(path).casefold(),
            )
            for path in files[:32]:
                if not _is_within(path, project_root):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if len(text.encode("utf-8")) > 512 * 1024:
                    continue
                relative = path.relative_to(project_root).as_posix()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                item_count = 0
                current_heading = path.stem
                current_lines: list[str] = []
                bullets: list[str] = []

                def flush_block() -> None:
                    nonlocal item_count
                    title = current_heading.strip()
                    if not title:
                        return
                    summary = " ".join(current_lines).strip()
                    if not summary and bullets:
                        summary = "；".join(bullets[:3])
                    if not summary:
                        summary = title
                    key = slug(title)
                    lower = f"{title} {summary}".lower()
                    if section_index == 3:
                        characters[key] = {
                            "name": title,
                            "status_summary": summary[:500],
                            "source_relative_path": relative,
                            "last_updated_from_chapter": chapter_id,
                            "outcome": outcome,
                        }
                        item_count += 1
                    elif section_index == 4:
                        if any(marker in title or marker in summary for marker in foreshadow_markers):
                            fid = f"foreshadow_{key}"
                            suffix = 2
                            existing_ids = {
                                str(item.get("id"))
                                for item in foreshadows
                                if isinstance(item, dict)
                            }
                            while fid in existing_ids:
                                fid = f"foreshadow_{key}_{suffix}"
                                suffix += 1
                            foreshadows.append(
                                {
                                    "id": fid,
                                    "title": title,
                                    "status": "open",
                                    "summary": summary[:500],
                                    "source_relative_path": relative,
                                    "last_updated_from_chapter": chapter_id,
                                }
                            )
                            item_count += 1
                        else:
                            tid = f"thread_{key}"
                            suffix = 2
                            existing_ids = {
                                str(item.get("id"))
                                for item in plot_threads
                                if isinstance(item, dict)
                            }
                            while tid in existing_ids:
                                tid = f"thread_{key}_{suffix}"
                                suffix += 1
                            plot_threads.append(
                                {
                                    "id": tid,
                                    "title": title,
                                    "summary": summary[:500],
                                    "source_relative_path": relative,
                                    "last_updated_from_chapter": chapter_id,
                                }
                            )
                            item_count += 1
                    elif section_index in {1, 2, 6}:
                        if any(marker in title or marker in summary for marker in info_markers) or section_index in {1, 2}:
                            info_entities[key] = {
                                "label": title,
                                "known_by": [],
                                "summary": summary[:500],
                                "source_relative_path": relative,
                                "last_updated_from_chapter": chapter_id,
                            }
                            item_count += 1

                for raw_line in text.splitlines()[:400]:
                    line = raw_line.rstrip()
                    heading = heading_re.match(line)
                    if heading:
                        flush_block()
                        current_heading = heading.group(1).strip()
                        current_lines = []
                        bullets = []
                        continue
                    bullet = bullet_re.match(line)
                    if bullet:
                        bullets.append(bullet.group(1).strip())
                        continue
                    if line.strip():
                        current_lines.append(line.strip())
                flush_block()
                source_files.append(
                    {
                        "relative_path": relative,
                        "source_hash": digest,
                        "section_index": section_index,
                        "extracted_item_count": item_count,
                    }
                )

        source_files = source_files[:128]
        return {
            "source_files": source_files,
            "characters": characters,
            "foreshadows": foreshadows[:128],
            "plot_threads": plot_threads[:128],
            "info_entities": info_entities,
        }

    def _read_original_nlcl_truth_content_extract(
        self,
        path: Path,
    ) -> NlclOriginalTruthContentExtractReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalTruthContentExtractReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_truth_content_extract_history(
        self,
        path: Path,
    ) -> list[NlclOriginalTruthContentExtractReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalTruthContentExtractReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalTruthContentExtractReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history


    def get_original_nlcl_mandatory_usage_plan(
        self,
        change_set_id: str,
    ) -> NlclOriginalMandatoryUsagePlanResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_mandatory_usage_plan(
            artifact_root / "nlcl-original-mandatory-usage-plan-receipt.json"
        )
        history = self._read_original_nlcl_mandatory_usage_plan_history(
            artifact_root / "nlcl-original-mandatory-usage-plan-history.json"
        )
        return NlclOriginalMandatoryUsagePlanResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_mandatory_usage_plan(
        self,
        change_set_id: str,
        request: NlclOriginalMandatoryUsagePlanRequest,
    ) -> NlclOriginalMandatoryUsagePlanResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL mandatory usage plan requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        extract = self._read_original_nlcl_truth_content_extract(
            artifact_root / "nlcl-original-truth-content-extract-receipt.json"
        )
        if extract is None:
            raise ChangeSetStateError(
                "original-compatible NLCL truth content extract is required before mandatory usage plan"
            )
        if (
            extract.receipt_hash
            != request.expected_nlcl_original_truth_content_extract_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original truth content extract receipt hash does not match the expected value"
            )
        if extract.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original truth content extract receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if extract.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original truth content extract outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_mandatory_usage_plan(
            artifact_root / "nlcl-original-mandatory-usage-plan-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_truth_content_extract_receipt_hash
            == extract.receipt_hash
            and existing.outcome == extract.outcome
        ):
            history = self._read_original_nlcl_mandatory_usage_plan_history(
                artifact_root / "nlcl-original-mandatory-usage-plan-history.json"
            )
            return NlclOriginalMandatoryUsagePlanResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        def _load_state(name: str) -> dict[str, object]:
            path = project_root / "state" / name
            if not path.is_file():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        character_state = _load_state("character_state.json")
        plot_state = _load_state("plot_state.json")
        foreshadow_state = _load_state("foreshadowing_state.json")
        info_state = _load_state("truth_info_boundary.json")
        items: list[NlclOriginalMandatoryUsagePlanItem] = []

        characters = character_state.get("characters")
        if isinstance(characters, dict):
            for key, value in list(characters.items())[:64]:
                if not isinstance(value, dict):
                    continue
                title = str(value.get("name") or key).strip() or str(key)
                items.append(
                    NlclOriginalMandatoryUsagePlanItem(
                        item_id=f"character:{key}"[:128],
                        kind="character",
                        title=title[:256],
                        source_relative_path=(
                            str(value.get("source_relative_path"))[:512]
                            if value.get("source_relative_path")
                            else None
                        ),
                        required_action="在正文中推进角色当前状态、关系代价或可见压力，不得只当推手",
                        status="pending",
                    )
                )

        open_debts = foreshadow_state.get("open_debts")
        if isinstance(open_debts, list):
            for raw in open_debts[:64]:
                if not isinstance(raw, dict):
                    continue
                item_id = str(raw.get("id") or raw.get("title") or "foreshadow").strip()
                title = str(raw.get("title") or item_id).strip() or item_id
                items.append(
                    NlclOriginalMandatoryUsagePlanItem(
                        item_id=f"foreshadow:{item_id}"[:128],
                        kind="foreshadow",
                        title=title[:256],
                        source_relative_path=(
                            str(raw.get("source_relative_path"))[:512]
                            if raw.get("source_relative_path")
                            else None
                        ),
                        required_action="推进旧债/伏笔，或写清下一次触碰接口；不得空转新谜团",
                        status="pending",
                    )
                )

        open_threads = plot_state.get("open_threads")
        if isinstance(open_threads, list):
            for raw in open_threads[:32]:
                if not isinstance(raw, dict):
                    continue
                item_id = str(raw.get("id") or raw.get("title") or "thread").strip()
                title = str(raw.get("title") or item_id).strip() or item_id
                items.append(
                    NlclOriginalMandatoryUsagePlanItem(
                        item_id=f"plot:{item_id}"[:128],
                        kind="plot_thread",
                        title=title[:256],
                        source_relative_path=(
                            str(raw.get("source_relative_path"))[:512]
                            if raw.get("source_relative_path")
                            else None
                        ),
                        required_action="延续主线冲突并留下可感知推进，不得复读设定",
                        status="pending",
                    )
                )

        entities = info_state.get("entities")
        if isinstance(entities, dict):
            for key, value in list(entities.items())[:32]:
                if not isinstance(value, dict):
                    continue
                title = str(value.get("label") or key).strip() or str(key)
                items.append(
                    NlclOriginalMandatoryUsagePlanItem(
                        item_id=f"info:{key}"[:128],
                        kind="info_boundary",
                        title=title[:256],
                        source_relative_path=(
                            str(value.get("source_relative_path"))[:512]
                            if value.get("source_relative_path")
                            else None
                        ),
                        required_action="角色只能基于自身已知信息行动；不得把读者信息直接塞给角色",
                        status="pending",
                    )
                )

        # Keep plan compact, deterministic, and unique by item_id.
        unique_items: list[NlclOriginalMandatoryUsagePlanItem] = []
        seen_ids: set[str] = set()
        for item in items:
            base_id = item.item_id
            candidate = base_id
            suffix = 2
            while candidate in seen_ids:
                candidate = f"{base_id}_{suffix}"[:128]
                suffix += 1
            seen_ids.add(candidate)
            if candidate != item.item_id:
                item = item.model_copy(update={"item_id": candidate})
            unique_items.append(item)
        items = unique_items[:128]
        ts = _now().isoformat().replace("+00:00", "Z")
        chapter_id = extract.chapter_id
        plan_payload = {
            "schema": "zhimeng.product-bridge-mandatory-usage-plan.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": extract.canonical_chapter_key,
            "outcome": extract.outcome,
            "generated_at": ts,
            "overall_status": "open_scaffold",
            "mandatory_usage_closure_closed": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_mandatory_usage_plan_scaffold",
            "source": {
                "truth_content_extract_receipt_id": extract.receipt_id,
                "truth_content_extract_receipt_hash": extract.receipt_hash,
                "novel_state_sync_receipt_id": extract.nlcl_original_novel_state_sync_receipt_id,
                "novel_state_sync_receipt_hash": extract.nlcl_original_novel_state_sync_receipt_hash,
            },
            "items": [item.model_dump(mode="json") for item in items],
            "writer_must_check": [
                "本计划是 scaffold，不是 closed mandatory_usage_closure",
                "pending 项未全部 used/unused/missed/suppressed 前不得标 validated",
                "formal_draft_allowed 仍为 false",
            ],
        }
        md_lines = [
            f"# Mandatory usage plan · {chapter_id}",
            "",
            f"- overall_status: open_scaffold",
            f"- mandatory_usage_closure_closed: false",
            f"- formal_draft_allowed: false",
            f"- items: {len(items)}",
            "",
            "## Pending items",
        ]
        for item in items:
            md_lines.append(
                f"- [{item.kind}] {item.title} — {item.required_action} ({item.status})"
            )
        if not items:
            md_lines.append("- (empty) extract 未产出可追踪项；保持 open_scaffold")
        md_text = "\n".join(md_lines) + "\n"

        stage_slug = "post_writeback" if extract.outcome == "applied" else "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_mandatory_usage_plan_latest.json",
                f"chapter_context/{chapter_id}_mandatory_usage_plan_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_mandatory_usage_plan_latest.md",
                f"chapter_context/{chapter_id}_mandatory_usage_plan_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_mandatory_usage_plan_{stage_slug}.json",
                f"chapter_context/{chapter_id}_mandatory_usage_plan_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_mandatory_usage_plan_{stage_slug}.md",
                f"chapter_context/{chapter_id}_mandatory_usage_plan_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalMandatoryUsagePlanWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"plan path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"plan file must be a regular file: {path}")
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, plan_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalMandatoryUsagePlanWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_truth_content_extract_receipt_id": extract.receipt_id,
            "nlcl_original_truth_content_extract_receipt_hash": extract.receipt_hash,
            "nlcl_original_novel_state_sync_receipt_id": extract.nlcl_original_novel_state_sync_receipt_id,
            "nlcl_original_novel_state_sync_receipt_hash": extract.nlcl_original_novel_state_sync_receipt_hash,
            "outcome": extract.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": extract.canonical_chapter_key,
            "items": [item.model_dump(mode="json") for item in items],
            "written_files": [item.model_dump(mode="json") for item in written],
            "plan_relative_path": f"chapter_context/{chapter_id}_mandatory_usage_plan_latest.json",
            "record_mode": "product_bridge_mandatory_usage_plan_scaffold",
            "original_nlcl_novel_state_synced": True,
            "original_nlcl_truth_matrix_synced": True,
            "truth_content_extracted": True,
            "mandatory_usage_plan_synced": True,
            "mandatory_usage_closure_closed": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-mandatory-usage-plan-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalMandatoryUsagePlanReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-mandatory-usage-plan-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-mandatory-usage-plan-history.json"
        history = self._read_original_nlcl_mandatory_usage_plan_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalMandatoryUsagePlanResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_mandatory_usage_plan(
        self,
        path: Path,
    ) -> NlclOriginalMandatoryUsagePlanReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalMandatoryUsagePlanReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_mandatory_usage_plan_history(
        self,
        path: Path,
    ) -> list[NlclOriginalMandatoryUsagePlanReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalMandatoryUsagePlanReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalMandatoryUsagePlanReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history



    def get_original_nlcl_mandatory_usage_closure(
        self,
        change_set_id: str,
    ) -> NlclOriginalMandatoryUsageClosureResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_mandatory_usage_closure(
            artifact_root / "nlcl-original-mandatory-usage-closure-receipt.json"
        )
        history = self._read_original_nlcl_mandatory_usage_closure_history(
            artifact_root / "nlcl-original-mandatory-usage-closure-history.json"
        )
        return NlclOriginalMandatoryUsageClosureResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_mandatory_usage_closure(
        self,
        change_set_id: str,
        request: NlclOriginalMandatoryUsageClosureRequest,
    ) -> NlclOriginalMandatoryUsageClosureResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL mandatory usage closure requires an applied or rolled_back ChangeSet"
            )
        if not request.finalize:
            raise ChangeSetStateError(
                "product-bridge mandatory usage closure currently requires finalize=true"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        plan = self._read_original_nlcl_mandatory_usage_plan(
            artifact_root / "nlcl-original-mandatory-usage-plan-receipt.json"
        )
        if plan is None:
            raise ChangeSetStateError(
                "original-compatible NLCL mandatory usage plan is required before closure"
            )
        if (
            plan.receipt_hash
            != request.expected_nlcl_original_mandatory_usage_plan_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original mandatory usage plan receipt hash does not match the expected value"
            )
        if plan.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original mandatory usage plan receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if plan.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original mandatory usage plan outcome does not match ChangeSet status"
            )
        if not plan.items:
            raise ChangeSetStateError(
                "mandatory usage plan has no items to close"
            )

        decision_map: dict[str, NlclOriginalMandatoryUsageClosureDecision] = {}
        for decision in request.decisions:
            if decision.item_id in decision_map:
                raise ChangeSetStateError(
                    f"duplicate mandatory usage decision for item_id={decision.item_id}"
                )
            decision_map[decision.item_id] = decision
        plan_ids = {item.item_id for item in plan.items}
        unknown = sorted(set(decision_map) - plan_ids)
        if unknown:
            raise ChangeSetStateError(
                "mandatory usage decisions contain unknown item_id values: "
                + ", ".join(unknown[:8])
            )
        missing = sorted(plan_ids - set(decision_map))
        if missing:
            raise ChangeSetStateError(
                "mandatory usage closure finalize requires decisions for all plan items; missing: "
                + ", ".join(missing[:8])
            )

        existing = self._read_original_nlcl_mandatory_usage_closure(
            artifact_root / "nlcl-original-mandatory-usage-closure-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_mandatory_usage_plan_receipt_hash == plan.receipt_hash
            and existing.outcome == plan.outcome
        ):
            # Same plan + same ordered decision material replays.
            existing_pairs = {
                (item.item_id, item.status, item.note) for item in existing.items
            }
            requested_pairs = {
                (
                    decision.item_id,
                    decision.status,
                    decision.note,
                )
                for decision in request.decisions
            }
            if existing_pairs == requested_pairs:
                history = self._read_original_nlcl_mandatory_usage_closure_history(
                    artifact_root / "nlcl-original-mandatory-usage-closure-history.json"
                )
                return NlclOriginalMandatoryUsageClosureResponse(
                    change_set_id=record.change_set_id,
                    receipt=existing,
                    history=history,
                )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        closed_items: list[NlclOriginalMandatoryUsageClosureItem] = []
        for item in plan.items:
            decision = decision_map[item.item_id]
            closed_items.append(
                NlclOriginalMandatoryUsageClosureItem(
                    item_id=item.item_id,
                    kind=item.kind,
                    title=item.title,
                    source_relative_path=item.source_relative_path,
                    required_action=item.required_action,
                    status=decision.status,
                    note=decision.note,
                )
            )

        ts = _now().isoformat().replace("+00:00", "Z")
        chapter_id = plan.chapter_id
        closure_payload = {
            "schema": "zhimeng.product-bridge-mandatory-usage-closure.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": plan.canonical_chapter_key,
            "outcome": plan.outcome,
            "generated_at": ts,
            "overall_status": "closed",
            "mandatory_usage_closure_closed": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_mandatory_usage_closure_author_decided",
            "source": {
                "mandatory_usage_plan_receipt_id": plan.receipt_id,
                "mandatory_usage_plan_receipt_hash": plan.receipt_hash,
                "truth_content_extract_receipt_id": plan.nlcl_original_truth_content_extract_receipt_id,
                "truth_content_extract_receipt_hash": plan.nlcl_original_truth_content_extract_receipt_hash,
                "novel_state_sync_receipt_id": plan.nlcl_original_novel_state_sync_receipt_id,
                "novel_state_sync_receipt_hash": plan.nlcl_original_novel_state_sync_receipt_hash,
            },
            "items": [item.model_dump(mode="json") for item in closed_items],
            "writer_must_check": [
                "closure closed only means author decided every mandatory item",
                "route_validated remains false",
                "formal_draft_allowed remains false",
            ],
        }
        counts = {
            "used": 0,
            "unused": 0,
            "missed": 0,
            "suppressed_for_project_truth": 0,
        }
        for item in closed_items:
            counts[item.status] = counts.get(item.status, 0) + 1
        md_lines = [
            f"# Mandatory usage closure · {chapter_id}",
            "",
            "- overall_status: closed",
            "- mandatory_usage_closure_closed: true",
            "- route_validated: false",
            "- formal_draft_allowed: false",
            f"- used={counts['used']} unused={counts['unused']} missed={counts['missed']} suppressed={counts['suppressed_for_project_truth']}",
            "",
            "## Decided items",
        ]
        for item in closed_items:
            note = f" — {item.note}" if item.note else ""
            md_lines.append(
                f"- [{item.status}] ({item.kind}) {item.title}{note}"
            )
        md_text = "\n".join(md_lines) + "\n"

        stage_slug = "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_mandatory_usage_closure_latest.json",
                f"chapter_context/{chapter_id}_mandatory_usage_closure_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_mandatory_usage_closure_latest.md",
                f"chapter_context/{chapter_id}_mandatory_usage_closure_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_mandatory_usage_closure_{stage_slug}.json",
                f"chapter_context/{chapter_id}_mandatory_usage_closure_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_mandatory_usage_closure_{stage_slug}.md",
                f"chapter_context/{chapter_id}_mandatory_usage_closure_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalMandatoryUsageClosureWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"closure path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"closure file must be a regular file: {path}")
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, closure_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalMandatoryUsageClosureWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_mandatory_usage_plan_receipt_id": plan.receipt_id,
            "nlcl_original_mandatory_usage_plan_receipt_hash": plan.receipt_hash,
            "nlcl_original_truth_content_extract_receipt_id": plan.nlcl_original_truth_content_extract_receipt_id,
            "nlcl_original_truth_content_extract_receipt_hash": plan.nlcl_original_truth_content_extract_receipt_hash,
            "nlcl_original_novel_state_sync_receipt_id": plan.nlcl_original_novel_state_sync_receipt_id,
            "nlcl_original_novel_state_sync_receipt_hash": plan.nlcl_original_novel_state_sync_receipt_hash,
            "outcome": plan.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": plan.canonical_chapter_key,
            "items": [item.model_dump(mode="json") for item in closed_items],
            "written_files": [item.model_dump(mode="json") for item in written],
            "closure_relative_path": f"chapter_context/{chapter_id}_mandatory_usage_closure_latest.json",
            "overall_status": "closed",
            "record_mode": "product_bridge_mandatory_usage_closure_author_decided",
            "original_nlcl_novel_state_synced": True,
            "original_nlcl_truth_matrix_synced": True,
            "truth_content_extracted": True,
            "mandatory_usage_plan_synced": True,
            "mandatory_usage_closure_closed": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-mandatory-usage-closure-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalMandatoryUsageClosureReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-mandatory-usage-closure-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-mandatory-usage-closure-history.json"
        history = self._read_original_nlcl_mandatory_usage_closure_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalMandatoryUsageClosureResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_mandatory_usage_closure(
        self,
        path: Path,
    ) -> NlclOriginalMandatoryUsageClosureReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalMandatoryUsageClosureReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_mandatory_usage_closure_history(
        self,
        path: Path,
    ) -> list[NlclOriginalMandatoryUsageClosureReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalMandatoryUsageClosureReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalMandatoryUsageClosureReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history



    def get_original_nlcl_operation_package_like(
        self,
        change_set_id: str,
    ) -> NlclOriginalOperationPackageLikeResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_operation_package_like(
            artifact_root / "nlcl-original-operation-package-like-receipt.json"
        )
        history = self._read_original_nlcl_operation_package_like_history(
            artifact_root / "nlcl-original-operation-package-like-history.json"
        )
        return NlclOriginalOperationPackageLikeResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_operation_package_like(
        self,
        change_set_id: str,
        request: NlclOriginalOperationPackageLikeRequest,
    ) -> NlclOriginalOperationPackageLikeResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL operation_package_like requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        closure = self._read_original_nlcl_mandatory_usage_closure(
            artifact_root / "nlcl-original-mandatory-usage-closure-receipt.json"
        )
        if closure is None:
            raise ChangeSetStateError(
                "original-compatible NLCL mandatory usage closure is required before operation_package_like"
            )
        if (
            closure.receipt_hash
            != request.expected_nlcl_original_mandatory_usage_closure_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original mandatory usage closure receipt hash does not match the expected value"
            )
        if closure.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original mandatory usage closure receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if closure.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original mandatory usage closure outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_operation_package_like(
            artifact_root / "nlcl-original-operation-package-like-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_mandatory_usage_closure_receipt_hash
            == closure.receipt_hash
            and existing.outcome == closure.outcome
        ):
            history = self._read_original_nlcl_operation_package_like_history(
                artifact_root / "nlcl-original-operation-package-like-history.json"
            )
            return NlclOriginalOperationPackageLikeResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        def _load_state(name: str) -> dict[str, object]:
            path = project_root / "state" / name
            if not path.is_file():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

        character_state = _load_state("character_state.json")
        foreshadow_state = _load_state("foreshadowing_state.json")
        truth_info = _load_state("truth_info_boundary.json")
        characters = character_state.get("characters")
        character_refs: list[dict[str, object]] = []
        if isinstance(characters, dict):
            for key, value in list(characters.items())[:32]:
                if not isinstance(value, dict):
                    continue
                character_refs.append(
                    {
                        "source_id": f"state:character:{key}",
                        "name": str(value.get("name") or key),
                        "status_summary": str(value.get("status_summary") or "")[:300],
                    }
                )
        truth_refs = [
            {"source_id": "state/truth_character_matrix.json", "kind": "truth_matrix"},
            {"source_id": "state/truth_foreshadow_lifecycle.json", "kind": "truth_matrix"},
            {"source_id": "state/truth_info_boundary.json", "kind": "truth_matrix"},
        ]
        open_debts = foreshadow_state.get("open_debts")
        if isinstance(open_debts, list):
            for raw in open_debts[:16]:
                if isinstance(raw, dict):
                    truth_refs.append(
                        {
                            "source_id": str(raw.get("id") or raw.get("title") or "foreshadow"),
                            "kind": "open_debt",
                            "title": str(raw.get("title") or "")[:200],
                        }
                    )
        ts = _now().isoformat().replace("+00:00", "Z")
        chapter_id = closure.chapter_id
        package_body = {
            "schema": "zhimeng.product-bridge-operation-package-like.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": closure.canonical_chapter_key,
            "outcome": closure.outcome,
            "generated_at": ts,
            "readiness": "risk_draft_only",
            "missing_requirements": [
                "formal_draft_authorization",
                "route_validated",
                "full_library_layout",
            ],
            "must_obey_truth": truth_refs[:64],
            "current_character_state": character_refs,
            "scene_contract": {
                "status": "bound" if character_refs or truth_refs else "unbound",
                "evidence_source_ids": [
                    item["source_id"] for item in character_refs[:8]
                ],
            },
            "style_output_contract": {
                "status": "scaffold",
                "note": "product bridge does not claim style card used",
            },
            "library_activation_budget": {
                "status": "not_full_library",
                "note": "library 全量布局仍未完成",
            },
            "failure_routing": [
                {
                    "failure_signature": "L1_PROJECT_TRUTH",
                    "rewrite_from": "state/*.json and 01-09 truth files",
                },
                {
                    "failure_signature": "L0_USAGE_CLOSURE",
                    "rewrite_from": "mandatory usage closure decisions",
                },
            ],
            "mandatory_usage_closure": {
                "receipt_id": closure.receipt_id,
                "receipt_hash": closure.receipt_hash,
                "closed": True,
                "item_count": len(closure.items),
            },
            "info_boundary_entity_count": (
                len(truth_info.get("entities", {}))
                if isinstance(truth_info.get("entities"), dict)
                else 0
            ),
            "formal_draft_allowed": False,
            "route_validated": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_operation_package_like_scaffold",
        }
        package_hash = canonical_json_hash(package_body)
        operation_package_id = f"product-bridge-operation-package-{package_hash[:32]}"
        package_payload = {
            **package_body,
            "operation_package_id": operation_package_id,
            "package_hash": package_hash,
        }
        md_text = (
            f"# Operation package like · {chapter_id}\n\n"
            f"- readiness: risk_draft_only\n"
            f"- formal_draft_allowed: false\n"
            f"- route_validated: false\n"
            f"- mandatory_usage_closure_closed: true\n"
            f"- operation_package_id: {operation_package_id}\n"
            f"- package_hash: {package_hash}\n"
            f"- characters: {len(character_refs)}\n"
            f"- missing_requirements: formal_draft_authorization, route_validated, full_library_layout\n"
        )
        stage_slug = "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_operation_package_like_latest.json",
                f"chapter_context/{chapter_id}_operation_package_like_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_operation_package_like_latest.md",
                f"chapter_context/{chapter_id}_operation_package_like_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_operation_package_like_{stage_slug}.json",
                f"chapter_context/{chapter_id}_operation_package_like_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_operation_package_like_{stage_slug}.md",
                f"chapter_context/{chapter_id}_operation_package_like_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalOperationPackageLikeWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"package path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(f"package file must be a regular file: {path}")
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, package_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalOperationPackageLikeWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-operation-package-like-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_mandatory_usage_closure_receipt_id": closure.receipt_id,
            "nlcl_original_mandatory_usage_closure_receipt_hash": closure.receipt_hash,
            "nlcl_original_mandatory_usage_plan_receipt_id": closure.nlcl_original_mandatory_usage_plan_receipt_id,
            "nlcl_original_mandatory_usage_plan_receipt_hash": closure.nlcl_original_mandatory_usage_plan_receipt_hash,
            "outcome": closure.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": closure.canonical_chapter_key,
            "operation_package_id": operation_package_id,
            "package_hash": package_hash,
            "readiness": "risk_draft_only",
            "written_files": [item.model_dump(mode="json") for item in written],
            "package_relative_path": f"chapter_context/{chapter_id}_operation_package_like_latest.json",
            "record_mode": "product_bridge_operation_package_like_scaffold",
            "original_nlcl_novel_state_synced": True,
            "truth_content_extracted": True,
            "mandatory_usage_plan_synced": True,
            "mandatory_usage_closure_closed": True,
            "operation_package_like_synced": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-operation-package-like-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalOperationPackageLikeReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-operation-package-like-receipt.json",
            payload,
        )
        history_path = artifact_root / "nlcl-original-operation-package-like-history.json"
        history = self._read_original_nlcl_operation_package_like_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalOperationPackageLikeResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_operation_package_like(
        self,
        path: Path,
    ) -> NlclOriginalOperationPackageLikeReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalOperationPackageLikeReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_operation_package_like_history(
        self,
        path: Path,
    ) -> list[NlclOriginalOperationPackageLikeReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalOperationPackageLikeReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalOperationPackageLikeReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history



    def get_original_nlcl_library_layout_inventory(
        self,
        change_set_id: str,
    ) -> NlclOriginalLibraryLayoutInventoryResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_library_layout_inventory(
            artifact_root / "nlcl-original-library-layout-inventory-receipt.json"
        )
        history = self._read_original_nlcl_library_layout_inventory_history(
            artifact_root / "nlcl-original-library-layout-inventory-history.json"
        )
        return NlclOriginalLibraryLayoutInventoryResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_library_layout_inventory(
        self,
        change_set_id: str,
        request: NlclOriginalLibraryLayoutInventoryRequest,
    ) -> NlclOriginalLibraryLayoutInventoryResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL library layout inventory requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        package = self._read_original_nlcl_operation_package_like(
            artifact_root / "nlcl-original-operation-package-like-receipt.json"
        )
        if package is None:
            raise ChangeSetStateError(
                "original-compatible NLCL operation_package_like is required before library layout inventory"
            )
        if (
            package.receipt_hash
            != request.expected_nlcl_original_operation_package_like_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original operation_package_like receipt hash does not match the expected value"
            )
        if package.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original operation_package_like receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if package.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original operation_package_like outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_library_layout_inventory(
            artifact_root / "nlcl-original-library-layout-inventory-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_operation_package_like_receipt_hash
            == package.receipt_hash
            and existing.outcome == package.outcome
        ):
            history = self._read_original_nlcl_library_layout_inventory_history(
                artifact_root / "nlcl-original-library-layout-inventory-history.json"
            )
            return NlclOriginalLibraryLayoutInventoryResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        section_labels = {
            1: "项目定位",
            2: "世界观设定",
            3: "人物设定",
            4: "剧情大纲",
            5: "正文内容",
            6: "写作规范",
            7: "运营数据",
            8: "素材库",
            9: "完本复盘",
        }
        slots: list[NlclOriginalLibraryLayoutSlot] = []
        for index, label in section_labels.items():
            prefix = f"{index:02d}_"
            found_dir: Path | None = None
            if project_root.is_dir():
                for child in sorted(project_root.iterdir(), key=lambda p: p.name.casefold()):
                    if (
                        child.is_dir()
                        and not _is_link_or_reparse(child)
                        and child.name.startswith(prefix)
                    ):
                        found_dir = child
                        break
            file_count = 0
            if found_dir is not None:
                try:
                    for path in found_dir.rglob("*"):
                        if path.is_file() and not _is_link_or_reparse(path):
                            file_count += 1
                            if file_count >= 1000:
                                break
                except OSError:
                    file_count = 0
            present = found_dir is not None and file_count > 0
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"section_{index:02d}",
                    kind=f"section_{index:02d}",  # type: ignore[arg-type]
                    label=label,
                    relative_path=found_dir.relative_to(project_root).as_posix()
                    if found_dir is not None
                    else None,
                    present=present,
                    file_count=file_count,
                    note="" if present else "section missing or empty",
                )
            )

        state_files = [
            "state/project_status.json",
            "state/character_state.json",
            "state/plot_state.json",
            "state/foreshadowing_state.json",
            "state/truth_character_matrix.json",
            "state/truth_foreshadow_lifecycle.json",
            "state/truth_info_boundary.json",
        ]
        for rel in state_files:
            path = project_root / Path(rel)
            present = path.is_file() and not _is_link_or_reparse(path)
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"state:{Path(rel).name}",
                    kind="state_file",
                    label=Path(rel).name,
                    relative_path=rel,
                    present=present,
                    file_count=1 if present else 0,
                    note="" if present else "state file missing",
                )
            )

        chapter_id = package.chapter_id
        context_required = [
            f"chapter_context/{chapter_id}_prewrite_context_report_latest.json",
            f"chapter_context/{chapter_id}_mandatory_usage_plan_latest.json",
            f"chapter_context/{chapter_id}_mandatory_usage_closure_latest.json",
            f"chapter_context/{chapter_id}_operation_package_like_latest.json",
        ]
        for rel in context_required:
            path = project_root / Path(rel)
            present = path.is_file() and not _is_link_or_reparse(path)
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"context:{Path(rel).name}",
                    kind="chapter_context",
                    label=Path(rel).name,
                    relative_path=rel,
                    present=present,
                    file_count=1 if present else 0,
                    note="" if present else "chapter_context artifact missing",
                )
            )

        present_count = sum(1 for item in slots if item.present)
        missing_count = sum(1 for item in slots if not item.present)
        ts = _now().isoformat().replace("+00:00", "Z")
        inventory_payload = {
            "schema": "zhimeng.product-bridge-library-layout-inventory.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": package.canonical_chapter_key,
            "outcome": package.outcome,
            "generated_at": ts,
            "library_layout_inventoried": True,
            "library_layout_complete": False,
            "whole_library_load_forbidden": True,
            "present_slot_count": present_count,
            "missing_slot_count": missing_count,
            "slots": [item.model_dump(mode="json") for item in slots],
            "library_activation_budget": {
                "status": "not_full_library",
                "activated_source_ids": [
                    item.relative_path
                    for item in slots
                    if item.present and item.relative_path
                ][:64],
                "note": "inventory only; whole-library load remains forbidden",
            },
            "formal_draft_allowed": False,
            "route_validated": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_library_layout_inventory",
            "source": {
                "operation_package_like_receipt_id": package.receipt_id,
                "operation_package_like_receipt_hash": package.receipt_hash,
            },
        }
        md_lines = [
            f"# Library layout inventory · {chapter_id}",
            "",
            f"- library_layout_inventoried: true",
            f"- library_layout_complete: false",
            f"- whole_library_load_forbidden: true",
            f"- present: {present_count}",
            f"- missing: {missing_count}",
            "",
            "## Slots",
        ]
        for item in slots:
            status = "present" if item.present else "missing"
            path = item.relative_path or "-"
            md_lines.append(
                f"- [{status}] {item.slot_id} · {item.label} · {path} · files={item.file_count}"
            )
        md_text = "\n".join(md_lines) + "\n"

        stage_slug = "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_library_layout_inventory_latest.json",
                f"chapter_context/{chapter_id}_library_layout_inventory_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_library_layout_inventory_latest.md",
                f"chapter_context/{chapter_id}_library_layout_inventory_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_library_layout_inventory_{stage_slug}.json",
                f"chapter_context/{chapter_id}_library_layout_inventory_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_library_layout_inventory_{stage_slug}.md",
                f"chapter_context/{chapter_id}_library_layout_inventory_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalLibraryLayoutInventoryWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"inventory path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(
                    f"inventory file must be a regular file: {path}"
                )
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, inventory_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalLibraryLayoutInventoryWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-library-layout-inventory-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_operation_package_like_receipt_id": package.receipt_id,
            "nlcl_original_operation_package_like_receipt_hash": package.receipt_hash,
            "outcome": package.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": package.canonical_chapter_key,
            "slots": [item.model_dump(mode="json") for item in slots],
            "present_slot_count": present_count,
            "missing_slot_count": missing_count,
            "written_files": [item.model_dump(mode="json") for item in written],
            "inventory_relative_path": f"chapter_context/{chapter_id}_library_layout_inventory_latest.json",
            "record_mode": "product_bridge_library_layout_inventory",
            "operation_package_like_synced": True,
            "library_layout_inventoried": True,
            "library_layout_complete": False,
            "whole_library_load_forbidden": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-library-layout-inventory-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalLibraryLayoutInventoryReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-library-layout-inventory-receipt.json",
            payload,
        )
        history_path = (
            artifact_root / "nlcl-original-library-layout-inventory-history.json"
        )
        history = self._read_original_nlcl_library_layout_inventory_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalLibraryLayoutInventoryResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_library_layout_inventory(
        self,
        path: Path,
    ) -> NlclOriginalLibraryLayoutInventoryReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalLibraryLayoutInventoryReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_library_layout_inventory_history(
        self,
        path: Path,
    ) -> list[NlclOriginalLibraryLayoutInventoryReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalLibraryLayoutInventoryReceipt] = []
        for item in payload:
            try:
                history.append(
                    NlclOriginalLibraryLayoutInventoryReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history


    def get_original_nlcl_library_layout_completeness(
        self,
        change_set_id: str,
    ) -> NlclOriginalLibraryLayoutCompletenessResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_library_layout_completeness(
            artifact_root / "nlcl-original-library-layout-completeness-receipt.json"
        )
        history = self._read_original_nlcl_library_layout_completeness_history(
            artifact_root / "nlcl-original-library-layout-completeness-history.json"
        )
        return NlclOriginalLibraryLayoutCompletenessResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_library_layout_completeness(
        self,
        change_set_id: str,
        request: NlclOriginalLibraryLayoutCompletenessRequest,
    ) -> NlclOriginalLibraryLayoutCompletenessResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL library layout completeness requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        inventory = self._read_original_nlcl_library_layout_inventory(
            artifact_root / "nlcl-original-library-layout-inventory-receipt.json"
        )
        if inventory is None:
            raise ChangeSetStateError(
                "original-compatible NLCL library layout inventory is required before completeness assessment"
            )
        if (
            inventory.receipt_hash
            != request.expected_nlcl_original_library_layout_inventory_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original library layout inventory receipt hash does not match the expected value"
            )
        if inventory.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original library layout inventory receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if inventory.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original library layout inventory outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_library_layout_completeness(
            artifact_root / "nlcl-original-library-layout-completeness-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_library_layout_inventory_receipt_hash
            == inventory.receipt_hash
            and existing.outcome == inventory.outcome
        ):
            history = self._read_original_nlcl_library_layout_completeness_history(
                artifact_root / "nlcl-original-library-layout-completeness-history.json"
            )
            return NlclOriginalLibraryLayoutCompletenessResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        slots = self._collect_original_nlcl_library_layout_required_slots(
            project_root=project_root,
            chapter_id=inventory.chapter_id,
        )
        present_count = sum(1 for item in slots if item.present)
        missing_count = sum(1 for item in slots if not item.present)
        complete = missing_count == 0
        completeness_status = "complete" if complete else "incomplete"
        ts = _now().isoformat().replace("+00:00", "Z")
        chapter_id = inventory.chapter_id
        assessment_payload = {
            "schema": "zhimeng.product-bridge-library-layout-completeness.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": inventory.canonical_chapter_key,
            "outcome": inventory.outcome,
            "generated_at": ts,
            "library_layout_inventoried": True,
            "library_layout_assessed": True,
            "library_layout_complete": complete,
            "whole_library_load_forbidden": True,
            "completeness_status": completeness_status,
            "required_slot_count": len(slots),
            "present_required_slot_count": present_count,
            "missing_required_slot_count": missing_count,
            "slots": [item.model_dump(mode="json") for item in slots],
            "library_activation_budget": {
                "status": "complete_layout_only" if complete else "not_full_library",
                "activated_source_ids": [
                    item.relative_path
                    for item in slots
                    if item.present and item.relative_path
                ][:64],
                "note": (
                    "layout complete for required slots; whole-library load remains forbidden"
                    if complete
                    else "layout incomplete; whole-library load remains forbidden"
                ),
            },
            "formal_draft_allowed": False,
            "route_validated": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_library_layout_completeness",
            "source": {
                "library_layout_inventory_receipt_id": inventory.receipt_id,
                "library_layout_inventory_receipt_hash": inventory.receipt_hash,
            },
        }
        md_lines = [
            f"# Library layout completeness · {chapter_id}",
            "",
            f"- library_layout_assessed: true",
            f"- library_layout_complete: {str(complete).lower()}",
            f"- whole_library_load_forbidden: true",
            f"- completeness_status: {completeness_status}",
            f"- present: {present_count}",
            f"- missing: {missing_count}",
            "",
            "## Required slots",
        ]
        for item in slots:
            status = "present" if item.present else "missing"
            path_text = item.relative_path or "-"
            md_lines.append(
                f"- [{status}] {item.slot_id} · {item.label} · {path_text} · files={item.file_count}"
            )
        md_text = "\n".join(md_lines) + "\n"

        stage_slug = "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_library_layout_completeness_latest.json",
                f"chapter_context/{chapter_id}_library_layout_completeness_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_library_layout_completeness_latest.md",
                f"chapter_context/{chapter_id}_library_layout_completeness_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_library_layout_completeness_{stage_slug}.json",
                f"chapter_context/{chapter_id}_library_layout_completeness_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_library_layout_completeness_{stage_slug}.md",
                f"chapter_context/{chapter_id}_library_layout_completeness_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalLibraryLayoutCompletenessWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"completeness path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(
                    f"completeness file must be a regular file: {path}"
                )
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, assessment_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalLibraryLayoutCompletenessWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-library-layout-completeness-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_library_layout_inventory_receipt_id": inventory.receipt_id,
            "nlcl_original_library_layout_inventory_receipt_hash": inventory.receipt_hash,
            "outcome": inventory.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": inventory.canonical_chapter_key,
            "slots": [item.model_dump(mode="json") for item in slots],
            "required_slot_count": len(slots),
            "present_required_slot_count": present_count,
            "missing_required_slot_count": missing_count,
            "completeness_status": completeness_status,
            "written_files": [item.model_dump(mode="json") for item in written],
            "assessment_relative_path": f"chapter_context/{chapter_id}_library_layout_completeness_latest.json",
            "record_mode": "product_bridge_library_layout_completeness",
            "library_layout_inventoried": True,
            "library_layout_assessed": True,
            "library_layout_complete": complete,
            "whole_library_load_forbidden": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-library-layout-completeness-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalLibraryLayoutCompletenessReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-library-layout-completeness-receipt.json",
            payload,
        )
        history_path = (
            artifact_root / "nlcl-original-library-layout-completeness-history.json"
        )
        history = self._read_original_nlcl_library_layout_completeness_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalLibraryLayoutCompletenessResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _collect_original_nlcl_library_layout_required_slots(
        self,
        *,
        project_root: Path,
        chapter_id: str,
    ) -> list[NlclOriginalLibraryLayoutSlot]:
        section_labels = {
            1: "项目定位",
            2: "世界观设定",
            3: "人物设定",
            4: "剧情大纲",
            5: "正文内容",
            6: "写作规范",
            7: "运营数据",
            8: "素材库",
            9: "完本复盘",
        }
        slots: list[NlclOriginalLibraryLayoutSlot] = []
        for index, label in section_labels.items():
            prefix = f"{index:02d}_"
            found_dir: Path | None = None
            if project_root.is_dir():
                for child in sorted(project_root.iterdir(), key=lambda p: p.name.casefold()):
                    if (
                        child.is_dir()
                        and not _is_link_or_reparse(child)
                        and child.name.startswith(prefix)
                    ):
                        found_dir = child
                        break
            file_count = 0
            if found_dir is not None:
                try:
                    for path in found_dir.rglob("*"):
                        if path.is_file() and not _is_link_or_reparse(path):
                            file_count += 1
                            if file_count >= 1000:
                                break
                except OSError:
                    file_count = 0
            present = found_dir is not None and file_count > 0
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"section_{index:02d}",
                    kind=f"section_{index:02d}",  # type: ignore[arg-type]
                    label=label,
                    relative_path=found_dir.relative_to(project_root).as_posix()
                    if found_dir is not None
                    else None,
                    present=present,
                    file_count=file_count,
                    note="" if present else "section missing or empty",
                )
            )

        state_files = [
            "state/project_status.json",
            "state/character_state.json",
            "state/plot_state.json",
            "state/foreshadowing_state.json",
            "state/truth_character_matrix.json",
            "state/truth_foreshadow_lifecycle.json",
            "state/truth_info_boundary.json",
        ]
        for rel in state_files:
            path = project_root / Path(rel)
            present = path.is_file() and not _is_link_or_reparse(path)
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"state:{Path(rel).name}",
                    kind="state_file",
                    label=Path(rel).name,
                    relative_path=rel,
                    present=present,
                    file_count=1 if present else 0,
                    note="" if present else "state file missing",
                )
            )

        context_required = [
            f"chapter_context/{chapter_id}_mandatory_usage_plan_latest.json",
            f"chapter_context/{chapter_id}_mandatory_usage_closure_latest.json",
            f"chapter_context/{chapter_id}_operation_package_like_latest.json",
            f"chapter_context/{chapter_id}_library_layout_inventory_latest.json",
        ]
        for rel in context_required:
            path = project_root / Path(rel)
            present = path.is_file() and not _is_link_or_reparse(path)
            slots.append(
                NlclOriginalLibraryLayoutSlot(
                    slot_id=f"context:{Path(rel).name}",
                    kind="chapter_context",
                    label=Path(rel).name,
                    relative_path=rel,
                    present=present,
                    file_count=1 if present else 0,
                    note="" if present else "chapter_context artifact missing",
                )
            )
        return slots

    def _read_original_nlcl_library_layout_completeness(
        self,
        path: Path,
    ) -> NlclOriginalLibraryLayoutCompletenessReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalLibraryLayoutCompletenessReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_library_layout_completeness_history(
        self,
        path: Path,
    ) -> list[NlclOriginalLibraryLayoutCompletenessReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalLibraryLayoutCompletenessReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(
                    NlclOriginalLibraryLayoutCompletenessReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history


    def get_original_nlcl_bounded_library_activation(
        self,
        change_set_id: str,
    ) -> NlclOriginalBoundedLibraryActivationResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_original_nlcl_bounded_library_activation(
            artifact_root / "nlcl-original-bounded-library-activation-receipt.json"
        )
        history = self._read_original_nlcl_bounded_library_activation_history(
            artifact_root / "nlcl-original-bounded-library-activation-history.json"
        )
        return NlclOriginalBoundedLibraryActivationResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def export_original_nlcl_bounded_library_activation(
        self,
        change_set_id: str,
        request: NlclOriginalBoundedLibraryActivationRequest,
    ) -> NlclOriginalBoundedLibraryActivationResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "original NLCL bounded library activation requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        completeness = self._read_original_nlcl_library_layout_completeness(
            artifact_root / "nlcl-original-library-layout-completeness-receipt.json"
        )
        if completeness is None:
            raise ChangeSetStateError(
                "original-compatible NLCL library layout completeness is required before bounded activation"
            )
        if (
            completeness.receipt_hash
            != request.expected_nlcl_original_library_layout_completeness_receipt_hash
        ):
            raise ChangeSetConflictError(
                "NLCL original library layout completeness receipt hash does not match the expected value"
            )
        if completeness.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "NLCL original library layout completeness receipt is not bound to this ChangeSet"
            )
        expected_outcome = (
            "applied" if record.status == ChangeSetStatus.APPLIED else "rolled_back"
        )
        if completeness.outcome != expected_outcome:
            raise ChangeSetStateError(
                "NLCL original library layout completeness outcome does not match ChangeSet status"
            )

        existing = self._read_original_nlcl_bounded_library_activation(
            artifact_root / "nlcl-original-bounded-library-activation-receipt.json"
        )
        if (
            existing is not None
            and existing.nlcl_original_library_layout_completeness_receipt_hash
            == completeness.receipt_hash
            and existing.outcome == completeness.outcome
            and existing.max_sources == request.max_sources
        ):
            history = self._read_original_nlcl_bounded_library_activation_history(
                artifact_root / "nlcl-original-bounded-library-activation-history.json"
            )
            return NlclOriginalBoundedLibraryActivationResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        chapter_context_root = project_root / "chapter_context"
        if chapter_context_root.exists() and not chapter_context_root.is_dir():
            raise ChangeSetStateError("chapter_context path must be a directory")
        if chapter_context_root.exists() and _is_link_or_reparse(chapter_context_root):
            raise ChangeSetStateError(
                "chapter_context path cannot be a link or reparse point"
            )
        chapter_context_root.mkdir(parents=True, exist_ok=True)

        present_slots = [
            item
            for item in completeness.slots
            if item.present and item.relative_path
        ]
        # Prefer state + chapter_context first, then sections, for bounded relevance.
        kind_rank = {
            "state_file": 0,
            "chapter_context": 1,
            "section_03": 2,
            "section_04": 3,
            "section_02": 4,
            "section_05": 5,
            "section_01": 6,
            "section_06": 7,
            "section_07": 8,
            "section_08": 9,
            "section_09": 10,
        }
        present_slots = sorted(
            present_slots,
            key=lambda item: (kind_rank.get(item.kind, 99), item.slot_id),
        )
        max_sources = int(request.max_sources)
        max_characters = 120_000
        max_tokens = 30_000
        activated: list[NlclOriginalBoundedLibraryActivationSource] = []
        used_characters = 0
        used_tokens = 0
        for slot in present_slots:
            if len(activated) >= max_sources:
                break
            rel = slot.relative_path
            assert rel is not None
            path = project_root / Path(rel)
            char_count = 0
            if path.is_file() and not _is_link_or_reparse(path):
                try:
                    raw = path.read_bytes()
                    # Bound per-file contribution.
                    sample = raw[: 64_000]
                    char_count = len(sample.decode("utf-8", errors="ignore"))
                except OSError:
                    char_count = 0
            elif path.is_dir() and not _is_link_or_reparse(path):
                try:
                    total = 0
                    for child in sorted(path.rglob("*"), key=lambda p: p.as_posix()):
                        if not child.is_file() or _is_link_or_reparse(child):
                            continue
                        try:
                            raw = child.read_bytes()[: 16_000]
                            total += len(raw.decode("utf-8", errors="ignore"))
                        except OSError:
                            continue
                        if total >= 64_000:
                            break
                    char_count = min(total, 64_000)
                except OSError:
                    char_count = 0
            token_estimate = max(1, char_count // 4) if char_count else 0
            if used_characters + char_count > max_characters:
                continue
            if used_tokens + token_estimate > max_tokens:
                continue
            source_id = rel
            activated.append(
                NlclOriginalBoundedLibraryActivationSource(
                    source_id=source_id,
                    slot_id=slot.slot_id,
                    kind=slot.kind,
                    relative_path=rel,
                    character_count=char_count,
                    token_estimate=token_estimate,
                )
            )
            used_characters += char_count
            used_tokens += token_estimate

        chapter_id = completeness.chapter_id
        ts = _now().isoformat().replace("+00:00", "Z")
        activation_payload = {
            "schema": "zhimeng.product-bridge-bounded-library-activation.v1",
            "chapter_id": chapter_id,
            "canonical_chapter_key": completeness.canonical_chapter_key,
            "outcome": completeness.outcome,
            "generated_at": ts,
            "library_layout_complete": completeness.library_layout_complete,
            "bounded_library_activated": True,
            "whole_library_load": False,
            "whole_library_load_forbidden": True,
            "max_sources": max_sources,
            "activated_count": len(activated),
            "candidate_present_count": len(present_slots),
            "library_activation_budget": {
                "max_characters": max_characters,
                "used_characters": used_characters,
                "max_tokens": max_tokens,
                "used_tokens": used_tokens,
                "activated_source_ids": [item.source_id for item in activated],
            },
            "activated_sources": [item.model_dump(mode="json") for item in activated],
            "formal_draft_allowed": False,
            "route_validated": False,
            "quality_assessed": False,
            "record_mode": "product_bridge_bounded_library_activation",
            "source": {
                "library_layout_completeness_receipt_id": completeness.receipt_id,
                "library_layout_completeness_receipt_hash": completeness.receipt_hash,
            },
        }
        md_lines = [
            f"# Bounded library activation · {chapter_id}",
            "",
            f"- bounded_library_activated: true",
            f"- whole_library_load: false",
            f"- whole_library_load_forbidden: true",
            f"- library_layout_complete: {str(completeness.library_layout_complete).lower()}",
            f"- activated: {len(activated)} / max {max_sources}",
            f"- budget chars: {used_characters}/{max_characters}",
            f"- budget tokens: {used_tokens}/{max_tokens}",
            "",
            "## Activated sources",
        ]
        if not activated:
            md_lines.append("- none")
        for item in activated:
            md_lines.append(
                f"- {item.source_id} · slot={item.slot_id} · chars={item.character_count} · tokens~{item.token_estimate}"
            )
        md_text = "\n".join(md_lines) + "\n"

        stage_slug = "post_writeback"
        path_specs = [
            (
                chapter_context_root / f"{chapter_id}_bounded_library_activation_latest.json",
                f"chapter_context/{chapter_id}_bounded_library_activation_latest.json",
                "json_latest",
                "json",
            ),
            (
                chapter_context_root / f"{chapter_id}_bounded_library_activation_latest.md",
                f"chapter_context/{chapter_id}_bounded_library_activation_latest.md",
                "md_latest",
                "md",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_bounded_library_activation_{stage_slug}.json",
                f"chapter_context/{chapter_id}_bounded_library_activation_{stage_slug}.json",
                "json_stage",
                "json",
            ),
            (
                chapter_context_root
                / f"{chapter_id}_bounded_library_activation_{stage_slug}.md",
                f"chapter_context/{chapter_id}_bounded_library_activation_{stage_slug}.md",
                "md_stage",
                "md",
            ),
        ]
        written: list[NlclOriginalBoundedLibraryActivationWrittenFile] = []
        for path, relative, kind, mode in path_specs:
            if not _is_within(path, project_root):
                raise ChangeSetStateError(f"activation path escapes project root: {path}")
            if path.exists() and (_is_link_or_reparse(path) or not path.is_file()):
                raise ChangeSetStateError(
                    f"activation file must be a regular file: {path}"
                )
            created = not path.exists()
            if mode == "json":
                _atomic_write_json(path, activation_payload)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(md_text, encoding="utf-8")
                tmp.replace(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            written.append(
                NlclOriginalBoundedLibraryActivationWrittenFile(
                    relative_path=relative,
                    source_hash=digest,
                    kind=kind,  # type: ignore[arg-type]
                    created=created,
                )
            )

        material = {
            "schema": "zhimeng.nlcl-original-bounded-library-activation-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "nlcl_original_library_layout_completeness_receipt_id": completeness.receipt_id,
            "nlcl_original_library_layout_completeness_receipt_hash": completeness.receipt_hash,
            "outcome": completeness.outcome,
            "chapter_id": chapter_id,
            "canonical_chapter_key": completeness.canonical_chapter_key,
            "activated_sources": [item.model_dump(mode="json") for item in activated],
            "activated_source_ids": [item.source_id for item in activated],
            "max_sources": max_sources,
            "activated_count": len(activated),
            "candidate_present_count": len(present_slots),
            "max_characters": max_characters,
            "used_characters": used_characters,
            "max_tokens": max_tokens,
            "used_tokens": used_tokens,
            "written_files": [item.model_dump(mode="json") for item in written],
            "activation_relative_path": f"chapter_context/{chapter_id}_bounded_library_activation_latest.json",
            "record_mode": "product_bridge_bounded_library_activation",
            "library_layout_inventoried": True,
            "library_layout_assessed": True,
            "library_layout_complete": completeness.library_layout_complete,
            "bounded_library_activated": True,
            "whole_library_load": False,
            "whole_library_load_forbidden": True,
            "route_validated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": True,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"nlcl-original-bounded-library-activation-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = NlclOriginalBoundedLibraryActivationReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "nlcl-original-bounded-library-activation-receipt.json",
            payload,
        )
        history_path = (
            artifact_root / "nlcl-original-bounded-library-activation-history.json"
        )
        history = self._read_original_nlcl_bounded_library_activation_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return NlclOriginalBoundedLibraryActivationResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_original_nlcl_bounded_library_activation(
        self,
        path: Path,
    ) -> NlclOriginalBoundedLibraryActivationReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalBoundedLibraryActivationReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_bounded_library_activation_history(
        self,
        path: Path,
    ) -> list[NlclOriginalBoundedLibraryActivationReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalBoundedLibraryActivationReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(
                    NlclOriginalBoundedLibraryActivationReceipt.model_validate(item)
                )
            except (TypeError, ValueError):
                continue
        return history


    def get_gate_pipeline(self, change_set_id: str) -> GatePipelineResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_gate_pipeline(
            artifact_root / "gate-pipeline-receipt.json"
        )
        history = self._read_gate_pipeline_history(
            artifact_root / "gate-pipeline-history.json"
        )
        return GatePipelineResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_gate_pipeline(
        self,
        change_set_id: str,
        request: GatePipelineEvaluateRequest | None = None,
    ) -> GatePipelineResponse:
        request = request or GatePipelineEvaluateRequest()
        # Ordered pipeline: literary quality first so hard-fact can consume its signals.
        # Advisory LLM-shaped score and external blind matrix ride along but never unlock.
        literary_response = self.evaluate_literary_quality_gate(
            change_set_id,
            LiteraryQualityEvaluateRequest(
                expected_result_fingerprint=request.expected_result_fingerprint,
                expected_review_revision=request.expected_review_revision,
            ),
        )
        llm_score_response = self.evaluate_llm_literary_score(
            change_set_id,
            LlmLiteraryScoreEvaluateRequest(
                expected_result_fingerprint=request.expected_result_fingerprint,
                expected_review_revision=request.expected_review_revision,
            ),
        )
        hard_fact_response = self.evaluate_hard_fact_gate(
            change_set_id,
            HardFactEvaluateRequest(
                expected_result_fingerprint=request.expected_result_fingerprint,
                expected_review_revision=request.expected_review_revision,
            ),
        )
        blind_matrix = evaluate_external_blind_review_matrix(None)
        blind_receipt = blind_matrix.get("receipt") or {}
        matrix_ready = bool(blind_receipt.get("matrix_ready"))
        record = self.get(change_set_id)
        literary = literary_response.receipt
        llm_score = llm_score_response.receipt
        hard_fact = hard_fact_response.receipt
        if literary is None or hard_fact is None or llm_score is None:
            raise ChangeSetStateError(
                "gate pipeline requires literary, llm literary score, and hard-fact receipts"
            )

        post_draft_status = hard_fact.post_draft_gate_status
        steps = [
            GatePipelineStep(
                code=GatePipelineStepCode.POST_DRAFT_GATE,
                ordinal=1,
                required=False,
                present=post_draft_status != "missing",
                status=post_draft_status,
                receipt_id=None,
                detail=(
                    f"post-draft gate status={post_draft_status}"
                    if post_draft_status != "missing"
                    else "post-draft gate receipt missing or not bound"
                ),
            ),
            GatePipelineStep(
                code=GatePipelineStepCode.LITERARY_QUALITY_GATE,
                ordinal=2,
                required=True,
                present=True,
                status=(
                    literary.decision.value
                    if hasattr(literary.decision, "value")
                    else str(literary.decision)
                ),
                receipt_id=literary.receipt_id,
                detail=(
                    "literary quality gate is advisory only; "
                    f"findings={sum(int(v) for v in literary.finding_counts.values()) if literary.finding_counts else len(literary.findings)}"
                ),
            ),
            GatePipelineStep(
                code=GatePipelineStepCode.LLM_LITERARY_SCORE,
                ordinal=3,
                required=False,
                present=True,
                status=(
                    llm_score.scorer_mode.value
                    if hasattr(llm_score.scorer_mode, "value")
                    else str(llm_score.scorer_mode)
                ),
                receipt_id=llm_score.receipt_id,
                detail=(
                    "llm-shaped literary score is advisory offline stub only; "
                    f"total={llm_score.total_score}; quality_assessed=false"
                ),
            ),
            GatePipelineStep(
                code=GatePipelineStepCode.EXTERNAL_BLIND_REVIEW_MATRIX,
                ordinal=4,
                required=False,
                present=True,
                status="ready" if matrix_ready else "missing_evidence",
                receipt_id=str(blind_receipt.get("receipt_id") or "") or None,
                detail=(
                    "external blind-review matrix ready but still no quality unlock"
                    if matrix_ready
                    else "external blind-review matrix missing evidence; fail-closed"
                ),
            ),
            GatePipelineStep(
                code=GatePipelineStepCode.HARD_FACT_GATE,
                ordinal=5,
                required=True,
                present=True,
                status=(
                    hard_fact.status.value
                    if hasattr(hard_fact.status, "value")
                    else str(hard_fact.status)
                ),
                receipt_id=hard_fact.receipt_id,
                detail=(
                    f"hard-fact gate status={hard_fact.status.value if hasattr(hard_fact.status, 'value') else hard_fact.status}; "
                    f"block_signals={hard_fact.signal_counts.get('block', 0) if hard_fact.signal_counts else 0}"
                ),
            ),
        ]

        hard_status = (
            hard_fact.status.value
            if hasattr(hard_fact.status, "value")
            else str(hard_fact.status)
        )
        if hard_status == "failed":
            status = GatePipelineStatus.BLOCKED
            authorization_input_ready = False
        elif hard_status in {"passed", "missing_inputs"} and literary is not None:
            # missing_inputs is still a completed hard-fact evaluation with fail-closed signals.
            # Advisory LLM score / external blind matrix never gate authorization_input_ready.
            if hard_status == "missing_inputs":
                status = GatePipelineStatus.BLOCKED
                authorization_input_ready = False
            else:
                status = GatePipelineStatus.COMPLETE
                authorization_input_ready = True
        else:
            status = GatePipelineStatus.INCOMPLETE
            authorization_input_ready = False

        evaluated_at = _now()
        receipt_id = (
            "gate-pipeline-"
            + hashlib.sha256(
                (
                    f"{record.change_set_id}\0{hard_fact.receipt_id}\0"
                    f"{literary.receipt_id}\0{llm_score.receipt_id}\0"
                    f"{matrix_ready}\0{status.value}\0{evaluated_at.isoformat()}"
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        provisional = GatePipelineReceipt.model_construct(
            schema_id="zhimeng.gate-pipeline-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=hard_fact.outcome,
            result_revision=hard_fact.result_revision,
            result_fingerprint=hard_fact.result_fingerprint,
            review_revision=record.review_revision,
            status=status,
            steps=steps,
            hard_fact_receipt_id=hard_fact.receipt_id,
            literary_quality_receipt_id=literary.receipt_id,
            llm_literary_score_receipt_id=llm_score.receipt_id,
            external_blind_review_matrix_ready=matrix_ready,
            post_draft_gate_status=post_draft_status,
            authorization_input_ready=authorization_input_ready,
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            policy_version="local-v1-gate-pipeline-2",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = GatePipelineReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=hard_fact.outcome,
            result_revision=hard_fact.result_revision,
            result_fingerprint=hard_fact.result_fingerprint,
            review_revision=record.review_revision,
            status=status,
            steps=steps,
            hard_fact_receipt_id=hard_fact.receipt_id,
            literary_quality_receipt_id=literary.receipt_id,
            llm_literary_score_receipt_id=llm_score.receipt_id,
            external_blind_review_matrix_ready=matrix_ready,
            post_draft_gate_status=post_draft_status,
            authorization_input_ready=authorization_input_ready,
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            policy_version="local-v1-gate-pipeline-2",
            evaluated_at=evaluated_at,
            receipt_hash=gate_pipeline_receipt_hash(provisional),
        )
        artifact_root = self._artifact_root(record.change_set_id)
        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            artifact_root / "gate-pipeline-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        history_path = artifact_root / "gate-pipeline-history.json"
        history = self._read_gate_pipeline_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return GatePipelineResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )


    def _read_gate_pipeline(self, path: Path) -> GatePipelineReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return GatePipelineReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_gate_pipeline_history(self, path: Path) -> list[GatePipelineReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[GatePipelineReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(GatePipelineReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def get_hard_fact_gate(self, change_set_id: str) -> HardFactGateResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_hard_fact_gate(
            artifact_root / "hard-fact-gate-receipt.json"
        )
        history = self._read_hard_fact_gate_history(
            artifact_root / "hard-fact-gate-history.json"
        )
        return HardFactGateResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_hard_fact_gate(
        self,
        change_set_id: str,
        request: HardFactEvaluateRequest | None = None,
    ) -> HardFactGateResponse:
        request = request or HardFactEvaluateRequest()
        record = self.get(change_set_id)
        if record.status not in {
            ChangeSetStatus.READY,
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.ROLLED_BACK,
        }:
            raise ChangeSetStateError(
                "hard-fact Gate requires a ready, applied, or rolled_back ChangeSet"
            )
        if (
            request.expected_review_revision is not None
            and request.expected_review_revision != record.review_revision
        ):
            raise ChangeSetConflictError(
                "ChangeSet review revision does not match the hard-fact Gate request"
            )

        result_revision: int | None = None
        result_fingerprint: str | None = None
        if record.status is ChangeSetStatus.APPLIED:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "applied ChangeSet is missing writeback for hard-fact Gate"
                )
            outcome = "applied"
            result_revision = record.writeback.result_revision
            result_fingerprint = record.writeback.result_fingerprint
        elif record.status is ChangeSetStatus.ROLLED_BACK:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "rolled_back ChangeSet is missing writeback for hard-fact Gate"
                )
            outcome = "rolled_back"
            result_revision = (
                record.rollback.result_revision
                if record.rollback is not None
                else record.writeback.result_revision
            )
            result_fingerprint = (
                record.rollback.result_fingerprint
                if record.rollback is not None
                else record.writeback.result_fingerprint
            )
        else:
            outcome = "ready_candidate"

        if (
            request.expected_result_fingerprint is not None
            and result_fingerprint is not None
            and request.expected_result_fingerprint != result_fingerprint
        ):
            raise ChangeSetConflictError(
                "ChangeSet result fingerprint does not match the hard-fact Gate request"
            )

        artifact_root = self._artifact_root(record.change_set_id)
        existing = self._read_hard_fact_gate(
            artifact_root / "hard-fact-gate-receipt.json"
        )
        if (
            existing is not None
            and existing.review_revision == record.review_revision
            and existing.outcome == outcome
            and existing.result_revision == result_revision
            and existing.result_fingerprint == result_fingerprint
        ):
            history = self._read_hard_fact_gate_history(
                artifact_root / "hard-fact-gate-history.json"
            )
            return HardFactGateResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        signals, post_draft_status, literary_receipt_id = (
            self._build_hard_fact_signals(record, artifact_root)
        )
        counts: dict[str, int] = {}
        for item in signals:
            key = item.code.value
            counts[key] = counts.get(key, 0) + 1

        if any(item.code is HardFactSignalCode.CANDIDATE_MISSING for item in signals):
            status = HardFactStatus.MISSING_INPUTS
        elif any(item.severity == "block" for item in signals):
            status = HardFactStatus.FAILED
        else:
            status = HardFactStatus.PASSED

        evaluated_at = _now()
        digest = hashlib.sha256(
            canonical_json_hash(
                {
                    "schema": "zhimeng.hard-fact-gate-receipt.v1",
                    "change_set_id": record.change_set_id,
                    "project_id": record.project_id,
                    "outcome": outcome,
                    "result_revision": result_revision,
                    "result_fingerprint": result_fingerprint,
                    "review_revision": record.review_revision,
                    "status": status.value,
                    "post_draft_gate_status": post_draft_status,
                    "literary_quality_receipt_id": literary_receipt_id,
                    "signals": [item.model_dump(mode="json") for item in signals],
                    "signal_counts": counts,
                    "evaluated_at": evaluated_at.isoformat(),
                }
            ).encode("utf-8")
        ).hexdigest()
        receipt_id = f"hard-fact-gate-{digest[:32]}"
        provisional = HardFactGateReceipt.model_construct(
            schema_id="zhimeng.hard-fact-gate-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            gate_mode="deterministic_hard_fact_v1",
            status=status,
            authorization_input_ready=(status is HardFactStatus.PASSED),
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            post_draft_gate_status=post_draft_status,
            literary_quality_receipt_id=literary_receipt_id,
            signals=signals,
            signal_counts=counts,
            policy_version="local-v1-hard-fact-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = HardFactGateReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            gate_mode="deterministic_hard_fact_v1",
            status=status,
            authorization_input_ready=(status is HardFactStatus.PASSED),
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            post_draft_gate_status=post_draft_status,
            literary_quality_receipt_id=literary_receipt_id,
            signals=signals,
            signal_counts=counts,
            policy_version="local-v1-hard-fact-1",
            evaluated_at=evaluated_at,
            receipt_hash=hard_fact_gate_receipt_hash(provisional),
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(artifact_root / "hard-fact-gate-receipt.json", payload)
        history_path = artifact_root / "hard-fact-gate-history.json"
        history = self._read_hard_fact_gate_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return HardFactGateResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def get_literary_quality_gate(
        self,
        change_set_id: str,
    ) -> LiteraryQualityGateResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_literary_quality_gate(
            artifact_root / "literary-quality-gate-receipt.json"
        )
        history = self._read_literary_quality_gate_history(
            artifact_root / "literary-quality-gate-history.json"
        )
        return LiteraryQualityGateResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_literary_quality_gate(
        self,
        change_set_id: str,
        request: LiteraryQualityEvaluateRequest | None = None,
    ) -> LiteraryQualityGateResponse:
        request = request or LiteraryQualityEvaluateRequest()
        record = self.get(change_set_id)
        if record.status not in {
            ChangeSetStatus.READY,
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.ROLLED_BACK,
        }:
            raise ChangeSetStateError(
                "literary quality Gate requires a ready, applied, or rolled_back ChangeSet"
            )
        if (
            request.expected_review_revision is not None
            and request.expected_review_revision != record.review_revision
        ):
            raise ChangeSetConflictError(
                "ChangeSet review revision does not match the literary quality Gate request"
            )

        result_revision: int | None = None
        result_fingerprint: str | None = None
        outcome: str
        if record.status is ChangeSetStatus.APPLIED:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "applied ChangeSet is missing writeback for literary quality Gate"
                )
            outcome = "applied"
            result_revision = record.writeback.result_revision
            result_fingerprint = record.writeback.result_fingerprint
        elif record.status is ChangeSetStatus.ROLLED_BACK:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "rolled_back ChangeSet is missing writeback for literary quality Gate"
                )
            outcome = "rolled_back"
            result_revision = (
                record.rollback.result_revision
                if record.rollback is not None
                else record.writeback.result_revision
            )
            result_fingerprint = (
                record.rollback.result_fingerprint
                if record.rollback is not None
                else record.writeback.result_fingerprint
            )
        else:
            outcome = "ready_candidate"
            result_revision = None
            result_fingerprint = None

        if (
            request.expected_result_fingerprint is not None
            and result_fingerprint is not None
            and request.expected_result_fingerprint != result_fingerprint
        ):
            raise ChangeSetConflictError(
                "ChangeSet result fingerprint does not match the literary quality Gate request"
            )

        artifact_root = self._artifact_root(record.change_set_id)
        existing = self._read_literary_quality_gate(
            artifact_root / "literary-quality-gate-receipt.json"
        )
        if (
            existing is not None
            and existing.review_revision == record.review_revision
            and existing.outcome == outcome
            and existing.result_revision == result_revision
            and existing.result_fingerprint == result_fingerprint
        ):
            history = self._read_literary_quality_gate_history(
                artifact_root / "literary-quality-gate-history.json"
            )
            return LiteraryQualityGateResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        findings, dimensions, rubric_scores, rubric_total = (
            self._build_literary_quality_assessment(record)
        )
        counts: dict[str, int] = {}
        for item in findings:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        evaluated_at = _now()
        digest = hashlib.sha256(
            canonical_json_hash(
                {
                    "schema": "zhimeng.literary-quality-gate-receipt.v1",
                    "change_set_id": record.change_set_id,
                    "project_id": record.project_id,
                    "outcome": outcome,
                    "result_revision": result_revision,
                    "result_fingerprint": result_fingerprint,
                    "review_revision": record.review_revision,
                    "gate_mode": "deterministic_structural_rubric_v2",
                    "decision": LiteraryQualityDecision.ADVISORY_ONLY.value,
                    "dimensions": [
                        item.model_dump(mode="json") for item in dimensions
                    ],
                    "findings": [item.model_dump(mode="json") for item in findings],
                    "finding_counts": counts,
                    "rubric_scores": [
                        item.model_dump(mode="json") for item in rubric_scores
                    ],
                    "rubric_total_score": rubric_total,
                    "policy_version": "local-v1-advisory-2",
                    "evaluated_at": evaluated_at.isoformat(),
                }
            ).encode("utf-8")
        ).hexdigest()
        receipt_id = f"literary-quality-{digest[:32]}"
        provisional = LiteraryQualityGateReceipt.model_construct(
            schema_id="zhimeng.literary-quality-gate-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            gate_mode="deterministic_structural_rubric_v2",
            decision=LiteraryQualityDecision.ADVISORY_ONLY,
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            structural_checks_run=True,
            deterministic_rubric_scored=True,
            dimensions=dimensions,
            findings=findings,
            finding_counts=counts,
            rubric_scores=rubric_scores,
            rubric_total_score=rubric_total,
            policy_version="local-v1-advisory-2",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = LiteraryQualityGateReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            gate_mode="deterministic_structural_rubric_v2",
            decision=LiteraryQualityDecision.ADVISORY_ONLY,
            authorization_unlock=False,
            formal_draft_allowed=False,
            quality_assessed=False,
            structural_checks_run=True,
            deterministic_rubric_scored=True,
            dimensions=dimensions,
            findings=findings,
            finding_counts=counts,
            rubric_scores=rubric_scores,
            rubric_total_score=rubric_total,
            policy_version="local-v1-advisory-2",
            evaluated_at=evaluated_at,
            receipt_hash=literary_quality_gate_receipt_hash(provisional),
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "literary-quality-gate-receipt.json",
            payload,
        )
        history_path = artifact_root / "literary-quality-gate-history.json"
        history = self._read_literary_quality_gate_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return LiteraryQualityGateResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )


    def _build_offline_llm_literary_axes(self, record: ChangeSetResponse) -> list[LlmLiteraryScoreAxis]:
        texts: list[str] = []
        for change in record.changes:
            if change.operation not in {ChangeOperation.CREATE, ChangeOperation.UPDATE}:
                continue
            accepted = [
                hunk.after_text
                for hunk in change.hunks
                if hunk.decision is HunkDecision.ACCEPTED
            ]
            if accepted:
                texts.append("".join(accepted))
            elif change.operation is ChangeOperation.CREATE:
                # fall back to proposed after text if present on hunks
                texts.append("".join(hunk.after_text for hunk in change.hunks))
        body = "\n".join(texts).strip()
        chars = max(len(body), 1)
        lines = [line for line in body.splitlines() if line.strip()]
        dialogue = sum(1 for line in lines if ("「" in line or "\"" in line or "“" in line))
        sensory_tokens = ("血", "风", "冷", "热", "光", "暗", "汗", "香", "声", "味")
        sensory = sum(body.count(token) for token in sensory_tokens)
        questions = body.count("？") + body.count("?")
        paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
        lengths = [len(p) for p in paragraphs] or [chars]
        length_var = (max(lengths) - min(lengths)) / max(max(lengths), 1)

        def clamp(value: float) -> int:
            return max(0, min(100, int(round(value))))

        immersion = clamp(42 + min(30, chars / 40) + min(18, sensory * 3))
        character_voice = clamp(40 + min(35, dialogue * 8) + min(15, body.count("他") + body.count("她")))
        emotional_arc = clamp(38 + min(25, questions * 6) + min(20, sensory * 2) + min(12, length_var * 40))
        prose_craft = clamp(45 + min(20, len(lines)) + min(20, (1 - abs(0.45 - (dialogue / max(len(lines), 1)))) * 40))
        suspense_pull = clamp(36 + min(30, questions * 7) + min(20, body.count("忽然") * 8 + body.count("却") * 3))

        specs = [
            ("immersion", immersion, 22, "离线 stub：按篇幅与感官密度估计沉浸感"),
            ("character_voice", character_voice, 20, "离线 stub：按对白占比估计角色声口"),
            ("emotional_arc", emotional_arc, 20, "离线 stub：按问句与节奏变化估计情绪弧"),
            ("prose_craft", prose_craft, 20, "离线 stub：按段落与对白结构估计文笔"),
            ("suspense_pull", suspense_pull, 18, "离线 stub：按悬念标记估计拉扯力"),
        ]
        axes: list[LlmLiteraryScoreAxis] = []
        for code, score, weight, rationale in specs:
            axes.append(
                LlmLiteraryScoreAxis(
                    code=code,  # type: ignore[arg-type]
                    score=score,
                    weight=weight,
                    weighted_score=score * weight,
                    rationale=rationale if body else "离线 stub：无正文候选，给出保守分",
                )
            )
        return axes

    def get_llm_literary_score(self, change_set_id: str) -> LlmLiteraryScoreResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_llm_literary_score(
            artifact_root / "llm-literary-score-receipt.json"
        )
        history = self._read_llm_literary_score_history(
            artifact_root / "llm-literary-score-history.json"
        )
        return LlmLiteraryScoreResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_llm_literary_score(
        self,
        change_set_id: str,
        request: LlmLiteraryScoreEvaluateRequest | None = None,
    ) -> LlmLiteraryScoreResponse:
        request = request or LlmLiteraryScoreEvaluateRequest()
        record = self.get(change_set_id)
        if record.status not in {
            ChangeSetStatus.READY,
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.ROLLED_BACK,
        }:
            raise ChangeSetStateError(
                "LLM literary score requires a ready, applied, or rolled_back ChangeSet"
            )
        if (
            request.expected_review_revision is not None
            and request.expected_review_revision != record.review_revision
        ):
            raise ChangeSetConflictError(
                "ChangeSet review revision does not match the LLM literary score request"
            )

        result_revision: int | None = None
        result_fingerprint: str | None = None
        if record.status is ChangeSetStatus.APPLIED:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "applied ChangeSet is missing writeback for LLM literary score"
                )
            outcome = "applied"
            result_revision = record.writeback.result_revision
            result_fingerprint = record.writeback.result_fingerprint
        elif record.status is ChangeSetStatus.ROLLED_BACK:
            if record.writeback is None:
                raise ChangeSetStateError(
                    "rolled_back ChangeSet is missing writeback for LLM literary score"
                )
            outcome = "rolled_back"
            result_revision = (
                record.rollback.result_revision
                if record.rollback is not None
                else record.writeback.result_revision
            )
            result_fingerprint = (
                record.rollback.result_fingerprint
                if record.rollback is not None
                else record.writeback.result_fingerprint
            )
        else:
            outcome = "ready_candidate"
            result_revision = None
            result_fingerprint = None

        if (
            request.expected_result_fingerprint is not None
            and result_fingerprint is not None
            and request.expected_result_fingerprint != result_fingerprint
        ):
            raise ChangeSetConflictError(
                "ChangeSet result fingerprint does not match the LLM literary score request"
            )

        artifact_root = self._artifact_root(record.change_set_id)
        existing = self._read_llm_literary_score(
            artifact_root / "llm-literary-score-receipt.json"
        )
        if (
            existing is not None
            and existing.review_revision == record.review_revision
            and existing.outcome == outcome
            and existing.result_revision == result_revision
            and existing.result_fingerprint == result_fingerprint
        ):
            history = self._read_llm_literary_score_history(
                artifact_root / "llm-literary-score-history.json"
            )
            return LlmLiteraryScoreResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        # Local v1 keeps an honest offline stub. There is no qualified live literary
        # provider matrix yet, so prefer_provider still records offline_stub only.
        scorer_mode = LlmLiteraryScorerMode.OFFLINE_STUB
        axes = self._build_offline_llm_literary_axes(record)
        llm_score_recorded = True
        provider_model = None
        if request.prefer_provider:
            detail = (
                "prefer_provider requested but Local v1 has no qualified live literary "
                "provider matrix; offline stub score recorded as advisory only"
            )
        else:
            detail = (
                "offline stub LLM-shaped literary score recorded; quality_assessed remains "
                "false and formal draft stays locked"
            )

        total_weight = sum(item.weight for item in axes)
        total_score = round(sum(item.weighted_score for item in axes) / total_weight)
        literary = self._read_literary_quality_gate(
            artifact_root / "literary-quality-gate-receipt.json"
        )
        evaluated_at = _now()
        digest = hashlib.sha256(
            canonical_json_hash(
                {
                    "schema": "zhimeng.llm-literary-score-receipt.v1",
                    "change_set_id": record.change_set_id,
                    "project_id": record.project_id,
                    "outcome": outcome,
                    "result_revision": result_revision,
                    "result_fingerprint": result_fingerprint,
                    "review_revision": record.review_revision,
                    "scorer_mode": scorer_mode.value,
                    "axes": [item.model_dump(mode="json") for item in axes],
                    "total_score": total_score,
                    "policy_version": "local-v1-llm-literary-score-1",
                    "evaluated_at": evaluated_at.isoformat(),
                }
            ).encode("utf-8")
        ).hexdigest()
        receipt_id = f"llm-literary-score-{digest[:32]}"
        provisional = LlmLiteraryScoreReceipt.model_construct(
            schema_id="zhimeng.llm-literary-score-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            scorer_mode=scorer_mode,
            advisory_only=True,
            llm_score_recorded=llm_score_recorded,
            quality_assessed=False,
            subjective_quality_unlocked=False,
            formal_draft_allowed=False,
            authorization_unlock=False,
            provider_model=provider_model,
            literary_quality_receipt_id=None if literary is None else literary.receipt_id,
            axes=axes,
            total_score=total_score,
            detail=detail,
            policy_version="local-v1-llm-literary-score-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = LlmLiteraryScoreReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,
            result_revision=result_revision,
            result_fingerprint=result_fingerprint,
            review_revision=record.review_revision,
            scorer_mode=scorer_mode,
            advisory_only=True,
            llm_score_recorded=llm_score_recorded,
            quality_assessed=False,
            subjective_quality_unlocked=False,
            formal_draft_allowed=False,
            authorization_unlock=False,
            provider_model=provider_model,
            literary_quality_receipt_id=None if literary is None else literary.receipt_id,
            axes=axes,
            total_score=total_score,
            detail=detail,
            policy_version="local-v1-llm-literary-score-1",
            evaluated_at=evaluated_at,
            receipt_hash=llm_literary_score_receipt_hash(provisional),
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(artifact_root / "llm-literary-score-receipt.json", payload)
        history_path = artifact_root / "llm-literary-score-history.json"
        history = self._read_llm_literary_score_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return LlmLiteraryScoreResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_llm_literary_score(self, path: Path) -> LlmLiteraryScoreReceipt | None:
        if not path.is_file():
            return None
        return LlmLiteraryScoreReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _read_llm_literary_score_history(
        self, path: Path
    ) -> list[LlmLiteraryScoreReceipt]:
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [LlmLiteraryScoreReceipt.model_validate(item) for item in payload]

    def get_reader_review(self, change_set_id: str) -> ReaderReviewResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_reader_review(
            artifact_root / "reader-review-receipt.json"
        )
        history = self._read_reader_review_history(
            artifact_root / "reader-review-history.json"
        )
        return ReaderReviewResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def create_reader_review(
        self,
        change_set_id: str,
        request: ReaderReviewRequest,
    ) -> ReaderReviewResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "Reader review requires an applied or rolled_back ChangeSet"
            )
        if record.writeback is None:
            raise ChangeSetStateError(
                "Reader review requires writeback result fingerprint and revision"
            )
        if (
            request.expected_result_fingerprint is not None
            and request.expected_result_fingerprint != record.writeback.result_fingerprint
        ):
            raise ChangeSetConflictError(
                "ChangeSet result fingerprint does not match the expected value"
            )

        artifact_root = self._artifact_root(record.change_set_id)
        existing = self._read_reader_review(
            artifact_root / "reader-review-receipt.json"
        )
        if (
            existing is not None
            and existing.result_fingerprint == record.writeback.result_fingerprint
            and existing.result_revision == record.writeback.result_revision
            and existing.outcome
            == (
                "applied"
                if record.status == ChangeSetStatus.APPLIED
                else "rolled_back"
            )
        ):
            history = self._read_reader_review_history(
                artifact_root / "reader-review-history.json"
            )
            return ReaderReviewResponse(
                change_set_id=record.change_set_id,
                receipt=existing,
                history=history,
            )

        findings = self._build_deterministic_reader_findings(record)
        candidate_paths = sorted(
            {
                change.relative_path
                for change in record.changes
                if change.operation
                in {ChangeOperation.CREATE, ChangeOperation.UPDATE}
            }
        )
        outcome = (
            "applied"
            if record.status == ChangeSetStatus.APPLIED
            else "rolled_back"
        )
        counts: dict[str, int] = {"fact_risk": 0, "continuity_risk": 0, "subjective_note": 0}
        for item in findings:
            counts[item.kind] = counts.get(item.kind, 0) + 1
        allowed_input_classes = [
            "prior_published_chapter_text",
            "current_candidate_after_content",
            "change_set_writeback_fingerprint",
        ]
        excluded_input_classes = [
            "future_outline",
            "author_truth_unrevealed",
            "planner_context",
            "standard_answers",
            "next_volume_secrets",
        ]
        prompt_fingerprint = knowledge_card_hash(
            {
                "schema": "zhimeng.reader-prompt-fingerprint.v1",
                "review_mode": "deterministic_cold_read_v1",
                "rules": [
                    "cold_read_only",
                    "no_future_outline",
                    "no_gate_override",
                    "fact_risk_vs_subjective_note",
                ],
            }
        )
        context_fingerprint = knowledge_card_hash(
            {
                "schema": "zhimeng.reader-context-fingerprint.v1",
                "change_set_id": record.change_set_id,
                "project_id": record.project_id,
                "outcome": outcome,
                "result_revision": record.writeback.result_revision,
                "result_fingerprint": record.writeback.result_fingerprint,
                "allowed_input_classes": allowed_input_classes,
                "excluded_input_classes": excluded_input_classes,
                "candidate_relative_paths": candidate_paths,
            }
        )
        evaluator_fingerprint = knowledge_card_hash(
            {
                "schema": "zhimeng.reader-evaluator-fingerprint.v1",
                "evaluator_id": "zhimeng.reader.deterministic-cold-read",
                "evaluator_kind": "deterministic_cold_read_v1",
                "model_id": "none",
                "prompt_fingerprint": prompt_fingerprint,
                "algorithm_version": "local-v1-reader-cold-read-1",
            }
        )
        material = {
            "schema": "zhimeng.reader-review-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "outcome": outcome,
            "result_revision": record.writeback.result_revision,
            "result_fingerprint": record.writeback.result_fingerprint,
            "review_mode": "deterministic_cold_read_v1",
            "evaluator_kind": "deterministic_cold_read_v1",
            "evaluator_id": "zhimeng.reader.deterministic-cold-read",
            "model_id": "none",
            "prompt_fingerprint": prompt_fingerprint,
            "context_fingerprint": context_fingerprint,
            "evaluator_fingerprint": evaluator_fingerprint,
            "allowed_input_classes": allowed_input_classes,
            "excluded_input_classes": excluded_input_classes,
            "candidate_relative_paths": candidate_paths,
            "findings": [item.model_dump(mode="json") for item in findings],
            "finding_counts": counts,
            "reader_opinion_recorded": True,
            "overrides_gate": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"reader-review-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = ReaderReviewReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(artifact_root / "reader-review-receipt.json", payload)
        history_path = artifact_root / "reader-review-history.json"
        history = self._read_reader_review_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return ReaderReviewResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def get_reader_review_decision(
        self,
        change_set_id: str,
    ) -> ReaderReviewDecisionResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_reader_review_decision(
            artifact_root / "reader-review-decision-receipt.json"
        )
        history = self._read_reader_review_decision_history(
            artifact_root / "reader-review-decision-history.json"
        )
        follow_up = None
        if latest is not None and latest.follow_up_change_set_id is not None:
            try:
                follow_up = self.get(latest.follow_up_change_set_id)
            except ChangeSetError:
                follow_up = None
        return ReaderReviewDecisionResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
            follow_up_change_set=follow_up,
        )

    def decide_reader_review(
        self,
        change_set_id: str,
        request: ReaderReviewDecisionRequest,
    ) -> ReaderReviewDecisionResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "Reader review decisions require an applied or rolled_back ChangeSet"
            )
        if record.writeback is None:
            raise ChangeSetStateError(
                "Reader review decisions require writeback result fingerprint and revision"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        reader = self._read_reader_review(artifact_root / "reader-review-receipt.json")
        if reader is None:
            raise ChangeSetStateError(
                "Reader review receipt is required before author decisions"
            )
        if reader.receipt_hash != request.expected_reader_review_receipt_hash:
            raise ChangeSetConflictError(
                "Reader review receipt hash does not match the expected value"
            )
        if reader.change_set_id != record.change_set_id:
            raise ChangeSetStateError(
                "Reader review receipt is not bound to this ChangeSet"
            )
        if (
            reader.result_fingerprint != record.writeback.result_fingerprint
            or reader.result_revision != record.writeback.result_revision
        ):
            raise ChangeSetStateError(
                "Reader review receipt is stale relative to ChangeSet writeback result"
            )

        finding_ids = [item.finding_id for item in reader.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ChangeSetStateError("Reader review findings must be unique")
        finding_set = set(finding_ids)
        decision_ids = [item.finding_id for item in request.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ChangeSetInputError("Reader finding decisions must be unique")
        unknown = [item for item in decision_ids if item not in finding_set]
        if unknown:
            raise ChangeSetInputError(
                "unknown Reader finding ids: " + ", ".join(unknown[:8])
            )
        if request.finalize and finding_set - set(decision_ids):
            missing = sorted(finding_set - set(decision_ids))
            raise ChangeSetInputError(
                "finalize requires a decision for every Reader finding: "
                + ", ".join(missing[:8])
            )

        existing = self._read_reader_review_decision(
            artifact_root / "reader-review-decision-receipt.json"
        )
        decision_material_key = {
            "reader_review_receipt_hash": reader.receipt_hash,
            "decisions": [
                item.model_dump(mode="json")
                for item in sorted(request.decisions, key=lambda row: row.finding_id)
            ],
            "finalize": request.finalize,
            "result_fingerprint": reader.result_fingerprint,
            "result_revision": reader.result_revision,
        }
        if existing is not None:
            existing_key = {
                "reader_review_receipt_hash": existing.reader_review_receipt_hash,
                "decisions": [
                    item.model_dump(mode="json")
                    for item in sorted(existing.decisions, key=lambda row: row.finding_id)
                ],
                "finalize": existing.finalized,
                "result_fingerprint": existing.result_fingerprint,
                "result_revision": existing.result_revision,
            }
            if existing_key == decision_material_key:
                history = self._read_reader_review_decision_history(
                    artifact_root / "reader-review-decision-history.json"
                )
                follow_up = None
                if existing.follow_up_change_set_id is not None:
                    try:
                        follow_up = self.get(existing.follow_up_change_set_id)
                    except ChangeSetError:
                        follow_up = None
                return ReaderReviewDecisionResponse(
                    change_set_id=record.change_set_id,
                    receipt=existing,
                    history=history,
                    follow_up_change_set=follow_up,
                )

        adopted = [
            item.finding_id
            for item in request.decisions
            if item.decision == "adopted"
        ]
        rejected = [
            item.finding_id
            for item in request.decisions
            if item.decision == "rejected"
        ]
        deferred = [
            item.finding_id
            for item in request.decisions
            if item.decision == "deferred"
        ]

        follow_up_change_set = None
        follow_up_id = None
        if request.finalize and adopted:
            follow_up_change_set = self._create_reader_follow_up_change_set(
                record,
                reader,
                request.decisions,
            )
            follow_up_id = follow_up_change_set.change_set_id

        material = {
            "schema": "zhimeng.reader-review-decision-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "reader_review_receipt_id": reader.receipt_id,
            "reader_review_receipt_hash": reader.receipt_hash,
            "result_revision": reader.result_revision,
            "result_fingerprint": reader.result_fingerprint,
            "decisions": [item.model_dump(mode="json") for item in request.decisions],
            "adopted_finding_ids": adopted,
            "rejected_finding_ids": rejected,
            "deferred_finding_ids": deferred,
            "finalized": request.finalize,
            "follow_up_change_set_id": follow_up_id,
            "parent_reader_valid_for_new_candidate": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"reader-review-decision-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = ReaderReviewDecisionReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(
            artifact_root / "reader-review-decision-receipt.json",
            payload,
        )
        history_path = artifact_root / "reader-review-decision-history.json"
        history = self._read_reader_review_decision_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        if request.finalize:
            self._record_feedback_candidates_from_reader_decision(
                record=record,
                reader=reader,
                decision_receipt=receipt,
            )
        return ReaderReviewDecisionResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
            follow_up_change_set=follow_up_change_set,
        )



    def get_eval_corpus_export(self, change_set_id: str) -> EvalCorpusExportResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_eval_corpus_export(
            artifact_root / "eval-corpus-export-receipt.json"
        )
        history = self._read_eval_corpus_export_history(
            artifact_root / "eval-corpus-export-history.json"
        )
        package = None
        if latest is not None:
            package = self._read_json_object(
                artifact_root / "eval-corpus-package.json"
            )
        return EvalCorpusExportResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
            package=package,
        )

    def export_eval_corpus(
        self,
        change_set_id: str,
        request: EvalCorpusExportRequest | None = None,
    ) -> EvalCorpusExportResponse:
        request = request or EvalCorpusExportRequest()
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "eval corpus export requires an applied or rolled_back ChangeSet"
            )

        artifact_root = self._artifact_root(record.change_set_id)
        sources: list[tuple[str, str, Path]] = []
        if request.include_gates:
            sources.extend(
                [
                    ("hard_fact_gate", "hard-fact-gate-receipt.json", artifact_root / "hard-fact-gate-receipt.json"),
                    (
                        "literary_quality_gate",
                        "literary-quality-gate-receipt.json",
                        artifact_root / "literary-quality-gate-receipt.json",
                    ),
                    (
                        "gate_pipeline",
                        "gate-pipeline-receipt.json",
                        artifact_root / "gate-pipeline-receipt.json",
                    ),
                ]
            )
        if request.include_feedback_experience:
            sources.extend(
                [
                    (
                        "feedback_candidate_bundle",
                        "feedback-candidate-bundle.json",
                        artifact_root / "feedback-candidate-bundle.json",
                    ),
                    (
                        "feedback_candidate_promotion",
                        "feedback-candidate-promotion-receipt.json",
                        artifact_root / "feedback-candidate-promotion-receipt.json",
                    ),
                    (
                        "reader_review",
                        "reader-review-receipt.json",
                        artifact_root / "reader-review-receipt.json",
                    ),
                    (
                        "reader_review_decision",
                        "reader-review-decision-receipt.json",
                        artifact_root / "reader-review-decision-receipt.json",
                    ),
                ]
            )
        if request.include_formal_knowledge_promotion:
            sources.append(
                (
                    "formal_knowledge_promotion",
                    "formal-knowledge-promotion-receipt.json",
                    artifact_root / "formal-knowledge-promotion-receipt.json",
                )
            )
        if request.include_writeback:
            sources.extend(
                [
                    (
                        "writeback_evidence",
                        "writeback-evidence-receipt.json",
                        artifact_root / "writeback-evidence-receipt.json",
                    ),
                    (
                        "formal_draft_authorization",
                        "formal-draft-authorization-receipt.json",
                        artifact_root / "formal-draft-authorization-receipt.json",
                    ),
                ]
            )

        package_artifacts: dict[str, object] = {}
        refs: list[EvalCorpusArtifactRef] = []
        for kind, relative_name, path in sources:
            payload = self._read_json_object(path)
            if payload is None:
                refs.append(
                    EvalCorpusArtifactRef(
                        kind=kind,  # type: ignore[arg-type]
                        present=False,
                        relative_artifact=None,
                        content_sha256=None,
                        desensitized=True,
                    )
                )
                continue
            redacted = self._redact_eval_corpus_payload(payload)
            digest = canonical_json_hash(redacted)
            package_artifacts[kind] = redacted
            refs.append(
                EvalCorpusArtifactRef(
                    kind=kind,  # type: ignore[arg-type]
                    present=True,
                    relative_artifact=relative_name,
                    content_sha256=digest,
                    desensitized=True,
                )
            )

        present_count = sum(1 for item in refs if item.present)
        if present_count == 0:
            raise ChangeSetStateError(
                "eval corpus export requires at least one desensitizable artifact"
            )

        created_at = _now()
        package = {
            "schema": "zhimeng.eval-corpus-package.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "outcome": (
                record.status.value
                if hasattr(record.status, "value")
                else str(record.status)
            ),
            "redaction_policy": "local-v1-eval-corpus-redact-1",
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "formal_knowledge_contaminated": False,
            "training_ready": False,
            "artifacts": package_artifacts,
            "exported_at": created_at.isoformat().replace("+00:00", "Z"),
        }
        package_text = json.dumps(
            package, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        package_bytes = (package_text + "\n").encode("utf-8")
        export_file_sha256 = hashlib.sha256(package_bytes).hexdigest()

        export_relative = (
            f"07_运营数据/eval_corpus/{record.change_set_id}.json"
        )
        binding = self._require_binding(record.project_id)
        project_root = self.reader.resolve(binding.root_path)
        export_path = project_root.joinpath(*PurePosixPath(export_relative).parts)
        if not _is_within(export_path.parent, project_root):
            raise ChangeSetStateError("eval corpus export path escapes project root")
        export_path.parent.mkdir(parents=True, exist_ok=True)
        if export_path.exists():
            value = export_path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ChangeSetStateError("eval corpus export target must be a regular file")
            if int(getattr(value, "st_nlink", 1)) > 1:
                raise ChangeSetStateError("eval corpus export target cannot be a hardlink")
        # Idempotent rewrite of the same change-set package file is allowed.
        with export_path.open("wb") as handle:
            handle.write(package_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(artifact_root / "eval-corpus-package.json", package)

        outcome = (
            record.status.value
            if hasattr(record.status, "value")
            else str(record.status)
        )
        if outcome not in {"applied", "rolled_back"}:
            outcome = "ready_candidate"

        receipt_id = (
            "eval-corpus-export-"
            + hashlib.sha256(
                f"{record.change_set_id}\0{export_file_sha256}\0{created_at.isoformat()}"
                .encode("utf-8")
            ).hexdigest()[:32]
        )
        provisional = EvalCorpusExportReceipt.model_construct(
            schema_id="zhimeng.eval-corpus-export-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,  # type: ignore[arg-type]
            export_relative_path=export_relative,
            export_file_sha256=export_file_sha256,
            artifact_count=len(refs),
            artifacts=refs,
            redaction_policy="local-v1-eval-corpus-redact-1",
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            created_at=created_at,
            receipt_hash="0" * 64,
        )
        receipt = EvalCorpusExportReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            outcome=outcome,  # type: ignore[arg-type]
            export_relative_path=export_relative,
            export_file_sha256=export_file_sha256,
            artifact_count=len(refs),
            artifacts=refs,
            redaction_policy="local-v1-eval-corpus-redact-1",
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            created_at=created_at,
            receipt_hash=eval_corpus_export_receipt_hash(provisional),
        )
        _atomic_write_json(
            artifact_root / "eval-corpus-export-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        history_path = artifact_root / "eval-corpus-export-history.json"
        history = self._read_eval_corpus_export_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return EvalCorpusExportResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
            package=package,
        )

    def _redact_eval_corpus_payload(self, payload: dict[str, object]) -> dict[str, object]:
        redacted = json.loads(json.dumps(payload, ensure_ascii=False))

        def walk(node: object) -> object:
            if isinstance(node, dict):
                out: dict[str, object] = {}
                for key, value in node.items():
                    lowered = str(key).lower()
                    if any(
                        token in lowered
                        for token in (
                            "absolute_path",
                            "root_path",
                            "api_key",
                            "authorization",
                            "secret",
                            "token",
                            "password",
                        )
                    ):
                        out[key] = "[redacted]"
                        continue
                    if lowered.endswith("_path") and isinstance(value, str):
                        # keep relative project paths; redact drive-absolute paths
                        if ":\\" in value or value.startswith("/") or value.startswith("\\\\"):
                            out[key] = "[redacted-absolute-path]"
                            continue
                    out[key] = walk(value)
                return out
            if isinstance(node, list):
                return [walk(item) for item in node]
            return node

        result = walk(redacted)
        assert isinstance(result, dict)
        return result

    def _read_json_object(self, path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _read_eval_corpus_export(
        self,
        path: Path,
    ) -> EvalCorpusExportReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return EvalCorpusExportReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_eval_corpus_export_history(
        self,
        path: Path,
    ) -> list[EvalCorpusExportReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[EvalCorpusExportReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(EvalCorpusExportReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def get_eval_corpus_baseline(self, change_set_id: str) -> EvalCorpusBaselineResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_eval_corpus_baseline(
            artifact_root / "eval-corpus-baseline-receipt.json"
        )
        history = self._read_eval_corpus_baseline_history(
            artifact_root / "eval-corpus-baseline-history.json"
        )
        return EvalCorpusBaselineResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def freeze_eval_corpus_baseline(
        self,
        change_set_id: str,
        request: EvalCorpusBaselineFreezeRequest | None = None,
    ) -> EvalCorpusBaselineResponse:
        request = request or EvalCorpusBaselineFreezeRequest()
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "eval corpus baseline freeze requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        export_receipt = self._read_eval_corpus_export(
            artifact_root / "eval-corpus-export-receipt.json"
        )
        package = self._read_json_object(artifact_root / "eval-corpus-package.json")
        if export_receipt is None or package is None:
            raise ChangeSetStateError(
                "eval corpus baseline freeze requires a prior eval corpus export package"
            )
        if (
            request.expected_export_file_sha256 is not None
            and export_receipt.export_file_sha256 != request.expected_export_file_sha256
        ):
            raise ChangeSetConflictError(
                "eval corpus export file hash does not match the baseline freeze request"
            )
        if package.get("schema") != "zhimeng.eval-corpus-package.v1":
            raise ChangeSetStateError(
                "eval corpus baseline freeze requires package schema zhimeng.eval-corpus-package.v1"
            )
        for flag in (
            "formal_draft_allowed",
            "quality_assessed",
            "formal_knowledge_contaminated",
            "training_ready",
        ):
            if package.get(flag) is not False:
                raise ChangeSetStateError(
                    f"eval corpus baseline freeze blocked because {flag} is not locked false"
                )

        present_kinds = [
            str(item.kind.value if hasattr(item.kind, "value") else item.kind)
            for item in export_receipt.artifacts
            if item.present
        ]
        if not present_kinds:
            raise ChangeSetStateError(
                "eval corpus baseline freeze requires at least one present artifact kind"
            )

        history = self._read_eval_corpus_baseline_history(
            artifact_root / "eval-corpus-baseline-history.json"
        )
        baseline_version = (history[-1].baseline_version + 1) if history else 1
        frozen_at = _now()
        receipt_id = (
            "eval-corpus-baseline-"
            + hashlib.sha256(
                f"{record.change_set_id}\0{export_receipt.export_file_sha256}\0{baseline_version}\0{frozen_at.isoformat()}"
                .encode("utf-8")
            ).hexdigest()[:32]
        )
        provisional = EvalCorpusBaselineReceipt.model_construct(
            schema_id="zhimeng.eval-corpus-baseline-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            baseline_version=baseline_version,
            source_export_receipt_id=export_receipt.receipt_id,
            source_export_file_sha256=export_receipt.export_file_sha256,
            package_schema="zhimeng.eval-corpus-package.v1",
            present_artifact_kinds=present_kinds,
            artifact_count=len(present_kinds),
            label=request.label,
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            policy_version="local-v1-eval-corpus-baseline-1",
            frozen_at=frozen_at,
            receipt_hash="0" * 64,
        )
        receipt = EvalCorpusBaselineReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            baseline_version=baseline_version,
            source_export_receipt_id=export_receipt.receipt_id,
            source_export_file_sha256=export_receipt.export_file_sha256,
            package_schema="zhimeng.eval-corpus-package.v1",
            present_artifact_kinds=present_kinds,
            artifact_count=len(present_kinds),
            label=request.label,
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            policy_version="local-v1-eval-corpus-baseline-1",
            frozen_at=frozen_at,
            receipt_hash=eval_corpus_baseline_receipt_hash(provisional),
        )
        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            artifact_root / "eval-corpus-baseline-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        _atomic_write_json(
            artifact_root / "eval-corpus-baseline-package.json",
            package,
        )
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            artifact_root / "eval-corpus-baseline-history.json",
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return EvalCorpusBaselineResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def get_eval_corpus_regression(
        self, change_set_id: str
    ) -> EvalCorpusRegressionResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_eval_corpus_regression(
            artifact_root / "eval-corpus-regression-receipt.json"
        )
        history = self._read_eval_corpus_regression_history(
            artifact_root / "eval-corpus-regression-history.json"
        )
        return EvalCorpusRegressionResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_eval_corpus_regression(
        self,
        change_set_id: str,
        request: EvalCorpusRegressionEvaluateRequest | None = None,
    ) -> EvalCorpusRegressionResponse:
        request = request or EvalCorpusRegressionEvaluateRequest()
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "eval corpus regression requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        baseline = self._read_eval_corpus_baseline(
            artifact_root / "eval-corpus-baseline-receipt.json"
        )
        baseline_package = self._read_json_object(
            artifact_root / "eval-corpus-baseline-package.json"
        )
        current_export = self._read_eval_corpus_export(
            artifact_root / "eval-corpus-export-receipt.json"
        )
        current_package = self._read_json_object(
            artifact_root / "eval-corpus-package.json"
        )

        if (
            request.expected_baseline_receipt_id is not None
            and (
                baseline is None
                or baseline.receipt_id != request.expected_baseline_receipt_id
            )
        ):
            raise ChangeSetConflictError(
                "eval corpus baseline receipt does not match the regression request"
            )
        if (
            request.expected_export_file_sha256 is not None
            and (
                current_export is None
                or current_export.export_file_sha256
                != request.expected_export_file_sha256
            )
        ):
            raise ChangeSetConflictError(
                "eval corpus export file hash does not match the regression request"
            )

        baseline_kinds = set(baseline.present_artifact_kinds) if baseline is not None else set()
        current_kinds = {
            str(item.kind.value if hasattr(item.kind, "value") else item.kind)
            for item in (current_export.artifacts if current_export is not None else [])
            if item.present
        }

        def package_locks_intact(package: dict[str, object] | None) -> bool:
            if package is None:
                return False
            return all(
                package.get(flag) is False
                for flag in (
                    "formal_draft_allowed",
                    "quality_assessed",
                    "formal_knowledge_contaminated",
                    "training_ready",
                )
            )

        def hard_fact_block_count(package: dict[str, object] | None) -> int | None:
            if package is None:
                return None
            artifacts = package.get("artifacts")
            if not isinstance(artifacts, dict):
                return None
            hard = artifacts.get("hard_fact_gate")
            if not isinstance(hard, dict):
                return None
            signals = hard.get("signals")
            if isinstance(signals, list):
                return sum(
                    1
                    for item in signals
                    if isinstance(item, dict)
                    and str(item.get("severity") or "").lower() == "block"
                )
            status = str(hard.get("status") or "").lower()
            if status in {"blocked", "failed"}:
                return 1
            if status == "passed":
                return 0
            return 0

        def literary_hard_fact_count(package: dict[str, object] | None) -> int | None:
            if package is None:
                return None
            artifacts = package.get("artifacts")
            if not isinstance(artifacts, dict):
                return None
            literary = artifacts.get("literary_quality_gate")
            if not isinstance(literary, dict):
                return None
            counts = literary.get("finding_counts")
            if isinstance(counts, dict):
                try:
                    return int(counts.get("hard_fact_signal") or 0)
                except (TypeError, ValueError):
                    return 0
            findings = literary.get("findings")
            if isinstance(findings, list):
                return sum(
                    1
                    for item in findings
                    if isinstance(item, dict)
                    and str(item.get("kind") or "") == "hard_fact_signal"
                )
            return 0

        baseline_blocks = hard_fact_block_count(baseline_package)
        current_blocks = hard_fact_block_count(current_package)
        baseline_literary = literary_hard_fact_count(baseline_package)
        current_literary = literary_hard_fact_count(current_package)

        hard_fact_ok = True
        hard_fact_detail = (
            "hard-fact block comparison skipped (artifact absent in baseline or current)"
        )
        if baseline_blocks is not None and current_blocks is not None:
            hard_fact_ok = current_blocks <= baseline_blocks
            hard_fact_detail = (
                f"hard-fact block signals current={current_blocks} baseline={baseline_blocks}"
            )

        literary_ok = True
        literary_detail = (
            "literary hard-fact comparison skipped (artifact absent in baseline or current)"
        )
        if baseline_literary is not None and current_literary is not None:
            literary_ok = current_literary <= baseline_literary
            literary_detail = (
                f"literary hard_fact_signal current={current_literary} baseline={baseline_literary}"
            )

        schema_ok = (
            baseline_package is not None
            and current_package is not None
            and baseline_package.get("schema") == "zhimeng.eval-corpus-package.v1"
            and current_package.get("schema") == "zhimeng.eval-corpus-package.v1"
        )
        kinds_ok = bool(baseline_kinds) and baseline_kinds.issubset(current_kinds)
        locks_ok = package_locks_intact(baseline_package) and package_locks_intact(
            current_package
        )
        min_sample_ok = len(current_kinds) >= 1

        checks = [
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.BASELINE_PRESENT,
                required=True,
                present=baseline is not None and baseline_package is not None,
                detail=(
                    f"baseline {baseline.receipt_id} v{baseline.baseline_version}"
                    if baseline is not None
                    else "eval corpus baseline is not frozen"
                ),
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.CURRENT_EXPORT_PRESENT,
                required=True,
                present=current_export is not None and current_package is not None,
                detail=(
                    f"current export {current_export.receipt_id}"
                    if current_export is not None
                    else "current eval corpus export package is missing"
                ),
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.PACKAGE_SCHEMA_COMPATIBLE,
                required=True,
                present=schema_ok,
                detail=(
                    "baseline and current packages use zhimeng.eval-corpus-package.v1"
                    if schema_ok
                    else "baseline/current package schema is missing or incompatible"
                ),
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.PRESENT_KINDS_SUPERSET,
                required=True,
                present=kinds_ok,
                detail=(
                    f"current present kinds cover baseline kinds ({sorted(baseline_kinds)})"
                    if kinds_ok
                    else (
                        "current present kinds missing baseline coverage: "
                        + ", ".join(sorted(baseline_kinds - current_kinds))
                        if baseline_kinds
                        else "baseline present kinds unavailable"
                    )
                ),
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.LOCKS_INTACT,
                required=True,
                present=locks_ok,
                detail=(
                    "formal_draft/quality/contamination/training locks remain false"
                    if locks_ok
                    else "baseline or current package lock flags are not intact"
                ),
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.HARD_FACT_BLOCK_NON_INCREASE,
                required=baseline_blocks is not None and current_blocks is not None,
                present=hard_fact_ok,
                detail=hard_fact_detail,
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.LITERARY_HARD_FACT_NON_INCREASE,
                required=baseline_literary is not None and current_literary is not None,
                present=literary_ok,
                detail=literary_detail,
            ),
            EvalCorpusRegressionCheck(
                code=EvalCorpusRegressionCheckCode.MIN_SAMPLE_ARTIFACTS,
                required=True,
                present=min_sample_ok,
                detail=(
                    f"current export has {len(current_kinds)} present artifact kind(s)"
                    if min_sample_ok
                    else "current export has no present artifacts for min-sample gate"
                ),
            ),
        ]
        missing = [item.code for item in checks if item.required and not item.present]
        if not missing:
            status = EvalCorpusRegressionStatus.NON_DEGRADED
        elif baseline is None or current_export is None:
            status = EvalCorpusRegressionStatus.INCOMPLETE
        else:
            status = EvalCorpusRegressionStatus.DEGRADED

        evaluated_at = _now()
        receipt_id = (
            "eval-corpus-regression-"
            + hashlib.sha256(
                f"{record.change_set_id}\0{baseline.receipt_id if baseline else ''}\0{current_export.receipt_id if current_export else ''}\0{status.value}\0{evaluated_at.isoformat()}"
                .encode("utf-8")
            ).hexdigest()[:32]
        )
        provisional = EvalCorpusRegressionReceipt.model_construct(
            schema_id="zhimeng.eval-corpus-regression-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            status=status,
            baseline_receipt_id=baseline.receipt_id if baseline is not None else None,
            baseline_version=baseline.baseline_version if baseline is not None else None,
            baseline_export_file_sha256=(
                baseline.source_export_file_sha256 if baseline is not None else None
            ),
            current_export_receipt_id=(
                current_export.receipt_id if current_export is not None else None
            ),
            current_export_file_sha256=(
                current_export.export_file_sha256 if current_export is not None else None
            ),
            checks=checks,
            missing_required=missing,
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            policy_version="local-v1-eval-corpus-regression-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = EvalCorpusRegressionReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            status=status,
            baseline_receipt_id=baseline.receipt_id if baseline is not None else None,
            baseline_version=baseline.baseline_version if baseline is not None else None,
            baseline_export_file_sha256=(
                baseline.source_export_file_sha256 if baseline is not None else None
            ),
            current_export_receipt_id=(
                current_export.receipt_id if current_export is not None else None
            ),
            current_export_file_sha256=(
                current_export.export_file_sha256 if current_export is not None else None
            ),
            checks=checks,
            missing_required=missing,
            formal_draft_allowed=False,
            quality_assessed=False,
            formal_knowledge_contaminated=False,
            training_ready=False,
            policy_version="local-v1-eval-corpus-regression-1",
            evaluated_at=evaluated_at,
            receipt_hash=eval_corpus_regression_receipt_hash(provisional),
        )
        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            artifact_root / "eval-corpus-regression-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        history = self._read_eval_corpus_regression_history(
            artifact_root / "eval-corpus-regression-history.json"
        )
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            artifact_root / "eval-corpus-regression-history.json",
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return EvalCorpusRegressionResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def _read_eval_corpus_baseline(
        self,
        path: Path,
    ) -> EvalCorpusBaselineReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return EvalCorpusBaselineReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_eval_corpus_baseline_history(
        self,
        path: Path,
    ) -> list[EvalCorpusBaselineReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[EvalCorpusBaselineReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(EvalCorpusBaselineReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _read_eval_corpus_regression(
        self,
        path: Path,
    ) -> EvalCorpusRegressionReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return EvalCorpusRegressionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_eval_corpus_regression_history(
        self,
        path: Path,
    ) -> list[EvalCorpusRegressionReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[EvalCorpusRegressionReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(EvalCorpusRegressionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def get_formal_knowledge_promotion(
        self,
        change_set_id: str,
    ) -> FormalKnowledgePromotionResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_formal_knowledge_promotion(
            artifact_root / "formal-knowledge-promotion-receipt.json"
        )
        history = self._read_formal_knowledge_promotion_history(
            artifact_root / "formal-knowledge-promotion-history.json"
        )
        return FormalKnowledgePromotionResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
        )

    def evaluate_formal_knowledge_promotion(
        self,
        change_set_id: str,
        request: FormalKnowledgePromotionEvaluateRequest | None = None,
    ) -> FormalKnowledgePromotionResponse:
        request = request or FormalKnowledgePromotionEvaluateRequest()
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        receipt = self._build_formal_knowledge_promotion_receipt(
            record=record,
            artifact_root=artifact_root,
            expected_bundle_hash=request.expected_feedback_candidate_bundle_hash,
            candidate_ids=list(request.candidate_ids),
            action_decisions=(),
            force_status=None,
        )
        history = self._persist_formal_knowledge_promotion_receipt(
            artifact_root=artifact_root,
            receipt=receipt,
        )
        return FormalKnowledgePromotionResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )

    def act_formal_knowledge_promotion(
        self,
        change_set_id: str,
        request: FormalKnowledgePromotionActionRequest,
    ) -> FormalKnowledgePromotionResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "formal knowledge promotion requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        bundle = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        if bundle is None:
            raise ChangeSetStateError(
                "feedback candidate bundle is required before formal knowledge promotion"
            )
        if bundle.receipt_hash != request.expected_feedback_candidate_bundle_hash:
            raise ChangeSetConflictError(
                "feedback candidate bundle hash does not match the expected value"
            )

        decision_ids = [item.candidate_id for item in request.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ChangeSetInputError(
                "formal knowledge promotion decisions must be unique by candidate_id"
            )
        known = {item.candidate_id for item in bundle.candidates}
        unknown = [item for item in decision_ids if item not in known]
        if unknown:
            raise ChangeSetInputError(
                "unknown feedback candidate ids: " + ", ".join(unknown[:8])
            )

        existing = self._read_formal_knowledge_promotion(
            artifact_root / "formal-knowledge-promotion-receipt.json"
        )
        staged_map: dict[str, FormalKnowledgePromotionPackage] = {}
        if existing is not None:
            for package in existing.packages:
                staged_map[package.candidate_id] = package

        queue_path = "07_运营数据/nlcl_feedback/formal_knowledge_promotion_queue.jsonl"
        queue_rows: list[dict[str, object]] = []
        force_status: FormalKnowledgePromotionStatus | None = None
        item_overrides: dict[str, dict[str, object]] = {}

        for decision in request.decisions:
            candidate = next(
                item for item in bundle.candidates if item.candidate_id == decision.candidate_id
            )
            action = decision.action
            if action is FormalKnowledgePromotionAction.STAGE_FOR_REVIEW:
                if candidate.promotion_status != "experience_recorded":
                    raise ChangeSetStateError(
                        "stage_for_review requires experience_recorded "
                        f"(candidate {candidate.candidate_id})"
                    )
                if candidate.regression_result != "pass_with_watch":
                    raise ChangeSetStateError(
                        "stage_for_review requires pass_with_watch regression result "
                        f"(candidate {candidate.candidate_id})"
                    )
                if candidate.formal_knowledge_contaminated is not False:
                    raise ChangeSetStateError(
                        "stage_for_review blocked by formal knowledge contamination flag"
                    )
                if candidate.formal_draft_allowed is not False:
                    raise ChangeSetStateError(
                        "stage_for_review blocked because formal_draft_allowed is not locked false"
                    )
                baseline = self._read_eval_corpus_baseline(
                    artifact_root / "eval-corpus-baseline-receipt.json"
                )
                if baseline is not None:
                    regression = self._read_eval_corpus_regression(
                        artifact_root / "eval-corpus-regression-receipt.json"
                    )
                    if (
                        regression is None
                        or regression.status is not EvalCorpusRegressionStatus.NON_DEGRADED
                        or regression.baseline_receipt_id != baseline.receipt_id
                    ):
                        raise ChangeSetStateError(
                            "stage_for_review requires eval corpus non_degraded "
                            "against the frozen baseline"
                        )
                package = self._build_formal_knowledge_promotion_package(
                    record=record,
                    candidate=candidate,
                )
                staged_map[candidate.candidate_id] = package
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_staged",
                    "staged_package_id": package.package_id,
                }
                queue_rows.append(
                    {
                        "bridge_schema": "zhimeng.formal-knowledge-promotion-queue-row.v1",
                        "package_id": package.package_id,
                        "package_hash": package.package_hash,
                        "candidate_id": package.candidate_id,
                        "change_set_id": record.change_set_id,
                        "project_id": record.project_id,
                        "finding_id": package.finding_id,
                        "finding_kind": package.finding_kind,
                        "summary": package.summary,
                        "proposed_card_type": package.proposed_card_type,
                        "proposed_card_title": package.proposed_card_title,
                        "writes_formal_knowledge": False,
                        "formal_knowledge_contaminated": False,
                        "formal_draft_allowed": False,
                        "staged_at": package.staged_at.isoformat().replace("+00:00", "Z"),
                    }
                )
                force_status = FormalKnowledgePromotionStatus.STAGED
                continue

            if action is FormalKnowledgePromotionAction.MATERIALIZE_FOR_REVIEW:
                package = staged_map.get(candidate.candidate_id)
                if package is None:
                    raise ChangeSetStateError(
                        "materialize_for_review requires a staged package "
                        f"(candidate {candidate.candidate_id})"
                    )
                if package.materialized_knowledge_candidate_id:
                    # Idempotent re-materialize of the same staged package.
                    item_overrides[candidate.candidate_id] = {
                        "promotion_status": "formal_promotion_materialized",
                        "staged_package_id": package.package_id,
                        "materialized_source_import_id": package.materialized_source_import_id,
                        "materialized_knowledge_candidate_id": (
                            package.materialized_knowledge_candidate_id
                        ),
                    }
                    force_status = FormalKnowledgePromotionStatus.MATERIALIZED
                    continue
                materialized = self._materialize_formal_knowledge_package(
                    record=record,
                    package=package,
                )
                staged_map[candidate.candidate_id] = materialized
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_materialized",
                    "staged_package_id": materialized.package_id,
                    "materialized_source_import_id": (
                        materialized.materialized_source_import_id
                    ),
                    "materialized_knowledge_candidate_id": (
                        materialized.materialized_knowledge_candidate_id
                    ),
                }
                force_status = FormalKnowledgePromotionStatus.MATERIALIZED
                continue

            if action is FormalKnowledgePromotionAction.AUTHOR_REQUEST_FORMALIZATION:
                package = staged_map.get(candidate.candidate_id)
                current = None
                if existing is not None:
                    current = next(
                        (
                            item
                            for item in existing.items
                            if item.candidate_id == candidate.candidate_id
                        ),
                        None,
                    )
                if package is None or not package.materialized_knowledge_candidate_id:
                    raise ChangeSetStateError(
                        "author_request_formalization requires a materialized package "
                        f"(candidate {candidate.candidate_id})"
                    )
                if current is not None and current.promotion_status not in {
                    "formal_promotion_materialized",
                    "formal_promotion_author_requested",
                    "formal_promotion_author_withheld",
                }:
                    raise ChangeSetStateError(
                        "author_request_formalization requires formal_promotion_materialized "
                        f"(candidate {candidate.candidate_id})"
                    )
                if package.writes_formal_knowledge is not False:
                    raise ChangeSetStateError(
                        "author_request_formalization blocked because writes_formal_knowledge is not locked false"
                    )
                if package.formal_draft_allowed is not False:
                    raise ChangeSetStateError(
                        "author_request_formalization blocked because formal_draft_allowed is not locked false"
                    )
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_author_requested",
                    "staged_package_id": package.package_id,
                    "materialized_source_import_id": package.materialized_source_import_id,
                    "materialized_knowledge_candidate_id": (
                        package.materialized_knowledge_candidate_id
                    ),
                    "author_formalization_intent": "request",
                }
                force_status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED
                continue

            if action is FormalKnowledgePromotionAction.AUTHOR_WITHHOLD_FORMALIZATION:
                package = staged_map.get(candidate.candidate_id)
                current = None
                if existing is not None:
                    current = next(
                        (
                            item
                            for item in existing.items
                            if item.candidate_id == candidate.candidate_id
                        ),
                        None,
                    )
                if package is None or not package.materialized_knowledge_candidate_id:
                    raise ChangeSetStateError(
                        "author_withhold_formalization requires a materialized package "
                        f"(candidate {candidate.candidate_id})"
                    )
                if current is not None and current.promotion_status not in {
                    "formal_promotion_materialized",
                    "formal_promotion_author_requested",
                    "formal_promotion_author_withheld",
                }:
                    raise ChangeSetStateError(
                        "author_withhold_formalization requires formal_promotion_materialized "
                        f"(candidate {candidate.candidate_id})"
                    )
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_author_withheld",
                    "staged_package_id": package.package_id,
                    "materialized_source_import_id": package.materialized_source_import_id,
                    "materialized_knowledge_candidate_id": (
                        package.materialized_knowledge_candidate_id
                    ),
                    "author_formalization_intent": "withhold",
                }
                force_status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD
                continue

            if action is FormalKnowledgePromotionAction.COMMIT_AUTHOR_ATTESTED_FORMAL_KNOWLEDGE:
                package = staged_map.get(candidate.candidate_id)
                current = None
                if existing is not None:
                    current = next(
                        (
                            item
                            for item in existing.items
                            if item.candidate_id == candidate.candidate_id
                        ),
                        None,
                    )
                if package is None or not package.materialized_knowledge_candidate_id:
                    raise ChangeSetStateError(
                        "commit_author_attested_formal_knowledge requires a materialized package "
                        f"(candidate {candidate.candidate_id})"
                    )
                if current is None or current.promotion_status not in {
                    "formal_promotion_author_requested",
                    "formal_promotion_author_committed",
                }:
                    raise ChangeSetStateError(
                        "commit_author_attested_formal_knowledge requires "
                        "formal_promotion_author_requested "
                        f"(candidate {candidate.candidate_id})"
                    )
                if package.formal_draft_allowed is not False:
                    raise ChangeSetStateError(
                        "commit_author_attested_formal_knowledge blocked because "
                        "formal_draft_allowed is not locked false"
                    )
                if package.formal_knowledge_contaminated is not False:
                    raise ChangeSetStateError(
                        "commit_author_attested_formal_knowledge blocked because "
                        "formal knowledge would be contaminated"
                    )
                committed = self._commit_author_attested_formal_knowledge(
                    record=record,
                    package=package,
                    existing_item=current,
                )
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_author_committed",
                    "staged_package_id": package.package_id,
                    "materialized_source_import_id": package.materialized_source_import_id,
                    "materialized_knowledge_candidate_id": (
                        package.materialized_knowledge_candidate_id
                    ),
                    "author_formalization_intent": "request",
                    "writes_formal_knowledge": True,
                    "committed_knowledge_card_id": committed["card_id"],
                    "committed_knowledge_version_id": committed["version_id"],
                }
                force_status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
                continue

            if action is FormalKnowledgePromotionAction.ROLLBACK_AUTHOR_ATTESTED_FORMAL_KNOWLEDGE:
                package = staged_map.get(candidate.candidate_id)
                current = None
                if existing is not None:
                    current = next(
                        (
                            item
                            for item in existing.items
                            if item.candidate_id == candidate.candidate_id
                        ),
                        None,
                    )
                if (
                    current is None
                    or current.promotion_status != "formal_promotion_author_committed"
                    or not current.committed_knowledge_card_id
                ):
                    raise ChangeSetStateError(
                        "rollback_author_attested_formal_knowledge requires "
                        "formal_promotion_author_committed "
                        f"(candidate {candidate.candidate_id})"
                    )
                knowledge_store = KnowledgeStore(self.store)
                scope = knowledge_store.ensure_project_scope(record.project_id)
                try:
                    card = knowledge_store.get_card(
                        current.committed_knowledge_card_id,
                        scope_id=scope.scope_id,
                    )
                    from zhimeng_runtime.knowledge_models import KnowledgeCardStatus
                    knowledge_store.set_card_status(
                        current.committed_knowledge_card_id,
                        status=KnowledgeCardStatus.DISABLED,
                        expected_state_revision=card.state_revision,
                        expected_active_version_id=card.active_version_id,
                        scope_id=scope.scope_id,
                    )
                except KnowledgeStoreError as exc:
                    raise ChangeSetStateError(
                        f"failed to disable author-attested formal knowledge card: {exc}"
                    ) from exc
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_rolled_back",
                    "staged_package_id": None if package is None else package.package_id,
                    "materialized_source_import_id": (
                        None if package is None else package.materialized_source_import_id
                    ),
                    "materialized_knowledge_candidate_id": (
                        None
                        if package is None
                        else package.materialized_knowledge_candidate_id
                    ),
                    "author_formalization_intent": "request",
                    "writes_formal_knowledge": False,
                    "committed_knowledge_card_id": current.committed_knowledge_card_id,
                    "committed_knowledge_version_id": current.committed_knowledge_version_id,
                }
                force_status = FormalKnowledgePromotionStatus.ROLLED_BACK
                continue

            if action is FormalKnowledgePromotionAction.REJECT:
                staged_map.pop(candidate.candidate_id, None)
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_rejected",
                    "staged_package_id": None,
                    "materialized_source_import_id": None,
                    "materialized_knowledge_candidate_id": None,
                }
                if force_status is None:
                    force_status = FormalKnowledgePromotionStatus.REJECTED
                continue

            if action is FormalKnowledgePromotionAction.ROLLBACK_STAGE:
                current = None
                if existing is not None:
                    current = next(
                        (
                            item
                            for item in existing.items
                            if item.candidate_id == candidate.candidate_id
                        ),
                        None,
                    )
                if current is None or current.promotion_status != "formal_promotion_staged":
                    raise ChangeSetStateError(
                        "rollback_stage requires formal_promotion_staged "
                        f"(candidate {candidate.candidate_id})"
                    )
                staged_map.pop(candidate.candidate_id, None)
                item_overrides[candidate.candidate_id] = {
                    "promotion_status": "formal_promotion_rolled_back",
                    "staged_package_id": None,
                }
                force_status = FormalKnowledgePromotionStatus.ROLLED_BACK
                continue

            raise ChangeSetInputError(
                f"unsupported formal knowledge promotion action: {action}"
            )

        rows_appended = 0
        if queue_rows:
            binding = self._require_binding(record.project_id)
            project_root = self.reader.resolve(binding.root_path)
            export_path = project_root.joinpath(*PurePosixPath(queue_path).parts)
            if not _is_within(export_path.parent, project_root):
                raise ChangeSetStateError("formal knowledge promotion queue path escapes project root")
            export_path.parent.mkdir(parents=True, exist_ok=True)
            for row in queue_rows:
                row_text = json.dumps(
                    row, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                row_bytes = (row_text + "\n").encode("utf-8")
                if export_path.exists():
                    value = export_path.lstat()
                    if not stat.S_ISREG(value.st_mode):
                        raise ChangeSetStateError(
                            "formal_knowledge_promotion_queue.jsonl must be a regular file"
                        )
                    if int(getattr(value, "st_nlink", 1)) > 1:
                        raise ChangeSetStateError(
                            "formal_knowledge_promotion_queue.jsonl cannot be a hardlink"
                        )
                    with export_path.open("ab") as handle:
                        handle.write(row_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    with export_path.open("xb") as handle:
                        handle.write(row_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                rows_appended += 1

        receipt = self._build_formal_knowledge_promotion_receipt(
            record=record,
            artifact_root=artifact_root,
            expected_bundle_hash=request.expected_feedback_candidate_bundle_hash,
            candidate_ids=decision_ids,
            action_decisions=tuple(request.decisions),
            force_status=force_status,
            item_overrides=item_overrides,
            packages=list(staged_map.values()),
            queue_relative_path=queue_path if rows_appended else None,
            queue_rows_appended=rows_appended,
        )
        history = self._persist_formal_knowledge_promotion_receipt(
            artifact_root=artifact_root,
            receipt=receipt,
        )
        return FormalKnowledgePromotionResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=history,
        )


    def _materialize_formal_knowledge_package(
        self,
        *,
        record: ChangeSetResponse,
        package: FormalKnowledgePromotionPackage,
    ) -> FormalKnowledgePromotionPackage:
        """Create a pending project knowledge candidate from a staged package.

        Uses synthetic feedback_experience source lineage. Does not formalize cards
        and never sets writes_formal_knowledge/formal_draft_allowed.
        """

        now = _now()
        source_import_id = (
            "source-import-"
            + hashlib.sha256(
                f"feedback-experience\0{package.package_id}\0{package.package_hash}"
                .encode("utf-8")
            ).hexdigest()[:32]
        )
        job = FeedbackSourceImportJob(
            import_id=source_import_id,
            provider="feedback_experience",
            query=package.package_id,
            candidate=FeedbackExperienceCandidate(
                package_id=package.package_id,
                change_set_id=package.change_set_id,
                feedback_candidate_id=package.candidate_id,
                package_hash=package.package_hash,
                title=package.proposed_card_title,
            ),
            status=SourceImportStatus.AWAITING_REVIEW,
            progress=SourceImportProgress(
                stage="awaiting_review",
                message=(
                    "feedback experience package staged for project knowledge review; "
                    "writes_formal_knowledge=false"
                ),
            ),
            source_packet=None,
            distillation_draft=None,
            error=None,
            created_at=now,
            updated_at=now,
        )
        payload = job.model_dump(mode="json", by_alias=True)
        try:
            self.store.create_source_import_job(payload)
        except RuntimeError as exc:
            # Identity already exists: reuse if it is the same feedback package.
            existing = self.store.get_source_import_job(source_import_id)
            if existing is None:
                raise ChangeSetStateError(
                    "failed to create feedback_experience source import"
                ) from exc
            if (
                str(existing.get("provider")) != "feedback_experience"
                or str(existing.get("query")) != package.package_id
            ):
                raise ChangeSetConflictError(
                    "feedback_experience source import identity conflict"
                ) from exc

        card_id = "feedback-validator-" + package.package_id[-24:]
        card = {
            "card_id": card_id,
            "card_type": "validator",
            "title": package.proposed_card_title,
            "summary": package.summary,
            "body": package.proposed_card_body,
            "source_kind": "feedback_experience",
            "finding_id": package.finding_id,
            "finding_kind": package.finding_kind,
            "evidence_quote": package.evidence_quote,
            "relative_path": package.relative_path,
            "experience_relative_path": package.experience_relative_path,
            "change_set_id": package.change_set_id,
            "package_id": package.package_id,
            "package_hash": package.package_hash,
            "formal_knowledge_contaminated": False,
            "formal_draft_allowed": False,
            "writes_formal_knowledge": False,
            "quality_assessed": False,
        }
        content_hash = knowledge_card_hash(card)
        knowledge_candidate_id = (
            "knowledge-candidate-"
            + hashlib.sha256(
                f"{source_import_id}\0{card_id}\0{content_hash}".encode("utf-8")
            ).hexdigest()[:32]
        )
        candidate = KnowledgeCandidate(
            candidate_id=knowledge_candidate_id,
            source_import_id=source_import_id,
            source_card_index=0,
            source_draft_hash=content_hash,
            lineage_status=KnowledgeLineageStatus.LEGACY_UNVERIFIED,
            origin=None,
            runtime_distillation_invocation_id=None,
            card_id=card_id,
            card_type="validator",
            status=KnowledgeCandidateStatus.PENDING,
            revision=1,
            content_hash=content_hash,
            card=card,
            issues=[
                KnowledgeReviewIssue(
                    code="feedback_experience_lineage",
                    field="source_kind",
                    message=(
                        "候选来自反馈经验物化，lineage=legacy_unverified；"
                        "未写入正式知识，不可自动正式化。"
                    ),
                )
            ],
            promoted_version_id=None,
            created_at=now,
            updated_at=now,
        )
        knowledge_store = KnowledgeStore(self.store)
        scope = knowledge_store.ensure_project_scope(record.project_id)
        created = knowledge_store.create_candidates(
            [candidate],
            scope_id=scope.scope_id,
        )
        created_candidate = created[0]
        provisional = package.model_copy(
            update={
                "materialized_source_import_id": source_import_id,
                "materialized_knowledge_candidate_id": created_candidate.candidate_id,
                "package_hash": "0" * 64,
            }
        )
        return FormalKnowledgePromotionPackage(
            package_id=package.package_id,
            change_set_id=package.change_set_id,
            project_id=package.project_id,
            candidate_id=package.candidate_id,
            finding_id=package.finding_id,
            finding_kind=package.finding_kind,
            summary=package.summary,
            evidence_quote=package.evidence_quote,
            relative_path=package.relative_path,
            proposed_card_type=package.proposed_card_type,
            proposed_card_title=package.proposed_card_title,
            proposed_card_body=package.proposed_card_body,
            source_kind=package.source_kind,
            experience_relative_path=package.experience_relative_path,
            formal_knowledge_contaminated=False,
            formal_draft_allowed=False,
            writes_formal_knowledge=False,
            quality_assessed=False,
            staged_at=package.staged_at,
            materialized_source_import_id=source_import_id,
            materialized_knowledge_candidate_id=created_candidate.candidate_id,
            package_hash=formal_knowledge_promotion_package_hash(provisional),
        )

    def _commit_author_attested_formal_knowledge(
        self,
        *,
        record: ChangeSetResponse,
        package: FormalKnowledgePromotionPackage,
        existing_item: FormalKnowledgePromotionItem | None,
    ) -> dict[str, str]:
        """Write formal knowledge from an author-requested feedback package.

        Does not verify lineage for Context injection and never unlocks formal_draft.
        """
        knowledge_candidate_id = package.materialized_knowledge_candidate_id
        if not knowledge_candidate_id:
            raise ChangeSetStateError(
                "author-attested formal knowledge commit requires materialized candidate"
            )
        if (
            existing_item is not None
            and existing_item.promotion_status == "formal_promotion_author_committed"
            and existing_item.committed_knowledge_card_id
            and existing_item.committed_knowledge_version_id
            and existing_item.writes_formal_knowledge is True
        ):
            return {
                "card_id": existing_item.committed_knowledge_card_id,
                "version_id": existing_item.committed_knowledge_version_id,
            }

        knowledge_store = KnowledgeStore(self.store)
        scope = knowledge_store.ensure_project_scope(record.project_id)
        try:
            candidate = knowledge_store.get_candidate(
                knowledge_candidate_id,
                scope_id=scope.scope_id,
            )
        except KnowledgeStoreError as exc:
            raise ChangeSetStateError(
                f"materialized knowledge candidate unavailable: {knowledge_candidate_id}"
            ) from exc

        # provisional receipt id only for audit binding; real receipt id is rebuilt later
        provisional_receipt_id = (
            "formal-knowledge-promotion-"
            + hashlib.sha256(
                f"author-commit\0{record.change_set_id}\0{package.package_id}".encode("utf-8")
            ).hexdigest()[:32]
        )
        try:
            card = knowledge_store.promote_author_attested_feedback_candidate(
                knowledge_candidate_id,
                expected_revision=candidate.revision,
                expected_content_hash=candidate.content_hash,
                author_promotion_receipt_id=provisional_receipt_id,
                scope_id=scope.scope_id,
            )
        except KnowledgeStoreError as exc:
            raise ChangeSetStateError(
                f"author-attested formal knowledge write failed: {exc}"
            ) from exc

        # Context injection remains blocked for legacy_unverified formalization.
        if (
            card.active_version.lineage_status is not KnowledgeLineageStatus.LEGACY_UNVERIFIED
            or card.active_version.formalization_status
            is not KnowledgeVersionFormalizationStatus.LEGACY_UNVERIFIED
        ):
            raise ChangeSetStateError(
                "author-attested formal knowledge write must remain legacy_unverified "
                "and non-activated for Context"
            )
        if card.active_version.formalization is not None:
            raise ChangeSetStateError(
                "author-attested formal knowledge write must not create formalization activation"
            )
        return {
            "card_id": card.card_id,
            "version_id": card.active_version_id,
        }

    def _build_formal_knowledge_promotion_package(
        self,
        *,
        record: ChangeSetResponse,
        candidate: FeedbackCandidate,
    ) -> FormalKnowledgePromotionPackage:
        staged_at = _now()
        package_id = (
            "formal-knowledge-promotion-package-"
            + hashlib.sha256(
                f"{record.change_set_id}\0{candidate.candidate_id}\0{candidate.summary}"
                .encode("utf-8")
            ).hexdigest()[:32]
        )
        title = f"反馈经验正式知识候选：{candidate.finding_id}"
        body = (
            f"来源：Reader 反馈经验库（非自动正式知识）。\n"
            f"finding_id={candidate.finding_id}\n"
            f"finding_kind={candidate.finding_kind}\n"
            f"summary={candidate.summary}\n"
            f"evidence={candidate.evidence_quote or '(none)'}\n"
            f"relative_path={candidate.relative_path or '(none)'}\n"
            "边界：writes_formal_knowledge=false；仅 stage-for-review。"
        )
        provisional = FormalKnowledgePromotionPackage.model_construct(
            schema_id="zhimeng.formal-knowledge-promotion-package.v1",
            package_id=package_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            candidate_id=candidate.candidate_id,
            finding_id=candidate.finding_id,
            finding_kind=candidate.finding_kind,
            summary=candidate.summary,
            evidence_quote=candidate.evidence_quote,
            relative_path=candidate.relative_path,
            proposed_card_type="validator",
            proposed_card_title=title[:256],
            proposed_card_body=body[:4000],
            source_kind="feedback_experience",
            experience_relative_path=(
                candidate.experience_relative_path
                or "07_运营数据/nlcl_feedback/experience_candidates.jsonl"
            ),
            formal_knowledge_contaminated=False,
            formal_draft_allowed=False,
            writes_formal_knowledge=False,
            quality_assessed=False,
            staged_at=staged_at,
            package_hash="0" * 64,
        )
        return FormalKnowledgePromotionPackage(
            package_id=package_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            candidate_id=candidate.candidate_id,
            finding_id=candidate.finding_id,
            finding_kind=candidate.finding_kind,
            summary=candidate.summary,
            evidence_quote=candidate.evidence_quote,
            relative_path=candidate.relative_path,
            proposed_card_type="validator",
            proposed_card_title=title[:256],
            proposed_card_body=body[:4000],
            source_kind="feedback_experience",
            experience_relative_path=(
                candidate.experience_relative_path
                or "07_运营数据/nlcl_feedback/experience_candidates.jsonl"
            ),
            formal_knowledge_contaminated=False,
            formal_draft_allowed=False,
            writes_formal_knowledge=False,
            quality_assessed=False,
            staged_at=staged_at,
            package_hash=formal_knowledge_promotion_package_hash(provisional),
        )

    def _build_formal_knowledge_promotion_receipt(
        self,
        *,
        record: ChangeSetResponse,
        artifact_root: Path,
        expected_bundle_hash: str | None,
        candidate_ids: list[str],
        action_decisions: tuple[FormalKnowledgePromotionDecision, ...],
        force_status: FormalKnowledgePromotionStatus | None,
        item_overrides: dict[str, dict[str, object]] | None = None,
        packages: list[FormalKnowledgePromotionPackage] | None = None,
        queue_relative_path: str | None = None,
        queue_rows_appended: int = 0,
    ) -> FormalKnowledgePromotionReceipt:
        item_overrides = item_overrides or {}
        packages = packages or []
        bundle = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        if expected_bundle_hash is not None and bundle is not None:
            if bundle.receipt_hash != expected_bundle_hash:
                raise ChangeSetConflictError(
                    "feedback candidate bundle hash does not match the expected value"
                )

        terminal = record.status in {
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.ROLLED_BACK,
        }
        selected_ids = set(candidate_ids)
        candidates = []
        if bundle is not None:
            if selected_ids:
                candidates = [
                    item for item in bundle.candidates if item.candidate_id in selected_ids
                ]
            else:
                candidates = [
                    item
                    for item in bundle.candidates
                    if item.promotion_status == "experience_recorded"
                    or item.candidate_id in item_overrides
                ]

        experience_candidates = [
            item
            for item in candidates
            if item.promotion_status == "experience_recorded"
            or item_overrides.get(item.candidate_id, {}).get("promotion_status")
            in {
                "formal_promotion_staged",
                "formal_promotion_materialized",
                "formal_promotion_author_requested",
                "formal_promotion_author_withheld",
                "formal_promotion_author_committed",
                "formal_promotion_rejected",
                "formal_promotion_rolled_back",
                "experience_recorded",
            }
        ]
        regression_ok = bool(experience_candidates) and all(
            item.regression_result == "pass_with_watch" for item in experience_candidates
        )
        no_contamination = all(
            item.formal_knowledge_contaminated is False for item in experience_candidates
        ) if experience_candidates else False
        formal_draft_locked = all(
            item.formal_draft_allowed is False for item in experience_candidates
        ) if experience_candidates else False

        checks = [
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.CHANGE_SET_TERMINAL,
                required=True,
                present=terminal,
                detail=(
                    f"change set status={record.status.value if hasattr(record.status, 'value') else record.status}"
                    if terminal
                    else "change set is not applied/rolled_back"
                ),
            ),
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.FEEDBACK_BUNDLE_PRESENT,
                required=True,
                present=bundle is not None,
                detail=(
                    f"feedback candidate bundle present ({bundle.bundle_id})"
                    if bundle is not None
                    else "feedback candidate bundle missing"
                ),
            ),
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.EXPERIENCE_RECORDED,
                required=True,
                present=bool(experience_candidates),
                detail=(
                    f"{len(experience_candidates)} experience-backed candidate(s) selected"
                    if experience_candidates
                    else "no experience_recorded feedback candidates available"
                ),
            ),
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.REGRESSION_PASS,
                required=True,
                present=regression_ok,
                detail=(
                    "all selected experience candidates passed regression with watch"
                    if regression_ok
                    else "regression pass_with_watch is required for formal knowledge promotion staging"
                ),
            ),
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.NO_FORMAL_CONTAMINATION,
                required=True,
                present=no_contamination,
                detail=(
                    "formal_knowledge_contaminated remains false"
                    if no_contamination
                    else "formal knowledge contamination boundary is not satisfied"
                ),
            ),
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.FORMAL_DRAFT_LOCKED,
                required=True,
                present=formal_draft_locked,
                detail=(
                    "formal_draft_allowed remains locked false"
                    if formal_draft_locked
                    else "formal_draft_allowed is not locked false on selected candidates"
                ),
            ),
        ]
        baseline = self._read_eval_corpus_baseline(
            artifact_root / "eval-corpus-baseline-receipt.json"
        )
        regression = self._read_eval_corpus_regression(
            artifact_root / "eval-corpus-regression-receipt.json"
        )
        if baseline is None:
            eval_required = False
            eval_present = True
            eval_detail = (
                "no eval corpus baseline frozen; non-regression deferred"
            )
        else:
            eval_required = True
            eval_present = (
                regression is not None
                and regression.status is EvalCorpusRegressionStatus.NON_DEGRADED
                and regression.baseline_receipt_id == baseline.receipt_id
            )
            if eval_present:
                eval_detail = (
                    f"eval corpus non_degraded against baseline {baseline.receipt_id}"
                )
            elif regression is None:
                eval_detail = (
                    f"frozen baseline {baseline.receipt_id} requires eval corpus "
                    "non-regression compare"
                )
            else:
                status_text = (
                    regression.status.value
                    if hasattr(regression.status, "value")
                    else str(regression.status)
                )
                eval_detail = (
                    f"eval corpus regression status={status_text}; "
                    f"required non_degraded for baseline {baseline.receipt_id}"
                )
        checks.append(
            FormalKnowledgePromotionCheck(
                code=FormalKnowledgePromotionCheckCode.EVAL_CORPUS_BASELINE_GATE,
                required=eval_required,
                present=eval_present,
                detail=eval_detail,
            )
        )
        missing_required = [
            item.code for item in checks if item.required and not item.present
        ]

        package_by_candidate = {item.candidate_id: item for item in packages}
        items: list[FormalKnowledgePromotionItem] = []
        for candidate in experience_candidates or candidates:
            override = item_overrides.get(candidate.candidate_id, {})
            promotion_status = str(
                override.get("promotion_status") or candidate.promotion_status
            )
            if promotion_status not in {
                "experience_recorded",
                "formal_promotion_staged",
                "formal_promotion_materialized",
                "formal_promotion_author_requested",
                "formal_promotion_author_withheld",
                "formal_promotion_author_committed",
                "formal_promotion_rejected",
                "formal_promotion_rolled_back",
            }:
                # Only report experience-backed or formal-promotion statuses.
                if candidate.promotion_status != "experience_recorded":
                    continue
                promotion_status = "experience_recorded"
            staged_package_id = override.get("staged_package_id")
            package = package_by_candidate.get(candidate.candidate_id)
            if staged_package_id is None and package is not None:
                staged_package_id = package.package_id
            materialized_source_import_id = override.get("materialized_source_import_id")
            if materialized_source_import_id is None and package is not None:
                materialized_source_import_id = package.materialized_source_import_id
            materialized_knowledge_candidate_id = override.get(
                "materialized_knowledge_candidate_id"
            )
            if materialized_knowledge_candidate_id is None and package is not None:
                materialized_knowledge_candidate_id = (
                    package.materialized_knowledge_candidate_id
                )
            items.append(
                FormalKnowledgePromotionItem(
                    candidate_id=candidate.candidate_id,
                    finding_id=candidate.finding_id,
                    finding_kind=candidate.finding_kind,
                    summary=candidate.summary,
                    evidence_quote=candidate.evidence_quote,
                    relative_path=candidate.relative_path,
                    experience_relative_path=candidate.experience_relative_path,
                    promotion_status=promotion_status,  # type: ignore[arg-type]
                    regression_result=candidate.regression_result,
                    staged_package_id=(
                        None if staged_package_id is None else str(staged_package_id)
                    ),
                    materialized_source_import_id=(
                        None
                        if materialized_source_import_id is None
                        else str(materialized_source_import_id)
                    ),
                    materialized_knowledge_candidate_id=(
                        None
                        if materialized_knowledge_candidate_id is None
                        else str(materialized_knowledge_candidate_id)
                    ),
                    formal_knowledge_contaminated=False,
                    formal_draft_allowed=False,
                    writes_formal_knowledge=bool(
                        override.get("writes_formal_knowledge", False)
                    ),
                    committed_knowledge_card_id=(
                        None
                        if override.get("committed_knowledge_card_id") is None
                        else str(override.get("committed_knowledge_card_id"))
                    ),
                    committed_knowledge_version_id=(
                        None
                        if override.get("committed_knowledge_version_id") is None
                        else str(override.get("committed_knowledge_version_id"))
                    ),
                )
            )

        if force_status is not None:
            status = force_status
        elif missing_required:
            status = FormalKnowledgePromotionStatus.INCOMPLETE
        elif any(
            item.promotion_status == "formal_promotion_author_committed" for item in items
        ):
            status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
        elif any(
            item.promotion_status == "formal_promotion_author_requested" for item in items
        ):
            status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED
        elif any(
            item.promotion_status == "formal_promotion_author_withheld" for item in items
        ):
            status = FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD
        elif any(
            item.promotion_status == "formal_promotion_materialized" for item in items
        ):
            status = FormalKnowledgePromotionStatus.MATERIALIZED
        elif any(
            item.promotion_status == "formal_promotion_staged" for item in items
        ):
            status = FormalKnowledgePromotionStatus.STAGED
        else:
            status = FormalKnowledgePromotionStatus.READY

        if status is FormalKnowledgePromotionStatus.STAGED and missing_required:
            status = FormalKnowledgePromotionStatus.BLOCKED

        evaluated_at = _now()
        receipt_id = (
            "formal-knowledge-promotion-"
            + hashlib.sha256(
                "\0".join(
                    [
                        record.change_set_id,
                        record.project_id,
                        status.value,
                        ",".join(item.value for item in missing_required),
                        ",".join(item.candidate_id for item in items),
                        evaluated_at.isoformat(),
                    ]
                ).encode("utf-8")
            ).hexdigest()[:32]
        )
        provisional = FormalKnowledgePromotionReceipt.model_construct(
            schema_id="zhimeng.formal-knowledge-promotion-receipt.v1",
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            feedback_candidate_bundle_id=(
                None if bundle is None else bundle.bundle_id
            ),
            feedback_candidate_bundle_hash=(
                None if bundle is None else bundle.receipt_hash
            ),
            status=status,
            authorization_input_ready=(
                status
                in {
                    FormalKnowledgePromotionStatus.READY,
                    FormalKnowledgePromotionStatus.STAGED,
                    FormalKnowledgePromotionStatus.MATERIALIZED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED,
                }
                and not missing_required
            ),
            author_formalization_intent=(
                "request"
                if status
                in {
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED,
                }
                else (
                    "withhold"
                    if status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD
                    else "none"
                )
            ),
            author_formalization_effective=(
                status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
            ),
            formal_knowledge_contaminated=False,
            formal_draft_allowed=False,
            writes_formal_knowledge=(
                status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
            ),
            quality_assessed=False,
            checks=checks,
            missing_required=missing_required,
            items=items,
            packages=packages,
            queue_relative_path=queue_relative_path,
            queue_rows_appended=queue_rows_appended,
            policy_version="local-v1-formal-knowledge-promotion-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        return FormalKnowledgePromotionReceipt(
            receipt_id=receipt_id,
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            feedback_candidate_bundle_id=(
                None if bundle is None else bundle.bundle_id
            ),
            feedback_candidate_bundle_hash=(
                None if bundle is None else bundle.receipt_hash
            ),
            status=status,
            authorization_input_ready=(
                status
                in {
                    FormalKnowledgePromotionStatus.READY,
                    FormalKnowledgePromotionStatus.STAGED,
                    FormalKnowledgePromotionStatus.MATERIALIZED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED,
                }
                and not missing_required
            ),
            author_formalization_intent=(
                "request"
                if status
                in {
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_REQUESTED,
                    FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED,
                }
                else (
                    "withhold"
                    if status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_WITHHELD
                    else "none"
                )
            ),
            author_formalization_effective=(
                status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
            ),
            formal_knowledge_contaminated=False,
            formal_draft_allowed=False,
            writes_formal_knowledge=(
                status is FormalKnowledgePromotionStatus.AUTHOR_FORMALIZATION_COMMITTED
            ),
            quality_assessed=False,
            checks=checks,
            missing_required=missing_required,
            items=items,
            packages=packages,
            queue_relative_path=queue_relative_path,
            queue_rows_appended=queue_rows_appended,
            policy_version="local-v1-formal-knowledge-promotion-1",
            evaluated_at=evaluated_at,
            receipt_hash=formal_knowledge_promotion_receipt_hash(provisional),
        )

    def _persist_formal_knowledge_promotion_receipt(
        self,
        *,
        artifact_root: Path,
        receipt: FormalKnowledgePromotionReceipt,
    ) -> list[FormalKnowledgePromotionReceipt]:
        artifact_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            artifact_root / "formal-knowledge-promotion-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        history_path = artifact_root / "formal-knowledge-promotion-history.json"
        history = self._read_formal_knowledge_promotion_history(history_path)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return history

    def _read_formal_knowledge_promotion(
        self,
        path: Path,
    ) -> FormalKnowledgePromotionReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FormalKnowledgePromotionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_formal_knowledge_promotion_history(
        self,
        path: Path,
    ) -> list[FormalKnowledgePromotionReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[FormalKnowledgePromotionReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(FormalKnowledgePromotionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def get_feedback_candidates(self, change_set_id: str) -> FeedbackCandidateResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        history = self._read_feedback_candidate_history(
            artifact_root / "feedback-candidate-history.json"
        )
        return FeedbackCandidateResponse(
            change_set_id=record.change_set_id,
            bundle=latest,
            history=history,
        )


    def get_feedback_candidate_promotion(
        self,
        change_set_id: str,
    ) -> FeedbackCandidatePromotionResponse:
        record = self.get(change_set_id)
        artifact_root = self._artifact_root(record.change_set_id)
        latest = self._read_feedback_candidate_promotion(
            artifact_root / "feedback-candidate-promotion-receipt.json"
        )
        history = self._read_feedback_candidate_promotion_history(
            artifact_root / "feedback-candidate-promotion-history.json"
        )
        bundle = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        return FeedbackCandidatePromotionResponse(
            change_set_id=record.change_set_id,
            receipt=latest,
            history=history,
            bundle=bundle,
        )

    def promote_feedback_candidates(
        self,
        change_set_id: str,
        request: FeedbackCandidatePromotionRequest,
    ) -> FeedbackCandidatePromotionResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.APPLIED, ChangeSetStatus.ROLLED_BACK}:
            raise ChangeSetStateError(
                "feedback candidate promotion requires an applied or rolled_back ChangeSet"
            )
        artifact_root = self._artifact_root(record.change_set_id)
        bundle = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        if bundle is None:
            raise ChangeSetStateError(
                "feedback candidate bundle is required before promotion"
            )
        if bundle.receipt_hash != request.expected_feedback_candidate_bundle_hash:
            raise ChangeSetConflictError(
                "feedback candidate bundle hash does not match the expected value"
            )

        decision_ids = [item.candidate_id for item in request.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ChangeSetInputError("promotion decisions must be unique by candidate_id")
        candidates_by_id = {item.candidate_id: item for item in bundle.candidates}
        unknown = [item for item in decision_ids if item not in candidates_by_id]
        if unknown:
            raise ChangeSetInputError(
                "unknown feedback candidate ids: " + ", ".join(unknown[:8])
            )

        existing = self._read_feedback_candidate_promotion(
            artifact_root / "feedback-candidate-promotion-receipt.json"
        )
        decision_key = {
            "bundle_hash": bundle.receipt_hash,
            "decisions": [
                item.model_dump(mode="json")
                for item in sorted(request.decisions, key=lambda row: row.candidate_id)
            ],
        }
        if existing is not None:
            existing_key = {
                "bundle_hash": existing.feedback_candidate_bundle_hash,
                "decisions": [
                    item.model_dump(mode="json")
                    for item in sorted(existing.decisions, key=lambda row: row.candidate_id)
                ],
            }
            if existing_key == decision_key:
                history = self._read_feedback_candidate_promotion_history(
                    artifact_root / "feedback-candidate-promotion-history.json"
                )
                refreshed = self._read_feedback_candidate_bundle(
                    artifact_root / "feedback-candidate-bundle.json"
                )
                return FeedbackCandidatePromotionResponse(
                    change_set_id=record.change_set_id,
                    receipt=existing,
                    history=history,
                    bundle=refreshed,
                )

        working = {
            item.candidate_id: item.model_copy(deep=True)
            for item in bundle.candidates
        }
        experience_path = "07_\u8fd0\u8425\u6570\u636e/nlcl_feedback/experience_candidates.jsonl"
        experience_rows: list[dict[str, object]] = []

        for decision in request.decisions:
            candidate = working[decision.candidate_id]
            status = candidate.promotion_status
            action = decision.action

            if action == "reject":
                if status in {"experience_recorded"}:
                    raise ChangeSetStateError(
                        "experience-recorded candidates must use rollback_experience before reject"
                    )
                working[decision.candidate_id] = candidate.model_copy(
                    update={
                        "status": "rejected",
                        "promotion_status": "rejected",
                        "regression_result": "skipped_rejected",
                    }
                )
                continue

            if action == "verify_source":
                if status not in {"not_promoted", "rolled_back"}:
                    raise ChangeSetStateError(
                        f"verify_source requires not_promoted/rolled_back, got {status}"
                    )
                if candidate.confidence != "author_explicit":
                    raise ChangeSetStateError(
                        "verify_source requires author_explicit confidence"
                    )
                if not candidate.source_kind:
                    raise ChangeSetStateError("verify_source requires source_kind")
                working[decision.candidate_id] = candidate.model_copy(
                    update={"promotion_status": "source_verified", "status": "candidate"}
                )
                continue

            if action == "check_conflicts":
                if status != "source_verified":
                    raise ChangeSetStateError(
                        f"check_conflicts requires source_verified, got {status}"
                    )
                conflict = False
                for other_id, other in working.items():
                    if other_id == candidate.candidate_id:
                        continue
                    if (
                        other.finding_id == candidate.finding_id
                        and other.summary == candidate.summary
                        and other.promotion_status
                        not in {"rejected", "rolled_back", "not_promoted"}
                    ):
                        conflict = True
                        break
                if conflict:
                    raise ChangeSetStateError(
                        f"conflict detected for candidate {candidate.candidate_id}"
                    )
                working[decision.candidate_id] = candidate.model_copy(
                    update={"promotion_status": "conflict_checked"}
                )
                continue

            if action == "evaluate_regression":
                if status != "conflict_checked":
                    raise ChangeSetStateError(
                        f"evaluate_regression requires conflict_checked, got {status}"
                    )
                # Deterministic literary-agnostic regression gate:
                # fact_risk remains watch-pass; continuity/subjective pass-with-watch.
                # Hard fail only if evidence is empty for fact_risk.
                if candidate.finding_kind == "fact_risk" and not candidate.evidence_quote.strip():
                    working[decision.candidate_id] = candidate.model_copy(
                        update={
                            "promotion_status": "regression_evaluated",
                            "regression_result": "fail_blocked",
                        }
                    )
                else:
                    working[decision.candidate_id] = candidate.model_copy(
                        update={
                            "promotion_status": "regression_evaluated",
                            "regression_result": "pass_with_watch",
                        }
                    )
                continue

            if action == "approve":
                if status != "regression_evaluated":
                    raise ChangeSetStateError(
                        f"approve requires regression_evaluated, got {status}"
                    )
                if candidate.regression_result == "fail_blocked":
                    raise ChangeSetStateError(
                        "approve blocked by failed regression evaluation"
                    )
                if candidate.regression_result != "pass_with_watch":
                    raise ChangeSetStateError(
                        "approve requires pass_with_watch regression result"
                    )
                working[decision.candidate_id] = candidate.model_copy(
                    update={"promotion_status": "author_approved"}
                )
                continue

            if action == "record_experience":
                if status != "author_approved":
                    raise ChangeSetStateError(
                        f"record_experience requires author_approved, got {status}"
                    )
                row = {
                    "bridge_schema": "zhimeng.feedback-experience-row.v1",
                    "candidate_id": candidate.candidate_id,
                    "change_set_id": record.change_set_id,
                    "project_id": record.project_id,
                    "finding_id": candidate.finding_id,
                    "finding_kind": candidate.finding_kind,
                    "source_decision": candidate.source_decision,
                    "summary": candidate.summary,
                    "evidence_quote": candidate.evidence_quote,
                    "relative_path": candidate.relative_path,
                    "recorded_at": _now().isoformat().replace("+00:00", "Z"),
                    "formal_knowledge_contaminated": False,
                    "formal_draft_allowed": False,
                }
                experience_rows.append(row)
                working[decision.candidate_id] = candidate.model_copy(
                    update={
                        "status": "experience",
                        "promotion_status": "experience_recorded",
                        "experience_relative_path": experience_path,
                    }
                )
                continue

            if action == "rollback_experience":
                if status != "experience_recorded":
                    raise ChangeSetStateError(
                        f"rollback_experience requires experience_recorded, got {status}"
                    )
                working[decision.candidate_id] = candidate.model_copy(
                    update={
                        "status": "candidate",
                        "promotion_status": "rolled_back",
                        "experience_relative_path": None,
                        "regression_result": "not_run",
                    }
                )
                continue

            raise ChangeSetInputError(f"unsupported promotion action: {action}")

        rows_appended = 0
        if experience_rows:
            binding = self._require_binding(record.project_id)
            project_root = self.reader.resolve(binding.root_path)
            export_path = project_root.joinpath(*PurePosixPath(experience_path).parts)
            if not _is_within(export_path.parent, project_root):
                raise ChangeSetStateError("experience path escapes project root")
            export_path.parent.mkdir(parents=True, exist_ok=True)
            for row in experience_rows:
                row_text = json.dumps(
                    row, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                )
                row_bytes = (row_text + "\n").encode("utf-8")
                if export_path.exists():
                    value = export_path.lstat()
                    if not stat.S_ISREG(value.st_mode):
                        raise ChangeSetStateError(
                            "experience_candidates.jsonl must be a regular file"
                        )
                    if int(getattr(value, "st_nlink", 1)) > 1:
                        raise ChangeSetStateError(
                            "experience_candidates.jsonl cannot be a hardlink"
                        )
                    with export_path.open("ab") as handle:
                        handle.write(row_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                else:
                    with export_path.open("xb") as handle:
                        handle.write(row_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                rows_appended += 1

        updated_candidates = [working[item.candidate_id] for item in bundle.candidates]
        bundle_material = {
            "schema": "zhimeng.feedback-candidate-bundle.v1",
            "change_set_id": bundle.change_set_id,
            "project_id": bundle.project_id,
            "reader_review_decision_receipt_id": bundle.reader_review_decision_receipt_id,
            "reader_review_decision_receipt_hash": (
                bundle.reader_review_decision_receipt_hash
            ),
            "follow_up_change_set_id": bundle.follow_up_change_set_id,
            "candidates": [item.model_dump(mode="json") for item in updated_candidates],
            "formal_knowledge_contaminated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        # Keep stable bundle_id; recompute receipt_hash over new candidate states.
        created_at = bundle.created_at
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        new_bundle_hash = canonical_json_hash(
            {
                **bundle_material,
                "bundle_id": bundle.bundle_id,
                "created_at": created_at_text,
            }
        )
        updated_bundle = FeedbackCandidateBundle.model_validate(
            {
                **bundle_material,
                "bundle_id": bundle.bundle_id,
                "created_at": created_at,
                "receipt_hash": new_bundle_hash,
            }
        )
        _atomic_write_json(
            artifact_root / "feedback-candidate-bundle.json",
            updated_bundle.model_dump(mode="json", by_alias=True),
        )
        history_path = artifact_root / "feedback-candidate-history.json"
        history = self._read_feedback_candidate_history(history_path)
        history = [item for item in history if item.bundle_id != updated_bundle.bundle_id]
        history.append(updated_bundle)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )

        material = {
            "schema": "zhimeng.feedback-candidate-promotion-receipt.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "feedback_candidate_bundle_id": updated_bundle.bundle_id,
            "feedback_candidate_bundle_hash": updated_bundle.receipt_hash,
            "decisions": [item.model_dump(mode="json") for item in request.decisions],
            "resulting_candidates": [
                item.model_dump(mode="json") for item in updated_candidates
            ],
            "experience_relative_path": experience_path if rows_appended else None,
            "experience_rows_appended": rows_appended,
            "formal_knowledge_contaminated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        receipt_id = f"feedback-candidate-promotion-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at_text,
            }
        )
        receipt = FeedbackCandidatePromotionReceipt.model_validate(
            {
                **material,
                "receipt_id": receipt_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        _atomic_write_json(
            artifact_root / "feedback-candidate-promotion-receipt.json",
            receipt.model_dump(mode="json", by_alias=True),
        )
        promo_history_path = artifact_root / "feedback-candidate-promotion-history.json"
        promo_history = self._read_feedback_candidate_promotion_history(promo_history_path)
        promo_history = [
            item for item in promo_history if item.receipt_id != receipt.receipt_id
        ]
        promo_history.append(receipt)
        promo_history = promo_history[-32:]
        _atomic_write_json(
            promo_history_path,
            [item.model_dump(mode="json", by_alias=True) for item in promo_history],
        )
        return FeedbackCandidatePromotionResponse(
            change_set_id=record.change_set_id,
            receipt=receipt,
            history=promo_history,
            bundle=updated_bundle,
        )

    def _read_feedback_candidate_promotion(
        self,
        path: Path,
    ) -> FeedbackCandidatePromotionReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FeedbackCandidatePromotionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_feedback_candidate_promotion_history(
        self,
        path: Path,
    ) -> list[FeedbackCandidatePromotionReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[FeedbackCandidatePromotionReceipt] = []
        for item in payload:
            try:
                history.append(FeedbackCandidatePromotionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _record_feedback_candidates_from_reader_decision(
        self,
        *,
        record: ChangeSetResponse,
        reader: ReaderReviewReceipt,
        decision_receipt: ReaderReviewDecisionReceipt,
    ) -> FeedbackCandidateBundle:
        artifact_root = self._artifact_root(record.change_set_id)
        existing = self._read_feedback_candidate_bundle(
            artifact_root / "feedback-candidate-bundle.json"
        )
        if (
            existing is not None
            and existing.reader_review_decision_receipt_hash
            == decision_receipt.receipt_hash
        ):
            return existing

        findings = {item.finding_id: item for item in reader.findings}
        candidates: list[FeedbackCandidate] = []
        for item in decision_receipt.decisions:
            finding = findings.get(item.finding_id)
            if finding is None:
                continue
            seed = {
                "change_set_id": record.change_set_id,
                "decision_receipt_id": decision_receipt.receipt_id,
                "finding_id": item.finding_id,
                "decision": item.decision,
            }
            digest = hashlib.sha256(
                canonical_json_hash(seed).encode("utf-8")
            ).hexdigest()
            candidates.append(
                FeedbackCandidate.model_validate(
                    {
                        "candidate_id": f"feedback-candidate-{digest[:32]}",
                        "source_kind": "reader_review_decision",
                        "source_decision": item.decision,
                        "finding_id": item.finding_id,
                        "finding_kind": finding.kind,
                        "summary": finding.summary,
                        "evidence_quote": finding.evidence_quote,
                        "relative_path": finding.relative_path,
                        "status": "candidate",
                        "promotion_status": "not_promoted",
                        "confidence": "author_explicit",
                        "regression_result": "not_run",
                        "experience_relative_path": None,
                        "formal_knowledge_contaminated": False,
                        "formal_draft_allowed": False,
                    }
                )
            )
        if not candidates:
            raise ChangeSetStateError(
                "finalized Reader decision produced no feedback candidates"
            )
        material = {
            "schema": "zhimeng.feedback-candidate-bundle.v1",
            "change_set_id": record.change_set_id,
            "project_id": record.project_id,
            "reader_review_decision_receipt_id": decision_receipt.receipt_id,
            "reader_review_decision_receipt_hash": decision_receipt.receipt_hash,
            "follow_up_change_set_id": decision_receipt.follow_up_change_set_id,
            "candidates": [item.model_dump(mode="json") for item in candidates],
            "formal_knowledge_contaminated": False,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "novel_state_synced": False,
        }
        digest = hashlib.sha256(
            canonical_json_hash(material).encode("utf-8")
        ).hexdigest()
        bundle_id = f"feedback-candidate-bundle-{digest[:32]}"
        created_at = _now()
        created_at_text = created_at.isoformat().replace("+00:00", "Z")
        receipt_hash = canonical_json_hash(
            {
                **material,
                "bundle_id": bundle_id,
                "created_at": created_at_text,
            }
        )
        bundle = FeedbackCandidateBundle.model_validate(
            {
                **material,
                "bundle_id": bundle_id,
                "created_at": created_at,
                "receipt_hash": receipt_hash,
            }
        )
        payload = bundle.model_dump(mode="json", by_alias=True)
        _atomic_write_json(artifact_root / "feedback-candidate-bundle.json", payload)
        history_path = artifact_root / "feedback-candidate-history.json"
        history = self._read_feedback_candidate_history(history_path)
        history = [item for item in history if item.bundle_id != bundle.bundle_id]
        history.append(bundle)
        history = history[-32:]
        _atomic_write_json(
            history_path,
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )
        return bundle

    def _read_feedback_candidate_bundle(
        self,
        path: Path,
    ) -> FeedbackCandidateBundle | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FeedbackCandidateBundle.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_feedback_candidate_history(
        self,
        path: Path,
    ) -> list[FeedbackCandidateBundle]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[FeedbackCandidateBundle] = []
        for item in payload:
            try:
                history.append(FeedbackCandidateBundle.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _create_reader_follow_up_change_set(
        self,
        parent: ChangeSetResponse,
        reader: ReaderReviewReceipt,
        decisions: list[ReaderFindingDecision],
    ) -> ChangeSetResponse:
        binding = self._require_binding(parent.project_id)
        root, snapshot, current = self._snapshot(binding)
        findings_by_id = {item.finding_id: item for item in reader.findings}
        adopted_findings = [
            findings_by_id[item.finding_id]
            for item in decisions
            if item.decision == "adopted" and item.finding_id in findings_by_id
        ]
        notes_path = "08_\u7d20\u6750\u5e93/reader_revision_notes.md"
        lines = [
            "# Reader revision notes",
            "",
            f"- parent_change_set_id: `{parent.change_set_id}`",
            f"- reader_review_receipt_id: `{reader.receipt_id}`",
            f"- result_fingerprint: `{reader.result_fingerprint}`",
            f"- recorded_at: `{_now().isoformat().replace('+00:00', 'Z')}`",
            "",
            "## Adopted findings",
            "",
        ]
        for finding in adopted_findings:
            lines.append(
                f"- `{finding.finding_id}` ({finding.kind}/{finding.severity}): {finding.summary}"
            )
            if finding.evidence_quote:
                lines.append(f"  - evidence: {finding.evidence_quote[:240]}")
            if finding.relative_path:
                lines.append(f"  - path: `{finding.relative_path}`")
        lines.append("")
        notes_body = "\n".join(lines)
        if not notes_body.endswith("\n"):
            notes_body += "\n"

        proposed: list[CreateFileChangeInput | UpdateFileChangeInput] = []
        files_by_path = {
            item.relative_path.casefold(): item for item in snapshot.files
        }
        notes_key = notes_path.casefold()
        if notes_key in files_by_path:
            existing_notes = files_by_path[notes_key]
            before_bytes, before_text, _preserve = self._read_snapshot_file(
                root, existing_notes
            )
            merged = before_text
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += "\n" + notes_body
            if merged != before_text:
                proposed.append(
                    UpdateFileChangeInput(
                        operation=ChangeOperation.UPDATE,
                        file_id=existing_notes.file_id,
                        after_content=merged,
                        rationale="Append adopted Reader findings into revision notes.",
                    )
                )
        else:
            proposed.append(
                CreateFileChangeInput(
                    operation=ChangeOperation.CREATE,
                    relative_path=notes_path,
                    after_content=notes_body,
                    rationale="Capture adopted Reader findings as a follow-up revision note.",
                )
            )

        touched: set[str] = {notes_path.casefold()}
        for finding in adopted_findings:
            if finding.kind != "fact_risk" or not finding.relative_path:
                continue
            key = finding.relative_path.casefold()
            if key in touched or key not in files_by_path:
                continue
            file_record = files_by_path[key]
            _before_bytes, before_text, _preserve = self._read_snapshot_file(
                root, file_record
            )
            cleaned = before_text
            for token in ("TODO", "TBD", "todo", "tbd", "placeholder", "PLACEHOLDER", "xxx", "XXX"):
                cleaned = cleaned.replace(token, "")
            if cleaned == before_text:
                continue
            proposed.append(
                UpdateFileChangeInput(
                    operation=ChangeOperation.UPDATE,
                    file_id=file_record.file_id,
                    after_content=cleaned,
                    rationale=(
                        "Apply adopted Reader fact_risk cleanup by removing "
                        "placeholder-like tokens."
                    ),
                )
            )
            touched.add(key)

        if not proposed:
            stamp = _now().isoformat().replace("+00:00", "Z")
            forced = notes_body + f"- decision_stamp: `{stamp}`\n"
            proposed.append(
                CreateFileChangeInput(
                    operation=ChangeOperation.CREATE,
                    relative_path=notes_path,
                    after_content=forced,
                    rationale="Record Reader decision follow-up even without prose edits.",
                )
            )

        request = ChangeSetCreateRequest(
            base_revision=current.revision,
            base_fingerprint=current.fingerprint,
            title=f"Reader follow-up for {parent.change_set_id}",
            summary=(
                "Author-adopted cold-read Reader findings produced this follow-up "
                "ChangeSet revision. Parent Reader opinions are no longer valid for "
                "the new candidate."
            ),
            origin=ChangeSetOrigin.MANUAL,
            changes=proposed,
        )
        return self.create(parent.project_id, request)

    def _read_reader_review_decision(
        self,
        path: Path,
    ) -> ReaderReviewDecisionReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ReaderReviewDecisionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_reader_review_decision_history(
        self,
        path: Path,
    ) -> list[ReaderReviewDecisionReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[ReaderReviewDecisionReceipt] = []
        for item in payload:
            try:
                history.append(ReaderReviewDecisionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _build_deterministic_reader_findings(
        self,
        record: ChangeSetResponse,
    ) -> list[ReaderFinding]:
        findings: list[ReaderFinding] = []
        for change in record.changes:
            if change.operation not in {ChangeOperation.CREATE, ChangeOperation.UPDATE}:
                continue
            accepted = [
                hunk
                for hunk in change.hunks
                if hunk.decision is HunkDecision.ACCEPTED
            ]
            if not accepted:
                continue
            after = "".join(hunk.after_text for hunk in accepted)
            before = "".join(hunk.before_text for hunk in accepted)
            path_value = change.relative_path
            digest_seed = f"{record.change_set_id}:{path_value}:{len(after)}"
            base = hashlib.sha256(digest_seed.encode("utf-8")).hexdigest()

            if not after.strip():
                findings.append(
                    ReaderFinding.model_validate(
                        {
                            "finding_id": f"reader-finding-{base[:16]}",
                            "kind": "continuity_risk",
                            "severity": "concern",
                            "summary": "Candidate after content is empty; cold-read continuity is broken.",
                            "evidence_quote": "",
                            "relative_path": path_value,
                            "overrides_gate": False,
                        }
                    )
                )
                continue

            quote = after.strip().replace("\r\n", "\n")
            if len(quote) > 240:
                quote = quote[:240]

            lowered = after.casefold()
            placeholder_tokens = ("todo", "tbd", "placeholder", "xxx")
            if any(token in lowered for token in placeholder_tokens):
                findings.append(
                    ReaderFinding.model_validate(
                        {
                            "finding_id": f"reader-finding-{base[:14]}01",
                            "kind": "fact_risk",
                            "severity": "concern",
                            "summary": (
                                "Candidate still contains placeholder-like tokens that "
                                "a cold reader would notice as unfinished."
                            ),
                            "evidence_quote": quote,
                            "relative_path": path_value,
                            "overrides_gate": False,
                        }
                    )
                )

            if before and len(after.strip()) < max(12, int(len(before.strip()) * 0.35)):
                findings.append(
                    ReaderFinding.model_validate(
                        {
                            "finding_id": f"reader-finding-{base[:14]}02",
                            "kind": "continuity_risk",
                            "severity": "watch",
                            "summary": (
                                "Candidate is much shorter than the prior published text; "
                                "chase continuity may feel abrupt."
                            ),
                            "evidence_quote": quote,
                            "relative_path": path_value,
                            "overrides_gate": False,
                        }
                    )
                )

            trailing = after.rstrip()
            sentence_closers = {".", "!", "?", "。", "！", "？", "…", '"', "'", "”", "’"}
            if trailing and trailing[-1] not in sentence_closers:
                findings.append(
                    ReaderFinding.model_validate(
                        {
                            "finding_id": f"reader-finding-{base[:14]}03",
                            "kind": "subjective_note",
                            "severity": "info",
                            "summary": (
                                "Ending does not land on a clear sentence close; "
                                "this may read as an intentional cliff or as unfinished."
                            ),
                            "evidence_quote": quote[-120:],
                            "relative_path": path_value,
                            "overrides_gate": False,
                        }
                    )
                )
            else:
                findings.append(
                    ReaderFinding.model_validate(
                        {
                            "finding_id": f"reader-finding-{base[:14]}04",
                            "kind": "subjective_note",
                            "severity": "info",
                            "summary": (
                                "Cold-read pass recorded a complete sentence close on the "
                                "candidate; no automatic literary score is claimed."
                            ),
                            "evidence_quote": quote[-120:],
                            "relative_path": path_value,
                            "overrides_gate": False,
                        }
                    )
                )
        return findings[:64]

    def _build_hard_fact_signals(
        self,
        record: ChangeSetResponse,
        artifact_root: Path,
    ) -> tuple[list[HardFactSignal], str, str | None]:
        signals: list[HardFactSignal] = []
        post_draft_status = "missing"
        literary_receipt_id: str | None = None

        # Candidate hard-fact token scan.
        hard_fact_tokens = (
            "TODO",
            "TBD",
            "PLACEHOLDER",
            "??? ",
            "【待定】",
            "[待定]",
            "待补事实",
        )
        scan_changes = [
            change
            for change in record.changes
            if change.operation in {ChangeOperation.CREATE, ChangeOperation.UPDATE}
        ]
        total_chars = 0
        for change in scan_changes:
            for hunk in change.hunks:
                body = hunk.after_text or ""
                total_chars += len(body)
                for token in hard_fact_tokens:
                    if token in body:
                        signals.append(
                            HardFactSignal(
                                code=(
                                    HardFactSignalCode.PLACEHOLDER_TOKEN
                                    if token in {"TODO", "TBD", "PLACEHOLDER"}
                                    else HardFactSignalCode.UNRESOLVED_FACT_MARKER
                                ),
                                severity="block",
                                summary=f"hard-fact risk token observed: {token}",
                                evidence_quote=token,
                                relative_path=change.relative_path,
                                source="candidate_scan",
                            )
                        )
        if not scan_changes or total_chars == 0:
            signals.append(
                HardFactSignal(
                    code=HardFactSignalCode.CANDIDATE_MISSING,
                    severity="block",
                    summary="no candidate prose content available for hard-fact Gate",
                    evidence_quote="",
                    relative_path=None,
                    source="runtime",
                )
            )

        # Post-draft Gate evidence when present.
        try:
            payload = self.store.get_latest_post_draft_gate_receipt(record.change_set_id)
        except (TypeError, ValueError, RuntimeError):
            payload = None
        if payload is not None:
            try:
                gate = self._validate_stored_gate_receipt(payload)
                if (
                    gate.review_revision == record.review_revision
                    and gate.base_revision == record.base_revision
                    and gate.base_fingerprint == record.base_fingerprint
                ):
                    if gate.status == "passed":
                        post_draft_status = "passed"
                    elif gate.status in {"blocked", "failed"}:
                        post_draft_status = "blocked"
                        signals.append(
                            HardFactSignal(
                                code=HardFactSignalCode.POST_DRAFT_BLOCKED,
                                severity="block",
                                summary="post-draft Gate blocked the candidate",
                                evidence_quote=gate.error_message or gate.status,
                                relative_path=None,
                                source="post_draft_gate",
                            )
                        )
                    else:
                        post_draft_status = "stale"
                else:
                    post_draft_status = "stale"
            except Exception:
                post_draft_status = "stale"

        # Literary quality hard-fact signals when present.
        literary = self._read_literary_quality_gate(
            artifact_root / "literary-quality-gate-receipt.json"
        )
        if literary is not None:
            literary_receipt_id = literary.receipt_id
            hard_hits = int(literary.finding_counts.get("hard_fact_signal", 0) or 0)
            if hard_hits > 0:
                signals.append(
                    HardFactSignal(
                        code=HardFactSignalCode.LITERARY_HARD_FACT_SIGNAL,
                        severity="block",
                        summary=(
                            f"literary quality Gate reported {hard_hits} hard-fact signal(s)"
                        ),
                        evidence_quote=literary.receipt_id,
                        relative_path=None,
                        source="literary_quality_gate",
                    )
                )

        # Deduplicate by code+summary+path.
        deduped: list[HardFactSignal] = []
        seen: set[str] = set()
        for item in signals:
            key = f"{item.code.value}|{item.summary}|{item.relative_path or ''}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped[:64], post_draft_status, literary_receipt_id

    def _read_hard_fact_gate(self, path: Path) -> HardFactGateReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return HardFactGateReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_hard_fact_gate_history(self, path: Path) -> list[HardFactGateReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[HardFactGateReceipt] = []
        for item in payload:
            try:
                history.append(HardFactGateReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history[-32:]

    def _build_literary_quality_assessment(
        self,
        record: ChangeSetResponse,
    ) -> tuple[
        list[LiteraryQualityFinding],
        list[LiteraryQualityDimension],
        list[LiteraryRubricScore],
        int,
    ]:
        findings: list[LiteraryQualityFinding] = []
        candidate_paths: list[str] = []
        placeholder_hits = 0
        ai_smell_hits = 0
        hard_fact_hits = 0
        total_chars = 0
        combined_parts: list[str] = []
        placeholder_tokens = (
            "TODO",
            "TBD",
            "PLACEHOLDER",
            "xxx",
            "XXX",
            "待补",
            "占位",
        )
        ai_smell_tokens = (
            "综上所述",
            "值得注意的是",
            "不可否认",
            "在这个充满",
            "心中不禁",
            "一丝不易察觉",
        )
        hard_fact_tokens = (
            "TODO",
            "TBD",
            "PLACEHOLDER",
            "??? ",
            "【待定】",
            "[待定]",
        )
        sensory_tokens = (
            "光",
            "影",
            "气味",
            "味道",
            "声音",
            "触",
            "凉",
            "热",
            "风",
            "雨",
            "血",
            "汗",
            "痛",
            "震",
            "响",
            "亮",
            "暗",
            "冷",
            "暖",
            "湿",
        )
        hook_tokens = (
            "突然",
            "不对",
            "危险",
            "杀",
            "逃",
            "死",
            "血",
            "真相",
            "秘密",
            "为什么",
            "谁",
            "惊",
            "怒",
            "怕",
            "!",
            "！",
            "?",
            "？",
        )

        prose_changes = [
            change
            for change in record.changes
            if change.operation in {ChangeOperation.CREATE, ChangeOperation.UPDATE}
            and change.post_draft_gate_scope == "prose_body"
        ]
        scan_changes = prose_changes or [
            change
            for change in record.changes
            if change.operation in {ChangeOperation.CREATE, ChangeOperation.UPDATE}
        ]
        for change in scan_changes:
            candidate_paths.append(change.relative_path)
            for hunk in change.hunks:
                text_body = hunk.after_text or ""
                total_chars += len(text_body)
                if text_body.strip():
                    combined_parts.append(text_body)
                lower = text_body.casefold()
                for token in placeholder_tokens:
                    if token.casefold() in lower or token in text_body:
                        placeholder_hits += text_body.count(token) + lower.count(
                            token.casefold()
                        )
                        findings.append(
                            LiteraryQualityFinding(
                                finding_id="lq-finding-"
                                + hashlib.sha256(
                                    f"{change.change_id}:{hunk.hunk_id}:ph:{token}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest()[:12],
                                kind="structural",
                                severity="concern",
                                summary=f"candidate still carries placeholder-like token: {token}",
                                evidence_quote=token,
                                relative_path=change.relative_path,
                            )
                        )
                for token in ai_smell_tokens:
                    if token in text_body:
                        ai_smell_hits += text_body.count(token)
                        findings.append(
                            LiteraryQualityFinding(
                                finding_id="lq-finding-"
                                + hashlib.sha256(
                                    f"{change.change_id}:{hunk.hunk_id}:ai:{token}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest()[:12],
                                kind="subjective_note",
                                severity="watch",
                                summary=f"structural AI-smell marker observed: {token}",
                                evidence_quote=token,
                                relative_path=change.relative_path,
                            )
                        )
                for token in hard_fact_tokens:
                    if token in text_body:
                        hard_fact_hits += text_body.count(token)
                        findings.append(
                            LiteraryQualityFinding(
                                finding_id="lq-finding-"
                                + hashlib.sha256(
                                    f"{change.change_id}:{hunk.hunk_id}:hf:{token}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest()[:12],
                                kind="hard_fact_signal",
                                severity="concern",
                                summary=f"hard-fact risk signal observed: {token}",
                                evidence_quote=token,
                                relative_path=change.relative_path,
                            )
                        )

        # Deduplicate findings by id while preserving order.
        deduped: list[LiteraryQualityFinding] = []
        seen: set[str] = set()
        for item in findings:
            if item.finding_id in seen:
                continue
            seen.add(item.finding_id)
            deduped.append(item)
        findings = deduped[:64]

        combined = "\n".join(combined_parts)
        lines = [line for line in combined.splitlines() if line.strip()]
        paragraphs = [p for p in combined.replace("\r\n", "\n").split("\n\n") if p.strip()]
        if not paragraphs and combined.strip():
            paragraphs = [combined.strip()]

        # Dialogue balance: ratio of dialogue-bearing lines.
        dialogue_markers = ('"', "“", "”", "「", "」", "『", "』", "‘", "’")
        dialogue_lines = sum(
            1 for line in lines if any(marker in line for marker in dialogue_markers)
        )
        if lines:
            dialogue_ratio = dialogue_lines / len(lines)
            # Prefer 15%-55% dialogue-bearing lines.
            if 0.15 <= dialogue_ratio <= 0.55:
                dialogue_score = 90
            elif 0.08 <= dialogue_ratio < 0.15 or 0.55 < dialogue_ratio <= 0.7:
                dialogue_score = 70
            elif dialogue_ratio == 0:
                dialogue_score = 35
            else:
                dialogue_score = 55
        else:
            dialogue_ratio = 0.0
            dialogue_score = 0

        # Sensory density per 100 chars.
        sensory_hits = sum(combined.count(token) for token in sensory_tokens)
        sensory_per_100 = (sensory_hits * 100 / total_chars) if total_chars else 0.0
        if total_chars == 0:
            sensory_score = 0
        elif sensory_per_100 >= 1.2:
            sensory_score = 88
        elif sensory_per_100 >= 0.5:
            sensory_score = 72
        elif sensory_per_100 > 0:
            sensory_score = 55
        else:
            sensory_score = 30

        # Pacing variation via paragraph length coefficient of variation.
        if len(paragraphs) >= 2:
            lengths = [len(p) for p in paragraphs]
            mean_len = sum(lengths) / len(lengths)
            variance = sum((item - mean_len) ** 2 for item in lengths) / len(lengths)
            std = variance ** 0.5
            cv = (std / mean_len) if mean_len else 0.0
            if 0.25 <= cv <= 1.2:
                pacing_score = 86
            elif 0.1 <= cv < 0.25 or 1.2 < cv <= 1.8:
                pacing_score = 68
            else:
                pacing_score = 48
        elif paragraphs:
            pacing_score = 50
            cv = 0.0
        else:
            pacing_score = 0
            cv = 0.0

        # Hook opening: tension markers in first 120 chars.
        opening = combined[:120]
        hook_hits = sum(1 for token in hook_tokens if token in opening)
        if total_chars == 0:
            hook_score = 0
        elif hook_hits >= 2:
            hook_score = 90
        elif hook_hits == 1:
            hook_score = 70
        else:
            hook_score = 40

        # Repetition risk: repeated 4-grams.
        tokens = [ch for ch in combined if not ch.isspace()]
        grams: dict[str, int] = {}
        for index in range(0, max(0, len(tokens) - 3)):
            gram = "".join(tokens[index : index + 4])
            grams[gram] = grams.get(gram, 0) + 1
        repeated = sum(1 for count in grams.values() if count >= 4)
        if total_chars == 0:
            repetition_score = 0
        elif repeated == 0:
            repetition_score = 92
        elif repeated <= 3:
            repetition_score = 70
        elif repeated <= 8:
            repetition_score = 50
        else:
            repetition_score = 30
            findings.append(
                LiteraryQualityFinding(
                    finding_id="lq-finding-"
                    + hashlib.sha256(
                        f"{record.change_set_id}:repetition:{repeated}".encode("utf-8")
                    ).hexdigest()[:12],
                    kind="rubric_note",
                    severity="watch",
                    summary=f"high repeated 4-gram clusters observed: {repeated}",
                    evidence_quote="repetition_risk",
                    relative_path=candidate_paths[0] if candidate_paths else None,
                )
            )

        def _axis(
            code: str,
            score: int,
            weight: int,
            detail: str,
        ) -> LiteraryRubricScore:
            return LiteraryRubricScore(
                code=code,  # type: ignore[arg-type]
                score=score,
                weight=weight,
                weighted_score=score * weight,
                detail=detail,
            )

        rubric_scores = [
            _axis(
                "dialogue_balance",
                dialogue_score,
                20,
                f"dialogue_line_ratio={dialogue_ratio:.2f} ({dialogue_lines}/{len(lines) if lines else 0})",
            ),
            _axis(
                "sensory_density",
                sensory_score,
                20,
                f"sensory_hits={sensory_hits}; per_100_chars={sensory_per_100:.2f}",
            ),
            _axis(
                "pacing_variation",
                pacing_score,
                20,
                f"paragraphs={len(paragraphs)}; length_cv={cv:.2f}",
            ),
            _axis(
                "hook_opening",
                hook_score,
                20,
                f"opening_hook_hits={hook_hits}",
            ),
            _axis(
                "repetition_risk",
                repetition_score,
                20,
                f"repeated_4gram_clusters={repeated}",
            ),
        ]
        total_weight = sum(item.weight for item in rubric_scores)
        rubric_total = (
            round(sum(item.weighted_score for item in rubric_scores) / total_weight)
            if total_weight
            else 0
        )
        if rubric_total >= 75:
            rubric_status = LiteraryQualityDimensionStatus.PASS
            rubric_detail = f"deterministic rubric total={rubric_total} (advisory)"
        elif rubric_total >= 55:
            rubric_status = LiteraryQualityDimensionStatus.WATCH
            rubric_detail = f"deterministic rubric total={rubric_total} needs author review"
        else:
            rubric_status = LiteraryQualityDimensionStatus.FAIL
            rubric_detail = f"deterministic rubric total={rubric_total} structurally weak"

        def _status_from_score(score: int) -> LiteraryQualityDimensionStatus:
            if score >= 75:
                return LiteraryQualityDimensionStatus.PASS
            if score >= 55:
                return LiteraryQualityDimensionStatus.WATCH
            return LiteraryQualityDimensionStatus.FAIL

        dimensions = [
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.CANDIDATE_PRESENT,
                status=(
                    LiteraryQualityDimensionStatus.PASS
                    if candidate_paths and total_chars > 0
                    else LiteraryQualityDimensionStatus.FAIL
                ),
                detail=(
                    f"scanned {len(candidate_paths)} candidate file(s), {total_chars} chars"
                    if candidate_paths
                    else "no candidate prose content available for structural scan"
                ),
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.PLACEHOLDER_SCAN,
                status=(
                    LiteraryQualityDimensionStatus.FAIL
                    if placeholder_hits
                    else LiteraryQualityDimensionStatus.PASS
                ),
                detail=(
                    f"placeholder-like hits={placeholder_hits}"
                    if placeholder_hits
                    else "no placeholder-like tokens detected"
                ),
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.AI_SMELL_MARKERS,
                status=(
                    LiteraryQualityDimensionStatus.WATCH
                    if ai_smell_hits
                    else LiteraryQualityDimensionStatus.PASS
                ),
                detail=(
                    f"ai-smell marker hits={ai_smell_hits}"
                    if ai_smell_hits
                    else "no fixed AI-smell markers detected"
                ),
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.HARD_FACT_SIGNAL,
                status=(
                    LiteraryQualityDimensionStatus.FAIL
                    if hard_fact_hits
                    else LiteraryQualityDimensionStatus.PASS
                ),
                detail=(
                    f"hard-fact risk hits={hard_fact_hits}"
                    if hard_fact_hits
                    else "no hard-fact risk signals detected"
                ),
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.DIALOGUE_BALANCE,
                status=_status_from_score(dialogue_score),
                detail=f"score={dialogue_score}; ratio={dialogue_ratio:.2f}",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.SENSORY_DENSITY,
                status=_status_from_score(sensory_score),
                detail=f"score={sensory_score}; per_100={sensory_per_100:.2f}",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.PACING_VARIATION,
                status=_status_from_score(pacing_score),
                detail=f"score={pacing_score}; cv={cv:.2f}",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.HOOK_OPENING,
                status=_status_from_score(hook_score),
                detail=f"score={hook_score}; opening_hits={hook_hits}",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.REPETITION_RISK,
                status=_status_from_score(repetition_score),
                detail=f"score={repetition_score}; clusters={repeated}",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.DETERMINISTIC_RUBRIC,
                status=rubric_status,
                detail=rubric_detail,
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.SUBJECTIVE_LITERARY_SCORE,
                status=LiteraryQualityDimensionStatus.NOT_SCORED,
                detail="Local v1 does not claim a subjective/LLM literary quality score",
            ),
            LiteraryQualityDimension(
                code=LiteraryQualityDimensionCode.AUTHORIZATION_UNLOCK,
                status=LiteraryQualityDimensionStatus.FAIL,
                detail="literary quality Gate is advisory only and never unlocks formal draft",
            ),
        ]
        return findings, dimensions, rubric_scores, rubric_total

    def _read_literary_quality_gate(
        self,
        path: Path,
    ) -> LiteraryQualityGateReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return LiteraryQualityGateReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_literary_quality_gate_history(
        self,
        path: Path,
    ) -> list[LiteraryQualityGateReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[LiteraryQualityGateReceipt] = []
        for item in payload:
            try:
                history.append(LiteraryQualityGateReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history[-32:]

    def _read_reader_review(self, path: Path) -> ReaderReviewReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return ReaderReviewReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_reader_review_history(self, path: Path) -> list[ReaderReviewReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[ReaderReviewReceipt] = []
        for item in payload:
            try:
                history.append(ReaderReviewReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _chapter_id_from_paths(self, paths: list[str]) -> str:
        for path in paths:
            match = re.search(r"CH0*(\d+)", path, flags=re.IGNORECASE)
            if match is not None:
                return f"CH{int(match.group(1)):03d}"
        return "CH001"

    def _canonical_chapter_key(self, chapter_id: str) -> str:
        match = re.search(r"(\d+)", chapter_id)
        if match is None:
            return f"chapter:{chapter_id.casefold()}"
        return f"chapter:{int(match.group(1))}"

    def _read_original_nlcl_call_usage_export(
        self,
        path: Path,
    ) -> NlclOriginalCallUsageExportReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclOriginalCallUsageExportReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_original_nlcl_call_usage_export_history(
        self,
        path: Path,
    ) -> list[NlclOriginalCallUsageExportReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclOriginalCallUsageExportReceipt] = []
        for item in payload:
            try:
                history.append(NlclOriginalCallUsageExportReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _read_nlcl_call_usage_continuity(
        self,
        path: Path,
    ) -> NlclCallUsageContinuityReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclCallUsageContinuityReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_nlcl_call_usage_continuity_history(
        self,
        path: Path,
    ) -> list[NlclCallUsageContinuityReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclCallUsageContinuityReceipt] = []
        for item in payload:
            try:
                history.append(NlclCallUsageContinuityReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _read_nlcl_writeback_sync(self, path: Path) -> NlclWritebackSyncReceipt | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return NlclWritebackSyncReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_nlcl_writeback_sync_history(
        self,
        path: Path,
    ) -> list[NlclWritebackSyncReceipt]:
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[NlclWritebackSyncReceipt] = []
        for item in payload:
            try:
                history.append(NlclWritebackSyncReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _writeback_manifest(
        self,
        record: ChangeSetResponse,
        prepared: list[_PreparedWrite],
        snapshot: ProjectInspectionResponse,
        binding: ProjectBindingRecord,
        *,
        outcome: str,
        base_revision: int | None = None,
        base_fingerprint: str | None = None,
        created_directories: list[Path] | list[str] | None = None,
    ) -> WritebackManifest:
        files_by_path = {item.relative_path.casefold(): item for item in snapshot.files}
        results: list[WritebackFileResult] = []
        for item in prepared:
            result_file = files_by_path.get(item.change.relative_path.casefold())
            actual_after_hash = (
                None
                if item.after_bytes is None
                else _sha256(item.after_bytes)
            )
            operation = item.change.operation
            if outcome == "rolled_back":
                operation = {
                    ChangeOperation.CREATE: ChangeOperation.DELETE,
                    ChangeOperation.UPDATE: ChangeOperation.UPDATE,
                    ChangeOperation.DELETE: ChangeOperation.CREATE,
                }[operation]
            before_hash = (
                _sha256(item.before_bytes)
                if item.before_bytes is not None
                else None
            )
            backup_ref = (
                item.backup.relative_to(self._artifact_root(record.change_set_id))
                .as_posix()
                if item.backup is not None
                else None
            )
            if outcome == "rolled_back":
                if operation is ChangeOperation.CREATE:
                    backup_ref = None
                elif operation is ChangeOperation.DELETE:
                    backup_ref = backup_ref or "rollback-current/unavailable"
            results.append(
                WritebackFileResult(
                    change_id=item.change.change_id,
                    operation=operation,
                    relative_path=item.change.relative_path,
                    before_sha256=before_hash,
                    after_sha256=actual_after_hash,
                    result_file_id=(
                        result_file.file_id if result_file is not None else None
                    ),
                    accepted_hunk_ids=item.accepted_hunks,
                    rejected_hunk_ids=item.rejected_hunks,
                    backup_ref=backup_ref,
                )
            )
        return WritebackManifest(
            change_set_id=record.change_set_id,
            project_id=record.project_id,
            base_revision=base_revision or record.base_revision,
            base_fingerprint=base_fingerprint or record.base_fingerprint,
            result_revision=binding.revision,
            result_fingerprint=binding.fingerprint,
            outcome=outcome,
            files=results,
            created_directories=[
                (
                    directory.relative_to(Path(self._require_binding(record.project_id).root_path))
                    .as_posix()
                    if isinstance(directory, Path)
                    else directory
                )
                for directory in (created_directories or [])
            ],
            created_at=_now(),
        )

    @staticmethod
    def _verify_prepared_result(
        root: Path,
        prepared: list[_PreparedWrite],
        snapshot: ProjectInspectionResponse,
    ) -> None:
        files_by_path = {item.relative_path.casefold(): item for item in snapshot.files}
        for item in prepared:
            result_file = files_by_path.get(item.change.relative_path.casefold())
            actual_bytes = ChangeSetService._read_recovery_file(item.target, root)
            if item.after_bytes is None:
                if actual_bytes is not None or result_file is not None:
                    raise ChangeSetConflictError(
                        f"writeback target is not deleted as expected: {item.change.relative_path}"
                    )
                continue
            expected_hash = _sha256(item.after_bytes)
            actual_hash = None if actual_bytes is None else _sha256(actual_bytes)
            if actual_hash != expected_hash or result_file is None or result_file.sha256 != expected_hash:
                raise ChangeSetConflictError(
                    f"writeback target changed before commit: {item.change.relative_path}"
                )

    def _artifact_root(self, change_set_id: str) -> Path:
        root = (self.state_root / change_set_id).resolve(strict=False)
        if not _is_within(root, self.state_root):
            raise ChangeSetWritebackError("change set artifact root escaped runtime state")
        return root

    def _write_journal(
        self,
        record: ChangeSetResponse,
        artifact_root: Path,
        prepared: list[_PreparedWrite],
        *,
        transaction: str,
        status: str,
        created_directories: list[Path] | None = None,
    ) -> None:
        _atomic_write_json(
            artifact_root / "writeback-journal.json",
            {
                "schema": "zhimeng.writeback-journal.v1",
                "change_set_id": record.change_set_id,
                "project_id": record.project_id,
                "base_revision": record.base_revision,
                "base_fingerprint": record.base_fingerprint,
                "transaction": transaction,
                "status": status,
                "created_directories": [
                    directory.relative_to(
                        Path(self._require_binding(record.project_id).root_path)
                    ).as_posix()
                    for directory in (created_directories or [])
                ],
                "files": [
                    {
                        "change_id": item.change.change_id,
                        "operation": item.change.operation.value,
                        "relative_path": item.change.relative_path,
                        "before_sha256": (
                            _sha256(item.before_bytes)
                            if item.before_bytes is not None
                            else None
                        ),
                        "after_sha256": (
                            _sha256(item.after_bytes)
                            if item.after_bytes is not None
                            else None
                        ),
                        "temporary_path": (
                            item.temporary.relative_to(item.root).as_posix()
                            if item.temporary is not None
                            else None
                        ),
                        "tombstone_path": (
                            item.tombstone.relative_to(item.root).as_posix()
                            if item.tombstone is not None
                            else None
                        ),
                        "applied": item.applied,
                    }
                    for item in prepared
                ],
                "updated_at": _now().isoformat(),
            },
        )

    @staticmethod
    def _payload(record: ChangeSetResponse) -> dict[str, object]:
        return change_set_persisted_payload(record)
