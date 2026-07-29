from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime

import pytest

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.distillation_invocation_store import (
    DistillationInvocationStore,
    DistillationInvocationStoreConflict,
    DistillationInvocationStoreIntegrity,
)
from zhimeng_runtime.distillation_receipt_models import (
    DistillationReceiptKind,
)
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


fixtures = importlib.import_module("test_distillation_invocation_models")

SOURCE_IMPORT_ID = f"source-import-{'a' * 32}"
PROJECT_ID = f"project-{'b' * 24}"
IDEMPOTENCY_KEY = f"distillation-idempotency-{'d' * 32}"
NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(fixtures, "SOURCE_IMPORT_ID", SOURCE_IMPORT_ID)
    monkeypatch.setattr(
        fixtures,
        "SOURCE_RECEIPT_ID",
        source_import_receipt_id(SOURCE_IMPORT_ID),
    )
    monkeypatch.setattr(fixtures, "PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(fixtures, "IDEMPOTENCY_KEY", IDEMPOTENCY_KEY)
    monkeypatch.setattr(fixtures, "NOW", NOW)
    return fixtures._normal_chain()


@pytest.fixture
def seeded_store(tmp_path, chain) -> DistillationInvocationStore:
    sqlite = SqliteStore(tmp_path / "runtime.sqlite3")
    sqlite.initialize()
    _seed_static_bindings(sqlite, chain)
    return DistillationInvocationStore(sqlite)


def test_create_and_append_success_chain_persists_head_ledger_and_candidates(
    seeded_store: DistillationInvocationStore,
    chain,
) -> None:
    snapshot = seeded_store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert snapshot.invocation.status.value == "prepared"
    assert snapshot.last_receipt_id == chain["prepared"].receipt_id
    assert [receipt.kind for receipt in snapshot.receipts] == [
        DistillationReceiptKind.PREPARED
    ]

    for key in ("dispatch1", "result1", "dispatch2", "result2", "candidate"):
        snapshot = seeded_store.append_receipt(chain[key])
    final = fixtures._success_final(chain, ordinal=7, parent=chain["candidate"])
    snapshot = seeded_store.append_receipt(final)

    assert snapshot.invocation.status.value == "succeeded"
    assert snapshot.invocation.provider_replay_count == 0
    assert snapshot.invocation.candidate_set_hash == chain["candidate_set"].candidate_set_hash
    assert [receipt.kind.value for receipt in snapshot.receipts] == [
        "prepared",
        "provider_dispatch",
        "provider_turn_result",
        "provider_dispatch",
        "provider_turn_result",
        "candidate_output",
        "final_result",
    ]

    with seeded_store.database.connection() as connection:
        rows = connection.execute(
            """
            SELECT source_card_index, card_type, status, runtime_distillation_invocation_id
            FROM knowledge_candidate
            WHERE runtime_distillation_invocation_id = ?
            ORDER BY source_card_index
            """,
            (chain["catalog"].invocation_id,),
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        (0, "work", "pending", chain["catalog"].invocation_id),
        (1, "voice", "pending", chain["catalog"].invocation_id),
        (2, "validator", "pending", chain["catalog"].invocation_id),
    ]


def test_duplicate_and_stale_receipts_fail_closed(
    seeded_store: DistillationInvocationStore,
    chain,
) -> None:
    seeded_store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=IDEMPOTENCY_KEY,
    )
    seeded_store.append_receipt(chain["dispatch1"])

    with pytest.raises(DistillationInvocationStoreConflict, match="already exists"):
        seeded_store.append_receipt(chain["dispatch1"])

    with pytest.raises(DistillationInvocationStoreConflict, match="ordinal|stale|parent"):
        seeded_store.append_receipt(chain["dispatch2"])


def test_success_final_requires_three_runtime_bound_pending_candidates(
    seeded_store: DistillationInvocationStore,
    chain,
) -> None:
    seeded_store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=IDEMPOTENCY_KEY,
    )
    for key in ("dispatch1", "result1", "dispatch2", "result2"):
        seeded_store.append_receipt(chain[key])
    seeded_store.append_receipt(chain["candidate"], persist_candidates=False)
    final = fixtures._success_final(chain, ordinal=7, parent=chain["candidate"])

    with pytest.raises(
        DistillationInvocationStoreConflict,
        match="exactly three pending candidates",
    ):
        seeded_store.append_receipt(final)


def test_read_detects_tampered_head_payload(
    seeded_store: DistillationInvocationStore,
    chain,
) -> None:
    seeded_store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=IDEMPOTENCY_KEY,
    )

    with seeded_store.database.connection() as connection:
        connection.execute("DROP TRIGGER IF EXISTS trg_distillation_invocation_state_transition")
        connection.execute("DROP TRIGGER IF EXISTS trg_distillation_invocation_receipt_binding")
        row = connection.execute(
            "SELECT payload_json FROM distillation_invocation WHERE id = ?",
            (chain["catalog"].invocation_id,),
        ).fetchone()
        payload = json.loads(str(row["payload_json"]))
        payload["source"]["source_receipt_hash"] = "0" * 64
        connection.execute(
            "UPDATE distillation_invocation SET payload_json = ? WHERE id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                chain["catalog"].invocation_id,
            ),
        )

    with pytest.raises(DistillationInvocationStoreIntegrity):
        seeded_store.get(chain["catalog"].invocation_id)


def _seed_static_bindings(sqlite: SqliteStore, chain) -> None:
    prepared_payload = chain["prepared"].payload
    source = prepared_payload.source
    project = prepared_payload.project
    knowledge = prepared_payload.knowledge
    instruction = prepared_payload.instruction
    created_at = NOW.isoformat()
    with sqlite.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO project_binding(
                id, root_path, display_name, fingerprint, revision,
                created_at, updated_at
            ) VALUES (?, 'C:/Novels/DeepDistillationStore',
                      'Deep Distillation Store', ?, ?, ?, ?)
            """,
            (
                project.project_id,
                project.project_fingerprint,
                project.project_revision,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO source_import_job(
                id, provider, query, book_id, source_key, status, payload_json,
                created_at, updated_at
            ) VALUES (?, 'tomato', 'store fixture', '12345', '12345',
                      'awaiting_review', '{}', ?, ?)
            """,
            (source.source_import_id, created_at, created_at),
        )
        connection.execute(
            """
            INSERT INTO source_import_execution_receipt(
                id, source_import_id, run_id, revision, idempotency_key,
                request_hash, prepare_invocation_id, prepare_tool_fingerprint,
                source_packet_fingerprint, distillation_invocation_id,
                distillation_tool_fingerprint, distillation_input_hash,
                distillation_output_hash, draft_fingerprint, cards_draft_hash,
                envelope_hash, receipt_hash, payload_json, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                source.source_receipt_id,
                source.source_import_id,
                f"source-import-run-{'1' * 32}",
                f"source-import-idempotency-{'2' * 32}",
                "3" * 64,
                f"source-import-invocation-{'4' * 32}",
                "5" * 64,
                source.source_packet_fingerprint,
                f"source-import-invocation-{'6' * 32}",
                "7" * 64,
                "8" * 64,
                "9" * 64,
                "a" * 64,
                "b" * 64,
                "c" * 64,
                source.source_receipt_hash,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO knowledge_scope(
                id, scope_type, project_binding_id, state_revision,
                created_at, updated_at
            ) VALUES (?, 'project', ?, ?, ?, ?)
            """,
            (
                knowledge.scope_id,
                project.project_id,
                knowledge.scope_state_revision,
                created_at,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO skill_version(
                id, skill_id, version, source_path, fingerprint,
                manifest_fingerprint, snapshot_status, status, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'managed', 'active', ?)
            """,
            (
                instruction.skill_version_id,
                instruction.skill_id,
                instruction.skill_version,
                "adapters/builtin/novel-distillation-deep-instruction/skill.manifest.json",
                instruction.package_fingerprint,
                instruction.manifest_fingerprint,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO skill_activation_receipt(
                id, scope_key, scope_type, project_binding_id, skill_id, action,
                previous_state, previous_version_id, next_state, next_version_id,
                expected_state_revision, expected_version_id, state_revision,
                receipt_hash, payload_json, created_at
            ) VALUES (?, ?, 'project', ?, ?, 'enable', 'disabled', NULL,
                      'enabled', ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                instruction.activation_receipt_id,
                project.project_id,
                project.project_id,
                instruction.skill_id,
                instruction.skill_version_id,
                instruction.activation_state_revision - 1,
                instruction.skill_version_id,
                instruction.activation_state_revision,
                instruction.activation_receipt_hash,
                created_at,
            ),
        )
        connection.commit()
