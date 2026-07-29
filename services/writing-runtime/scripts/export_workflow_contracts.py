from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))

from zhimeng_runtime.workflow_models import (  # noqa: E402
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "workflow-plan.schema.json": WorkflowPlan,
    "workflow-step-checkpoint.schema.json": WorkflowStepCheckpoint,
    "provider-io-boundary.schema.json": ProviderIoBoundary,
}


def _require_all_properties(schema: dict) -> None:
    schema["required"] = list(schema["properties"])


def _nonnull(*field_names: str) -> dict:
    return {
        "properties": {
            field_name: {"not": {"type": "null"}}
            for field_name in field_names
        },
        "required": list(field_names),
    }


def _null(*field_names: str) -> dict:
    return {
        "properties": {
            field_name: {"type": "null"}
            for field_name in field_names
        },
        "required": list(field_names),
    }


def _condition_on_null(
    field_name: str,
    *,
    when_null: dict,
    when_nonnull: dict,
) -> dict:
    return {
        "if": {
            "properties": {field_name: {"type": "null"}},
            "required": [field_name],
        },
        "then": when_null,
        "else": when_nonnull,
    }


def _augment_plan_schema(schema: dict) -> None:
    _require_all_properties(schema)
    step = schema["$defs"]["WorkflowStep"]
    _require_all_properties(step)
    for contract_field in ("input_contract", "output_contract"):
        step["properties"][contract_field]["not"] = {
            "pattern": r"/\.\.(?:/|$)"
        }
    step["properties"]["continue_on_domain_status"].update(
        {
            "enum": [
                ["healthy"],
                ["degraded"],
                ["blocked"],
                ["healthy", "degraded"],
                ["healthy", "blocked"],
                ["degraded", "blocked"],
                ["healthy", "degraded", "blocked"],
            ],
            "uniqueItems": True,
        }
    )
    schema["properties"]["steps"]["oneOf"] = [
        {
            "prefixItems": [
                {
                    "properties": {"ordinal": {"const": ordinal}},
                    "required": ["ordinal"],
                }
                for ordinal in range(1, count + 1)
            ],
            "minItems": count,
            "maxItems": count,
        }
        for count in range(1, 33)
    ]
    schema["$comment"] = (
        "Canonical hashes, derived identities, unique step IDs and cross-object "
        "bindings MUST also pass WorkflowPlan.model_validate_json."
    )


def _augment_checkpoint_schema(schema: dict) -> None:
    _require_all_properties(schema)
    calls = ("tool_call_id", "provider_tool_call_id", "skill_call_id")
    skill_receipt = ("skill_receipt_id", "skill_receipt_hash")
    route_receipt = (
        "route_execution_receipt_id",
        "route_execution_receipt_hash",
    )
    complete_success = (
        *calls,
        *skill_receipt,
        *route_receipt,
        "input_hash",
        "output_hash",
        "tool_result_hash",
        "domain_status",
    )
    schema["allOf"] = [
        {
            "if": {
                "properties": {"step_ordinal": {"const": 1}},
                "required": ["step_ordinal"],
            },
            "then": _null(
                "previous_checkpoint_id",
                "previous_checkpoint_hash",
            ),
            "else": _nonnull(
                "previous_checkpoint_id",
                "previous_checkpoint_hash",
            ),
        },
        _condition_on_null(
            "tool_call_id",
            when_null=_null(
                "provider_tool_call_id",
                "skill_call_id",
                *skill_receipt,
                *route_receipt,
                "output_hash",
                "tool_result_hash",
            ),
            when_nonnull=_nonnull(
                "provider_tool_call_id",
                "skill_call_id",
                "input_hash",
            ),
        ),
        _condition_on_null(
            "skill_receipt_id",
            when_null=_null(
                "skill_receipt_hash",
                *route_receipt,
                "output_hash",
            ),
            when_nonnull=_nonnull(
                "skill_receipt_hash",
                *calls,
                "input_hash",
                "output_hash",
            ),
        ),
        _condition_on_null(
            "route_execution_receipt_id",
            when_null=_null("route_execution_receipt_hash"),
            when_nonnull=_nonnull(
                "route_execution_receipt_hash",
                *skill_receipt,
                "tool_result_hash",
            ),
        ),
        {
            "if": {
                "properties": {"tool_result_hash": {"not": {"type": "null"}}},
                "required": ["tool_result_hash"],
            },
            "then": _nonnull("tool_call_id"),
        },
        {
            "if": {
                "properties": {"execution_status": {"const": "succeeded"}},
                "required": ["execution_status"],
            },
            "then": {
                "allOf": [
                    _nonnull(*complete_success),
                    _null("error_code", "error_message"),
                ]
            },
            "else": {
                "allOf": [
                    _null("domain_status"),
                    _nonnull("error_code", "error_message"),
                ]
            },
        },
    ]
    schema["$comment"] = (
        "Canonical hashes, predecessor hash continuity, step order and plan binding "
        "MUST also pass WorkflowStepCheckpoint.model_validate_json and "
        "validate_workflow_checkpoint_chain."
    )


def _augment_boundary_schema(schema: dict) -> None:
    _require_all_properties(schema)
    schema["$comment"] = (
        "Canonical hashes and the completed predecessor chain MUST also pass "
        "ProviderIoBoundary.model_validate_json and validate_workflow_checkpoint_chain."
    )


def build_schema(file_name: str, model: type) -> dict:
    schema = model.model_json_schema(by_alias=True)
    if file_name == "workflow-plan.schema.json":
        _augment_plan_schema(schema)
    elif file_name == "workflow-step-checkpoint.schema.json":
        _augment_checkpoint_schema(schema)
    elif file_name == "provider-io-boundary.schema.json":
        _augment_boundary_schema(schema)
    else:  # pragma: no cover - MODELS is closed above.
        raise ValueError(f"unknown workflow contract: {file_name}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return schema


def render_schema(file_name: str, model: type) -> str:
    schema = build_schema(file_name, model)
    return json.dumps(
        schema,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)


if __name__ == "__main__":
    main()
