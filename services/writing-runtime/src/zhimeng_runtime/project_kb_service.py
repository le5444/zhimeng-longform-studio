from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from zhimeng_runtime.context_models import canonical_json, canonical_json_hash
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCard,
    KnowledgeCardStatus,
    KnowledgeFormalizationReceipt,
    KnowledgeFormalizationReceiptV2,
    KnowledgeLineageStatus,
    KnowledgeVersionFormalizationStatus,
)
from zhimeng_runtime.knowledge_store import (
    KnowledgeProjectStateSnapshot,
    KnowledgeStore,
    KnowledgeStoreError,
    KnowledgeStoreNotFound,
)
from zhimeng_runtime.project_kb_models import (
    ProjectKbActiveIndexEntry,
    ProjectKbArtifact,
    ProjectKbCounts,
    ProjectKbCurrentPointer,
    ProjectKbPendingPublication,
    ProjectKbProjectRef,
    ProjectKbRebuildRequest,
    ProjectKbRebuildResult,
    ProjectKbScopeState,
    ProjectKbSnapshotManifest,
    ProjectKbSourceOfTruth,
    ProjectKbStateRecord,
    ProjectKbStatus,
    project_kb_manifest_hash,
    project_kb_pointer_hash,
    project_kb_publication_hash,
    project_kb_state_hash,
    project_kb_state_id,
)
from zhimeng_runtime.project_reader import ProjectReader, ProjectReaderError
from zhimeng_runtime.fs_atomic import replace_with_retry


PROJECT_ID_PATTERN = re.compile(r"^project-[0-9a-f]{24}$")
MAX_POINTER_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 512 * 1024


class ProjectKbServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: list[dict[str, object]] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = details or []
        self.retryable = retryable


class ProjectKbNotFoundError(ProjectKbServiceError):
    pass


class ProjectKbConflictError(ProjectKbServiceError):
    pass


class ProjectKbTamperedError(ProjectKbServiceError):
    pass


class _SnapshotTampered(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _ProjectObservation:
    project_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _SnapshotBundle:
    manifest: ProjectKbSnapshotManifest
    manifest_bytes: bytes
    artifacts: dict[str, bytes]


class ProjectKbService:
    """Publishes a rebuildable, versioned projection of Runtime knowledge state."""

    def __init__(
        self,
        database: SqliteStore,
        project_reader: ProjectReader,
        state_root: Path,
    ) -> None:
        requested_root = state_root.expanduser()
        if requested_root.exists() and _is_link_or_junction(requested_root):
            raise ValueError("project KB state root cannot be a link or junction")
        self.database = database
        self.store = KnowledgeStore(database)
        self.project_reader = project_reader
        self.state_root = requested_root.resolve(strict=False)
        self._locks_guard = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}

    def get_state(
        self,
        project_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ProjectKbStateRecord:
        project_id = self._validate_project_id(project_id)
        binding = (
            self.database.get_project_binding(project_id)
            if connection is None
            else self.database.get_project_binding_in_connection(connection, project_id)
        )
        if binding is None:
            raise ProjectKbNotFoundError("project binding was not found")
        if connection is None:
            knowledge_reader = lambda: self.store.project_state_snapshot(project_id)
        else:
            knowledge_reader = lambda: self.store.project_state_snapshot_in_connection(
                connection,
                project_id,
            )
        relative_root = self._relative_root(project_id)
        project_root = self.state_root / f"{project_id}.kb"
        current_path = project_root / "current.json"
        if _is_link_or_junction(self.state_root):
            return ProjectKbStateRecord(
                project_id=project_id,
                status=ProjectKbStatus.TAMPERED,
                runtime_relative_root=relative_root,
                reasons=["project_kb_state_root_is_link"],
            )
        if not current_path.exists():
            if _is_link_or_junction(project_root):
                return ProjectKbStateRecord(
                    project_id=project_id,
                    status=ProjectKbStatus.TAMPERED,
                    runtime_relative_root=relative_root,
                    reasons=["project_kb_root_is_link"],
                )
            if _is_link_or_junction(current_path):
                return ProjectKbStateRecord(
                    project_id=project_id,
                    status=ProjectKbStatus.TAMPERED,
                    runtime_relative_root=relative_root,
                    reasons=["project_kb_pointer_is_link"],
                )
            return ProjectKbStateRecord(
                project_id=project_id,
                status=ProjectKbStatus.MISSING,
                runtime_relative_root=relative_root,
            )

        pointer: ProjectKbCurrentPointer | None = None
        manifest: ProjectKbSnapshotManifest | None = None
        try:
            pointer, manifest = self._read_verified_current(project_id, project_root)
        except _SnapshotTampered as exc:
            return ProjectKbStateRecord(
                project_id=project_id,
                status=ProjectKbStatus.TAMPERED,
                runtime_relative_root=relative_root,
                pointer=pointer,
                manifest=manifest,
                reasons=[str(exc)],
            )

        reasons: list[str] = []
        initial_state_hash: str | None = None
        try:
            knowledge = knowledge_reader()
        except KnowledgeStoreNotFound:
            knowledge = None
            reasons.append("knowledge_scope_missing")
        except KnowledgeStoreError:
            knowledge = None
            reasons.append("runtime_knowledge_unavailable")
        if knowledge is not None:
            current_fingerprint = canonical_json_hash(knowledge.fingerprint_material)
            try:
                current_state_hash = self._build_bundle(knowledge).manifest.state_hash
            except (ValidationError, ValueError):
                current_state_hash = None
                reasons.append("runtime_knowledge_unavailable")
            initial_state_hash = current_state_hash
            if manifest.project.revision != knowledge.project.revision:
                reasons.append("project_revision_changed")
            if manifest.project.fingerprint != knowledge.project.fingerprint:
                reasons.append("project_fingerprint_changed")
            if manifest.knowledge_scope.state_revision != knowledge.scope.state_revision:
                reasons.append("knowledge_scope_revision_changed")
            if manifest.knowledge_fingerprint != current_fingerprint:
                reasons.append("knowledge_fingerprint_changed")
            if current_state_hash is not None and manifest.state_hash != current_state_hash:
                reasons.append("knowledge_projection_changed")
        try:
            live = self._inspect_project(binding)
        except ProjectReaderError:
            reasons.append("project_files_unavailable")
        else:
            if live.project_id != project_id:
                reasons.append("project_identity_changed")
            if live.fingerprint != manifest.project.fingerprint:
                reasons.append("project_files_changed")

        try:
            verified_knowledge = knowledge_reader()
            verified_state_hash = self._build_bundle(
                verified_knowledge
            ).manifest.state_hash
        except KnowledgeStoreNotFound:
            verified_state_hash = None
            reasons.append("knowledge_scope_missing")
        except (KnowledgeStoreError, ValidationError, ValueError):
            verified_state_hash = None
            reasons.append("runtime_knowledge_unavailable")
        if verified_state_hash != initial_state_hash:
            reasons.append("knowledge_changed_during_read")
        if (
            verified_state_hash is not None
            and manifest.state_hash != verified_state_hash
        ):
            reasons.append("knowledge_projection_changed")

        return ProjectKbStateRecord(
            project_id=project_id,
            status=(ProjectKbStatus.STALE if reasons else ProjectKbStatus.CURRENT),
            runtime_relative_root=relative_root,
            pointer=pointer,
            manifest=manifest,
            reasons=sorted(set(reasons)),
        )

    def require_current_binding(
        self,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> ProjectKbSnapshotManifest:
        """Resolve one verified current snapshot without rebuilding or inferring it."""

        return self._require_current_binding_record(
            self.get_state(project_id),
            project_kb_state_id=project_kb_state_id,
            project_kb_manifest_hash=project_kb_manifest_hash,
            expected_knowledge_fingerprint=expected_knowledge_fingerprint,
        )

    def require_current_binding_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> ProjectKbSnapshotManifest:
        """Verify a binding inside a caller-owned SQLite transaction."""

        return self._require_current_binding_record(
            self.get_state(project_id, connection=connection),
            project_kb_state_id=project_kb_state_id,
            project_kb_manifest_hash=project_kb_manifest_hash,
            expected_knowledge_fingerprint=expected_knowledge_fingerprint,
        )

    def _require_current_binding_record(
        self,
        record: ProjectKbStateRecord,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None,
    ) -> ProjectKbSnapshotManifest:
        if record.status is ProjectKbStatus.TAMPERED:
            raise ProjectKbTamperedError(
                "project KB state failed integrity verification",
                details=[
                    {
                        "code": "project_kb_state_tampered",
                        "reasons": list(record.reasons),
                    }
                ],
            )
        if record.status is ProjectKbStatus.MISSING:
            raise ProjectKbConflictError(
                "project KB state has not been published",
                details=[{"code": "project_kb_state_missing"}],
                retryable=True,
            )
        if record.status is not ProjectKbStatus.CURRENT:
            raise ProjectKbConflictError(
                "project KB state is stale",
                details=[
                    {
                        "code": "project_kb_state_stale",
                        "reasons": list(record.reasons),
                    }
                ],
                retryable=True,
            )
        pointer = record.pointer
        manifest = record.manifest
        if pointer is None or manifest is None:  # pragma: no cover - model invariant.
            raise ProjectKbTamperedError(
                "current project KB state has no verified snapshot",
                details=[{"code": "project_kb_state_tampered"}],
            )
        if (
            pointer.state_id != project_kb_state_id
            or pointer.manifest_hash != project_kb_manifest_hash
            or manifest.state_id != project_kb_state_id
            or manifest.manifest_hash != project_kb_manifest_hash
        ):
            raise ProjectKbConflictError(
                "project KB binding changed",
                details=[
                    {
                        "code": "project_kb_binding_changed",
                        "expected_state_id": project_kb_state_id,
                        "observed_state_id": manifest.state_id,
                        "expected_manifest_hash": project_kb_manifest_hash,
                        "observed_manifest_hash": manifest.manifest_hash,
                    }
                ],
                retryable=True,
            )
        if (
            expected_knowledge_fingerprint is not None
            and manifest.knowledge_fingerprint != expected_knowledge_fingerprint
        ):
            raise ProjectKbConflictError(
                "native knowledge fingerprint does not match the project KB manifest",
                details=[
                    {
                        "code": "project_kb_knowledge_fingerprint_mismatch",
                        "expected_knowledge_fingerprint": (
                            expected_knowledge_fingerprint
                        ),
                        "manifest_knowledge_fingerprint": (
                            manifest.knowledge_fingerprint
                        ),
                    }
                ],
                retryable=True,
            )
        return manifest

    @contextmanager
    def current_binding_lease(
        self,
        project_id: str,
        *,
        project_kb_state_id: str,
        project_kb_manifest_hash: str,
        expected_knowledge_fingerprint: str | None = None,
    ) -> Iterator[ProjectKbSnapshotManifest]:
        """Keep one verified current binding stable against project-KB publication."""

        project_id = self._validate_project_id(project_id)
        with self._project_lock(project_id):
            yield self.require_current_binding(
                project_id,
                project_kb_state_id=project_kb_state_id,
                project_kb_manifest_hash=project_kb_manifest_hash,
                expected_knowledge_fingerprint=expected_knowledge_fingerprint,
            )

    def rebuild(
        self,
        project_id: str,
        request: ProjectKbRebuildRequest,
    ) -> ProjectKbRebuildResult:
        project_id = self._validate_project_id(project_id)
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise ProjectKbNotFoundError("project binding was not found")
        try:
            self.store.ensure_project_scope(project_id)
        except KnowledgeStoreNotFound as exc:
            raise ProjectKbNotFoundError(str(exc)) from exc
        except KnowledgeStoreError as exc:
            raise ProjectKbConflictError(str(exc), retryable=True) from exc

        with self._project_lock(project_id):
            try:
                with self.store.locked_project_state_reader(project_id) as read:
                    before = read()
                    self._assert_rebuild_cas(before, request)
                    live_before = self._inspect_project(before.project)
                    if (
                        live_before.project_id != project_id
                        or live_before.fingerprint != before.project.fingerprint
                    ):
                        raise ProjectKbConflictError(
                            "project files changed after the Runtime binding was observed",
                            details=[{"code": "project_files_changed"}],
                            retryable=True,
                        )

                    bundle = self._build_bundle(before)
                    project_root = self._prepare_project_root(
                        project_id,
                        bundle.manifest.state_id,
                    )
                    temporary = self._write_snapshot_temporary(project_root, bundle)
                    try:
                        after = read()
                        after_bundle = self._build_bundle(after)
                        if after_bundle.manifest.state_hash != bundle.manifest.state_hash:
                            raise ProjectKbConflictError(
                                "Runtime knowledge changed while the project KB was built",
                                details=[{"code": "knowledge_state_changed"}],
                                retryable=True,
                            )
                        live_after = self._inspect_project(after.project)
                        if live_after != live_before:
                            raise ProjectKbConflictError(
                                "project files changed while the project KB was built",
                                details=[{"code": "project_files_changed"}],
                                retryable=True,
                            )
                        snapshot_root = (
                            project_root / "snapshots" / bundle.manifest.state_id
                        )
                        publication = self._pending_publication_for(bundle)
                        pending_path = self._write_pending_publication(
                            project_root,
                            publication,
                        )
                        reused_snapshot = snapshot_root.exists()
                        published_snapshot = False
                        try:
                            if reused_snapshot:
                                self._assert_snapshot_matches(snapshot_root, bundle)
                            else:
                                replace_with_retry(temporary, snapshot_root)
                                published_snapshot = True
                            pointer = self._pointer_for(bundle)
                            try:
                                self._atomic_write_current(project_root, pointer)
                            except Exception:
                                pointer_committed = self._pointer_is_current(
                                    project_root,
                                    pointer,
                                )
                                if (
                                    published_snapshot
                                    and not pointer_committed
                                    and snapshot_root.exists()
                                ):
                                    if (
                                        _is_link_or_junction(snapshot_root)
                                        or not snapshot_root.is_dir()
                                    ):
                                        raise _SnapshotTampered(
                                            "project_kb_unreferenced_snapshot_invalid"
                                        )
                                    shutil.rmtree(snapshot_root)
                                if not pointer_committed:
                                    raise
                        except Exception:
                            self._clear_pending_publication(
                                pending_path,
                                publication,
                            )
                            raise
                        else:
                            self._clear_pending_publication(
                                pending_path,
                                publication,
                            )
                    finally:
                        if temporary.exists():
                            shutil.rmtree(temporary)
            except ProjectKbServiceError:
                raise
            except ProjectReaderError as exc:
                raise ProjectKbConflictError(
                    "project files could not be read consistently",
                    details=[{"code": "project_files_unavailable"}],
                    retryable=True,
                ) from exc
            except (_SnapshotTampered, OSError, ValidationError, ValueError) as exc:
                raise ProjectKbTamperedError(
                    f"project KB publication failed closed: {exc}",
                    retryable=False,
                ) from exc

        record = ProjectKbStateRecord(
            project_id=project_id,
            status=ProjectKbStatus.CURRENT,
            runtime_relative_root=self._relative_root(project_id),
            pointer=pointer,
            manifest=bundle.manifest,
            reasons=[],
        )
        return ProjectKbRebuildResult(
            reused_snapshot=reused_snapshot,
            record=record,
        )

    def _build_bundle(self, snapshot: KnowledgeProjectStateSnapshot) -> _SnapshotBundle:
        project = ProjectKbProjectRef(
            project_id=snapshot.project.project_id,
            revision=snapshot.project.revision,
            fingerprint=snapshot.project.fingerprint,
        )
        source_of_truth = ProjectKbSourceOfTruth()
        knowledge_fingerprint = canonical_json_hash(snapshot.fingerprint_material)
        scope_state = ProjectKbScopeState(
            project=project,
            knowledge_scope=snapshot.scope,
            knowledge_fingerprint=knowledge_fingerprint,
            inherited_card_ids=list(snapshot.inherited_card_ids),
            source_of_truth=source_of_truth,
        )
        candidates = [
            candidate.model_dump(mode="json", by_alias=True)
            for candidate in snapshot.candidates
        ]
        cards = [card.model_dump(mode="json", by_alias=True) for card in snapshot.cards]
        receipts = [
            receipt.model_dump(mode="json", by_alias=True)
            for receipt in snapshot.formalization_receipts
        ]
        origin_scope_ids = dict(snapshot.origin_scope_ids)
        index_entries: list[ProjectKbActiveIndexEntry] = []
        for card in snapshot.cards:
            version = card.active_version
            if (
                card.status is not KnowledgeCardStatus.ACTIVE
                or version.lineage_status is not KnowledgeLineageStatus.VERIFIED
                or version.formalization_status
                is not KnowledgeVersionFormalizationStatus.VERIFIED
                or version.formalization is None
            ):
                continue
            payload = version.card
            index_entries.append(
                ProjectKbActiveIndexEntry(
                    card_id=card.card_id,
                    card_type=str(payload.get("card_type") or "unknown"),
                    version_id=version.version_id,
                    content_hash=version.content_hash,
                    origin_scope_id=origin_scope_ids[card.card_id],
                    formalization_receipt_id=(
                        version.formalization.formalization_receipt_id
                    ),
                    formalization_receipt_hash=(
                        version.formalization.formalization_receipt_hash
                    ),
                    title=_bounded_text(payload.get("title"), 500),
                    summary=_bounded_text(payload.get("summary"), 8_000),
                    tags=_string_list(payload.get("tags")),
                    keywords=_string_list(payload.get("keywords")),
                    applicable_tasks=_string_list(payload.get("applicable_tasks")),
                )
            )
        index_entries.sort(key=lambda item: item.card_id)

        artifacts = {
            "state/scope.json": _json_bytes(scope_state),
            "knowledge/candidates.jsonl": _jsonl_bytes(candidates),
            "knowledge/cards.jsonl": _jsonl_bytes(cards),
            "receipts/formalization.jsonl": _jsonl_bytes(receipts),
            "distillation_index/entries.jsonl": _jsonl_bytes(index_entries),
        }
        artifact_refs = [
            ProjectKbArtifact(
                logical_path=logical_path,
                size_bytes=len(content),
                sha256=_sha256(content),
            )
            for logical_path, content in sorted(artifacts.items())
        ]
        counts = ProjectKbCounts(
            candidates=len(candidates),
            cards=len(cards),
            active_cards=sum(
                card.status is KnowledgeCardStatus.ACTIVE for card in snapshot.cards
            ),
            formalization_receipts=len(receipts),
            active_index_entries=len(index_entries),
        )
        manifest_seed: dict[str, Any] = {
            "schema": "zhimeng.project-kb-state.v1",
            "project": project.model_dump(mode="json", by_alias=True),
            "knowledge_scope": snapshot.scope.to_ref().model_dump(
                mode="json", by_alias=True
            ),
            "knowledge_fingerprint": knowledge_fingerprint,
            "inherited_card_ids": list(snapshot.inherited_card_ids),
            "source_of_truth": source_of_truth.model_dump(mode="json", by_alias=True),
            "counts": counts.model_dump(mode="json", by_alias=True),
            "artifacts": [
                artifact.model_dump(mode="json", by_alias=True)
                for artifact in artifact_refs
            ],
        }
        state_hash = project_kb_state_hash(manifest_seed)
        manifest_seed["state_id"] = project_kb_state_id(state_hash)
        manifest_seed["state_hash"] = state_hash
        manifest_seed["manifest_hash"] = project_kb_manifest_hash(manifest_seed)
        manifest = ProjectKbSnapshotManifest.model_validate(manifest_seed)
        return _SnapshotBundle(
            manifest=manifest,
            manifest_bytes=_json_bytes(manifest),
            artifacts=artifacts,
        )

    def _assert_rebuild_cas(
        self,
        snapshot: KnowledgeProjectStateSnapshot,
        request: ProjectKbRebuildRequest,
    ) -> None:
        knowledge_fingerprint = canonical_json_hash(snapshot.fingerprint_material)
        mismatches: list[dict[str, object]] = []
        comparisons = (
            (
                "project_revision",
                request.expected_project_revision,
                snapshot.project.revision,
            ),
            (
                "project_fingerprint",
                request.expected_project_fingerprint,
                snapshot.project.fingerprint,
            ),
            (
                "scope_state_revision",
                request.expected_scope_state_revision,
                snapshot.scope.state_revision,
            ),
            (
                "knowledge_fingerprint",
                request.expected_knowledge_fingerprint,
                knowledge_fingerprint,
            ),
        )
        for field, expected, actual in comparisons:
            if expected != actual:
                mismatches.append(
                    {"field": field, "expected": expected, "actual": actual}
                )
        if mismatches:
            raise ProjectKbConflictError(
                "project KB rebuild expectations are stale",
                details=mismatches,
                retryable=True,
            )

    def _inspect_project(self, binding: ProjectBindingRecord) -> _ProjectObservation:
        payload = self.project_reader.inspect(binding.root_path)
        project = payload.get("project")
        if not isinstance(project, dict):
            raise ProjectReaderError("project inspection omitted its identity")
        project_id = str(project.get("project_id") or "")
        fingerprint = str(project.get("fingerprint") or "")
        if not PROJECT_ID_PATTERN.fullmatch(project_id) or not re.fullmatch(
            r"[0-9a-f]{64}", fingerprint
        ):
            raise ProjectReaderError("project inspection returned an invalid identity")
        return _ProjectObservation(project_id=project_id, fingerprint=fingerprint)

    def _prepare_project_root(
        self,
        project_id: str,
        target_state_id: str,
    ) -> Path:
        self.state_root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_junction(self.state_root):
            raise _SnapshotTampered("project_kb_state_root_is_link")
        project_root = self.state_root / f"{project_id}.kb"
        project_root.mkdir(exist_ok=True)
        if _is_link_or_junction(project_root):
            raise _SnapshotTampered("project_kb_root_is_link")
        snapshots = project_root / "snapshots"
        snapshots.mkdir(exist_ok=True)
        if _is_link_or_junction(snapshots):
            raise _SnapshotTampered("project_kb_snapshots_root_is_link")
        for orphan in snapshots.iterdir():
            if orphan.name.startswith(".tmp-"):
                if _is_link_or_junction(orphan) or not orphan.is_dir():
                    raise _SnapshotTampered("project_kb_temporary_snapshot_invalid")
                shutil.rmtree(orphan)
                continue
            if not re.fullmatch(r"project-kb-state-[0-9a-f]{32}", orphan.name):
                raise _SnapshotTampered("project_kb_snapshot_entry_invalid")
            if _is_link_or_junction(orphan) or not orphan.is_dir():
                raise _SnapshotTampered("project_kb_snapshot_root_invalid")
        for orphan in project_root.iterdir():
            is_pointer_temp = (
                orphan.name.startswith(".current-")
                and orphan.name.endswith(".tmp")
            )
            is_publication_temp = (
                orphan.name.startswith(".publication-")
                and orphan.name.endswith(".tmp")
            )
            if not (is_pointer_temp or is_publication_temp):
                continue
            if _is_link_or_junction(orphan) or not orphan.is_file():
                raise _SnapshotTampered("project_kb_temporary_control_file_invalid")
            orphan.unlink()
        self._recover_pending_publication(
            project_root,
            project_id,
            target_state_id,
        )
        return project_root

    def _write_snapshot_temporary(
        self,
        project_root: Path,
        bundle: _SnapshotBundle,
    ) -> Path:
        snapshots_root = project_root / "snapshots"
        temporary = snapshots_root / f".tmp-{uuid4().hex}"
        temporary.mkdir()
        try:
            for logical_path, content in bundle.artifacts.items():
                target = temporary.joinpath(*PurePosixPath(logical_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_new_file(target, content)
            _write_new_file(temporary / "manifest.json", bundle.manifest_bytes)
            self._assert_snapshot_matches(temporary, bundle)
            return temporary
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _assert_snapshot_matches(
        self,
        snapshot_root: Path,
        bundle: _SnapshotBundle,
    ) -> None:
        if _is_link_or_junction(snapshot_root) or not snapshot_root.is_dir():
            raise _SnapshotTampered("project_kb_snapshot_root_invalid")
        _assert_snapshot_inventory(snapshot_root, set(bundle.artifacts))
        manifest_path = snapshot_root / "manifest.json"
        if _read_regular_file(manifest_path, MAX_MANIFEST_BYTES) != bundle.manifest_bytes:
            raise _SnapshotTampered("project_kb_snapshot_manifest_mismatch")
        for logical_path, expected in bundle.artifacts.items():
            path = snapshot_root.joinpath(*PurePosixPath(logical_path).parts)
            if _read_regular_file(path, 512 * 1024 * 1024) != expected:
                raise _SnapshotTampered(f"project_kb_artifact_mismatch:{logical_path}")

    def _pointer_for(self, bundle: _SnapshotBundle) -> ProjectKbCurrentPointer:
        payload: dict[str, object] = {
            "schema": "zhimeng.project-kb-current-pointer.v1",
            "project_id": bundle.manifest.project.project_id,
            "state_id": bundle.manifest.state_id,
            "state_hash": bundle.manifest.state_hash,
            "manifest_hash": bundle.manifest.manifest_hash,
            "manifest_file_sha256": _sha256(bundle.manifest_bytes),
        }
        payload["pointer_hash"] = project_kb_pointer_hash(payload)
        return ProjectKbCurrentPointer.model_validate(payload)

    def _pending_publication_for(
        self,
        bundle: _SnapshotBundle,
    ) -> ProjectKbPendingPublication:
        payload: dict[str, object] = {
            "schema": "zhimeng.project-kb-pending-publication.v1",
            "project_id": bundle.manifest.project.project_id,
            "state_id": bundle.manifest.state_id,
            "state_hash": bundle.manifest.state_hash,
            "manifest_hash": bundle.manifest.manifest_hash,
        }
        payload["publication_hash"] = project_kb_publication_hash(payload)
        return ProjectKbPendingPublication.model_validate(payload)

    def _write_pending_publication(
        self,
        project_root: Path,
        publication: ProjectKbPendingPublication,
    ) -> Path:
        target = project_root / "publication.pending.json"
        if target.exists() or _is_link_or_junction(target):
            raise _SnapshotTampered("project_kb_pending_publication_already_exists")
        temporary = project_root / f".publication-{uuid4().hex}.tmp"
        try:
            _write_new_file(temporary, _json_bytes(publication))
            replace_with_retry(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def _clear_pending_publication(
        pending_path: Path,
        publication: ProjectKbPendingPublication,
    ) -> None:
        if _read_regular_file(pending_path, MAX_POINTER_BYTES) != _json_bytes(
            publication
        ):
            raise _SnapshotTampered("project_kb_pending_publication_changed")
        pending_path.unlink()

    def _recover_pending_publication(
        self,
        project_root: Path,
        project_id: str,
        target_state_id: str,
    ) -> None:
        pending_path = project_root / "publication.pending.json"
        if not pending_path.exists():
            if _is_link_or_junction(pending_path):
                raise _SnapshotTampered("project_kb_pending_publication_is_link")
            return
        publication = _parse_canonical_json_model(
            _read_regular_file(pending_path, MAX_POINTER_BYTES),
            ProjectKbPendingPublication,
        )
        if publication.project_id != project_id:
            raise _SnapshotTampered("project_kb_pending_publication_cross_project")
        current = self._read_current_pointer_if_present(project_root, project_id)
        snapshot_root = project_root / "snapshots" / publication.state_id
        if snapshot_root.exists():
            if _is_link_or_junction(snapshot_root) or not snapshot_root.is_dir():
                raise _SnapshotTampered("project_kb_pending_snapshot_invalid")
            referenced = current is not None and current.state_id == publication.state_id
            if not referenced and publication.state_id != target_state_id:
                shutil.rmtree(snapshot_root)
        elif _is_link_or_junction(snapshot_root):
            raise _SnapshotTampered("project_kb_pending_snapshot_is_link")
        self._clear_pending_publication(pending_path, publication)

    @staticmethod
    def _read_current_pointer_if_present(
        project_root: Path,
        project_id: str,
    ) -> ProjectKbCurrentPointer | None:
        current_path = project_root / "current.json"
        if not current_path.exists():
            if _is_link_or_junction(current_path):
                raise _SnapshotTampered("project_kb_pointer_is_link")
            return None
        pointer = _parse_canonical_json_model(
            _read_regular_file(current_path, MAX_POINTER_BYTES),
            ProjectKbCurrentPointer,
        )
        if pointer.project_id != project_id:
            raise _SnapshotTampered("project_kb_pointer_cross_project")
        return pointer

    def _atomic_write_current(
        self,
        project_root: Path,
        pointer: ProjectKbCurrentPointer,
    ) -> None:
        temporary = project_root / f".current-{uuid4().hex}.tmp"
        try:
            _write_new_file(temporary, _json_bytes(pointer))
            replace_with_retry(temporary, project_root / "current.json")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _pointer_is_current(
        project_root: Path,
        pointer: ProjectKbCurrentPointer,
    ) -> bool:
        try:
            return _read_regular_file(
                project_root / "current.json",
                MAX_POINTER_BYTES,
            ) == _json_bytes(pointer)
        except _SnapshotTampered:
            return False

    def _read_verified_current(
        self,
        project_id: str,
        project_root: Path,
    ) -> tuple[ProjectKbCurrentPointer, ProjectKbSnapshotManifest]:
        if _is_link_or_junction(project_root) or not project_root.is_dir():
            raise _SnapshotTampered("project_kb_root_invalid")
        current_bytes = _read_regular_file(project_root / "current.json", MAX_POINTER_BYTES)
        pointer = _parse_canonical_json_model(current_bytes, ProjectKbCurrentPointer)
        if pointer.project_id != project_id:
            raise _SnapshotTampered("project_kb_pointer_cross_project")
        snapshot_root = project_root / "snapshots" / pointer.state_id
        if _is_link_or_junction(snapshot_root) or not snapshot_root.is_dir():
            raise _SnapshotTampered("project_kb_snapshot_missing")
        manifest_bytes = _read_regular_file(
            snapshot_root / "manifest.json",
            MAX_MANIFEST_BYTES,
        )
        if _sha256(manifest_bytes) != pointer.manifest_file_sha256:
            raise _SnapshotTampered("project_kb_manifest_file_hash_mismatch")
        manifest = _parse_canonical_json_model(
            manifest_bytes,
            ProjectKbSnapshotManifest,
        )
        if (
            manifest.project.project_id != project_id
            or manifest.state_id != pointer.state_id
            or manifest.state_hash != pointer.state_hash
            or manifest.manifest_hash != pointer.manifest_hash
        ):
            raise _SnapshotTampered("project_kb_pointer_manifest_mismatch")
        _assert_snapshot_inventory(
            snapshot_root,
            {artifact.logical_path for artifact in manifest.artifacts},
        )
        artifact_bytes: dict[str, bytes] = {}
        for artifact in manifest.artifacts:
            path = snapshot_root.joinpath(*PurePosixPath(artifact.logical_path).parts)
            content = _read_regular_file(path, 512 * 1024 * 1024)
            if len(content) != artifact.size_bytes or _sha256(content) != artifact.sha256:
                raise _SnapshotTampered(
                    f"project_kb_artifact_hash_mismatch:{artifact.logical_path}"
                )
            artifact_bytes[artifact.logical_path] = content
        self._verify_artifact_contracts(manifest, artifact_bytes)
        return pointer, manifest

    @staticmethod
    def _verify_artifact_contracts(
        manifest: ProjectKbSnapshotManifest,
        artifacts: dict[str, bytes],
    ) -> None:
        scope = _parse_canonical_json_model(
            artifacts["state/scope.json"],
            ProjectKbScopeState,
        )
        candidates = _parse_canonical_jsonl(
            artifacts["knowledge/candidates.jsonl"],
            KnowledgeCandidate,
        )
        cards = _parse_canonical_jsonl(
            artifacts["knowledge/cards.jsonl"],
            KnowledgeCard,
        )
        receipts = _parse_formalization_jsonl(
            artifacts["receipts/formalization.jsonl"]
        )
        entries = _parse_canonical_jsonl(
            artifacts["distillation_index/entries.jsonl"],
            ProjectKbActiveIndexEntry,
        )
        if (
            scope.project != manifest.project
            or scope.knowledge_scope.to_ref() != manifest.knowledge_scope
            or scope.knowledge_fingerprint != manifest.knowledge_fingerprint
            or scope.inherited_card_ids != manifest.inherited_card_ids
            or scope.source_of_truth != manifest.source_of_truth
        ):
            raise _SnapshotTampered("project_kb_scope_manifest_mismatch")
        observed_counts = ProjectKbCounts(
            candidates=len(candidates),
            cards=len(cards),
            active_cards=sum(
                card.status is KnowledgeCardStatus.ACTIVE for card in cards
            ),
            formalization_receipts=len(receipts),
            active_index_entries=len(entries),
        )
        if observed_counts != manifest.counts:
            raise _SnapshotTampered("project_kb_artifact_counts_mismatch")
        candidate_ids = {candidate.candidate_id for candidate in candidates}
        if len(candidate_ids) != len(candidates):
            raise _SnapshotTampered("project_kb_candidate_ids_duplicated")
        card_ids = {card.card_id for card in cards}
        if len(card_ids) != len(cards):
            raise _SnapshotTampered("project_kb_card_ids_duplicated")
        if not set(manifest.inherited_card_ids).issubset(card_ids):
            raise _SnapshotTampered("project_kb_inherited_card_missing")
        version_candidates = {
            version.source_candidate_id
            for card in cards
            for version in card.versions
        }
        if not version_candidates.issubset(candidate_ids):
            raise _SnapshotTampered("project_kb_version_candidate_missing")
        receipts_by_id = {
            receipt.formalization_receipt_id: receipt for receipt in receipts
        }
        if len(receipts_by_id) != len(receipts):
            raise _SnapshotTampered("project_kb_formalization_ids_duplicated")
        entries_by_card = {entry.card_id: entry for entry in entries}
        if len(entries_by_card) != len(entries):
            raise _SnapshotTampered("project_kb_active_index_ids_duplicated")
        expected_index_ids: set[str] = set()
        for card in cards:
            version = card.active_version
            if (
                card.status is KnowledgeCardStatus.ACTIVE
                and version.lineage_status is KnowledgeLineageStatus.VERIFIED
                and version.formalization_status
                is KnowledgeVersionFormalizationStatus.VERIFIED
                and version.formalization is not None
            ):
                expected_index_ids.add(card.card_id)
                entry = entries_by_card.get(card.card_id)
                if entry is None or (
                    entry.version_id != version.version_id
                    or entry.content_hash != version.content_hash
                    or entry.formalization_receipt_id
                    != version.formalization.formalization_receipt_id
                    or entry.formalization_receipt_hash
                    != version.formalization.formalization_receipt_hash
                ):
                    raise _SnapshotTampered("project_kb_active_index_binding_mismatch")
                receipt = receipts_by_id.get(entry.formalization_receipt_id)
                if receipt is None or receipt.receipt_hash != entry.formalization_receipt_hash:
                    raise _SnapshotTampered("project_kb_active_index_receipt_missing")
        if set(entries_by_card) != expected_index_ids:
            raise _SnapshotTampered("project_kb_active_index_membership_mismatch")

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._project_locks.setdefault(project_id, threading.Lock())

    def _relative_root(self, project_id: str) -> str:
        return f"{self.state_root.name}/{project_id}.kb"

    @staticmethod
    def _validate_project_id(project_id: str) -> str:
        candidate = str(project_id).strip()
        if not PROJECT_ID_PATTERN.fullmatch(candidate):
            raise ProjectKbNotFoundError("project binding was not found")
        return candidate


def _json_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8") + b"\n"


def _jsonl_bytes(values: list[object]) -> bytes:
    return b"".join(_json_bytes(value) for value in values)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bounded_text(value: object, limit: int) -> str:
    return str(value if isinstance(value, str) else "")[:limit]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)][:256]


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _write_new_file(path: Path, content: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _read_regular_file(path: Path, maximum_size: int) -> bytes:
    try:
        if _is_link_or_junction(path) or not path.is_file():
            raise _SnapshotTampered("project_kb_file_missing_or_link")
        size = path.stat().st_size
        if size < 0 or size > maximum_size:
            raise _SnapshotTampered("project_kb_file_size_invalid")
        content = path.read_bytes()
    except _SnapshotTampered:
        raise
    except OSError as exc:
        raise _SnapshotTampered("project_kb_file_unreadable") from exc
    if len(content) != size:
        raise _SnapshotTampered("project_kb_file_changed_during_read")
    return content


def _assert_snapshot_inventory(
    snapshot_root: Path,
    artifact_paths: set[str],
) -> None:
    expected_files = {"manifest.json", *artifact_paths}
    expected_directories = {
        PurePosixPath(path).parent.as_posix()
        for path in artifact_paths
        if PurePosixPath(path).parent.as_posix() != "."
    }
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    pending = [snapshot_root]
    observed_entries = 0
    try:
        while pending:
            directory = pending.pop()
            for child in directory.iterdir():
                observed_entries += 1
                if observed_entries > 64:
                    raise _SnapshotTampered("project_kb_snapshot_inventory_too_large")
                if _is_link_or_junction(child):
                    raise _SnapshotTampered("project_kb_snapshot_inventory_has_link")
                relative = child.relative_to(snapshot_root).as_posix()
                if child.is_dir():
                    observed_directories.add(relative)
                    pending.append(child)
                elif child.is_file():
                    observed_files.add(relative)
                else:
                    raise _SnapshotTampered("project_kb_snapshot_inventory_invalid")
    except _SnapshotTampered:
        raise
    except OSError as exc:
        raise _SnapshotTampered("project_kb_snapshot_inventory_unreadable") from exc
    if observed_files != expected_files or observed_directories != expected_directories:
        raise _SnapshotTampered("project_kb_snapshot_inventory_mismatch")


def _parse_canonical_json_model(content: bytes, model: Any) -> Any:
    try:
        payload = json.loads(content.decode("utf-8"))
        record = model.model_validate(payload)
    except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise _SnapshotTampered("project_kb_json_invalid") from exc
    if content != _json_bytes(record):
        raise _SnapshotTampered("project_kb_json_not_canonical")
    return record


def _parse_canonical_jsonl(content: bytes, model: Any) -> list[Any]:
    if not content:
        return []
    if not content.endswith(b"\n"):
        raise _SnapshotTampered("project_kb_jsonl_not_canonical")
    records: list[Any] = []
    for raw_line in content.splitlines():
        if not raw_line:
            raise _SnapshotTampered("project_kb_jsonl_has_blank_record")
        try:
            payload = json.loads(raw_line.decode("utf-8"))
            records.append(model.model_validate(payload))
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise _SnapshotTampered("project_kb_jsonl_invalid") from exc
    if content != _jsonl_bytes(records):
        raise _SnapshotTampered("project_kb_jsonl_not_canonical")
    return records


def _parse_formalization_jsonl(
    content: bytes,
) -> list[KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2]:
    if not content:
        return []
    if not content.endswith(b"\n"):
        raise _SnapshotTampered("project_kb_jsonl_not_canonical")
    records: list[KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2] = []
    for raw_line in content.splitlines():
        try:
            payload = json.loads(raw_line.decode("utf-8"))
            model = (
                KnowledgeFormalizationReceiptV2
                if isinstance(payload, dict)
                and payload.get("schema") == "zhimeng.knowledge-formalization-receipt.v2"
                else KnowledgeFormalizationReceipt
            )
            records.append(model.model_validate(payload))
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise _SnapshotTampered("project_kb_formalization_jsonl_invalid") from exc
    if content != _jsonl_bytes(records):
        raise _SnapshotTampered("project_kb_jsonl_not_canonical")
    return records


__all__ = [
    "ProjectKbConflictError",
    "ProjectKbNotFoundError",
    "ProjectKbService",
    "ProjectKbServiceError",
    "ProjectKbTamperedError",
]
