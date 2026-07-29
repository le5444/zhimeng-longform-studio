from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from kernel_harness.adapters.base import CleanupReport, TransportProbeResult
from kernel_harness.assertions import (
    gate_statuses,
    terminal_state_for_results,
    terminal_state_for_run,
    validate_assertion_results,
    validate_harness_run_against_case,
)
from kernel_harness.fixture import (
    prepare_disposable_fixture,
    tree_fingerprint,
    verify_case_fixture,
)
from kernel_harness.evidence import build_evidence_index
from kernel_harness.models import (
    AssertionResult,
    AssertionStatus,
    CandidateChangeSet,
    CleanupStatus,
    EvidenceKind,
    HarnessCase,
    HarnessObservation,
    HarnessOperation,
    HarnessRun,
    HarnessState,
    ProviderModelProfile,
    canonical_case_fingerprint,
)
from kernel_harness.state_machine import HarnessStateMachine
from scripts.export_kernel_harness_contracts import build_schema
from scripts.run_kernel_transport_probe import (
    SAFE_OUTPUT_BASE,
    _await_case_step,
    _cli_exit_code,
    _cli_has_failure,
    _evidence_tree_size_bytes,
    _finalize_cleanup_secret_scan,
    _redact_sensitive_text,
    _resolve_safe_output_root,
    _transport_execution_contract,
    _validate_manifest,
    _write_manifest,
)
from zhimeng_runtime.execution_protocol import ExecutorIdentity, ProjectRevision


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "kernel-harness"
CASE_FILE = FIXTURE_ROOT / "case.json"
CONTRACTS = Path(__file__).resolve().parents[3] / "packages" / "contracts"


def _case() -> HarnessCase:
    return HarnessCase.model_validate_json(CASE_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("file_name", "model_type"),
    [
        ("kernel-harness-case.schema.json", HarnessCase),
        ("kernel-harness-run.schema.json", HarnessRun),
        ("kernel-harness-observation.schema.json", HarnessObservation),
        ("kernel-harness-assertion-result.schema.json", AssertionResult),
        ("candidate-change-set.schema.json", CandidateChangeSet),
        ("kernel-transport-probe.schema.json", TransportProbeResult),
        ("kernel-harness-cleanup.schema.json", CleanupReport),
    ],
)
def test_generated_harness_contracts_match_pydantic_models(
    file_name: str,
    model_type: type,
) -> None:
    stored = json.loads((CONTRACTS / file_name).read_text(encoding="utf-8"))
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == build_schema(file_name, model_type)


def _passing_results(
    case: HarnessCase,
) -> tuple[list[AssertionResult], dict[str, EvidenceKind]]:
    results: list[AssertionResult] = []
    evidence: dict[str, EvidenceKind] = {}
    for spec in case.assertions:
        refs: list[str] = []
        for kind in spec.required_evidence_kinds:
            ref = f"evidence://{spec.assertion_id}/{kind.value}"
            refs.append(ref)
            evidence[ref] = kind
        results.append(
            AssertionResult(
                assertion_id=spec.assertion_id,
                gate=spec.gate,
                operator=spec.operator,
                status=AssertionStatus.PASS,
                expected=spec.expected,
                observed_ref=refs[0],
                evidence_refs=refs,
                failure_fingerprint=None,
                message="Assertion passed with bounded evidence.",
            )
        )
    return results, evidence


def test_case_fixture_is_hash_pinned_and_prepares_a_real_01_09_project(
    tmp_path: Path,
) -> None:
    case = _case()
    source_before = tree_fingerprint(FIXTURE_ROOT)

    assert case.lane.value == "transport"
    verify_case_fixture(CASE_FILE, case)
    workspace = prepare_disposable_fixture(CASE_FILE, case, tmp_path / "run")

    assert workspace.source_fixture_fingerprint == case.fixture_fingerprint
    assert workspace.project_snapshot["project"]["display_name"] == "project"
    assert len(workspace.project_snapshot["sections"]) == 9
    assert len(workspace.project_snapshot["files"]) == 9
    assert workspace.project_snapshot["chapters"][0]["chapter_id"] == "VOL001-CH001"
    assert len(workspace.git_head) == 40
    assert not any(workspace.staging.iterdir())
    assert tree_fingerprint(FIXTURE_ROOT) == source_before


def test_stale_fixture_fingerprint_and_nonempty_destination_are_rejected(
    tmp_path: Path,
) -> None:
    case = _case()
    stale = case.model_copy(update={"fixture_fingerprint": "0" * 64})
    with pytest.raises(ValueError, match="fingerprint is stale"):
        verify_case_fixture(CASE_FILE, stale)

    destination = tmp_path / "occupied"
    destination.mkdir()
    (destination / "sentinel.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="absent or empty"):
        prepare_disposable_fixture(CASE_FILE, case, destination)
    assert (destination / "sentinel.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "malicious_path",
    ["../outside.json", "C:/outside.json", "C:outside.json", "//server/share/file.json"],
)
def test_case_graph_is_contiguous_strict_and_rejects_path_traversal(
    malicious_path: str,
) -> None:
    value = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    value["steps"][1]["ordinal"] = 7
    with pytest.raises(ValidationError, match="ordinals"):
        HarnessCase.model_validate(value)

    value = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    value["fixture"]["files"][0]["path"] = malicious_path
    with pytest.raises(ValidationError, match="cannot traverse"):
        HarnessCase.model_validate(value)

    value = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    value["vendor_private"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HarnessCase.model_validate(value)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "project/chapter.md:stream",
        "project/CON.txt",
        "project/chapter.",
        "project/chapter ",
        "project/.git/config",
    ],
)
def test_case_rejects_windows_aliases_ads_and_git_metadata(unsafe_path: str) -> None:
    value = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    value["fixture"]["files"][0]["path"] = unsafe_path

    with pytest.raises(ValidationError):
        HarnessCase.model_validate(value)


def test_case_rejects_case_insensitive_duplicate_fixture_paths() -> None:
    value = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    value["fixture"]["files"][1]["path"] = value["fixture"]["files"][0][
        "path"
    ].upper()

    with pytest.raises(ValidationError, match="must be unique"):
        HarnessCase.model_validate(value)


def test_fixture_rejects_git_metadata_and_hardlinks_before_copy(tmp_path: Path) -> None:
    copied_fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE_ROOT, copied_fixture)
    copied_case_file = copied_fixture / "case.json"
    case = HarnessCase.model_validate_json(
        copied_case_file.read_text(encoding="utf-8")
    )
    git_dir = copied_fixture / case.fixture.project_root / ".git"
    git_dir.mkdir()

    with pytest.raises(ValueError, match="Git metadata"):
        prepare_disposable_fixture(copied_case_file, case, tmp_path / "git-run")
    assert not (tmp_path / "git-run").exists()

    git_dir.rmdir()
    seed = case.fixture.files[0]
    seed_path = copied_fixture / seed.path
    external = tmp_path / "external-seed"
    external.write_bytes(seed_path.read_bytes())
    seed_path.unlink()
    try:
        os.link(external, seed_path)
    except OSError:
        pytest.skip("hardlinks are not available for this test account")

    with pytest.raises(ValueError, match="hardlinked"):
        prepare_disposable_fixture(copied_case_file, case, tmp_path / "hardlink-run")
    assert not (tmp_path / "hardlink-run").exists()


def test_state_machine_requires_cleanup_before_any_terminal_result() -> None:
    machine = HarnessStateMachine()
    for target in (
        HarnessState.PREPARING,
        HarnessState.STARTING,
        HarnessState.READY,
        HarnessState.RUNNING,
        HarnessState.CANCELLING,
        HarnessState.CANCELLED,
        HarnessState.RESTARTING,
        HarnessState.RESUMED,
        HarnessState.VERIFYING,
        HarnessState.CLEANING,
        HarnessState.PASSED,
    ):
        machine.transition(target, reason=f"advance to {target.value}")

    assert machine.state is HarnessState.PASSED
    assert len(machine.history) == 11
    with pytest.raises(ValueError, match="invalid harness transition"):
        HarnessStateMachine().transition(HarnessState.PASSED, reason="fake green")

    bypass = HarnessStateMachine()
    bypass.transition(HarnessState.CLEANING, reason="cleanup after preparation failure")
    with pytest.raises(ValueError, match="requires verification before cleanup"):
        bypass.transition(HarnessState.PASSED, reason="fake green after cleanup")
    bypass.transition(HarnessState.FAILED, reason="record preparation failure")


def test_transport_runner_uses_case_deadline_and_restricts_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case()
    observed: dict[str, float] = {}

    async def fake_wait_for(awaitable, *, timeout: float):
        observed["timeout"] = timeout
        return await awaitable

    async def sentinel() -> str:
        return "done"

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    assert asyncio.run(
        _await_case_step(case, HarnessOperation.TRANSPORT_HANDSHAKE, sentinel())
    ) == "done"
    assert observed == {"timeout": 60}

    contract = _transport_execution_contract(case)
    executable = {
        item["operation"]: item for item in contract["executable_steps"]
    }
    assert executable["transport_handshake"]["timeout_seconds"] == 60
    assert executable["cleanup"]["timeout_seconds"] == 60
    assert contract["preflight"]["timeout_enforcement"] == "not_measured"
    assert "step.bind-snapshot" in contract["unexecuted_step_ids"]

    safe = _resolve_safe_output_root(SAFE_OUTPUT_BASE / "contract-test-run")
    assert safe.is_relative_to(SAFE_OUTPUT_BASE.resolve(strict=False))
    for unsafe in (
        SAFE_OUTPUT_BASE,
        SAFE_OUTPUT_BASE / "existing-v11" / "nested-run",
        FIXTURE_ROOT / "output",
        Path(__file__).resolve().parents[1] / "src" / "output",
    ):
        with pytest.raises(ValueError, match="direct named-run child"):
            _resolve_safe_output_root(unsafe)


def test_cli_distinguishes_failed_and_blocked_harness_states() -> None:
    assert _cli_has_failure(
        [{"transport_passed": True, "harness_state": "failed"}]
    )
    assert _cli_has_failure(
        [{"transport_passed": True, "harness_state": "blocked"}]
    )
    assert _cli_exit_code([{"transport_passed": True, "harness_state": "failed"}]) == 1
    assert _cli_exit_code([{"transport_passed": True, "harness_state": "blocked"}]) == 2
    assert _cli_exit_code([{"transport_passed": True, "harness_state": "passed"}]) == 0


def test_late_evidence_secret_scan_redacts_raw_and_basic_tokens(
    tmp_path: Path,
) -> None:
    raw_secret = "fixture-password-123456789"
    credential_secret = f"fixture-user:{raw_secret}"
    basic_secret = "Zml4dHVyZS11c2VyOmZpeHR1cmUtcGFzc3dvcmQtMTIzNDU2Nzg5"
    basic_header = f"Basic {basic_secret}"
    secrets = (raw_secret, credential_secret, basic_header, basic_secret)
    (tmp_path / "stdout.log").write_text(raw_secret, encoding="utf-8")
    (tmp_path / "stderr.log").write_text(credential_secret, encoding="utf-8")
    (tmp_path / "probe-error.json").write_text(basic_secret, encoding="utf-8")
    (tmp_path / "transport-probe.json").write_text(basic_header, encoding="utf-8")
    cleanup_report = _cleanup_report_value()

    assert _finalize_cleanup_secret_scan(tmp_path, cleanup_report, secrets) == 4
    rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.iterdir()
    )
    assert raw_secret not in rendered
    assert credential_secret not in rendered
    assert basic_secret not in rendered
    assert basic_header not in rendered
    report = json.loads((tmp_path / "cleanup-report.json").read_text(encoding="utf-8"))
    assert report["evidence_secret_scan_passed"] is True
    assert report["evidence_secret_value_count"] == 4
    assert report["evidence_secret_redaction_count"] == 4
    assert _redact_sensitive_text(
        f"failed with {raw_secret} and {basic_header}", secrets
    ) == "failed with <redacted> and <redacted>"


def test_evidence_tree_total_limit_is_enforced_before_full_file_scans(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.log").write_bytes(b"a" * 5)
    (tmp_path / "second.log").write_bytes(b"b" * 4)

    assert _evidence_tree_size_bytes(tmp_path, limit_bytes=9) == 9
    with pytest.raises(ValueError, match="total limit"):
        _evidence_tree_size_bytes(tmp_path, limit_bytes=8)


def test_assertion_aggregation_never_turns_blocked_or_unmeasured_into_pass() -> None:
    case = _case()
    passing, evidence = _passing_results(case)
    validate_assertion_results(case, passing, evidence_kinds=evidence)

    assert set(gate_statuses(case, passing).values()) == {AssertionStatus.PASS}
    assert terminal_state_for_results(case, passing) is HarnessState.PASSED

    first = passing[0]
    blocked = first.model_copy(
        update={
            "status": AssertionStatus.BLOCKED,
            "observed_ref": None,
            "evidence_refs": [],
            "message": "Product transaction support is not implemented.",
        }
    )
    blocked_results = [blocked, *passing[1:]]
    validate_assertion_results(case, blocked_results, evidence_kinds=evidence)
    assert terminal_state_for_results(case, blocked_results) is HarnessState.BLOCKED

    failed = first.model_copy(
        update={
            "status": AssertionStatus.FAIL,
            "failure_fingerprint": hashlib.sha256(b"fixture changed").hexdigest(),
            "message": "The source fixture changed.",
        }
    )
    failed_results = [failed, *passing[1:]]
    validate_assertion_results(case, failed_results, evidence_kinds=evidence)
    assert terminal_state_for_results(case, failed_results) is HarnessState.FAILED


def test_cleanup_status_participates_in_terminal_state_aggregation() -> None:
    case = _case()
    passing, _ = _passing_results(case)

    assert terminal_state_for_run(
        case, passing, cleanup_status=CleanupStatus.CLEAN
    ) is HarnessState.PASSED
    for cleanup_status in (CleanupStatus.PARTIAL, CleanupStatus.NOT_MEASURED):
        assert terminal_state_for_run(
            case, passing, cleanup_status=cleanup_status
        ) is HarnessState.BLOCKED
    for cleanup_status in (CleanupStatus.DIRTY, CleanupStatus.FAILED):
        assert terminal_state_for_run(
            case, passing, cleanup_status=cleanup_status
        ) is HarnessState.FAILED
    with pytest.raises(ValueError, match="before cleanup"):
        terminal_state_for_run(
            case,
            passing,
            cleanup_status=CleanupStatus.NOT_RUN,
        )


def test_passing_assertion_requires_every_declared_evidence_kind() -> None:
    case = _case()
    passing, evidence = _passing_results(case)
    target = next(
        index
        for index, spec in enumerate(case.assertions)
        if len(spec.required_evidence_kinds) > 1
    )
    incomplete = passing[target].model_copy(
        update={"evidence_refs": passing[target].evidence_refs[:1]}
    )

    with pytest.raises(ValueError, match="lacks required evidence"):
        validate_assertion_results(
            case,
            [*passing[:target], incomplete, *passing[target + 1 :]],
            evidence_kinds=evidence,
        )


def _passing_run_value() -> dict[str, object]:
    case = _case()
    passing, _ = _passing_results(case)
    now = datetime.now(UTC)
    return {
        "schema": "zhimeng.kernel-harness-run.v2",
        "harness_run_id": "harness-run-0001",
        "case_id": case.case_id,
        "case_version": case.version,
        "case_fingerprint": canonical_case_fingerprint(case),
        "state": "passed",
        "adapter": ExecutorIdentity(
            executor_id="executor.test",
            kind="process",
            implementation="scripted fixture adapter",
            version="1.0.0",
            protocol_version="1",
            instance_id="instance.test.0001",
        ).model_dump(mode="json"),
        "binary_hash": "a" * 64,
        "protocol_schema_hash": "b" * 64,
        "config_hash": "c" * 64,
        "provider_model": ProviderModelProfile(
            provider_id="provider.fixture",
            model_id="fixture-model",
            protocol="openai-compatible",
            capability_probe_ref="evidence://provider/probe",
            capability_probe_hash="d" * 64,
        ).model_dump(mode="json"),
        "normalized_session_id": "session.test.0001",
        "normalized_run_ids": ["run.test.0001"],
        "case_step_ids": [step.step_id for step in case.steps],
        "case_assertion_ids": [item.assertion_id for item in case.assertions],
        "step_cursor": len(case.steps),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "baseline_snapshot_ref": "evidence://workspace/before",
        "final_snapshot_ref": "evidence://workspace/after",
        "observation_count": 1,
        "assertion_results": [item.model_dump(mode="json") for item in passing],
        "cleanup_status": "clean",
    }


def test_terminal_run_cannot_claim_pass_with_nonpassing_assertions() -> None:
    run_value = _passing_run_value()
    HarnessRun.model_validate(run_value)
    invalid_result = copy.deepcopy(run_value)
    invalid_result["assertion_results"][0]["status"] = "not_measured"
    invalid_result["assertion_results"][0]["evidence_refs"] = []
    invalid_result["assertion_results"][0]["observed_ref"] = None
    with pytest.raises(ValidationError, match="cannot contain non-passing"):
        HarnessRun.model_validate(invalid_result)

    empty_results = copy.deepcopy(run_value)
    empty_results["assertion_results"] = []
    with pytest.raises(ValidationError, match="at least 1 item"):
        HarnessRun.model_validate(empty_results)

    missing_snapshot = copy.deepcopy(run_value)
    missing_snapshot["final_snapshot_ref"] = None
    with pytest.raises(ValidationError, match="requires a final workspace snapshot"):
        HarnessRun.model_validate(missing_snapshot)

    partial_cleanup = copy.deepcopy(run_value)
    partial_cleanup["cleanup_status"] = "partial"
    with pytest.raises(ValidationError, match="requires clean cleanup"):
        HarnessRun.model_validate(partial_cleanup)

    missing_assertions = copy.deepcopy(run_value)
    missing_assertions["assertion_results"] = missing_assertions["assertion_results"][:1]
    with pytest.raises(ValidationError, match="cover the declared case assertions"):
        HarnessRun.model_validate(missing_assertions)

    incomplete_steps = copy.deepcopy(run_value)
    incomplete_steps["step_cursor"] = len(incomplete_steps["case_step_ids"]) - 1
    with pytest.raises(ValidationError, match="account for every case step"):
        HarnessRun.model_validate(incomplete_steps)


def test_harness_run_is_bound_to_the_case_contract() -> None:
    case = _case()
    run = HarnessRun.model_validate(_passing_run_value())
    validate_harness_run_against_case(case, run)

    tampered = run.model_copy(
        update={"case_step_ids": [*run.case_step_ids[:-1], "step.tampered"]}
    )
    with pytest.raises(ValueError, match="step set does not match"):
        validate_harness_run_against_case(case, tampered)


def _contract_validator(file_name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / file_name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    ("status", "evidence_refs", "failure_fingerprint"),
    [
        ("pass", [], None),
        ("pass", ["evidence://assertion/result"], "f" * 64),
        ("fail", ["evidence://assertion/result"], None),
        ("blocked", ["evidence://assertion/result"], "f" * 64),
        ("not_measured", ["evidence://assertion/result"], "f" * 64),
    ],
)
def test_assertion_result_json_schema_rejects_invalid_status_evidence_pairs(
    status: str,
    evidence_refs: list[str],
    failure_fingerprint: str | None,
) -> None:
    validator = _contract_validator(
        "kernel-harness-assertion-result.schema.json"
    )
    value = _passing_results(_case())[0][0].model_dump(mode="json")
    validator.validate(value)

    value.update(
        status=status,
        evidence_refs=evidence_refs,
        failure_fingerprint=failure_fingerprint,
    )
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(value)


@pytest.mark.parametrize(
    ("mutator", "assertion_status", "failure_fingerprint"),
    [
        ("cleanup", None, None),
        ("snapshot", None, None),
        ("assertion", "fail", "f" * 64),
        ("assertion", "blocked", None),
        ("assertion", "not_measured", None),
    ],
)
def test_harness_run_json_schema_rejects_false_pass_receipts(
    mutator: str,
    assertion_status: str | None,
    failure_fingerprint: str | None,
) -> None:
    validator = _contract_validator("kernel-harness-run.schema.json")
    value = _passing_run_value()
    validator.validate(value)

    if mutator == "cleanup":
        value["cleanup_status"] = "partial"
    elif mutator == "snapshot":
        value["final_snapshot_ref"] = None
    else:
        assertion_results = value["assertion_results"]
        assert isinstance(assertion_results, list)
        assertion = assertion_results[0]
        assert isinstance(assertion, dict)
        assertion["status"] = assertion_status
        assertion["failure_fingerprint"] = failure_fingerprint

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(value)


def _cleanup_report_value() -> dict[str, object]:
    return {
        "schema": "zhimeng.kernel-harness-cleanup.v3",
        "overall_status": "partial",
        "source_fixture_unchanged": True,
        "disposable_project_snapshot_unchanged": True,
        "write_denial_exercised": False,
        "provider_used": False,
        "user_root_snapshot_measured": False,
        "root_process_close_status": "pass",
        "process_tree_cleanup_status": "not_measured",
        "process_cleanup_error": None,
        "timed_step_deadlines": {"step.transport": 30, "step.cleanup": 15},
        "execution_contract": {
            "schema": "zhimeng.kernel-transport-execution-contract.v1"
        },
        "unmeasured_cleanup_dimensions": ["process_tree", "user_roots"],
        "resource_limits_passed": True,
        "adapter_resource_limits": {
            "adapter_stream_byte_limit": 8388608,
            "adapter_stream_bytes_written": 1024,
            "adapter_stream_truncated": False,
            "event_buffer": {
                "item_limit": 256,
                "byte_limit": 2097152,
                "per_event_byte_limit": 262144,
                "queued_items": 1,
                "queued_bytes": 128,
                "overflowed": False,
            },
        },
        "evidence_root_byte_limit": 16777216,
        "evidence_root_bytes_after_adapter_close": 2048,
        "evidence_secret_scan_passed": True,
        "evidence_secret_value_count": 2,
        "evidence_secret_redaction_count": 0,
    }


def test_cleanup_report_model_and_public_schema_validate_final_receipt() -> None:
    value = _cleanup_report_value()
    CleanupReport.model_validate(value)
    _contract_validator("kernel-harness-cleanup.schema.json").validate(value)


def test_cleanup_report_rejects_false_clean_and_unscanned_receipts() -> None:
    validator = _contract_validator("kernel-harness-cleanup.schema.json")
    false_clean = _cleanup_report_value()
    false_clean["overall_status"] = "clean"
    with pytest.raises(ValidationError, match="every cleanup dimension"):
        CleanupReport.model_validate(false_clean)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(false_clean)

    unscanned = _cleanup_report_value()
    unscanned["evidence_secret_scan_passed"] = False
    with pytest.raises(ValidationError, match="secret scan"):
        CleanupReport.model_validate(unscanned)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(unscanned)

    oversized = _cleanup_report_value()
    oversized["evidence_root_bytes_after_adapter_close"] = 16777217
    with pytest.raises(ValidationError, match="exceed"):
        CleanupReport.model_validate(oversized)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(oversized)


def test_cleanup_report_rejects_blank_error_on_failed_root_close() -> None:
    validator = _contract_validator("kernel-harness-cleanup.schema.json")
    blank = _cleanup_report_value()
    blank["root_process_close_status"] = "fail"
    blank["process_cleanup_error"] = ""
    blank["overall_status"] = "failed"

    with pytest.raises(ValidationError, match="non-empty error"):
        CleanupReport.model_validate(blank)
    with pytest.raises(JsonSchemaValidationError):
        validator.validate(blank)


def _candidate_change_set_value() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "schema": "zhimeng.candidate-change-set.v1",
        "change_set_id": "changeset.test.0001",
        "base_revision": ProjectRevision(
            project_id="project.test.0001",
            revision=1,
            fingerprint="e" * 64,
            observed_at=now,
        ).model_dump(mode="json"),
        "staging_root_ref": "artifact://staging/run-0001",
        "files": [
            {
                "file_id": "file.chapter.0001",
                "path": "05_正文内容/第一卷/CH001_正文.md",
                "operation": "update",
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
                "patch_format": "unified_diff",
                "patch_artifact_ref": "artifact://patch/chapter-001",
                "hunks": [
                    {
                        "hunk_id": "hunk.chapter.0001",
                        "anchor": "卷帘门被人从外面敲响。",
                        "patch_hash": "c" * 64,
                    }
                ],
            }
        ],
    }


def test_candidate_changeset_carries_content_addressed_hunks_and_revision() -> None:
    value = _candidate_change_set_value()

    change_set = CandidateChangeSet.model_validate(value)
    assert change_set.files[0].hunks[0].hunk_id == "hunk.chapter.0001"
    value["files"][0]["after_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="must differ"):
        CandidateChangeSet.model_validate(value)


@pytest.mark.parametrize(
    "mutation",
    ["traversal", "create_hashes", "delete_hashes"],
)
def test_candidate_changeset_schema_rejects_unsafe_paths_and_hash_transitions(
    mutation: str,
) -> None:
    value = _candidate_change_set_value()
    file_change = value["files"][0]
    assert isinstance(file_change, dict)
    if mutation == "traversal":
        file_change["path"] = "../outside.txt"
    elif mutation == "create_hashes":
        file_change.update(
            {
                "file_id": None,
                "operation": "create",
                "before_sha256": "a" * 64,
                "after_sha256": None,
            }
        )
    else:
        file_change.update(
            {
                "operation": "delete",
                "before_sha256": None,
                "after_sha256": "b" * 64,
            }
        )
    with pytest.raises(JsonSchemaValidationError):
        _contract_validator("candidate-change-set.schema.json").validate(value)


def test_observation_payload_is_bounded_and_schema_is_required() -> None:
    now = datetime.now(UTC)
    value = {
        "schema": "zhimeng.kernel-harness-observation.v1",
        "observation_id": "observation.0001",
        "sequence": 1,
        "step_id": "step.prepare",
        "recorded_at": now.isoformat(),
        "kind": "snapshot",
        "normalized_activity_id": None,
        "evidence_ref": "evidence://workspace/before",
        "payload": {"fingerprint": "a" * 64},
    }
    HarnessObservation.model_validate(value)
    value["payload"] = {"text": "x" * (64 * 1024)}
    with pytest.raises(ValidationError, match="exceeds 64 KiB"):
        HarnessObservation.model_validate(value)
    value.pop("schema")
    with pytest.raises(ValidationError):
        HarnessObservation.model_validate(value)


def test_logical_evidence_uris_are_bound_to_content_addressed_files(
    tmp_path: Path,
) -> None:
    for name, content in {
        "transport-probe.json": json.dumps(
            {
                "adapter": {"executor_id": "fixture"},
                "capabilities": {"declared": ["streaming"], "measured": []},
                "checks": [{"evidence_ref": "evidence://fixture/http-check"}],
            }
        )
        + "\n",
        "workspace-snapshots.json": json.dumps(
            {"project_before": "a" * 64, "project_after": "a" * 64}
        )
        + "\n",
        "cleanup-report.json": json.dumps({"root_process_close_status": "pass"})
        + "\n",
        "isolation-report.json": json.dumps(
            {"skill_isolation_status": "not_measured"}
        )
        + "\n",
        "http-transcript.ndjson": "{}\n",
        "stdout.log": "started\n",
        "stderr.log": "",
    }.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    catalog = {
        "evidence://fixture/protocol-transcript": EvidenceKind.PROTOCOL_TRANSCRIPT,
        "evidence://fixture/process-lifecycle": EvidenceKind.PROCESS_LIFECYCLE,
        "evidence://fixture/capability-snapshot": EvidenceKind.CAPABILITY_SNAPSHOT,
        "evidence://fixture/workspace-before": EvidenceKind.WORKSPACE_SNAPSHOT,
        "evidence://fixture/workspace-after": EvidenceKind.WORKSPACE_SNAPSHOT,
        "evidence://fixture/cleanup-report": EvidenceKind.CLEANUP_REPORT,
        "evidence://fixture/isolation-report": EvidenceKind.ISOLATION_REPORT,
    }

    index = build_evidence_index(
        tmp_path,
        adapter_name="opencode",
        adapter_id="fixture",
        catalog=catalog,
        probe_evidence_refs=["evidence://fixture/http-check"],
        receipt_evidence_refs=["evidence://fixture/workspace-after"],
    )

    entries = {item["uri"]: item for item in index["entries"]}
    assert entries["evidence://fixture/http-check"]["locations"][0]["path"] == (
        "transport-probe.json"
    )
    assert len(entries["evidence://fixture/workspace-after"]["locations"][0]["sha256"]) == 64
    with pytest.raises(ValueError, match="not bound"):
        build_evidence_index(
            tmp_path,
            adapter_name="opencode",
            adapter_id="fixture",
            catalog=catalog,
            probe_evidence_refs=[],
            receipt_evidence_refs=["evidence://fixture/missing"],
        )


def test_manifest_remains_valid_after_final_cleanup_and_index_are_written(
    tmp_path: Path,
) -> None:
    (tmp_path / "transport-probe.json").write_text(
        json.dumps(
            {
                "adapter": {"executor_id": "fixture"},
                "capabilities": {"declared": [], "measured": []},
                "checks": [{"evidence_ref": "evidence://fixture/http-check"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "workspace-snapshots.json").write_text(
        json.dumps({"project_before": "a" * 64, "project_after": "a" * 64}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "isolation-report.json").write_text(
        json.dumps({"skill_isolation_status": "not_measured"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "http-transcript.ndjson").write_text("{}\n", encoding="utf-8")
    cleanup = _cleanup_report_value()
    _finalize_cleanup_secret_scan(tmp_path, cleanup, ())
    catalog = {
        "evidence://fixture/workspace-after": EvidenceKind.WORKSPACE_SNAPSHOT,
        "evidence://fixture/cleanup-report": EvidenceKind.CLEANUP_REPORT,
        "evidence://fixture/isolation-report": EvidenceKind.ISOLATION_REPORT,
    }
    (tmp_path / "evidence-index.json").write_text(
        json.dumps(
            build_evidence_index(
                tmp_path,
                adapter_name="fixture",
                adapter_id="fixture",
                catalog=catalog,
                probe_evidence_refs=["evidence://fixture/http-check"],
                receipt_evidence_refs=[
                    "evidence://fixture/workspace-after",
                    "evidence://fixture/cleanup-report",
                    "evidence://fixture/isolation-report",
                ],
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_manifest(tmp_path)
    _validate_manifest(tmp_path)

    cleanup_path = tmp_path / "cleanup-report.json"
    cleanup_path.write_text(cleanup_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest entry drifted"):
        _validate_manifest(tmp_path)


def test_evidence_scan_rejects_hardlinks_before_reading(tmp_path: Path) -> None:
    original = tmp_path / "outside.log"
    original.write_text("external evidence", encoding="utf-8")
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    linked = evidence_root / "linked.log"
    os.link(original, linked)

    with pytest.raises(ValueError, match="hard link"):
        _evidence_tree_size_bytes(evidence_root)
