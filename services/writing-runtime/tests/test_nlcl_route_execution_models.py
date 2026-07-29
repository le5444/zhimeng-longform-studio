from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.nlcl_models import NlclRouteSource, build_nlcl_route_receipt
from zhimeng_runtime.nlcl_route_execution_models import (
    NlclRouteExecutionReceipt,
    build_nlcl_route_execution_receipt,
)
from test_nlcl_models import _pack


def _receipt() -> NlclRouteExecutionReceipt:
    route = build_nlcl_route_receipt(_pack(), source=NlclRouteSource.UNAVAILABLE)
    output = {
        "schema": "zhimeng.tool-result.skill-execute.v1",
        "skill_id": route.chapter_call_list[0].skill_id,
        "receipt_id": "skill-receipt-0001",
        "receipt_hash": "d" * 64,
        "domain_status": "blocked",
    }
    return build_nlcl_route_execution_receipt(
        route,
        route.chapter_call_list[0],
        run_id="run-route-execution-0001",
        tool_call_id="tool-route-execution-0001",
        skill_call_id="skill-call-route-execution-0001",
        skill_receipt_id="skill-receipt-0001",
        skill_receipt_hash="d" * 64,
        execution_status="succeeded",
        domain_status="blocked",
        output_hash=canonical_json_hash(output),
        tool_result_hash="e" * 64,
        created_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
    )


def test_execution_receipt_binds_route_call_skill_and_terminal_result() -> None:
    receipt = _receipt()

    assert receipt.call_ordinal == 1
    assert receipt.execution_status == "succeeded"
    assert receipt.domain_status == "blocked"
    assert receipt.skill_receipt_hash == "d" * 64
    assert len(receipt.receipt_hash) == 64


def test_execution_receipt_rejects_hash_tampering_and_false_success() -> None:
    receipt = _receipt()
    payload = receipt.model_dump(mode="json", by_alias=True)
    payload["receipt_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="receipt hash"):
        NlclRouteExecutionReceipt.model_validate(payload)

    payload = receipt.model_dump(mode="json", by_alias=True)
    payload["skill_receipt_id"] = None
    payload["skill_receipt_hash"] = None
    with pytest.raises(ValidationError, match="requires Skill"):
        NlclRouteExecutionReceipt.model_validate(payload)
