from __future__ import annotations

import asyncio
import base64
import difflib
import hashlib
import json
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
    ChangeSetDiscardRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetHunkEditRequest,
    ChangeSetListResponse,
    ChangeSetOrigin,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
    ChangeSetStatus,
    ChangeSetSummary,
    HunkDecision,
    HunkDecisionUpdate,
    WritebackEvidenceReceipt,
    WritebackEvidenceResponse,
    WritebackFileResult,
    WritebackManifest,
    change_set_persisted_payload,
)
from zhimeng_runtime.database import (
    ProjectBindingRecord,
    SqliteStore,
    latest_own_write_pin,
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
    PostDraftGateResult,
    canonical_json_hash,
    post_draft_gate_receipt_hash,
)
from zhimeng_runtime.fs_atomic import replace_with_retry
from zhimeng_runtime.text_encoding import decode_project_text

if TYPE_CHECKING:
    from zhimeng_runtime.skill_executor import SkillExecutor, SkillExecutionOutcome


UTF8_BOM = b"\xef\xbb\xbf"
REPARSE_POINT_ATTRIBUTE = 0x400
# Legacy tool classification imports this; text editing itself uses byte decoding.
TEXT_EXTENSIONS = frozenset({".json", ".jsonl", ".md", ".markdown", ".txt", ".yaml", ".yml", ".csv", ".toml"})
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


class ChangeSetReviewRequiredError(ChangeSetError):
    """The requested write is intentionally left for explicit author review."""


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
    # 移动/改名（move）的源路径：apply 时把它原子改名成 target，撤回挪回。
    source: Path | None = None


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


def _encode_text(content: str, *, preserve_utf8_bom: bool, original_bytes: bytes | None = None) -> bytes:
    if original_bytes is not None:
        _, encoding, bom = decode_project_text(original_bytes)
    else:
        encoding, bom = "utf-8", UTF8_BOM if preserve_utf8_bom else b""
    encoded = bom + content.encode(encoding)
    if len(encoded) > MAX_TEXT_FILE_BYTES:
        raise ChangeSetInputError(
            f"project text exceeds the {MAX_TEXT_FILE_BYTES} byte limit"
        )
    return encoded


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
        replace_with_retry(temporary, path)
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
    ) -> None:
        self.reader = reader
        self.store = store
        self.state_root = state_root.resolve(strict=False)
        self.post_draft_gate_executor = post_draft_gate_executor
        self.knowledge_fingerprint_probe = knowledge_fingerprint_probe
        self.knowledge_validator_resolver = knowledge_validator_resolver

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
                    base_revision=record.base_revision,
                    base_fingerprint=record.base_fingerprint,
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
                elif change.operation is ChangeOperation.MOVE:
                    # 移动/改名字节不变，指纹和落盘前一致。
                    expected_after = expected_before
                else:
                    backup = artifact_root / "backups" / Path(change.relative_path)
                    base_bytes = self._read_verified_backup(
                        backup,
                        expected_before,
                        artifact_root,
                    )
                    try:
                        base_text = decode_project_text(base_bytes)[0]
                    except ValueError as exc:
                        raise ChangeSetWritebackError(
                            f"recovery backup is not readable text: {change.relative_path}"
                        ) from exc
                    expected_after = _sha256(
                        _encode_text(
                            self._apply_hunk_decisions(base_text, change),
                            preserve_utf8_bom=change.preserve_utf8_bom,
                            original_bytes=base_bytes,
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

            if change.operation is ChangeOperation.MOVE:
                # 移动/改名只有一个字节版本（before == after），但位置有两个。
                # apply 恢复回「源在、目标空」；rollback 恢复回 applied 态
                # 「目标在、源空」。只认这两种完整状态，其余视为中断后被改过。
                move_target_raw = raw.get("move_target_path")
                if not isinstance(move_target_raw, str):
                    raise ChangeSetWritebackError(
                        "writeback journal has an invalid move_target_path"
                    )
                source_path = self._recovery_project_path(root, relative_path)
                if transaction == "apply":
                    anchor_path = source_path
                    other_path = self._recovery_project_path(root, move_target_raw)
                else:
                    result = writeback_results.get(change_id)
                    if result is None or result.target_relative_path is None:
                        raise ChangeSetWritebackError(
                            "rollback journal references a move result without a target path"
                        )
                    anchor_path = self._recovery_project_path(
                        root, result.target_relative_path
                    )
                    other_path = source_path
                if before_sha256 is None:
                    raise ChangeSetWritebackError(
                        "writeback journal move row has no before hash"
                    )
                desired_bytes = self._read_verified_backup(
                    (
                        artifact_root / "backups"
                        if transaction == "apply"
                        else artifact_root / "rollback-current"
                    )
                    / Path(relative_path),
                    before_sha256,
                    artifact_root,
                )
                anchor_bytes = self._read_recovery_file(anchor_path, root)
                other_bytes = self._read_recovery_file(other_path, root)
                anchor_sha = None if anchor_bytes is None else _sha256(anchor_bytes)
                other_sha = None if other_bytes is None else _sha256(other_bytes)
                if not (
                    (anchor_sha == before_sha256 and other_bytes is None)
                    or (anchor_bytes is None and other_sha == before_sha256)
                ):
                    raise ChangeSetConflictError(
                        f"project file changed after interruption: {relative_path}"
                    )
                writes.append(
                    _RecoveryWrite(
                        relative_path=relative_path,
                        target=anchor_path,
                        desired_bytes=desired_bytes,
                        observed_sha256=anchor_sha,
                        cleanup_paths=((other_path, before_sha256),),
                    )
                )
                continue

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
        replace_with_retry(temporary, item.target)


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
            target_relative_path: str | None = None
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
                        _encode_text(after_text, preserve_utf8_bom=preserve_bom, original_bytes=before_bytes)
                    )
                    if proposed_after_hash == before_hash:
                        raise ChangeSetInputError(
                            f"update does not change file content: {relative_path}"
                        )
                elif item.operation is ChangeOperation.MOVE:
                    target_relative_path = self._normalize_new_path(
                        item.target_relative_path or ""
                    )
                    if target_relative_path.casefold() == target_key:
                        raise ChangeSetInputError(
                            "move target must differ from the current path: "
                            f"{relative_path}"
                        )
                    if target_relative_path.casefold() in paths_casefold or os.path.lexists(
                        root / Path(target_relative_path)
                    ):
                        raise ChangeSetInputError(
                            f"move target already exists: {target_relative_path}"
                        )
                    # 移动/改名只换位置，字节原样跟着走。
                    after_text = before_text
                    proposed_after_hash = before_hash
                else:
                    after_text = ""
                    proposed_after_hash = None

            if target_key in targeted:
                raise ChangeSetInputError(
                    f"a change set cannot target one path more than once: {relative_path}"
                )
            targeted.add(target_key)
            if target_relative_path is not None:
                if target_relative_path.casefold() in targeted:
                    raise ChangeSetInputError(
                        "a change set cannot target one path more than once: "
                        f"{target_relative_path}"
                    )
                targeted.add(target_relative_path.casefold())

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
                    target_relative_path=target_relative_path,
                    rationale=item.rationale,
                    before_sha256=before_hash,
                    proposed_after_sha256=proposed_after_hash,
                    preserve_utf8_bom=preserve_bom,
                    post_draft_gate_scope=(
                        "prose_body"
                        if item.operation in {ChangeOperation.CREATE, ChangeOperation.UPDATE}
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
            # 写前门禁只按请求本身来。以前 Agent 来源一律强制过门禁：每次写正文
            # 都整本书复制一份跑校验，还会拦「系统：」「无法完成」这类正常句子
            # （2026-09-22 审阅 A1 真机回执）。作者要的是普通 Agent，不是审批链。
            gate_required=bool(request.gate_required),
            ai_smell=request.ai_smell,
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
            own_pin = latest_own_write_pin(connection, run_id)
        if row is None:
            raise ChangeSetInputError("agent Run or ToolCall provenance was not found")
        if str(row["project_binding_id"]) != project_id:
            raise ChangeSetInputError("agent ToolCall belongs to another project")
        if row["context_pack_id"] is None:
            raise ChangeSetInputError("agent change proposal requires a bound Context Pack")
        base_matches = (
            int(row["project_revision"]) == request.base_revision
            and str(row["project_fingerprint"]) == request.base_fingerprint
        ) or (
            # 这场 Run 自己落盘推进过的钉子同样是合法基准：同一场对话里
            # 「先直接保存一份，再把后续改法提案给作者看」是合法工作流。
            own_pin is not None
            and own_pin == (request.base_revision, request.base_fingerprint)
        )
        if not base_matches:
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

    def edit_hunk(
        self,
        change_set_id: str,
        hunk_id: str,
        request: ChangeSetHunkEditRequest,
    ) -> ChangeSetResponse:
        record = self.get(change_set_id)
        if record.status not in {ChangeSetStatus.REVIEWING, ChangeSetStatus.READY}:
            raise ChangeSetStateError("当前修改已经不能再编辑")
        self._require_review_revision(record, request.expected_review_revision)

        matches = [
            (change_index, hunk_index)
            for change_index, change in enumerate(record.changes)
            for hunk_index, hunk in enumerate(change.hunks)
            if hunk.hunk_id == hunk_id
        ]
        if not matches:
            raise ChangeSetInputError("找不到这处修改，请刷新后再试")
        change_index, hunk_index = matches[0]
        target_change = record.changes[change_index]
        if target_change.operation is ChangeOperation.DELETE:
            raise ChangeSetInputError("整文件删除不能在这里改写")
        if target_change.operation is ChangeOperation.MOVE:
            raise ChangeSetInputError("移动/改名没有正文可以在这里改写")

        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)
        self._require_base(record.base_revision, record.base_fingerprint, current)
        files_by_id = {item.file_id: item for item in snapshot.files}

        before_bytes = None
        before_text = ""
        if target_change.operation is ChangeOperation.CREATE:
            if os.path.lexists(root / Path(target_change.relative_path)):
                raise ChangeSetConflictError(
                    f"create target now exists: {target_change.relative_path}"
                )
        else:
            file_record = files_by_id.get(target_change.file_id or "")
            if (
                file_record is None
                or file_record.relative_path != target_change.relative_path
            ):
                raise ChangeSetConflictError(
                    f"target file identity changed: {target_change.relative_path}"
                )
            before_bytes, before_text, preserve_bom = self._read_snapshot_file(
                root,
                file_record,
            )
            if preserve_bom != target_change.preserve_utf8_bom:
                raise ChangeSetConflictError(
                    f"target encoding changed: {target_change.relative_path}"
                )
            if _sha256(before_bytes) != target_change.before_sha256:
                raise ChangeSetConflictError(
                    f"target hash changed: {target_change.relative_path}"
                )

        target_hunk = target_change.hunks[hunk_index]
        author_text = self._preserve_newline_style(
            request.after_text,
            before_text or target_hunk.after_text,
        )
        self._validate_text(target_change.relative_path, author_text)
        if author_text == target_hunk.before_text:
            raise ChangeSetInputError("改后的内容和原文相同；不想采用时直接点‘不要’")

        line_delta = len(author_text.splitlines(keepends=True)) - len(
            target_hunk.after_text.splitlines(keepends=True)
        )
        next_hunks: list[ChangeHunk] = []
        for index, hunk in enumerate(target_change.hunks):
            if index == hunk_index:
                next_hunks.append(
                    hunk.model_copy(
                        update={
                            "after_text": author_text,
                            "after_end": hunk.after_start
                            + len(author_text.splitlines(keepends=True)),
                            "after_sha256": _text_sha256(author_text),
                            "decision": HunkDecision.PENDING,
                        }
                    )
                )
            elif index > hunk_index and line_delta:
                next_hunks.append(
                    hunk.model_copy(
                        update={
                            "after_start": hunk.after_start + line_delta,
                            "after_end": hunk.after_end + line_delta,
                        }
                    )
                )
            else:
                next_hunks.append(hunk)

        proposed_change = target_change.model_copy(update={"hunks": next_hunks})
        all_accepted = proposed_change.model_copy(
            update={
                "hunks": [
                    hunk.model_copy(update={"decision": HunkDecision.ACCEPTED})
                    for hunk in next_hunks
                ]
            }
        )
        proposed_text = self._apply_hunk_decisions(before_text, all_accepted)
        proposed_hash = _sha256(
            _encode_text(
                proposed_text,
                preserve_utf8_bom=target_change.preserve_utf8_bom,
                original_bytes=before_bytes,
            )
        )
        if proposed_hash == target_change.before_sha256:
            raise ChangeSetInputError("改后的内容和原文相同；不想采用时直接点‘不要’")

        changes = list(record.changes)
        changes[change_index] = proposed_change.model_copy(
            update={"proposed_after_sha256": proposed_hash}
        )
        updated = record.model_copy(
            update={
                "changes": changes,
                "status": ChangeSetStatus.REVIEWING,
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
                    original_bytes=before_bytes,
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

    def discard(
        self,
        change_set_id: str,
        request: ChangeSetDiscardRequest,
    ) -> ChangeSetResponse:
        """作者明确说「不要这份修改了」。一键作废，不需要逐条拒绝再走应用。

        还没逐条表态的小块统一落成 rejected——终态记录不允许残留 pending。
        """
        record = self.get(change_set_id)
        if record.status not in (ChangeSetStatus.REVIEWING, ChangeSetStatus.READY):
            raise ChangeSetStateError(
                f"only reviewing or ready change sets can be discarded; current state is {record.status.value}"
            )
        self._require_review_revision(record, request.expected_review_revision)
        # hunks/changes 合同是 list；这里必须用 list 并在末尾重新校验，
        # 否则 model_copy(update=) 会把 tuple 塞进 list 字段（跳过校验），
        # 序列化时刷 PydanticSerializationUnexpectedValue 警告。
        changes = [
            change.model_copy(
                update={
                    "hunks": [
                        hunk.model_copy(update={"decision": HunkDecision.REJECTED})
                        if hunk.decision is HunkDecision.PENDING
                        else hunk
                        for hunk in change.hunks
                    ],
                },
            )
            for change in record.changes
        ]
        discarded = ChangeSetResponse.model_validate(
            record.model_copy(
                update={
                    "status": ChangeSetStatus.DISCARDED,
                    "changes": changes,
                    "updated_at": _now(),
                }
            ).model_dump(mode="json", by_alias=True)
        )
        self.store.save_change_set(self._payload(discarded))
        return discarded

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

        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)
        if (
            current.revision != record.base_revision
            or current.fingerprint != record.base_fingerprint
        ):
            self._conflict(record, "project revision changed after the proposal was created")

        # Historical gate_required flags remain readable; saving no longer runs
        # writing checks. File conflicts and writeback integrity still apply.
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

    def apply_direct(
        self,
        change_set_id: str,
        request: ChangeSetApplyRequest,
    ) -> ChangeSetResponse:
        """Apply the candidate the author chose to save directly, preserving
        review history.

        This is the author-facing Agent/IDE path: accept a fresh candidate,
        run the existing prose gate when required, and reuse the normal atomic
        apply transaction. 作者选了「直接保存」就直接保存——删除、多文件、
        新建都一样落盘，撤回链兜底。已拒绝的块保持拒绝，只接受待定的块。
        """

        record = self.get(change_set_id)
        if record.status is ChangeSetStatus.APPLIED:
            # The desktop UI can retry after a lost response. Returning the
            # committed record is safe and prevents a duplicate write.
            return record
        if record.status not in {ChangeSetStatus.REVIEWING, ChangeSetStatus.READY}:
            raise ChangeSetStateError(
                f"direct apply is not allowed in state {record.status.value}"
            )
        self._require_review_revision(record, request.expected_review_revision)
        # 作者已经手动拒掉几块也照常保存：只把还没表态的块当作接受，
        # 被拒的块保持被拒，落盘的就是作者取舍后的结果，不再整批弹回审阅页。
        pending = [
            HunkDecisionUpdate(
                hunk_id=hunk.hunk_id,
                decision=HunkDecision.ACCEPTED,
            )
            for change in record.changes
            for hunk in change.hunks
            if hunk.decision is HunkDecision.PENDING
        ]
        if pending:
            record = self.decide(
                change_set_id,
                ChangeSetDecisionRequest(
                    expected_review_revision=record.review_revision,
                    decisions=pending,
                ),
            )

        return self.apply(
            change_set_id,
            ChangeSetApplyRequest(
                expected_review_revision=record.review_revision,
            ),
        )

    def rollback(
        self,
        change_set_id: str,
        request: ChangeSetRollbackRequest,
    ) -> ChangeSetResponse:
        record = self.get(change_set_id)
        if record.status is not ChangeSetStatus.APPLIED or record.writeback is None:
            raise ChangeSetStateError("only applied change sets can be rolled back")
        self._require_review_revision(record, request.expected_review_revision)
        binding = self._require_binding(record.project_id)
        root, snapshot, current = self._snapshot(binding)

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
                if result.operation is ChangeOperation.MOVE:
                    # 撤回移动/改名＝挪回：新位置的原文件原子改名回源路径；
                    # 新位置上的内容若被作者又改过，按既有规矩拒绝撤回。
                    if result.target_relative_path is None:
                        raise ChangeSetWritebackError(
                            "rollback move result has no target path: "
                            f"{result.relative_path}"
                        )
                    moved_path = root / Path(result.target_relative_path)
                    restore_path = root / Path(result.relative_path)
                    moved_file = files_by_path.get(
                        result.target_relative_path.casefold()
                    )
                    if moved_file is None:
                        raise ChangeSetConflictError(
                            "这份文件后来又被挪走了，不能直接撤销："
                            f"{result.target_relative_path}"
                        )
                    moved_bytes, _, _ = self._read_snapshot_file(root, moved_file)
                    if result.after_sha256 is None or (
                        _sha256(moved_bytes) != result.after_sha256
                    ):
                        raise ChangeSetConflictError(
                            "这份文件后来又被改过，已保留现有内容，不能直接撤销："
                            f"{result.target_relative_path}"
                        )
                    if os.path.lexists(restore_path):
                        raise ChangeSetConflictError(
                            "原来的位置已经有别的文件了，不能直接撤销："
                            f"{result.relative_path}"
                        )
                    self._validate_existing_parent_chain(root, restore_path.parent)
                    rollback_backup = artifact_root / "rollback-current" / Path(
                        result.relative_path
                    )
                    rollback_backup.parent.mkdir(parents=True, exist_ok=True)
                    rollback_backup.write_bytes(moved_bytes)
                    prepared.append(
                        _PreparedWrite(
                            change=next(
                                change
                                for change in record.changes
                                if change.change_id == result.change_id
                            ),
                            root=root,
                            target=restore_path,
                            before_bytes=moved_bytes,
                            after_bytes=moved_bytes,
                            backup=rollback_backup,
                            temporary=None,
                            tombstone=None,
                            accepted_hunks=result.accepted_hunk_ids,
                            rejected_hunks=result.rejected_hunk_ids,
                            source=moved_path,
                        )
                    )
                    continue
                target = root / Path(result.relative_path)
                if result.operation is ChangeOperation.DELETE and os.path.lexists(target):
                    raise ChangeSetConflictError(
                        "原来的位置已有新内容，已保留现有文件，不能直接撤销删除："
                        f"{result.relative_path}"
                    )
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
                            "这份文件后来又被改过，已保留现有内容，不能直接撤销："
                            f"{result.relative_path}"
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
                base_revision=current.revision,
                base_fingerprint=current.fingerprint,
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
            if isinstance(exc, ChangeSetError):
                raise
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
        if any(
            part.startswith(".") or part.casefold() in {"node_modules", "__pycache__"}
            for part in candidate.parts[:-1]
        ):
            raise ChangeSetInputError("new files cannot target hidden or system directories")
        normalized = candidate.as_posix()
        return normalized

    @staticmethod
    def _validate_text(relative_path: str, content: str) -> None:
        if "\x00" in content:
            raise ChangeSetInputError("project text cannot contain NUL characters")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ChangeSetInputError("project text must be valid UTF-8") from exc
        try:
            decode_project_text(encoded)
        except ValueError as exc:
            raise ChangeSetInputError("project text contains binary characters") from exc
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
            text = decode_project_text(content)[0]
        except ValueError as exc:
            raise ChangeSetInputError(
                f"project file is not readable text: {file_record.relative_path}"
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
        if operation is ChangeOperation.MOVE:
            # 移动/改名没有文字差异，留一条零长度标记 hunk，让审阅/撤回链
            # 与 create/update/delete 同构（接受它即同意这次挪动）。
            empty_hash = _text_sha256("")
            material = f"{change_id}\0move".encode("utf-8")
            return [
                ChangeHunk(
                    hunk_id=f"hunk-{_sha256(material)[:32]}",
                    index=0,
                    before_start=0,
                    before_end=0,
                    after_start=0,
                    after_end=0,
                    before_text="",
                    after_text="",
                    before_sha256=empty_hash,
                    after_sha256=empty_hash,
                )
            ]
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

        if change.operation is ChangeOperation.MOVE:
            # 移动/改名：字节原样跟着走（after == before），不需要临时文件
            # 与墓碑；apply 阶段一步原子改名（replace_with_retry 同卷 rename）。
            if change.target_relative_path is None:
                raise ChangeSetWritebackError(
                    f"move change has no target path: {change.relative_path}"
                )
            move_target = root / Path(change.target_relative_path)
            if os.path.lexists(move_target):
                raise ChangeSetConflictError(
                    f"move target now exists: {change.target_relative_path}"
                )
            move_parent = move_target.parent
            missing = []
            cursor = move_parent
            while cursor != root and not cursor.exists():
                missing.append(cursor)
                cursor = cursor.parent
            self._validate_existing_parent_chain(root, cursor)
            for directory in reversed(missing):
                directory.mkdir()
                created_directories.append(directory)
            self._validate_existing_parent_chain(root, move_parent)
            if before_bytes is None:
                raise ChangeSetWritebackError(
                    f"move change has no source bytes: {change.relative_path}"
                )
            backup = artifact_root / "backups" / Path(change.relative_path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before_bytes)
            if _sha256(backup.read_bytes()) != change.before_sha256:
                raise ChangeSetWritebackError(
                    f"backup verification failed: {change.relative_path}"
                )
            return _PreparedWrite(
                change=change,
                root=root,
                target=move_target,
                before_bytes=before_bytes,
                after_bytes=before_bytes,
                backup=backup,
                temporary=None,
                tombstone=None,
                accepted_hunks=accepted_hunks,
                rejected_hunks=rejected_hunks,
                source=target,
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
                original_bytes=before_bytes,
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
        if item.change.operation is ChangeOperation.MOVE:
            # 移动/改名＝源文件原子改名成目标（同卷 rename，撤回时挪回）。
            if item.source is None or item.before_bytes is None:
                raise ChangeSetWritebackError("move operation has no source")
            try:
                if _is_link_or_reparse(item.source):
                    raise ChangeSetConflictError(
                        f"move source became a link: {item.change.relative_path}"
                    )
                if int(getattr(item.source.lstat(), "st_nlink", 1)) > 1:
                    raise ChangeSetConflictError(
                        f"move source became hardlinked: {item.change.relative_path}"
                    )
                current = item.source.read_bytes()
            except OSError as exc:
                raise ChangeSetConflictError(
                    f"move source became unreadable: {item.change.relative_path}"
                ) from exc
            if _sha256(current) != _sha256(item.before_bytes):
                raise ChangeSetConflictError(
                    f"move source changed during writeback: {item.change.relative_path}"
                )
            if os.path.lexists(item.target):
                raise ChangeSetConflictError(
                    f"move target appeared during writeback: {item.change.relative_path}"
                )
            replace_with_retry(item.source, item.target)
            item.applied = True
            return
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
            replace_with_retry(item.target, item.tombstone)
        else:
            if item.temporary is None:
                raise ChangeSetWritebackError("write operation has no temporary file")
            replace_with_retry(item.temporary, item.target)
            item.temporary = None
        item.applied = True

    @staticmethod
    def _restore_prepared(items: list[_PreparedWrite]) -> str | None:
        errors: list[str] = []
        for item in reversed(items):
            try:
                if item.change.operation is ChangeOperation.MOVE:
                    # 移动/改名的补偿＝挪回：目标文件改名回源路径。
                    # 改名是单步动作，只认两种状态：已挪走（目标在）或已归位。
                    if item.applied and item.source is not None:
                        current = ChangeSetService._read_recovery_file(
                            item.target, item.root
                        )
                        before_hash = (
                            None
                            if item.before_bytes is None
                            else _sha256(item.before_bytes)
                        )
                        current_hash = None if current is None else _sha256(current)
                        if current_hash == before_hash:
                            if os.path.lexists(item.source):
                                raise ChangeSetConflictError(
                                    "move source is occupied before compensation"
                                )
                            ChangeSetService._validate_existing_parent_chain(
                                item.root, item.source.parent
                            )
                            replace_with_retry(item.target, item.source)
                        elif current is None:
                            restored = ChangeSetService._read_recovery_file(
                                item.source, item.root
                            )
                            if restored is None or _sha256(restored) != before_hash:
                                raise ChangeSetConflictError(
                                    "move target and source both missing before compensation"
                                )
                        else:
                            raise ChangeSetConflictError(
                                "write target changed before compensation"
                            )
                        item.applied = False
                    continue
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
                            replace_with_retry(item.tombstone, item.target)
                        else:
                            temporary = ChangeSetService._write_temporary_sibling(
                                item.target,
                                item.before_bytes,
                            )
                            replace_with_retry(temporary, item.target)
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


    def _record_writeback_evidence(
        self,
        record: ChangeSetResponse,
        manifest: WritebackManifest,
        *,
        artifact_root: Path,
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
            "formal_draft_authorization_receipt_id": None,
            "formal_draft_authorization_receipt_hash": None,
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
            if item.change.operation is ChangeOperation.MOVE:
                # 移动/改名落盘后文件待在 item.target（撤回时＝源路径）。
                result_file = files_by_path.get(
                    item.target.relative_to(item.root).as_posix().casefold()
                )
            else:
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
                    ChangeOperation.MOVE: ChangeOperation.MOVE,
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
                    target_relative_path=(
                        item.change.target_relative_path
                        if item.change.operation is ChangeOperation.MOVE
                        else None
                    ),
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
            if item.change.operation is ChangeOperation.MOVE:
                # 移动/改名落盘后：目标在位且字节未变，源路径已经空出来。
                expected_rel = item.target.relative_to(item.root).as_posix()
                result_file = files_by_path.get(expected_rel.casefold())
                actual_bytes = ChangeSetService._read_recovery_file(item.target, root)
                expected_hash = _sha256(item.after_bytes) if item.after_bytes is not None else None
                actual_hash = None if actual_bytes is None else _sha256(actual_bytes)
                if (
                    actual_hash != expected_hash
                    or result_file is None
                    or result_file.sha256 != expected_hash
                ):
                    raise ChangeSetConflictError(
                        f"writeback target changed before commit: {item.change.relative_path}"
                    )
                if item.source is not None:
                    source_rel = item.source.relative_to(item.root).as_posix()
                    if (
                        files_by_path.get(source_rel.casefold()) is not None
                        or os.path.lexists(item.source)
                    ):
                        raise ChangeSetConflictError(
                            "move source still present after writeback: "
                            f"{item.change.relative_path}"
                        )
                continue
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
                        # 移动/改名的落点（本次事务把它挪到哪里）；恢复时靠它
                        # 判断「已挪走/未挪走」两种半截状态。
                        "move_target_path": (
                            item.target.relative_to(item.root).as_posix()
                            if item.change.operation is ChangeOperation.MOVE
                            else None
                        ),
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
