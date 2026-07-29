from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from zhimeng_runtime.execution_protocol import (
    ActivityItem,
    ActivityKind,
    CapabilityName,
    ExecutionSession,
    Run,
    RunStatus,
    validate_activity_sequence,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _resolve_pointer(document: Any, pointer: str) -> Any:
    value = document
    if pointer:
        assert pointer.startswith("/")
        for part in pointer[1:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def _resolve_ref(
    reference: str,
    *,
    document: dict[str, Any],
    document_name: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    file_name, _, fragment = reference.partition("#")
    if file_name:
        target_document = _schema(file_name)
        target_name = file_name
    else:
        target_document = document
        target_name = document_name
    target = _resolve_pointer(target_document, fragment)
    return target, target_document, target_name


def _is_json_type(value: Any, type_name: str) -> bool:
    if type_name == "null":
        return value is None
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "object":
        return isinstance(value, dict)
    raise AssertionError(f"unsupported JSON type in test validator: {type_name}")


def _matches_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    document: dict[str, Any],
    document_name: str,
) -> bool:
    try:
        _assert_schema_valid(
            value,
            schema,
            document=document,
            document_name=document_name,
        )
    except AssertionError:
        return False
    return True


def _assert_schema_valid(
    value: Any,
    schema: dict[str, Any],
    *,
    document: dict[str, Any],
    document_name: str,
) -> None:
    """Validate the contract keyword subset without adding a runtime dependency."""

    if "$ref" in schema:
        target, target_document, target_name = _resolve_ref(
            schema["$ref"],
            document=document,
            document_name=document_name,
        )
        _assert_schema_valid(
            value,
            target,
            document=target_document,
            document_name=target_name,
        )

    for member in schema.get("allOf", []):
        _assert_schema_valid(
            value,
            member,
            document=document,
            document_name=document_name,
        )

    if "anyOf" in schema:
        assert any(
            _matches_schema(
                value,
                member,
                document=document,
                document_name=document_name,
            )
            for member in schema["anyOf"]
        )

    if "oneOf" in schema:
        matches = sum(
            _matches_schema(
                value,
                member,
                document=document,
                document_name=document_name,
            )
            for member in schema["oneOf"]
        )
        assert matches == 1

    if "if" in schema:
        branch = "then" if _matches_schema(
            value,
            schema["if"],
            document=document,
            document_name=document_name,
        ) else "else"
        if branch in schema:
            _assert_schema_valid(
                value,
                schema[branch],
                document=document,
                document_name=document_name,
            )

    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]

    declared_type = schema.get("type")
    if declared_type is not None:
        types = [declared_type] if isinstance(declared_type, str) else declared_type
        assert any(_is_json_type(value, type_name) for type_name in types)

    if isinstance(value, dict):
        required = set(schema.get("required", []))
        assert required.issubset(value)
        properties = schema.get("properties", {})
        for key, member in value.items():
            if "propertyNames" in schema:
                _assert_schema_valid(
                    key,
                    schema["propertyNames"],
                    document=document,
                    document_name=document_name,
                )
            if key in properties:
                _assert_schema_valid(
                    member,
                    properties[key],
                    document=document,
                    document_name=document_name,
                )
            elif schema.get("additionalProperties") is False:
                raise AssertionError(f"unexpected property {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _assert_schema_valid(
                    member,
                    schema["additionalProperties"],
                    document=document,
                    document_name=document_name,
                )

        for invariant in schema.get("x-zhimeng-invariants", []):
            if invariant == "update_hashes_differ" and value.get("operation") == "update":
                assert value.get("content_hash_before") != value.get("content_hash_after")
            elif invariant == "approval_option_ids_unique":
                option_ids = [option["option_id"] for option in value["options"]]
                assert len(option_ids) == len(set(option_ids))
            elif invariant == "selected_option_id_in_options_when_resolved":
                if value.get("status") == "resolved":
                    assert value.get("selected_option_id") in {
                        option["option_id"] for option in value["options"]
                    }
            elif invariant == "from_sequence_lte_to_sequence":
                assert value["from_sequence"] <= value["to_sequence"]
            elif invariant == "proposal_base_revision_matches_project_revision":
                assert value["payload"]["base_revision"] == value["project_revision"]["revision"]
            elif invariant == "proposal_base_fingerprint_matches_project_revision":
                assert (
                    value["payload"]["base_fingerprint"]
                    == value["project_revision"]["fingerprint"]
                )
            elif invariant == "compaction_precedes_current_activity":
                assert value["payload"]["to_sequence"] < value["sequence"]
            elif invariant == "message_usage_total_covers_parts":
                assert value["total_tokens"] >= (
                    value["input_tokens"] + value["output_tokens"]
                )

    if isinstance(value, list):
        if "minItems" in schema:
            assert len(value) >= schema["minItems"]
        if schema.get("uniqueItems"):
            rendered = [json.dumps(item, sort_keys=True) for item in value]
            assert len(rendered) == len(set(rendered))
        if "items" in schema:
            for member in value:
                _assert_schema_valid(
                    member,
                    schema["items"],
                    document=document,
                    document_name=document_name,
                )

    if isinstance(value, str):
        if "minLength" in schema:
            assert len(value) >= schema["minLength"]
        if "maxLength" in schema:
            assert len(value) <= schema["maxLength"]
        if "pattern" in schema:
            assert re.search(schema["pattern"], value)
        if schema.get("format") == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
    ):
        assert value >= schema["minimum"]


def _assert_entry_schema(value: dict[str, Any], schema_name: str) -> None:
    document = _schema(schema_name)
    _assert_schema_valid(
        value,
        document,
        document=document,
        document_name=schema_name,
    )


def _entry_example(schema_name: str) -> dict[str, Any]:
    return deepcopy(_schema(schema_name)["examples"][0])


def _error_envelope() -> dict[str, Any]:
    return {
        "schema": "zhimeng.error.v1",
        "error": {
            "code": "executor_failed",
            "message": "The candidate executor failed.",
            "retryable": True,
            "details": {"exit_code": 1},
            "request_id": "request_01J0000000000000000001",
        },
    }


def _activity_payloads() -> dict[str, dict[str, Any]]:
    return {
        "message": {
            "role": "assistant",
            "content": "I will inspect the project.",
            "name": None,
            "message_id": "message_01J00000000000000000001",
            "phase": "completed",
            "delta": None,
            "format": "markdown",
            "finish_reason": "stop",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
            },
        },
        "run_state": {
            "status": "running",
            "previous_status": "starting",
            "detail": "Provider stream opened",
        },
        "reasoning": {
            "summary": "The outline is the first required source.",
            "content": None,
            "visibility": "summary",
        },
        "tool_call": {
            "tool_call_id": "toolcall_01J0000000000000000001",
            "tool_name": "project.read",
            "arguments": {"path": "04_outline.md"},
        },
        "tool_result": {
            "tool_call_id": "toolcall_01J0000000000000000001",
            "status": "succeeded",
            "output": {"text": "outline"},
            "error": None,
            "duration_ms": 12,
        },
        "command": {
            "command_id": "command_01J00000000000000000001",
            "command": "rg --files",
            "cwd": "project://bound-root",
            "status": "succeeded",
            "exit_code": 0,
            "output_artifact_id": None,
        },
        "change_proposal": {
            "proposal_id": "proposal_01J000000000000000001",
            "title": "Revise the outline",
            "summary": "Strengthen the opening conflict without writing to the project.",
            "base_revision": 1,
            "base_fingerprint": "8c8b9d0a8c8b9d0a8c8b9d0a8c8b9d0a8c8b9d0a8c8b9d0a8c8b9d0a8c8b9d0a",
            "files": [
                {
                    "file_id": "file_01J0000000000000000000001",
                    "path": "04_outline.md",
                    "operation": "update",
                    "content_hash_before": "sha256:before",
                    "content_hash_after": "sha256:after",
                }
            ],
        },
        "approval_request": {
            "approval_id": "approval_01J00000000000000001",
            "subject_kind": "change_proposal",
            "subject_id": "proposal_01J000000000000000001",
            "prompt": "Apply this proposal?",
            "options": [
                {
                    "option_id": "option_approve_01J0000000000001",
                    "label": "Approve",
                    "effect": "approve",
                },
                {
                    "option_id": "option_reject_01J00000000000001",
                    "label": "Reject",
                    "effect": "reject",
                },
            ],
            "status": "pending",
            "selected_option_id": None,
        },
        "skill_call": {
            "skill_call_id": "skillcall_01J00000000000000001",
            "skill_id": "novel-distillation",
            "skill_version": "1",
            "status": "running",
            "input": {"source_ids": ["reference_a", "reference_b"]},
            "output": None,
        },
        "subagent": {
            "subagent_id": "subagent_01J000000000000000001",
            "name": "reference-reader",
            "task": "Compare the two reference novels.",
            "status": "running",
            "child_run_id": "run_child_01J00000000000000001",
        },
        "compaction": {
            "from_sequence": 1,
            "to_sequence": 20,
            "strategy": "semantic-summary",
            "summary": "The run inspected the outline and proposed two changes.",
            "tokens_before": 12000,
            "tokens_after": 2200,
            "artifact_id": "artifact_01J000000000000000001",
        },
        "artifact": {
            "artifact_id": "artifact_01J000000000000000001",
            "artifact_type": "analysis-report",
            "name": "comparison.json",
            "uri": "artifact://artifact_01J000000000000000001",
            "media_type": "application/json",
            "content_hash": "sha256:report",
            "size_bytes": 2048,
        },
        "error": {"error": _error_envelope(), "fatal": False, "scope": "activity"},
    }


def _activity(kind: str, sequence: int = 1) -> dict[str, Any]:
    value = _entry_example("activity-item.schema.json")
    value["activity_id"] = f"activity_01J000000000000000{sequence:06d}"
    value["sequence"] = sequence
    value["causation_id"] = (
        None
        if sequence == 1
        else f"activity_01J000000000000000{sequence - 1:06d}"
    )
    value["kind"] = kind
    value["payload"] = deepcopy(_activity_payloads()[kind])
    if kind == "compaction" and sequence > 1:
        value["payload"]["from_sequence"] = 1
        value["payload"]["to_sequence"] = sequence - 1
    return value


@pytest.mark.parametrize(
    ("schema_name", "model_type"),
    [
        ("execution-session.schema.json", ExecutionSession),
        ("execution-run.schema.json", Run),
        ("activity-item.schema.json", ActivityItem),
    ],
)
def test_schema_examples_round_trip_through_pydantic_and_json_schema(
    schema_name: str,
    model_type: type[ExecutionSession] | type[Run] | type[ActivityItem],
) -> None:
    example = _entry_example(schema_name)
    model = model_type.model_validate(example)
    rendered = model.model_dump(mode="json", by_alias=True)

    assert rendered == example
    _assert_entry_schema(rendered, schema_name)


def test_all_activity_kinds_have_discriminated_strict_payloads() -> None:
    expected_kinds = {kind.value for kind in ActivityKind}
    canonical = _schema("execution-protocol.schema.json")["$defs"]["activityItem"]

    assert set(canonical["properties"]["kind"]["enum"]) == expected_kinds
    assert {branch["properties"]["kind"]["const"] for branch in canonical["oneOf"]} == expected_kinds
    assert set(_activity_payloads()) == expected_kinds

    for sequence, kind in enumerate(sorted(expected_kinds), start=1):
        value = _activity(kind, sequence)
        rendered = ActivityItem.model_validate(value).model_dump(mode="json", by_alias=True)
        _assert_entry_schema(rendered, "activity-item.schema.json")


def test_activity_discriminator_rejects_mismatched_or_extra_payload_fields() -> None:
    mismatched = _activity("tool_call")
    mismatched["payload"] = _activity_payloads()["message"]
    with pytest.raises(ValidationError):
        ActivityItem.model_validate(mismatched)
    with pytest.raises(AssertionError):
        _assert_entry_schema(mismatched, "activity-item.schema.json")

    extra = _activity("message")
    extra["payload"]["vendor_private"] = True
    with pytest.raises(ValidationError):
        ActivityItem.model_validate(extra)
    with pytest.raises(AssertionError):
        _assert_entry_schema(extra, "activity-item.schema.json")


def test_activity_sequence_is_contiguous_duplicate_free_and_run_scoped() -> None:
    first = ActivityItem.model_validate(_activity("message", 1))
    second = ActivityItem.model_validate(_activity("reasoning", 2))
    validate_activity_sequence([first, second])

    duplicate_value = _activity("reasoning", 2)
    duplicate_value["activity_id"] = first.root.activity_id
    duplicate = ActivityItem.model_validate(duplicate_value)
    with pytest.raises(ValueError, match="duplicate activity id"):
        validate_activity_sequence([first, duplicate])

    gap = ActivityItem.model_validate(_activity("reasoning", 3))
    with pytest.raises(ValueError, match="expected activity sequence 2"):
        validate_activity_sequence([first, gap])

    other_run_value = _activity("reasoning", 2)
    other_run_value["run_id"] = "run_other_01J000000000000000001"
    other_run = ActivityItem.model_validate(other_run_value)
    with pytest.raises(ValueError, match="one session and run"):
        validate_activity_sequence([first, other_run])


def test_capability_snapshot_is_normalized_and_extensible_without_private_fields() -> None:
    example = _entry_example("execution-session.schema.json")
    session = ExecutionSession.model_validate(example)
    assert session.executor.implementation == "candidate-adapter"
    assert set(session.capabilities.extensions) == {"x-candidate.native-events"}

    duplicate = deepcopy(example)
    duplicate["capabilities"]["names"].append("streaming")
    with pytest.raises(ValidationError, match="capability names must be unique"):
        ExecutionSession.model_validate(duplicate)
    with pytest.raises(AssertionError):
        _assert_entry_schema(duplicate, "execution-session.schema.json")

    private_field = deepcopy(example)
    private_field["executor"]["codex_thread_id"] = "vendor-field"
    with pytest.raises(ValidationError):
        ExecutionSession.model_validate(private_field)
    with pytest.raises(AssertionError):
        _assert_entry_schema(private_field, "execution-session.schema.json")

    invalid_extension_name = deepcopy(example)
    extension = invalid_extension_name["capabilities"]["extensions"].pop(
        "x-candidate.native-events"
    )
    invalid_extension_name["capabilities"]["extensions"]["codex.native-events"] = extension
    with pytest.raises(ValidationError):
        ExecutionSession.model_validate(invalid_extension_name)
    with pytest.raises(AssertionError):
        _assert_entry_schema(invalid_extension_name, "execution-session.schema.json")

    extension_schema = _schema("execution-protocol.schema.json")["$defs"]
    assert extension_schema["capabilityProfile"]["properties"]["extensions"]["type"] == "object"

    capability_enum = _schema("execution-protocol.schema.json")["$defs"]
    schema_names = set(capability_enum["capabilityProfile"]["properties"]["names"]["items"]["enum"])
    assert schema_names == {capability.value for capability in CapabilityName}


def test_run_lifecycle_and_error_envelope_are_strict() -> None:
    running = _entry_example("execution-run.schema.json")
    run_schema = _schema("execution-protocol.schema.json")["$defs"]["run"]
    assert set(run_schema["properties"]["status"]["enum"]) == {
        status.value for status in RunStatus
    }

    failed = deepcopy(running)
    failed["status"] = "failed"
    failed["completed_at"] = "2026-07-18T04:02:00Z"
    failed["updated_at"] = "2026-07-18T04:02:00Z"
    failed["error"] = _error_envelope()
    rendered = Run.model_validate(failed).model_dump(mode="json", by_alias=True)
    _assert_entry_schema(rendered, "execution-run.schema.json")

    missing_error = deepcopy(failed)
    missing_error["error"] = None
    with pytest.raises(ValidationError, match="failed runs require"):
        Run.model_validate(missing_error)
    with pytest.raises(AssertionError):
        _assert_entry_schema(missing_error, "execution-run.schema.json")

    terminal_without_timestamp = deepcopy(failed)
    terminal_without_timestamp["completed_at"] = None
    with pytest.raises(ValidationError, match="terminal runs require"):
        Run.model_validate(terminal_without_timestamp)
    with pytest.raises(AssertionError):
        _assert_entry_schema(terminal_without_timestamp, "execution-run.schema.json")


@pytest.mark.parametrize(
    ("operation", "file_id", "before", "after"),
    [
        ("create", None, "sha256:unexpected", "sha256:after"),
        ("create", None, None, None),
        ("update", None, "sha256:before", "sha256:after"),
        ("update", "file_01J0000000000000000000001", None, "sha256:after"),
        (
            "update",
            "file_01J0000000000000000000001",
            "sha256:same",
            "sha256:same",
        ),
        ("delete", None, "sha256:before", None),
        ("delete", "file_01J0000000000000000000001", None, None),
        (
            "delete",
            "file_01J0000000000000000000001",
            "sha256:before",
            "sha256:unexpected",
        ),
    ],
)
def test_change_proposal_rejects_invalid_hash_transitions(
    operation: str,
    file_id: str | None,
    before: str | None,
    after: str | None,
) -> None:
    value = _activity("change_proposal", 2)
    change = value["payload"]["files"][0]
    change.update(
        operation=operation,
        file_id=file_id,
        content_hash_before=before,
        content_hash_after=after,
    )

    with pytest.raises(ValidationError):
        ActivityItem.model_validate(value)
    with pytest.raises(AssertionError):
        _assert_entry_schema(value, "activity-item.schema.json")


def test_change_proposal_base_revision_matches_activity_revision() -> None:
    value = _activity("change_proposal", 2)
    value["payload"]["base_revision"] = 2

    with pytest.raises(ValidationError, match="base_revision"):
        ActivityItem.model_validate(value)
    with pytest.raises(AssertionError):
        _assert_entry_schema(value, "activity-item.schema.json")

    stale_fingerprint = _activity("change_proposal", 2)
    stale_fingerprint["payload"]["base_fingerprint"] = "a" * 64
    with pytest.raises(ValidationError, match="base_fingerprint"):
        ActivityItem.model_validate(stale_fingerprint)
    with pytest.raises(AssertionError):
        _assert_entry_schema(stale_fingerprint, "activity-item.schema.json")


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("revision", 0),
        ("revision", "1"),
        ("fingerprint", "not-a-sha256"),
        ("fingerprint", "A" * 64),
    ],
)
def test_project_revision_uses_reader_integer_and_sha256_contract(
    field: str,
    invalid_value: Any,
) -> None:
    value = _entry_example("execution-session.schema.json")
    value["project_revision"][field] = invalid_value

    with pytest.raises(ValidationError):
        ExecutionSession.model_validate(value)
    with pytest.raises(AssertionError):
        _assert_entry_schema(value, "execution-session.schema.json")


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("base_revision", 0),
        ("base_revision", "1"),
        ("base_fingerprint", "not-a-sha256"),
        ("base_fingerprint", "A" * 64),
    ],
)
def test_change_proposal_base_identity_uses_reader_contract(
    field: str,
    invalid_value: Any,
) -> None:
    value = _activity("change_proposal", 2)
    value["payload"][field] = invalid_value

    with pytest.raises(ValidationError):
        ActivityItem.model_validate(value)
    with pytest.raises(AssertionError):
        _assert_entry_schema(value, "activity-item.schema.json")


@pytest.mark.parametrize(
    ("status", "selected_option_id", "duplicate_options"),
    [
        ("resolved", None, False),
        ("pending", "option_approve_01J0000000000001", False),
        ("resolved", "option_missing_01J0000000000001", False),
        ("pending", None, True),
    ],
)
def test_approval_selection_semantics_match_the_handwritten_schema(
    status: str,
    selected_option_id: str | None,
    duplicate_options: bool,
) -> None:
    value = _activity("approval_request", 2)
    value["payload"]["status"] = status
    value["payload"]["selected_option_id"] = selected_option_id
    if duplicate_options:
        value["payload"]["options"][1]["option_id"] = value["payload"]["options"][0][
            "option_id"
        ]

    with pytest.raises(ValidationError):
        ActivityItem.model_validate(value)
    with pytest.raises(AssertionError):
        _assert_entry_schema(value, "activity-item.schema.json")


def test_compaction_range_and_outer_sequence_are_enforced() -> None:
    reversed_range = _activity("compaction", 4)
    reversed_range["payload"]["from_sequence"] = 3
    reversed_range["payload"]["to_sequence"] = 2
    with pytest.raises(ValidationError, match="range must be ordered"):
        ActivityItem.model_validate(reversed_range)
    with pytest.raises(AssertionError):
        _assert_entry_schema(reversed_range, "activity-item.schema.json")

    future_range = _activity("compaction", 4)
    future_range["payload"]["to_sequence"] = 4
    with pytest.raises(ValidationError, match="future activity"):
        ActivityItem.model_validate(future_range)
    with pytest.raises(AssertionError):
        _assert_entry_schema(future_range, "activity-item.schema.json")


def test_activity_correlation_causation_compaction_and_run_cursor() -> None:
    first = ActivityItem.model_validate(_activity("message", 1))
    second = ActivityItem.model_validate(_activity("reasoning", 2))
    run = Run.model_validate(_entry_example("execution-run.schema.json"))
    validate_activity_sequence([first, second], run=run)

    correlation_value = _activity("reasoning", 2)
    correlation_value["correlation_id"] = "correlation_other_01J000000000001"
    correlation = ActivityItem.model_validate(correlation_value)
    with pytest.raises(ValueError, match="share one correlation"):
        validate_activity_sequence([first, correlation])

    self_caused_value = _activity("message", 1)
    self_caused_value["causation_id"] = self_caused_value["activity_id"]
    self_caused = ActivityItem.model_validate(self_caused_value)
    with pytest.raises(ValueError, match="earlier known activity"):
        validate_activity_sequence([self_caused])

    future_caused_value = _activity("message", 1)
    future_caused_value["causation_id"] = _activity("reasoning", 2)["activity_id"]
    future_caused = ActivityItem.model_validate(future_caused_value)
    with pytest.raises(ValueError, match="earlier known activity"):
        validate_activity_sequence([future_caused, second])

    missing_caused_value = _activity("message", 1)
    missing_caused_value["causation_id"] = "activity_missing_01J00000000001"
    missing_caused = ActivityItem.model_validate(missing_caused_value)
    with pytest.raises(ValueError, match="earlier known activity"):
        validate_activity_sequence([missing_caused])

    fifth_value = _activity("message", 5)
    fifth = ActivityItem.model_validate(fifth_value)
    validate_activity_sequence(
        [fifth],
        expected_start=5,
        prior_activity_ids={fifth_value["causation_id"]},
    )

    bad_cursor = run.model_copy(update={"last_activity_sequence": 3})
    with pytest.raises(ValueError, match="last_activity_sequence"):
        validate_activity_sequence([first, second], run=bad_cursor)

    compaction = ActivityItem.model_validate(_activity("compaction", 3))
    invalid_payload = compaction.root.payload.model_copy(update={"to_sequence": 4})
    invalid_variant = compaction.root.model_copy(update={"payload": invalid_payload})
    invalid_compaction = ActivityItem.model_construct(root=invalid_variant)
    with pytest.raises(ValueError, match="future activity"):
        validate_activity_sequence([first, second, invalid_compaction])


def test_structured_schema_objects_are_strict_and_reuse_existing_error_contract() -> None:
    protocol = _schema("execution-protocol.schema.json")
    intentionally_dynamic = {"jsonValue"}
    for name, definition in protocol["$defs"].items():
        if definition.get("type") == "object" and name not in intentionally_dynamic:
            assert definition.get("additionalProperties") is False, name

    protocol_text = (CONTRACTS / "execution-protocol.schema.json").read_text(encoding="utf-8")
    assert "error-envelope.schema.json" in protocol_text
    assert '"code"' not in protocol_text
    assert '"request_id"' not in protocol_text


def test_pydantic_activity_schema_exposes_the_same_discriminator() -> None:
    generated = ActivityItem.model_json_schema()
    expected_kinds = {kind.value for kind in ActivityKind}

    assert generated["discriminator"]["propertyName"] == "kind"
    assert set(generated["discriminator"]["mapping"]) == expected_kinds
    for name, definition in generated["$defs"].items():
        if name.endswith("ActivityItem") or name.endswith("Payload"):
            assert definition.get("additionalProperties") is False, name

    assert ExecutionSession.model_json_schema()["additionalProperties"] is False
    assert Run.model_json_schema()["additionalProperties"] is False


def test_execution_models_are_frozen_and_require_the_schema_alias() -> None:
    session_value = _entry_example("execution-session.schema.json")
    session = ExecutionSession.model_validate(session_value)
    activity = ActivityItem.model_validate(_activity("message", 1))

    with pytest.raises(ValidationError, match="frozen"):
        session.status = "closed"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        activity.root.sequence = 2
    with pytest.raises(ValidationError, match="frozen"):
        activity.root = activity.root

    for schema_name, model_type in (
        ("execution-session.schema.json", ExecutionSession),
        ("execution-run.schema.json", Run),
        ("activity-item.schema.json", ActivityItem),
    ):
        missing = _entry_example(schema_name)
        missing.pop("schema")
        with pytest.raises(ValidationError):
            model_type.model_validate(missing)

        field_name = _entry_example(schema_name)
        field_name["schema_id"] = field_name.pop("schema")
        with pytest.raises(ValidationError):
            model_type.model_validate(field_name)
