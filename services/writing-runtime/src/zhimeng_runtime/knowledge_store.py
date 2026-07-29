from __future__ import annotations

import json
import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import uuid4

from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCandidateOrigin,
    KnowledgeCandidateStatus,
    KnowledgeCard,
    KnowledgeCardStatus,
    KnowledgeCardVersion,
    KnowledgeFormalizationReceipt,
    KnowledgeFormalizationDecision,
    KnowledgeFormalizationRef,
    KnowledgeLineageStatus,
    KnowledgeReviewIssue,
    KnowledgeVersionLineageReceipt,
    KnowledgeVersionLineageReceiptV2,
    KnowledgeFormalizationReceiptV2,
    KnowledgeVersionFormalizationStatus,
    knowledge_version_lineage_receipt_hash,
    knowledge_candidate_set_hash,
    knowledge_formalization_receipt_hash,
    knowledge_formalization_receipt_id,
    native_knowledge_state_hash,
)
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.distillation_receipt_models import (
    DistillationCandidateOutputPayload,
    DistillationFinalResultPayload,
    DistillationInvocationReceipt,
    DistillationProviderTurnResultPayload,
    DistillationReceiptKind,
    distillation_provider_turn_chain_hash,
)
from zhimeng_runtime.knowledge_scope_models import (
    APPLICATION_KNOWLEDGE_SCOPE_ID,
    KnowledgeScopeRecord,
    KnowledgeScopeRef,
    KnowledgeScopeType,
    KnowledgeSourceScopeRecord,
    knowledge_scope_id,
)
from zhimeng_runtime.source_import_models import (
    SourceImportProgress,
    SourceImportStatus,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_receipt_models import (
    AnySourceImportExecutionReceipt,
    parse_source_import_execution_receipt,
    validate_source_import_receipt_job,
)


class KnowledgeStoreError(RuntimeError):
    pass


class KnowledgeStoreNotFound(KnowledgeStoreError):
    pass


class KnowledgeStoreConflict(KnowledgeStoreError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeScopeSnapshot:
    scope: KnowledgeScopeRecord
    cards: list[KnowledgeCard]
    fingerprint_material: dict[str, object] | None
    inherited_card_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectStateSnapshot:
    project: ProjectBindingRecord
    scope: KnowledgeScopeRecord
    cards: tuple[KnowledgeCard, ...]
    candidates: tuple[KnowledgeCandidate, ...]
    formalization_receipts: tuple[
        KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2,
        ...,
    ]
    fingerprint_material: dict[str, object]
    inherited_card_ids: tuple[str, ...]
    origin_scope_ids: tuple[tuple[str, str], ...]


class KnowledgeStore:
    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    # Scope helpers live in the store so every caller uses the same immutable
    # source binding and card-pointer CAS rules.
    @staticmethod
    def _normalize_scope_id(scope_id: str | None) -> str:
        value = APPLICATION_KNOWLEDGE_SCOPE_ID if scope_id is None else str(scope_id)
        if not value.strip():
            raise ValueError("knowledge scope id cannot be empty")
        return value

    @staticmethod
    def _scope_ref_from_row(row: sqlite3.Row) -> KnowledgeScopeRef:
        return KnowledgeScopeRef(
            scope_id=str(row["id"]),
            scope_type=str(row["scope_type"]),
            project_binding_id=(
                str(row["project_binding_id"])
                if row["project_binding_id"] is not None
                else None
            ),
            state_revision=int(row["state_revision"]),
        )

    @classmethod
    def _scope_from_row(cls, row: sqlite3.Row) -> KnowledgeScopeRecord:
        ref = cls._scope_ref_from_row(row)
        return KnowledgeScopeRecord(
            **ref.model_dump(mode="python", exclude={"schema_id"}),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _source_scope_from_row(row: sqlite3.Row) -> KnowledgeSourceScopeRecord:
        return KnowledgeSourceScopeRecord(
            source_import_id=str(row["source_import_id"]),
            scope_id=str(row["scope_id"]),
            bound_at=str(row["bound_at"]),
        )

    @classmethod
    def _scope_row_in_connection(
        cls,
        connection: sqlite3.Connection,
        scope_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM knowledge_scope WHERE id = ?",
            (cls._normalize_scope_id(scope_id),),
        ).fetchone()
        if row is None:
            raise KnowledgeStoreNotFound("knowledge scope was not found")
        return row

    @classmethod
    def _ensure_application_scope_in_connection(
        cls,
        connection: sqlite3.Connection,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM knowledge_scope WHERE id = ?",
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        ).fetchone()
        if row is not None:
            if str(row["scope_type"]) != KnowledgeScopeType.APPLICATION.value:
                raise KnowledgeStoreError("knowledge application scope is invalid")
            return row
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_scope(
                id, scope_type, project_binding_id, state_revision,
                created_at, updated_at
            ) VALUES (?, 'application', NULL, 1, ?, ?)
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID, now, now),
        )
        row = connection.execute(
            "SELECT * FROM knowledge_scope WHERE id = ?",
            (APPLICATION_KNOWLEDGE_SCOPE_ID,),
        ).fetchone()
        if row is None or str(row["scope_type"]) != KnowledgeScopeType.APPLICATION.value:
            raise KnowledgeStoreError("knowledge application scope is invalid")
        return row

    @classmethod
    def _ensure_scope_in_connection(
        cls,
        connection: sqlite3.Connection,
        scope_id: str,
    ) -> sqlite3.Row:
        normalized = cls._normalize_scope_id(scope_id)
        if normalized == APPLICATION_KNOWLEDGE_SCOPE_ID:
            return cls._ensure_application_scope_in_connection(connection)
        return cls._scope_row_in_connection(connection, normalized)

    @classmethod
    def _ensure_source_scope_in_connection(
        cls,
        connection: sqlite3.Connection,
        source_import_id: str,
        scope_id: str,
    ) -> sqlite3.Row:
        scope_row = cls._ensure_scope_in_connection(connection, scope_id)
        existing = connection.execute(
            "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
            (source_import_id,),
        ).fetchone()
        if existing is not None:
            if str(existing["scope_id"]) != str(scope_row["id"]):
                raise KnowledgeStoreConflict(
                    "source import is already bound to another knowledge scope"
                )
            return existing
        source = connection.execute(
            "SELECT id, created_at FROM source_import_job WHERE id = ?",
            (source_import_id,),
        ).fetchone()
        if source is None:
            raise KnowledgeStoreNotFound("source import was not found")
        bound_at = str(source["created_at"] or datetime.now(UTC).isoformat())
        try:
            connection.execute(
                """
                INSERT INTO knowledge_source_scope(
                    source_import_id, scope_id, bound_at
                ) VALUES (?, ?, ?)
                """,
                (source_import_id, str(scope_row["id"]), bound_at),
            )
        except sqlite3.IntegrityError as exc:
            current = connection.execute(
                "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
                (source_import_id,),
            ).fetchone()
            if current is not None and str(current["scope_id"]) == str(scope_row["id"]):
                return current
            raise KnowledgeStoreConflict(
                "source import scope binding conflicted with another writer"
            ) from exc
        result = connection.execute(
            "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
            (source_import_id,),
        ).fetchone()
        if result is None:
            raise KnowledgeStoreError("source import scope binding was not visible")
        return result

    @classmethod
    def _touch_scope_in_connection(
        cls,
        connection: sqlite3.Connection,
        scope_id: str,
        now: str,
    ) -> sqlite3.Row:
        normalized = cls._normalize_scope_id(scope_id)
        result = connection.execute(
            """
            UPDATE knowledge_scope
            SET state_revision = state_revision + 1, updated_at = ?
            WHERE id = ?
            """,
            (now, normalized),
        )
        if result.rowcount != 1:
            raise KnowledgeStoreNotFound("knowledge scope was not found")
        return cls._scope_row_in_connection(connection, normalized)

    def ensure_application_scope(self) -> KnowledgeScopeRecord:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._ensure_application_scope_in_connection(connection)
                connection.commit()
                return self._scope_from_row(row)
            except Exception:
                connection.rollback()
                raise

    def ensure_project_scope(self, project_id: str) -> KnowledgeScopeRecord:
        if not str(project_id).strip():
            raise ValueError("project binding id cannot be empty")
        scope_id = knowledge_scope_id(str(project_id))
        now = datetime.now(UTC).isoformat()
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                project = connection.execute(
                    "SELECT id FROM project_binding WHERE id = ?",
                    (str(project_id),),
                ).fetchone()
                if project is None:
                    raise KnowledgeStoreNotFound("project binding was not found")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_scope(
                        id, scope_type, project_binding_id, state_revision,
                        created_at, updated_at
                    ) VALUES (?, 'project', ?, 1, ?, ?)
                    """,
                    (scope_id, str(project_id), now, now),
                )
                row = self._scope_row_in_connection(connection, scope_id)
                if (
                    str(row["scope_type"]) != KnowledgeScopeType.PROJECT.value
                    or str(row["project_binding_id"]) != str(project_id)
                ):
                    raise KnowledgeStoreConflict(
                        "deterministic project scope id is bound to another project"
                    )
                connection.commit()
                return self._scope_from_row(row)
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KnowledgeStoreConflict(
                    "project knowledge scope conflicts with an existing binding"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def get_scope(self, scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID) -> KnowledgeScopeRecord:
        with self.database.connection() as connection:
            row = self._scope_row_in_connection(connection, self._normalize_scope_id(scope_id))
        return self._scope_from_row(row)

    def get_scope_for_project(self, project_id: str) -> KnowledgeScopeRecord:
        scope_id = knowledge_scope_id(str(project_id))
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM knowledge_scope
                WHERE scope_type = 'project' AND project_binding_id = ?
                """,
                (str(project_id),),
            ).fetchone()
            if row is None:
                raise KnowledgeStoreNotFound("project knowledge scope was not found")
            if str(row["id"]) != scope_id:
                raise KnowledgeStoreError("project knowledge scope id is not deterministic")
        return self._scope_from_row(row)

    def bind_source_import_scope(
        self,
        source_import_id: str,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeSourceScopeRecord:
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._ensure_source_scope_in_connection(
                    connection, source_import_id, self._normalize_scope_id(scope_id)
                )
                connection.commit()
                return self._source_scope_from_row(row)
            except Exception:
                connection.rollback()
                raise

    def get_source_import_scope(self, source_import_id: str) -> KnowledgeSourceScopeRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
                (source_import_id,),
            ).fetchone()
            if row is None:
                raise KnowledgeStoreNotFound("source import scope was not found")
        return self._source_scope_from_row(row)

    def get_scoped_card(
        self,
        scope_id: str,
        card_id: str,
    ) -> KnowledgeCard:
        with self.database.connection() as connection:
            return self._get_card_in_connection(
                connection, card_id, scope_id=self._normalize_scope_id(scope_id)
            )

    def list_scope_cards(
        self,
        scope_id: str,
        *,
        include_application: bool = False,
        active_only: bool = False,
    ) -> list[KnowledgeCard]:
        with self.database.connection() as connection:
            return self._list_cards_in_connection(
                connection,
                scope_id=self._normalize_scope_id(scope_id),
                include_application=include_application,
                active_only=active_only,
            )

    def scope_state_revision(self, scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID) -> int:
        return self.get_scope(scope_id).state_revision

    def scope_fingerprint_material(
        self,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
        *,
        include_application: bool = True,
    ) -> dict[str, object]:
        normalized = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                snapshot = self._scope_snapshot_in_connection(
                    connection,
                    normalized,
                    include_application=include_application,
                    active_only=False,
                )
                connection.commit()
                if snapshot.fingerprint_material is None:
                    raise KnowledgeStoreError("knowledge scope fingerprint material is missing")
                return snapshot.fingerprint_material
            except Exception:
                connection.rollback()
                raise

    def scope_snapshot(
        self,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
        *,
        include_application: bool = False,
        active_only: bool = False,
    ) -> KnowledgeScopeSnapshot:
        """Read visible cards and their fingerprint material in one SQLite snapshot."""

        normalized = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                snapshot = self._scope_snapshot_in_connection(
                    connection,
                    normalized,
                    include_application=include_application,
                    active_only=active_only,
                )
                connection.commit()
                return snapshot
            except Exception:
                connection.rollback()
                raise

    def project_state_snapshot(self, project_id: str) -> KnowledgeProjectStateSnapshot:
        """Read the complete visible project knowledge state in one transaction."""

        with self.database.connection() as connection:
            connection.execute("BEGIN")
            try:
                snapshot = self._project_state_snapshot_in_connection(
                    connection,
                    str(project_id),
                )
                connection.commit()
                return snapshot
            except Exception:
                connection.rollback()
                raise

    def project_state_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> KnowledgeProjectStateSnapshot:
        """Read project knowledge without managing the caller's transaction."""

        return self._project_state_snapshot_in_connection(connection, str(project_id))

    @contextmanager
    def locked_project_state_reader(
        self,
        project_id: str,
    ) -> Iterator[Callable[[], KnowledgeProjectStateSnapshot]]:
        """Hold SQLite's single-writer lock while a derived snapshot is published."""

        connection = self.database.connection()
        with connection as active:
            active.execute("BEGIN IMMEDIATE")
            try:
                def read() -> KnowledgeProjectStateSnapshot:
                    return self._project_state_snapshot_in_connection(
                        active,
                        str(project_id),
                    )

                yield read
                active.commit()
            except Exception:
                active.rollback()
                raise

    def _project_state_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> KnowledgeProjectStateSnapshot:
        if not project_id.strip():
            raise ValueError("project binding id cannot be empty")
        project_row = connection.execute(
            "SELECT * FROM project_binding WHERE id = ?",
            (project_id,),
        ).fetchone()
        if project_row is None:
            raise KnowledgeStoreNotFound("project binding was not found")
        project = ProjectBindingRecord(
            project_id=str(project_row["id"]),
            root_path=str(project_row["root_path"]),
            display_name=str(project_row["display_name"]),
            fingerprint=str(project_row["fingerprint"]),
            revision=int(project_row["revision"]),
            created_at=str(project_row["created_at"]),
            updated_at=str(project_row["updated_at"]),
        )
        scope_id = knowledge_scope_id(project_id)
        scope_snapshot = self._scope_snapshot_in_connection(
            connection,
            scope_id,
            include_application=True,
            active_only=False,
        )
        if scope_snapshot.fingerprint_material is None:
            raise KnowledgeStoreError("project knowledge fingerprint material is missing")

        inherited_ids = set(scope_snapshot.inherited_card_ids)
        origin_scope_ids = tuple(
            sorted(
                (
                    card.card_id,
                    (
                        APPLICATION_KNOWLEDGE_SCOPE_ID
                        if card.card_id in inherited_ids
                        else scope_id
                    ),
                )
                for card in scope_snapshot.cards
            )
        )
        referenced_candidate_ids = {
            version.source_candidate_id
            for card in scope_snapshot.cards
            for version in card.versions
        }
        candidate_rows = connection.execute(
            """
            SELECT candidate.*, source_scope.scope_id AS source_scope_id
            FROM knowledge_candidate AS candidate
            JOIN knowledge_source_scope AS source_scope
              ON source_scope.source_import_id = candidate.source_import_id
            WHERE source_scope.scope_id = ?
            ORDER BY candidate.id
            """,
            (scope_id,),
        ).fetchall()
        candidates_by_id = {
            str(row["id"]): self._candidate_from_row(row)
            for row in candidate_rows
        }
        missing_candidate_ids = sorted(
            referenced_candidate_ids.difference(candidates_by_id)
        )
        if missing_candidate_ids:
            placeholders = ",".join("?" for _ in missing_candidate_ids)
            inherited_rows = connection.execute(
                f"""
                SELECT candidate.*, source_scope.scope_id AS source_scope_id
                FROM knowledge_candidate AS candidate
                JOIN knowledge_source_scope AS source_scope
                  ON source_scope.source_import_id = candidate.source_import_id
                WHERE candidate.id IN ({placeholders})
                ORDER BY candidate.id
                """,
                tuple(missing_candidate_ids),
            ).fetchall()
            for row in inherited_rows:
                if str(row["source_scope_id"]) != APPLICATION_KNOWLEDGE_SCOPE_ID:
                    raise KnowledgeStoreError(
                        "inherited project KB candidate is not application-scoped"
                    )
                candidates_by_id[str(row["id"])] = self._candidate_from_row(row)
        if referenced_candidate_ids.difference(candidates_by_id):
            raise KnowledgeStoreError("project KB card version has no candidate evidence")

        source_import_ids = sorted(
            {candidate.source_import_id for candidate in candidates_by_id.values()}
        )
        formalization_receipts: list[
            KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2
        ] = []
        if source_import_ids:
            placeholders = ",".join("?" for _ in source_import_ids)
            receipt_rows = connection.execute(
                f"""
                SELECT receipt.*, source_scope.scope_id AS source_scope_id
                FROM knowledge_formalization_receipt AS receipt
                JOIN knowledge_source_scope AS source_scope
                  ON source_scope.source_import_id = receipt.source_import_id
                WHERE receipt.source_import_id IN ({placeholders})
                ORDER BY receipt.source_import_id
                """,
                tuple(source_import_ids),
            ).fetchall()
            for row in receipt_rows:
                expected_scope_id = str(row["source_scope_id"])
                if expected_scope_id not in {
                    scope_id,
                    APPLICATION_KNOWLEDGE_SCOPE_ID,
                }:
                    raise KnowledgeStoreError(
                        "project KB formalization belongs to an unrelated scope"
                    )
                formalization_receipts.append(
                    self._formalization_from_row(
                        row,
                        expected_scope_id=expected_scope_id,
                    )
                )

        return KnowledgeProjectStateSnapshot(
            project=project,
            scope=scope_snapshot.scope,
            cards=tuple(sorted(scope_snapshot.cards, key=lambda item: item.card_id)),
            candidates=tuple(
                sorted(candidates_by_id.values(), key=lambda item: item.candidate_id)
            ),
            formalization_receipts=tuple(formalization_receipts),
            fingerprint_material=scope_snapshot.fingerprint_material,
            inherited_card_ids=scope_snapshot.inherited_card_ids,
            origin_scope_ids=origin_scope_ids,
        )

    def _scope_snapshot_in_connection(
        self,
        connection: sqlite3.Connection,
        scope_id: str,
        *,
        include_application: bool,
        active_only: bool,
    ) -> KnowledgeScopeSnapshot:
        normalized = self._normalize_scope_id(scope_id)
        scope_row = self._scope_row_in_connection(connection, normalized)
        all_cards = self._list_cards_in_connection(
            connection,
            scope_id=normalized,
            include_application=include_application,
            active_only=False,
        )
        source_rows = connection.execute(
            """
            SELECT card.card_id, scoped.scope_id AS origin_scope_id
            FROM knowledge_scope_card AS scoped
            JOIN knowledge_card AS card ON card.card_id = scoped.card_id
            WHERE scoped.scope_id = ?
            """,
            (normalized,),
        ).fetchall()
        origins = {str(row["card_id"]): str(row["origin_scope_id"]) for row in source_rows}
        if include_application and normalized != APPLICATION_KNOWLEDGE_SCOPE_ID:
            inherited = connection.execute(
                """
                SELECT app.card_id, app.scope_id AS origin_scope_id
                FROM knowledge_scope_card AS app
                WHERE app.scope_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_scope_card AS own
                      WHERE own.scope_id = ? AND own.card_id = app.card_id
                  )
                """,
                (APPLICATION_KNOWLEDGE_SCOPE_ID, normalized),
            ).fetchall()
            origins.update(
                {str(row["card_id"]): str(row["origin_scope_id"]) for row in inherited}
            )
        material_cards = []
        for card in all_cards:
            version = card.active_version
            material_cards.append(
                {
                    "card_id": card.card_id,
                    "origin_scope_id": origins.get(card.card_id, normalized),
                    "status": card.status.value,
                    "state_revision": card.state_revision,
                    "active_version_id": card.active_version_id,
                    "content_hash": version.content_hash,
                    "lineage_receipt_hash": (
                        version.lineage.receipt_hash
                        if version.lineage_status is KnowledgeLineageStatus.VERIFIED
                        and version.lineage is not None
                        else None
                    ),
                    "formalization_receipt_hash": (
                        version.formalization.formalization_receipt_hash
                        if version.formalization_status
                        is KnowledgeVersionFormalizationStatus.VERIFIED
                        and version.formalization is not None
                        else None
                    ),
                }
            )
        visible = (
            [card for card in all_cards if card.status is KnowledgeCardStatus.ACTIVE]
            if active_only
            else all_cards
        )
        fingerprint_material = {
            "schema": "zhimeng.knowledge-scope-fingerprint.v1",
            "scope": {
                "scope_id": str(scope_row["id"]),
                "scope_type": str(scope_row["scope_type"]),
                "project_binding_id": (
                    str(scope_row["project_binding_id"])
                    if scope_row["project_binding_id"] is not None
                    else None
                ),
                "state_revision": int(scope_row["state_revision"]),
            },
            "cards": sorted(material_cards, key=lambda item: str(item["card_id"])),
        }
        inherited_card_ids = tuple(
            sorted(
                card.card_id
                for card in visible
                if normalized != APPLICATION_KNOWLEDGE_SCOPE_ID
                and origins.get(card.card_id) == APPLICATION_KNOWLEDGE_SCOPE_ID
            )
        )
        return KnowledgeScopeSnapshot(
            scope=self._scope_from_row(scope_row),
            cards=visible,
            fingerprint_material=fingerprint_material,
            inherited_card_ids=inherited_card_ids,
        )

    def create_candidate(
        self,
        candidate: KnowledgeCandidate,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCandidate:
        return self.create_candidates([candidate], scope_id=scope_id)[0]

    def create_candidates(
        self,
        candidates: list[KnowledgeCandidate],
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> list[KnowledgeCandidate]:
        if not candidates:
            return []
        rows: list[sqlite3.Row] = []
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                normalized_scope = self._normalize_scope_id(scope_id)
                self._ensure_scope_in_connection(connection, normalized_scope)
                bound_sources: dict[str, sqlite3.Row] = {}
                for candidate in candidates:
                    if candidate.source_import_id not in bound_sources:
                        bound_sources[candidate.source_import_id] = (
                            self._ensure_source_scope_in_connection(
                                connection,
                                candidate.source_import_id,
                                normalized_scope,
                            )
                        )
                    existing = connection.execute(
                        """
                        SELECT * FROM knowledge_candidate
                        WHERE source_import_id = ? AND source_card_index = ?
                          AND source_draft_hash = ?
                        """,
                        (
                            candidate.source_import_id,
                            candidate.source_card_index,
                            candidate.source_draft_hash,
                        ),
                    ).fetchone()
                    if existing is not None:
                        rows.append(existing)
                        continue
                    connection.execute(
                        """
                        INSERT INTO knowledge_candidate(
                            id, source_import_id, source_card_index, source_draft_hash,
                            lineage_status, origin_json, origin_hash,
                            card_id, card_type, status, revision, content_hash,
                            payload_json, issues_json, promoted_version_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            candidate.candidate_id,
                            candidate.source_import_id,
                            candidate.source_card_index,
                            candidate.source_draft_hash,
                            candidate.lineage_status.value,
                            (
                                self._json(candidate.origin.model_dump(mode="json", by_alias=True))
                                if candidate.origin is not None
                                else None
                            ),
                            candidate.origin.origin_hash if candidate.origin is not None else None,
                            candidate.card_id,
                            candidate.card_type,
                            candidate.status.value,
                            candidate.revision,
                            candidate.content_hash,
                            self._json(candidate.card),
                            self._json(
                                [
                                    item.model_dump(mode="json")
                                    for item in candidate.issues
                                ]
                            ),
                            candidate.promoted_version_id,
                            candidate.created_at.isoformat(),
                            candidate.updated_at.isoformat(),
                        ),
                    )
                    inserted = connection.execute(
                        "SELECT * FROM knowledge_candidate WHERE id = ?",
                        (candidate.candidate_id,),
                    ).fetchone()
                    if inserted is None:
                        raise KnowledgeStoreError("knowledge candidate insert was not visible")
                    rows.append(inserted)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KnowledgeStoreConflict("knowledge candidate identity already exists") from exc
            except Exception:
                connection.rollback()
                raise
        return [self._candidate_from_row(row) for row in rows]

    def get_candidate(
        self,
        candidate_id: str,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCandidate:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT candidate.*
                FROM knowledge_candidate AS candidate
                JOIN knowledge_source_scope AS source_scope
                  ON source_scope.source_import_id = candidate.source_import_id
                WHERE candidate.id = ? AND source_scope.scope_id = ?
                """,
                (candidate_id, self._normalize_scope_id(scope_id)),
            ).fetchone()
        if row is None:
            raise KnowledgeStoreNotFound("knowledge candidate was not found")
        return self._candidate_from_row(row)

    def list_candidates(
        self,
        source_import_id: str | None = None,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> list[KnowledgeCandidate]:
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                self._ensure_application_scope_in_connection(connection)
            if source_import_id is None:
                rows = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE source_scope.scope_id = ?
                    ORDER BY candidate.updated_at DESC, candidate.id
                    """,
                    (normalized_scope,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.source_import_id = ?
                      AND source_scope.scope_id = ?
                    ORDER BY candidate.source_card_index, candidate.id
                    """,
                    (source_import_id, normalized_scope),
                ).fetchall()
        return [self._candidate_from_row(row) for row in rows]

    def update_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_content_hash: str,
        card_id: str,
        card_type: str,
        content_hash: str,
        card: dict[str, object],
        issues: list[KnowledgeReviewIssue],
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCandidate:
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.id = ? AND source_scope.scope_id = ?
                    """,
                    (candidate_id, normalized_scope),
                ).fetchone()
                if row is None:
                    raise KnowledgeStoreNotFound("knowledge candidate was not found")
                if str(row["status"]) != KnowledgeCandidateStatus.PENDING.value:
                    raise KnowledgeStoreConflict("only pending candidates can be edited")
                source = connection.execute(
                    "SELECT status FROM source_import_job WHERE id = ?",
                    (str(row["source_import_id"]),),
                ).fetchone()
                if source is None or str(source["status"]) != "awaiting_review":
                    raise KnowledgeStoreConflict(
                        "knowledge editing is closed after source formalization"
                    )
                if (
                    int(row["revision"]) != expected_revision
                    or str(row["content_hash"]) != expected_content_hash
                ):
                    raise KnowledgeStoreConflict("knowledge candidate changed before this edit")
                result = connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET card_id = ?, card_type = ?, revision = revision + 1,
                        content_hash = ?, payload_json = ?, issues_json = ?, updated_at = ?
                    WHERE id = ? AND revision = ? AND content_hash = ?
                    """,
                    (
                        card_id,
                        card_type,
                        content_hash,
                        self._json(card),
                        self._json([item.model_dump(mode="json") for item in issues]),
                        now,
                        candidate_id,
                        expected_revision,
                        expected_content_hash,
                    ),
                )
                if result.rowcount != 1:
                    raise KnowledgeStoreConflict("knowledge candidate changed before this edit")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id, scope_id=normalized_scope)

    def reject_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_content_hash: str,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCandidate:
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.id = ? AND source_scope.scope_id = ?
                    """,
                    (candidate_id, normalized_scope),
                ).fetchone()
                if row is None:
                    raise KnowledgeStoreNotFound("knowledge candidate was not found")
                current_status = str(row["status"])
                if current_status == KnowledgeCandidateStatus.REJECTED.value:
                    if (
                        int(row["revision"]) == expected_revision
                        and str(row["content_hash"]) == expected_content_hash
                    ):
                        connection.commit()
                        return self._candidate_from_row(row)
                    raise KnowledgeStoreConflict("knowledge candidate changed before rejection")
                if current_status not in {
                    KnowledgeCandidateStatus.PENDING.value,
                    KnowledgeCandidateStatus.DEFERRED.value,
                    KnowledgeCandidateStatus.PROMOTED.value,
                }:
                    raise KnowledgeStoreConflict(
                        "only review-stage candidates can be rejected"
                    )
                source = connection.execute(
                    "SELECT status FROM source_import_job WHERE id = ?",
                    (str(row["source_import_id"]),),
                ).fetchone()
                if source is None or str(source["status"]) != "awaiting_review":
                    raise KnowledgeStoreConflict(
                        "knowledge decisions are closed after source formalization"
                    )
                if current_status == KnowledgeCandidateStatus.PROMOTED.value:
                    version_row = connection.execute(
                        "SELECT * FROM knowledge_card_version WHERE id = ?",
                        (str(row["promoted_version_id"] or ""),),
                    ).fetchone()
                    if version_row is None:
                        raise KnowledgeStoreError(
                            "promoted candidate version is unavailable"
                        )
                    version = self._version_from_row(
                        connection,
                        version_row,
                        expected_scope_id=normalized_scope,
                    )
                    if (
                        version.formalization_status
                        is KnowledgeVersionFormalizationStatus.VERIFIED
                    ):
                        raise KnowledgeStoreConflict(
                            "formalized knowledge candidates cannot be rejected"
                        )
                result = connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET status = 'rejected', revision = revision + 1,
                        promoted_version_id = NULL, updated_at = ?
                    WHERE id = ? AND revision = ? AND content_hash = ?
                    """,
                    (now, candidate_id, expected_revision, expected_content_hash),
                )
                if result.rowcount != 1:
                    raise KnowledgeStoreConflict("knowledge candidate changed before rejection")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_candidate(candidate_id, scope_id=normalized_scope)

    def promote_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_content_hash: str,
        replace_active: bool,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        version_id = f"knowledge-version-{uuid4().hex}"
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                scope_row = self._ensure_scope_in_connection(connection, normalized_scope)
                candidate = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.id = ? AND source_scope.scope_id = ?
                    """,
                    (candidate_id, normalized_scope),
                ).fetchone()
                if candidate is None:
                    raise KnowledgeStoreNotFound("knowledge candidate was not found")
                if str(candidate["status"]) != KnowledgeCandidateStatus.PENDING.value:
                    if str(candidate["status"]) == KnowledgeCandidateStatus.PROMOTED.value:
                        if (
                            int(candidate["revision"]) != expected_revision
                            or str(candidate["content_hash"]) != expected_content_hash
                        ):
                            raise KnowledgeStoreConflict(
                                "knowledge candidate changed before promotion replay"
                            )
                        existing_version = str(candidate["promoted_version_id"] or "")
                        card = self._get_card_in_connection(
                            connection,
                            str(candidate["card_id"]),
                            scope_id=normalized_scope,
                        )
                        if any(
                            version.version_id == existing_version
                            for version in card.versions
                        ):
                            connection.commit()
                            return card
                    raise KnowledgeStoreConflict("knowledge candidate is not pending promotion")
                if str(candidate["lineage_status"]) != KnowledgeLineageStatus.VERIFIED.value:
                    raise KnowledgeStoreConflict(
                        "legacy-unverified knowledge candidates cannot be promoted"
                    )
                source = connection.execute(
                    "SELECT status FROM source_import_job WHERE id = ?",
                    (str(candidate["source_import_id"]),),
                ).fetchone()
                if source is None or str(source["status"]) != "awaiting_review":
                    raise KnowledgeStoreConflict(
                        "knowledge promotion is closed after source formalization"
                    )
                if (
                    int(candidate["revision"]) != expected_revision
                    or str(candidate["content_hash"]) != expected_content_hash
                ):
                    raise KnowledgeStoreConflict("knowledge candidate changed before promotion")
                issues = json.loads(str(candidate["issues_json"]))
                if issues:
                    raise KnowledgeStoreConflict("knowledge candidate still has review issues")
                card_id = str(candidate["card_id"])
                global_card = connection.execute(
                    "SELECT * FROM knowledge_card WHERE card_id = ?",
                    (card_id,),
                ).fetchone()
                current = connection.execute(
                    """
                    SELECT * FROM knowledge_scope_card
                    WHERE scope_id = ? AND card_id = ?
                    """,
                    (normalized_scope, card_id),
                ).fetchone()
                if global_card is None:
                    connection.execute(
                        """
                        INSERT INTO knowledge_card(
                            card_id, status, state_revision, active_version_id,
                            created_at, updated_at
                        ) VALUES (?, 'active', 1, NULL, ?, ?)
                        """,
                        (card_id, now, now),
                    )
                if current is None:
                    version_number = int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(version_number), 0)
                            FROM knowledge_card_version WHERE card_id = ?
                            """,
                            (card_id,),
                        ).fetchone()[0]
                    ) + 1
                    activation_mode = "initial"
                    activation_expected_version_id = version_id
                    activation_expected_state_revision = 1
                else:
                    active_version = connection.execute(
                        "SELECT * FROM knowledge_card_version WHERE id = ?",
                        (str(current["active_version_id"]),),
                    ).fetchone()
                    if active_version is None:
                        raise KnowledgeStoreError(
                            "knowledge card active version is missing"
                        )
                    active_record = self._version_from_row(
                        connection,
                        active_version,
                        expected_scope_id=normalized_scope,
                    )
                    if (
                        str(active_version["content_hash"]) == expected_content_hash
                        and active_record.formalization_status
                        is not KnowledgeVersionFormalizationStatus.ABANDONED
                    ):
                        raise KnowledgeStoreConflict(
                            "identical formal content already belongs to another candidate lineage"
                        )
                    if (
                        active_record.formalization_status
                        not in {
                            KnowledgeVersionFormalizationStatus.VERIFIED,
                            KnowledgeVersionFormalizationStatus.ABANDONED,
                        }
                    ):
                        raise KnowledgeStoreConflict(
                            "existing knowledge card still awaits formalization"
                        )
                    if not replace_active:
                        raise KnowledgeStoreConflict(
                            "an active card with this card_id already exists; explicit replacement is required"
                        )
                    version_number = int(
                        connection.execute(
                            """
                            SELECT COALESCE(MAX(version_number), 0)
                            FROM knowledge_card_version WHERE card_id = ?
                            """,
                            (card_id,),
                        ).fetchone()[0]
                    ) + 1
                    activation_mode = "replace"
                    activation_expected_version_id = str(current["active_version_id"])
                    activation_expected_state_revision = int(current["state_revision"])
                connection.execute(
                    """
                    INSERT INTO knowledge_card_version(
                        id, card_id, version_number, content_hash,
                        payload_json, source_candidate_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        card_id,
                        version_number,
                        expected_content_hash,
                        str(candidate["payload_json"]),
                        candidate_id,
                        now,
                    ),
                )
                origin = self._origin_from_candidate_row(candidate)
                if origin is None:
                    raise KnowledgeStoreConflict(
                        "verified knowledge candidate origin is unavailable"
                    )
                source_receipt = connection.execute(
                    """
                    SELECT id, receipt_hash, run_id, revision, idempotency_key
                    FROM source_import_execution_receipt
                    WHERE source_import_id = ?
                    """,
                    (str(candidate["source_import_id"]),),
                ).fetchone()
                if (
                    source_receipt is None
                    or any(
                        source_receipt[field] is None
                        for field in ("run_id", "revision", "idempotency_key")
                    )
                    or str(source_receipt["id"]) != origin.source_receipt_id
                    or str(source_receipt["receipt_hash"]) != origin.source_receipt_hash
                ):
                    raise KnowledgeStoreConflict(
                        "knowledge candidate source receipt is unavailable or changed"
                    )
                lineage_id = f"knowledge-lineage-{uuid4().hex}"
                created_at = datetime.fromisoformat(now)
                lineage_material = {
                    "lineage_receipt_id": lineage_id,
                    "version_id": version_id,
                    "card_id": card_id,
                    "version_content_hash": expected_content_hash,
                    "candidate_id": candidate_id,
                    "approved_candidate_revision": expected_revision,
                    "approved_candidate_content_hash": expected_content_hash,
                    "candidate_origin": origin,
                    "source_import_id": str(candidate["source_import_id"]),
                    "activation_mode": activation_mode,
                    "replacement_authorized": activation_mode == "replace",
                    "activation_expected_version_id": activation_expected_version_id,
                    "activation_expected_state_revision": (
                        activation_expected_state_revision
                    ),
                    "created_at": created_at,
                }
                if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                    provisional_lineage = KnowledgeVersionLineageReceipt.model_construct(
                        **lineage_material,
                        receipt_hash="0" * 64,
                    )
                    lineage = KnowledgeVersionLineageReceipt(
                        **lineage_material,
                        receipt_hash=knowledge_version_lineage_receipt_hash(
                            provisional_lineage
                        ),
                    )
                    receipt_scope_id: str | None = None
                else:
                    scope_ref = self._scope_ref_from_row(scope_row)
                    scoped_material = {**lineage_material, "scope": scope_ref}
                    provisional_lineage = KnowledgeVersionLineageReceiptV2.model_construct(
                        **scoped_material,
                        receipt_hash="0" * 64,
                    )
                    lineage = KnowledgeVersionLineageReceiptV2(
                        **scoped_material,
                        receipt_hash=knowledge_version_lineage_receipt_hash(
                            provisional_lineage
                        ),
                    )
                    receipt_scope_id = normalized_scope
                connection.execute(
                    """
                    INSERT INTO knowledge_version_lineage_receipt(
                        id, version_id, candidate_id, source_import_id,
                        source_receipt_id, source_receipt_hash, candidate_origin_hash,
                        approved_candidate_revision, approved_candidate_content_hash,
                        activation_mode, activation_expected_version_id,
                        activation_expected_state_revision, replacement_authorized,
                        receipt_hash, payload_json, created_at, scope_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineage.lineage_receipt_id,
                        lineage.version_id,
                        lineage.candidate_id,
                        lineage.source_import_id,
                        origin.source_receipt_id,
                        origin.source_receipt_hash,
                        origin.origin_hash,
                        lineage.approved_candidate_revision,
                        lineage.approved_candidate_content_hash,
                        lineage.activation_mode,
                        lineage.activation_expected_version_id,
                        lineage.activation_expected_state_revision,
                        int(lineage.replacement_authorized),
                        lineage.receipt_hash,
                        self._json(lineage.model_dump(mode="json", by_alias=True)),
                        lineage.created_at.isoformat(),
                        receipt_scope_id,
                    ),
                )
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO knowledge_scope_card(
                            scope_id, card_id, status, state_revision,
                            active_version_id, created_at, updated_at
                        ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                        """,
                        (normalized_scope, card_id, version_id, now, now),
                    )
                    if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                        connection.execute(
                            """
                            UPDATE knowledge_card
                            SET active_version_id = ?, updated_at = ?
                            WHERE card_id = ?
                            """,
                            (version_id, now, card_id),
                        )
                    self._touch_scope_in_connection(connection, normalized_scope, now)
                connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET status = 'promoted', promoted_version_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (version_id, now, candidate_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KnowledgeStoreConflict("knowledge promotion conflicted with an existing version") from exc
            except Exception:
                connection.rollback()
                raise
        return self.get_card(str(candidate["card_id"]), scope_id=normalized_scope)

    def promote_author_attested_feedback_candidate(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        expected_content_hash: str,
        author_promotion_receipt_id: str,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        """Write a formal knowledge card from author-attested feedback experience.

        Creates an active card version with legacy_unverified lineage/formalization.
        Context injection still requires verified formalization, so this is a write
        without auto-activation for formal Context/NLCL consumption.
        """
        if not author_promotion_receipt_id or not str(author_promotion_receipt_id).strip():
            raise KnowledgeStoreConflict(
                "author-attested promotion requires a formal knowledge promotion receipt id"
            )
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        version_id = f"knowledge-version-{uuid4().hex}"
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_scope_in_connection(connection, normalized_scope)
                candidate = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.id = ? AND source_scope.scope_id = ?
                    """,
                    (candidate_id, normalized_scope),
                ).fetchone()
                if candidate is None:
                    raise KnowledgeStoreNotFound("knowledge candidate was not found")
                if str(candidate["status"]) == KnowledgeCandidateStatus.PROMOTED.value:
                    if (
                        int(candidate["revision"]) != expected_revision
                        or str(candidate["content_hash"]) != expected_content_hash
                    ):
                        raise KnowledgeStoreConflict(
                            "knowledge candidate changed before author-attested promotion replay"
                        )
                    existing_version = str(candidate["promoted_version_id"] or "")
                    if not existing_version:
                        raise KnowledgeStoreConflict(
                            "promoted feedback candidate is missing version identity"
                        )
                    card = self._get_card_in_connection(
                        connection,
                        str(candidate["card_id"]),
                        scope_id=normalized_scope,
                    )
                    if any(version.version_id == existing_version for version in card.versions):
                        connection.commit()
                        return card
                    raise KnowledgeStoreConflict(
                        "promoted feedback candidate version is missing from formal knowledge"
                    )
                if str(candidate["status"]) != KnowledgeCandidateStatus.PENDING.value:
                    raise KnowledgeStoreConflict(
                        "knowledge candidate is not pending author-attested promotion"
                    )
                if str(candidate["lineage_status"]) != KnowledgeLineageStatus.LEGACY_UNVERIFIED.value:
                    raise KnowledgeStoreConflict(
                        "author-attested feedback promotion only accepts legacy_unverified candidates"
                    )
                source = connection.execute(
                    "SELECT status, payload_json FROM source_import_job WHERE id = ?",
                    (str(candidate["source_import_id"]),),
                ).fetchone()
                if source is None:
                    raise KnowledgeStoreConflict(
                        "feedback source import is unavailable for author-attested promotion"
                    )
                try:
                    source_payload = json.loads(str(source["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise KnowledgeStoreError(
                        "feedback source import payload is invalid"
                    ) from exc
                if str(source_payload.get("provider") or "") != "feedback_experience":
                    raise KnowledgeStoreConflict(
                        "author-attested promotion requires feedback_experience source lineage"
                    )
                if str(source["status"]) not in {"awaiting_review", "completed"}:
                    raise KnowledgeStoreConflict(
                        "feedback source import is not available for author-attested promotion"
                    )
                if (
                    int(candidate["revision"]) != expected_revision
                    or str(candidate["content_hash"]) != expected_content_hash
                ):
                    raise KnowledgeStoreConflict(
                        "knowledge candidate changed before author-attested promotion"
                    )
                card_id = str(candidate["card_id"])
                current = connection.execute(
                    """
                    SELECT * FROM knowledge_scope_card
                    WHERE scope_id = ? AND card_id = ?
                    """,
                    (normalized_scope, card_id),
                ).fetchone()
                if current is not None:
                    raise KnowledgeStoreConflict(
                        "formal knowledge card already exists for this feedback candidate identity"
                    )
                global_card = connection.execute(
                    "SELECT * FROM knowledge_card WHERE card_id = ?",
                    (card_id,),
                ).fetchone()
                if global_card is None:
                    connection.execute(
                        """
                        INSERT INTO knowledge_card(
                            card_id, status, state_revision, active_version_id,
                            created_at, updated_at
                        ) VALUES (?, 'active', 1, NULL, ?, ?)
                        """,
                        (card_id, now, now),
                    )
                version_number = int(
                    connection.execute(
                        """
                        SELECT COALESCE(MAX(version_number), 0)
                        FROM knowledge_card_version WHERE card_id = ?
                        """,
                        (card_id,),
                    ).fetchone()[0]
                ) + 1
                payload = json.loads(str(candidate["payload_json"]))
                if not isinstance(payload, dict):
                    raise KnowledgeStoreError("knowledge candidate payload is invalid")
                if canonical_json_hash(payload) != expected_content_hash:
                    raise KnowledgeStoreConflict(
                        "knowledge candidate payload hash does not match approved content"
                    )
                connection.execute(
                    """
                    INSERT INTO knowledge_card_version(
                        id, card_id, version_number, content_hash,
                        payload_json, source_candidate_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        card_id,
                        version_number,
                        expected_content_hash,
                        str(candidate["payload_json"]),
                        candidate_id,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_scope_card(
                        scope_id, card_id, status, state_revision,
                        active_version_id, created_at, updated_at
                    ) VALUES (?, ?, 'active', 1, ?, ?, ?)
                    """,
                    (normalized_scope, card_id, version_id, now, now),
                )
                if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                    connection.execute(
                        """
                        UPDATE knowledge_card
                        SET active_version_id = ?, updated_at = ?
                        WHERE card_id = ?
                        """,
                        (version_id, now, card_id),
                    )
                self._touch_scope_in_connection(connection, normalized_scope, now)
                connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET status = 'promoted', promoted_version_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (version_id, now, candidate_id),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KnowledgeStoreConflict(
                    "author-attested formal knowledge promotion conflicted with existing state"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return self.get_card(card_id, scope_id=normalized_scope)

    def get_card(
        self,
        card_id: str,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        with self.database.connection() as connection:
            return self._get_card_in_connection(
                connection,
                card_id,
                scope_id=self._normalize_scope_id(scope_id),
            )

    def list_cards(
        self,
        *,
        active_only: bool = False,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
        include_application: bool = False,
    ) -> list[KnowledgeCard]:
        with self.database.connection() as connection:
            return self._list_cards_in_connection(
                connection,
                active_only=active_only,
                scope_id=self._normalize_scope_id(scope_id),
                include_application=include_application,
            )

    @staticmethod
    def _distillation_receipt_from_row(
        row: sqlite3.Row | None,
        *,
        expected_invocation_id: str,
        expected_kind: DistillationReceiptKind,
        expected_receipt_id: str | None = None,
        expected_receipt_hash: str | None = None,
    ) -> DistillationInvocationReceipt:
        if row is None:
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation receipt is missing before formalization"
            )
        try:
            receipt = DistillationInvocationReceipt.model_validate_json(
                str(row["payload_json"])
            )
        except ValueError as exc:
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation receipt is invalid before formalization"
            ) from exc
        if (
            str(row["id"]) != receipt.receipt_id
            or str(row["invocation_id"]) != receipt.invocation_id
            or str(row["kind"]) != receipt.kind.value
            or str(row["receipt_hash"]) != receipt.receipt_hash
            or receipt.invocation_id != expected_invocation_id
            or receipt.kind is not expected_kind
            or (
                expected_receipt_id is not None
                and receipt.receipt_id != expected_receipt_id
            )
            or (
                expected_receipt_hash is not None
                and receipt.receipt_hash != expected_receipt_hash
            )
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation receipt columns disagree before formalization"
            )
        return receipt

    @classmethod
    def _verify_runtime_deep_finalization_evidence(
        cls,
        connection: sqlite3.Connection,
        *,
        invocation_id: str,
        scope_id: str,
        source_import_id: str,
        source_receipt: AnySourceImportExecutionReceipt,
        candidate_draft_hash: str,
        indexed_candidates: tuple[KnowledgeCandidate, ...],
    ) -> None:
        invocation_row = connection.execute(
            "SELECT * FROM distillation_invocation WHERE id = ?",
            (invocation_id,),
        ).fetchone()
        if invocation_row is None:
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation invocation is missing before formalization"
            )
        if (
            str(invocation_row["source_import_id"]) != source_import_id
            or str(invocation_row["source_receipt_id"]) != source_receipt.receipt_id
            or str(invocation_row["source_receipt_hash"])
            != source_receipt.receipt_hash
            or str(invocation_row["source_packet_fingerprint"])
            != source_receipt.source_packet_fingerprint
            or str(invocation_row["knowledge_scope_id"]) != scope_id
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation invocation does not bind this source scope"
            )
        if (
            str(invocation_row["status"]) != "succeeded"
            or int(invocation_row["provider_replay_count"]) != 0
            or str(invocation_row["candidate_set_hash"]) != candidate_draft_hash
            or tuple(
                int(invocation_row[key])
                for key in ("work_count", "voice_count", "validator_count")
            )
            != (1, 1, 1)
            or invocation_row["last_turn_result_receipt_id"] is None
            or invocation_row["candidate_output_receipt_id"] is None
            or invocation_row["final_result_receipt_id"] is None
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep distillation invocation is not a complete final success"
            )

        provider_rows = connection.execute(
            """
            SELECT * FROM distillation_invocation_receipt
            WHERE invocation_id = ? AND kind = 'provider_turn_result'
            ORDER BY ordinal
            """,
            (invocation_id,),
        ).fetchall()
        provider_receipts = tuple(
            cls._distillation_receipt_from_row(
                row,
                expected_invocation_id=invocation_id,
                expected_kind=DistillationReceiptKind.PROVIDER_TURN_RESULT,
            )
            for row in provider_rows
        )
        if (
            len(provider_receipts) != int(invocation_row["provider_turn_count"])
            or not provider_receipts
            or not isinstance(
                provider_receipts[-1].payload,
                DistillationProviderTurnResultPayload,
            )
            or provider_receipts[-1].receipt_id
            != str(invocation_row["last_turn_result_receipt_id"])
            or provider_receipts[-1].receipt_hash
            != str(invocation_row["last_turn_result_receipt_hash"])
            or distillation_provider_turn_chain_hash(provider_receipts)
            != str(invocation_row["provider_turn_chain_hash"])
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep Provider result evidence changed before formalization"
            )

        candidate_output_receipt = cls._distillation_receipt_from_row(
            connection.execute(
                "SELECT * FROM distillation_invocation_receipt WHERE id = ?",
                (str(invocation_row["candidate_output_receipt_id"]),),
            ).fetchone(),
            expected_invocation_id=invocation_id,
            expected_kind=DistillationReceiptKind.CANDIDATE_OUTPUT,
            expected_receipt_id=str(invocation_row["candidate_output_receipt_id"]),
            expected_receipt_hash=str(invocation_row["candidate_output_receipt_hash"]),
        )
        candidate_payload = candidate_output_receipt.payload
        if not isinstance(candidate_payload, DistillationCandidateOutputPayload):
            raise KnowledgeStoreConflict(
                "Runtime-deep candidate output receipt has the wrong payload type"
            )
        if (
            candidate_payload.final_turn_result_receipt_id
            != str(invocation_row["last_turn_result_receipt_id"])
            or candidate_payload.final_turn_result_receipt_hash
            != str(invocation_row["last_turn_result_receipt_hash"])
            or candidate_payload.provider_turn_count
            != int(invocation_row["provider_turn_count"])
            or candidate_payload.provider_turn_chain_hash
            != str(invocation_row["provider_turn_chain_hash"])
            or candidate_payload.source_receipt_id != source_receipt.receipt_id
            or candidate_payload.source_receipt_hash != source_receipt.receipt_hash
            or candidate_payload.candidate_set.candidate_set_hash
            != candidate_draft_hash
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep candidate output evidence changed before formalization"
            )

        final_receipt = cls._distillation_receipt_from_row(
            connection.execute(
                "SELECT * FROM distillation_invocation_receipt WHERE id = ?",
                (str(invocation_row["final_result_receipt_id"]),),
            ).fetchone(),
            expected_invocation_id=invocation_id,
            expected_kind=DistillationReceiptKind.FINAL_RESULT,
            expected_receipt_id=str(invocation_row["final_result_receipt_id"]),
            expected_receipt_hash=str(invocation_row["final_result_receipt_hash"]),
        )
        final_payload = final_receipt.payload
        if not isinstance(final_payload, DistillationFinalResultPayload):
            raise KnowledgeStoreConflict(
                "Runtime-deep final receipt has the wrong payload type"
            )
        if (
            final_payload.outcome != "succeeded"
            or final_payload.provider_turn_count
            != int(invocation_row["provider_turn_count"])
            or final_payload.provider_turn_chain_hash
            != str(invocation_row["provider_turn_chain_hash"])
            or final_payload.candidate_output_receipt_id
            != candidate_output_receipt.receipt_id
            or final_payload.candidate_output_receipt_hash
            != candidate_output_receipt.receipt_hash
            or final_payload.candidate_set_hash != candidate_draft_hash
            or (
                final_payload.work_count,
                final_payload.voice_count,
                final_payload.validator_count,
            )
            != (1, 1, 1)
        ):
            raise KnowledgeStoreConflict(
                "Runtime-deep final result evidence changed before formalization"
            )

        candidate_cards = candidate_payload.candidate_set.cards
        final_candidates = final_payload.candidates
        expected_types = ("work", "voice", "validator")
        if len(candidate_cards) != 3 or len(final_candidates) != 3:
            raise KnowledgeStoreConflict(
                "Runtime-deep final result does not bind exactly three candidates"
            )
        for index, (candidate, candidate_card, final_candidate) in enumerate(
            zip(indexed_candidates, candidate_cards, final_candidates, strict=True)
        ):
            origin = candidate.origin
            if (
                origin is None
                or candidate.source_card_index != index
                or candidate.card_type != expected_types[index]
                or candidate_card.output_index != index
                or candidate_card.candidate_id != candidate.candidate_id
                or candidate_card.card_type.value != candidate.card_type
                or candidate_card.card_hash != origin.source_card_hash
                or final_candidate.candidate_id != candidate.candidate_id
                or final_candidate.card_type != candidate.card_type
                or final_candidate.content_hash != origin.source_card_hash
            ):
                raise KnowledgeStoreConflict(
                    "Runtime-deep final candidate evidence does not match this batch"
                )

    def get_formalization_receipt(
        self,
        source_import_id: str,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2 | None:
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            source_scope = connection.execute(
                "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
                (source_import_id,),
            ).fetchone()
            row = connection.execute(
                """
                SELECT * FROM knowledge_formalization_receipt
                WHERE source_import_id = ?
                """,
                (source_import_id,),
            ).fetchone()
        if source_scope is not None and str(source_scope["scope_id"]) != normalized_scope:
            raise KnowledgeStoreConflict(
                "source import is bound to another knowledge scope"
            )
        if row is None:
            return None
        if source_scope is None:
            raise KnowledgeStoreError(
                "formalized source import has no immutable knowledge scope binding"
            )
        return self._formalization_from_row(row, expected_scope_id=normalized_scope)

    def finalize_source_import(
        self,
        source_import_id: str,
        *,
        expected_source_receipt_hash: str,
        expected_draft_hash: str,
        expected_candidate_set_hash: str,
        source_state_root: Path,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        now = datetime.now(UTC)
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                scope_row = self._ensure_scope_in_connection(connection, normalized_scope)
                source_scope = connection.execute(
                    "SELECT * FROM knowledge_source_scope WHERE source_import_id = ?",
                    (source_import_id,),
                ).fetchone()
                if source_scope is None:
                    raise KnowledgeStoreConflict(
                        "source import candidates must be imported before formalization"
                    )
                if str(source_scope["scope_id"]) != normalized_scope:
                    raise KnowledgeStoreConflict(
                        "source import is bound to another knowledge scope"
                    )
                existing = connection.execute(
                    """
                    SELECT * FROM knowledge_formalization_receipt
                    WHERE source_import_id = ?
                    """,
                    (source_import_id,),
                ).fetchone()
                if existing is not None:
                    receipt = self._formalization_from_row(
                        existing,
                        expected_scope_id=normalized_scope,
                    )
                    if (
                        receipt.source_receipt_hash != expected_source_receipt_hash
                        or receipt.cards_draft_hash != expected_draft_hash
                        or receipt.candidate_set_hash != expected_candidate_set_hash
                    ):
                        raise KnowledgeStoreConflict(
                            "knowledge formalization already exists for different material"
                        )
                    connection.commit()
                    return receipt

                job_row = connection.execute(
                    "SELECT * FROM source_import_job WHERE id = ?",
                    (source_import_id,),
                ).fetchone()
                if job_row is None:
                    raise KnowledgeStoreNotFound("source import was not found")
                job = parse_source_import_job(
                    SqliteStore._source_import_job_payload_from_row(job_row)
                )
                if job.status is not SourceImportStatus.AWAITING_REVIEW:
                    raise KnowledgeStoreConflict(
                        "only an awaiting-review source import can be formalized"
                    )
                if job.distillation_draft is None:
                    raise KnowledgeStoreConflict(
                        "source import has no distillation draft to formalize"
                    )
                source_receipt_row = connection.execute(
                    """
                    SELECT * FROM source_import_execution_receipt
                    WHERE source_import_id = ?
                    """,
                    (source_import_id,),
                ).fetchone()
                if source_receipt_row is None:
                    raise KnowledgeStoreConflict(
                        "source import has no verified execution receipt"
                    )
                if any(
                    source_receipt_row[field] is None
                    for field in ("run_id", "revision", "idempotency_key")
                ):
                    raise KnowledgeStoreConflict(
                        "source import has no verified execution identity"
                    )
                source_receipt = parse_source_import_execution_receipt(
                    json.loads(str(source_receipt_row["payload_json"]))
                )
                try:
                    validate_source_import_receipt_job(source_receipt, job)
                except ValueError as exc:
                    raise KnowledgeStoreConflict(
                        "source import job request does not match its execution receipt"
                    ) from exc
                if (
                    str(source_receipt_row["id"]) != source_receipt.receipt_id
                    or str(source_receipt_row["receipt_hash"])
                    != source_receipt.receipt_hash
                    or str(source_receipt_row["source_import_id"])
                    != source_receipt.source_import_id
                    or source_receipt.source_import_id != source_import_id
                    or str(source_receipt_row["run_id"]) != source_receipt.run_id
                    or int(source_receipt_row["revision"]) != source_receipt.revision
                    or str(source_receipt_row["idempotency_key"])
                    != source_receipt.idempotency_key
                    or str(source_receipt_row["request_hash"])
                    != source_receipt.request_hash
                    or str(source_receipt_row["prepare_invocation_id"])
                    != source_receipt.prepare_invocation_id
                    or str(source_receipt_row["prepare_tool_fingerprint"])
                    != source_receipt.prepare_tool.tool_fingerprint
                    or str(source_receipt_row["source_packet_fingerprint"])
                    != source_receipt.source_packet_fingerprint
                    or str(source_receipt_row["distillation_invocation_id"])
                    != source_receipt.distillation_invocation_id
                    or str(source_receipt_row["distillation_tool_fingerprint"])
                    != source_receipt.distillation_tool.tool_fingerprint
                    or str(source_receipt_row["distillation_input_hash"])
                    != source_receipt.distillation_input_hash
                    or str(source_receipt_row["distillation_output_hash"])
                    != source_receipt.distillation_output_hash
                    or str(source_receipt_row["draft_fingerprint"])
                    != source_receipt.draft_fingerprint
                    or str(source_receipt_row["cards_draft_hash"])
                    != source_receipt.cards_draft_hash
                    or str(source_receipt_row["envelope_hash"])
                    != source_receipt.envelope_hash
                    or not self._stored_datetime_matches(
                        source_receipt_row["created_at"],
                        source_receipt.created_at,
                    )
                ):
                    raise KnowledgeStoreError(
                        "source import receipt columns disagree with its payload"
                    )
                if source_receipt.receipt_hash != expected_source_receipt_hash:
                    raise KnowledgeStoreConflict(
                        "source import execution receipt changed before formalization"
                    )
                verified_cards_payload = self._verify_source_receipt_artifacts(
                    source_state_root,
                    source_receipt,
                )
                candidate_rows = connection.execute(
                    """
                    SELECT candidate.*
                    FROM knowledge_candidate AS candidate
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE candidate.source_import_id = ?
                      AND source_scope.scope_id = ?
                    ORDER BY candidate.source_card_index, candidate.id
                    """,
                    (source_import_id, normalized_scope),
                ).fetchall()
                try:
                    candidates = [
                        self._candidate_from_row(row) for row in candidate_rows
                    ]
                except (json.JSONDecodeError, ValueError, KnowledgeStoreError) as exc:
                    raise KnowledgeStoreConflict(
                        "knowledge candidate storage changed before formalization"
                    ) from exc
                if not candidates:
                    raise KnowledgeStoreConflict(
                        "source import candidates must be imported before formalization"
                    )
                indexes = tuple(
                    sorted(candidate.source_card_index for candidate in candidates)
                )
                indexed_candidates = tuple(
                    sorted(candidates, key=lambda candidate: candidate.source_card_index)
                )
                runtime_invocation_values = tuple(
                    row["runtime_distillation_invocation_id"] for row in candidate_rows
                )
                has_runtime_deep_marker = any(
                    value is not None for value in runtime_invocation_values
                )
                runtime_invocation_ids = {
                    str(value)
                    for value in runtime_invocation_values
                    if value is not None
                }
                runtime_invocation_id: str | None = (
                    next(iter(runtime_invocation_ids))
                    if len(runtime_invocation_ids) == 1
                    else None
                )
                runtime_deep_shape_valid = (
                    len(candidates) == 3
                    and indexes == (0, 1, 2)
                    and tuple(candidate.card_type for candidate in indexed_candidates)
                    == ("work", "voice", "validator")
                )
                if has_runtime_deep_marker and (
                    not runtime_deep_shape_valid
                    or runtime_invocation_id is None
                    or any(
                        value is None or str(value) != runtime_invocation_id
                        for value in runtime_invocation_values
                    )
                ):
                    raise KnowledgeStoreConflict(
                        "Runtime-deep candidates must all bind one complete distillation invocation"
                    )
                runtime_deep_batch = has_runtime_deep_marker
                candidate_draft_hash = candidates[0].source_draft_hash
                if any(
                    candidate.source_draft_hash != candidate_draft_hash
                    for candidate in candidates
                ):
                    raise KnowledgeStoreConflict(
                        "knowledge candidates do not share one immutable draft"
                    )
                if runtime_deep_batch:
                    if candidate_draft_hash != expected_draft_hash:
                        raise KnowledgeStoreConflict(
                            "distillation draft changed before formalization"
                        )
                elif (
                    source_receipt.cards_draft_hash != expected_draft_hash
                    or job.distillation_draft.card_count != source_receipt.card_count
                ):
                    raise KnowledgeStoreConflict(
                        "distillation draft changed before formalization"
                    )
                if (
                    not runtime_deep_batch
                    and (
                        len(candidates) != source_receipt.card_count
                        or indexes != tuple(range(source_receipt.card_count))
                    )
                ):
                    raise KnowledgeStoreConflict(
                        "knowledge candidates do not cover the complete distillation draft"
                    )
                for candidate in candidates:
                    origin = candidate.origin
                    if (
                        origin is None
                        or origin.source_receipt_id != source_receipt.receipt_id
                        or origin.source_receipt_hash != source_receipt.receipt_hash
                        or origin.source_packet_fingerprint
                        != source_receipt.source_packet_fingerprint
                        or (
                            not runtime_deep_batch
                            and origin.draft_fingerprint != source_receipt.draft_fingerprint
                        )
                        or origin.cards_draft_hash != candidate_draft_hash
                        or candidate.source_draft_hash
                        != candidate_draft_hash
                    ):
                        raise KnowledgeStoreConflict(
                            "knowledge candidate origin does not match this source batch"
                        )
                if runtime_deep_batch:
                    if runtime_invocation_id is None:
                        raise KnowledgeStoreConflict(
                            "Runtime-deep candidates must all bind one complete distillation invocation"
                        )
                    self._verify_runtime_deep_finalization_evidence(
                        connection,
                        invocation_id=runtime_invocation_id,
                        scope_id=normalized_scope,
                        source_import_id=source_import_id,
                        source_receipt=source_receipt,
                        candidate_draft_hash=candidate_draft_hash,
                        indexed_candidates=indexed_candidates,
                    )
                if not runtime_deep_batch:
                    try:
                        raw_cards = json.loads(verified_cards_payload.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        raise KnowledgeStoreConflict(
                            "verified cards draft is no longer valid UTF-8 JSON"
                        ) from exc
                    if not isinstance(raw_cards, list) or len(raw_cards) != len(candidates):
                        raise KnowledgeStoreConflict(
                            "verified cards draft does not match the candidate batch"
                        )
                    for candidate in candidates:
                        if candidate.origin is None or canonical_json_hash(
                            raw_cards[candidate.source_card_index]
                        ) != candidate.origin.source_card_hash:
                            raise KnowledgeStoreConflict(
                                "knowledge candidate origin does not match its source card"
                            )
                candidate_set_hash = knowledge_candidate_set_hash(candidates)
                if candidate_set_hash != expected_candidate_set_hash:
                    raise KnowledgeStoreConflict(
                        "knowledge candidate set changed before formalization"
                    )

                decisions: list[KnowledgeFormalizationDecision] = []
                promoted_targets: dict[str, tuple[str, str, int]] = {}
                for candidate in candidates:
                    if candidate.lineage_status is not KnowledgeLineageStatus.VERIFIED:
                        raise KnowledgeStoreConflict(
                            "legacy-unverified candidates cannot be formalized"
                        )
                    if candidate.status is KnowledgeCandidateStatus.PENDING:
                        raise KnowledgeStoreConflict(
                            "all knowledge candidates require a terminal decision"
                        )
                    if candidate.origin is None:
                        raise KnowledgeStoreError(
                            "verified knowledge candidate origin is unavailable"
                        )
                    decision_values: dict[str, object] = {
                        "candidate_id": candidate.candidate_id,
                        "source_card_index": candidate.source_card_index,
                        "origin_hash": candidate.origin.origin_hash,
                        "decision": candidate.status.value,
                        "revision": candidate.revision,
                        "content_hash": candidate.content_hash,
                    }
                    if candidate.status is KnowledgeCandidateStatus.PROMOTED:
                        version_row = connection.execute(
                            "SELECT * FROM knowledge_card_version WHERE id = ?",
                            (candidate.promoted_version_id,),
                        ).fetchone()
                        if version_row is None:
                            raise KnowledgeStoreError(
                                "promoted candidate version is unavailable"
                            )
                        version = self._version_from_row(
                            connection,
                            version_row,
                            expected_scope_id=normalized_scope,
                        )
                        if (
                            version.lineage is None
                            or version.lineage.candidate_id != candidate.candidate_id
                            or version.content_hash != candidate.content_hash
                        ):
                            raise KnowledgeStoreError(
                                "promoted version lineage does not match its candidate"
                            )
                        if candidate.card_id in promoted_targets:
                            raise KnowledgeStoreConflict(
                                "a source batch cannot promote the same card identity twice"
                            )
                        promoted_targets[candidate.card_id] = (
                            version.version_id,
                            version.lineage.activation_expected_version_id,
                            version.lineage.activation_expected_state_revision,
                        )
                        decision_values.update(
                            promoted_version_id=version.version_id,
                            version_lineage_receipt_id=(
                                version.lineage.lineage_receipt_id
                            ),
                            version_lineage_receipt_hash=version.lineage.receipt_hash,
                        )
                    decisions.append(KnowledgeFormalizationDecision(**decision_values))

                receipt_id = knowledge_formalization_receipt_id(source_import_id)
                for card_id, target in promoted_targets.items():
                    version_id, expected_version_id, expected_state_revision = target
                    result = connection.execute(
                        """
                        UPDATE knowledge_scope_card
                        SET active_version_id = ?, status = 'active',
                            state_revision = CASE
                                WHEN active_version_id = ? THEN state_revision
                                ELSE state_revision + 1
                            END,
                            updated_at = ?
                        WHERE scope_id = ? AND card_id = ?
                          AND active_version_id = ?
                          AND state_revision = ?
                        """,
                        (
                            version_id,
                            version_id,
                            now.isoformat(),
                            normalized_scope,
                            card_id,
                            expected_version_id,
                            expected_state_revision,
                        ),
                    )
                    if result.rowcount != 1:
                        raise KnowledgeStoreConflict(
                            "knowledge card changed after promotion; reject the staged candidate or retry the source batch"
                        )
                    if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                        legacy_result = connection.execute(
                            """
                            UPDATE knowledge_card
                            SET active_version_id = ?, status = 'active',
                                state_revision = CASE
                                    WHEN active_version_id = ? THEN state_revision
                                    ELSE state_revision + 1
                                END,
                                updated_at = ?
                            WHERE card_id = ?
                              AND active_version_id = ?
                              AND state_revision = ?
                            """,
                            (
                                version_id,
                                version_id,
                                now.isoformat(),
                                card_id,
                                expected_version_id,
                                expected_state_revision,
                            ),
                        )
                        if legacy_result.rowcount != 1:
                            raise KnowledgeStoreError(
                                "application knowledge mapping disagrees with its legacy card"
                            )
                if promoted_targets:
                    scope_row = self._touch_scope_in_connection(
                        connection,
                        normalized_scope,
                        now.isoformat(),
                    )
                cards = self._list_cards_in_connection(
                    connection,
                    scope_id=normalized_scope,
                    include_application=(
                        normalized_scope != APPLICATION_KNOWLEDGE_SCOPE_ID
                    ),
                )
                knowledge_state_hash = native_knowledge_state_hash(cards)
                receipt_material = {
                    "formalization_receipt_id": receipt_id,
                    "source_import_id": source_import_id,
                    "source_receipt_id": source_receipt.receipt_id,
                    "source_receipt_hash": source_receipt.receipt_hash,
                    "source_packet_fingerprint": source_receipt.source_packet_fingerprint,
                    "draft_fingerprint": (
                        candidates[0].origin.draft_fingerprint
                        if runtime_deep_batch and candidates[0].origin is not None
                        else source_receipt.draft_fingerprint
                    ),
                    "cards_draft_hash": candidate_draft_hash,
                    "candidate_set_hash": candidate_set_hash,
                    "decisions": tuple(sorted(decisions, key=lambda item: item.candidate_id)),
                    "knowledge_state_hash": knowledge_state_hash,
                    "created_at": now,
                }
                if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                    provisional = KnowledgeFormalizationReceipt.model_construct(
                        **receipt_material,
                        receipt_hash="0" * 64,
                    )
                    receipt = KnowledgeFormalizationReceipt(
                        **receipt_material,
                        receipt_hash=knowledge_formalization_receipt_hash(provisional),
                    )
                    receipt_scope_id: str | None = None
                else:
                    scope_ref = self._scope_ref_from_row(scope_row)
                    scoped_material = {**receipt_material, "scope": scope_ref}
                    provisional = KnowledgeFormalizationReceiptV2.model_construct(
                        **scoped_material,
                        receipt_hash="0" * 64,
                    )
                    receipt = KnowledgeFormalizationReceiptV2(
                        **scoped_material,
                        receipt_hash=knowledge_formalization_receipt_hash(provisional),
                    )
                    receipt_scope_id = normalized_scope
                connection.execute(
                    """
                    INSERT INTO knowledge_formalization_receipt(
                        id, source_import_id, source_receipt_id,
                        source_receipt_hash, cards_draft_hash,
                        candidate_set_hash, knowledge_state_hash,
                        receipt_hash, payload_json, created_at, scope_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.formalization_receipt_id,
                        source_import_id,
                        receipt.source_receipt_id,
                        receipt.source_receipt_hash,
                        receipt.cards_draft_hash,
                        receipt.candidate_set_hash,
                        receipt.knowledge_state_hash,
                        receipt.receipt_hash,
                        self._json(receipt.model_dump(mode="json", by_alias=True)),
                        receipt.created_at.isoformat(),
                        receipt_scope_id,
                    ),
                )
                completed = job.model_copy(
                    update={
                        "status": SourceImportStatus.COMPLETED,
                        "progress": SourceImportProgress(
                            stage="completed",
                            message="Knowledge review decisions were formalized by the Runtime",
                            percent=100,
                        ),
                        "updated_at": now,
                    }
                )
                completed = parse_source_import_job(
                    completed.model_dump(mode="json", by_alias=True)
                )
                result = connection.execute(
                    """
                    UPDATE source_import_job
                    SET status = 'completed', payload_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'awaiting_review'
                    """,
                    (
                        self._json(completed.model_dump(mode="json", by_alias=True)),
                        completed.updated_at.isoformat(),
                        source_import_id,
                    ),
                )
                if result.rowcount != 1:
                    raise KnowledgeStoreConflict(
                        "source import changed before formalization commit"
                    )
                connection.commit()
                return receipt
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise KnowledgeStoreConflict(
                    "knowledge formalization conflicted with existing state"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def set_card_status(
        self,
        card_id: str,
        *,
        status: KnowledgeCardStatus,
        expected_state_revision: int,
        expected_active_version_id: str,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_scope_in_connection(connection, normalized_scope)
                current = connection.execute(
                    """
                    SELECT * FROM knowledge_scope_card
                    WHERE scope_id = ? AND card_id = ?
                    """,
                    (normalized_scope, card_id),
                ).fetchone()
                if current is None:
                    raise KnowledgeStoreNotFound("knowledge card was not found")
                if status is KnowledgeCardStatus.ACTIVE:
                    active_row = connection.execute(
                        "SELECT * FROM knowledge_card_version WHERE id = ?",
                        (str(current["active_version_id"]),),
                    ).fetchone()
                    if active_row is None:
                        raise KnowledgeStoreError(
                            "knowledge card active version is missing"
                        )
                    active_version = self._version_from_row(
                        connection,
                        active_row,
                        expected_scope_id=normalized_scope,
                    )
                    if (
                        active_version.formalization_status
                        is not KnowledgeVersionFormalizationStatus.VERIFIED
                    ):
                        raise KnowledgeStoreConflict(
                            "unformalized knowledge versions cannot be enabled"
                        )
                result = connection.execute(
                    """
                    UPDATE knowledge_scope_card
                    SET status = ?, state_revision = state_revision + 1, updated_at = ?
                    WHERE scope_id = ? AND card_id = ?
                      AND state_revision = ? AND active_version_id = ?
                    """,
                    (
                        status.value,
                        now,
                        normalized_scope,
                        card_id,
                        expected_state_revision,
                        expected_active_version_id,
                    ),
                )
                if result.rowcount != 1:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM knowledge_scope_card
                        WHERE scope_id = ? AND card_id = ?
                        """,
                        (normalized_scope, card_id),
                    ).fetchone()
                    if exists is None:
                        raise KnowledgeStoreNotFound("knowledge card was not found")
                    raise KnowledgeStoreConflict("knowledge card changed before this state update")
                if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                    legacy_result = connection.execute(
                        """
                        UPDATE knowledge_card
                        SET status = ?, state_revision = state_revision + 1,
                            updated_at = ?
                        WHERE card_id = ? AND state_revision = ?
                          AND active_version_id = ?
                        """,
                        (
                            status.value,
                            now,
                            card_id,
                            expected_state_revision,
                            expected_active_version_id,
                        ),
                    )
                    if legacy_result.rowcount != 1:
                        raise KnowledgeStoreError(
                            "application knowledge mapping disagrees with its legacy card"
                        )
                self._touch_scope_in_connection(connection, normalized_scope, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_card(card_id, scope_id=normalized_scope)

    def rollback_card(
        self,
        card_id: str,
        *,
        target_version_id: str,
        expected_state_revision: int,
        expected_active_version_id: str,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        now = datetime.now(UTC).isoformat()
        normalized_scope = self._normalize_scope_id(scope_id)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_scope_in_connection(connection, normalized_scope)
                target = connection.execute(
                    """
                    SELECT version.*
                    FROM knowledge_card_version AS version
                    JOIN knowledge_candidate AS candidate
                      ON candidate.id = version.source_candidate_id
                    JOIN knowledge_source_scope AS source_scope
                      ON source_scope.source_import_id = candidate.source_import_id
                    WHERE version.id = ? AND version.card_id = ?
                      AND source_scope.scope_id = ?
                    """,
                    (target_version_id, card_id, normalized_scope),
                ).fetchone()
                if target is None:
                    raise KnowledgeStoreNotFound("target knowledge version was not found")
                target_version = self._version_from_row(
                    connection,
                    target,
                    expected_scope_id=normalized_scope,
                )
                if (
                    target_version.formalization_status
                    is not KnowledgeVersionFormalizationStatus.VERIFIED
                ):
                    raise KnowledgeStoreConflict(
                        "unformalized knowledge versions cannot be activated by rollback"
                    )
                result = connection.execute(
                    """
                    UPDATE knowledge_scope_card
                    SET active_version_id = ?, status = 'active',
                        state_revision = state_revision + 1, updated_at = ?
                    WHERE scope_id = ? AND card_id = ?
                      AND state_revision = ? AND active_version_id = ?
                    """,
                    (
                        target_version_id,
                        now,
                        normalized_scope,
                        card_id,
                        expected_state_revision,
                        expected_active_version_id,
                    ),
                )
                if result.rowcount != 1:
                    exists = connection.execute(
                        """
                        SELECT 1 FROM knowledge_scope_card
                        WHERE scope_id = ? AND card_id = ?
                        """,
                        (normalized_scope, card_id),
                    ).fetchone()
                    if exists is None:
                        raise KnowledgeStoreNotFound("knowledge card was not found")
                    raise KnowledgeStoreConflict("knowledge card changed before rollback")
                if normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
                    legacy_result = connection.execute(
                        """
                        UPDATE knowledge_card
                        SET active_version_id = ?, status = 'active',
                            state_revision = state_revision + 1, updated_at = ?
                        WHERE card_id = ? AND state_revision = ?
                          AND active_version_id = ?
                        """,
                        (
                            target_version_id,
                            now,
                            card_id,
                            expected_state_revision,
                            expected_active_version_id,
                        ),
                    )
                    if legacy_result.rowcount != 1:
                        raise KnowledgeStoreError(
                            "application knowledge mapping disagrees with its legacy card"
                        )
                self._touch_scope_in_connection(connection, normalized_scope, now)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_card(card_id, scope_id=normalized_scope)

    def _get_card_in_connection(
        self,
        connection: sqlite3.Connection,
        card_id: str,
        *,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCard:
        normalized_scope = self._normalize_scope_id(scope_id)
        self._ensure_scope_in_connection(connection, normalized_scope)
        scoped = connection.execute(
            """
            SELECT * FROM knowledge_scope_card
            WHERE scope_id = ? AND card_id = ?
            """,
            (normalized_scope, card_id),
        ).fetchone()
        if scoped is None and normalized_scope == APPLICATION_KNOWLEDGE_SCOPE_ID:
            # Preserve read compatibility for databases populated by an older
            # test/utility that inserted a legacy card directly.
            legacy = connection.execute(
                "SELECT * FROM knowledge_card WHERE card_id = ?",
                (card_id,),
            ).fetchone()
            if legacy is not None and legacy["active_version_id"] is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO knowledge_scope_card(
                        scope_id, card_id, status, state_revision,
                        active_version_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        APPLICATION_KNOWLEDGE_SCOPE_ID,
                        card_id,
                        str(legacy["status"]),
                        int(legacy["state_revision"]),
                        str(legacy["active_version_id"]),
                        str(legacy["created_at"]),
                        str(legacy["updated_at"]),
                    ),
                )
                scoped = connection.execute(
                    """
                    SELECT * FROM knowledge_scope_card
                    WHERE scope_id = ? AND card_id = ?
                    """,
                    (normalized_scope, card_id),
                ).fetchone()
        if scoped is None:
            raise KnowledgeStoreNotFound("knowledge card was not found")
        row = connection.execute(
            "SELECT * FROM knowledge_card WHERE card_id = ?",
            (card_id,),
        ).fetchone()
        if row is None:
            raise KnowledgeStoreError("scoped knowledge card has no version owner")
        version_rows = connection.execute(
            """
            SELECT version.*
            FROM knowledge_card_version AS version
            JOIN knowledge_candidate AS candidate
              ON candidate.id = version.source_candidate_id
            JOIN knowledge_source_scope AS source_scope
              ON source_scope.source_import_id = candidate.source_import_id
            WHERE version.card_id = ? AND source_scope.scope_id = ?
            ORDER BY version.version_number DESC
            """,
            (card_id, normalized_scope),
        ).fetchall()
        versions = [
            self._version_from_row(
                connection,
                item,
                expected_scope_id=normalized_scope,
            )
            for item in version_rows
        ]
        active_version_id = str(scoped["active_version_id"] or "")
        active = next(
            (item for item in versions if item.version_id == active_version_id),
            None,
        )
        if active is None:
            raise KnowledgeStoreError("knowledge card active version is missing")
        return KnowledgeCard(
            card_id=card_id,
            status=str(scoped["status"]),
            state_revision=int(scoped["state_revision"]),
            active_version_id=active_version_id,
            active_version=active,
            versions=versions,
            created_at=str(scoped["created_at"]),
            updated_at=str(scoped["updated_at"]),
        )

    def _list_cards_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        active_only: bool = False,
        scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
        include_application: bool = False,
    ) -> list[KnowledgeCard]:
        normalized_scope = self._normalize_scope_id(scope_id)
        self._ensure_scope_in_connection(connection, normalized_scope)
        status_clause = " AND status = 'active'" if active_only else ""
        rows = connection.execute(
            f"""
            SELECT card_id, scope_id, updated_at
            FROM knowledge_scope_card
            WHERE scope_id = ?{status_clause}
            ORDER BY updated_at DESC, card_id
            """,
            (normalized_scope,),
        ).fetchall()
        selected: list[tuple[str, str, str]] = [
            (str(row["card_id"]), str(row["scope_id"]), str(row["updated_at"]))
            for row in rows
        ]
        if include_application and normalized_scope != APPLICATION_KNOWLEDGE_SCOPE_ID:
            inherited = connection.execute(
                f"""
                SELECT app.card_id, app.scope_id, app.updated_at
                FROM knowledge_scope_card AS app
                WHERE app.scope_id = ?{status_clause.replace('status', 'app.status')}
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_scope_card AS own
                      WHERE own.scope_id = ? AND own.card_id = app.card_id
                  )
                ORDER BY app.updated_at DESC, app.card_id
                """,
                (APPLICATION_KNOWLEDGE_SCOPE_ID, normalized_scope),
            ).fetchall()
            selected.extend(
                [
                    (str(row["card_id"]), str(row["scope_id"]), str(row["updated_at"]))
                    for row in inherited
                ]
            )
        selected.sort(key=lambda item: (item[2], item[0]), reverse=True)
        return [
            self._get_card_in_connection(connection, card_id, scope_id=origin_scope)
            for card_id, origin_scope, _ in selected
        ]

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> KnowledgeCandidate:
        card = json.loads(str(row["payload_json"]))
        if canonical_json_hash(card) != str(row["content_hash"]):
            raise KnowledgeStoreError("knowledge candidate payload hash does not match storage")
        origin = KnowledgeStore._origin_from_candidate_row(row)
        return KnowledgeCandidate(
            candidate_id=str(row["id"]),
            source_import_id=str(row["source_import_id"]),
            source_card_index=int(row["source_card_index"]),
            source_draft_hash=str(row["source_draft_hash"]),
            lineage_status=str(row["lineage_status"]),
            origin=origin,
            runtime_distillation_invocation_id=(
                str(row["runtime_distillation_invocation_id"])
                if row["runtime_distillation_invocation_id"] is not None
                else None
            ),
            card_id=str(row["card_id"]),
            card_type=str(row["card_type"]),
            status=str(row["status"]),
            revision=int(row["revision"]),
            content_hash=str(row["content_hash"]),
            card=card,
            issues=[
                KnowledgeReviewIssue.model_validate(item)
                for item in json.loads(str(row["issues_json"]))
            ],
            promoted_version_id=(
                str(row["promoted_version_id"])
                if row["promoted_version_id"] is not None
                else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _origin_from_candidate_row(row: sqlite3.Row) -> KnowledgeCandidateOrigin | None:
        raw = row["origin_json"]
        if raw is None:
            if row["origin_hash"] is not None:
                raise KnowledgeStoreError("knowledge candidate origin columns disagree")
            return None
        origin = KnowledgeCandidateOrigin.model_validate_json(str(raw))
        if str(row["origin_hash"] or "") != origin.origin_hash:
            raise KnowledgeStoreError("knowledge candidate origin hash column disagrees")
        return origin

    @staticmethod
    def _version_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        expected_scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeCardVersion:
        normalized_scope = KnowledgeStore._normalize_scope_id(expected_scope_id)
        card = json.loads(str(row["payload_json"]))
        if canonical_json_hash(card) != str(row["content_hash"]):
            raise KnowledgeStoreError("knowledge version payload hash does not match storage")
        lineage_row = connection.execute(
            "SELECT * FROM knowledge_version_lineage_receipt WHERE version_id = ?",
            (str(row["id"]),),
        ).fetchone()
        lineage = None
        lineage_status = KnowledgeLineageStatus.LEGACY_UNVERIFIED
        formalization = None
        formalization_status = KnowledgeVersionFormalizationStatus.LEGACY_UNVERIFIED
        if lineage_row is not None:
            try:
                lineage_payload = json.loads(str(lineage_row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise KnowledgeStoreError("knowledge lineage receipt payload is invalid") from exc
            lineage_model = (
                KnowledgeVersionLineageReceiptV2
                if isinstance(lineage_payload, dict)
                and lineage_payload.get("schema") == "zhimeng.knowledge-version-lineage-receipt.v2"
                else KnowledgeVersionLineageReceipt
            )
            lineage = lineage_model.model_validate(lineage_payload)
            stored_scope_id = (
                str(lineage_row["scope_id"])
                if lineage_row["scope_id"] is not None
                else None
            )
            if isinstance(lineage, KnowledgeVersionLineageReceiptV2):
                if stored_scope_id != lineage.scope.scope_id:
                    raise KnowledgeStoreError(
                        "knowledge lineage scope columns disagree with its payload"
                    )
                if lineage.scope.scope_id != normalized_scope:
                    raise KnowledgeStoreError(
                        "knowledge lineage receipt belongs to another knowledge scope"
                    )
            elif stored_scope_id is not None or normalized_scope != APPLICATION_KNOWLEDGE_SCOPE_ID:
                raise KnowledgeStoreError(
                    "legacy v1 knowledge lineage is not bound to this project scope"
                )
            if (
                str(lineage_row["id"]) != lineage.lineage_receipt_id
                or str(lineage_row["version_id"]) != lineage.version_id
                or str(lineage_row["candidate_id"]) != lineage.candidate_id
                or str(lineage_row["source_import_id"]) != lineage.source_import_id
                or str(lineage_row["source_receipt_id"])
                != lineage.candidate_origin.source_receipt_id
                or str(lineage_row["source_receipt_hash"])
                != lineage.candidate_origin.source_receipt_hash
                or str(lineage_row["candidate_origin_hash"])
                != lineage.candidate_origin.origin_hash
                or int(lineage_row["approved_candidate_revision"])
                != lineage.approved_candidate_revision
                or str(lineage_row["approved_candidate_content_hash"])
                != lineage.approved_candidate_content_hash
                or str(lineage_row["activation_mode"])
                != lineage.activation_mode
                or bool(lineage_row["replacement_authorized"])
                is not lineage.replacement_authorized
                or str(lineage_row["activation_expected_version_id"])
                != lineage.activation_expected_version_id
                or int(lineage_row["activation_expected_state_revision"])
                != lineage.activation_expected_state_revision
                or str(lineage_row["receipt_hash"]) != lineage.receipt_hash
                or not KnowledgeStore._stored_datetime_matches(
                    lineage_row["created_at"],
                    lineage.created_at,
                )
            ):
                raise KnowledgeStoreError("knowledge lineage receipt columns disagree")
            candidate_row = connection.execute(
                """
                SELECT candidate.status, candidate.promoted_version_id,
                       source_scope.scope_id
                FROM knowledge_candidate AS candidate
                JOIN knowledge_source_scope AS source_scope
                  ON source_scope.source_import_id = candidate.source_import_id
                WHERE candidate.id = ?
                """,
                (lineage.candidate_id,),
            ).fetchone()
            if candidate_row is None:
                raise KnowledgeStoreError("knowledge lineage candidate is unavailable")
            if str(candidate_row["scope_id"]) != normalized_scope:
                raise KnowledgeStoreError(
                    "knowledge lineage candidate belongs to another knowledge scope"
                )
            candidate_status = KnowledgeCandidateStatus(str(candidate_row["status"]))
            abandoned = candidate_status is KnowledgeCandidateStatus.REJECTED
            if not abandoned and (
                candidate_status is not KnowledgeCandidateStatus.PROMOTED
                or str(candidate_row["promoted_version_id"] or "") != lineage.version_id
            ):
                raise KnowledgeStoreError(
                    "knowledge lineage candidate disposition does not match its version"
                )
            lineage_status = KnowledgeLineageStatus.VERIFIED
            formalization_row = connection.execute(
                """
                SELECT * FROM knowledge_formalization_receipt
                WHERE source_import_id = ?
                """,
                (lineage.source_import_id,),
            ).fetchone()
            if formalization_row is None:
                formalization_status = (
                    KnowledgeVersionFormalizationStatus.ABANDONED
                    if abandoned
                    else KnowledgeVersionFormalizationStatus.AWAITING_FORMALIZATION
                )
            else:
                receipt = KnowledgeStore._formalization_from_row(
                    formalization_row,
                    expected_scope_id=normalized_scope,
                )
                decision = next(
                    (
                        item
                        for item in receipt.decisions
                        if item.candidate_id == lineage.candidate_id
                    ),
                    None,
                )
                if receipt.source_import_id != lineage.source_import_id or decision is None:
                    raise KnowledgeStoreError(
                        "knowledge formalization receipt does not bind its version"
                    )
                formalization = KnowledgeFormalizationRef(
                    formalization_receipt_id=receipt.formalization_receipt_id,
                    formalization_receipt_hash=receipt.receipt_hash,
                )
                if abandoned:
                    if decision.decision != KnowledgeCandidateStatus.REJECTED.value:
                        raise KnowledgeStoreError(
                            "knowledge formalization receipt does not preserve staged rejection"
                        )
                    formalization_status = KnowledgeVersionFormalizationStatus.ABANDONED
                else:
                    if (
                        decision.decision != KnowledgeCandidateStatus.PROMOTED.value
                        or decision.promoted_version_id != lineage.version_id
                        or decision.version_lineage_receipt_id
                        != lineage.lineage_receipt_id
                        or decision.version_lineage_receipt_hash != lineage.receipt_hash
                    ):
                        raise KnowledgeStoreError(
                            "knowledge formalization receipt does not bind its version"
                        )
                    formalization_status = KnowledgeVersionFormalizationStatus.VERIFIED
        return KnowledgeCardVersion(
            version_id=str(row["id"]),
            card_id=str(row["card_id"]),
            version_number=int(row["version_number"]),
            content_hash=str(row["content_hash"]),
            card=card,
            source_candidate_id=str(row["source_candidate_id"]),
            lineage_status=lineage_status,
            lineage=lineage,
            formalization_status=formalization_status,
            formalization=formalization,
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _formalization_from_row(
        row: sqlite3.Row,
        *,
        expected_scope_id: str = APPLICATION_KNOWLEDGE_SCOPE_ID,
    ) -> KnowledgeFormalizationReceipt | KnowledgeFormalizationReceiptV2:
        normalized_scope = KnowledgeStore._normalize_scope_id(expected_scope_id)
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KnowledgeStoreError(
                "knowledge formalization receipt payload is invalid"
            ) from exc
        receipt_model = (
            KnowledgeFormalizationReceiptV2
            if isinstance(payload, dict)
            and payload.get("schema") == "zhimeng.knowledge-formalization-receipt.v2"
            else KnowledgeFormalizationReceipt
        )
        receipt = receipt_model.model_validate(payload)
        stored_scope_id = str(row["scope_id"]) if row["scope_id"] is not None else None
        if isinstance(receipt, KnowledgeFormalizationReceiptV2):
            if stored_scope_id != receipt.scope.scope_id:
                raise KnowledgeStoreError(
                    "knowledge formalization scope columns disagree with its payload"
                )
            if receipt.scope.scope_id != normalized_scope:
                raise KnowledgeStoreError(
                    "knowledge formalization belongs to another knowledge scope"
                )
        elif stored_scope_id is not None or normalized_scope != APPLICATION_KNOWLEDGE_SCOPE_ID:
            raise KnowledgeStoreError(
                "legacy v1 formalization is not bound to this project scope"
            )
        if (
            str(row["id"]) != receipt.formalization_receipt_id
            or str(row["source_import_id"]) != receipt.source_import_id
            or str(row["source_receipt_id"]) != receipt.source_receipt_id
            or str(row["source_receipt_hash"]) != receipt.source_receipt_hash
            or str(row["cards_draft_hash"]) != receipt.cards_draft_hash
            or str(row["candidate_set_hash"]) != receipt.candidate_set_hash
            or str(row["knowledge_state_hash"]) != receipt.knowledge_state_hash
            or str(row["receipt_hash"]) != receipt.receipt_hash
            or not KnowledgeStore._stored_datetime_matches(
                row["created_at"],
                receipt.created_at,
            )
        ):
            raise KnowledgeStoreError(
                "knowledge formalization receipt columns disagree with its payload"
            )
        return receipt

    @staticmethod
    def _verify_source_receipt_artifacts(
        source_state_root: Path,
        receipt: AnySourceImportExecutionReceipt,
    ) -> bytes:
        try:
            root = source_state_root.resolve(strict=True)
        except OSError as exc:
            raise KnowledgeStoreConflict(
                "Runtime source state is unavailable during formalization"
            ) from exc
        cards_payload: bytes | None = None
        for artifact in (*receipt.source_artifacts, *receipt.draft_artifacts):
            if (
                artifact.kind == "downloaded_epub"
                and artifact.relative_path.startswith("external/downloaded_epub/")
            ):
                continue
            logical_parts = PurePosixPath(artifact.relative_path).parts
            raw_path = root.joinpath(*logical_parts)
            cursor = root
            try:
                for part in logical_parts:
                    cursor = cursor / part
                    is_junction = getattr(cursor, "is_junction", None)
                    if cursor.is_symlink() or bool(is_junction and is_junction()):
                        raise KnowledgeStoreConflict(
                            "source receipt artifact traverses a link or junction"
                        )
                path = raw_path.resolve(strict=True)
                path.relative_to(root)
                if not path.is_file():
                    raise KnowledgeStoreConflict(
                        "source receipt artifact is not a regular file"
                    )
                before = path.stat()
                digest = hashlib.sha256()
                observed_size = 0
                captured = bytearray() if artifact.kind == "cards_draft_json" else None
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        observed_size += len(chunk)
                        digest.update(chunk)
                        if captured is not None:
                            captured.extend(chunk)
                after = path.stat()
            except KnowledgeStoreConflict:
                raise
            except (OSError, ValueError) as exc:
                raise KnowledgeStoreConflict(
                    "source receipt artifact is unavailable during formalization"
                ) from exc
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or observed_size != artifact.size_bytes
                or digest.hexdigest() != artifact.sha256
            ):
                raise KnowledgeStoreConflict(
                    "source receipt artifact changed before formalization"
                )
            if captured is not None:
                cards_payload = bytes(captured)
        if cards_payload is None:
            raise KnowledgeStoreConflict(
                "source receipt has no Runtime-owned cards draft artifact"
            )
        return cards_payload

    @staticmethod
    def _stored_datetime_matches(raw: object, expected: datetime) -> bool:
        try:
            observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return False
        if observed.tzinfo is None or observed.utcoffset() is None:
            return False
        return observed.astimezone(UTC) == expected.astimezone(UTC)

    @staticmethod
    def _json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


__all__ = [
    "KnowledgeProjectStateSnapshot",
    "KnowledgeScopeSnapshot",
    "KnowledgeStore",
    "KnowledgeStoreConflict",
    "KnowledgeStoreError",
    "KnowledgeStoreNotFound",
]
