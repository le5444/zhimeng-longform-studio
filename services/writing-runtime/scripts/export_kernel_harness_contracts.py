from __future__ import annotations

import json
import sys
from pathlib import Path

WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(WRITING_RUNTIME / "tests"))

from kernel_harness.models import (  # noqa: E402
    AssertionResult,
    CandidateChangeSet,
    HarnessCase,
    HarnessObservation,
    HarnessRun,
)
from kernel_harness.adapters.base import (  # noqa: E402
    CleanupReport,
    EVIDENCE_ROOT_LIMIT_BYTES,
    TransportProbeResult,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
MODELS = {
    "kernel-harness-case.schema.json": HarnessCase,
    "kernel-harness-run.schema.json": HarnessRun,
    "kernel-harness-observation.schema.json": HarnessObservation,
    "kernel-harness-assertion-result.schema.json": AssertionResult,
    "candidate-change-set.schema.json": CandidateChangeSet,
    "kernel-transport-probe.schema.json": TransportProbeResult,
    "kernel-harness-cleanup.schema.json": CleanupReport,
}


def _extend_all_of(
    schema: dict[str, object],
    rules: list[dict[str, object]],
) -> None:
    all_of = schema.setdefault("allOf", [])
    if not isinstance(all_of, list):
        raise TypeError("schema allOf must be an array")
    all_of.extend(rules)


def _apply_assertion_result_invariants(schema: dict[str, object]) -> None:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    evidence_refs = properties["evidence_refs"]
    assert isinstance(evidence_refs, dict)
    evidence_refs["uniqueItems"] = True
    _extend_all_of(
        schema,
        [
            {
                "if": {
                    "properties": {"status": {"const": "pass"}},
                    "required": ["status"],
                },
                "then": {
                    "properties": {
                        "evidence_refs": {"minItems": 1},
                        "failure_fingerprint": {"type": "null"},
                    }
                },
            },
            {
                "if": {
                    "properties": {"status": {"const": "fail"}},
                    "required": ["status"],
                },
                "then": {
                    "properties": {"failure_fingerprint": {"type": "string"}}
                },
            },
            {
                "if": {
                    "properties": {
                        "status": {"enum": ["blocked", "not_measured"]}
                    },
                    "required": ["status"],
                },
                "then": {
                    "properties": {"failure_fingerprint": {"type": "null"}}
                },
            },
        ],
    )


def _apply_harness_run_invariants(schema: dict[str, object]) -> None:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    assertion_result = definitions["AssertionResult"]
    assert isinstance(assertion_result, dict)
    _apply_assertion_result_invariants(assertion_result)

    properties = schema["properties"]
    assert isinstance(properties, dict)
    normalized_run_ids = properties["normalized_run_ids"]
    assert isinstance(normalized_run_ids, dict)
    normalized_run_ids["uniqueItems"] = True
    for field_name in ("case_step_ids", "case_assertion_ids"):
        values = properties[field_name]
        assert isinstance(values, dict)
        values["uniqueItems"] = True
    _extend_all_of(
        schema,
        [
            {
                "if": {
                    "properties": {
                        "state": {"enum": ["passed", "failed", "blocked"]}
                    },
                    "required": ["state"],
                },
                "then": {
                    "properties": {
                        "completed_at": {"type": "string"},
                        "cleanup_status": {"not": {"const": "not_run"}},
                    }
                },
                "else": {"properties": {"completed_at": {"type": "null"}}},
            },
            {
                "if": {
                    "properties": {"state": {"const": "passed"}},
                    "required": ["state"],
                },
                "then": {
                    "properties": {
                        "cleanup_status": {"const": "clean"},
                        "final_snapshot_ref": {"type": "string"},
                        "assertion_results": {
                            "items": {
                                "properties": {"status": {"const": "pass"}},
                                "required": ["status"],
                            }
                        },
                    }
                },
            },
        ],
    )
    schema["x-zhimeng-invariants"] = [
        "case_step_ids and case_assertion_ids must match the case identified by case_fingerprint",
        "assertion_results must cover case_assertion_ids exactly by assertion_id",
        "terminal runs must account for every case step",
        "assertion result metadata and expected values must match the case contract",
    ]


def _apply_cleanup_report_invariants(schema: dict[str, object]) -> None:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    dimensions = properties["unmeasured_cleanup_dimensions"]
    assert isinstance(dimensions, dict)
    dimensions["uniqueItems"] = True
    secret_scan = properties["evidence_secret_scan_passed"]
    assert isinstance(secret_scan, dict)
    secret_scan["const"] = True
    evidence_limit = properties["evidence_root_byte_limit"]
    evidence_bytes = properties["evidence_root_bytes_after_adapter_close"]
    assert isinstance(evidence_limit, dict) and isinstance(evidence_bytes, dict)
    evidence_limit["const"] = EVIDENCE_ROOT_LIMIT_BYTES
    evidence_bytes["maximum"] = EVIDENCE_ROOT_LIMIT_BYTES
    _extend_all_of(
        schema,
        [
            {
                "if": {
                    "properties": {"root_process_close_status": {"const": "pass"}},
                    "required": ["root_process_close_status"],
                },
                "then": {"properties": {"process_cleanup_error": {"type": "null"}}},
                "else": {
                    "properties": {
                        "process_cleanup_error": {
                            "type": "string",
                            "minLength": 1,
                        }
                    }
                },
            },
            {
                "if": {
                    "properties": {"overall_status": {"const": "clean"}},
                    "required": ["overall_status"],
                },
                "then": {
                    "properties": {
                        "source_fixture_unchanged": {"const": True},
                        "disposable_project_snapshot_unchanged": {"const": True},
                        "root_process_close_status": {"const": "pass"},
                        "process_tree_cleanup_status": {"const": "pass"},
                        "unmeasured_cleanup_dimensions": {"maxItems": 0},
                        "resource_limits_passed": {"const": True},
                    }
                },
            },
        ],
    )


def _apply_transport_probe_invariants(schema: dict[str, object]) -> None:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    snapshot = definitions.get("TransportCapabilitySnapshot")
    if isinstance(snapshot, dict):
        properties = snapshot.get("properties")
        assert isinstance(properties, dict)
        for field_name in ("declared", "measured"):
            field = properties[field_name]
            assert isinstance(field, dict)
            field["uniqueItems"] = True
        snapshot["x-zhimeng-invariants"] = [
            "measured capabilities must be a subset of declared capabilities",
            "declared and measured capability names must be unique",
        ]

    properties = schema["properties"]
    assert isinstance(properties, dict)
    checks = properties["checks"]
    required_ids = properties["required_check_ids"]
    assert isinstance(checks, dict) and isinstance(required_ids, dict)
    required_ids["uniqueItems"] = True
    required_ids["minItems"] = 1
    check_items = checks.get("items")
    if isinstance(check_items, dict):
        check_properties = check_items.get("properties")
        if isinstance(check_properties, dict) and "check_id" in check_properties:
            # Unique check ids cannot be expressed purely as uniqueItems on objects
            # without a comparable object equality contract; export the invariant.
            pass
    schema["x-zhimeng-invariants"] = [
        "check_id values must be unique",
        "required_check_ids must be unique and each must appear in checks",
        "passed requires every required check and every emitted check to be true",
        "measured capabilities must be a subset of declared capabilities",
    ]


def _apply_candidate_change_set_invariants(schema: dict[str, object]) -> None:
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    file_change = definitions["CandidateFileChange"]
    assert isinstance(file_change, dict)
    properties = file_change["properties"]
    assert isinstance(properties, dict)
    path = properties["path"]
    assert isinstance(path, dict)
    path["pattern"] = (
        r"^(?![A-Za-z]:)(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))"
        r"(?!.*(?:^|/)\.(?:/|$))(?!.*(?:^|/)\.git(?:/|$)).+$"
    )
    _extend_all_of(
        file_change,
        [
            {
                "if": {
                    "properties": {"operation": {"const": "create"}},
                    "required": ["operation"],
                },
                "then": {
                    "properties": {
                        "file_id": {"type": "null"},
                        "before_sha256": {"type": "null"},
                        "after_sha256": {"type": "string"},
                    }
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "update"}},
                    "required": ["operation"],
                },
                "then": {
                    "properties": {
                        "file_id": {"type": "string"},
                        "before_sha256": {"type": "string"},
                        "after_sha256": {"type": "string"},
                    }
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "delete"}},
                    "required": ["operation"],
                },
                "then": {
                    "properties": {
                        "file_id": {"type": "string"},
                        "before_sha256": {"type": "string"},
                        "after_sha256": {"type": "null"},
                    }
                },
            },
        ],
    )
    file_change["x-zhimeng-invariants"] = [
        "paths must satisfy the Runtime normalized POSIX and Windows-safe path validator",
        "update before_sha256 and after_sha256 values must differ",
        "hunk_id values must be unique per file",
    ]
    schema["x-zhimeng-invariants"] = [
        "candidate file paths must be unique under Windows path identity rules",
    ]


def build_schema(file_name: str, model: type) -> dict[str, object]:
    schema = model.model_json_schema(by_alias=True)
    # Pydantic model validators are not emitted into portable JSON Schema.
    if file_name == "kernel-harness-assertion-result.schema.json":
        _apply_assertion_result_invariants(schema)
    elif file_name == "kernel-harness-run.schema.json":
        _apply_harness_run_invariants(schema)
    elif file_name == "kernel-harness-cleanup.schema.json":
        _apply_cleanup_report_invariants(schema)
    elif file_name == "kernel-transport-probe.schema.json":
        _apply_transport_probe_invariants(schema)
    elif file_name == "candidate-change-set.schema.json":
        _apply_candidate_change_set_invariants(schema)
    return schema


def render_schema(file_name: str, model: type) -> str:
    schema = build_schema(file_name, model)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"https://zhimeng.local/contracts/{file_name}"
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    CONTRACTS.mkdir(parents=True, exist_ok=True)
    for file_name, model in MODELS.items():
        target = CONTRACTS / file_name
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(render_schema(file_name, model), encoding="utf-8")
        temporary.replace(target)


if __name__ == "__main__":
    main()
