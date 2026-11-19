from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import (
    REQUIRED_COLUMNS,
    REQUIRED_TABLES,
    SCHEMA_VERSION,
    SqliteStore,
)
from zhimeng_runtime.execution_protocol import (
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.post_draft_gate_models import (
    GateCheck,
    GateFileResult,
    PostDraftGateReceipt,
    PostDraftGateResult,
    post_draft_gate_receipt_hash,
)
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportRequestEvidence,
    source_import_idempotency_key,
    source_import_request_hash,
    source_import_run_id,
)
from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    PreparedSourcePacket,
    SourceArtifact,
    SourceImportJob,
    SourceImportProgress,
    SourceImportStatus,
    TomatoSearchCandidate,
)


PROJECT_ID = "project-0123456789abcdef01234567"
PROJECT_FINGERPRINT = "a" * 64


def _create_session_and_run(store: SqliteStore):
    project = store.sync_project_binding(
        project_id=PROJECT_ID,
        root_path="C:/Novels/DatabaseFixture",
        display_name="Database Fixture",
        fingerprint=PROJECT_FINGERPRINT,
    )
    agent_store = AgentStore(store)
    session = agent_store.create_session(
        project_id=project.project_id,
        title="Database migration fixture",
        executor=ExecutorIdentity(
            executor_id="executor.database-test",
            kind=ExecutorKind.EMBEDDED,
            implementation="database-test",
            version="1",
            protocol_version="1",
            instance_id=None,
        ),
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[],
            extensions={},
        ),
    )
    run, _, _ = agent_store.create_run(
        session_id=session.session_id,
        message="Preserve this Run while migrating the database.",
        expected_project_revision=project.revision,
        expected_project_fingerprint=project.fingerprint,
        active_chapter_id="CH001",
        idempotency_key="database-migration-run-0001",
    )
    return project, session, run, agent_store


CONTEXT_PACK_INSERT = """
    INSERT INTO context_pack(
        id, session_id, project_binding_id, history_through_ordinal,
        objective, active_chapter_id, profile, project_revision,
        project_fingerprint, knowledge_fingerprint, readiness,
        pack_hash, injection_hash, provider_messages_hash,
        payload_json, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _context_pack_values(
    *,
    context_pack_id: str,
    session_id: str,
    project_id: str = PROJECT_ID,
    readiness: str = "ready",
) -> tuple[object, ...]:
    return (
        context_pack_id,
        session_id,
        project_id,
        0,
        "Continue the current chapter.",
        "CH001",
        "chapter",
        1,
        PROJECT_FINGERPRINT,
        None,
        readiness,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "{}",
        "2026-07-20T00:00:00+00:00",
    )


def _create_gate_change_set(store: SqliteStore) -> tuple[str, str]:
    project = store.sync_project_binding(
        project_id=PROJECT_ID,
        root_path="C:/Novels/PostDraftGateFixture",
        display_name="Post-Draft Gate Fixture",
        fingerprint=PROJECT_FINGERPRINT,
    )
    change_set_id = "changeset-" + "a" * 32
    created_at = "2026-07-21T01:00:00+00:00"
    store.create_change_set(
        {
            "change_set_id": change_set_id,
            "project_id": project.project_id,
            "status": "ready",
            "base_revision": project.revision,
            "base_fingerprint": project.fingerprint,
            "review_revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    return change_set_id, project.fingerprint


def _gate_receipt_payload(
    *,
    change_set_id: str,
    base_fingerprint: str,
    receipt_hex: str,
    status: str,
    created_at: datetime,
) -> dict[str, object]:
    candidate_hash = "b" * 64
    result = (
        PostDraftGateResult(
            status="passed",
            summary="Candidate delivery contract passed.",
            candidate_id="candidate-database-fixture",
            candidate_manifest_hash=candidate_hash,
            global_findings=[],
            checks=[
                GateCheck(
                    code="candidate_files_present",
                    status="passed",
                    message="Candidate files were present.",
                    evidence_refs=["chapters/001.md"],
                )
            ],
            files=[
                GateFileResult(
                    path="chapters/001.md",
                    status="passed",
                    content_hash=candidate_hash,
                    byte_length=10,
                    body_line_count=1,
                    findings=[],
                )
            ],
            failure_signatures=[],
        )
        if status == "passed"
        else None
    )
    succeeded = result is not None
    material = {
        "schema_id": "zhimeng.post-draft-gate-receipt.v1",
        "gate_receipt_id": "post-draft-gate-" + receipt_hex,
        "change_set_id": change_set_id,
        "project_id": PROJECT_ID,
        "review_revision": 1,
        "base_revision": 1,
        "base_fingerprint": base_fingerprint,
        "candidate_hash": candidate_hash,
        "validator_id": "creation-suite-draft-gate",
        "validator_version": "1.0.0",
        "validator_manifest_fingerprint": "d" * 64,
        "validator_package_fingerprint": "e" * 64,
        "context_pack_id": None,
        "context_pack_hash": None,
        "knowledge_fingerprint": None,
        "candidate_file_count": 1,
        "skill_receipt_id": "skill-receipt-" + receipt_hex if succeeded else None,
        "skill_receipt_hash": "f" * 64 if succeeded else None,
        "skill_output_hash": "1" * 64 if succeeded else None,
        "execution_status": "succeeded" if succeeded else "failed",
        "status": status,
        "result": result,
        "error_code": None if succeeded else "gate_execution_failed",
        "error_message": None if succeeded else "Fixture execution failed.",
        "quality_assessed": False,
        "formal_draft_allowed": False,
        "created_at": created_at,
    }
    draft = PostDraftGateReceipt.model_construct(
        **material,
        receipt_hash="0" * 64,
    )
    receipt = PostDraftGateReceipt(
        **material,
        receipt_hash=post_draft_gate_receipt_hash(draft),
    )
    return receipt.model_dump(mode="json", by_alias=True)


def _replace_gate_table_with_legacy_unique_identity(
    connection: sqlite3.Connection,
    *,
    extra_constraint: str = "",
) -> None:
    columns = (
        "id, change_set_id, project_binding_id, review_revision, candidate_hash, "
        "base_revision, base_fingerprint, context_pack_id, context_pack_hash, "
        "knowledge_fingerprint, validator_id, validator_version, "
        "validator_manifest_fingerprint, validator_package_fingerprint, "
        "candidate_file_count, skill_receipt_id, skill_receipt_hash, status, "
        "receipt_hash, payload_json, created_at"
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP INDEX idx_post_draft_gate_change_set_created")
    connection.execute("DROP INDEX idx_post_draft_gate_identity")
    connection.execute(
        f"""
        CREATE TABLE post_draft_gate_receipt_legacy (
            id TEXT PRIMARY KEY,
            change_set_id TEXT NOT NULL REFERENCES change_set(id) ON DELETE CASCADE,
            project_binding_id TEXT NOT NULL
                REFERENCES project_binding(id) ON DELETE RESTRICT,
            review_revision INTEGER NOT NULL CHECK (review_revision >= 0),
            candidate_hash TEXT NOT NULL CHECK (length(candidate_hash) = 64),
            base_revision INTEGER NOT NULL CHECK (base_revision >= 1),
            base_fingerprint TEXT NOT NULL CHECK (length(base_fingerprint) = 64),
            context_pack_id TEXT REFERENCES context_pack(id) ON DELETE RESTRICT,
            context_pack_hash TEXT,
            knowledge_fingerprint TEXT,
            validator_id TEXT NOT NULL,
            validator_version TEXT NOT NULL,
            validator_manifest_fingerprint TEXT NOT NULL,
            validator_package_fingerprint TEXT NOT NULL,
            candidate_file_count INTEGER NOT NULL CHECK (candidate_file_count >= 1),
            skill_receipt_id TEXT,
            skill_receipt_hash TEXT,
            status TEXT NOT NULL CHECK (status IN ('passed', 'blocked', 'failed')),
            receipt_hash TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (
                change_set_id,
                review_revision,
                candidate_hash,
                validator_package_fingerprint
            )
            {extra_constraint}
        ) STRICT
        """
    )
    connection.execute(
        f"""
        INSERT INTO post_draft_gate_receipt_legacy ({columns})
        SELECT {columns} FROM post_draft_gate_receipt
        """
    )
    connection.execute("DROP TABLE post_draft_gate_receipt")
    connection.execute(
        "ALTER TABLE post_draft_gate_receipt_legacy "
        "RENAME TO post_draft_gate_receipt"
    )
    connection.execute(
        "CREATE INDEX idx_post_draft_gate_status_fixture "
        "ON post_draft_gate_receipt(status, created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX idx_post_draft_gate_identity_fixture "
        "ON post_draft_gate_receipt("
        "change_set_id, review_revision, candidate_hash, "
        "validator_package_fingerprint)"
    )
    connection.execute("PRAGMA user_version = 16")


def _replace_gate_table_with_weak_canonical_columns(
    connection: sqlite3.Connection,
) -> None:
    columns = (
        "id, change_set_id, project_binding_id, attempt_ordinal, review_revision, "
        "candidate_hash, base_revision, base_fingerprint, context_pack_id, "
        "context_pack_hash, knowledge_fingerprint, validator_id, validator_version, "
        "validator_manifest_fingerprint, validator_package_fingerprint, "
        "candidate_file_count, skill_receipt_id, skill_receipt_hash, status, "
        "receipt_hash, payload_json, created_at"
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE post_draft_gate_receipt_weak (
            id TEXT,
            change_set_id TEXT,
            project_binding_id TEXT,
            attempt_ordinal INTEGER,
            review_revision INTEGER,
            candidate_hash TEXT,
            base_revision INTEGER,
            base_fingerprint TEXT,
            context_pack_id TEXT,
            context_pack_hash TEXT,
            knowledge_fingerprint TEXT,
            validator_id TEXT,
            validator_version TEXT,
            validator_manifest_fingerprint TEXT,
            validator_package_fingerprint TEXT,
            candidate_file_count INTEGER,
            skill_receipt_id TEXT,
            skill_receipt_hash TEXT,
            status TEXT,
            receipt_hash TEXT,
            payload_json TEXT,
            created_at TEXT
        )
        """
    )
    connection.execute(
        f"""
        INSERT INTO post_draft_gate_receipt_weak ({columns})
        SELECT {columns} FROM post_draft_gate_receipt
        """
    )
    connection.execute("DROP TABLE post_draft_gate_receipt")
    connection.execute(
        "ALTER TABLE post_draft_gate_receipt_weak "
        "RENAME TO post_draft_gate_receipt"
    )
    connection.execute("PRAGMA user_version = 16")


def _source_import_payload(
    status: SourceImportStatus,
    *,
    ordinal: int,
    now: datetime,
) -> dict[str, object]:
    artifacts = [
        SourceArtifact(
            kind=f"fixture-{index}",
            path=f"C:/runtime/source-imports/fixture-{ordinal}/{index}.json",
            size_bytes=index,
            sha256=f"{index:x}" * 64,
        )
        for index in range(1, 5)
    ]
    requires_source = status in {
        SourceImportStatus.SOURCE_READY,
        SourceImportStatus.DRAFTING,
        SourceImportStatus.AWAITING_REVIEW,
        SourceImportStatus.FORMALIZING,
        SourceImportStatus.COMPLETED,
    }
    requires_draft = status in {
        SourceImportStatus.AWAITING_REVIEW,
        SourceImportStatus.FORMALIZING,
        SourceImportStatus.COMPLETED,
    }
    source_packet = (
        PreparedSourcePacket(
            title=f"Recovery Fixture {ordinal}",
            author="Fixture Author",
            download_scope="full",
            packet_dir=f"C:/runtime/source-imports/fixture-{ordinal}",
            packet_manifest_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/manifest.json"
            ),
            epub_path=f"C:/runtime/source-imports/fixture-{ordinal}/book.epub",
            downloaded_epub_path=(
                f"C:/downloads/source-import-fixture-{ordinal}.epub"
            ),
            text_path=f"C:/runtime/source-imports/fixture-{ordinal}/book.txt",
            metadata_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/metadata.json"
            ),
            char_count=100,
            chapter_marker_count=2,
            artifacts=artifacts,
        )
        if requires_source
        else None
    )
    distillation_draft = (
        DistillationDraft(
            output_dir=f"C:/runtime/source-imports/fixture-{ordinal}/draft",
            cards_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/draft/cards.json"
            ),
            report_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/draft/report.md"
            ),
            validation_report_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/draft/validation.md"
            ),
            envelope_path=(
                f"C:/runtime/source-imports/fixture-{ordinal}/draft/envelope.json"
            ),
            card_count=1,
            artifacts=artifacts,
        )
        if requires_draft
        else None
    )
    record = SourceImportJob(
        import_id=f"source-import-{ordinal:032x}",
        query=f"Recovery Fixture {ordinal}",
        candidate=TomatoSearchCandidate(
            index=1,
            book_id=str(10_000 + ordinal),
            title=f"Recovery Fixture {ordinal}",
            author="Fixture Author",
        ),
        status=status,
        progress=SourceImportProgress(
            stage=status.value,
            message=f"Fixture was {status.value}",
            saved_chapters=3,
            total_chapters=10,
            percent=30,
        ),
        source_packet=source_packet,
        distillation_draft=distillation_draft,
        error=(
            "Fixture failed"
            if status is SourceImportStatus.FAILED
            else "Already interrupted"
            if status is SourceImportStatus.INTERRUPTED
            else None
        ),
        created_at=now,
        updated_at=now,
    )
    return record.model_dump(mode="json", by_alias=True)


def test_initialize_creates_wal_database_and_required_tables(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "nested" / "runtime.sqlite3")

    health = store.initialize()

    assert health.ready is True
    assert health.journal_mode == "wal"
    assert health.schema_version == SCHEMA_VERSION
    with store.connection() as connection:
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        run_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(run)").fetchall()
        }
        context_pack_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(context_pack)").fetchall()
        }
        tool_call_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(tool_call)").fetchall()
        }
        provenance_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(change_set_provenance)"
            ).fetchall()
        }
        gate_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(post_draft_gate_receipt)"
            ).fetchall()
        }
        gate_indexes = {
            str(row["name"]): (bool(row["is_unique"]), bool(row["partial"]))
            for row in connection.execute(
                """
                SELECT name, "unique" AS is_unique, partial
                FROM pragma_index_list('post_draft_gate_receipt')
                """
            ).fetchall()
        }
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    assert REQUIRED_TABLES.issubset(table_names)
    assert REQUIRED_COLUMNS["run"].issubset(run_columns)
    assert REQUIRED_COLUMNS["context_pack"].issubset(context_pack_columns)
    assert REQUIRED_COLUMNS["tool_call"].issubset(tool_call_columns)
    assert REQUIRED_COLUMNS["change_set_provenance"].issubset(provenance_columns)
    assert REQUIRED_COLUMNS["post_draft_gate_receipt"].issubset(gate_columns)
    assert gate_indexes["idx_post_draft_gate_change_set_created"] == (False, False)
    assert gate_indexes["idx_post_draft_gate_identity"] == (False, False)
    assert gate_indexes["idx_post_draft_gate_terminal_identity"] == (True, True)
    assert foreign_keys == 1


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")

    first = store.initialize()
    second = store.initialize()

    assert first == second
    assert store.probe().ready is True


def test_v23_migration_adds_session_pinning_without_losing_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    project, session, _, _ = _create_session_and_run(store)
    with store.connection() as connection:
        connection.execute("ALTER TABLE execution_session DROP COLUMN pinned")
        connection.execute("PRAGMA user_version = 23")

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    assert health.schema_version == SCHEMA_VERSION
    restored = AgentStore(migrated).list_sessions(project.project_id)
    assert [item.session_id for item in restored] == [session.session_id]
    assert restored[0].pinned is False


def test_failed_post_draft_gate_receipt_allows_a_new_successful_attempt(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    failed_at = datetime(2026, 7, 22, 2, 0, tzinfo=UTC)
    retry_at = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)
    failed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="f" * 32,
        status="failed",
        created_at=failed_at,
    )
    passed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="0" * 32,
        status="passed",
        created_at=retry_at,
    )

    assert store.save_post_draft_gate_receipt(failed) == failed
    assert store.save_post_draft_gate_receipt(failed) == failed
    assert store.save_post_draft_gate_receipt(passed) == passed

    assert store.get_post_draft_gate_receipt(str(failed["gate_receipt_id"])) == failed
    assert store.get_latest_post_draft_gate_receipt(change_set_id) == passed
    assert (
        store.find_post_draft_gate_receipt(
            change_set_id=change_set_id,
            review_revision=1,
            candidate_hash="b" * 64,
            base_revision=1,
            base_fingerprint=base_fingerprint,
            context_pack_id=None,
            context_pack_hash=None,
            knowledge_fingerprint=None,
            validator_package_fingerprint="e" * 64,
        )
        == passed
    )
    assert store.list_post_draft_gate_receipts(change_set_id) == [passed, failed]
    with store.connection() as connection:
        ordinals = [
            int(row["attempt_ordinal"])
            for row in connection.execute(
                """
                SELECT attempt_ordinal
                FROM post_draft_gate_receipt
                WHERE change_set_id = ?
                ORDER BY attempt_ordinal DESC
                """,
                (change_set_id,),
            ).fetchall()
        ]
    assert ordinals == [2, 1]

    conflicting_terminal_attempt = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="1" * 32,
        status="passed",
        created_at=retry_at,
    )
    with pytest.raises(RuntimeError, match="different immutable content"):
        store.save_post_draft_gate_receipt(conflicting_terminal_attempt)


def test_post_draft_gate_terminal_receipt_is_unique_under_concurrent_writers(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    created_at = datetime(2026, 7, 21, 2, 30, tzinfo=UTC)
    candidates = [
        _gate_receipt_payload(
            change_set_id=change_set_id,
            base_fingerprint=base_fingerprint,
            receipt_hex=character * 32,
            status="passed",
            created_at=created_at,
        )
        for character in ("7", "8")
    ]
    barrier = Barrier(2)

    def save(payload: dict[str, object]) -> tuple[str, object]:
        barrier.wait()
        try:
            return "saved", store.save_post_draft_gate_receipt(payload)
        except RuntimeError as exc:
            return "rejected", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, candidates))

    assert [status for status, _ in outcomes].count("saved") == 1
    assert [status for status, _ in outcomes].count("rejected") == 1
    rejected = next(value for status, value in outcomes if status == "rejected")
    assert "different immutable content" in str(rejected)
    with store.connection() as connection:
        terminals = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM post_draft_gate_receipt
                WHERE change_set_id = ? AND status IN ('passed', 'blocked')
                """,
                (change_set_id,),
            ).fetchone()[0]
        )
    assert terminals == 1


def test_v16_gate_migration_rebuilds_legacy_unique_identity_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    created_at = datetime(2026, 7, 21, 3, 0, tzinfo=UTC)
    failed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="4" * 32,
        status="failed",
        created_at=created_at,
    )
    store.save_post_draft_gate_receipt(failed)
    with store.connection() as connection:
        _replace_gate_table_with_legacy_unique_identity(connection)
        connection.execute(
            "CREATE TABLE post_draft_gate_audit(receipt_id TEXT NOT NULL) STRICT"
        )
        connection.execute(
            """
            CREATE TRIGGER trg_post_draft_gate_audit
            AFTER INSERT ON post_draft_gate_receipt
            BEGIN
                INSERT INTO post_draft_gate_audit(receipt_id) VALUES (NEW.id);
            END
            """
        )

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    assert health.schema_version == SCHEMA_VERSION
    assert migrated.get_post_draft_gate_receipt(str(failed["gate_receipt_id"])) == failed
    with migrated.connection() as connection:
        index_rows = connection.execute(
            """
            SELECT name, "unique" AS is_unique, origin, partial
            FROM pragma_index_list('post_draft_gate_receipt')
            """
        ).fetchall()
        index_names = {str(row["name"]) for row in index_rows}
        full_unique_identities = []
        partial_terminal_identities = []
        for row in index_rows:
            columns = tuple(
                str(item["name"])
                for item in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (str(row["name"]),),
                ).fetchall()
            )
            if bool(row["is_unique"]) and columns == (
                "change_set_id",
                "review_revision",
                "candidate_hash",
                "validator_package_fingerprint",
            ):
                target = (
                    partial_terminal_identities
                    if bool(row["partial"])
                    else full_unique_identities
                )
                target.append(str(row["name"]))
        foreign_key_targets = {
            str(row["table"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(post_draft_gate_receipt)"
            ).fetchall()
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        trigger_names = {
            str(row["name"])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'post_draft_gate_receipt'
                """
            ).fetchall()
        }
        attempt_ordinal = int(
            connection.execute(
                """
                SELECT attempt_ordinal FROM post_draft_gate_receipt WHERE id = ?
                """,
                (failed["gate_receipt_id"],),
            ).fetchone()[0]
        )

    assert full_unique_identities == []
    assert partial_terminal_identities == ["idx_post_draft_gate_terminal_identity"]
    assert {
        "idx_post_draft_gate_change_set_created",
        "idx_post_draft_gate_identity",
        "idx_post_draft_gate_terminal_identity",
        "idx_post_draft_gate_identity_fixture",
        "idx_post_draft_gate_status_fixture",
    }.issubset(index_names)
    assert foreign_key_targets == {"change_set", "project_binding", "context_pack"}
    assert violations == []
    assert trigger_names == {"trg_post_draft_gate_audit"}
    assert attempt_ordinal == 1

    passed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="5" * 32,
        status="passed",
        created_at=created_at,
    )
    assert migrated.save_post_draft_gate_receipt(passed) == passed
    assert {
        str(item["gate_receipt_id"])
        for item in migrated.list_post_draft_gate_receipts(change_set_id)
    } == {str(failed["gate_receipt_id"]), str(passed["gate_receipt_id"])}
    with migrated.connection() as connection:
        assert connection.execute(
            "SELECT receipt_id FROM post_draft_gate_audit"
        ).fetchall()[0][0] == passed["gate_receipt_id"]
        before = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE tbl_name = 'post_draft_gate_receipt'
            ORDER BY type, name
            """
        ).fetchall()

    migrated.initialize()
    with migrated.connection() as connection:
        after = connection.execute(
            """
            SELECT type, name, sql
            FROM sqlite_master
            WHERE tbl_name = 'post_draft_gate_receipt'
            ORDER BY type, name
            """
        ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_v16_gate_audit_repairs_missing_constraints_and_foreign_keys(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    failed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="9" * 32,
        status="failed",
        created_at=datetime(2026, 7, 21, 3, 30, tzinfo=UTC),
    )
    store.save_post_draft_gate_receipt(failed)
    with store.connection() as connection:
        _replace_gate_table_with_weak_canonical_columns(connection)

    migrated = SqliteStore(database_path)
    assert migrated.initialize().ready is True
    assert migrated.get_post_draft_gate_receipt(str(failed["gate_receipt_id"])) == failed

    with migrated.connection() as connection:
        table_sql = str(
            connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = 'post_draft_gate_receipt'
                """
            ).fetchone()[0]
        )
        columns = {
            str(row["name"]): row
            for row in connection.execute(
                "PRAGMA table_xinfo(post_draft_gate_receipt)"
            ).fetchall()
        }
        foreign_keys = {
            str(row["from"]): (
                str(row["table"]),
                str(row["to"]),
                str(row["on_delete"]),
            )
            for row in connection.execute(
                "PRAGMA foreign_key_list(post_draft_gate_receipt)"
            ).fetchall()
        }
        unique_columns = {
            tuple(
                str(item["name"])
                for item in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (str(row["name"]),),
                ).fetchall()
            )
            for row in connection.execute(
                "PRAGMA index_list(post_draft_gate_receipt)"
            ).fetchall()
            if bool(row["unique"])
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert table_sql.rstrip().endswith("STRICT")
    assert bool(columns["id"]["pk"]) is True
    assert bool(columns["id"]["notnull"]) is True
    assert bool(columns["attempt_ordinal"]["notnull"]) is True
    assert foreign_keys == {
        "change_set_id": ("change_set", "id", "CASCADE"),
        "project_binding_id": ("project_binding", "id", "RESTRICT"),
        "context_pack_id": ("context_pack", "id", "RESTRICT"),
    }
    assert ("receipt_hash",) in unique_columns
    assert ("change_set_id", "attempt_ordinal") in unique_columns
    assert "CHECK (attempt_ordinal >= 1)" in table_sql
    assert "CHECK ((context_pack_id IS NULL) = (context_pack_hash IS NULL))" in table_sql
    assert violations == []


@pytest.mark.parametrize(
    ("extra_constraint", "error_pattern"),
    [
        (", UNIQUE (status, receipt_hash)", "automatic constraint"),
        (", CHECK (candidate_file_count < 99)", "CHECK constraints"),
    ],
)
def test_v16_gate_audit_fails_closed_for_unknown_table_constraints(
    tmp_path: Path,
    extra_constraint: str,
    error_pattern: str,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    failed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="a" * 32,
        status="failed",
        created_at=datetime(2026, 7, 21, 3, 45, tzinfo=UTC),
    )
    store.save_post_draft_gate_receipt(failed)
    with store.connection() as connection:
        _replace_gate_table_with_legacy_unique_identity(
            connection,
            extra_constraint=extra_constraint,
        )

    with pytest.raises(RuntimeError, match=error_pattern):
        SqliteStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(post_draft_gate_receipt)"
            ).fetchall()
        }
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM post_draft_gate_receipt"
            ).fetchone()[0]
        )
    assert "attempt_ordinal" not in columns
    assert receipt_count == 1


def test_v16_gate_rebuild_failure_rolls_back_table_indexes_and_triggers(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    change_set_id, base_fingerprint = _create_gate_change_set(store)
    failed = _gate_receipt_payload(
        change_set_id=change_set_id,
        base_fingerprint=base_fingerprint,
        receipt_hex="b" * 32,
        status="failed",
        created_at=datetime(2026, 7, 21, 4, 0, tzinfo=UTC),
    )
    store.save_post_draft_gate_receipt(failed)
    with store.connection() as connection:
        connection.create_collation(
            "gate_legacy_only",
            lambda left, right: (left > right) - (left < right),
        )
        _replace_gate_table_with_legacy_unique_identity(connection)
        connection.execute(
            """
            CREATE INDEX idx_post_draft_gate_legacy_collation
            ON post_draft_gate_receipt(status COLLATE gate_legacy_only)
            """
        )
        connection.execute(
            "CREATE TABLE post_draft_gate_rollback_audit(receipt_id TEXT) STRICT"
        )
        connection.execute(
            """
            CREATE TRIGGER trg_post_draft_gate_rollback
            AFTER INSERT ON post_draft_gate_receipt
            BEGIN
                INSERT INTO post_draft_gate_rollback_audit VALUES (NEW.id);
            END
            """
        )

    with pytest.raises(sqlite3.OperationalError, match="collation"):
        SqliteStore(database_path).initialize()

    with sqlite3.connect(database_path) as connection:
        connection.create_collation(
            "gate_legacy_only",
            lambda left, right: (left > right) - (left < right),
        )
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(post_draft_gate_receipt)"
            ).fetchall()
        }
        schema_objects = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                """
                SELECT type, name FROM sqlite_master
                WHERE tbl_name = 'post_draft_gate_receipt'
                """
            ).fetchall()
        }
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM post_draft_gate_receipt"
            ).fetchone()[0]
        )
        rebuild_table = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'post_draft_gate_receipt_v16_rebuild'
            """
        ).fetchone()

    assert "attempt_ordinal" not in columns
    assert ("index", "idx_post_draft_gate_legacy_collation") in schema_objects
    assert ("trigger", "trg_post_draft_gate_rollback") in schema_objects
    assert receipt_count == 1
    assert rebuild_table is None


def test_source_import_startup_recovery_is_complete_and_idempotent(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    recorded_at = datetime(2026, 7, 20, tzinfo=UTC)
    active_statuses = (
        SourceImportStatus.QUEUED,
        SourceImportStatus.DOWNLOADING,
        SourceImportStatus.SOURCE_READY,
        SourceImportStatus.DRAFTING,
        SourceImportStatus.FORMALIZING,
        SourceImportStatus.CANCEL_REQUESTED,
    )
    terminal_statuses = (
        SourceImportStatus.AWAITING_REVIEW,
        SourceImportStatus.COMPLETED,
        SourceImportStatus.FAILED,
        SourceImportStatus.CANCELLED,
        SourceImportStatus.INTERRUPTED,
    )
    active_ids: list[str] = []
    terminal_ids: list[str] = []
    for ordinal, status in enumerate((*active_statuses, *terminal_statuses), start=1):
        payload = _source_import_payload(status, ordinal=ordinal, now=recorded_at)
        store.create_source_import_job(payload)
        target = active_ids if status in active_statuses else terminal_ids
        target.append(str(payload["import_id"]))

    terminal_before = {
        import_id: store.get_source_import_job(import_id) for import_id in terminal_ids
    }

    assert store.recover_interrupted_source_imports() == len(active_statuses)
    assert store.recover_interrupted_source_imports() == 0

    for import_id in active_ids:
        recovered = store.get_source_import_job(import_id)
        assert recovered is not None
        assert recovered["status"] == "interrupted"
        assert recovered["progress"] == {
            "stage": "interrupted",
            "message": (
                "Runtime stopped before this import reached a durable review point"
            ),
            "saved_chapters": 3,
            "total_chapters": 10,
            "percent": 30,
        }
        assert "retry" in str(recovered["error"])
        assert recovered["updated_at"] != recorded_at.isoformat()

    assert {
        import_id: store.get_source_import_job(import_id) for import_id in terminal_ids
    } == terminal_before


def test_app_startup_recovers_source_import_without_adapter(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    payload = _source_import_payload(
        SourceImportStatus.DOWNLOADING,
        ordinal=100,
        now=datetime(2026, 7, 20, tzinfo=UTC),
    )
    store.create_source_import_job(payload)
    settings = RuntimeSettings(
        environment="test",
        database_path=database_path,
        managed_skill_root=None,
        tomato_skill_root=None,
        allowed_origins=("http://127.0.0.1:1420",),
        sse_heartbeat_seconds=0.05,
    )
    app = create_app(settings)

    with TestClient(app):
        assert app.state.runtime.startup_error is None

    recovered = store.get_source_import_job(str(payload["import_id"]))
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert recovered["progress"]["saved_chapters"] == 3
    assert "retry" in str(recovered["error"])


def test_project_binding_revision_persists_and_only_advances_on_fingerprint_change(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()

    first = store.sync_project_binding(
        project_id="project-0123456789abcdef01234567",
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint="a" * 64,
    )
    unchanged = store.sync_project_binding(
        project_id=first.project_id,
        root_path=first.root_path,
        display_name="Renamed display only",
        fingerprint=first.fingerprint,
    )

    reopened = SqliteStore(database_path)
    reopened.initialize()
    changed = reopened.sync_project_binding(
        project_id=first.project_id,
        root_path=first.root_path,
        display_name=unchanged.display_name,
        fingerprint="b" * 64,
    )

    assert first.revision == 1
    assert unchanged.revision == 1
    assert changed.revision == 2
    assert reopened.get_project_binding(first.project_id) == changed
    assert reopened.list_project_bindings() == [changed]


def test_initialize_migrates_v5_without_losing_run_activity_or_event_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    _, _, run, _ = _create_session_and_run(store)

    with store.connection() as connection:
        before_run = tuple(
            connection.execute(
                """
                SELECT id, session_id, objective, status, last_activity_sequence
                FROM run WHERE id = ?
                """,
                (run.run_id,),
            ).fetchone()
        )
        before_activities = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, sequence, kind, payload_json
                FROM activity WHERE run_id = ? ORDER BY sequence
                """,
                (run.run_id,),
            ).fetchall()
        ]
        before_events = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT sequence, event_type, activity_id, payload_json
                FROM run_event WHERE run_id = ? ORDER BY sequence
                """,
                (run.run_id,),
            ).fetchall()
        ]

    legacy = sqlite3.connect(database_path)
    legacy.execute("PRAGMA foreign_keys = OFF")
    legacy.execute("DROP INDEX idx_run_context_pack_unique")
    legacy.execute("ALTER TABLE run DROP COLUMN context_pack_id")
    legacy.execute("DROP TABLE context_pack")
    legacy.execute("PRAGMA user_version = 5")
    legacy.close()

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    with migrated.connection() as connection:
        after_run_row = connection.execute(
            """
            SELECT id, session_id, objective, status, last_activity_sequence,
                   context_pack_id
            FROM run WHERE id = ?
            """,
            (run.run_id,),
        ).fetchone()
        after_activities = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT id, sequence, kind, payload_json
                FROM activity WHERE run_id = ? ORDER BY sequence
                """,
                (run.run_id,),
            ).fetchall()
        ]
        after_events = [
            tuple(row)
            for row in connection.execute(
                """
                SELECT sequence, event_type, activity_id, payload_json
                FROM run_event WHERE run_id = ? ORDER BY sequence
                """,
                (run.run_id,),
            ).fetchall()
        ]
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert health.ready is True
    assert health.schema_version == SCHEMA_VERSION
    assert tuple(after_run_row[:-1]) == before_run
    assert after_run_row["context_pack_id"] is None
    assert after_activities == before_activities
    assert after_events == before_events
    assert foreign_key_violations == []


def test_initialize_migrates_v6_tool_calls_without_losing_legacy_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    _, _, run, _ = _create_session_and_run(store)

    legacy = sqlite3.connect(database_path)
    legacy.execute("PRAGMA foreign_keys = OFF")
    legacy.execute("DROP TABLE change_set_provenance")
    legacy.execute("DROP TABLE tool_call")
    legacy.executescript(
        """
        CREATE TABLE tool_call (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
            tool_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')
            ),
            idempotency_key TEXT,
            input_json TEXT NOT NULL,
            output_json TEXT,
            error_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE (run_id, idempotency_key)
        ) STRICT;
        CREATE INDEX idx_tool_call_run_status ON tool_call(run_id, status);
        """
    )
    legacy.execute(
        """
        INSERT INTO tool_call(
            id, run_id, tool_name, status, idempotency_key,
            input_json, output_json, error_json, started_at, finished_at
        ) VALUES (?, ?, 'project.inspect', 'succeeded', ?, ?, ?, NULL, ?, ?)
        """,
        (
            "toolcall-legacy-v6-0001",
            run.run_id,
            "legacy-tool-call-key",
            '{"scope":"project"}',
            '{"ready":true}',
            "2026-07-20T01:00:00+00:00",
            "2026-07-20T01:00:01+00:00",
        ),
    )
    legacy.execute("PRAGMA user_version = 6")
    legacy.commit()
    legacy.close()

    migrated = SqliteStore(database_path)
    health = migrated.initialize()
    tool_call = AgentStore(migrated).get_tool_call("toolcall-legacy-v6-0001")

    assert health.schema_version == SCHEMA_VERSION
    assert tool_call.run_id == run.run_id
    assert tool_call.tool_name == "project.inspect"
    assert tool_call.tool_version == "0"
    assert tool_call.status == "succeeded"
    assert tool_call.arguments == {"scope": "project"}
    assert tool_call.output == {"ready": True}
    assert len(tool_call.request_hash) == 64
    assert tool_call.result_hash is not None and len(tool_call.result_hash) == 64
    assert tool_call.call_activity_id is None
    assert tool_call.result_activity_id is None
    with migrated.connection() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "change_set_provenance" in tables
    assert violations == []


def test_agent_change_set_and_provenance_are_atomic_and_strongly_linked(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project, session, run, agent_store = _create_session_and_run(store)
    context_pack_id = "context-pack-agent-provenance"
    with store.connection() as connection:
        connection.execute(
            CONTEXT_PACK_INSERT,
            _context_pack_values(
                context_pack_id=context_pack_id,
                session_id=session.session_id,
            ),
        )
        connection.execute(
            "UPDATE run SET context_pack_id = ? WHERE id = ?",
            (context_pack_id, run.run_id),
        )
    agent_store.transition_run(
        run.run_id,
        RunStatus.STARTING,
        detail="Start tool fixture",
    )
    agent_store.transition_run(
        run.run_id,
        RunStatus.RUNNING,
        detail="Run tool fixture",
    )
    request_hash = "e" * 64
    tool_call, replayed = agent_store.begin_tool_call(
        run.run_id,
        tool_call_id="toolcall-agent-provenance-0001",
        tool_name="changeset.propose",
        tool_version="1",
        arguments={"title": "Revise chapter"},
        request_hash=request_hash,
        idempotency_key="changeset-proposal-key-0001",
    )
    assert replayed is False

    created_at = "2026-07-20T02:00:00+00:00"
    payload: dict[str, object] = {
        "schema": "zhimeng.change-set.v1",
        "change_set_id": "changeset-0123456789abcdef0123456789abcdef",
        "project_id": project.project_id,
        "base_revision": project.revision,
        "base_fingerprint": project.fingerprint,
        "title": "Revise chapter",
        "summary": "Runtime-owned Agent proposal",
        "origin": "agent",
        "source_run_id": run.run_id,
        "source_tool_call_id": tool_call.tool_call_id,
        "source_request_hash": request_hash,
        "source_skill_ids": [],
        "status": "reviewing",
        "review_revision": 0,
        "changes": [{"fixture": True}],
        "writeback": None,
        "rollback": None,
        "error": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    provenance: dict[str, object] = {
        "change_set_id": payload["change_set_id"],
        "run_id": run.run_id,
        "tool_call_id": tool_call.tool_call_id,
        "context_pack_id": context_pack_id,
        "project_id": project.project_id,
        "project_revision": project.revision,
        "project_fingerprint": project.fingerprint,
        "request_hash": request_hash,
        "created_at": created_at,
    }

    store.create_agent_change_set(payload, provenance)

    assert store.get_change_set_provenance(str(payload["change_set_id"])) == provenance
    by_tool = store.get_change_set_by_tool_call(tool_call.tool_call_id)
    assert by_tool == (payload, provenance)

    duplicate_payload = dict(payload)
    duplicate_payload["change_set_id"] = "changeset-fedcba9876543210fedcba9876543210"
    duplicate_provenance = dict(provenance)
    duplicate_provenance["change_set_id"] = duplicate_payload["change_set_id"]
    with pytest.raises(RuntimeError, match="provenance already exists"):
        store.create_agent_change_set(duplicate_payload, duplicate_provenance)
    assert store.get_change_set(str(duplicate_payload["change_set_id"])) is None

    with store.connection() as connection:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == []


def test_context_pack_foreign_keys_constraints_and_unique_run_consumption(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project, session, first_run, agent_store = _create_session_and_run(store)
    agent_store.mark_interrupted(first_run.run_id, detail="Create a second fixture Run")
    second_run, _, _ = agent_store.create_run(
        session_id=session.session_id,
        message="Second Run for unique Context Pack consumption.",
        expected_project_revision=project.revision,
        expected_project_fingerprint=project.fingerprint,
        active_chapter_id="CH002",
        idempotency_key="database-migration-run-0002",
    )

    with store.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                CONTEXT_PACK_INSERT,
                _context_pack_values(
                    context_pack_id="context-pack-missing-session",
                    session_id="session-does-not-exist",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                CONTEXT_PACK_INSERT,
                _context_pack_values(
                    context_pack_id="context-pack-missing-project",
                    session_id=session.session_id,
                    project_id="project-does-not-exist",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                CONTEXT_PACK_INSERT,
                _context_pack_values(
                    context_pack_id="context-pack-invalid-readiness",
                    session_id=session.session_id,
                    readiness="unknown",
                ),
            )

        context_pack_id = "context-pack-database-fixture"
        connection.execute(
            CONTEXT_PACK_INSERT,
            _context_pack_values(
                context_pack_id=context_pack_id,
                session_id=session.session_id,
            ),
        )
        connection.execute(
            "UPDATE run SET context_pack_id = ? WHERE id = ?",
            (context_pack_id, first_run.run_id),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run SET context_pack_id = ? WHERE id = ?",
                (context_pack_id, second_run.run_id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE run SET context_pack_id = ? WHERE id = ?",
                ("context-pack-does-not-exist", second_run.run_id),
            )

        run_foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
            for row in connection.execute("PRAGMA foreign_key_list(run)").fetchall()
        }
        context_pack_foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
            for row in connection.execute(
                "PRAGMA foreign_key_list(context_pack)"
            ).fetchall()
        }
        context_pack_indexes = {
            str(row[1]): (bool(row[2]), bool(row[4]))
            for row in connection.execute("PRAGMA index_list(run)").fetchall()
        }
        bound_pack = connection.execute(
            "SELECT context_pack_id FROM run WHERE id = ?",
            (first_run.run_id,),
        ).fetchone()[0]
        unbound_pack = connection.execute(
            "SELECT context_pack_id FROM run WHERE id = ?",
            (second_run.run_id,),
        ).fetchone()[0]

    assert (
        "context_pack_id",
        "context_pack",
        "id",
        "RESTRICT",
    ) in run_foreign_keys
    assert (
        "session_id",
        "execution_session",
        "id",
        "RESTRICT",
    ) in context_pack_foreign_keys
    assert (
        "project_binding_id",
        "project_binding",
        "id",
        "RESTRICT",
    ) in context_pack_foreign_keys
    assert context_pack_indexes["idx_run_context_pack_unique"] == (True, True)
    assert bound_pack == context_pack_id
    assert unbound_pack is None


def test_initialize_migrates_phase_one_project_binding(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE project_binding (
            id TEXT PRIMARY KEY,
            root_path TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO project_binding(
            id, root_path, display_name, revision, created_at, updated_at
        ) VALUES (
            'project-0123456789abcdef01234567', 'C:\\Novels\\Legacy',
            'Legacy', 4, '2026-07-17T00:00:00Z', '2026-07-17T00:00:00Z'
        );
        CREATE TABLE run (
            id TEXT PRIMARY KEY,
            project_binding_id TEXT,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'waiting_approval', 'paused', 'completed', 'failed', 'cancelled')
            ),
            idempotency_key TEXT UNIQUE,
            project_revision INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        INSERT INTO run(
            id, project_binding_id, run_type, status, project_revision,
            created_at, updated_at, completed_at
        ) VALUES (
            'run-legacy', 'project-0123456789abcdef01234567', 'legacy',
            'completed', 4, '2026-07-17T00:00:00Z',
            '2026-07-17T00:01:00Z', '2026-07-17T00:01:00Z'
        );
        CREATE TABLE run_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES run(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, sequence)
        );
        INSERT INTO run_event(
            run_id, sequence, event_type, payload_json, created_at
        ) VALUES (
            'run-legacy', 0, 'completed', '{}', '2026-07-17T00:01:00Z'
        );
        INSERT INTO run(
            id, project_binding_id, run_type, status, project_revision,
            created_at, updated_at, completed_at
        ) VALUES (
            'run-legacy-running', 'project-0123456789abcdef01234567', 'legacy',
            'running', 4, '2026-07-17T00:02:00Z',
            '2026-07-17T00:03:00Z', NULL
        );
        INSERT INTO run_event(
            run_id, sequence, event_type, payload_json, created_at
        ) VALUES
            ('run-legacy-running', 0, 'started', '{}', '2026-07-17T00:02:00Z'),
            ('run-legacy-running', 1, 'progress', '{}', '2026-07-17T00:02:30Z'),
            ('run-legacy-running', 2, 'progress', '{}', '2026-07-17T00:03:00Z');
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    store = SqliteStore(database_path)
    health = store.initialize()

    with store.connection() as migrated:
        columns = {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(project_binding)").fetchall()
        }
        run_columns = {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(run)").fetchall()
        }
        context_pack_columns = {
            str(row[1])
            for row in migrated.execute("PRAGMA table_info(context_pack)").fetchall()
        }
        migrated_context_pack_ids = [
            row[0]
            for row in migrated.execute(
                "SELECT context_pack_id FROM run ORDER BY created_at, id"
            ).fetchall()
        ]
        run_status = str(
            migrated.execute("SELECT status FROM run WHERE id = 'run-legacy'").fetchone()[0]
        )
        event_run_id = str(
            migrated.execute("SELECT run_id FROM run_event WHERE sequence = 0").fetchone()[0]
        )
        foreign_key_violations = migrated.execute("PRAGMA foreign_key_check").fetchall()
    assert health.ready is True
    assert health.schema_version == SCHEMA_VERSION
    assert "fingerprint" in columns
    assert {"session_id", "project_fingerprint"}.issubset(run_columns)
    assert "context_pack_id" in run_columns
    assert REQUIRED_COLUMNS["context_pack"].issubset(context_pack_columns)
    assert migrated_context_pack_ids == [None, None]
    assert run_status == "succeeded"
    assert event_run_id == "run-legacy"
    assert foreign_key_violations == []
    legacy = store.get_project_binding("project-0123456789abcdef01234567")
    assert legacy is not None
    assert legacy.fingerprint == ""
    assert legacy.revision == 4
    migrated_sessions = AgentStore(store).list_sessions(legacy.project_id)
    assert len(migrated_sessions) == 2
    assert {session.status.value for session in migrated_sessions} == {"interrupted"}
    migrated_run = AgentStore(store).get_run("run-legacy")
    assert migrated_run.status.value == "succeeded"
    matching_session = next(
        session for session in migrated_sessions if session.session_id == migrated_run.session_id
    )
    assert migrated_run.project_revision.fingerprint == matching_session.project_revision.fingerprint
    assert migrated_run.executor.implementation == "legacy-runtime-record"
    agent_store = AgentStore(store)
    assert agent_store.recover_interrupted() == 1
    recovered = agent_store.get_run("run-legacy-running")
    assert recovered.status.value == "interrupted"
    assert recovered.last_activity_sequence == 1
    assert [item.root.sequence for item in agent_store.list_activities(recovered.run_id)] == [1]
    replay_events = agent_store.list_run_events(recovered.run_id)
    assert [event.sequence for event in replay_events] == [3]
    assert [event.activity.root.sequence for event in replay_events] == [1]


def test_v11_source_import_migration_does_not_invent_execution_receipts(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    import_id = "source-import-" + "a" * 32
    created_at = "2026-07-21T00:00:00+00:00"
    payload = {
        "schema": "zhimeng.source-import.v1",
        "import_id": import_id,
        "provider": "tomato",
        "query": "legacy fixture",
        "candidate": {
            "index": 1,
            "book_id": "12345",
            "title": "Legacy Fixture",
            "author": "Fixture Author",
        },
        "status": "awaiting_review",
        "retry_of": None,
        "progress": {"stage": "awaiting_review", "message": "legacy"},
        "source_packet": None,
        "distillation_draft": None,
        "error": None,
        "created_at": created_at,
        "updated_at": created_at,
    }
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO source_import_job(
                id, provider, query, book_id, status, payload_json,
                created_at, updated_at
            ) VALUES (?, 'tomato', 'legacy fixture', '12345',
                      'awaiting_review', ?, ?, ?)
            """,
            (
                import_id,
                __import__("json").dumps(payload),
                created_at,
                created_at,
            ),
        )
        connection.execute("DROP TABLE source_import_execution_receipt")
        connection.execute("PRAGMA user_version = 11")

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    assert health.schema_version == SCHEMA_VERSION
    assert migrated.get_source_import_execution_receipt(import_id) is None
    with migrated.connection() as connection:
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM source_import_execution_receipt"
            ).fetchone()[0]
        )
    assert receipt_count == 0


def test_v12_receipt_migration_preserves_row_without_inventing_execution_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    import_id = "source-import-" + "d" * 32
    created_at = "2026-07-21T00:00:00+00:00"
    store.create_source_import_job(
        {
            "import_id": import_id,
            "provider": "tomato",
            "query": "legacy receipt fixture",
            "candidate": {"book_id": "67890"},
            "status": "awaiting_review",
            "created_at": created_at,
            "updated_at": created_at,
        }
    )
    with store.connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE source_import_execution_receipt")
        connection.execute(
            """
            CREATE TABLE source_import_execution_receipt (
                id TEXT PRIMARY KEY,
                source_import_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                prepare_invocation_id TEXT NOT NULL UNIQUE,
                prepare_tool_fingerprint TEXT NOT NULL,
                source_packet_fingerprint TEXT NOT NULL,
                distillation_invocation_id TEXT NOT NULL UNIQUE,
                distillation_tool_fingerprint TEXT NOT NULL,
                distillation_input_hash TEXT NOT NULL,
                distillation_output_hash TEXT NOT NULL,
                draft_fingerprint TEXT NOT NULL,
                cards_draft_hash TEXT NOT NULL,
                envelope_hash TEXT NOT NULL,
                receipt_hash TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO source_import_execution_receipt(
                id, source_import_id, request_hash, prepare_invocation_id,
                prepare_tool_fingerprint, source_packet_fingerprint,
                distillation_invocation_id, distillation_tool_fingerprint,
                distillation_input_hash, distillation_output_hash,
                draft_fingerprint, cards_draft_hash, envelope_hash,
                receipt_hash, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "source-import-receipt-" + "d" * 32,
                import_id,
                "1" * 64,
                "source-import-invocation-" + "1" * 32,
                "2" * 64,
                "3" * 64,
                "source-import-invocation-" + "2" * 32,
                "4" * 64,
                "5" * 64,
                "6" * 64,
                "7" * 64,
                "8" * 64,
                "9" * 64,
                "a" * 64,
                '{"schema":"zhimeng.source-import-execution-receipt.v1"}',
                created_at,
            ),
        )
        connection.execute("PRAGMA user_version = 12")

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    assert health.schema_version == SCHEMA_VERSION
    assert migrated.get_source_import_execution_receipt(import_id) is None
    with migrated.connection() as connection:
        row = connection.execute(
            """
            SELECT run_id, revision, idempotency_key
            FROM source_import_execution_receipt
            WHERE source_import_id = ?
            """,
            (import_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == (None, None, None)


def test_v13_migration_preserves_versions_and_releases_abandoned_content_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    created_at = "2026-07-21T00:00:00+00:00"
    import_id = "source-import-" + "e" * 32
    card_id = "work-v14-migration"
    version_id = "knowledge-version-" + "1" * 32
    content_hash = "a" * 64
    with store.connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP INDEX IF EXISTS idx_knowledge_version_card_number")
        connection.execute("DROP INDEX IF EXISTS idx_knowledge_version_card_content")
        connection.execute("DROP TABLE knowledge_card_version")
        connection.execute(
            """
            CREATE TABLE knowledge_card_version (
                id TEXT PRIMARY KEY,
                card_id TEXT NOT NULL REFERENCES knowledge_card(card_id) ON DELETE RESTRICT,
                version_number INTEGER NOT NULL CHECK (version_number >= 1),
                content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
                payload_json TEXT NOT NULL,
                source_candidate_id TEXT NOT NULL
                    REFERENCES knowledge_candidate(id) ON DELETE RESTRICT,
                created_at TEXT NOT NULL,
                UNIQUE (card_id, version_number),
                UNIQUE (card_id, content_hash)
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO source_import_job(
                id, provider, query, book_id, status, payload_json,
                created_at, updated_at
            ) VALUES (?, 'tomato', 'v14 fixture', '14001', 'completed', '{}', ?, ?)
            """,
            (import_id, created_at, created_at),
        )
        for index in (1, 2):
            connection.execute(
                """
                INSERT INTO knowledge_candidate(
                    id, source_import_id, source_card_index, source_draft_hash,
                    lineage_status, origin_json, origin_hash, card_id, card_type,
                    status, revision, content_hash, payload_json, issues_json,
                    promoted_version_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'legacy_unverified', NULL, NULL, ?, 'work',
                          'rejected', 1, ?, '{}', '[]', NULL, ?, ?)
                """,
                (
                    f"knowledge-candidate-{'1' if index == 1 else '2'}" + "0" * 31,
                    import_id,
                    index,
                    str(index) * 64,
                    card_id,
                    content_hash,
                    created_at,
                    created_at,
                ),
            )
        first_candidate = "knowledge-candidate-1" + "0" * 31
        second_candidate = "knowledge-candidate-2" + "0" * 31
        connection.execute(
            """
            INSERT INTO knowledge_card(
                card_id, status, state_revision, active_version_id, created_at, updated_at
            ) VALUES (?, 'active', 1, ?, ?, ?)
            """,
            (card_id, version_id, created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO knowledge_card_version(
                id, card_id, version_number, content_hash,
                payload_json, source_candidate_id, created_at
            ) VALUES (?, ?, 1, ?, '{}', ?, ?)
            """,
            (version_id, card_id, content_hash, first_candidate, created_at),
        )
        connection.execute("PRAGMA user_version = 13")

    migrated = SqliteStore(database_path)
    health = migrated.initialize()

    assert health.schema_version == SCHEMA_VERSION
    with migrated.connection() as connection:
        preserved = connection.execute(
            "SELECT id, content_hash FROM knowledge_card_version WHERE id = ?",
            (version_id,),
        ).fetchone()
        assert preserved is not None
        assert tuple(preserved) == (version_id, content_hash)
        connection.execute(
            """
            INSERT INTO knowledge_card_version(
                id, card_id, version_number, content_hash,
                payload_json, source_candidate_id, created_at
            ) VALUES (?, ?, 2, ?, '{}', ?, ?)
            """,
            (
                "knowledge-version-" + "2" * 32,
                card_id,
                content_hash,
                second_candidate,
                created_at,
            ),
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_source_import_job_and_execution_receipt_publish_atomically(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    import_id = "source-import-" + "b" * 32
    created_at = "2026-07-21T00:00:00+00:00"
    initial_payload = {
        "import_id": import_id,
        "provider": "tomato",
        "query": "atomic fixture",
        "candidate": {
            "index": 1,
            "book_id": "54321",
            "title": "Atomic Fixture",
            "author": "Fixture Author",
        },
        "status": "drafting",
        "created_at": created_at,
        "updated_at": created_at,
    }
    store.create_source_import_job(initial_payload)
    publish_payload = {
        **initial_payload,
        "status": "awaiting_review",
        # Deliberately omit updated_at so failure happens after receipt INSERT.
    }
    publish_payload.pop("updated_at")
    request = SourceImportRequestEvidence(
        query="atomic fixture",
        candidate_index=1,
        book_id="54321",
        title="Atomic Fixture",
        author="Fixture Author",
    )
    request_hash = source_import_request_hash(request)
    run_id = source_import_run_id(import_id)
    receipt = {
        "receipt_id": "source-import-receipt-" + "c" * 32,
        "source_import_id": import_id,
        "run_id": run_id,
        "revision": 1,
        "idempotency_key": source_import_idempotency_key(run_id, 1, request_hash),
        "lineage_status": "verified",
        "provider": "tomato",
        "request": request.model_dump(mode="json"),
        "request_hash": request_hash,
        "prepare_invocation_id": "source-import-invocation-" + "1" * 32,
        "prepare_tool": {"tool_fingerprint": "2" * 64},
        "source_packet_fingerprint": "3" * 64,
        "distillation_invocation_id": "source-import-invocation-" + "2" * 32,
        "distillation_tool": {"tool_fingerprint": "4" * 64},
        "distillation_input_hash": "5" * 64,
        "distillation_output_hash": "6" * 64,
        "draft_fingerprint": "7" * 64,
        "cards_draft_hash": "8" * 64,
        "envelope_hash": "9" * 64,
        "receipt_hash": "a" * 64,
        "created_at": created_at,
    }

    with pytest.raises(KeyError, match="updated_at"):
        store.save_source_import_job_with_execution_receipt(
            publish_payload,
            receipt,
        )

    persisted = store.get_source_import_job(import_id)
    assert persisted is not None and persisted["status"] == "drafting"
    assert store.get_source_import_execution_receipt(import_id) is None


def test_newer_database_is_rejected_before_schema_ddl(tmp_path: Path) -> None:
    database_path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 99")
    connection.close()

    with pytest.raises(RuntimeError, match="newer than runtime schema"):
        SqliteStore(database_path).initialize()

    check = sqlite3.connect(database_path)
    tables = check.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    check.close()
    assert tables == []


def test_v18_migration_marks_legacy_skill_rows_unmanaged_without_forging_activation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-skill.sqlite3"
    legacy = sqlite3.connect(database_path)
    legacy.executescript(
        """
        CREATE TABLE skill_version (
            id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            version TEXT NOT NULL,
            source_path TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('active', 'version_changed', 'disabled')
            ),
            registered_at TEXT NOT NULL,
            UNIQUE (skill_id, fingerprint)
        ) STRICT;
        INSERT INTO skill_version(
            id, skill_id, version, source_path, fingerprint, status, registered_at
        ) VALUES (
            'skill-version-11111111111111111111111111111111',
            'fixture.legacy-skill',
            '1',
            'C:\\legacy\\skill.manifest.json',
            'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
            'active',
            '2026-07-21T00:00:00+00:00'
        );
        PRAGMA user_version = 17;
        """
    )
    legacy.close()

    store = SqliteStore(database_path)
    health = store.initialize()

    assert health.schema_version == SCHEMA_VERSION
    with store.connection() as connection:
        row = connection.execute(
            "SELECT * FROM skill_version WHERE skill_id = 'fixture.legacy-skill'"
        ).fetchone()
        activation_count = connection.execute(
            "SELECT COUNT(*) FROM skill_activation"
        ).fetchone()[0]
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM skill_import_receipt"
        ).fetchone()[0]
    assert row is not None
    assert row["snapshot_status"] == "legacy_unmanaged"
    assert row["manifest_fingerprint"] is None
    assert row["adapter_fingerprint"] is None
    assert activation_count == 0
    assert receipt_count == 0


def test_v19_source_import_migration_preserves_tomato_and_adds_source_key(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "source-import-v18.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    payload = _source_import_payload(
        SourceImportStatus.QUEUED,
        ordinal=1901,
        now=datetime(2026, 7, 22, tzinfo=UTC),
    )
    store.create_source_import_job(payload)

    with store.connection() as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP INDEX IF EXISTS idx_source_import_status")
        connection.execute(
            """
            CREATE TABLE source_import_job_v18 (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL CHECK (provider IN ('tomato')),
                query TEXT NOT NULL,
                book_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            ) STRICT
            """
        )
        connection.execute(
            """
            INSERT INTO source_import_job_v18(
                id, provider, query, book_id, status, payload_json,
                created_at, updated_at
            )
            SELECT id, provider, query, book_id, status, payload_json,
                   created_at, updated_at
            FROM source_import_job
            """
        )
        connection.execute("DROP TABLE source_import_job")
        connection.execute("ALTER TABLE source_import_job_v18 RENAME TO source_import_job")
        connection.execute(
            "CREATE INDEX idx_source_import_status "
            "ON source_import_job(status, updated_at DESC)"
        )
        connection.execute("PRAGMA user_version = 18")

    migrated = SqliteStore(database_path)
    assert migrated.initialize().schema_version == SCHEMA_VERSION
    assert migrated.get_source_import_job(str(payload["import_id"])) == payload
    with migrated.connection() as connection:
        row = connection.execute(
            "SELECT provider, book_id, source_key FROM source_import_job WHERE id = ?",
            (payload["import_id"],),
        ).fetchone()
        table_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'source_import_job'"
            ).fetchone()[0]
        )
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert row is not None
    assert row["provider"] == "tomato"
    assert row["source_key"] == row["book_id"] == payload["candidate"]["book_id"]
    assert "local_file" in table_sql
    assert violations == []


def test_source_import_store_keeps_local_file_distinct_from_tomato(
    tmp_path: Path,
) -> None:
    store = SqliteStore(tmp_path / "local-source.sqlite3")
    store.initialize()
    now = datetime(2026, 7, 22, tzinfo=UTC).isoformat()
    payload: dict[str, object] = {
        "schema": "zhimeng.source-import.v2",
        "import_id": "source-import-" + "f" * 32,
        "provider": "local_file",
        "query": "C:/fixtures/archive-night-sample.txt",
        "candidate": {
            "source_key": "3" * 64,
            "source_sha256": "3" * 64,
            "source_path": "C:/fixtures/archive-night-sample.txt",
            "source_format": "txt",
            "title": "archive-night-sample",
            "author": "unknown",
        },
        "status": "queued",
        "retry_of": None,
        "progress": {"stage": "queued", "message": "Waiting for local file"},
        "source_packet": None,
        "distillation_draft": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }

    store.create_source_import_job(payload)

    assert store.get_source_import_job(str(payload["import_id"])) == payload
    with store.connection() as connection:
        row = connection.execute(
            "SELECT provider, book_id, source_key FROM source_import_job WHERE id = ?",
            (payload["import_id"],),
        ).fetchone()
    assert row is not None
    assert row["provider"] == "local_file"
    assert row["book_id"] is None
    assert row["source_key"] == "3" * 64


def test_stale_binding_observation_cannot_overwrite_newer_snapshot(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project_id = "project-0123456789abcdef01234567"
    root_path = str(tmp_path / "book")
    first = store.sync_project_binding(
        project_id=project_id,
        root_path=root_path,
        display_name="Book",
        fingerprint="a" * 64,
    )

    stale = store.sync_project_binding(
        project_id=project_id,
        root_path=root_path,
        display_name="Book",
        fingerprint="b" * 64,
        observed_at=datetime.now(UTC) - timedelta(days=1),
    )
    fresh = store.sync_project_binding(
        project_id=project_id,
        root_path=root_path,
        display_name="Book",
        fingerprint="b" * 64,
        observed_at=datetime.now(UTC) + timedelta(seconds=1),
    )

    assert stale == first
    assert fresh.fingerprint == "b" * 64
    assert fresh.revision == first.revision + 1
