from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore
from zhimeng_runtime.knowledge_models import (
    KnowledgeCardStatus,
    KnowledgeScopedCardList,
    native_knowledge_fingerprint,
)
from zhimeng_runtime.knowledge_scope_models import (
    APPLICATION_KNOWLEDGE_SCOPE_ID,
    KnowledgeScopeType,
    knowledge_scope_id,
)
from zhimeng_runtime.knowledge_service import KnowledgeService
from zhimeng_runtime.knowledge_store import (
    KnowledgeScopeSnapshot,
    KnowledgeStore,
    KnowledgeStoreConflict,
    KnowledgeStoreNotFound,
)


PROJECT_ID = "project-0123456789abcdef01234567"
SOURCE_APP = "source-import-" + "a" * 32
SOURCE_PROJECT = "source-import-" + "b" * 32
CARD_ID = "work-scope-isolation"
APP_CANDIDATE_ID = "knowledge-candidate-" + "a" * 32
PROJECT_CANDIDATE_ID = "knowledge-candidate-" + "b" * 32
APP_VERSION_ID = "knowledge-version-" + "a" * 32
PROJECT_VERSION_ID = "knowledge-version-" + "b" * 32


def _database(tmp_path: Path) -> SqliteStore:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    return database


def _insert_project(connection: sqlite3.Connection, now: str) -> None:
    connection.execute(
        """
        INSERT INTO project_binding(
            id, root_path, display_name, fingerprint, revision,
            created_at, updated_at
        ) VALUES (?, ?, 'Scope test', ?, 1, ?, ?)
        """,
        (PROJECT_ID, "C:/scope-test", "1" * 64, now, now),
    )


def _insert_source(
    connection: sqlite3.Connection,
    source_import_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO source_import_job(
            id, provider, query, book_id, status,
            payload_json, created_at, updated_at
        ) VALUES (?, 'tomato', 'scope test', 'book-test',
                  'awaiting_review', '{}', ?, ?)
        """,
        (source_import_id, now, now),
    )


def _insert_candidate_and_version(
    connection: sqlite3.Connection,
    *,
    source_import_id: str,
    candidate_id: str,
    version_id: str,
    version_number: int,
    marker: str,
    now: str,
) -> None:
    card = {"card_id": CARD_ID, "card_type": "work", "marker": marker}
    content_hash = canonical_json_hash(card)
    connection.execute(
        """
        INSERT INTO knowledge_candidate(
            id, source_import_id, source_card_index, source_draft_hash,
            lineage_status, origin_json, origin_hash,
            card_id, card_type, status, revision, content_hash,
            payload_json, issues_json, promoted_version_id,
            created_at, updated_at
        ) VALUES (?, ?, 0, ?, 'legacy_unverified', NULL, NULL,
                  ?, 'work', 'pending', 1, ?, ?, '[]', NULL, ?, ?)
        """,
        (
            candidate_id,
            source_import_id,
            marker * 64,
            CARD_ID,
            content_hash,
            __import__("json").dumps(card, sort_keys=True, separators=(",", ":")),
            now,
            now,
        ),
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
            CARD_ID,
            version_number,
            content_hash,
            __import__("json").dumps(card, sort_keys=True, separators=(",", ":")),
            candidate_id,
            now,
        ),
    )


def test_v15_migration_creates_deterministic_project_scopes_and_legacy_bindings(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        _insert_project(connection, now)
        _insert_source(connection, SOURCE_APP, now)
        connection.execute("PRAGMA user_version = 14")

    health = database.initialize()
    assert health.schema_version == SCHEMA_VERSION
    with database.connection() as connection:
        project_scope = connection.execute(
            "SELECT * FROM knowledge_scope WHERE project_binding_id = ?",
            (PROJECT_ID,),
        ).fetchone()
        assert project_scope is not None
        assert project_scope["id"] == knowledge_scope_id(PROJECT_ID)
        assert project_scope["scope_type"] == KnowledgeScopeType.PROJECT.value
        source_scope = connection.execute(
            "SELECT scope_id FROM knowledge_source_scope WHERE source_import_id = ?",
            (SOURCE_APP,),
        ).fetchone()
        assert source_scope is not None
        assert source_scope["scope_id"] == APPLICATION_KNOWLEDGE_SCOPE_ID
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_source_import_scope_binding_is_idempotent_and_immutable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = KnowledgeStore(database)
    now = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        _insert_project(connection, now)
        _insert_source(connection, SOURCE_PROJECT, now)
    project_scope = store.ensure_project_scope(PROJECT_ID)

    with pytest.raises(KnowledgeStoreNotFound, match="scope was not found"):
        store.get_source_import_scope(SOURCE_PROJECT)
    with database.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM knowledge_source_scope WHERE source_import_id = ?",
            (SOURCE_PROJECT,),
        ).fetchone() is None
    with pytest.raises(ValueError, match="cannot be empty"):
        store.get_scope("")

    first = store.bind_source_import_scope(SOURCE_PROJECT, project_scope.scope_id)
    replay = store.bind_source_import_scope(SOURCE_PROJECT, project_scope.scope_id)
    assert replay == first
    with pytest.raises(KnowledgeStoreConflict, match="another knowledge scope"):
        store.bind_source_import_scope(SOURCE_PROJECT, APPLICATION_KNOWLEDGE_SCOPE_ID)
    with database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE knowledge_source_scope SET scope_id = ? WHERE source_import_id = ?",
                (APPLICATION_KNOWLEDGE_SCOPE_ID, SOURCE_PROJECT),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM knowledge_source_scope WHERE source_import_id = ?",
                (SOURCE_PROJECT,),
            )


def test_explicit_project_scope_identity_requires_a_project_binding() -> None:
    with pytest.raises(ValueError, match="requires a project binding"):
        knowledge_scope_id(scope_type=KnowledgeScopeType.PROJECT)


def test_scoped_card_reads_and_state_cas_do_not_leak_versions(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = KnowledgeStore(database)
    now = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        _insert_project(connection, now)
        _insert_source(connection, SOURCE_APP, now)
        _insert_source(connection, SOURCE_PROJECT, now)
    project_scope = store.ensure_project_scope(PROJECT_ID)
    store.bind_source_import_scope(SOURCE_APP, APPLICATION_KNOWLEDGE_SCOPE_ID)
    store.bind_source_import_scope(SOURCE_PROJECT, project_scope.scope_id)

    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO knowledge_card(
                card_id, status, state_revision, active_version_id,
                created_at, updated_at
            ) VALUES (?, 'active', 1, NULL, ?, ?)
            """,
            (CARD_ID, now, now),
        )
        _insert_candidate_and_version(
            connection,
            source_import_id=SOURCE_APP,
            candidate_id=APP_CANDIDATE_ID,
            version_id=APP_VERSION_ID,
            version_number=1,
            marker="a",
            now=now,
        )
        _insert_candidate_and_version(
            connection,
            source_import_id=SOURCE_PROJECT,
            candidate_id=PROJECT_CANDIDATE_ID,
            version_id=PROJECT_VERSION_ID,
            version_number=2,
            marker="b",
            now=now,
        )
        connection.execute(
            "UPDATE knowledge_card SET active_version_id = ? WHERE card_id = ?",
            (APP_VERSION_ID, CARD_ID),
        )
        connection.execute(
            """
            INSERT INTO knowledge_scope_card(
                scope_id, card_id, status, state_revision,
                active_version_id, created_at, updated_at
            ) VALUES (?, ?, 'active', 1, ?, ?, ?)
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID, CARD_ID, APP_VERSION_ID, now, now),
        )
        connection.execute(
            """
            INSERT INTO knowledge_scope_card(
                scope_id, card_id, status, state_revision,
                active_version_id, created_at, updated_at
            ) VALUES (?, ?, 'active', 1, ?, ?, ?)
            """,
            (project_scope.scope_id, CARD_ID, PROJECT_VERSION_ID, now, now),
        )

    application_card = store.get_card(CARD_ID)
    project_card = store.get_scoped_card(project_scope.scope_id, CARD_ID)
    assert [item.version_id for item in application_card.versions] == [APP_VERSION_ID]
    assert [item.version_id for item in project_card.versions] == [PROJECT_VERSION_ID]
    assert application_card.active_version.card["marker"] == "a"
    assert project_card.active_version.card["marker"] == "b"

    snapshot = store.scope_snapshot(
        project_scope.scope_id,
        include_application=True,
        active_only=True,
    )
    assert [item.active_version_id for item in snapshot.cards] == [PROJECT_VERSION_ID]
    assert snapshot.scope == project_scope
    assert snapshot.inherited_card_ids == ()
    assert snapshot.fingerprint_material is not None
    assert snapshot.fingerprint_material["cards"] == [
        {
            "card_id": CARD_ID,
            "origin_scope_id": project_scope.scope_id,
            "status": "active",
            "state_revision": 1,
            "active_version_id": PROJECT_VERSION_ID,
            "content_hash": project_card.active_version.content_hash,
            "lineage_receipt_hash": None,
            "formalization_receipt_hash": None,
        }
    ]

    disabled = store.set_card_status(
        CARD_ID,
        status=KnowledgeCardStatus.DISABLED,
        expected_state_revision=project_card.state_revision,
        expected_active_version_id=project_card.active_version_id,
        scope_id=project_scope.scope_id,
    )
    assert disabled.status is KnowledgeCardStatus.DISABLED
    assert store.get_card(CARD_ID).status is KnowledgeCardStatus.ACTIVE
    snapshot_after_disable = store.scope_snapshot(
        project_scope.scope_id,
        include_application=True,
        active_only=True,
    )
    assert snapshot_after_disable.cards == []
    assert snapshot_after_disable.inherited_card_ids == ()
    assert snapshot_after_disable.fingerprint_material is not None
    assert snapshot_after_disable.fingerprint_material["cards"][0]["status"] == "disabled"
    assert (
        snapshot_after_disable.fingerprint_material["cards"][0]["active_version_id"]
        == PROJECT_VERSION_ID
    )
    with pytest.raises(KnowledgeStoreConflict, match="changed"):
        store.set_card_status(
            CARD_ID,
            status=KnowledgeCardStatus.DISABLED,
            expected_state_revision=project_card.state_revision,
            expected_active_version_id=project_card.active_version_id,
            scope_id=project_scope.scope_id,
        )
    with pytest.raises(KnowledgeStoreNotFound, match="target knowledge version"):
        store.rollback_card(
            CARD_ID,
            target_version_id=APP_VERSION_ID,
            expected_state_revision=disabled.state_revision,
            expected_active_version_id=disabled.active_version_id,
            scope_id=project_scope.scope_id,
        )


def test_project_snapshot_marks_application_cards_as_inherited_and_read_only(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = KnowledgeStore(database)
    now = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        _insert_project(connection, now)
        _insert_source(connection, SOURCE_APP, now)
    project_scope = store.ensure_project_scope(PROJECT_ID)
    store.bind_source_import_scope(SOURCE_APP, APPLICATION_KNOWLEDGE_SCOPE_ID)

    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO knowledge_card(
                card_id, status, state_revision, active_version_id,
                created_at, updated_at
            ) VALUES (?, 'active', 1, NULL, ?, ?)
            """,
            (CARD_ID, now, now),
        )
        _insert_candidate_and_version(
            connection,
            source_import_id=SOURCE_APP,
            candidate_id=APP_CANDIDATE_ID,
            version_id=APP_VERSION_ID,
            version_number=1,
            marker="a",
            now=now,
        )
        connection.execute(
            "UPDATE knowledge_card SET active_version_id = ? WHERE card_id = ?",
            (APP_VERSION_ID, CARD_ID),
        )
        connection.execute(
            """
            INSERT INTO knowledge_scope_card(
                scope_id, card_id, status, state_revision,
                active_version_id, created_at, updated_at
            ) VALUES (?, ?, 'active', 1, ?, ?, ?)
            """,
            (APPLICATION_KNOWLEDGE_SCOPE_ID, CARD_ID, APP_VERSION_ID, now, now),
        )

    snapshot = store.scope_snapshot(
        project_scope.scope_id,
        include_application=True,
    )
    assert snapshot.scope == project_scope
    assert [card.card_id for card in snapshot.cards] == [CARD_ID]
    assert snapshot.inherited_card_ids == (CARD_ID,)
    assert snapshot.fingerprint_material is not None
    assert snapshot.fingerprint_material["cards"][0]["origin_scope_id"] == (
        APPLICATION_KNOWLEDGE_SCOPE_ID
    )

    with database.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        scope_rows_before = writer.execute(
            "SELECT id, scope_type, state_revision FROM knowledge_scope ORDER BY id"
        ).fetchall()
        inherited_while_writer_active = store.scope_snapshot(
            project_scope.scope_id,
            include_application=True,
        )
        scope_rows_after = writer.execute(
            "SELECT id, scope_type, state_revision FROM knowledge_scope ORDER BY id"
        ).fetchall()
        writer.rollback()
    assert inherited_while_writer_active.inherited_card_ids == (CARD_ID,)
    assert [tuple(row) for row in scope_rows_after] == [
        tuple(row) for row in scope_rows_before
    ]

    listed = KnowledgeService(database, tmp_path).list_cards(project_id=PROJECT_ID)
    assert isinstance(listed, KnowledgeScopedCardList)
    assert listed.scope == snapshot.scope
    assert listed.inherited_card_ids == [CARD_ID]
    payload = listed.model_dump(mode="json", by_alias=True)
    with pytest.raises(ValueError, match="unique"):
        KnowledgeScopedCardList.model_validate(
            {**payload, "inherited_card_ids": [CARD_ID, CARD_ID]}
        )
    with pytest.raises(ValueError, match="visible cards"):
        KnowledgeScopedCardList.model_validate(
            {**payload, "inherited_card_ids": ["unknown-card"]}
        )
    second_card_id = f"{CARD_ID}-z"
    second_card = deepcopy(payload["cards"][0])
    second_card["card_id"] = second_card_id
    for version in [second_card["active_version"], *second_card["versions"]]:
        version["card_id"] = second_card_id
        version["card"]["card_id"] = second_card_id
        version["content_hash"] = canonical_json_hash(version["card"])
    with pytest.raises(ValueError, match="sorted"):
        KnowledgeScopedCardList.model_validate(
            {
                **payload,
                "cards": [*payload["cards"], second_card],
                "inherited_card_ids": [second_card_id, CARD_ID],
            }
        )

    application_card = store.get_card(CARD_ID)
    with pytest.raises(KnowledgeStoreNotFound, match="knowledge card was not found"):
        store.set_card_status(
            CARD_ID,
            status=KnowledgeCardStatus.DISABLED,
            expected_state_revision=application_card.state_revision,
            expected_active_version_id=application_card.active_version_id,
            scope_id=project_scope.scope_id,
        )
    with pytest.raises(KnowledgeStoreNotFound, match="target knowledge version"):
        store.rollback_card(
            CARD_ID,
            target_version_id=APP_VERSION_ID,
            expected_state_revision=application_card.state_revision,
            expected_active_version_id=application_card.active_version_id,
            scope_id=project_scope.scope_id,
        )
    assert store.get_card(CARD_ID).status is KnowledgeCardStatus.ACTIVE


def test_project_service_reads_cards_and_fingerprint_from_one_scope_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    now = datetime.now(UTC).isoformat()
    with database.connection() as connection:
        _insert_project(connection, now)
    service = KnowledgeService(database, tmp_path)
    scope = service.store.ensure_project_scope(PROJECT_ID)
    material: dict[str, object] = {
        "schema": "zhimeng.knowledge-scope-fingerprint.v1",
        "scope": {
            "scope_id": scope.scope_id,
            "scope_type": scope.scope_type.value,
            "project_binding_id": PROJECT_ID,
            "state_revision": scope.state_revision,
        },
        "cards": [{"card_id": "paired-snapshot-marker"}],
    }
    snapshot_calls: list[tuple[str, bool, bool]] = []

    def scope_snapshot(
        scope_id: str,
        *,
        include_application: bool,
        active_only: bool,
    ) -> KnowledgeScopeSnapshot:
        snapshot_calls.append((scope_id, include_application, active_only))
        return KnowledgeScopeSnapshot(
            scope=scope,
            cards=[],
            fingerprint_material=material,
            inherited_card_ids=(),
        )

    def separated_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("project reads must not use a separate scope query")

    monkeypatch.setattr(service.store, "scope_snapshot", scope_snapshot)
    monkeypatch.setattr(service.store, "list_scope_cards", separated_read)
    monkeypatch.setattr(service.store, "scope_fingerprint_material", separated_read)
    monkeypatch.setattr(service.store, "get_scope", separated_read)

    expected_fingerprint = canonical_json_hash(material)
    listed = service.list_cards(project_id=PROJECT_ID)
    assert listed.scope == scope
    assert listed.inherited_card_ids == []
    assert listed.fingerprint == expected_fingerprint
    assert service.has_active_cards(project_id=PROJECT_ID) is False
    assert service.observe_knowledge_fingerprint(PROJECT_ID) == expected_fingerprint
    selection = service.select_for_context("scope test", "chapter", project_id=PROJECT_ID)
    assert selection.fingerprint == expected_fingerprint
    assert selection.cards == ()
    assert snapshot_calls == [
        (scope.scope_id, True, False),
        (scope.scope_id, True, True),
        (scope.scope_id, True, True),
        (scope.scope_id, True, True),
    ]


def test_application_service_keeps_native_fingerprint_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = KnowledgeService(_database(tmp_path), tmp_path)

    def unexpected_snapshot(*args: object, **kwargs: object) -> None:
        raise AssertionError("application compatibility path must remain native")

    monkeypatch.setattr(service.store, "scope_snapshot", unexpected_snapshot)
    expected_fingerprint = native_knowledge_fingerprint([])

    assert service.list_cards().fingerprint == expected_fingerprint
    assert service.has_active_cards() is False
    assert service.observe_knowledge_fingerprint() == expected_fingerprint
    selection = service.select_for_context("scope test", "chapter")
    assert selection.fingerprint == expected_fingerprint
    assert selection.cards == ()
