from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from kernel_harness.models import HarnessState


ALLOWED_TRANSITIONS: dict[HarnessState, frozenset[HarnessState]] = {
    HarnessState.PLANNED: frozenset({HarnessState.PREPARING, HarnessState.CLEANING}),
    HarnessState.PREPARING: frozenset({HarnessState.STARTING, HarnessState.CLEANING}),
    HarnessState.STARTING: frozenset({HarnessState.READY, HarnessState.CLEANING}),
    HarnessState.READY: frozenset({HarnessState.RUNNING, HarnessState.CLEANING}),
    HarnessState.RUNNING: frozenset(
        {
            HarnessState.AWAITING_APPROVAL,
            HarnessState.CANCELLING,
            HarnessState.VERIFYING,
            HarnessState.CLEANING,
        }
    ),
    HarnessState.AWAITING_APPROVAL: frozenset(
        {HarnessState.RUNNING, HarnessState.CANCELLING, HarnessState.CLEANING}
    ),
    HarnessState.CANCELLING: frozenset(
        {HarnessState.CANCELLED, HarnessState.CLEANING}
    ),
    HarnessState.CANCELLED: frozenset(
        {HarnessState.RESTARTING, HarnessState.CLEANING}
    ),
    HarnessState.RESTARTING: frozenset(
        {HarnessState.RESUMED, HarnessState.CLEANING}
    ),
    HarnessState.RESUMED: frozenset(
        {HarnessState.RUNNING, HarnessState.VERIFYING, HarnessState.CLEANING}
    ),
    HarnessState.VERIFYING: frozenset({HarnessState.CLEANING}),
    HarnessState.CLEANING: frozenset(
        {HarnessState.PASSED, HarnessState.FAILED, HarnessState.BLOCKED}
    ),
    HarnessState.PASSED: frozenset(),
    HarnessState.FAILED: frozenset(),
    HarnessState.BLOCKED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StateTransition:
    source: HarnessState
    target: HarnessState
    reason: str
    occurred_at: datetime


@dataclass(slots=True)
class HarnessStateMachine:
    state: HarnessState = HarnessState.PLANNED
    history: list[StateTransition] = field(default_factory=list)

    def transition(
        self,
        target: HarnessState,
        *,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> StateTransition:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(
                f"invalid harness transition {self.state.value} -> {target.value}"
            )
        if target is HarnessState.PASSED:
            cleaning_entry = self.history[-1] if self.history else None
            if (
                cleaning_entry is None
                or cleaning_entry.target is not HarnessState.CLEANING
                or cleaning_entry.source is not HarnessState.VERIFYING
            ):
                raise ValueError(
                    "invalid harness transition cleaning -> passed: "
                    "passing requires verification before cleanup"
                )
        if not reason.strip():
            raise ValueError("harness transitions require a reason")
        transition = StateTransition(
            source=self.state,
            target=target,
            reason=reason.strip(),
            occurred_at=occurred_at or datetime.now(UTC),
        )
        self.history.append(transition)
        self.state = target
        return transition
