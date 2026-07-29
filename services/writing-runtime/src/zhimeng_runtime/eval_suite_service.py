from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json

from zhimeng_runtime.eval_suite_models import (
    EvalSuiteAction,
    EvalSuiteActionRequest,
    EvalSuiteBlindMemberView,
    EvalSuiteBlindScore,
    EvalSuiteCompareCheck,
    EvalSuiteListItem,
    EvalSuiteListResponse,
    EvalSuiteMemberRecord,
    EvalSuitePrepareRequest,
    EvalSuiteProviderFingerprintMember,
    EvalSuiteProviderFingerprintReport,
    EvalSuiteProviderFingerprintResponse,
    EvalSuiteReceipt,
    EvalSuiteResponse,
    EvalSuiteStatus,
    eval_suite_provider_fingerprint_report_hash,
    eval_suite_receipt_hash,
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




def canonical_member_fingerprint(
    *,
    origin: str,
    source_run_id: str | None,
    requested_models: list[str],
    reported_models: list[str],
    model_identity_statuses: list[str],
    provider_identity_status: str | None,
    turn_count: int,
    evidence_kind: str,
) -> str:
    material = {
        "schema": "zhimeng.eval-suite-member-provider-fingerprint.v1",
        "origin": origin,
        "source_run_id": source_run_id,
        "requested_models": list(requested_models),
        "reported_models": list(reported_models),
        "model_identity_statuses": list(model_identity_statuses),
        "provider_identity_status": provider_identity_status,
        "turn_count": turn_count,
        "evidence_kind": evidence_kind,
    }
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

class EvalSuiteError(RuntimeError):
    code = "eval_suite_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class EvalSuiteNotFoundError(EvalSuiteError):
    code = "eval_suite_not_found"


class EvalSuiteInvalidError(EvalSuiteError):
    code = "eval_suite_invalid"


class EvalSuiteConflictError(EvalSuiteError):
    code = "eval_suite_conflict"


class EvalSuiteBlockedError(EvalSuiteError):
    code = "eval_suite_blocked"


class EvalSuiteService:
    """Cross-project eval corpus suite orchestration and author blind review.

    Local v1 keeps formal_draft_allowed / quality_assessed / training_ready locked
    false. Suites never auto-upgrade Skills or write project truth.
    """

    def __init__(
        self,
        *,
        state_root: Path,
        change_set_service: object | None = None,
        change_set_state_root: Path | None = None,
        agent_store: object | None = None,
    ) -> None:
        self.state_root = Path(state_root)
        self.change_set_service = change_set_service
        self.change_set_state_root = (
            None if change_set_state_root is None else Path(change_set_state_root)
        )
        self.agent_store = agent_store
        self.state_root.mkdir(parents=True, exist_ok=True)



    def get_provider_fingerprint_report(
        self,
        suite_id: str,
    ) -> EvalSuiteProviderFingerprintResponse:
        path = (self.state_root / suite_id) / "provider-fingerprint-report.json"
        if not path.is_file():
            raise EvalSuiteNotFoundError(
                "eval suite provider fingerprint report was not found"
            )
        report = EvalSuiteProviderFingerprintReport.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        return EvalSuiteProviderFingerprintResponse(report=report)

    def build_provider_fingerprint_report(
        self,
        suite_id: str,
    ) -> EvalSuiteProviderFingerprintResponse:
        suite = self._read_receipt(suite_id)
        if suite is None:
            raise EvalSuiteNotFoundError("eval suite was not found")
        if suite.status is EvalSuiteStatus.PREPARED and not suite.frozen:
            raise EvalSuiteInvalidError(
                "provider fingerprint report requires a frozen or post-freeze suite"
            )
        if self.change_set_service is None:
            raise EvalSuiteInvalidError("eval suite requires change set service")

        members = [self._member_provider_fingerprint(item) for item in suite.members]
        present_count = sum(1 for row in members if row.present)
        missing = [row.member_token for row in members if not row.present]
        all_present = not missing
        generated_at = _utcnow()
        detail = (
            "Provider/model fingerprint report built from change-set provenance "
            "and optional turn receipts/sidecars; quality remains unassessed."
            if all_present
            else (
                "Provider/model fingerprint report incomplete: "
                f"{len(missing)} members lack provider identity evidence."
            )
        )
        material_for_id = {
            "suite_id": suite.suite_id,
            "suite_receipt_hash": suite.receipt_hash,
            "members": [
                {
                    "member_token": row.member_token,
                    "fingerprint": row.fingerprint,
                    "evidence_kind": row.evidence_kind,
                    "present": row.present,
                }
                for row in members
            ],
        }
        report_id = "eval-suite-provider-fp-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        temp = EvalSuiteProviderFingerprintReport.model_construct(
            schema_id="zhimeng.eval-suite-provider-fingerprint-report.v1",
            report_id=report_id,
            suite_id=suite.suite_id,
            suite_receipt_hash=suite.receipt_hash,
            status="complete" if all_present else "incomplete",
            members=members,
            member_count=len(members),
            present_count=present_count,
            missing_member_tokens=missing,
            all_present=all_present,
            formal_draft_allowed=False,
            quality_assessed=False,
            training_ready=False,
            policy_version="local-v1-eval-suite-provider-fp-1",
            detail=detail,
            generated_at=generated_at,
            report_hash="0" * 64,
        )
        report_hash = eval_suite_provider_fingerprint_report_hash(temp)
        report = EvalSuiteProviderFingerprintReport(
            schema_id="zhimeng.eval-suite-provider-fingerprint-report.v1",
            report_id=report_id,
            suite_id=suite.suite_id,
            suite_receipt_hash=suite.receipt_hash,
            status="complete" if all_present else "incomplete",
            members=members,
            member_count=len(members),
            present_count=present_count,
            missing_member_tokens=missing,
            all_present=all_present,
            formal_draft_allowed=False,
            quality_assessed=False,
            training_ready=False,
            policy_version="local-v1-eval-suite-provider-fp-1",
            detail=detail,
            generated_at=generated_at,
            report_hash=report_hash,
        )
        _atomic_write_json(
            (self.state_root / suite_id) / "provider-fingerprint-report.json",
            report.model_dump(mode="json", by_alias=True),
        )
        return EvalSuiteProviderFingerprintResponse(report=report)

    def _member_provider_fingerprint(
        self,
        item: EvalSuiteMemberRecord,
    ) -> EvalSuiteProviderFingerprintMember:
        change_set = self.change_set_service.get(item.change_set_id)
        origin = getattr(change_set.origin, "value", change_set.origin)
        source_run_id = change_set.source_run_id

        sidecar = None
        if self.change_set_state_root is not None:
            candidate = (
                self.change_set_state_root
                / item.change_set_id
                / "provider-fingerprint.json"
            )
            if candidate.is_file():
                try:
                    sidecar = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    sidecar = None
        if isinstance(sidecar, dict):
            requested = [
                str(value)
                for value in list(sidecar.get("requested_models") or [])
                if str(value).strip()
            ]
            reported = [
                str(value)
                for value in list(sidecar.get("reported_models") or [])
                if str(value).strip()
            ]
            statuses = [
                str(value)
                for value in list(sidecar.get("model_identity_statuses") or [])
                if str(value).strip()
            ]
            provider_identity = sidecar.get("provider_identity_status")
            provider_identity = (
                str(provider_identity).strip() if provider_identity is not None else None
            ) or None
            turn_count = int(sidecar.get("turn_count") or 0)
            fingerprint = sidecar.get("fingerprint")
            if not (
                isinstance(fingerprint, str)
                and len(fingerprint) == 64
                and all(ch in "0123456789abcdef" for ch in fingerprint)
            ):
                fingerprint = canonical_member_fingerprint(
                    origin=str(origin),
                    source_run_id=source_run_id,
                    requested_models=requested,
                    reported_models=reported,
                    model_identity_statuses=statuses,
                    provider_identity_status=provider_identity,
                    turn_count=turn_count,
                    evidence_kind="artifact_sidecar",
                )
            present = bool(requested or reported or provider_identity or turn_count > 0)
            return EvalSuiteProviderFingerprintMember(
                member_token=item.member_token,
                project_id=item.project_id,
                change_set_id=item.change_set_id,
                origin=str(origin),
                source_run_id=source_run_id,
                present=present,
                fingerprint=fingerprint if present else None,
                requested_models=requested,
                reported_models=reported,
                model_identity_statuses=statuses,
                provider_identity_status=provider_identity,
                turn_count=max(0, min(turn_count, 32)),
                evidence_kind="artifact_sidecar" if present else "missing",
                detail=(
                    "provider fingerprint sidecar present"
                    if present
                    else "provider fingerprint sidecar empty"
                ),
            )

        if source_run_id and self.agent_store is not None:
            try:
                receipt_list = self.agent_store.get_provider_turn_receipt_list(source_run_id)
            except Exception:
                receipt_list = None
            if receipt_list is not None:
                turns = list(getattr(receipt_list, "provider_turn_receipts", ()) or ())
                requested: list[str] = []
                reported: list[str] = []
                statuses: list[str] = []
                for turn in turns:
                    req = getattr(turn, "requested_model", None)
                    rep = getattr(turn, "reported_model", None)
                    status = getattr(turn, "model_identity_status", None)
                    if req:
                        requested.append(str(req))
                    if rep:
                        reported.append(str(rep))
                    if status:
                        statuses.append(
                            status.value if hasattr(status, "value") else str(status)
                        )
                provider_identity = getattr(receipt_list, "provider_identity_status", None)
                if provider_identity is not None and hasattr(provider_identity, "value"):
                    provider_identity = provider_identity.value
                elif provider_identity is not None:
                    provider_identity = str(provider_identity)
                turn_count = len(turns)
                present = turn_count > 0
                fingerprint = (
                    canonical_member_fingerprint(
                        origin=str(origin),
                        source_run_id=source_run_id,
                        requested_models=requested,
                        reported_models=reported,
                        model_identity_statuses=statuses,
                        provider_identity_status=provider_identity,
                        turn_count=turn_count,
                        evidence_kind="provider_turn_receipts",
                    )
                    if present
                    else None
                )
                return EvalSuiteProviderFingerprintMember(
                    member_token=item.member_token,
                    project_id=item.project_id,
                    change_set_id=item.change_set_id,
                    origin=str(origin),
                    source_run_id=source_run_id,
                    present=present,
                    fingerprint=fingerprint,
                    requested_models=requested,
                    reported_models=reported,
                    model_identity_statuses=statuses,
                    provider_identity_status=provider_identity,
                    turn_count=turn_count,
                    evidence_kind="provider_turn_receipts" if present else "missing",
                    detail=(
                        f"provider turn receipts present ({turn_count})"
                        if present
                        else "source_run_id present but no provider turn receipts"
                    ),
                )

        return EvalSuiteProviderFingerprintMember(
            member_token=item.member_token,
            project_id=item.project_id,
            change_set_id=item.change_set_id,
            origin=str(origin),
            source_run_id=source_run_id,
            present=False,
            fingerprint=None,
            requested_models=[],
            reported_models=[],
            model_identity_statuses=[],
            provider_identity_status=None,
            turn_count=0,
            evidence_kind="missing",
            detail=(
                "manual/skill change set has no provider identity evidence"
                if not source_run_id
                else "source_run_id present but provider identity evidence unavailable"
            ),
        )

    def list_suites(self) -> EvalSuiteListResponse:
        items: list[EvalSuiteListItem] = []
        if self.state_root.is_dir():
            for path in sorted(self.state_root.glob("eval-suite-*/receipt.json")):
                try:
                    receipt = EvalSuiteReceipt.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                except Exception:
                    continue
                items.append(
                    EvalSuiteListItem(
                        suite_id=receipt.suite_id,
                        title=receipt.title,
                        status=receipt.status,
                        member_count=receipt.member_count,
                        frozen=receipt.frozen,
                        blind_review_finalized=receipt.blind_review_finalized,
                        formal_draft_allowed=False,
                        quality_assessed=False,
                        updated_at=receipt.updated_at,
                        receipt_hash=receipt.receipt_hash,
                    )
                )
        return EvalSuiteListResponse(suites=items)

    def get(self, suite_id: str) -> EvalSuiteResponse:
        receipt = self._read_receipt(suite_id)
        if receipt is None:
            raise EvalSuiteNotFoundError("eval suite was not found")
        return self._response(receipt, history=self._read_history(suite_id))

    def prepare(self, request: EvalSuitePrepareRequest) -> EvalSuiteResponse:
        if self.change_set_service is None:
            raise EvalSuiteInvalidError("eval suite requires change set service")
        if len(request.members) < 2:
            raise EvalSuiteInvalidError("eval suite requires at least two members")

        seen_keys: set[tuple[str, str]] = set()
        members: list[EvalSuiteMemberRecord] = []
        for item in request.members:
            key = (item.project_id, item.change_set_id)
            if key in seen_keys:
                raise EvalSuiteInvalidError(
                    "eval suite members must be unique by project_id + change_set_id"
                )
            seen_keys.add(key)
            record = self.change_set_service.get(item.change_set_id)
            if record.project_id != item.project_id:
                raise EvalSuiteInvalidError(
                    "eval suite member project_id does not match change set project"
                )
            baseline_response = self.change_set_service.get_eval_corpus_baseline(
                item.change_set_id
            )
            baseline = baseline_response.receipt
            if baseline is None:
                raise EvalSuiteInvalidError(
                    "eval suite member requires a frozen eval corpus baseline"
                )
            if (
                item.expected_baseline_receipt_id is not None
                and item.expected_baseline_receipt_id != baseline.receipt_id
            ):
                raise EvalSuiteConflictError(
                    "eval suite member baseline receipt id does not match"
                )
            if (
                item.expected_baseline_receipt_hash is not None
                and item.expected_baseline_receipt_hash != baseline.receipt_hash
            ):
                raise EvalSuiteConflictError(
                    "eval suite member baseline receipt hash does not match"
                )
            if baseline.formal_draft_allowed is not False:
                raise EvalSuiteBlockedError(
                    "eval suite refuses baselines that unlock formal draft"
                )
            if baseline.quality_assessed is not False:
                raise EvalSuiteBlockedError(
                    "eval suite refuses baselines that claim quality assessed"
                )
            if baseline.training_ready is not False:
                raise EvalSuiteBlockedError(
                    "eval suite refuses baselines that claim training ready"
                )
            member_token = self._member_token(
                project_id=item.project_id,
                change_set_id=item.change_set_id,
                baseline_receipt_id=baseline.receipt_id,
            )
            members.append(
                EvalSuiteMemberRecord(
                    member_token=member_token,
                    project_id=item.project_id,
                    change_set_id=item.change_set_id,
                    baseline_receipt_id=baseline.receipt_id,
                    baseline_receipt_hash=baseline.receipt_hash,
                    export_file_sha256=baseline.source_export_file_sha256,
                    present_artifact_kinds=list(baseline.present_artifact_kinds),
                    label=item.label,
                    reveal_after_finalize=True,
                )
            )

        members = sorted(
            members,
            key=lambda row: (row.project_id, row.change_set_id, row.member_token),
        )
        suite_id = self._suite_id(
            title=request.title,
            summary=request.summary,
            members=members,
        )
        existing = self._read_receipt(suite_id)
        if existing is not None:
            return self._response(existing, history=self._read_history(suite_id))

        receipt = self._build_receipt(
            suite_id=suite_id,
            title=request.title,
            summary=request.summary,
            status=EvalSuiteStatus.PREPARED,
            state_revision=0,
            members=members,
            frozen=False,
            compare_against_suite_id=None,
            compare_status="deferred",
            compare_checks=[],
            blind_review_open=False,
            blind_review_finalized=False,
            blind_scores=[],
            reveal_member_identities=True,
            detail=(
                "Cross-project eval suite prepared from frozen baselines; "
                "formal draft remains locked and no auto-upgrade is claimed."
            ),
        )
        return self._persist(receipt, history=[])

    def act(self, suite_id: str, request: EvalSuiteActionRequest) -> EvalSuiteResponse:
        current = self._read_receipt(suite_id)
        if current is None:
            raise EvalSuiteNotFoundError("eval suite was not found")
        if (
            request.expected_status is not None
            and request.expected_status != current.status
        ):
            raise EvalSuiteConflictError("eval suite status does not match request")
        if (
            request.expected_state_revision is not None
            and request.expected_state_revision != current.state_revision
        ):
            raise EvalSuiteConflictError(
                "eval suite state revision does not match request"
            )

        if request.action is EvalSuiteAction.FREEZE:
            receipt = self._freeze(current)
        elif request.action is EvalSuiteAction.COMPARE:
            receipt = self._compare(current, request.compare_against_suite_id)
        elif request.action is EvalSuiteAction.BLIND_REVIEW_OPEN:
            receipt = self._blind_review_open(current)
        elif request.action is EvalSuiteAction.BLIND_REVIEW_SUBMIT:
            receipt = self._blind_review_submit(current, request)
        elif request.action is EvalSuiteAction.BLIND_REVIEW_FINALIZE:
            receipt = self._blind_review_finalize(current)
        else:
            raise EvalSuiteInvalidError(f"unsupported eval suite action: {request.action}")
        history = self._read_history(suite_id)
        return self._persist(receipt, history=history)

    def _freeze(self, current: EvalSuiteReceipt) -> EvalSuiteReceipt:
        if current.status not in {
            EvalSuiteStatus.PREPARED,
            EvalSuiteStatus.FROZEN,
        }:
            raise EvalSuiteInvalidError(
                "eval suite freeze requires prepared or already frozen status"
            )
        if current.status is EvalSuiteStatus.FROZEN and current.frozen:
            return current
        return self._build_receipt(
            suite_id=current.suite_id,
            title=current.title,
            summary=current.summary,
            status=EvalSuiteStatus.FROZEN,
            state_revision=current.state_revision + 1,
            members=list(current.members),
            frozen=True,
            compare_against_suite_id=None,
            compare_status="deferred",
            compare_checks=[],
            blind_review_open=False,
            blind_review_finalized=False,
            blind_scores=[],
            reveal_member_identities=True,
            detail=(
                "Eval suite frozen as a versioned cross-project orchestration package; "
                "author blind review and compare remain optional next steps."
            ),
        )

    def _compare(
        self,
        current: EvalSuiteReceipt,
        compare_against_suite_id: str | None,
    ) -> EvalSuiteReceipt:
        allowed_statuses = {
            EvalSuiteStatus.FROZEN,
            EvalSuiteStatus.COMPARE_READY,
            EvalSuiteStatus.COMPARE_DEGRADED,
            EvalSuiteStatus.BLIND_REVIEW_OPEN,
            EvalSuiteStatus.BLIND_REVIEW_FINALIZED,
        }
        if not (current.frozen or current.status in allowed_statuses):
            raise EvalSuiteInvalidError("eval suite compare requires a frozen suite")
        if compare_against_suite_id is None:
            raise EvalSuiteInvalidError("compare_against_suite_id is required")
        if compare_against_suite_id == current.suite_id:
            raise EvalSuiteInvalidError("eval suite cannot compare against itself")
        prior = self._read_receipt(compare_against_suite_id)
        if prior is None:
            raise EvalSuiteNotFoundError("compare against eval suite was not found")
        if not prior.frozen:
            raise EvalSuiteInvalidError("compare against suite must be frozen")

        current_map = {
            (item.project_id, item.change_set_id): item for item in current.members
        }
        prior_map = {
            (item.project_id, item.change_set_id): item for item in prior.members
        }
        missing = sorted(set(prior_map) - set(current_map))
        changed = []
        for key, prior_member in prior_map.items():
            current_member = current_map.get(key)
            if current_member is None:
                continue
            if (
                current_member.baseline_receipt_hash != prior_member.baseline_receipt_hash
                or current_member.export_file_sha256 != prior_member.export_file_sha256
            ):
                changed.append(key)

        checks = [
            EvalSuiteCompareCheck(
                code="prior_suite_frozen",
                present=True,
                detail="compare against suite is frozen",
            ),
            EvalSuiteCompareCheck(
                code="current_suite_frozen",
                present=True,
                detail="current suite freeze recorded",
            ),
            EvalSuiteCompareCheck(
                code="prior_members_present",
                present=not missing,
                detail=(
                    "all prior suite members present"
                    if not missing
                    else f"missing {len(missing)} prior members"
                ),
            ),
            EvalSuiteCompareCheck(
                code="baseline_fingerprints_stable",
                present=not changed,
                detail=(
                    "member baseline fingerprints unchanged"
                    if not changed
                    else f"{len(changed)} members changed baseline fingerprints"
                ),
            ),
        ]
        non_degraded = all(item.present for item in checks)
        status = (
            EvalSuiteStatus.COMPARE_READY
            if non_degraded
            else EvalSuiteStatus.COMPARE_DEGRADED
        )
        return self._build_receipt(
            suite_id=current.suite_id,
            title=current.title,
            summary=current.summary,
            status=status,
            state_revision=current.state_revision + 1,
            members=list(current.members),
            frozen=True,
            compare_against_suite_id=compare_against_suite_id,
            compare_status="non_degraded" if non_degraded else "degraded",
            compare_checks=checks,
            blind_review_open=current.blind_review_open,
            blind_review_finalized=current.blind_review_finalized,
            blind_scores=list(current.blind_scores),
            reveal_member_identities=(
                False if current.blind_review_open and not current.blind_review_finalized
                else True
            ),
            detail=(
                "Cross-project suite compare non_degraded against frozen prior suite."
                if non_degraded
                else "Cross-project suite compare degraded; author must inspect missing or changed members."
            ),
        )

    def _blind_review_open(self, current: EvalSuiteReceipt) -> EvalSuiteReceipt:
        allowed = {
            EvalSuiteStatus.FROZEN,
            EvalSuiteStatus.COMPARE_READY,
            EvalSuiteStatus.COMPARE_DEGRADED,
            EvalSuiteStatus.BLIND_REVIEW_OPEN,
        }
        if not (current.frozen or current.status in allowed):
            raise EvalSuiteInvalidError(
                "blind review open requires a frozen or compared suite"
            )
        if current.blind_review_finalized:
            raise EvalSuiteInvalidError("blind review already finalized")
        if current.status is EvalSuiteStatus.BLIND_REVIEW_OPEN and current.blind_review_open:
            return current
        return self._build_receipt(
            suite_id=current.suite_id,
            title=current.title,
            summary=current.summary,
            status=EvalSuiteStatus.BLIND_REVIEW_OPEN,
            state_revision=current.state_revision + 1,
            members=list(current.members),
            frozen=True,
            compare_against_suite_id=current.compare_against_suite_id,
            compare_status=current.compare_status,
            compare_checks=list(current.compare_checks),
            blind_review_open=True,
            blind_review_finalized=False,
            blind_scores=[],
            reveal_member_identities=False,
            detail=(
                "Author blind review opened with anonymized member tokens; "
                "project identities stay hidden until finalize."
            ),
        )

    def _blind_review_submit(
        self,
        current: EvalSuiteReceipt,
        request: EvalSuiteActionRequest,
    ) -> EvalSuiteReceipt:
        if current.status is not EvalSuiteStatus.BLIND_REVIEW_OPEN or not current.blind_review_open:
            raise EvalSuiteInvalidError("blind review submit requires open blind review")
        if current.blind_review_finalized:
            raise EvalSuiteInvalidError("blind review already finalized")
        if not request.blind_review_items:
            raise EvalSuiteInvalidError("blind review submit requires at least one score item")
        allowed = {item.member_token for item in current.members}
        seen: set[str] = set()
        scores: list[EvalSuiteBlindScore] = []
        for item in request.blind_review_items:
            if item.member_token not in allowed:
                raise EvalSuiteInvalidError(
                    f"unknown blind review member token: {item.member_token}"
                )
            if item.member_token in seen:
                raise EvalSuiteInvalidError("duplicate blind review member token")
            seen.add(item.member_token)
            scores.append(
                EvalSuiteBlindScore(
                    member_token=item.member_token,
                    score=item.score,
                    notes=item.notes,
                )
            )
        scores = sorted(scores, key=lambda row: row.member_token)
        return self._build_receipt(
            suite_id=current.suite_id,
            title=current.title,
            summary=current.summary,
            status=EvalSuiteStatus.BLIND_REVIEW_OPEN,
            state_revision=current.state_revision + 1,
            members=list(current.members),
            frozen=True,
            compare_against_suite_id=current.compare_against_suite_id,
            compare_status=current.compare_status,
            compare_checks=list(current.compare_checks),
            blind_review_open=True,
            blind_review_finalized=False,
            blind_scores=scores,
            reveal_member_identities=False,
            detail=(
                f"Author submitted {len(scores)} blind scores; "
                "identities remain hidden until finalize."
            ),
        )

    def _blind_review_finalize(self, current: EvalSuiteReceipt) -> EvalSuiteReceipt:
        if current.status is not EvalSuiteStatus.BLIND_REVIEW_OPEN or not current.blind_review_open:
            raise EvalSuiteInvalidError("blind review finalize requires open blind review")
        required = {item.member_token for item in current.members}
        scored = {item.member_token for item in current.blind_scores}
        if scored != required:
            raise EvalSuiteInvalidError(
                "blind review finalize requires scores for every suite member"
            )
        return self._build_receipt(
            suite_id=current.suite_id,
            title=current.title,
            summary=current.summary,
            status=EvalSuiteStatus.BLIND_REVIEW_FINALIZED,
            state_revision=current.state_revision + 1,
            members=list(current.members),
            frozen=True,
            compare_against_suite_id=current.compare_against_suite_id,
            compare_status=current.compare_status,
            compare_checks=list(current.compare_checks),
            blind_review_open=False,
            blind_review_finalized=True,
            blind_scores=list(current.blind_scores),
            reveal_member_identities=True,
            detail=(
                "Author blind review finalized; member identities revealed for audit only. "
                "formal_draft_allowed and quality_assessed remain false."
            ),
        )

    def _response(
        self,
        receipt: EvalSuiteReceipt,
        *,
        history: list[EvalSuiteReceipt],
    ) -> EvalSuiteResponse:
        blind_views: list[EvalSuiteBlindMemberView] = []
        if receipt.blind_review_open and not receipt.blind_review_finalized:
            ordered = sorted(receipt.members, key=lambda item: item.member_token)
            for index, item in enumerate(ordered, start=1):
                blind_views.append(
                    EvalSuiteBlindMemberView(
                        member_token=item.member_token,
                        ordinal=index,
                        present_artifact_kinds=list(item.present_artifact_kinds),
                    )
                )
        return EvalSuiteResponse(
            receipt=receipt,
            history=history,
            blind_member_views=blind_views,
        )

    def _persist(
        self,
        receipt: EvalSuiteReceipt,
        *,
        history: list[EvalSuiteReceipt],
    ) -> EvalSuiteResponse:
        payload = receipt.model_dump(mode="json", by_alias=True)
        _atomic_write_json(self._receipt_path(receipt.suite_id), payload)
        next_history = [
            item for item in history if item.receipt_hash != receipt.receipt_hash
        ]
        next_history.append(receipt)
        next_history = next_history[-32:]
        _atomic_write_json(
            self._history_path(receipt.suite_id),
            [item.model_dump(mode="json", by_alias=True) for item in next_history],
        )
        return self._response(receipt, history=next_history)

    def _build_receipt(
        self,
        *,
        suite_id: str,
        title: str,
        summary: str,
        status: EvalSuiteStatus,
        state_revision: int,
        members: list[EvalSuiteMemberRecord],
        frozen: bool,
        compare_against_suite_id: str | None,
        compare_status: str,
        compare_checks: list[EvalSuiteCompareCheck],
        blind_review_open: bool,
        blind_review_finalized: bool,
        blind_scores: list[EvalSuiteBlindScore],
        reveal_member_identities: bool,
        detail: str,
    ) -> EvalSuiteReceipt:
        updated_at = _utcnow()
        provisional = EvalSuiteReceipt.model_construct(
            schema_id="zhimeng.eval-suite-receipt.v1",
            suite_id=suite_id,
            title=title,
            summary=summary,
            status=status,
            state_revision=state_revision,
            members=members,
            member_count=len(members),
            frozen=frozen,
            compare_against_suite_id=compare_against_suite_id,
            compare_status=compare_status,
            compare_checks=compare_checks,
            blind_review_open=blind_review_open,
            blind_review_finalized=blind_review_finalized,
            blind_scores=blind_scores,
            reveal_member_identities=reveal_member_identities,
            formal_draft_allowed=False,
            quality_assessed=False,
            training_ready=False,
            auto_upgrade=False,
            policy_version="local-v1-eval-suite-1",
            detail=detail,
            updated_at=updated_at,
            receipt_hash="0" * 64,
        )
        receipt_hash = eval_suite_receipt_hash(provisional)
        return EvalSuiteReceipt(
            suite_id=suite_id,
            title=title,
            summary=summary,
            status=status,
            state_revision=state_revision,
            members=members,
            member_count=len(members),
            frozen=frozen,
            compare_against_suite_id=compare_against_suite_id,
            compare_status=compare_status,
            compare_checks=compare_checks,
            blind_review_open=blind_review_open,
            blind_review_finalized=blind_review_finalized,
            blind_scores=blind_scores,
            reveal_member_identities=reveal_member_identities,
            formal_draft_allowed=False,
            quality_assessed=False,
            training_ready=False,
            auto_upgrade=False,
            policy_version="local-v1-eval-suite-1",
            detail=detail,
            updated_at=updated_at,
            receipt_hash=receipt_hash,
        )

    def _suite_id(
        self,
        *,
        title: str,
        summary: str,
        members: list[EvalSuiteMemberRecord],
    ) -> str:
        material = {
            "schema": "zhimeng.eval-suite-id.v1",
            "title": title,
            "summary": summary,
            "members": [
                {
                    "project_id": item.project_id,
                    "change_set_id": item.change_set_id,
                    "baseline_receipt_id": item.baseline_receipt_id,
                    "baseline_receipt_hash": item.baseline_receipt_hash,
                    "export_file_sha256": item.export_file_sha256,
                }
                for item in members
            ],
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return f"eval-suite-{digest[:32]}"

    def _member_token(
        self,
        *,
        project_id: str,
        change_set_id: str,
        baseline_receipt_id: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{project_id}:{change_set_id}:{baseline_receipt_id}".encode("utf-8")
        ).hexdigest()
        return f"eval-member-{digest[:24]}"

    def _receipt_path(self, suite_id: str) -> Path:
        return self.state_root / suite_id / "receipt.json"

    def _history_path(self, suite_id: str) -> Path:
        return self.state_root / suite_id / "history.json"

    def _read_receipt(self, suite_id: str) -> EvalSuiteReceipt | None:
        path = self._receipt_path(suite_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return EvalSuiteReceipt.model_validate(payload)

    def _read_history(self, suite_id: str) -> list[EvalSuiteReceipt]:
        path = self._history_path(suite_id)
        if not path.is_file():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            return []
        return [EvalSuiteReceipt.model_validate(item) for item in payload]


__all__ = [
    "EvalSuiteBlockedError",
    "EvalSuiteConflictError",
    "EvalSuiteError",
    "EvalSuiteInvalidError",
    "EvalSuiteNotFoundError",
    "EvalSuiteService",
]
