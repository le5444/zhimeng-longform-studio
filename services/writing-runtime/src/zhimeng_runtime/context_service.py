from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zhimeng_runtime.agent_store import (
    AgentStore,
    AgentStoreConflict,
    AgentStoreError,
    AgentStoreInvalidState,
    AgentStoreNotFound,
)
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    canonical_json_hash,
    fragment_sort_key,
    knowledge_source_fingerprint,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreConflict,
    ContextStoreError,
    ContextStoreIntegrity,
    ContextStoreNotFound,
    StoredContextPack,
)
from zhimeng_runtime.database import ProjectBindingRecord
from zhimeng_runtime.distillation_adapter import (
    DistillationAdapterError,
    DistillationPreviewAdapter,
)
from zhimeng_runtime.distillation_models import DistillationCoverageRequest
from zhimeng_runtime.knowledge_service import (
    KnowledgeConflictError,
    KnowledgeContextSelection,
    KnowledgeService,
    KnowledgeServiceError,
)
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_kb_models import ProjectKbSnapshotManifest
from zhimeng_runtime.project_kb_service import (
    ProjectKbService,
    ProjectKbServiceError,
)
from zhimeng_runtime.project_reader import (
    ProjectChangedDuringReadError,
    ProjectFileNotFoundError,
    ProjectReader,
    ProjectReaderError,
    ProjectScanLimitError,
    UnreadableProjectFileError,
    UntrustedProjectPathError,
)


DEFAULT_MAX_CHARACTERS = 48_000
DEFAULT_MAX_TOKENS = 24_000
MAX_CONTEXT_CANDIDATES = 256
CORE_SECTION_INDEXES = frozenset({1, 2, 3, 4, 6, 7})
CORE_FILE_KINDS = frozenset(
    {"document", "control", "context", "status", "structured_state", "asset"}
)
CONTEXT_READ_KINDS = frozenset(
    {
        "prose",
        "document",
        "control",
        "review",
        "context",
        "status",
        "structured_state",
        "asset",
    }
)
CHAPTER_MATERIAL_PRIMARY_KINDS = frozenset(
    {"control", "document", "context", "structured_state"}
)
CHAPTER_MATERIAL_KIND_ORDER = {
    "control": 0,
    "document": 1,
    "context": 2,
    "structured_state": 3,
    "review": 4,
}


class ContextServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "context_unavailable",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContextNotFoundError(ContextServiceError):
    pass


class ContextConflictError(ContextServiceError):
    pass


class ContextUnavailableError(ContextServiceError):
    pass


class ContextService:
    def __init__(
        self,
        store: ContextStore,
        agent_store: AgentStore,
        reader: ProjectReader,
        locks: ProjectLockRegistry,
        distillation_adapter: DistillationPreviewAdapter | None = None,
        native_knowledge: KnowledgeService | None = None,
        project_kb_service: ProjectKbService | None = None,
        *,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if isinstance(max_characters, bool) or max_characters < 1:
            raise ValueError("max_characters must be a positive integer")
        if isinstance(max_tokens, bool) or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        self.store = store
        self.agent_store = agent_store
        self.reader = reader
        self.locks = locks
        self.distillation = distillation_adapter
        self.native_knowledge = native_knowledge
        self.project_kb_service = project_kb_service
        self.max_characters = max_characters
        self.max_tokens = max_tokens

    async def create_pack(
        self,
        session_id: str,
        request: ContextCreateRequest,
        *,
        idempotency_key: str,
    ) -> StoredContextPack:
        if not idempotency_key:
            raise ContextConflictError("Idempotency key must not be blank")
        context_pack_id = self._context_pack_id(session_id, idempotency_key)
        _, binding = await asyncio.to_thread(
            self._resolve_database_truth,
            session_id,
        )
        await asyncio.to_thread(
            self._require_project_kb_binding,
            binding.project_id,
            request,
        )
        replay = await asyncio.to_thread(
            self._matching_replay,
            context_pack_id,
            session_id,
            request,
        )
        if replay is not None:
            await asyncio.to_thread(
                self._require_project_kb_binding,
                binding.project_id,
                request,
            )
            return replay
        try:
            root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc

        (
            knowledge_candidates,
            knowledge_fingerprint,
            route_source,
            route_cards,
        ) = await asyncio.to_thread(
            self._knowledge_candidates,
            request,
            binding.project_id,
        )
        await asyncio.to_thread(
            self._require_project_kb_binding,
            binding.project_id,
            request,
            expected_knowledge_fingerprint=(
                knowledge_fingerprint
                if route_source is NlclRouteSource.NATIVE_KNOWLEDGE
                else None
            ),
        )

        async with self.locks.for_path(root):
            return await asyncio.to_thread(
                self._create_pack_locked,
                session_id,
                request,
                context_pack_id,
                root,
                knowledge_candidates,
                knowledge_fingerprint,
                route_source,
                route_cards,
            )

    def get_pack(self, context_pack_id: str) -> StoredContextPack:
        try:
            return self.store.get_pack(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_nlcl_route(self, context_pack_id: str):
        try:
            return self.store.get_nlcl_route(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_prewrite_operation_package(self, context_pack_id: str):
        try:
            return self.store.get_prewrite_operation_package(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_route_execution_state(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int = 1,
    ):
        try:
            return self.store.get_route_execution_state(
                context_pack_id,
                call_ordinal=call_ordinal,
            )
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def list_session_packs(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[StoredContextPack]:
        try:
            return self.store.list_session_packs(session_id, limit=limit)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_run_pack(self, run_id: str) -> StoredContextPack:
        try:
            return self.store.get_run_pack(run_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def _create_pack_locked(
        self,
        session_id: str,
        request: ContextCreateRequest,
        context_pack_id: str,
        locked_root: Path,
        knowledge_candidates: Sequence[ContextCandidateFragment],
        knowledge_fingerprint: str | None,
        route_source: NlclRouteSource,
        route_cards: Sequence[NlclSelectedCard],
    ) -> StoredContextPack:
        session, binding = self._resolve_database_truth(session_id)
        expected_native_fingerprint = (
            knowledge_fingerprint
            if route_source is NlclRouteSource.NATIVE_KNOWLEDGE
            else None
        )
        self._require_project_kb_binding(
            binding.project_id,
            request,
            expected_knowledge_fingerprint=expected_native_fingerprint,
        )
        try:
            current_root = self.reader.resolve(binding.root_path)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc
        if current_root != locked_root:
            raise ContextConflictError(
                "Project binding changed before Context compilation",
                retryable=True,
            )
        self._verify_request_revision(request, binding)

        try:
            initial = self.reader.inspect(locked_root)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc
        self._verify_snapshot(initial, binding, request)

        history_through_ordinal = 0
        history: Sequence[Any] = ()
        candidates: list[ContextCandidateFragment] = []
        pending_error: Exception | None = None
        try:
            history_through_ordinal, history = self.agent_store.completed_history(
                session_id
            )
            candidates.extend(self._project_candidates(initial, request))
            candidates.extend(knowledge_candidates)
        except Exception as exc:
            pending_error = exc

        final: Mapping[str, object] | None = None
        final_error: Exception | None = None
        try:
            final = self.reader.inspect(locked_root)
        except Exception as exc:
            final_error = exc

        if final_error is not None:
            if isinstance(final_error, ProjectReaderError):
                raise self._project_error(final_error) from final_error
            raise ContextUnavailableError(
                "Final project inspection failed during Context compilation",
                retryable=True,
            ) from final_error
        if final is None:  # pragma: no cover - guarded by the branch above.
            raise ContextUnavailableError("Final project inspection produced no snapshot")
        self._verify_final_snapshot(initial, final)

        if pending_error is not None:
            if isinstance(pending_error, ContextServiceError):
                raise pending_error
            if isinstance(pending_error, ProjectReaderError):
                raise self._project_error(pending_error) from pending_error
            if isinstance(pending_error, AgentStoreError):
                raise self._agent_error(pending_error) from pending_error
            raise ContextUnavailableError(
                "Context candidates could not be assembled",
                retryable=True,
            ) from pending_error

        candidates = self._bounded_candidates(candidates)
        try:
            self._require_project_kb_binding(
                binding.project_id,
                request,
                expected_knowledge_fingerprint=expected_native_fingerprint,
            )
            pack = compile_context_pack(
                context_pack_id=context_pack_id,
                session_id=session_id,
                project_id=session.project_revision.project_id,
                history_through_ordinal=history_through_ordinal,
                request=request,
                history=history,
                candidates=candidates,
                max_characters=self.max_characters,
                max_tokens=self.max_tokens,
                max_fragments=MAX_CONTEXT_CANDIDATES,
                created_at=datetime.now(UTC),
                knowledge_fingerprint=knowledge_fingerprint,
            )
            self._require_project_kb_binding(
                binding.project_id,
                request,
                expected_knowledge_fingerprint=expected_native_fingerprint,
            )
            route_receipt = build_nlcl_route_receipt(
                pack,
                source=route_source,
                route_cards=tuple(route_cards),
            )
            return self.store.create_pack(pack, nlcl_route=route_receipt)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextUnavailableError(
                "Context compiler rejected the assembled inputs"
            ) from exc

    def _matching_replay(
        self,
        context_pack_id: str,
        session_id: str,
        request: ContextCreateRequest,
    ) -> StoredContextPack | None:
        try:
            stored = self.store.get_pack(context_pack_id)
        except ContextStoreNotFound:
            return None
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc
        pack = stored.pack
        if (
            pack.session_id == session_id
            and pack.objective == request.objective
            and pack.profile == request.profile
            and pack.active_chapter_id == request.active_chapter_id
            and pack.project_revision == request.expected_project_revision
            and pack.project_fingerprint == request.expected_project_fingerprint
            and pack.project_kb_state_id == request.project_kb_state_id
            and pack.project_kb_manifest_hash == request.project_kb_manifest_hash
            and pack.formal_knowledge_selector == request.formal_knowledge_selector
            and pack.manual_file_ids == request.manual_file_ids
        ):
            return stored
        return None

    def _require_project_kb_binding(
        self,
        project_id: str,
        request: ContextCreateRequest,
        *,
        expected_knowledge_fingerprint: str | None = None,
    ) -> ProjectKbSnapshotManifest:
        service = self.project_kb_service
        if service is None:
            raise ContextUnavailableError(
                "Project KB verification is not configured",
                code="project_kb_unavailable",
                retryable=True,
            )
        try:
            return service.require_current_binding(
                project_id,
                project_kb_state_id=request.project_kb_state_id,
                project_kb_manifest_hash=request.project_kb_manifest_hash,
                expected_knowledge_fingerprint=expected_knowledge_fingerprint,
            )
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            code = str(detail.get("code") or "project_kb_unavailable")
            raise ContextConflictError(
                str(exc),
                code=code,
                retryable=exc.retryable,
            ) from exc

    @staticmethod
    def _bounded_candidates(
        candidates: Sequence[ContextCandidateFragment],
    ) -> list[ContextCandidateFragment]:
        ordered = sorted(candidates, key=fragment_sort_key)
        if len(ordered) <= MAX_CONTEXT_CANDIDATES:
            return ordered

        retained = ordered[: MAX_CONTEXT_CANDIDATES - 1]
        omitted = ordered[MAX_CONTEXT_CANDIDATES - 1 :]
        omitted_mandatory = any(item.mandatory for item in omitted)
        reason = (
            "mandatory_candidate_limit_exceeded"
            if omitted_mandatory
            else "candidate_limit_exceeded"
        )
        retained.append(
            ContextCandidateFragment(
                source_id="context:candidate-limit",
                source_type="context_selection_limit",
                title=f"{len(omitted)} context candidates omitted by the hard limit",
                authority=(
                    ContextAuthority.PROJECT_TRUTH
                    if omitted_mandatory
                    else ContextAuthority.REFERENCE
                ),
                version="context-selection-v1",
                source_hash=canonical_json_hash(
                    [
                        {
                            "source_id": item.source_id,
                            "source_hash": item.source_hash,
                            "mandatory": item.mandatory,
                        }
                        for item in omitted
                    ]
                ),
                content="Additional Context candidates were omitted by the bounded compiler input.",
                priority=0,
                mandatory=omitted_mandatory,
                eligible=False,
                exclusion_reason=reason,
            )
        )
        return retained

    def _resolve_database_truth(
        self,
        session_id: str,
    ) -> tuple[Any, ProjectBindingRecord]:
        try:
            session = self.agent_store.get_session(session_id)
        except AgentStoreError as exc:
            raise self._agent_error(exc) from exc
        project_id = session.project_revision.project_id
        binding = self.agent_store.database.get_project_binding(project_id)
        if binding is None:
            raise ContextNotFoundError("Context project binding was not found")
        return session, binding

    @staticmethod
    def _verify_request_revision(
        request: ContextCreateRequest,
        binding: ProjectBindingRecord,
    ) -> None:
        if (
            request.expected_project_revision != binding.revision
            or request.expected_project_fingerprint != binding.fingerprint
        ):
            raise ContextConflictError(
                "Project revision changed; refresh before compiling Context",
                retryable=True,
            )

    @staticmethod
    def _snapshot_project(snapshot: Mapping[str, object]) -> Mapping[str, object]:
        project = snapshot.get("project")
        if not isinstance(project, Mapping):
            raise ContextUnavailableError("Project Reader returned an invalid snapshot")
        return project

    def _verify_snapshot(
        self,
        snapshot: Mapping[str, object],
        binding: ProjectBindingRecord,
        request: ContextCreateRequest,
    ) -> None:
        project = self._snapshot_project(snapshot)
        if project.get("project_id") != binding.project_id:
            raise ContextConflictError(
                "Inspected project identity does not match the session binding"
            )
        if project.get("fingerprint") != request.expected_project_fingerprint:
            raise ContextConflictError(
                "Project files changed after the requested revision",
                retryable=True,
            )

    def _verify_final_snapshot(
        self,
        initial: Mapping[str, object],
        final: Mapping[str, object],
    ) -> None:
        initial_project = self._snapshot_project(initial)
        final_project = self._snapshot_project(final)
        if (
            final_project.get("project_id") != initial_project.get("project_id")
            or final_project.get("fingerprint") != initial_project.get("fingerprint")
        ):
            raise ContextConflictError(
                "Project snapshot changed while Context was being compiled",
                retryable=True,
            )

    def _project_candidates(
        self,
        snapshot: Mapping[str, object],
        request: ContextCreateRequest,
    ) -> list[ContextCandidateFragment]:
        raw_files = snapshot.get("files")
        raw_chapters = snapshot.get("chapters")
        if not isinstance(raw_files, list) or not isinstance(raw_chapters, list):
            raise ContextUnavailableError("Project snapshot is missing files or chapters")
        files = [item for item in raw_files if isinstance(item, Mapping)]
        chapters = [item for item in raw_chapters if isinstance(item, Mapping)]
        files_by_id = {
            str(item["file_id"]): item
            for item in files
            if isinstance(item.get("file_id"), str)
        }
        candidates: list[ContextCandidateFragment] = []
        selected_file_ids: set[str] = set()

        for file_id in request.manual_file_ids:
            record = files_by_id.get(file_id)
            if record is None:
                raise ContextConflictError(
                    "A manually selected file is no longer in the project snapshot",
                    retryable=True,
                )
            if record.get("kind") not in CONTEXT_READ_KINDS:
                raise ContextConflictError(
                    "A manually selected file cannot be used as writing context"
                )
            selected_file_ids.add(file_id)
            candidates.append(
                self._read_project_candidate(
                    snapshot,
                    record,
                    source_type="manual_project_file",
                    priority=995,
                    mandatory=True,
                    project_revision=request.expected_project_revision,
                )
            )

        if request.active_chapter_id is not None:
            active_index = next(
                (
                    index
                    for index, chapter in enumerate(chapters)
                    if chapter.get("chapter_id") == request.active_chapter_id
                ),
                None,
            )
            if active_index is None:
                candidates.append(
                    self._unavailable_chapter_candidate(
                        request,
                        request.active_chapter_id,
                        mandatory=True,
                        reason="active_chapter_not_found",
                        priority=1_000,
                    )
                )
            else:
                chapter_targets = [
                    (chapters[active_index], True, 1_000, "active_chapter"),
                    *[
                        (chapters[index], False, priority, "previous_chapter")
                        for index, priority in zip(
                            range(active_index - 1, max(-1, active_index - 3), -1),
                            (950, 900),
                            strict=False,
                        )
                    ],
                ]
                for chapter, mandatory, priority, source_type in chapter_targets:
                    chapter_id = str(chapter.get("chapter_id", "unknown"))
                    prose_ids = chapter.get("prose_file_ids")
                    if not isinstance(prose_ids, list) or not prose_ids:
                        materials = self._chapter_material_candidates(
                            snapshot,
                            chapter,
                            files_by_id,
                            source_type=f"{source_type}_material",
                            priority=priority,
                            mandatory=mandatory,
                            project_revision=request.expected_project_revision,
                            selected_file_ids=selected_file_ids,
                        )
                        candidates.extend(materials)
                        if not any(item.eligible for item in materials):
                            candidates.append(
                                self._unavailable_chapter_candidate(
                                    request,
                                    chapter_id,
                                    mandatory=mandatory,
                                    reason=(
                                        "active_chapter_material_not_found"
                                        if mandatory
                                        else "previous_chapter_material_not_found"
                                    ),
                                    priority=priority,
                                )
                            )
                        continue
                    for raw_file_id in prose_ids:
                        if (
                            not isinstance(raw_file_id, str)
                            or raw_file_id in selected_file_ids
                        ):
                            continue
                        record = files_by_id.get(raw_file_id)
                        if record is None:
                            raise ContextConflictError(
                                "Chapter references a file outside its project snapshot",
                                retryable=True,
                            )
                        selected_file_ids.add(raw_file_id)
                        candidates.append(
                            self._read_project_candidate(
                                snapshot,
                                record,
                                source_type=source_type,
                                priority=priority,
                                mandatory=mandatory,
                                project_revision=request.expected_project_revision,
                            )
                        )

        section_priorities = {1: 760, 2: 770, 3: 800, 4: 790, 6: 750, 7: 720}
        for record in files:
            file_id = record.get("file_id")
            section_index = record.get("section_index")
            kind = record.get("kind")
            if (
                not isinstance(file_id, str)
                or file_id in selected_file_ids
                or section_index not in CORE_SECTION_INDEXES
                or kind not in CORE_FILE_KINDS
            ):
                continue
            candidates.append(
                self._read_project_candidate(
                    snapshot,
                    record,
                    source_type="project_section",
                    priority=section_priorities[int(section_index)],
                    mandatory=False,
                    project_revision=request.expected_project_revision,
                )
            )
        return candidates

    def _chapter_material_candidates(
        self,
        snapshot: Mapping[str, object],
        chapter: Mapping[str, object],
        files_by_id: Mapping[str, Mapping[str, object]],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        project_revision: int,
        selected_file_ids: set[str],
    ) -> list[ContextCandidateFragment]:
        raw_file_ids = chapter.get("file_ids")
        if not isinstance(raw_file_ids, list):
            return []

        records: list[Mapping[str, object]] = []
        seen_chapter_ids: set[str] = set()
        for raw_file_id in raw_file_ids:
            if (
                not isinstance(raw_file_id, str)
                or raw_file_id in seen_chapter_ids
                or raw_file_id in selected_file_ids
            ):
                continue
            seen_chapter_ids.add(raw_file_id)
            record = files_by_id.get(raw_file_id)
            if record is None:
                raise ContextConflictError(
                    "Chapter references a file outside its project snapshot",
                    retryable=True,
                )
            if record.get("kind") in CHAPTER_MATERIAL_KIND_ORDER:
                records.append(record)

        records.sort(
            key=lambda item: (
                CHAPTER_MATERIAL_KIND_ORDER[str(item.get("kind"))],
                str(item.get("relative_path", "")).casefold(),
                str(item.get("file_id", "")),
            )
        )
        primary_records = [
            item
            for item in records
            if item.get("kind") in CHAPTER_MATERIAL_PRIMARY_KINDS
        ]
        review_records = [item for item in records if item.get("kind") == "review"]

        candidates: list[ContextCandidateFragment] = []

        def read_records(items: Sequence[Mapping[str, object]]) -> None:
            for record in items:
                file_id = str(record["file_id"])
                if file_id in selected_file_ids:
                    continue
                selected_file_ids.add(file_id)
                candidates.append(
                    self._read_project_candidate(
                        snapshot,
                        record,
                        source_type=source_type,
                        priority=priority,
                        mandatory=False,
                        project_revision=project_revision,
                    )
                )

        read_records(primary_records)
        if not any(item.eligible for item in candidates):
            read_records(review_records)

        if mandatory:
            primary_index = next(
                (index for index, item in enumerate(candidates) if item.eligible),
                None,
            )
            if primary_index is not None:
                values = candidates[primary_index].model_dump(mode="python")
                values["mandatory"] = True
                candidates[primary_index] = ContextCandidateFragment.model_validate(
                    values
                )
        return candidates

    def _read_project_candidate(
        self,
        snapshot: Mapping[str, object],
        record: Mapping[str, object],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        project_revision: int,
    ) -> ContextCandidateFragment:
        file_id = record.get("file_id")
        relative_path = record.get("relative_path")
        source_hash = record.get("sha256")
        if (
            not isinstance(file_id, str)
            or not isinstance(relative_path, str)
            or not isinstance(source_hash, str)
        ):
            raise ContextUnavailableError("Project snapshot contains an invalid file record")
        try:
            payload = self.reader.read_text_file_from_snapshot(
                snapshot,
                file_id,
                allowed_kinds=CONTEXT_READ_KINDS,
            )
            content = payload.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._unavailable_file_candidate(
                    record,
                    source_type=source_type,
                    priority=priority,
                    mandatory=mandatory,
                    reason="project_file_blank",
                )
            return ContextCandidateFragment(
                source_id=f"project-file:{file_id}",
                source_type=source_type,
                title=relative_path[:500],
                authority=ContextAuthority.PROJECT_TRUTH,
                version=f"project-revision-{project_revision}",
                source_hash=source_hash,
                content=content,
                priority=priority,
                mandatory=mandatory,
                eligible=True,
                exclusion_reason=None,
            )
        except (UnreadableProjectFileError, ProjectScanLimitError):
            return self._unavailable_file_candidate(
                record,
                source_type=source_type,
                priority=priority,
                mandatory=mandatory,
                reason="project_file_unreadable",
            )

    def _unavailable_file_candidate(
        self,
        record: Mapping[str, object],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        reason: str,
    ) -> ContextCandidateFragment:
        file_id = str(record.get("file_id", "unknown"))
        relative_path = str(record.get("relative_path", file_id))
        source_hash = str(record.get("sha256", ""))
        if len(source_hash) != 64:
            source_hash = canonical_json_hash(
                {"file_id": file_id, "relative_path": relative_path, "reason": reason}
            )
        return ContextCandidateFragment(
            source_id=f"project-file:{file_id}",
            source_type=source_type,
            title=relative_path[:500],
            authority=ContextAuthority.PROJECT_TRUTH,
            version="project-file-unavailable",
            source_hash=source_hash,
            content="Project file was unavailable for this Context Pack.",
            priority=priority,
            mandatory=mandatory,
            eligible=False,
            exclusion_reason=reason,
        )

    @staticmethod
    def _unavailable_chapter_candidate(
        request: ContextCreateRequest,
        chapter_id: str,
        *,
        mandatory: bool,
        reason: str,
        priority: int,
    ) -> ContextCandidateFragment:
        return ContextCandidateFragment(
            source_id=f"project-chapter:{chapter_id}",
            source_type="active_chapter" if mandatory else "previous_chapter",
            title=chapter_id[:500],
            authority=ContextAuthority.PROJECT_TRUTH,
            version=f"project-revision-{request.expected_project_revision}",
            source_hash=canonical_json_hash(
                {
                    "chapter_id": chapter_id,
                    "project_fingerprint": request.expected_project_fingerprint,
                    "reason": reason,
                }
            ),
            content="Requested chapter prose was unavailable for this Context Pack.",
            priority=priority,
            mandatory=mandatory,
            eligible=False,
            exclusion_reason=reason,
        )

    def _knowledge_candidates(
        self,
        request: ContextCreateRequest,
        project_id: str | None = None,
    ) -> tuple[
        list[ContextCandidateFragment],
        str | None,
        NlclRouteSource,
        tuple[NlclSelectedCard, ...],
    ]:
        if request.formal_knowledge_selector is not None:
            if self.native_knowledge is None:
                raise ContextUnavailableError(
                    "Native knowledge service is required for an explicit formal batch"
                )
            if project_id is None:
                raise ContextConflictError(
                    "Explicit formal knowledge requires a bound project"
                )
            try:
                selection = self.native_knowledge.select_formalized_batch_for_context(
                    request.formal_knowledge_selector,
                    project_id=project_id,
                )
            except KnowledgeConflictError as exc:
                raise ContextConflictError(str(exc)) from exc
            except KnowledgeServiceError as exc:
                raise ContextUnavailableError(
                    "Explicit formal knowledge could not be verified",
                    retryable=exc.retryable,
                ) from exc
            candidates, route_cards = self._native_selection_material(
                selection,
                mandatory=True,
            )
            return (
                candidates,
                selection.fingerprint,
                NlclRouteSource.NATIVE_KNOWLEDGE,
                route_cards,
            )

        if self.native_knowledge is not None:
            try:
                if self.native_knowledge.has_active_cards(project_id=project_id):
                    query = " ".join(request.objective.split())[:1000]
                    selection = self.native_knowledge.select_for_context(
                        query,
                        request.profile,
                        project_id=project_id,
                    )
                    candidates, route_cards = self._native_selection_material(
                        selection,
                        mandatory=False,
                    )
                    if not candidates:
                        candidates.append(
                            self._unavailable_knowledge_candidate(
                                "native_knowledge_no_matching_cards",
                                version=selection.fingerprint,
                            )
                        )
                    return (
                        candidates,
                        selection.fingerprint,
                        NlclRouteSource.NATIVE_KNOWLEDGE,
                        route_cards,
                    )
            except KnowledgeServiceError:
                return [
                    self._unavailable_knowledge_candidate(
                        "native_knowledge_unavailable"
                    )
                ], None, NlclRouteSource.UNAVAILABLE, ()

        if self.distillation is None:
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )

        try:
            query = " ".join(request.objective.split())[:1000]
            coverage_request = DistillationCoverageRequest(
                query=query,
                profile=request.profile,
            )
            coverage = self.distillation.coverage(coverage_request)
        except (DistillationAdapterError, ValidationError, TypeError, ValueError):
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )
        except Exception:
            # Optional knowledge must fail closed into an excluded fragment, not block
            # otherwise valid project context.
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )

        knowledge_fingerprint = knowledge_source_fingerprint(
            library_fingerprint=coverage.library_fingerprint,
            tool_fingerprint=coverage.tool_fingerprint,
        )
        candidates: list[ContextCandidateFragment] = []
        route_cards: list[NlclSelectedCard] = []
        for card in coverage.selected_cards:
            eligible = bool(
                coverage.formal_injection_allowed and card.hard_rule_eligible
            )
            if card.hard_rule_eligible:
                authority = ContextAuthority.FORMAL_KNOWLEDGE
                exclusion_reason = None if eligible else "formal_injection_not_allowed"
            else:
                authority = ContextAuthority.RISK
                exclusion_reason = "knowledge_card_risk_only"
            content = card.summary if card.summary.strip() else card.title
            candidates.append(
                ContextCandidateFragment(
                    source_id=f"knowledge-card:{card.card_id}",
                    source_type=f"distillation_card:{card.card_type}",
                    title=card.title,
                    authority=authority,
                    version=coverage.library_fingerprint,
                    source_hash=canonical_json_hash(card.model_dump(mode="json")),
                    content=content,
                    priority=600,
                    mandatory=False,
                    eligible=eligible,
                    exclusion_reason=exclusion_reason,
                )
            )
            if eligible:
                route_cards.append(
                    NlclSelectedCard(
                        card_id=card.card_id,
                        card_type=str(card.card_type),
                        title=card.title,
                        version_id=coverage.library_fingerprint,
                        content_hash=canonical_json_hash(card.model_dump(mode="json")),
                        route_terms=tuple(dict.fromkeys(card.tags)),
                        recommended_profiles=(request.profile,),
                        negative_routes=(),
                        safety_summary=(
                            "External distillation card passed formal-injection coverage."
                        ),
                        feedback_layer="external_distillation_review",
                    )
                )
        if not candidates:
            candidates.append(
                self._unavailable_knowledge_candidate(
                    "distillation_no_matching_cards",
                    version=knowledge_fingerprint,
                )
            )
        return (
            candidates,
            knowledge_fingerprint,
            NlclRouteSource.EXTERNAL_DISTILLATION,
            tuple(route_cards),
        )

    @staticmethod
    def _native_selection_material(
        selection: KnowledgeContextSelection,
        *,
        mandatory: bool,
    ) -> tuple[list[ContextCandidateFragment], tuple[NlclSelectedCard, ...]]:
        candidates = [
            ContextCandidateFragment(
                source_id=f"knowledge-card:{card.card_id}",
                source_type=f"native_knowledge_card:{card.card_type}",
                title=card.title,
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                version=card.version_id,
                source_hash=card.content_hash,
                content=card.content,
                priority=950 if mandatory else min(900, 600 + card.score),
                mandatory=mandatory,
                eligible=True,
                exclusion_reason=None,
                knowledge_provenance=card.provenance,
            )
            for card in selection.cards
        ]
        route_cards = tuple(
            NlclSelectedCard(
                card_id=card.card_id,
                card_type=card.card_type,
                title=card.title,
                version_id=card.version_id,
                content_hash=card.content_hash,
                route_terms=card.route_terms,
                recommended_profiles=card.recommended_profiles,
                negative_routes=card.negative_routes,
                safety_summary=card.safety_summary,
                feedback_layer=card.feedback_layer,
                knowledge_provenance=card.provenance,
            )
            for card in selection.cards
        )
        return candidates, route_cards

    @staticmethod
    def _unavailable_knowledge_candidate(
        reason: str,
        *,
        version: str = "unavailable",
    ) -> ContextCandidateFragment:
        return ContextCandidateFragment(
            source_id="knowledge:distillation",
            source_type="distillation_coverage",
            title="Distillation knowledge coverage",
            authority=ContextAuthority.FORMAL_KNOWLEDGE,
            version=version,
            source_hash=canonical_json_hash({"reason": reason, "version": version}),
            content="Formal distillation knowledge was unavailable for this Context Pack.",
            priority=500,
            mandatory=False,
            eligible=False,
            exclusion_reason=reason,
        )

    @staticmethod
    def _context_pack_id(session_id: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            f"{session_id}\x00{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return f"context-pack-{digest[:32]}"

    @staticmethod
    def _agent_error(exc: AgentStoreError) -> ContextServiceError:
        if isinstance(exc, AgentStoreNotFound):
            return ContextNotFoundError(str(exc))
        if isinstance(exc, (AgentStoreConflict, AgentStoreInvalidState)):
            return ContextConflictError(str(exc), retryable=True)
        return ContextUnavailableError(str(exc), retryable=True)

    @staticmethod
    def _store_error(exc: ContextStoreError) -> ContextServiceError:
        if isinstance(exc, ContextStoreNotFound):
            return ContextNotFoundError(str(exc))
        if isinstance(exc, ContextStoreConflict):
            return ContextConflictError(str(exc))
        if isinstance(exc, ContextStoreIntegrity):
            return ContextUnavailableError(str(exc))
        return ContextUnavailableError(str(exc), retryable=True)

    @staticmethod
    def _project_error(exc: ProjectReaderError) -> ContextServiceError:
        if isinstance(exc, ProjectChangedDuringReadError):
            message = "Project changed during Context compilation"
            return ContextConflictError(message, retryable=True)
        if isinstance(exc, ProjectFileNotFoundError):
            message = "A project file disappeared during Context compilation"
            return ContextConflictError(message, retryable=True)
        if isinstance(exc, UntrustedProjectPathError):
            message = "Project trust boundary rejected Context compilation"
            return ContextConflictError(message, retryable=False)
        return ContextUnavailableError(
            "Project could not be read for Context compilation",
            retryable=True,
        )


__all__ = [
    "ContextConflictError",
    "ContextNotFoundError",
    "ContextService",
    "ContextServiceError",
    "ContextUnavailableError",
    "DEFAULT_MAX_CHARACTERS",
    "DEFAULT_MAX_TOKENS",
    "MAX_CONTEXT_CANDIDATES",
]
