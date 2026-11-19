from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextPack,
    canonical_json_hash,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreConflict,
    ContextStoreIntegrity,
    ContextStoreNotFound,
)
from zhimeng_runtime.database import SCHEMA_VERSION, SqliteStore
from zhimeng_runtime.knowledge_models import knowledge_formalization_receipt_id
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
    nlcl_route_receipt_hash,
)
from zhimeng_runtime.nlcl_route_execution_models import (
    build_nlcl_route_execution_receipt,
)
from zhimeng_runtime.prewrite_models import (
    build_prewrite_operation_package,
    prewrite_operation_package_hash,
)
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


PROJECT_ID = "project-0123456789abcdef01234567"
SESSION_ID = "session-context-store"
PROJECT_FINGERPRINT = "a" * 64
CREATED_AT = datetime(2026, 7, 20, tzinfo=UTC)


def _database(tmp_path: Path, *, last_message_ordinal: int = 0) -> SqliteStore:
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path="C:/Novels/ContextStoreFixture",
        display_name="Context Store Fixture",
        fingerprint=PROJECT_FINGERPRINT,
    )
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO execution_session(
                id, project_binding_id, title, status, revision,
                latest_project_revision, latest_project_fingerprint,
                executor_json, capabilities_json, correlation_id,
                last_run_ordinal, last_message_ordinal, created_at, updated_at
            ) VALUES (?, ?, ?, 'idle', 1, 1, ?, '{}', '{}', ?, 0, ?, ?, ?)
            """,
            (
                SESSION_ID,
                PROJECT_ID,
                "Context Store Fixture",
                PROJECT_FINGERPRINT,
                "correlation-context-store",
                last_message_ordinal,
                CREATED_AT.isoformat(),
                CREATED_AT.isoformat(),
            ),
        )
    return database


def _pack(
    *,
    context_pack_id: str = "context-pack-fixture",
    objective: str = "Continue the current chapter.",
    project_revision: int = 1,
    project_fingerprint: str = PROJECT_FINGERPRINT,
    history_through_ordinal: int = 0,
    created_at: datetime = CREATED_AT,
    session_id: str = SESSION_ID,
    project_id: str = PROJECT_ID,
    candidates: tuple[ContextCandidateFragment, ...] = (),
    knowledge_fingerprint: str | None = None,
) -> ContextPack:
    return compile_context_pack(
        context_pack_id=context_pack_id,
        session_id=session_id,
        project_id=project_id,
        history_through_ordinal=history_through_ordinal,
        request=ContextCreateRequest(
            objective=objective,
            profile="chapter",
            active_chapter_id="CH001",
            expected_project_revision=project_revision,
            expected_project_fingerprint=project_fingerprint,
            project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            project_kb_manifest_hash="b" * 64,
        ),
        history=(),
        candidates=candidates,
        max_characters=20_000,
        max_tokens=20_000,
        knowledge_fingerprint=knowledge_fingerprint,
        created_at=created_at,
    )


def _insert_run(
    database: SqliteStore,
    *,
    run_id: str,
    objective: str,
    context_pack_id: str | None,
    ordinal: int = 1,
) -> None:
    with database.connection() as connection:
        connection.execute(
            """
            INSERT INTO run(
                id, session_id, project_binding_id, context_pack_id, ordinal,
                run_type, status, objective, idempotency_key, request_hash,
                project_revision, project_fingerprint, executor_json,
                capabilities_json, correlation_id, causation_id,
                last_activity_sequence, error_json, created_at, started_at,
                updated_at, completed_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'agent_turn', 'succeeded', ?, ?, ?, 1, ?,
                '{}', '{}', ?, NULL, 0, NULL, ?, ?, ?, ?
            )
            """,
            (
                run_id,
                SESSION_ID,
                PROJECT_ID,
                context_pack_id,
                ordinal,
                objective,
                f"idempotency-{run_id}",
                "e" * 64,
                PROJECT_FINGERPRINT,
                f"correlation-{run_id}",
                CREATED_AT.isoformat(),
                CREATED_AT.isoformat(),
                CREATED_AT.isoformat(),
                CREATED_AT.isoformat(),
            ),
        )


def test_pack_persists_across_restart_and_lists_newest_first(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    first = _pack(context_pack_id="context-pack-first")
    second = _pack(
        context_pack_id="context-pack-second",
        created_at=CREATED_AT + timedelta(seconds=1),
    )

    created = store.create_pack(first)
    store.create_pack(second)
    reopened_database = SqliteStore(database.path)
    reopened_database.initialize()
    reopened = ContextStore(reopened_database)

    persisted = reopened.get_pack(first.context_pack_id)
    listed = reopened.list_session_packs(SESSION_ID, limit=2)

    assert created == persisted
    assert persisted.pack == first
    assert persisted.consumed_by_run_id is None
    assert [item.pack.context_pack_id for item in listed] == [
        second.context_pack_id,
        first.context_pack_id,
    ]
    with pytest.raises(ContextStoreNotFound):
        reopened.list_session_packs("session-missing")
    with pytest.raises(ValueError):
        reopened.list_session_packs(SESSION_ID, limit=0)


def test_pack_idempotency_replays_same_hash_and_rejects_different_hash(
    tmp_path: Path,
) -> None:
    store = ContextStore(_database(tmp_path))
    original = _pack()
    conflicting = _pack(
        context_pack_id=original.context_pack_id,
        objective="Rewrite the current chapter.",
    )

    first = store.create_pack(original)
    replay = store.create_pack(original)

    assert replay == first
    with pytest.raises(ContextStoreConflict, match="different pack hash"):
        store.create_pack(conflicting)


def test_route_receipt_is_atomic_replay_checked_and_tamper_evident(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.UNAVAILABLE,
        extra_warnings=("first_route",),
    )
    store.create_pack(pack, nlcl_route=route)

    assert store.get_nlcl_route(pack.context_pack_id) == route
    conflicting = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.UNAVAILABLE,
        extra_warnings=("different_route",),
    )
    with pytest.raises(ContextStoreConflict, match="different NLCL route hash"):
        store.create_pack(pack, nlcl_route=conflicting)

    with database.connection() as connection:
        connection.execute(
            "UPDATE nlcl_route_receipt SET route_status = 'risk_ready' WHERE context_pack_id = ?",
            (pack.context_pack_id,),
        )
    with pytest.raises(ContextStoreIntegrity, match="route column"):
        store.get_nlcl_route(pack.context_pack_id)


def test_route_rejects_rehashed_forged_knowledge_provenance(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    source_import_id = "source-import-" + "1" * 32
    card_id = "pressure-card"
    version_id = "knowledge-version-" + "b" * 32
    version_content_hash = canonical_json_hash({"card": "pressure"})
    provenance = KnowledgeProvenanceRef(
        source_import_id=source_import_id,
        source_receipt_id=source_import_receipt_id(source_import_id),
        source_receipt_hash="2" * 64,
        source_packet_fingerprint="3" * 64,
        cards_draft_hash="4" * 64,
        candidate_id="knowledge-candidate-" + "5" * 32,
        candidate_origin_hash="6" * 64,
        card_id=card_id,
        version_id=version_id,
        version_content_hash=version_content_hash,
        version_lineage_receipt_id="knowledge-lineage-" + "7" * 32,
        version_lineage_receipt_hash="8" * 64,
        formalization_receipt_id=knowledge_formalization_receipt_id(
            source_import_id
        ),
        formalization_receipt_hash="9" * 64,
    )
    pack = _pack(
        context_pack_id="context-pack-provenance-forgery",
        candidates=(
            ContextCandidateFragment(
                source_id=f"knowledge-card:{card_id}",
                source_type="native_knowledge_card:work",
                title="Pressure escalation",
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                version=version_id,
                source_hash=version_content_hash,
                content='{"summary":"Escalate observable pressure."}',
                priority=700,
                mandatory=False,
                eligible=True,
                knowledge_provenance=provenance,
            ),
        ),
        knowledge_fingerprint="c" * 64,
    )
    fragment = pack.included[0]
    route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(
            NlclSelectedCard(
                card_id=card_id,
                card_type="work",
                title="Pressure escalation",
                version_id=fragment.version,
                content_hash=fragment.source_hash,
                route_terms=("pressure", "escalation"),
                recommended_profiles=("chapter",),
                knowledge_provenance=fragment.knowledge_provenance,
            ),
        ),
    )
    store.create_pack(pack, nlcl_route=route)

    forged_provenance = provenance.model_copy(
        update={"source_receipt_hash": "f" * 64}
    )
    forged_card = route.selected_cards[0].model_copy(
        update={"knowledge_provenance": forged_provenance}
    )
    forged_draft = route.model_copy(
        update={
            "selected_cards": (forged_card,),
            "receipt_hash": "0" * 64,
        }
    )
    forged_route = forged_draft.model_copy(
        update={"receipt_hash": nlcl_route_receipt_hash(forged_draft)}
    )
    forged_payload = json.dumps(
        forged_route.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with database.connection() as connection:
        connection.execute(
            """
            UPDATE nlcl_route_receipt
            SET receipt_hash = ?, payload_json = ?
            WHERE context_pack_id = ?
            """,
            (
                forged_route.receipt_hash,
                forged_payload,
                pack.context_pack_id,
            ),
        )

    with pytest.raises(ContextStoreIntegrity, match="selected card identity"):
        store.get_nlcl_route(pack.context_pack_id)


def test_prewrite_package_is_atomic_replay_checked_and_tamper_evident(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    route = build_nlcl_route_receipt(pack, source=NlclRouteSource.UNAVAILABLE)
    created = store.create_pack(pack, nlcl_route=route)

    package = store.get_prewrite_operation_package(pack.context_pack_id)
    assert created.pack == pack
    assert package.context_pack_hash == pack.pack_hash
    assert package.route_receipt_hash == route.receipt_hash
    assert package.readiness == "blocked"
    assert package.formal_draft_allowed is False
    assert store.create_pack(
        pack,
        nlcl_route=route,
        prewrite_package=package,
    ) == created

    conflicting = build_prewrite_operation_package(
        pack,
        route,
        extra_missing_requirements=("migrated_prewrite_package_requires_recompile",),
    )
    with pytest.raises(ContextStoreIntegrity, match="does not satisfy"):
        store.create_pack(
            pack,
            nlcl_route=route,
            prewrite_package=conflicting,
        )

    with database.connection() as connection:
        connection.execute(
            "UPDATE prewrite_operation_package SET readiness = 'risk_draft_only' "
            "WHERE context_pack_id = ?",
            (pack.context_pack_id,),
        )
    with pytest.raises(ContextStoreIntegrity, match="prewrite package column"):
        store.get_prewrite_operation_package(pack.context_pack_id)


def test_prewrite_package_rejects_rehashed_deterministic_field_tampering(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    route = build_nlcl_route_receipt(pack, source=NlclRouteSource.UNAVAILABLE)
    store.create_pack(pack, nlcl_route=route)
    package = store.get_prewrite_operation_package(pack.context_pack_id)
    tampered = package.model_copy(
        update={
            "acceptance_gate": package.acceptance_gate.model_copy(
                update={"minimum_review_score": 1}
            ),
            "package_hash": "0" * 64,
        }
    )
    payload = tampered.model_copy(
        update={"package_hash": prewrite_operation_package_hash(tampered)}
    ).model_dump(mode="json", by_alias=True)

    with database.connection() as connection:
        connection.execute(
            """
            UPDATE prewrite_operation_package
            SET package_hash = ?, payload_json = ?
            WHERE context_pack_id = ?
            """,
            (
                payload["package_hash"],
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                pack.context_pack_id,
            ),
        )

    with pytest.raises(ContextStoreIntegrity, match="deterministic inputs"):
        store.get_prewrite_operation_package(pack.context_pack_id)


def test_v8_context_pack_is_backfilled_with_honest_unavailable_route(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    store.create_pack(pack)
    with database.connection() as connection:
        connection.execute(
            "DELETE FROM nlcl_route_receipt WHERE context_pack_id = ?",
            (pack.context_pack_id,),
        )
        connection.execute("PRAGMA user_version = 8")

    reopened = SqliteStore(database.path)
    reopened.initialize()
    route = ContextStore(reopened).get_nlcl_route(pack.context_pack_id)

    assert route.source == "unavailable"
    assert route.status == "unavailable"
    assert "migrated_context_pack_route_unavailable" in route.warnings


def test_v9_database_adds_route_execution_table_without_rewriting_route(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    original_route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.UNAVAILABLE,
    )
    store.create_pack(pack, nlcl_route=original_route)
    with database.connection() as connection:
        connection.execute("DROP TABLE nlcl_route_execution_receipt")
        connection.execute("DROP TABLE prewrite_operation_package")
        connection.execute("PRAGMA user_version = 9")

    reopened = SqliteStore(database.path)
    health = reopened.initialize()

    assert health.schema_version == SCHEMA_VERSION
    assert ContextStore(reopened).get_nlcl_route(pack.context_pack_id) == original_route
    migrated_package = ContextStore(reopened).get_prewrite_operation_package(
        pack.context_pack_id
    )
    assert migrated_package.readiness == "blocked"
    assert "migrated_prewrite_package_requires_recompile" in (
        migrated_package.missing_requirements
    )
    with reopened.connection() as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(nlcl_route_execution_receipt)"
            ).fetchall()
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {"route_receipt_id", "tool_result_hash", "receipt_hash"} <= columns
    assert violations == []


def test_v19_context_history_migrates_with_null_project_kb_bindings(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    legacy_request = ContextCreateRequest.model_construct(
        objective="Continue the legacy chapter.",
        profile="chapter",
        active_chapter_id="CH001",
        expected_project_revision=1,
        expected_project_fingerprint=PROJECT_FINGERPRINT,
        project_kb_state_id=None,
        project_kb_manifest_hash=None,
        formal_knowledge_selector=None,
    )
    pack = compile_context_pack(
        context_pack_id="context-pack-v19-history",
        session_id=SESSION_ID,
        project_id=PROJECT_ID,
        history_through_ordinal=0,
        request=legacy_request,
        history=(),
        candidates=(),
        max_characters=20_000,
        max_tokens=20_000,
        created_at=CREATED_AT,
    )
    route = build_nlcl_route_receipt(pack, source=NlclRouteSource.UNAVAILABLE)
    store.create_pack(pack, nlcl_route=route)
    run_id = "run-route-execution-v19"
    tool_call_id = "tool-route-execution-v19"
    _insert_run(
        database,
        run_id=run_id,
        objective=pack.objective,
        context_pack_id=pack.context_pack_id,
    )
    tool_output = {"error": "legacy route execution failed"}
    execution = build_nlcl_route_execution_receipt(
        route,
        route.chapter_call_list[0],
        run_id=run_id,
        tool_call_id=tool_call_id,
        skill_call_id=None,
        skill_receipt_id=None,
        skill_receipt_hash=None,
        execution_status="failed",
        domain_status=None,
        output_hash=canonical_json_hash(tool_output),
        tool_result_hash="e" * 64,
        created_at=CREATED_AT,
    )

    def legacy_payload(model) -> str:
        payload = model.model_dump(mode="json", by_alias=True)
        payload.pop("project_kb_state_id", None)
        payload.pop("project_kb_manifest_hash", None)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    pack_payload = legacy_payload(pack)
    route_payload = legacy_payload(route)
    execution_payload = legacy_payload(execution)
    tool_arguments = {
        "skill_id": execution.skill_id,
        "input": {"schema": execution.input_contract},
    }
    with database.connection() as connection:
        connection.execute(
            "UPDATE context_pack SET payload_json = ? WHERE id = ?",
            (pack_payload, pack.context_pack_id),
        )
        connection.execute(
            "UPDATE nlcl_route_receipt SET payload_json = ? WHERE id = ?",
            (route_payload, route.route_receipt_id),
        )
        connection.execute(
            """
            INSERT INTO tool_call(
                id, run_id, tool_name, tool_version, status, idempotency_key,
                request_hash, input_json, output_json, error_json,
                call_activity_id, result_activity_id, result_hash, duration_ms,
                started_at, finished_at
            ) VALUES (?, ?, 'skill_execute', '1', 'failed', ?, ?, ?, ?,
                      NULL, NULL, NULL, ?, 1, ?, ?)
            """,
            (
                tool_call_id,
                run_id,
                tool_call_id,
                "c" * 64,
                json.dumps(tool_arguments, separators=(",", ":")),
                json.dumps(tool_output, separators=(",", ":")),
                execution.tool_result_hash,
                CREATED_AT.isoformat(),
                CREATED_AT.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO nlcl_route_execution_receipt(
                id, route_receipt_id, context_pack_id, route_receipt_hash,
                context_pack_hash, project_kb_state_id,
                project_kb_manifest_hash, call_ordinal, run_id, tool_call_id,
                skill_call_id, skill_receipt_id, skill_receipt_hash,
                execution_status, domain_status, output_hash, tool_result_hash,
                receipt_hash, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, NULL, NULL, NULL,
                      ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                execution.execution_receipt_id,
                execution.route_receipt_id,
                execution.context_pack_id,
                execution.route_receipt_hash,
                execution.context_pack_hash,
                execution.call_ordinal,
                execution.run_id,
                execution.tool_call_id,
                execution.execution_status,
                execution.output_hash,
                execution.tool_result_hash,
                execution.receipt_hash,
                execution_payload,
                execution.created_at.isoformat(),
            ),
        )

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE context_pack_v19 AS SELECT
                id, session_id, project_binding_id, history_through_ordinal,
                objective, active_chapter_id, profile, project_revision,
                project_fingerprint, knowledge_fingerprint, readiness,
                pack_hash, injection_hash, provider_messages_hash,
                payload_json, created_at
            FROM context_pack;
            CREATE TABLE nlcl_route_receipt_v19 AS SELECT
                id, context_pack_id, project_binding_id, context_pack_hash,
                project_revision, project_fingerprint, active_chapter_id,
                profile, objective_hash, knowledge_fingerprint, source,
                route_status, receipt_hash, payload_json, created_at
            FROM nlcl_route_receipt;
            CREATE TABLE nlcl_route_execution_receipt_v19 AS SELECT
                id, route_receipt_id, context_pack_id, route_receipt_hash,
                context_pack_hash, call_ordinal, run_id, tool_call_id,
                skill_call_id, skill_receipt_id, skill_receipt_hash,
                execution_status, domain_status, output_hash, tool_result_hash,
                receipt_hash, payload_json, created_at
            FROM nlcl_route_execution_receipt;
            DROP TABLE nlcl_route_execution_receipt;
            DROP TABLE nlcl_route_receipt;
            DROP TABLE context_pack;
            ALTER TABLE context_pack_v19 RENAME TO context_pack;
            ALTER TABLE nlcl_route_receipt_v19 RENAME TO nlcl_route_receipt;
            ALTER TABLE nlcl_route_execution_receipt_v19
                RENAME TO nlcl_route_execution_receipt;
            CREATE UNIQUE INDEX ux_context_pack_v19_id ON context_pack(id);
            CREATE UNIQUE INDEX ux_nlcl_route_v19_id ON nlcl_route_receipt(id);
            CREATE UNIQUE INDEX ux_nlcl_route_execution_v19_id
                ON nlcl_route_execution_receipt(id);
            PRAGMA user_version = 19;
            """
        )

    reopened = SqliteStore(database.path)
    assert reopened.initialize().schema_version == SCHEMA_VERSION
    migrated = ContextStore(reopened)

    assert migrated.get_pack(pack.context_pack_id).pack == pack
    assert migrated.get_nlcl_route(pack.context_pack_id) == route
    assert migrated.get_route_execution(pack.context_pack_id) == execution
    with reopened.connection() as connection:
        pack_row = connection.execute(
            "SELECT * FROM context_pack WHERE id = ?",
            (pack.context_pack_id,),
        ).fetchone()
        route_row = connection.execute(
            "SELECT * FROM nlcl_route_receipt WHERE id = ?",
            (route.route_receipt_id,),
        ).fetchone()
        execution_row = connection.execute(
            "SELECT * FROM nlcl_route_execution_receipt WHERE id = ?",
            (execution.execution_receipt_id,),
        ).fetchone()
        assert pack_row is not None
        assert route_row is not None
        assert execution_row is not None
        for row in (pack_row, route_row, execution_row):
            assert row["project_kb_state_id"] is None
            assert row["project_kb_manifest_hash"] is None
        assert pack_row["payload_json"] == pack_payload
        assert pack_row["pack_hash"] == pack.pack_hash
        assert pack_row["injection_hash"] == pack.injection_hash
        assert pack_row["provider_messages_hash"] == pack.provider_messages_hash
        assert route_row["payload_json"] == route_payload
        assert route_row["receipt_hash"] == route.receipt_hash
        assert execution_row["payload_json"] == execution_payload
        assert execution_row["receipt_hash"] == execution.receipt_hash
        with pytest.raises(sqlite3.IntegrityError, match="project KB binding"):
            connection.execute(
                "UPDATE context_pack SET project_kb_state_id = ? WHERE id = ?",
                ("project-kb-state-" + "a" * 32, pack.context_pack_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="project KB binding"):
            connection.execute(
                """
                UPDATE context_pack
                SET project_kb_state_id = ?, project_kb_manifest_hash = ?
                WHERE id = ?
                """,
                (
                    "project-kb-state-" + "a" * 31 + "z",
                    "f" * 64,
                    pack.context_pack_id,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="project KB binding"):
            connection.execute(
                """
                UPDATE context_pack
                SET project_kb_state_id = ?, project_kb_manifest_hash = ?
                WHERE id = ?
                """,
                (
                    "project-kb-state-" + "a" * 32,
                    "f" * 63 + "z",
                    pack.context_pack_id,
                ),
            )

    with pytest.raises(ContextStoreConflict, match="Legacy unbound"):
        migrated.create_route_execution(execution)


def test_fresh_v20_rejects_non_hex_project_kb_binding_values(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    pack = _pack()
    ContextStore(database).create_pack(pack)

    with database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE context_pack SET project_kb_state_id = ? WHERE id = ?",
                (
                    "project-kb-state-" + "a" * 31 + "z",
                    pack.context_pack_id,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE context_pack SET project_kb_manifest_hash = ? WHERE id = ?",
                ("b" * 63 + "z", pack.context_pack_id),
            )


def test_v20_restart_self_heals_project_kb_binding_triggers(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    trigger_specs = {
        (table, operation): f"trg_{table}_project_kb_binding_{operation.lower()}"
        for table in (
            "context_pack",
            "nlcl_route_receipt",
            "nlcl_route_execution_receipt",
        )
        for operation in ("INSERT", "UPDATE")
    }
    with database.connection() as connection:
        for (table, operation), trigger_name in trigger_specs.items():
            connection.execute(f"DROP TRIGGER {trigger_name}")
            connection.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE {operation} ON {table}
                FOR EACH ROW WHEN
                    (NEW.project_kb_state_id IS NULL)
                        != (NEW.project_kb_manifest_hash IS NULL)
                BEGIN
                    SELECT RAISE(ABORT, 'lax project KB binding');
                END
                """
            )
        connection.execute("PRAGMA user_version = 20")

    reopened = SqliteStore(database.path)
    assert reopened.initialize().schema_version == SCHEMA_VERSION
    with reopened.connection() as connection:
        restored = {
            str(row[0]): str(row[1]).lower()
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    assert set(trigger_specs.values()) <= set(restored)
    for trigger_name in trigger_specs.values():
        definition = restored[trigger_name]
        assert "length(new.project_kb_state_id) != 49" in definition
        assert "new.project_kb_state_id, 18" in definition
        assert "glob '*[^0-9a-f]*'" in definition
        assert "length(new.project_kb_manifest_hash) != 64" in definition


def test_v10_database_backfills_honestly_blocked_prewrite_package(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    current_route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.UNAVAILABLE,
    )
    route_draft = current_route.model_copy(
        update={
            "chapter_call_list": current_route.chapter_call_list[:1],
            "receipt_hash": "0" * 64,
        }
    )
    route = NlclRouteReceipt.model_validate_json(
        json.dumps(
            route_draft.model_copy(
                update={"receipt_hash": nlcl_route_receipt_hash(route_draft)}
            ).model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
        ),
        strict=True,
    )
    store.create_pack(pack, nlcl_route=route)
    with database.connection() as connection:
        connection.execute("DROP TABLE prewrite_operation_package")
        connection.execute("PRAGMA user_version = 10")

    reopened = SqliteStore(database.path)
    assert reopened.initialize().schema_version == SCHEMA_VERSION
    migrated = ContextStore(reopened).get_prewrite_operation_package(
        pack.context_pack_id
    )

    assert migrated.context_pack_hash == pack.pack_hash
    assert migrated.route_receipt_hash == route.receipt_hash
    assert len(ContextStore(reopened).get_nlcl_route(pack.context_pack_id).chapter_call_list) == 1
    assert migrated.readiness == "blocked"
    assert migrated.formal_draft_allowed is False
    assert "migrated_prewrite_package_requires_recompile" in (
        migrated.missing_requirements
    )


def test_create_rejects_stale_project_history_and_missing_bindings(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)

    with pytest.raises(ContextStoreConflict, match="revision is stale"):
        store.create_pack(
            _pack(
                context_pack_id="context-pack-stale-project",
                project_revision=2,
                project_fingerprint="b" * 64,
            )
        )
    with pytest.raises(ContextStoreConflict, match="history ordinal is stale"):
        store.create_pack(
            _pack(
                context_pack_id="context-pack-stale-history",
                history_through_ordinal=1,
            )
        )
    with pytest.raises(ContextStoreNotFound, match="session"):
        store.create_pack(
            _pack(
                context_pack_id="context-pack-missing-session",
                session_id="session-missing",
            )
        )
    with pytest.raises(ContextStoreNotFound, match="project"):
        store.create_pack(
            _pack(
                context_pack_id="context-pack-missing-project",
                project_id="project-missing",
            )
        )


def test_get_pack_fails_closed_on_payload_or_duplicate_column_corruption(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    invalid_payload = _pack(context_pack_id="context-pack-invalid-payload")
    mismatched_column = _pack(context_pack_id="context-pack-mismatched-column")
    store.create_pack(invalid_payload)
    store.create_pack(mismatched_column)

    with database.connection() as connection:
        connection.execute(
            "UPDATE context_pack SET payload_json = '{}' WHERE id = ?",
            (invalid_payload.context_pack_id,),
        )
        connection.execute(
            "UPDATE context_pack SET objective = ? WHERE id = ?",
            ("Tampered objective", mismatched_column.context_pack_id),
        )

    with pytest.raises(ContextStoreIntegrity, match="payload is invalid"):
        store.get_pack(invalid_payload.context_pack_id)
    with pytest.raises(ContextStoreIntegrity, match="objective"):
        store.get_pack(mismatched_column.context_pack_id)


def test_get_run_pack_reports_consumption_and_rejects_unbound_or_missing_runs(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    store = ContextStore(database)
    pack = _pack()
    store.create_pack(pack)
    _insert_run(
        database,
        run_id="run-context-consumer",
        objective=pack.objective,
        context_pack_id=pack.context_pack_id,
    )
    _insert_run(
        database,
        run_id="run-without-context",
        objective="Legacy Run without a Context Pack.",
        context_pack_id=None,
        ordinal=2,
    )

    by_pack = store.get_pack(pack.context_pack_id)
    by_run = store.get_run_pack("run-context-consumer")

    assert by_pack == by_run
    assert by_run.consumed_by_run_id == "run-context-consumer"
    with pytest.raises(ContextStoreNotFound, match="has no Context Pack"):
        store.get_run_pack("run-without-context")
    with pytest.raises(ContextStoreNotFound, match="Run was not found"):
        store.get_run_pack("run-missing")
    with pytest.raises(ContextStoreNotFound, match="Pack was not found"):
        store.get_pack("context-pack-missing")
