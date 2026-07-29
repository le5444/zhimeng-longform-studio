from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json

from zhimeng_runtime.managed_skill_service import (
    ManagedSkillActivationConflictError,
    ManagedSkillActivationInvalidError,
    ManagedSkillError,
    ManagedSkillNotFoundError,
    ManagedSkillService,
)
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest
from zhimeng_runtime.skill_rule_evolution_models import (
    SkillRuleEvolutionAction,
    SkillRuleEvolutionActionRequest,
    SkillRuleEvolutionCompareStatus,
    SkillRuleEvolutionPrepareRequest,
    SkillRuleEvolutionReceipt,
    SkillRuleEvolutionResponse,
    SkillRuleEvolutionStatus,
    skill_rule_evolution_receipt_hash,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class SkillRuleEvolutionError(RuntimeError):
    code = "skill_rule_evolution_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class SkillRuleEvolutionNotFoundError(SkillRuleEvolutionError):
    code = "skill_rule_evolution_not_found"


class SkillRuleEvolutionInvalidError(SkillRuleEvolutionError):
    code = "skill_rule_evolution_invalid"


class SkillRuleEvolutionConflictError(SkillRuleEvolutionError):
    code = "skill_rule_evolution_conflict"


class SkillRuleEvolutionBlockedError(SkillRuleEvolutionError):
    code = "skill_rule_evolution_blocked"


class SkillRuleEvolutionService:
    """Author-controlled Skill/rule upgrade with freeze/compare/rollback.

    Local v1 never auto-upgrades production Skills. When an eval-corpus baseline
    is bound, compare must be non_degraded before apply.
    """

    def __init__(
        self,
        managed_skill_service: ManagedSkillService,
        *,
        state_root: Path,
        change_set_service: object | None = None,
    ) -> None:
        self.managed = managed_skill_service
        self.state_root = Path(state_root)
        self.change_set_service = change_set_service
        self.state_root.mkdir(parents=True, exist_ok=True)

    def get(self, evolution_id: str) -> SkillRuleEvolutionResponse:
        receipt = self._read_receipt(evolution_id)
        if receipt is None:
            raise SkillRuleEvolutionNotFoundError("skill rule evolution was not found")
        history = self._read_history(evolution_id)
        return SkillRuleEvolutionResponse(receipt=receipt, history=history)

    def prepare(
        self,
        request: SkillRuleEvolutionPrepareRequest,
    ) -> SkillRuleEvolutionResponse:
        skill_id = request.skill_id.strip()
        if not skill_id:
            raise SkillRuleEvolutionInvalidError("skill_id is required")

        activation = self._require_activation(skill_id, project_id=request.project_id)
        if activation.effective_state != "enabled" or activation.effective_version_id is None:
            raise SkillRuleEvolutionInvalidError(
                "skill rule evolution prepare requires a currently enabled Skill version"
            )
        if (
            request.expected_current_version_id is not None
            and request.expected_current_version_id != activation.effective_version_id
        ):
            raise SkillRuleEvolutionConflictError(
                "current enabled Skill version does not match prepare request"
            )
        if (
            request.expected_state_revision is not None
            and request.expected_state_revision != activation.state_revision
        ):
            raise SkillRuleEvolutionConflictError(
                "Skill activation revision does not match prepare request"
            )
        if request.target_version_id == activation.effective_version_id:
            raise SkillRuleEvolutionInvalidError(
                "target Skill version must differ from the currently enabled version"
            )

        from_version = self._require_version_record(
            skill_id,
            activation.effective_version_id,
            project_id=request.project_id,
        )
        to_version = self._require_version_record(
            skill_id,
            request.target_version_id,
            project_id=request.project_id,
        )

        updated_at = _utcnow()
        evolution_id = (
            "skill-rule-evolution-"
            + hashlib.sha256(
                f"{skill_id}\0{request.project_id or ''}\0{from_version.skill_version_id}"
                f"\0{to_version.skill_version_id}\0{updated_at.isoformat()}".encode("utf-8")
            ).hexdigest()[:32]
        )
        receipt = self._build_receipt(
            evolution_id=evolution_id,
            skill_id=skill_id,
            project_id=request.project_id,
            change_set_id=request.change_set_id,
            status=SkillRuleEvolutionStatus.PREPARED,
            from_version_id=from_version.skill_version_id,
            from_version=from_version.manifest.version,
            from_package_fingerprint=from_version.package_fingerprint,
            to_version_id=to_version.skill_version_id,
            to_version=to_version.manifest.version,
            to_package_fingerprint=to_version.package_fingerprint,
            compare_status=SkillRuleEvolutionCompareStatus.DEFERRED,
            eval_corpus_baseline_receipt_id=None,
            eval_corpus_regression_receipt_id=None,
            activation_receipt_id=None,
            activation_state_revision=activation.state_revision,
            detail=(
                "froze current and target Skill identities for author-controlled upgrade; "
                "compare has not run yet and auto-upgrade remains false"
            ),
            updated_at=updated_at,
        )
        self._persist(receipt, append_history=True)
        return SkillRuleEvolutionResponse(
            receipt=receipt,
            history=self._read_history(evolution_id),
        )

    def act(
        self,
        evolution_id: str,
        request: SkillRuleEvolutionActionRequest,
    ) -> SkillRuleEvolutionResponse:
        current = self._read_receipt(evolution_id)
        if current is None:
            raise SkillRuleEvolutionNotFoundError("skill rule evolution was not found")
        if (
            request.expected_status is not None
            and request.expected_status is not current.status
        ):
            raise SkillRuleEvolutionConflictError(
                "skill rule evolution status does not match action request"
            )

        if request.action is SkillRuleEvolutionAction.COMPARE:
            receipt = self._compare(current)
        elif request.action is SkillRuleEvolutionAction.APPLY:
            receipt = self._apply(current, expected_state_revision=request.expected_state_revision)
        elif request.action is SkillRuleEvolutionAction.ROLLBACK:
            receipt = self._rollback(
                current,
                expected_state_revision=request.expected_state_revision,
            )
        else:
            raise SkillRuleEvolutionInvalidError(f"unsupported action: {request.action}")

        self._persist(receipt, append_history=True)
        return SkillRuleEvolutionResponse(
            receipt=receipt,
            history=self._read_history(evolution_id),
        )

    def _compare(self, current: SkillRuleEvolutionReceipt) -> SkillRuleEvolutionReceipt:
        if current.status not in {
            SkillRuleEvolutionStatus.PREPARED,
            SkillRuleEvolutionStatus.COMPARE_READY,
            SkillRuleEvolutionStatus.COMPARE_BLOCKED,
        }:
            raise SkillRuleEvolutionInvalidError(
                "compare is only allowed from prepared or previous compare states"
            )

        compare_status = SkillRuleEvolutionCompareStatus.DEFERRED
        baseline_id = None
        regression_id = None
        detail = (
            "no eval-corpus baseline bound; compare deferred and author may still apply"
        )
        status = SkillRuleEvolutionStatus.COMPARE_READY

        if current.change_set_id and self.change_set_service is not None:
            get_baseline = getattr(self.change_set_service, "get_eval_corpus_baseline", None)
            evaluate_regression = getattr(
                self.change_set_service, "evaluate_eval_corpus_regression", None
            )
            if callable(get_baseline) and callable(evaluate_regression):
                baseline_response = get_baseline(current.change_set_id)
                baseline = getattr(baseline_response, "receipt", None)
                if baseline is None:
                    compare_status = SkillRuleEvolutionCompareStatus.MISSING_BASELINE
                    detail = (
                        "eval-corpus baseline missing for bound change set; "
                        "compare deferred until baseline freeze"
                    )
                    status = SkillRuleEvolutionStatus.COMPARE_READY
                else:
                    baseline_id = getattr(baseline, "receipt_id", None)
                    from zhimeng_runtime.eval_corpus_regression_models import (
                        EvalCorpusRegressionEvaluateRequest,
                    )

                    regression_response = evaluate_regression(
                        current.change_set_id,
                        EvalCorpusRegressionEvaluateRequest(),
                    )
                    regression = getattr(regression_response, "receipt", None)
                    if regression is None:
                        compare_status = SkillRuleEvolutionCompareStatus.MISSING_EXPORT
                        status = SkillRuleEvolutionStatus.COMPARE_BLOCKED
                        detail = "eval-corpus regression receipt missing after evaluate"
                    else:
                        regression_id = getattr(regression, "receipt_id", None)
                        regression_status = getattr(
                            getattr(regression, "status", None),
                            "value",
                            getattr(regression, "status", None),
                        )
                        if regression_status == "non_degraded":
                            compare_status = SkillRuleEvolutionCompareStatus.NON_DEGRADED
                            status = SkillRuleEvolutionStatus.COMPARE_READY
                            detail = (
                                "eval-corpus non_degraded compare passed for bound change set; "
                                "author may apply upgrade"
                            )
                        else:
                            compare_status = SkillRuleEvolutionCompareStatus.DEGRADED
                            status = SkillRuleEvolutionStatus.COMPARE_BLOCKED
                            detail = (
                                "eval-corpus compare degraded for bound change set; "
                                "upgrade apply is blocked"
                            )
        elif current.change_set_id and self.change_set_service is None:
            compare_status = SkillRuleEvolutionCompareStatus.MISSING_EXPORT
            status = SkillRuleEvolutionStatus.COMPARE_BLOCKED
            detail = (
                "change set was bound but eval-corpus service is unavailable; "
                "upgrade apply is blocked"
            )

        return self._build_receipt(
            evolution_id=current.evolution_id,
            skill_id=current.skill_id,
            project_id=current.project_id,
            change_set_id=current.change_set_id,
            status=status,
            from_version_id=current.from_version_id,
            from_version=current.from_version,
            from_package_fingerprint=current.from_package_fingerprint,
            to_version_id=current.to_version_id,
            to_version=current.to_version,
            to_package_fingerprint=current.to_package_fingerprint,
            compare_status=compare_status,
            eval_corpus_baseline_receipt_id=baseline_id,
            eval_corpus_regression_receipt_id=regression_id,
            activation_receipt_id=current.activation_receipt_id,
            activation_state_revision=current.activation_state_revision,
            detail=detail,
            updated_at=_utcnow(),
        )

    def _apply(
        self,
        current: SkillRuleEvolutionReceipt,
        *,
        expected_state_revision: int | None,
    ) -> SkillRuleEvolutionReceipt:
        if current.status is SkillRuleEvolutionStatus.COMPARE_BLOCKED:
            raise SkillRuleEvolutionBlockedError(
                "skill rule evolution compare blocked the upgrade"
            )
        if current.status not in {
            SkillRuleEvolutionStatus.COMPARE_READY,
            SkillRuleEvolutionStatus.PREPARED,
        }:
            raise SkillRuleEvolutionInvalidError(
                "apply requires compare_ready or prepared (deferred) evolution"
            )
        if current.status is SkillRuleEvolutionStatus.PREPARED:
            # Force an explicit compare pass first when no prior compare exists.
            compared = self._compare(current)
            if compared.status is SkillRuleEvolutionStatus.COMPARE_BLOCKED:
                self._persist(compared, append_history=True)
                raise SkillRuleEvolutionBlockedError(
                    "skill rule evolution compare blocked the upgrade"
                )
            current = compared

        activation = self._require_activation(
            current.skill_id, project_id=current.project_id
        )
        state_revision = (
            expected_state_revision
            if expected_state_revision is not None
            else activation.state_revision
        )
        try:
            response = self.managed.activate(
                current.skill_id,
                ManagedSkillActivationRequest(
                    action="upgrade",
                    skill_version_id=current.to_version_id,
                    expected_state_revision=state_revision,
                    expected_version_id=current.from_version_id,
                ),
                project_id=current.project_id,
            )
        except (
            ManagedSkillActivationConflictError,
            ManagedSkillActivationInvalidError,
            ManagedSkillNotFoundError,
            ManagedSkillError,
        ) as exc:
            raise SkillRuleEvolutionConflictError(str(exc)) from exc

        return self._build_receipt(
            evolution_id=current.evolution_id,
            skill_id=current.skill_id,
            project_id=current.project_id,
            change_set_id=current.change_set_id,
            status=SkillRuleEvolutionStatus.APPLIED,
            from_version_id=current.from_version_id,
            from_version=current.from_version,
            from_package_fingerprint=current.from_package_fingerprint,
            to_version_id=current.to_version_id,
            to_version=current.to_version,
            to_package_fingerprint=current.to_package_fingerprint,
            compare_status=current.compare_status,
            eval_corpus_baseline_receipt_id=current.eval_corpus_baseline_receipt_id,
            eval_corpus_regression_receipt_id=current.eval_corpus_regression_receipt_id,
            activation_receipt_id=response.receipt.receipt_id,
            activation_state_revision=response.activation.state_revision,
            detail=(
                "author-applied Skill upgrade after freeze/compare; "
                "formal draft remains locked and auto-upgrade remains false"
            ),
            updated_at=_utcnow(),
        )

    def _rollback(
        self,
        current: SkillRuleEvolutionReceipt,
        *,
        expected_state_revision: int | None,
    ) -> SkillRuleEvolutionReceipt:
        if current.status is not SkillRuleEvolutionStatus.APPLIED:
            raise SkillRuleEvolutionInvalidError(
                "rollback requires an applied skill rule evolution"
            )
        activation = self._require_activation(
            current.skill_id, project_id=current.project_id
        )
        state_revision = (
            expected_state_revision
            if expected_state_revision is not None
            else activation.state_revision
        )
        try:
            response = self.managed.activate(
                current.skill_id,
                ManagedSkillActivationRequest(
                    action="rollback",
                    skill_version_id=current.from_version_id,
                    expected_state_revision=state_revision,
                    expected_version_id=current.to_version_id,
                ),
                project_id=current.project_id,
            )
        except (
            ManagedSkillActivationConflictError,
            ManagedSkillActivationInvalidError,
            ManagedSkillNotFoundError,
            ManagedSkillError,
        ) as exc:
            raise SkillRuleEvolutionConflictError(str(exc)) from exc

        return self._build_receipt(
            evolution_id=current.evolution_id,
            skill_id=current.skill_id,
            project_id=current.project_id,
            change_set_id=current.change_set_id,
            status=SkillRuleEvolutionStatus.ROLLED_BACK,
            from_version_id=current.from_version_id,
            from_version=current.from_version,
            from_package_fingerprint=current.from_package_fingerprint,
            to_version_id=current.to_version_id,
            to_version=current.to_version,
            to_package_fingerprint=current.to_package_fingerprint,
            compare_status=current.compare_status,
            eval_corpus_baseline_receipt_id=current.eval_corpus_baseline_receipt_id,
            eval_corpus_regression_receipt_id=current.eval_corpus_regression_receipt_id,
            activation_receipt_id=response.receipt.receipt_id,
            activation_state_revision=response.activation.state_revision,
            detail=(
                "author rolled Skill upgrade back to frozen previous version; "
                "formal draft remains locked"
            ),
            updated_at=_utcnow(),
        )

    def _require_activation(self, skill_id: str, *, project_id: str | None):
        catalog = self.managed.list_lifecycle(project_id=project_id)
        for item in catalog.activations:
            if item.skill_id == skill_id:
                return item
        raise SkillRuleEvolutionNotFoundError(
            "managed Skill activation was not found for evolution scope"
        )

    def _require_version_record(
        self,
        skill_id: str,
        version_id: str,
        *,
        project_id: str | None,
    ):
        catalog = self.managed.list_lifecycle(project_id=project_id)
        for item in catalog.versions:
            if item.skill_version_id == version_id:
                if item.manifest.id != skill_id:
                    raise SkillRuleEvolutionInvalidError(
                        "target Skill version does not belong to the requested skill_id"
                    )
                return item
        raise SkillRuleEvolutionNotFoundError(
            "managed Skill version was not found for evolution scope"
        )

    def _build_receipt(self, **kwargs: object) -> SkillRuleEvolutionReceipt:
        provisional = SkillRuleEvolutionReceipt.model_construct(
            schema_id="zhimeng.skill-rule-evolution-receipt.v1",
            receipt_hash="0" * 64,
            **kwargs,
        )
        return SkillRuleEvolutionReceipt(
            receipt_hash=skill_rule_evolution_receipt_hash(provisional),
            **kwargs,
        )

    def _receipt_path(self, evolution_id: str) -> Path:
        return self.state_root / f"{evolution_id}.json"

    def _history_path(self, evolution_id: str) -> Path:
        return self.state_root / f"{evolution_id}.history.json"

    def _read_receipt(self, evolution_id: str) -> SkillRuleEvolutionReceipt | None:
        path = self._receipt_path(evolution_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return SkillRuleEvolutionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _read_history(self, evolution_id: str) -> list[SkillRuleEvolutionReceipt]:
        path = self._history_path(evolution_id)
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[SkillRuleEvolutionReceipt] = []
        for item in payload[-32:]:
            try:
                history.append(SkillRuleEvolutionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history

    def _persist(
        self,
        receipt: SkillRuleEvolutionReceipt,
        *,
        append_history: bool,
    ) -> None:
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(self._receipt_path(receipt.evolution_id), payload)
        if not append_history:
            return
        history = [
            item
            for item in self._read_history(receipt.evolution_id)
            if item.receipt_hash != receipt.receipt_hash
        ]
        history.append(receipt)
        history = history[-32:]
        _atomic_write_json(
            self._history_path(receipt.evolution_id),
            [item.model_dump(mode="json", by_alias=True) for item in history],
        )


__all__ = [
    "SkillRuleEvolutionBlockedError",
    "SkillRuleEvolutionConflictError",
    "SkillRuleEvolutionError",
    "SkillRuleEvolutionInvalidError",
    "SkillRuleEvolutionNotFoundError",
    "SkillRuleEvolutionService",
]
