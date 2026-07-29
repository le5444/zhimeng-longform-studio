from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from zhimeng_runtime.execution_protocol import (
    ActivityKind,
    CapabilityName,
    ExecutorIdentity,
    ProjectRevision,
    Sha256Fingerprint,
    StableId,
    StrictModel,
)


RelativeFixturePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512),
]
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')
EvidenceReference = Annotated[
    str,
    StringConstraints(min_length=1, max_length=2048),
]


def validate_relative_fixture_path(value: str) -> str:
    if "\\" in value:
        raise ValueError("fixture paths must use forward slashes")
    path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows_path.drive
        or windows_path.is_absolute()
        or value in {".", ".."}
        or ".." in path.parts
    ):
        raise ValueError("fixture paths must be relative and cannot traverse")
    normalized = path.as_posix()
    if normalized != value or any(part in {"", "."} for part in path.parts):
        raise ValueError("fixture paths must be normalized POSIX paths")
    for part in path.parts:
        if part.casefold() == ".git":
            raise ValueError("fixture paths cannot address Git metadata")
        if part.endswith((" ", ".")):
            raise ValueError("fixture paths cannot use Windows trailing aliases")
        if any(character in _WINDOWS_FORBIDDEN_CHARS for character in part):
            raise ValueError("fixture paths contain Windows-reserved characters")
        device_name = part.split(".", 1)[0].upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise ValueError("fixture paths cannot use Windows device names")
    return value


def windows_path_identity(value: str) -> str:
    return PureWindowsPath(value).as_posix().casefold()


class HarnessLane(StrEnum):
    TRANSPORT = "transport"
    COMPATIBLE_PROVIDER = "compatible_provider"
    NATIVE_PROVIDER = "native_provider"


class HarnessOperation(StrEnum):
    PREPARE = "prepare"
    TRANSPORT_HANDSHAKE = "transport_handshake"
    BIND_SNAPSHOT = "bind_snapshot"
    BENCHMARK_TURN = "benchmark_turn"
    SKILL_CALL = "skill_call"
    CHANGE_TURN = "change_turn"
    PERMISSION_MATRIX = "permission_matrix"
    CANCEL_LONG_TOOL = "cancel_long_tool"
    RESTART_RESUME = "restart_resume"
    PARTIAL_HUNK_ACCEPT = "partial_hunk_accept"
    INJECT_REVISION_CONFLICT = "inject_revision_conflict"
    COMPACT_FOLLOWUP = "compact_followup"
    CLEANUP = "cleanup"


class HarnessGate(StrEnum):
    EMBEDDABLE = "G1"
    CONTROLLED_WRITES = "G2"
    RECOVERABLE = "G3"
    TRUTH_ISOLATION = "G4"
    WINDOWS_DISTRIBUTION = "G5"


class FixtureFileRole(StrEnum):
    PROJECT = "project"
    REFERENCE = "reference"
    SKILL = "skill"
    EXPECTED = "expected"


class ObservationKind(StrEnum):
    TRANSITION = "transition"
    PROCESS = "process"
    POLICY = "policy"
    SNAPSHOT = "snapshot"
    METRIC = "metric"
    RAW_EVENT_REF = "raw_event_ref"


class EvidenceKind(StrEnum):
    PROTOCOL_TRANSCRIPT = "protocol_transcript"
    CAPABILITY_SNAPSHOT = "capability_snapshot"
    ACTIVITY_STREAM = "activity_stream"
    ARTIFACT = "artifact"
    CITATION_REPORT = "citation_report"
    CHANGESET = "changeset"
    PERMISSION_DECISION = "permission_decision"
    PROCESS_LIFECYCLE = "process_lifecycle"
    WORKSPACE_SNAPSHOT = "workspace_snapshot"
    METRIC = "metric"
    CLEANUP_REPORT = "cleanup_report"
    ISOLATION_REPORT = "isolation_report"
    PROVIDER_PROBE = "provider_probe"


class AssertionOperator(StrEnum):
    CAPABILITY_PRESENT = "capability_present"
    ACTIVITY_KIND_PRESENT = "activity_kind_present"
    EVENT_SEQUENCE_VALID = "event_sequence_valid"
    STATUS_EQUALS = "status_equals"
    SNAPSHOT_UNCHANGED = "snapshot_unchanged"
    CITATION_FIELDS_PRESENT = "citation_fields_present"
    PERMISSION_EFFECT = "permission_effect"
    ARTIFACT_HASH_STABLE = "artifact_hash_stable"
    PATHS_CHANGED_ONLY = "paths_changed_only"
    CONFLICT_REJECTED = "conflict_rejected"
    CLEANUP_NO_DIFF = "cleanup_no_diff"
    TRANSPORT_HANDSHAKE_PASSED = "transport_handshake_passed"
    ISOLATION_BOUNDARY = "isolation_boundary"


class AssertionStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NOT_MEASURED = "not_measured"


class HarnessState(StrEnum):
    PLANNED = "planned"
    PREPARING = "preparing"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    RESTARTING = "restarting"
    RESUMED = "resumed"
    VERIFYING = "verifying"
    CLEANING = "cleaning"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"


class CleanupStatus(StrEnum):
    NOT_RUN = "not_run"
    NOT_MEASURED = "not_measured"
    PARTIAL = "partial"
    CLEAN = "clean"
    DIRTY = "dirty"
    FAILED = "failed"


class PatchFormat(StrEnum):
    UNIFIED_DIFF = "unified_diff"
    JSON_PATCH = "json_patch"


class HarnessSeedFile(StrictModel):
    file_id: StableId
    role: FixtureFileRole
    path: RelativeFixturePath
    sha256: Sha256Fingerprint
    size_bytes: int = Field(ge=0)

    _validate_path = field_validator("path")(validate_relative_fixture_path)


class HarnessFixtureSpec(StrictModel):
    project_root: RelativeFixturePath
    references_root: RelativeFixturePath
    skills_root: RelativeFixturePath
    staging_root: RelativeFixturePath
    expected_root: RelativeFixturePath
    files: list[HarnessSeedFile] = Field(min_length=1)
    formal_project_must_remain_unchanged: Literal[True] = True

    _validate_roots = field_validator(
        "project_root",
        "references_root",
        "skills_root",
        "staging_root",
        "expected_root",
    )(validate_relative_fixture_path)

    @model_validator(mode="after")
    def validate_layout(self) -> HarnessFixtureSpec:
        roots = {
            self.project_root,
            self.references_root,
            self.skills_root,
            self.staging_root,
            self.expected_root,
        }
        if len({windows_path_identity(root) for root in roots}) != 5:
            raise ValueError("fixture roots must be distinct")
        paths = [item.path for item in self.files]
        if len(paths) != len({windows_path_identity(path) for path in paths}):
            raise ValueError("fixture file paths must be unique")
        role_roots = {
            FixtureFileRole.PROJECT: self.project_root,
            FixtureFileRole.REFERENCE: self.references_root,
            FixtureFileRole.SKILL: self.skills_root,
            FixtureFileRole.EXPECTED: self.expected_root,
        }
        seen_roles = {item.role for item in self.files}
        if seen_roles != set(role_roots):
            raise ValueError("fixture requires project, reference, skill and expected files")
        for item in self.files:
            root = role_roots[item.role]
            if item.path != root and not item.path.startswith(f"{root}/"):
                raise ValueError(f"fixture file {item.path!r} is outside its role root")
        return self


class HarnessStepSpec(StrictModel):
    step_id: StableId
    ordinal: int = Field(ge=1)
    operation: HarnessOperation
    objective: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    timeout_seconds: int = Field(ge=1, le=3600)
    assertion_ids: list[StableId] = Field(min_length=1)
    required_capabilities: list[CapabilityName]
    expected_activity_kinds: list[ActivityKind]

    @model_validator(mode="after")
    def validate_unique_lists(self) -> HarnessStepSpec:
        for label, values in (
            ("assertion ids", self.assertion_ids),
            ("required capabilities", self.required_capabilities),
            ("expected activity kinds", self.expected_activity_kinds),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        return self


class HarnessAssertionSpec(StrictModel):
    assertion_id: StableId
    gate: HarnessGate
    operator: AssertionOperator
    expected: JsonValue
    required_evidence_kinds: list[EvidenceKind] = Field(min_length=1)
    required: bool = True

    @model_validator(mode="after")
    def validate_evidence_kinds(self) -> HarnessAssertionSpec:
        if len(self.required_evidence_kinds) != len(set(self.required_evidence_kinds)):
            raise ValueError("required evidence kinds must be unique")
        return self


class HarnessCase(StrictModel):
    schema_id: Literal["zhimeng.kernel-harness-case.v2"] = Field(alias="schema")
    case_id: StableId
    version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    lane: HarnessLane
    fixture_fingerprint: Sha256Fingerprint
    fixture: HarnessFixtureSpec
    steps: list[HarnessStepSpec] = Field(min_length=1)
    assertions: list[HarnessAssertionSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_case_graph(self) -> HarnessCase:
        step_ids = [step.step_id for step in self.steps]
        assertion_ids = [item.assertion_id for item in self.assertions]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("harness step ids must be unique")
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("harness assertion ids must be unique")
        if [step.ordinal for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("harness step ordinals must be contiguous from 1")
        referenced = [item for step in self.steps for item in step.assertion_ids]
        if len(referenced) != len(set(referenced)):
            raise ValueError("each assertion must belong to exactly one step")
        if set(referenced) != set(assertion_ids):
            raise ValueError("step assertion references must cover the case assertions")
        return self


class ProviderModelProfile(StrictModel):
    provider_id: StableId
    model_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    protocol: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    capability_probe_ref: EvidenceReference
    capability_probe_hash: Sha256Fingerprint


class HarnessObservation(StrictModel):
    schema_id: Literal["zhimeng.kernel-harness-observation.v1"] = Field(alias="schema")
    observation_id: StableId
    sequence: int = Field(ge=1)
    step_id: StableId
    recorded_at: datetime
    kind: ObservationKind
    normalized_activity_id: StableId | None
    evidence_ref: EvidenceReference
    payload: JsonValue

    @model_validator(mode="after")
    def validate_payload_budget(self) -> HarnessObservation:
        rendered = json.dumps(
            self.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(rendered) > 64 * 1024:
            raise ValueError("harness observation payload exceeds 64 KiB")
        return self


class AssertionResult(StrictModel):
    assertion_id: StableId
    gate: HarnessGate
    operator: AssertionOperator
    status: AssertionStatus
    expected: JsonValue
    observed_ref: EvidenceReference | None
    evidence_refs: list[EvidenceReference]
    failure_fingerprint: Sha256Fingerprint | None
    message: Annotated[str, StringConstraints(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def validate_result_evidence(self) -> AssertionResult:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("assertion evidence refs must be unique")
        if self.status is AssertionStatus.PASS:
            if not self.evidence_refs or self.failure_fingerprint is not None:
                raise ValueError("passing assertions require evidence and no failure fingerprint")
        elif self.status is AssertionStatus.FAIL:
            if self.failure_fingerprint is None:
                raise ValueError("failed assertions require a failure fingerprint")
        elif self.failure_fingerprint is not None:
            raise ValueError("blocked or unmeasured assertions cannot carry a failure fingerprint")
        return self


class HarnessRun(StrictModel):
    schema_id: Literal["zhimeng.kernel-harness-run.v2"] = Field(alias="schema")
    harness_run_id: StableId
    case_id: StableId
    case_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    case_fingerprint: Sha256Fingerprint
    state: HarnessState
    adapter: ExecutorIdentity
    binary_hash: Sha256Fingerprint
    protocol_schema_hash: Sha256Fingerprint
    config_hash: Sha256Fingerprint
    provider_model: ProviderModelProfile | None
    normalized_session_id: StableId | None
    normalized_run_ids: list[StableId]
    case_step_ids: list[StableId] = Field(min_length=1)
    case_assertion_ids: list[StableId] = Field(min_length=1)
    step_cursor: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    baseline_snapshot_ref: EvidenceReference
    final_snapshot_ref: EvidenceReference | None
    observation_count: int = Field(ge=0)
    assertion_results: list[AssertionResult] = Field(min_length=1)
    cleanup_status: CleanupStatus

    @model_validator(mode="after")
    def validate_run(self) -> HarnessRun:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if len(self.normalized_run_ids) != len(set(self.normalized_run_ids)):
            raise ValueError("normalized run ids must be unique")
        if len(self.case_step_ids) != len(set(self.case_step_ids)):
            raise ValueError("case step ids must be unique")
        if len(self.case_assertion_ids) != len(set(self.case_assertion_ids)):
            raise ValueError("case assertion ids must be unique")
        result_ids = [result.assertion_id for result in self.assertion_results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("assertion results must be unique")
        if set(result_ids) != set(self.case_assertion_ids):
            raise ValueError("assertion results must cover the declared case assertions exactly")
        if self.step_cursor > len(self.case_step_ids):
            raise ValueError("step_cursor cannot exceed the declared case steps")
        terminal = self.state in {
            HarnessState.PASSED,
            HarnessState.FAILED,
            HarnessState.BLOCKED,
        }
        if terminal:
            if self.completed_at is None or self.cleanup_status is CleanupStatus.NOT_RUN:
                raise ValueError("terminal harness runs require completion and cleanup")
            if self.step_cursor != len(self.case_step_ids):
                raise ValueError("terminal harness runs must account for every case step")
        elif self.completed_at is not None:
            raise ValueError("non-terminal harness runs cannot be completed")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must not precede created_at")
        if self.state is HarnessState.PASSED:
            if self.final_snapshot_ref is None:
                raise ValueError("a passed harness run requires a final workspace snapshot")
            if self.cleanup_status is not CleanupStatus.CLEAN:
                raise ValueError("a passed harness run requires clean cleanup")
            if any(
                result.status is not AssertionStatus.PASS
                for result in self.assertion_results
            ):
                raise ValueError("a passed harness run cannot contain non-passing assertions")
        return self


class CandidateHunk(StrictModel):
    hunk_id: StableId
    anchor: Annotated[str, StringConstraints(min_length=1, max_length=1024)]
    patch_hash: Sha256Fingerprint


class CandidateFileChange(StrictModel):
    file_id: StableId | None
    path: RelativeFixturePath
    operation: Literal["create", "update", "delete"]
    before_sha256: Sha256Fingerprint | None
    after_sha256: Sha256Fingerprint | None
    patch_format: PatchFormat
    patch_artifact_ref: EvidenceReference
    hunks: list[CandidateHunk] = Field(min_length=1)

    _validate_path = field_validator("path")(validate_relative_fixture_path)

    @model_validator(mode="after")
    def validate_hash_transition(self) -> CandidateFileChange:
        if self.operation == "create":
            if self.file_id is not None or self.before_sha256 is not None or self.after_sha256 is None:
                raise ValueError("create changes require only after_sha256")
        elif self.operation == "update":
            if self.file_id is None or self.before_sha256 is None or self.after_sha256 is None:
                raise ValueError("update changes require file_id and both hashes")
            if self.before_sha256 == self.after_sha256:
                raise ValueError("update hashes must differ")
        elif self.file_id is None or self.before_sha256 is None or self.after_sha256 is not None:
            raise ValueError("delete changes require file_id and only before_sha256")
        hunk_ids = [hunk.hunk_id for hunk in self.hunks]
        if len(hunk_ids) != len(set(hunk_ids)):
            raise ValueError("candidate hunk ids must be unique per file")
        return self


class CandidateChangeSet(StrictModel):
    schema_id: Literal["zhimeng.candidate-change-set.v1"] = Field(alias="schema")
    change_set_id: StableId
    base_revision: ProjectRevision
    staging_root_ref: EvidenceReference
    files: list[CandidateFileChange] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_files(self) -> CandidateChangeSet:
        paths = [item.path for item in self.files]
        if len(paths) != len({windows_path_identity(path) for path in paths}):
            raise ValueError("candidate change paths must be unique")
        return self


def canonical_case_fingerprint(case: HarnessCase) -> str:
    material = case.model_dump(mode="json", by_alias=True)
    material.pop("fixture_fingerprint", None)
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
