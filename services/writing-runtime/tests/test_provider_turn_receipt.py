from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from test_workflow_checkpoint_store import (
    PROVIDER_MESSAGES_HASH,
    TOOL_DEFINITIONS_HASH,
    WorkflowFixture,
    _prepare_creation_writer_dispatch,
    append_full_chain,
    build_fixture,
    executor_identity,
)
from zhimeng_runtime.agent_store import (
    AgentStore,
    ProviderTurnReceiptConflict,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.provider_turn_models import (
    ProviderTurnReceipt,
    ProviderTurnReceiptListResponse,
    build_provider_turn_receipt,
    build_provider_turn_receipt_list_response,
    provider_turn_receipt_hash,
    provider_turn_receipt_id,
)
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    build_provider_io_boundary,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "packages" / "contracts" / "provider-turn-receipt.schema.json"
LIST_CONTRACT = (
    ROOT / "packages" / "contracts" / "provider-turn-receipt-list.schema.json"
)


def _fixture_with_boundary(
    tmp_path: Path,
) -> tuple[WorkflowFixture, ProviderIoBoundary]:
    fixture = build_fixture(tmp_path)
    checkpoints = append_full_chain(fixture)
    _prepare_creation_writer_dispatch(fixture)
    boundary = build_provider_io_boundary(
        fixture.plan,
        checkpoints,
        request_hash="1" * 64,
        provider_model="requested-model",
        provider_dispatch_config_fingerprint="f" * 64,
        executor=executor_identity(),
        created_at=datetime.now(UTC),
    )
    return fixture, fixture.store.create_provider_io_boundary(
        boundary,
        expected_previous_checkpoint_hash=checkpoints[-1].checkpoint_hash,
        provider_messages_hash=PROVIDER_MESSAGES_HASH,
        tool_definitions_hash=TOOL_DEFINITIONS_HASH,
    )


def _receipt(
    boundary: ProviderIoBoundary,
    *,
    turn_ordinal: int = 1,
    request_hash: str | None = None,
    reported_model: str | None = "requested-model",
    aliases: tuple[str, ...] = ("gateway-model",),
) -> ProviderTurnReceipt:
    return build_provider_turn_receipt(
        boundary,
        turn_ordinal=turn_ordinal,
        request_hash=request_hash or boundary.request_hash,
        reported_model=reported_model,
        accepted_reported_models=aliases,
        finish_reason="tool_calls" if turn_ordinal == 1 else "stop",
        usage=None,
        created_at=datetime.now(UTC),
    )


def _rebuild_receipt(
    receipt: ProviderTurnReceipt,
    **updates: object,
) -> ProviderTurnReceipt:
    material = receipt.model_dump(mode="python", by_alias=False)
    material.update(updates)
    material.pop("receipt_hash", None)
    material["receipt_id"] = provider_turn_receipt_id(
        run_id=str(material["run_id"]),
        provider_boundary_id=str(material["provider_boundary_id"]),
        turn_ordinal=int(material["turn_ordinal"]),
        request_hash=str(material["request_hash"]),
    )
    draft = ProviderTurnReceipt.model_construct(
        **material,
        receipt_hash="0" * 64,
    )
    material["receipt_hash"] = provider_turn_receipt_hash(draft)
    return ProviderTurnReceipt.model_validate(material)


def test_provider_turn_receipt_contract_is_strict_and_exported() -> None:
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == (
        "https://zhimeng.local/contracts/provider-turn-receipt.schema.json"
    )
    assert stored == ProviderTurnReceipt.model_json_schema(by_alias=True)
    assert stored["additionalProperties"] is False
    assert stored["properties"]["schema"]["const"] == (
        "zhimeng.provider-turn-receipt.v1"
    )

    list_schema = json.loads(LIST_CONTRACT.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(list_schema)
    assert list_schema.pop("$schema") == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert list_schema.pop("$id") == (
        "https://zhimeng.local/contracts/provider-turn-receipt-list.schema.json"
    )
    assert list_schema == ProviderTurnReceiptListResponse.model_json_schema(
        by_alias=True
    )
    assert list_schema["additionalProperties"] is False


def test_provider_turn_receipt_derives_explicit_model_identity_statuses(
    tmp_path: Path,
) -> None:
    _, boundary = _fixture_with_boundary(tmp_path)

    exact = _receipt(boundary)
    alias = _receipt(
        boundary,
        turn_ordinal=2,
        request_hash="2" * 64,
        reported_model="gateway-model",
    )
    unreported = _receipt(boundary, reported_model=None)
    rejected = _receipt(boundary, reported_model="unexpected-model")

    assert exact.model_identity_status == "exact_match"
    assert alias.model_identity_status == "accepted_alias"
    assert unreported.model_identity_status == "unreported"
    assert rejected.model_identity_status == "rejected_mismatch"
    assert exact.receipt_hash == provider_turn_receipt_hash(exact)

    pending = build_provider_turn_receipt_list_response(boundary, ())
    verified = build_provider_turn_receipt_list_response(boundary, (exact, alias))
    unavailable = build_provider_turn_receipt_list_response(boundary, (unreported,))
    mismatch = build_provider_turn_receipt_list_response(boundary, (rejected,))
    assert pending.provider_identity_status == "pending"
    assert verified.provider_identity_status == "verified"
    assert unavailable.provider_identity_status == "unreported"
    assert mismatch.provider_identity_status == "rejected_mismatch"

    payload = exact.model_dump(mode="python", by_alias=True)
    payload["receipt_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="receipt hash"):
        ProviderTurnReceipt.model_validate(payload)


def test_store_appends_idempotently_rejects_conflicts_and_survives_restart(
    tmp_path: Path,
) -> None:
    fixture, boundary = _fixture_with_boundary(tmp_path)
    first = _receipt(boundary)

    assert fixture.store.create_provider_turn_receipt(first) == first
    assert fixture.store.create_provider_turn_receipt(first) == first

    conflicting_first = _receipt(boundary, reported_model="gateway-model")
    with pytest.raises(ProviderTurnReceiptConflict) as duplicate:
        fixture.store.create_provider_turn_receipt(conflicting_first)
    assert duplicate.value.code == "provider_turn_receipt_duplicate"

    skipped = _receipt(
        boundary,
        turn_ordinal=3,
        request_hash="3" * 64,
    )
    with pytest.raises(ProviderTurnReceiptConflict) as order:
        fixture.store.create_provider_turn_receipt(skipped)
    assert order.value.code == "provider_turn_receipt_order_invalid"

    second = _receipt(
        boundary,
        turn_ordinal=2,
        request_hash="2" * 64,
        reported_model="gateway-model",
    )
    assert fixture.store.create_provider_turn_receipt(second) == second

    changed_aliases = _receipt(
        boundary,
        turn_ordinal=3,
        request_hash="3" * 64,
        aliases=("other-gateway-model",),
    )
    with pytest.raises(ProviderTurnReceiptConflict) as aliases:
        fixture.store.create_provider_turn_receipt(changed_aliases)
    assert aliases.value.code == "provider_turn_receipt_alias_changed"

    reopened_database = SqliteStore(fixture.database.path)
    reopened_database.initialize()
    reopened = AgentStore(reopened_database)
    assert reopened.list_provider_turn_receipts(fixture.run.run_id) == (
        first,
        second,
    )
    evidence = reopened.get_provider_turn_receipt_list(fixture.run.run_id)
    assert evidence.provider_turn_receipts == (first, second)
    assert evidence.provider_identity_status == "verified"
    assert evidence.requested_model == boundary.provider_model


def test_store_rejects_a_valid_receipt_bound_to_another_boundary(
    tmp_path: Path,
) -> None:
    fixture, boundary = _fixture_with_boundary(tmp_path)
    first = _receipt(boundary)
    forged = _rebuild_receipt(
        first,
        provider_boundary_id=f"provider-io-{'c' * 32}",
        provider_boundary_hash="d" * 64,
    )

    with pytest.raises(ProviderTurnReceiptConflict) as conflict:
        fixture.store.create_provider_turn_receipt(forged)
    assert conflict.value.code == "provider_turn_receipt_boundary_mismatch"


def test_store_fails_closed_for_tampered_or_cross_run_receipt_rows(
    tmp_path: Path,
) -> None:
    fixture, boundary = _fixture_with_boundary(tmp_path)
    first = fixture.store.create_provider_turn_receipt(_receipt(boundary))

    with fixture.database.connection() as connection:
        row = connection.execute(
            """
            SELECT sequence, payload_json FROM run_event
            WHERE run_id = ? AND event_type = 'provider_turn_receipt'
            """,
            (fixture.run.run_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(str(row["payload_json"]))
        payload["finish_reason"] = "tampered"
        connection.execute(
            "UPDATE run_event SET payload_json = ? WHERE run_id = ? AND sequence = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                fixture.run.run_id,
                int(row["sequence"]),
            ),
        )

    with pytest.raises(ProviderTurnReceiptConflict) as tampered:
        fixture.store.list_provider_turn_receipts(fixture.run.run_id)
    assert tampered.value.code == "provider_turn_receipt_integrity_failed"

    # Restore the valid row, then copy self-validating evidence from another Run
    # into this Run's event stream. Row ownership must still fail closed.
    with fixture.database.connection() as connection:
        connection.execute(
            "UPDATE run_event SET payload_json = ? WHERE run_id = ? AND sequence = ?",
            (
                json.dumps(
                    first.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                fixture.run.run_id,
                int(row["sequence"]),
            ),
        )
        copied = _rebuild_receipt(
            first,
            run_id="run_other_provider_00000001",
            turn_ordinal=2,
            request_hash="2" * 64,
        )
        next_sequence = int(
            connection.execute(
                "SELECT MAX(sequence) + 1 FROM run_event WHERE run_id = ?",
                (fixture.run.run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO run_event(
                run_id, sequence, event_type, activity_id, payload_json, created_at
            ) VALUES (?, ?, 'provider_turn_receipt', NULL, ?, ?)
            """,
            (
                fixture.run.run_id,
                next_sequence,
                json.dumps(
                    copied.model_dump(mode="json", by_alias=True),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                copied.created_at.isoformat(),
            ),
        )

    with pytest.raises(ProviderTurnReceiptConflict) as cross_run:
        fixture.store.list_provider_turn_receipts(fixture.run.run_id)
    assert cross_run.value.code == "provider_turn_receipt_integrity_failed"
