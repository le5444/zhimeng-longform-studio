from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from zhimeng_runtime.context_models import ContextAuthority, canonical_json_hash
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.knowledge_models import KnowledgeCard, KnowledgeCardStatus
from zhimeng_runtime.source_import_models import SourceImportStatus, parse_source_import_job
from zhimeng_runtime.project_reader import (
    PUBLIC_TEXT_FILE_KINDS,
    ProjectReader,
    ProjectReaderError,
)
from zhimeng_runtime.search_models import (
    SearchHit,
    SearchIndexDocument,
    SearchIndexManifest,
    SearchIndexSnapshot,
    SearchQueryRequest,
    SearchQueryResponse,
    SearchRebuildRequest,
    SearchRebuildResult,
    SearchSourceType,
    search_index_hash,
)


BUILDER_ID = "zhimeng.project-search"
BUILDER_VERSION = "1"
INDEX_POLICY_VERSION = "exclude-non-truth-v1"
INDEX_FILENAME = "index.json"
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_SNIPPET_CHARS = 240
TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
PROJECT_ID_PATTERN = re.compile(r"^project-[0-9a-f]{24}$")
REPARSE_POINT_ATTRIBUTE = 0x400
NON_TRUTH_PATH_MARKER = "勿作为真值"


class KnowledgeCardSource(Protocol):
    def list_cards(
        self,
        *,
        project_id: str | None = None,
        scope_id: str | None = None,
    ) -> Any: ...

    def observe_knowledge_fingerprint(self, project_id: str | None = None) -> str: ...


class AuthorizedSourceRecord:
    """Indexable authorized source packet snapshot for project search."""

    __slots__ = (
        "source_id",
        "title",
        "version",
        "source_hash",
        "content",
        "updated_at",
    )

    def __init__(
        self,
        *,
        source_id: str,
        title: str,
        version: str,
        source_hash: str,
        content: str,
        updated_at: datetime | None = None,
    ) -> None:
        self.source_id = source_id
        self.title = title
        self.version = version
        self.source_hash = source_hash
        self.content = content
        self.updated_at = updated_at


class AuthorizedSourceCatalog(Protocol):
    def list_authorized_sources(self, project_id: str) -> list[AuthorizedSourceRecord]: ...

    def observe_authorized_source_fingerprint(self, project_id: str) -> str: ...


AUTHORIZED_SOURCE_STATUSES = frozenset(
    {
        SourceImportStatus.SOURCE_READY,
        SourceImportStatus.DRAFTING,
        SourceImportStatus.AWAITING_REVIEW,
        SourceImportStatus.FORMALIZING,
        SourceImportStatus.COMPLETED,
    }
)
MAX_AUTHORIZED_SOURCE_CHARS = 8 * 1024 * 1024


class BoundAuthorizedSourceCatalog:
    """Project-bound source packets from knowledge_source_scope + source_import_job."""

    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    def list_authorized_sources(self, project_id: str) -> list[AuthorizedSourceRecord]:
        records: list[AuthorizedSourceRecord] = []
        for job_payload in self._list_bound_jobs(project_id):
            record = self._record_from_job(job_payload)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.source_id)
        return records

    def observe_authorized_source_fingerprint(self, project_id: str) -> str:
        material = []
        for job_payload in self._list_bound_jobs(project_id):
            import_id = str(
                job_payload.get("import_id") or job_payload.get("id") or ""
            ).strip()
            status = str(job_payload.get("status") or "").strip()
            packet = job_payload.get("source_packet")
            source_hash = self._packet_text_hash(packet if isinstance(packet, dict) else None)
            if not import_id or source_hash is None:
                continue
            if status not in {item.value for item in AUTHORIZED_SOURCE_STATUSES}:
                continue
            material.append(
                {
                    "source_import_id": import_id,
                    "status": status,
                    "source_hash": source_hash,
                }
            )
        material.sort(key=lambda item: item["source_import_id"])
        return canonical_json_hash(
            {
                "schema": "zhimeng.project-search-authorized-source-fingerprint.v1",
                "project_id": project_id,
                "sources": material,
            }
        )

    def _list_bound_jobs(self, project_id: str) -> list[dict[str, object]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT job.*
                FROM knowledge_source_scope AS source_scope
                JOIN knowledge_scope AS scope
                  ON scope.id = source_scope.scope_id
                JOIN source_import_job AS job
                  ON job.id = source_scope.source_import_id
                WHERE scope.scope_type = 'project'
                  AND scope.project_binding_id = ?
                ORDER BY job.id ASC
                """,
                (project_id,),
            ).fetchall()
        return [
            self.database._source_import_job_payload_from_row(row) for row in rows
        ]

    def _record_from_job(self, job_payload: dict[str, object]) -> AuthorizedSourceRecord | None:
        try:
            job = parse_source_import_job(job_payload)
        except Exception:
            return None
        if job.status not in AUTHORIZED_SOURCE_STATUSES:
            return None
        packet = job.source_packet
        if packet is None:
            return None
        packet_data = packet.model_dump(mode="json")
        text_path_raw = str(packet_data.get("text_path") or "").strip()
        packet_dir_raw = str(packet_data.get("packet_dir") or "").strip()
        import_id = str(job.import_id)
        title = str(packet_data.get("title") or job.query or import_id).strip() or import_id
        source_hash = self._packet_text_hash(packet_data)
        if not text_path_raw or not packet_dir_raw or source_hash is None:
            return None
        try:
            packet_dir = Path(packet_dir_raw).expanduser().resolve(strict=False)
            text_path = Path(text_path_raw).expanduser().resolve(strict=False)
        except OSError:
            return None
        if _is_link_or_junction(packet_dir) or _is_link_or_junction(text_path):
            return None
        try:
            text_path.relative_to(packet_dir)
        except ValueError:
            return None
        if not text_path.is_file():
            return None
        try:
            content = text_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        if len(content) > MAX_AUTHORIZED_SOURCE_CHARS:
            content = content[:MAX_AUTHORIZED_SOURCE_CHARS]
        return AuthorizedSourceRecord(
            source_id=import_id,
            title=title[:1024],
            version=f"status-{job.status.value}",
            source_hash=source_hash,
            content=content,
            updated_at=job.updated_at,
        )

    @staticmethod
    def _packet_text_hash(packet: dict[str, object] | None) -> str | None:
        if not isinstance(packet, dict):
            return None
        direct = packet.get("text_sha256")
        if isinstance(direct, str) and len(direct) == 64:
            return direct
        artifacts = packet.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    continue
                kind = str(artifact.get("kind") or "")
                digest = artifact.get("sha256")
                if kind in {"packet_text", "text"} and isinstance(digest, str) and len(digest) == 64:
                    return digest
        return None


class SearchServiceError(RuntimeError):
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


class SearchNotFoundError(SearchServiceError):
    pass


class SearchConflictError(SearchServiceError):
    pass


class SearchInvalidError(SearchServiceError):
    pass


class SearchUnavailableError(SearchServiceError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return True
    try:
        if os.name == "nt":
            attrs = getattr(path.stat(), "st_file_attributes", 0)
            return bool(attrs & REPARSE_POINT_ATTRIBUTE)
    except OSError:
        return True
    return False


def _tokenize_query(query: str) -> list[str]:
    tokens = [match.group(0).casefold() for match in TOKEN_PATTERN.finditer(query)]
    unique: list[str] = []
    for token in tokens:
        if token and token not in unique:
            unique.append(token)
    if not unique:
        collapsed = " ".join(query.split()).casefold()
        if collapsed:
            unique.append(collapsed)
    return unique


def _is_non_truth_project_path(relative_path: str) -> bool:
    return any(
        NON_TRUTH_PATH_MARKER in part
        for part in relative_path.replace("\\", "/").split("/")
    )


def _card_title(card_payload: dict[str, Any], card_id: str) -> str:
    title = str(card_payload.get("title") or "").strip()
    if title:
        return title[:1024]
    return card_id


def _stringify_card_value(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 4 or value is None:
        return []
    if isinstance(value, str):
        text = " ".join(value.split())
        return [text] if text else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        parts: list[str] = []
        for item in value[:64]:
            parts.extend(_stringify_card_value(item, depth=depth + 1))
        return parts
    if isinstance(value, dict):
        parts = []
        for key in sorted(value):
            if str(key) in {"schema", "source"}:
                continue
            parts.extend(_stringify_card_value(value[key], depth=depth + 1))
        return parts
    return []


def _card_search_text(card_payload: dict[str, Any]) -> str:
    preferred_keys = (
        "title",
        "summary",
        "reader_promise",
        "keywords",
        "tags",
        "applicable_tasks",
        "promise",
        "notes",
    )
    chunks: list[str] = []
    for key in preferred_keys:
        if key in card_payload:
            chunks.extend(_stringify_card_value(card_payload.get(key)))
    for key, value in sorted(card_payload.items()):
        if key in preferred_keys or key in {"schema", "source", "card_id"}:
            continue
        chunks.extend(_stringify_card_value(value))
    text = "\n".join(chunk for chunk in chunks if chunk)
    return text[: 8 * 1024 * 1024]


def _snippet_for(content: str, query: str, tokens: list[str]) -> str:
    haystack = content.replace("\r\n", "\n").replace("\r", "\n")
    lowered = haystack.casefold()
    needle = query.casefold()
    index = lowered.find(needle) if needle else -1
    matched_token = needle if index >= 0 else ""
    if index < 0:
        for token in tokens:
            index = lowered.find(token)
            if index >= 0:
                matched_token = token
                break
    if index < 0:
        compact = " ".join(haystack.split())
        return compact[:MAX_SNIPPET_CHARS]
    start = max(0, index - 48)
    end = min(len(haystack), index + max(len(matched_token), 1) + 160)
    snippet = haystack[start:end]
    snippet = " ".join(snippet.split())
    if start > 0:
        snippet = "..." + snippet
    if end < len(haystack):
        snippet = snippet + "..."
    return snippet[:MAX_SNIPPET_CHARS]


def _score_document(
    document: SearchIndexDocument,
    *,
    query: str,
    tokens: list[str],
) -> tuple[float, str] | None:
    title = document.title.casefold()
    path = (document.relative_path or "").casefold()
    content = document.content.casefold()
    query_cf = query.casefold()
    score = 0.0
    reasons: list[str] = []

    if query_cf and query_cf in title:
        score += 12.0
        reasons.append("title_match")
    if query_cf and path and query_cf in path:
        score += 8.0
        reasons.append("path_match")
    if query_cf and query_cf in content:
        score += 6.0
        reasons.append("content_match")

    matched_tokens = 0
    for token in tokens:
        token_score = 0.0
        if token in title:
            token_score += 4.0
        if path and token in path:
            token_score += 2.0
        if token in content:
            token_score += 1.0 + min(content.count(token), 5) * 0.15
        if token_score > 0:
            matched_tokens += 1
            score += token_score
            if f"token:{token}" not in reasons and len(reasons) < 6:
                reasons.append(f"token:{token}")

    if score <= 0:
        return None
    if matched_tokens == len(tokens) and tokens:
        score += 1.5
        reasons.append("all_tokens")
    if document.authority is ContextAuthority.PROJECT_TRUTH:
        score += 0.25
    elif document.authority is ContextAuthority.FORMAL_KNOWLEDGE:
        score += 0.5
    reason = ",".join(reasons) if reasons else "match"
    return score, reason[:512]


class SearchService:
    """Rebuildable structured project/formal-knowledge search index."""

    def __init__(
        self,
        database: SqliteStore,
        project_reader: ProjectReader,
        knowledge_service: KnowledgeCardSource,
        state_root: Path,
        authorized_source_catalog: AuthorizedSourceCatalog | None = None,
    ) -> None:
        requested_root = state_root.expanduser()
        if requested_root.exists() and _is_link_or_junction(requested_root):
            raise ValueError("project search state root cannot be a link or junction")
        self.database = database
        self.project_reader = project_reader
        self.knowledge_service = knowledge_service
        self.authorized_source_catalog = (
            authorized_source_catalog
            if authorized_source_catalog is not None
            else BoundAuthorizedSourceCatalog(database)
        )
        self.state_root = requested_root.resolve(strict=False)
        self._locks_guard = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}

    def rebuild(
        self,
        project_id: str,
        request: SearchRebuildRequest | None = None,
    ) -> SearchRebuildResult:
        request = request or SearchRebuildRequest()
        project_id = self._validate_project_id(project_id)
        with self._project_lock(project_id):
            existing = self._read_snapshot(project_id)
            (
                source_fingerprint,
                project_revision,
                project_fingerprint,
                knowledge_fingerprint,
            ) = self._observe_source_fingerprint(project_id)
            if (
                existing is not None
                and not request.force
                and existing.manifest.source_fingerprint == source_fingerprint
                and existing.manifest.project_revision == project_revision
                and existing.manifest.project_fingerprint == project_fingerprint
                and existing.manifest.knowledge_fingerprint == knowledge_fingerprint
            ):
                return SearchRebuildResult(
                    project_id=project_id,
                    rebuilt=False,
                    status="current",
                    manifest=existing.manifest,
                )
            snapshot = self._build_snapshot(
                project_id,
                source_fingerprint=source_fingerprint,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                knowledge_fingerprint=knowledge_fingerprint,
            )
            self._write_snapshot(project_id, snapshot)
            return SearchRebuildResult(
                project_id=project_id,
                rebuilt=True,
                status="rebuilt",
                manifest=snapshot.manifest,
            )

    def search(self, project_id: str, request: SearchQueryRequest) -> SearchQueryResponse:
        project_id = self._validate_project_id(project_id)
        with self._project_lock(project_id):
            rebuilt_before_search = False
            snapshot = self._read_snapshot(project_id)
            (
                source_fingerprint,
                project_revision,
                project_fingerprint,
                knowledge_fingerprint,
            ) = self._observe_source_fingerprint(project_id)
            stale = (
                snapshot is None
                or snapshot.manifest.source_fingerprint != source_fingerprint
                or snapshot.manifest.project_revision != project_revision
                or snapshot.manifest.project_fingerprint != project_fingerprint
                or snapshot.manifest.knowledge_fingerprint != knowledge_fingerprint
            )
            if stale:
                if not request.auto_rebuild and snapshot is None:
                    raise SearchNotFoundError("project search index was not found")
                if not request.auto_rebuild:
                    raise SearchConflictError(
                        "project search index is stale; rebuild required",
                        details=[
                            {
                                "code": "search_index_stale",
                                "index_hash": (
                                    snapshot.manifest.index_hash if snapshot else None
                                ),
                                "source_fingerprint": source_fingerprint,
                            }
                        ],
                        retryable=True,
                    )
                snapshot = self._build_snapshot(
                    project_id,
                    source_fingerprint=source_fingerprint,
                    project_revision=project_revision,
                    project_fingerprint=project_fingerprint,
                    knowledge_fingerprint=knowledge_fingerprint,
                )
                self._write_snapshot(project_id, snapshot)
                rebuilt_before_search = True

            tokens = _tokenize_query(request.query)
            hits: list[SearchHit] = []
            for document in snapshot.documents:
                if (
                    request.source_types is not None
                    and document.source_type not in request.source_types
                ):
                    continue
                if (
                    request.authorities is not None
                    and document.authority not in request.authorities
                ):
                    continue
                if request.updated_after is not None:
                    if document.updated_at is None or document.updated_at < request.updated_after:
                        continue
                if request.updated_before is not None:
                    if document.updated_at is None or document.updated_at > request.updated_before:
                        continue
                scored = _score_document(document, query=request.query, tokens=tokens)
                if scored is None:
                    continue
                score, reason = scored
                hits.append(
                    SearchHit(
                        source_id=document.source_id,
                        source_type=document.source_type,
                        authority=document.authority,
                        title=document.title,
                        relative_path=document.relative_path,
                        version=document.version,
                        source_hash=document.source_hash,
                        snippet=_snippet_for(document.content, request.query, tokens),
                        reason=reason,
                        score=round(score, 4),
                        updated_at=document.updated_at,
                    )
                )
            hits.sort(
                key=lambda item: (
                    -item.score,
                    item.source_type.value,
                    item.source_id,
                )
            )
            limited = hits[: request.limit]
            return SearchQueryResponse(
                project_id=project_id,
                query=request.query,
                index_hash=snapshot.manifest.index_hash,
                source_fingerprint=snapshot.manifest.source_fingerprint,
                rebuilt_before_search=rebuilt_before_search,
                hit_count=len(limited),
                match_mode="full_text",
                vector_enhanced=False,
                hits=limited,
            )

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_id] = lock
            return lock

    def _validate_project_id(self, project_id: str) -> str:
        normalized = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(normalized):
            raise SearchInvalidError("project_id is invalid")
        return normalized

    def _require_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise SearchNotFoundError("project binding was not found")
        return binding

    def _observe_source_fingerprint(
        self,
        project_id: str,
    ) -> tuple[str, int, str, str]:
        binding = self._require_binding(project_id)
        try:
            inspection = self.project_reader.inspect(binding.root_path)
        except ProjectReaderError as exc:
            raise SearchInvalidError(str(exc)) from exc
        project = inspection.get("project")
        if not isinstance(project, dict):
            raise SearchInvalidError("project inspection is missing project summary")
        project_fingerprint = str(project.get("fingerprint") or "").strip()
        project_revision = int(project.get("revision") or 0)
        if len(project_fingerprint) != 64 or project_revision < 1:
            raise SearchInvalidError("project inspection fingerprint/revision is invalid")
        knowledge_fingerprint = self.knowledge_service.observe_knowledge_fingerprint(
            project_id
        )
        if len(knowledge_fingerprint) != 64:
            raise SearchInvalidError("knowledge fingerprint is invalid")
        authorized_source_fingerprint = (
            self.authorized_source_catalog.observe_authorized_source_fingerprint(project_id)
        )
        if len(authorized_source_fingerprint) != 64:
            raise SearchInvalidError("authorized source fingerprint is invalid")
        source_fingerprint = canonical_json_hash(
            {
                "schema": "zhimeng.project-search-source-fingerprint.v1",
                "project_id": project_id,
                "project_revision": project_revision,
                "project_fingerprint": project_fingerprint,
                "knowledge_fingerprint": knowledge_fingerprint,
                "authorized_source_fingerprint": authorized_source_fingerprint,
                "builder_id": BUILDER_ID,
                "builder_version": BUILDER_VERSION,
                "index_policy_version": INDEX_POLICY_VERSION,
            }
        )
        return (
            source_fingerprint,
            project_revision,
            project_fingerprint,
            knowledge_fingerprint,
        )

    def _build_snapshot(
        self,
        project_id: str,
        *,
        source_fingerprint: str,
        project_revision: int,
        project_fingerprint: str,
        knowledge_fingerprint: str,
    ) -> SearchIndexSnapshot:
        binding = self._require_binding(project_id)
        try:
            inspection = self.project_reader.inspect(binding.root_path)
        except ProjectReaderError as exc:
            raise SearchInvalidError(str(exc)) from exc

        documents: list[SearchIndexDocument] = []
        files = inspection.get("files")
        if not isinstance(files, list):
            raise SearchInvalidError("project inspection is missing files")
        for raw_file in files:
            if not isinstance(raw_file, dict):
                continue
            kind = str(raw_file.get("kind") or "")
            if kind not in PUBLIC_TEXT_FILE_KINDS:
                continue
            file_id = str(raw_file.get("file_id") or "").strip()
            relative_path = str(raw_file.get("relative_path") or "").strip()
            source_hash = str(raw_file.get("sha256") or "").strip()
            if not file_id or not relative_path or len(source_hash) != 64:
                continue
            if _is_non_truth_project_path(relative_path):
                continue
            try:
                content_payload = self.project_reader.read_text_file_from_snapshot(
                    inspection,
                    file_id,
                    allowed_kinds=PUBLIC_TEXT_FILE_KINDS,
                )
            except ProjectReaderError:
                continue
            content = str(content_payload.get("content") or "")
            documents.append(
                SearchIndexDocument(
                    source_id=file_id,
                    source_type=SearchSourceType.PROJECT_FILE,
                    authority=ContextAuthority.PROJECT_TRUTH,
                    title=relative_path,
                    relative_path=relative_path,
                    version=f"rev-{project_revision}",
                    source_hash=source_hash,
                    content=content,
                    updated_at=None,
                    project_revision=project_revision,
                )
            )

        # Formal knowledge only: KnowledgeService.list_cards never returns candidates.
        card_list = self.knowledge_service.list_cards(project_id=project_id)
        cards = getattr(card_list, "cards", None)
        if cards is None and isinstance(card_list, dict):
            cards = card_list.get("cards")
        if not isinstance(cards, list):
            raise SearchInvalidError("knowledge card list is invalid")
        for card in cards:
            document = self._document_from_card(card)
            if document is not None:
                documents.append(document)

        for source in self.authorized_source_catalog.list_authorized_sources(project_id):
            documents.append(
                SearchIndexDocument(
                    source_id=source.source_id,
                    source_type=SearchSourceType.AUTHORIZED_SOURCE,
                    authority=ContextAuthority.REFERENCE,
                    title=source.title,
                    relative_path=None,
                    version=source.version,
                    source_hash=source.source_hash,
                    content=source.content,
                    updated_at=source.updated_at,
                    project_revision=None,
                )
            )

        documents.sort(key=lambda item: (item.source_type.value, item.source_id))
        index_hash = search_index_hash(
            project_id=project_id,
            source_fingerprint=source_fingerprint,
            documents=documents,
        )
        manifest = SearchIndexManifest(
            project_id=project_id,
            source_fingerprint=source_fingerprint,
            project_revision=project_revision,
            project_fingerprint=project_fingerprint,
            knowledge_fingerprint=knowledge_fingerprint,
            document_count=len(documents),
            full_text_indexed=True,
            vector_index_ready=False,
            rebuilt_at=_utcnow(),
            index_hash=index_hash,
        )
        return SearchIndexSnapshot(manifest=manifest, documents=documents)

    def _document_from_card(self, card: Any) -> SearchIndexDocument | None:
        if isinstance(card, KnowledgeCard):
            model = card
        elif isinstance(card, dict):
            try:
                model = KnowledgeCard.model_validate(card)
            except ValidationError:
                return None
        else:
            return None
        if model.status is not KnowledgeCardStatus.ACTIVE:
            return None
        payload = model.active_version.card
        if not isinstance(payload, dict):
            return None
        title = _card_title(payload, model.card_id)
        content = _card_search_text(payload)
        return SearchIndexDocument(
            source_id=model.card_id,
            source_type=SearchSourceType.FORMAL_KNOWLEDGE,
            authority=ContextAuthority.FORMAL_KNOWLEDGE,
            title=title,
            relative_path=None,
            version=model.active_version_id,
            source_hash=model.active_version.content_hash,
            content=content,
            updated_at=model.updated_at,
            project_revision=None,
            card_status="active",
            formalization_status=model.active_version.formalization_status.value,
        )

    def _project_dir(self, project_id: str) -> Path:
        return self.state_root / project_id

    def _index_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / INDEX_FILENAME

    def _read_snapshot(self, project_id: str) -> SearchIndexSnapshot | None:
        path = self._index_path(project_id)
        if not path.exists():
            return None
        if _is_link_or_junction(path) or _is_link_or_junction(self._project_dir(project_id)):
            raise SearchConflictError(
                "project search index path is not a regular file",
                details=[{"code": "search_index_tampered", "path": str(path)}],
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SearchConflictError(
                "project search index could not be read",
                details=[{"code": "search_index_unreadable", "error": str(exc)}],
                retryable=True,
            ) from exc
        if len(raw) > MAX_INDEX_BYTES:
            raise SearchConflictError(
                "project search index exceeds size limit",
                details=[{"code": "search_index_too_large", "size_bytes": len(raw)}],
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
            snapshot = SearchIndexSnapshot.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise SearchConflictError(
                "project search index is corrupted",
                details=[{"code": "search_index_corrupt", "error": str(exc)}],
            ) from exc
        expected = search_index_hash(
            project_id=snapshot.manifest.project_id,
            source_fingerprint=snapshot.manifest.source_fingerprint,
            documents=list(snapshot.documents),
        )
        if (
            snapshot.manifest.project_id != project_id
            or snapshot.manifest.index_hash != expected
        ):
            raise SearchConflictError(
                "project search index hash mismatch",
                details=[
                    {
                        "code": "search_index_hash_mismatch",
                        "expected": expected,
                        "actual": snapshot.manifest.index_hash,
                    }
                ],
            )
        return snapshot

    def _write_snapshot(self, project_id: str, snapshot: SearchIndexSnapshot) -> None:
        project_dir = self._project_dir(project_id)
        if project_dir.exists() and _is_link_or_junction(project_dir):
            raise SearchConflictError(
                "project search project directory cannot be a link or junction",
                details=[{"code": "search_state_tampered", "path": str(project_dir)}],
            )
        try:
            self.state_root.mkdir(parents=True, exist_ok=True)
            project_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SearchUnavailableError(
                "project search state root is unavailable",
                details=[{"code": "search_state_unwritable", "error": str(exc)}],
                retryable=True,
            ) from exc
        payload = snapshot.model_dump(mode="json", by_alias=True)
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        data = encoded.encode("utf-8")
        if len(data) > MAX_INDEX_BYTES:
            raise SearchInvalidError(
                "project search index would exceed size limit",
                details=[{"code": "search_index_too_large", "size_bytes": len(data)}],
            )
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{project_id}.",
            suffix=".tmp",
            dir=str(project_dir),
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._index_path(project_id))
        except Exception:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise
        try:
            dir_fd = os.open(str(project_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


__all__ = [
    "SearchConflictError",
    "SearchInvalidError",
    "SearchNotFoundError",
    "SearchService",
    "SearchServiceError",
    "SearchUnavailableError",
]
