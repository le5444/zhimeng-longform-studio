from __future__ import annotations

from collections.abc import Mapping, Sequence

from kernel_harness.models import (
    AssertionResult,
    AssertionStatus,
    CleanupStatus,
    EvidenceKind,
    HarnessCase,
    HarnessGate,
    HarnessRun,
    HarnessState,
    canonical_case_fingerprint,
)


_STATUS_PRECEDENCE = {
    AssertionStatus.PASS: 0,
    AssertionStatus.NOT_MEASURED: 1,
    AssertionStatus.BLOCKED: 2,
    AssertionStatus.FAIL: 3,
}


def validate_assertion_results(
    case: HarnessCase,
    results: Sequence[AssertionResult],
    *,
    evidence_kinds: Mapping[str, EvidenceKind],
) -> None:
    specs = {item.assertion_id: item for item in case.assertions}
    result_ids = [item.assertion_id for item in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("harness assertion results contain duplicate ids")
    if set(result_ids) != set(specs):
        raise ValueError("harness assertion results must cover the case exactly")

    for result in results:
        spec = specs[result.assertion_id]
        if result.gate is not spec.gate or result.operator is not spec.operator:
            raise ValueError(f"assertion result metadata drift: {result.assertion_id}")
        if result.expected != spec.expected:
            raise ValueError(f"assertion expected value drift: {result.assertion_id}")
        unknown_refs = [ref for ref in result.evidence_refs if ref not in evidence_kinds]
        if unknown_refs:
            raise ValueError(f"assertion references unknown evidence: {result.assertion_id}")
        if result.status is AssertionStatus.PASS:
            observed_kinds = {evidence_kinds[ref] for ref in result.evidence_refs}
            missing = set(spec.required_evidence_kinds) - observed_kinds
            if missing:
                names = ", ".join(sorted(item.value for item in missing))
                raise ValueError(
                    f"passing assertion lacks required evidence {names}: {result.assertion_id}"
                )


def validate_harness_run_against_case(case: HarnessCase, run: HarnessRun) -> None:
    expected_step_ids = [step.step_id for step in case.steps]
    expected_assertion_ids = [item.assertion_id for item in case.assertions]
    if run.case_id != case.case_id or run.case_version != case.version:
        raise ValueError("harness run case identity does not match the case contract")
    if run.case_fingerprint != canonical_case_fingerprint(case):
        raise ValueError("harness run case fingerprint does not match the case contract")
    if run.case_step_ids != expected_step_ids:
        raise ValueError("harness run step set does not match the case contract")
    if run.case_assertion_ids != expected_assertion_ids:
        raise ValueError("harness run assertion set does not match the case contract")
    if run.step_cursor > len(case.steps):
        raise ValueError("harness run step cursor exceeds the case contract")
    specs = {item.assertion_id: item for item in case.assertions}
    for result in run.assertion_results:
        spec = specs[result.assertion_id]
        if result.gate is not spec.gate or result.operator is not spec.operator:
            raise ValueError(f"harness run assertion metadata drift: {result.assertion_id}")
        if result.expected != spec.expected:
            raise ValueError(f"harness run assertion expected value drift: {result.assertion_id}")


def gate_statuses(
    case: HarnessCase,
    results: Sequence[AssertionResult],
) -> dict[HarnessGate, AssertionStatus]:
    by_id = {item.assertion_id: item for item in results}
    statuses: dict[HarnessGate, AssertionStatus] = {}
    for gate in HarnessGate:
        required = [
            by_id[spec.assertion_id].status
            for spec in case.assertions
            if spec.gate is gate and spec.required
        ]
        if not required:
            statuses[gate] = AssertionStatus.NOT_MEASURED
            continue
        statuses[gate] = max(required, key=_STATUS_PRECEDENCE.__getitem__)
    return statuses


def terminal_state_for_results(
    case: HarnessCase,
    results: Sequence[AssertionResult],
) -> HarnessState:
    statuses = gate_statuses(case, results).values()
    if AssertionStatus.FAIL in statuses:
        return HarnessState.FAILED
    if AssertionStatus.BLOCKED in statuses or AssertionStatus.NOT_MEASURED in statuses:
        return HarnessState.BLOCKED
    return HarnessState.PASSED


def terminal_state_for_run(
    case: HarnessCase,
    results: Sequence[AssertionResult],
    *,
    cleanup_status: CleanupStatus,
) -> HarnessState:
    if cleanup_status is CleanupStatus.NOT_RUN:
        raise ValueError("cannot aggregate a terminal harness state before cleanup")
    assertion_state = terminal_state_for_results(case, results)
    if cleanup_status in {CleanupStatus.DIRTY, CleanupStatus.FAILED}:
        return HarnessState.FAILED
    if cleanup_status in {CleanupStatus.NOT_MEASURED, CleanupStatus.PARTIAL}:
        return (
            HarnessState.FAILED
            if assertion_state is HarnessState.FAILED
            else HarnessState.BLOCKED
        )
    return assertion_state
