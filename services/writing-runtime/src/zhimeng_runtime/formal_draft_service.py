from __future__ import annotations

from pathlib import Path
import json
import hashlib
import re
from datetime import UTC, datetime

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.formal_draft_models import (
    FormalDraftAuthorizationReceipt,
    FormalDraftAuthorizationResponse,
    FormalDraftAuthorDecisionReceipt,
    FormalDraftAuthorDecisionRequest,
    FormalDraftAuthorDecisionResponse,
    FormalDraftAuthorIntent,
    FormalDraftDecision,
    FormalDraftDenial,
    FormalDraftDenialCode,
    FormalDraftEvaluateRequest,
    FormalDraftPrerequisite,
    FormalDraftPrerequisiteCode,
    formal_draft_authorization_receipt_hash,
    formal_draft_author_decision_receipt_hash,
)
from zhimeng_runtime.four_skills_continuous_models import (
    FourSkillsContinuousEvaluateRequest,
    FourSkillsContinuousStatus,
)
from zhimeng_runtime.four_skills_continuous_service import (
    FourSkillsContinuousService,
)
from zhimeng_runtime.formal_draft_continuous_o2_models import (
    FormalDraftContinuousO2Check,
    FormalDraftContinuousO2CheckCode,
    FormalDraftContinuousO2EvaluateRequest,
    FormalDraftContinuousO2Receipt,
    FormalDraftContinuousO2Response,
    FormalDraftContinuousO2Status,
    formal_draft_continuous_o2_receipt_hash,
)
from zhimeng_runtime.four_skills_raw_adaptation_models import (
    FourSkillsInstructionChainHandoffReceipt,
    ProjectInstructionChainHandoffBindRequest,
    ProjectInstructionChainHandoffBindingReceipt,
    ProjectInstructionChainHandoffBindingResponse,
    project_instruction_chain_handoff_binding_receipt_hash,
)
from zhimeng_runtime.formal_draft_provenance_models import (
    FormalDraftProvenanceCheck,
    FormalDraftProvenanceCheckCode,
    FormalDraftProvenanceEvaluateRequest,
    FormalDraftProvenanceReceipt,
    FormalDraftProvenanceResponse,
    FormalDraftProvenanceStatus,
    formal_draft_provenance_receipt_hash,
)


PROJECT_ID_PATTERN = re.compile(r"^project-[0-9a-f]{24}$")


class FormalDraftAuthorizationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: list[dict[str, object]] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = details or []
        self.retryable = retryable


class FormalDraftAuthorizationNotFound(FormalDraftAuthorizationError):
    pass


class FormalDraftAuthorizationInvalid(FormalDraftAuthorizationError):
    pass


class FormalDraftAuthorizationConflict(FormalDraftAuthorizationError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class FormalDraftAuthorizationService:
    """Evaluate formal-draft authorization.

    Local v1 keeps formal_draft_allowed locked false. This service makes the
    denial auditable instead of scattering silent hardcoded flags only.
    """

    def __init__(
        self,
        database: SqliteStore,
        *,
        change_set_state_root: Path | None = None,
        runtime_state_root: Path | None = None,
    ) -> None:
        self.database = database
        self.runtime_state_root = (
            Path(runtime_state_root)
            if runtime_state_root is not None
            else Path(database.path).parent
        )
        self.change_set_state_root = (
            Path(change_set_state_root)
            if change_set_state_root is not None
            else self.runtime_state_root / "change-sets"
        )
        self.author_decision_root = self.runtime_state_root / "formal-draft-authorization"
        self.adjacent_chain_root = self.runtime_state_root / "adjacent-chains"
        self.managed_skill_chain_root = self.runtime_state_root / "managed-skill-chains"
        self.four_skills_raw_root = self.runtime_state_root / "four-skills-raw-adaptations"
        self.instruction_chain_handoff_binding_root = (
            self.runtime_state_root / "instruction-chain-handoff-bindings"
        )

    def evaluate(
        self,
        project_id: str,
        request: FormalDraftEvaluateRequest | None = None,
    ) -> FormalDraftAuthorizationResponse:
        request = request or FormalDraftEvaluateRequest()
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")

        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")

        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != binding.revision
        ):
            raise FormalDraftAuthorizationConflict(
                "project revision does not match the authorization evaluation request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != (binding.fingerprint or "")
        ):
            raise FormalDraftAuthorizationConflict(
                "project fingerprint does not match the authorization evaluation request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        literary_receipt = self._load_literary_quality_receipt(request.change_set_id)
        hard_fact_receipt = self._load_hard_fact_receipt(request.change_set_id)
        pipeline_receipt = self._load_gate_pipeline_receipt(request.change_set_id)

        # Prefer ordered gate-pipeline hard-fact status when a pipeline receipt exists.
        if pipeline_receipt is not None:
            hard_step = next(
                (
                    item
                    for item in pipeline_receipt.steps
                    if getattr(item.code, "value", item.code) == "hard_fact_gate"
                ),
                None,
            )
            hard_status = (
                hard_step.status
                if hard_step is not None
                else getattr(pipeline_receipt.status, "value", str(pipeline_receipt.status))
            )
            pipeline_status = getattr(
                pipeline_receipt.status, "value", str(pipeline_receipt.status)
            )
            if (
                pipeline_status == "complete"
                and pipeline_receipt.authorization_input_ready
                and hard_status == "passed"
            ):
                hard_fact_satisfied = True
                hard_fact_detail = (
                    "ordered gate pipeline hard-fact step passed as an authorization input "
                    f"(pipeline {pipeline_receipt.receipt_id}); "
                    "it still does not unlock formal draft"
                )
            elif pipeline_status == "blocked" or hard_status in {"failed", "missing_inputs"}:
                hard_fact_satisfied = False
                hard_fact_detail = (
                    "ordered gate pipeline hard-fact step did not pass as an authorization input "
                    f"(pipeline {pipeline_receipt.receipt_id}; hard-fact status={hard_status})"
                )
            else:
                hard_fact_satisfied = False
                hard_fact_detail = (
                    "ordered gate pipeline hard-fact step is incomplete as an authorization input "
                    f"(pipeline {pipeline_receipt.receipt_id}; hard-fact status={hard_status})"
                )
        elif hard_fact_receipt is None:
            hard_fact_satisfied = False
            hard_fact_detail = (
                "hard-fact Gate receipt is missing for this evaluation scope"
            )
        elif getattr(hard_fact_receipt.status, "value", hard_fact_receipt.status) == "passed":
            hard_fact_satisfied = True
            hard_fact_detail = (
                "hard-fact Gate structural pass is ready as an authorization input; "
                "it still does not unlock formal draft"
            )
        elif getattr(hard_fact_receipt.status, "value", hard_fact_receipt.status) == "failed":
            hard_fact_satisfied = False
            hard_fact_detail = (
                "hard-fact Gate failed structural checks and cannot pass authorization input"
            )
        else:
            hard_fact_satisfied = False
            hard_fact_detail = (
                "hard-fact Gate is missing required candidate inputs"
            )

        if pipeline_receipt is None:
            pipeline_satisfied = False
            pipeline_enforced = False
            pipeline_detail = (
                "ordered gate pipeline receipt is not present; "
                "completion is deferred until the pipeline is evaluated"
            )
        else:
            pipeline_enforced = True
            pipeline_status = getattr(
                pipeline_receipt.status, "value", str(pipeline_receipt.status)
            )
            if pipeline_status == "complete" and pipeline_receipt.authorization_input_ready:
                pipeline_satisfied = True
                pipeline_detail = (
                    "ordered gate pipeline is complete as an authorization input "
                    f"(receipt {pipeline_receipt.receipt_id}); "
                    "literary quality remains advisory and formal draft stays locked"
                )
            else:
                pipeline_satisfied = False
                pipeline_detail = (
                    "ordered gate pipeline is not complete as an authorization input "
                    f"(receipt {pipeline_receipt.receipt_id}; status={pipeline_status})"
                )

        author_decision = self._load_author_decision(project_id)
        if author_decision is None:
            author_satisfied = False
            author_detail = "no author formal-draft authorization artifact exists"
        elif author_decision.intent == FormalDraftAuthorIntent.REQUEST_AUTHORIZATION:
            author_satisfied = True
            author_detail = (
                "author requested formal-draft authorization; Local v1 policy still denies grant"
            )
        elif author_decision.intent == FormalDraftAuthorIntent.WITHHOLD_AUTHORIZATION:
            author_satisfied = False
            author_detail = "author explicitly withheld formal-draft authorization"
        else:
            author_satisfied = False
            author_detail = "author revoked formal-draft authorization request"

        provenance_receipt = None
        if request.change_set_id:
            try:
                provenance_receipt = self.evaluate_provenance(
                    project_id,
                    FormalDraftProvenanceEvaluateRequest(
                        change_set_id=request.change_set_id,
                    ),
                ).receipt
            except FormalDraftAuthorizationError:
                provenance_receipt = None
        if provenance_receipt is None:
            provenance_satisfied = False
            provenance_detail = (
                "end-to-end formal-draft provenance contract remains unproven"
                if not request.change_set_id
                else "formal-draft provenance evaluation is unavailable for this change set"
            )
        elif provenance_receipt.authorization_input_ready:
            provenance_satisfied = True
            provenance_detail = (
                "formal-draft provenance chain is complete as an authorization input; "
                "it still does not unlock formal draft"
            )
        else:
            provenance_satisfied = False
            missing = ", ".join(
                item.value if hasattr(item, "value") else str(item)
                for item in provenance_receipt.missing_required
            ) or "required checks incomplete"
            provenance_detail = (
                f"formal-draft provenance incomplete: {missing}"
            )

        continuous_o2_receipt = None
        try:
            continuous_o2_receipt = self.evaluate_continuous_o2(
                project_id,
                FormalDraftContinuousO2EvaluateRequest(
                    expected_project_revision=binding.revision,
                    expected_project_fingerprint=binding.fingerprint or None,
                ),
            ).receipt
        except FormalDraftAuthorizationError:
            continuous_o2_receipt = None
        if continuous_o2_receipt is None:
            continuous_o2_satisfied = False
            continuous_o2_detail = (
                "generic four-skills continuous O2 chain is not complete"
            )
        elif continuous_o2_receipt.authorization_input_ready:
            continuous_o2_satisfied = True
            continuous_o2_detail = (
                "continuous O2 readiness is complete as an authorization input; "
                "it still does not unlock formal draft and does not claim "
                "four_skills_generic_exactly_once"
            )
        else:
            continuous_o2_satisfied = False
            missing = ", ".join(
                item.value if hasattr(item, "value") else str(item)
                for item in continuous_o2_receipt.missing_required
            ) or "required continuous O2 checks incomplete"
            continuous_o2_detail = (
                f"continuous O2 readiness incomplete: {missing}"
            )

        prerequisites = [
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.PROJECT_BOUND,
                satisfied=True,
                detail=f"project bound at revision {binding.revision}",
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.FOUR_SKILLS_CONTINUOUS_O2,
                satisfied=continuous_o2_satisfied,
                detail=continuous_o2_detail,
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.LITERARY_QUALITY_GATE,
                satisfied=False,
                detail=(
                    "literary quality Gate receipt exists but does not unlock formal draft"
                    if literary_receipt is not None
                    else "literary quality Gate receipt is missing for this evaluation scope"
                ),
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.HARD_FACT_GATE_PASS,
                satisfied=hard_fact_satisfied,
                detail=hard_fact_detail,
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.GATE_PIPELINE_COMPLETE,
                satisfied=pipeline_satisfied,
                detail=pipeline_detail,
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.AUTHOR_EXPLICIT_AUTHORIZATION,
                satisfied=author_satisfied,
                detail=author_detail,
            ),
            FormalDraftPrerequisite(
                code=FormalDraftPrerequisiteCode.PROVENANCE_COMPLETE,
                satisfied=provenance_satisfied,
                detail=provenance_detail,
            ),
        ]
        denials = [
            FormalDraftDenial(
                code=FormalDraftDenialCode.LOCAL_V1_POLICY_LOCKED,
                message="Local v1 keeps formal_draft_allowed locked false until a proven authorization contract exists",
            ),
            FormalDraftDenial(
                code=FormalDraftDenialCode.AUTHORIZATION_CONTRACT_UNPROVEN,
                message="formal draft authorization contract is present as evaluation only and cannot grant write rights",
            ),
        ]
        if not continuous_o2_satisfied:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.FOUR_SKILLS_CHAIN_INCOMPLETE,
                    message=(
                        "four-skills continuous execution is incomplete"
                        if continuous_o2_receipt is None
                        else (
                            "continuous O2 readiness is not ready as an authorization input "
                            f"(receipt {continuous_o2_receipt.receipt_id})"
                        )
                    ),
                )
            )
        if literary_receipt is None:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.LITERARY_QUALITY_GATE_MISSING,
                    message="literary quality Gate is not available as a formal-draft unlock",
                )
            )
        else:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.LITERARY_QUALITY_GATE_MISSING,
                    message=(
                        "literary quality Gate is advisory only and cannot unlock formal draft "
                        f"(receipt {literary_receipt.receipt_id})"
                    ),
                )
            )
        if not hard_fact_satisfied:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.HARD_FACT_GATE_INCOMPLETE,
                    message=(
                        "hard-fact Gate is not a passing authorization input"
                        if hard_fact_receipt is None and pipeline_receipt is None
                        else (
                            "ordered gate pipeline hard-fact step did not pass as an authorization input "
                            f"(receipt {pipeline_receipt.receipt_id})"
                            if pipeline_receipt is not None
                            else (
                                "hard-fact Gate did not pass as an authorization input "
                                f"(receipt {hard_fact_receipt.receipt_id})"
                            )
                        )
                    ),
                )
            )
        if pipeline_enforced and not pipeline_satisfied:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.GATE_PIPELINE_INCOMPLETE,
                    message=(
                        "ordered gate pipeline is not complete as an authorization input "
                        f"(receipt {pipeline_receipt.receipt_id})"
                    ),
                )
            )
        if not author_satisfied:
            if author_decision is None:
                author_message = "author has not issued a formal-draft authorization decision"
            elif author_decision.intent == FormalDraftAuthorIntent.WITHHOLD_AUTHORIZATION:
                author_message = "author withheld formal-draft authorization"
            else:
                author_message = "author revoked formal-draft authorization request"
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.AUTHOR_AUTHORIZATION_MISSING,
                    message=author_message,
                )
            )
        if not provenance_satisfied:
            denials.append(
                FormalDraftDenial(
                    code=FormalDraftDenialCode.PROVENANCE_INCOMPLETE,
                    message=(
                        "formal-draft provenance contract is incomplete"
                        if provenance_receipt is None
                        else (
                            "formal-draft provenance is not ready as an authorization input "
                            f"(receipt {provenance_receipt.receipt_id})"
                        )
                    ),
                )
            )

        evaluated_at = _utcnow()
        receipt_id = _digest_id(
            "formal-draft-auth-",
            project_id,
            binding.revision,
            binding.fingerprint or "",
            request.context_pack_id or "",
            request.run_id or "",
            request.change_set_id or "",
            "local-v1-locked-1",
            evaluated_at.isoformat(),
        )
        provisional = FormalDraftAuthorizationReceipt.model_construct(
            schema_id="zhimeng.formal-draft-authorization-receipt.v1",
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            context_pack_id=request.context_pack_id,
            run_id=request.run_id,
            change_set_id=request.change_set_id,
            decision=FormalDraftDecision.DENIED,
            formal_draft_allowed=False,
            writes_project_truth=False,
            prerequisites=prerequisites,
            denials=denials,
            policy_version="local-v1-locked-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt_hash = formal_draft_authorization_receipt_hash(provisional)
        receipt = FormalDraftAuthorizationReceipt(
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            context_pack_id=request.context_pack_id,
            run_id=request.run_id,
            change_set_id=request.change_set_id,
            decision=FormalDraftDecision.DENIED,
            formal_draft_allowed=False,
            writes_project_truth=False,
            prerequisites=prerequisites,
            denials=denials,
            policy_version="local-v1-locked-1",
            evaluated_at=evaluated_at,
            receipt_hash=receipt_hash,
        )
        return FormalDraftAuthorizationResponse(receipt=receipt)



    def evaluate_continuous_o2(
        self,
        project_id: str,
        request: FormalDraftContinuousO2EvaluateRequest | None = None,
    ) -> FormalDraftContinuousO2Response:
        request = request or FormalDraftContinuousO2EvaluateRequest()
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")

        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != binding.revision
        ):
            raise FormalDraftAuthorizationConflict(
                "project revision does not match the continuous O2 evaluation request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != (binding.fingerprint or "")
        ):
            raise FormalDraftAuthorizationConflict(
                "project fingerprint does not match the continuous O2 evaluation request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        adjacent = self._load_adjacent_chain_evidence(
            project_id,
            preferred_chain_id=request.adjacent_chain_id,
        )
        managed = self._load_managed_skill_chain_evidence(
            project_id,
            preferred_chain_id=request.managed_skill_chain_id,
            require_creation_suite=False,
        )
        creation = self._load_managed_skill_chain_evidence(
            project_id,
            preferred_chain_id=request.managed_skill_chain_id,
            require_creation_suite=True,
        )

        instruction_handoff = self._load_instruction_chain_handoff_binding(project_id)

        four_skills = FourSkillsContinuousService(
            self.database,
            runtime_state_root=self.runtime_state_root,
        ).evaluate(
            project_id,
            FourSkillsContinuousEvaluateRequest(
                expected_project_revision=binding.revision,
                expected_project_fingerprint=binding.fingerprint or None,
                source_import_id=request.source_import_id,
                adjacent_chain_id=request.adjacent_chain_id,
                managed_skill_chain_id=request.managed_skill_chain_id,
            ),
        ).receipt
        four_skills_ready = (
            four_skills.status is FourSkillsContinuousStatus.COMPLETE
            and four_skills.authorization_input_ready
            and four_skills.four_skills_generic_exactly_once is False
            and four_skills.formal_draft_allowed is False
        )

        checks = [
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.PROJECT_BOUND,
                required=True,
                present=True,
                detail=f"project bound at revision {binding.revision}",
                evidence_id=project_id,
            ),
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.FOUR_SKILLS_CONTINUOUS_LINKED,
                required=True,
                present=four_skills_ready,
                detail=(
                    (
                        "linked four-skills continuous evidence is complete as an "
                        f"authorization input (receipt {four_skills.receipt_id}); "
                        "still does not claim four_skills_generic_exactly_once"
                    )
                    if four_skills_ready
                    else (
                        "linked four-skills continuous evidence is incomplete: "
                        + (
                            ",".join(
                                item.value if hasattr(item, "value") else str(item)
                                for item in four_skills.missing_required
                            )
                            or "required stages missing or unlinked"
                        )
                    )
                ),
                evidence_id=four_skills.receipt_id if four_skills_ready else None,
            ),
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.ADJACENT_O2_CHAIN_SUCCEEDED,
                required=True,
                present=adjacent is not None,
                detail=(
                    (
                        "adjacent O2-D->O2-E chain succeeded "
                        f"(chain {adjacent['chain_id']})"
                    )
                    if adjacent is not None
                    else "adjacent O2-D->O2-E continuous chain has not succeeded"
                ),
                evidence_id=None if adjacent is None else str(adjacent["chain_id"]),
            ),
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.MANAGED_SKILL_CHAIN_SUCCEEDED,
                required=True,
                present=managed is not None,
                detail=(
                    (
                        f"managed skill chain succeeded with {managed['step_count']} steps "
                        f"(chain {managed['chain_id']})"
                    )
                    if managed is not None
                    else "no succeeded multi-step managed skill chain evidence"
                ),
                evidence_id=None if managed is None else str(managed["chain_id"]),
            ),
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.CREATION_SUITE_CONTINUOUS_EVIDENCE,
                required=True,
                present=creation is not None,
                detail=(
                    (
                        "creation-suite continuous managed chain succeeded "
                        f"(chain {creation['chain_id']}; "
                        f"skills={','.join(creation['skill_ids'])})"
                    )
                    if creation is not None
                    else (
                        "no succeeded multi-step creation-suite continuous chain evidence"
                    )
                ),
                evidence_id=None if creation is None else str(creation["chain_id"]),
            ),
            FormalDraftContinuousO2Check(
                code=FormalDraftContinuousO2CheckCode.INSTRUCTION_CHAIN_HANDOFF_BOUND,
                required=True,
                present=instruction_handoff is not None,
                detail=(
                    (
                        "project-bound instruction chain handoff is ready "
                        f"(binding {instruction_handoff['binding_id']}; "
                        f"handoff {instruction_handoff['handoff_id']}); "
                        "still does not claim managed/raw execution or generic exactly-once"
                    )
                    if instruction_handoff is not None
                    else (
                        "no project-bound instruction chain handoff ready as authorization input"
                    )
                ),
                evidence_id=(
                    None
                    if instruction_handoff is None
                    else str(instruction_handoff["binding_id"])
                ),
            ),
        ]
        missing_required = [
            item.code for item in checks if item.required and not item.present
        ]
        status = (
            FormalDraftContinuousO2Status.COMPLETE
            if not missing_required
            else FormalDraftContinuousO2Status.INCOMPLETE
        )
        evaluated_at = _utcnow()
        receipt_id = _digest_id(
            "formal-draft-continuous-o2-",
            project_id,
            binding.revision,
            binding.fingerprint or "",
            four_skills.receipt_id,
            adjacent["chain_id"] if adjacent else "",
            managed["chain_id"] if managed else "",
            creation["chain_id"] if creation else "",
            instruction_handoff["binding_id"] if instruction_handoff else "",
            status.value,
            ",".join(item.value for item in missing_required),
            evaluated_at.isoformat(),
        )
        kwargs = {
            "receipt_id": receipt_id,
            "project_id": project_id,
            "project_revision": binding.revision,
            "project_fingerprint": binding.fingerprint or None,
            "status": status,
            "authorization_input_ready": (
                status is FormalDraftContinuousO2Status.COMPLETE
            ),
            "authorization_unlock": False,
            "formal_draft_allowed": False,
            "writes_project_truth": False,
            "four_skills_generic_exactly_once": False,
            "checks": checks,
            "missing_required": missing_required,
            "adjacent_chain_id": None if adjacent is None else str(adjacent["chain_id"]),
            "managed_skill_chain_id": (
                None if managed is None else str(managed["chain_id"])
            ),
            "creation_suite_chain_id": (
                None if creation is None else str(creation["chain_id"])
            ),
            "source_import_id": four_skills.source_import_id,
            "formalization_receipt_id": four_skills.formalization_receipt_id,
            "four_skills_continuous_receipt_id": (
                four_skills.receipt_id if four_skills_ready else None
            ),
            "instruction_chain_handoff_binding_id": (
                None if instruction_handoff is None else str(instruction_handoff["binding_id"])
            ),
            "instruction_chain_handoff_id": (
                None if instruction_handoff is None else str(instruction_handoff["handoff_id"])
            ),
            "policy_version": "local-v1-continuous-o2-3",
            "evaluated_at": evaluated_at,
        }
        provisional = FormalDraftContinuousO2Receipt.model_construct(
            schema_id="zhimeng.formal-draft-continuous-o2-receipt.v1",
            receipt_hash="0" * 64,
            **kwargs,
        )
        receipt = FormalDraftContinuousO2Receipt(
            receipt_hash=formal_draft_continuous_o2_receipt_hash(provisional),
            **kwargs,
        )

        project_root = self.author_decision_root / project_id
        project_root.mkdir(parents=True, exist_ok=True)
        payload = receipt.model_dump(mode="json", by_alias=True)
        (project_root / "continuous-o2-receipt.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return FormalDraftContinuousO2Response(receipt=receipt)


    def bind_instruction_chain_handoff(
        self,
        project_id: str,
        request: ProjectInstructionChainHandoffBindRequest,
    ) -> ProjectInstructionChainHandoffBindingResponse:
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")
        if request.expected_project_revision != binding.revision:
            raise FormalDraftAuthorizationConflict(
                "project revision does not match the instruction chain handoff bind request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if request.expected_project_fingerprint != (binding.fingerprint or ""):
            raise FormalDraftAuthorizationConflict(
                "project fingerprint does not match the instruction chain handoff bind request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        handoff_path = (
            self.four_skills_raw_root
            / request.batch_id
            / "instruction-chain-handoff.json"
        )
        if not handoff_path.is_file():
            raise FormalDraftAuthorizationNotFound(
                "instruction chain handoff was not found for the requested batch"
            )
        handoff = FourSkillsInstructionChainHandoffReceipt.model_validate(
            json.loads(handoff_path.read_text(encoding="utf-8"))
        )
        if handoff.batch_id != request.batch_id:
            raise FormalDraftAuthorizationConflict("handoff batch_id does not match request")
        if handoff.batch_hash != request.batch_hash:
            raise FormalDraftAuthorizationConflict("handoff batch_hash does not match request")
        if handoff.handoff_id != request.handoff_id:
            raise FormalDraftAuthorizationConflict("handoff_id does not match request")
        if handoff.handoff_hash != request.handoff_hash:
            raise FormalDraftAuthorizationConflict("handoff_hash does not match request")
        if (
            not handoff.instruction_chain_ready
            or handoff.status != "ready"
            or handoff.prepared_step_count != 4
            or handoff.managed_skill_execution is not False
            or handoff.raw_skill_md_execution is not False
            or handoff.four_skills_generic_exactly_once is not False
            or handoff.formal_draft_allowed is not False
            or handoff.writes_project_truth is not False
        ):
            raise FormalDraftAuthorizationInvalid(
                "instruction chain handoff is not ready for project binding"
            )

        existing = self._read_instruction_chain_handoff_binding(project_id)
        if (
            existing is not None
            and existing.project_revision == binding.revision
            and existing.project_fingerprint == (binding.fingerprint or "")
            and existing.batch_id == handoff.batch_id
            and existing.batch_hash == handoff.batch_hash
            and existing.handoff_id == handoff.handoff_id
            and existing.handoff_hash == handoff.handoff_hash
        ):
            return ProjectInstructionChainHandoffBindingResponse(receipt=existing)

        bound_at = _utcnow()
        material_for_id = {
            "project_id": project_id,
            "project_revision": binding.revision,
            "project_fingerprint": binding.fingerprint or "",
            "batch_id": handoff.batch_id,
            "batch_hash": handoff.batch_hash,
            "handoff_id": handoff.handoff_id,
            "handoff_hash": handoff.handoff_hash,
        }
        binding_id = "project-instruction-chain-handoff-" + hashlib.sha256(
            json.dumps(material_for_id, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:32]
        detail = (
            "Instruction chain handoff bound to project as authorization input; "
            "raw SKILL.md was not executed, managed skill execution is not claimed, "
            "and formal draft remains denied."
        )
        temp = ProjectInstructionChainHandoffBindingReceipt.model_construct(
            schema_id="zhimeng.project-instruction-chain-handoff-binding-receipt.v1",
            binding_id=binding_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or "",
            batch_id=handoff.batch_id,
            batch_hash=handoff.batch_hash,
            handoff_id=handoff.handoff_id,
            handoff_hash=handoff.handoff_hash,
            prepared_step_count=handoff.prepared_step_count,
            instruction_chain_ready=True,
            status="bound",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-project-instruction-chain-handoff-1",
            detail=detail,
            bound_at=bound_at,
            binding_hash="0" * 64,
        )
        binding_hash = project_instruction_chain_handoff_binding_receipt_hash(temp)
        receipt = ProjectInstructionChainHandoffBindingReceipt(
            binding_id=binding_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or "",
            batch_id=handoff.batch_id,
            batch_hash=handoff.batch_hash,
            handoff_id=handoff.handoff_id,
            handoff_hash=handoff.handoff_hash,
            prepared_step_count=handoff.prepared_step_count,
            instruction_chain_ready=True,
            status="bound",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            four_skills_generic_exactly_once=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-project-instruction-chain-handoff-1",
            detail=detail,
            bound_at=bound_at,
            binding_hash=binding_hash,
        )
        path = self._instruction_chain_handoff_binding_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(receipt.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return ProjectInstructionChainHandoffBindingResponse(receipt=receipt)

    def get_instruction_chain_handoff_binding(
        self, project_id: str
    ) -> ProjectInstructionChainHandoffBindingResponse:
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        receipt = self._read_instruction_chain_handoff_binding(project_id)
        if receipt is None:
            raise FormalDraftAuthorizationNotFound(
                "project instruction chain handoff binding was not found"
            )
        return ProjectInstructionChainHandoffBindingResponse(receipt=receipt)

    def _instruction_chain_handoff_binding_path(self, project_id: str) -> Path:
        return self.instruction_chain_handoff_binding_root / f"{project_id}.json"

    def _read_instruction_chain_handoff_binding(
        self, project_id: str
    ) -> ProjectInstructionChainHandoffBindingReceipt | None:
        path = self._instruction_chain_handoff_binding_path(project_id)
        if not path.is_file():
            return None
        return ProjectInstructionChainHandoffBindingReceipt.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _load_instruction_chain_handoff_binding(
        self, project_id: str
    ) -> dict[str, object] | None:
        receipt = self._read_instruction_chain_handoff_binding(project_id)
        if receipt is None:
            return None
        if (
            receipt.instruction_chain_ready is not True
            or receipt.status != "bound"
            or receipt.prepared_step_count != 4
            or receipt.managed_skill_execution is not False
            or receipt.raw_skill_md_execution is not False
            or receipt.four_skills_generic_exactly_once is not False
            or receipt.formal_draft_allowed is not False
            or receipt.writes_project_truth is not False
        ):
            return None
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            return None
        if (
            receipt.project_revision != binding.revision
            or receipt.project_fingerprint != (binding.fingerprint or "")
        ):
            return None
        # Re-validate underlying handoff artifact still matches.
        handoff_path = (
            self.four_skills_raw_root
            / receipt.batch_id
            / "instruction-chain-handoff.json"
        )
        if not handoff_path.is_file():
            return None
        try:
            handoff = FourSkillsInstructionChainHandoffReceipt.model_validate(
                json.loads(handoff_path.read_text(encoding="utf-8"))
            )
        except Exception:
            return None
        if (
            handoff.handoff_id != receipt.handoff_id
            or handoff.handoff_hash != receipt.handoff_hash
            or handoff.batch_hash != receipt.batch_hash
            or handoff.instruction_chain_ready is not True
            or handoff.status != "ready"
        ):
            return None
        return {
            "binding_id": receipt.binding_id,
            "handoff_id": receipt.handoff_id,
            "batch_id": receipt.batch_id,
        }

    def _load_adjacent_chain_evidence(
        self,
        project_id: str,
        *,
        preferred_chain_id: str | None = None,
    ) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        if preferred_chain_id:
            payload = self._read_json_file(
                self.adjacent_chain_root / f"{preferred_chain_id}.json"
            )
            if payload is not None:
                candidates.append(payload)
        else:
            digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
            index = self._read_json_file(
                self.adjacent_chain_root / f"project-{digest}.json"
            )
            latest_id = None if index is None else index.get("latest_chain_id")
            if isinstance(latest_id, str) and latest_id:
                payload = self._read_json_file(
                    self.adjacent_chain_root / f"{latest_id}.json"
                )
                if payload is not None:
                    candidates.append(payload)
            if self.adjacent_chain_root.is_dir():
                for path in sorted(self.adjacent_chain_root.glob("adjacent-chain-*.json")):
                    payload = self._read_json_file(path)
                    if payload is not None:
                        candidates.append(payload)

        for payload in candidates:
            if not self._is_adjacent_o2_success(payload, project_id):
                continue
            return {
                "chain_id": str(payload.get("chain_id") or ""),
                "status": str(payload.get("status") or ""),
            }
        return None

    def _is_adjacent_o2_success(
        self,
        payload: dict[str, object],
        project_id: str,
    ) -> bool:
        if str(payload.get("status") or "") != "succeeded":
            return False
        if str(payload.get("chain_key") or "") != "novel-distillation-kb-to-context":
            return False
        project = payload.get("project")
        if not isinstance(project, dict):
            return False
        if str(project.get("project_id") or "") != project_id:
            return False
        steps = payload.get("steps")
        if not isinstance(steps, list) or len(steps) != 2:
            return False
        step_ids = [
            str(item.get("step_id") or "")
            for item in steps
            if isinstance(item, dict)
        ]
        statuses = [
            str(item.get("status") or "")
            for item in steps
            if isinstance(item, dict)
        ]
        skill_ids = [
            str(item.get("skill_id") or "")
            for item in steps
            if isinstance(item, dict)
        ]
        if step_ids != ["o2d-kb-handoff", "o2e-context-handoff"]:
            return False
        if statuses != ["succeeded", "succeeded"]:
            return False
        if skill_ids != [
            "novel-distillation.kb-handoff",
            "novel-kb.context-handoff",
        ]:
            return False
        if payload.get("formal_draft_allowed") is not False:
            return False
        return True

    def _load_managed_skill_chain_evidence(
        self,
        project_id: str,
        *,
        preferred_chain_id: str | None = None,
        require_creation_suite: bool,
    ) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        if preferred_chain_id:
            payload = self._read_json_file(
                self.managed_skill_chain_root / f"{preferred_chain_id}.json"
            )
            if payload is not None:
                candidates.append(payload)
        if self.managed_skill_chain_root.is_dir():
            for path in sorted(
                self.managed_skill_chain_root.glob("managed-skill-chain-*.json")
            ):
                payload = self._read_json_file(path)
                if payload is not None:
                    candidates.append(payload)

        best: dict[str, object] | None = None
        for payload in candidates:
            evidence = self._managed_chain_success_evidence(
                payload,
                project_id,
                require_creation_suite=require_creation_suite,
            )
            if evidence is None:
                continue
            if best is None or int(evidence["step_count"]) > int(best["step_count"]):
                best = evidence
        return best

    def _managed_chain_success_evidence(
        self,
        payload: dict[str, object],
        project_id: str,
        *,
        require_creation_suite: bool,
    ) -> dict[str, object] | None:
        if str(payload.get("status") or "") != "succeeded":
            return None
        project = payload.get("project")
        if not isinstance(project, dict):
            return None
        if str(project.get("project_id") or "") != project_id:
            return None
        steps = payload.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            return None
        skill_ids: list[str] = []
        for item in steps:
            if not isinstance(item, dict):
                return None
            if str(item.get("status") or "") != "succeeded":
                return None
            skill_id = str(item.get("skill_id") or "")
            if not skill_id:
                return None
            skill_ids.append(skill_id)
        if payload.get("formal_draft_allowed") is not False:
            return None
        if require_creation_suite:
            if not all(
                skill_id.startswith("novel-creation-suite.") for skill_id in skill_ids
            ):
                return None
        return {
            "chain_id": str(payload.get("chain_id") or ""),
            "step_count": len(skill_ids),
            "skill_ids": skill_ids,
        }

    def evaluate_provenance(
        self,
        project_id: str,
        request: FormalDraftProvenanceEvaluateRequest | None = None,
    ) -> FormalDraftProvenanceResponse:
        request = request or FormalDraftProvenanceEvaluateRequest()
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")

        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != binding.revision
        ):
            raise FormalDraftAuthorizationConflict(
                "project revision does not match the provenance evaluation request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != (binding.fingerprint or "")
        ):
            raise FormalDraftAuthorizationConflict(
                "project fingerprint does not match the provenance evaluation request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        change_set_id = request.change_set_id
        change_set_payload = None
        if change_set_id:
            change_set_payload = self.database.get_change_set(change_set_id)
            if change_set_payload is None:
                raise FormalDraftAuthorizationNotFound("change set was not found")
            if str(change_set_payload.get("project_id") or "") != project_id:
                raise FormalDraftAuthorizationInvalid(
                    "change set does not belong to the project"
                )

        artifact_root = (
            self.change_set_state_root / change_set_id
            if change_set_id
            else None
        )
        writeback_path = (
            artifact_root / "writeback-evidence-receipt.json"
            if artifact_root is not None
            else None
        )
        hard_fact_path = (
            artifact_root / "hard-fact-gate-receipt.json"
            if artifact_root is not None
            else None
        )
        literary_path = (
            artifact_root / "literary-quality-gate-receipt.json"
            if artifact_root is not None
            else None
        )
        pipeline_path = (
            artifact_root / "gate-pipeline-receipt.json"
            if artifact_root is not None
            else None
        )
        author_decision = self._load_author_decision(project_id)

        writeback_payload = self._read_json_file(writeback_path) if writeback_path else None
        hard_fact_payload = self._read_json_file(hard_fact_path) if hard_fact_path else None
        literary_payload = self._read_json_file(literary_path) if literary_path else None
        pipeline_payload = self._read_json_file(pipeline_path) if pipeline_path else None

        change_set_status = (
            str(change_set_payload.get("status") or "")
            if isinstance(change_set_payload, dict)
            else ""
        )
        applied_like = change_set_status in {"applied", "rolled_back"}
        origin = (
            str(change_set_payload.get("origin") or "")
            if isinstance(change_set_payload, dict)
            else ""
        )

        agent_provenance = None
        if change_set_id and origin == "agent":
            try:
                agent_provenance = self.database.get_change_set_provenance(change_set_id)
            except Exception:
                agent_provenance = None

        formal_auth_on_writeback = False
        if isinstance(writeback_payload, dict):
            formal_auth_on_writeback = bool(
                writeback_payload.get("formal_draft_authorization_receipt_id")
                and writeback_payload.get("formal_draft_authorization_receipt_hash")
                and writeback_payload.get("formal_draft_allowed") is False
            )

        scoped = change_set_id is not None
        checks = [
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.PROJECT_BOUND,
                required=True,
                present=True,
                detail=f"project bound at revision {binding.revision}",
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.CHANGE_SET_BOUND,
                required=scoped,
                present=change_set_payload is not None,
                detail=(
                    f"change set bound with status={change_set_status or 'missing'}"
                    if change_set_payload is not None
                    else (
                        "change set scope was not provided"
                        if not scoped
                        else "change set was not found"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.WRITEBACK_EVIDENCE,
                required=scoped and applied_like,
                present=writeback_payload is not None,
                detail=(
                    "writeback evidence receipt present"
                    if writeback_payload is not None
                    else (
                        "writeback evidence not required until apply/rollback"
                        if scoped and not applied_like
                        else "writeback evidence receipt missing"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.FORMAL_DRAFT_AUTH_ON_WRITEBACK,
                required=scoped and applied_like,
                present=formal_auth_on_writeback,
                detail=(
                    "writeback evidence binds formal-draft authorization receipt"
                    if formal_auth_on_writeback
                    else (
                        "formal-draft auth binding not required until apply/rollback"
                        if scoped and not applied_like
                        else "writeback evidence lacks formal-draft authorization binding"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.HARD_FACT_GATE_RECEIPT,
                required=scoped,
                present=hard_fact_payload is not None,
                detail=(
                    "hard-fact Gate receipt present"
                    if hard_fact_payload is not None
                    else (
                        "hard-fact Gate receipt not required without change set scope"
                        if not scoped
                        else "hard-fact Gate receipt missing"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.LITERARY_QUALITY_GATE_RECEIPT,
                required=scoped,
                present=literary_payload is not None,
                detail=(
                    "literary quality Gate receipt present"
                    if literary_payload is not None
                    else (
                        "literary quality Gate receipt not required without change set scope"
                        if not scoped
                        else "literary quality Gate receipt missing"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.GATE_PIPELINE_RECEIPT,
                required=False,
                present=pipeline_payload is not None,
                detail=(
                    "ordered gate pipeline receipt present"
                    if pipeline_payload is not None
                    else (
                        "ordered gate pipeline receipt is optional until pipeline evaluation"
                        if scoped
                        else "ordered gate pipeline receipt not required without change set scope"
                    )
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.AUTHOR_DECISION_RECEIPT,
                required=True,
                present=author_decision is not None,
                detail=(
                    f"author decision present intent={author_decision.intent.value}"
                    if author_decision is not None
                    else "author formal-draft decision receipt missing"
                ),
            ),
            FormalDraftProvenanceCheck(
                code=FormalDraftProvenanceCheckCode.AGENT_CHANGE_SET_PROVENANCE,
                required=scoped and origin == "agent",
                present=agent_provenance is not None,
                detail=(
                    "agent change set provenance present"
                    if agent_provenance is not None
                    else (
                        "agent provenance not required for non-agent origin"
                        if not (scoped and origin == "agent")
                        else "agent change set provenance missing"
                    )
                ),
            ),
        ]

        missing_required = [
            item.code for item in checks if item.required and not item.present
        ]
        if not scoped:
            status = FormalDraftProvenanceStatus.SCOPE_LIMITED
        elif missing_required:
            status = FormalDraftProvenanceStatus.INCOMPLETE
        else:
            status = FormalDraftProvenanceStatus.COMPLETE

        evaluated_at = _utcnow()
        receipt_id = _digest_id(
            "formal-draft-provenance-",
            project_id,
            binding.revision,
            binding.fingerprint or "",
            change_set_id or "",
            status.value,
            ",".join(item.value for item in missing_required),
            evaluated_at.isoformat(),
        )
        provisional = FormalDraftProvenanceReceipt.model_construct(
            schema_id="zhimeng.formal-draft-provenance-receipt.v1",
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            change_set_id=change_set_id,
            status=status,
            authorization_input_ready=(status is FormalDraftProvenanceStatus.COMPLETE),
            authorization_unlock=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            checks=checks,
            missing_required=missing_required,
            policy_version="local-v1-provenance-1",
            evaluated_at=evaluated_at,
            receipt_hash="0" * 64,
        )
        receipt = FormalDraftProvenanceReceipt(
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            change_set_id=change_set_id,
            status=status,
            authorization_input_ready=(status is FormalDraftProvenanceStatus.COMPLETE),
            authorization_unlock=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            checks=checks,
            missing_required=missing_required,
            policy_version="local-v1-provenance-1",
            evaluated_at=evaluated_at,
            receipt_hash=formal_draft_provenance_receipt_hash(provisional),
        )

        # Persist latest provenance receipt under project formal-draft root and optional change-set root.
        project_root = self.author_decision_root / project_id
        project_root.mkdir(parents=True, exist_ok=True)
        payload = receipt.model_dump(mode="json", by_alias=True)
        (project_root / "provenance-receipt.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if artifact_root is not None:
            artifact_root.mkdir(parents=True, exist_ok=True)
            (artifact_root / "formal-draft-provenance-receipt.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return FormalDraftProvenanceResponse(receipt=receipt)

    @staticmethod
    def _read_json_file(path: Path | None) -> dict[str, object] | None:
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def get_author_decision(self, project_id: str) -> FormalDraftAuthorDecisionResponse:
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")
        latest = self._load_author_decision(project_id)
        history = self._load_author_decision_history(project_id)
        return FormalDraftAuthorDecisionResponse(receipt=latest, history=history)

    def record_author_decision(
        self,
        project_id: str,
        request: FormalDraftAuthorDecisionRequest,
    ) -> FormalDraftAuthorDecisionResponse:
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FormalDraftAuthorizationInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FormalDraftAuthorizationNotFound("project binding was not found")

        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != binding.revision
        ):
            raise FormalDraftAuthorizationConflict(
                "project revision does not match the author decision request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != (binding.fingerprint or "")
        ):
            raise FormalDraftAuthorizationConflict(
                "project fingerprint does not match the author decision request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        note = request.note.strip() if request.note else None
        if note == "":
            note = None
        decided_at = _utcnow()
        receipt_id = _digest_id(
            "formal-draft-author-",
            project_id,
            binding.revision,
            binding.fingerprint or "",
            request.intent.value,
            request.change_set_id or "",
            note or "",
            decided_at.isoformat(),
        )
        provisional = FormalDraftAuthorDecisionReceipt.model_construct(
            schema_id="zhimeng.formal-draft-author-decision-receipt.v1",
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            change_set_id=request.change_set_id,
            intent=request.intent,
            note=note,
            authorization_effective=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-locked-1",
            decided_at=decided_at,
            receipt_hash="0" * 64,
        )
        receipt = FormalDraftAuthorDecisionReceipt(
            receipt_id=receipt_id,
            project_id=project_id,
            project_revision=binding.revision,
            project_fingerprint=binding.fingerprint or None,
            change_set_id=request.change_set_id,
            intent=request.intent,
            note=note,
            authorization_effective=False,
            formal_draft_allowed=False,
            writes_project_truth=False,
            policy_version="local-v1-locked-1",
            decided_at=decided_at,
            receipt_hash=formal_draft_author_decision_receipt_hash(provisional),
        )

        root = self._author_project_root(project_id)
        root.mkdir(parents=True, exist_ok=True)
        payload = receipt.model_dump(mode="json", by_alias=True)
        latest_path = root / "author-decision-receipt.json"
        history_path = root / "author-decision-history.json"
        latest_path.write_text(
            __import__("json").dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        history = self._load_author_decision_history(project_id)
        history = [item for item in history if item.receipt_id != receipt.receipt_id]
        history.append(receipt)
        history = history[-32:]
        history_path.write_text(
            __import__("json").dumps(
                [item.model_dump(mode="json", by_alias=True) for item in history],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return FormalDraftAuthorDecisionResponse(receipt=receipt, history=history)

    def _author_project_root(self, project_id: str) -> Path:
        return self.author_decision_root / project_id

    def _load_author_decision(
        self,
        project_id: str,
    ) -> FormalDraftAuthorDecisionReceipt | None:
        path = self._author_project_root(project_id) / "author-decision-receipt.json"
        if not path.is_file():
            return None
        try:
            import json
            payload = json.loads(path.read_text(encoding="utf-8"))
            return FormalDraftAuthorDecisionReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError):
            return None

    def _load_author_decision_history(
        self,
        project_id: str,
    ) -> list[FormalDraftAuthorDecisionReceipt]:
        path = self._author_project_root(project_id) / "author-decision-history.json"
        if not path.is_file():
            return []
        try:
            import json
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return []
        if not isinstance(payload, list):
            return []
        history: list[FormalDraftAuthorDecisionReceipt] = []
        for item in payload:
            try:
                history.append(FormalDraftAuthorDecisionReceipt.model_validate(item))
            except (TypeError, ValueError):
                continue
        return history[-32:]

    def _load_hard_fact_receipt(self, change_set_id: str | None):
        if not change_set_id:
            return None
        path = (
            self.change_set_state_root
            / change_set_id
            / "hard-fact-gate-receipt.json"
        )
        if not path.is_file():
            return None
        try:
            import json
            from zhimeng_runtime.hard_fact_models import HardFactGateReceipt

            payload = json.loads(path.read_text(encoding="utf-8"))
            return HardFactGateReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError):
            return None

    def _load_literary_quality_receipt(self, change_set_id: str | None):
        if not change_set_id:
            return None
        path = (
            self.change_set_state_root
            / change_set_id
            / "literary-quality-gate-receipt.json"
        )
        if not path.is_file():
            return None
        try:
            import json
            from zhimeng_runtime.literary_quality_models import LiteraryQualityGateReceipt

            payload = json.loads(path.read_text(encoding="utf-8"))
            return LiteraryQualityGateReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError):
            return None

    def _load_gate_pipeline_receipt(self, change_set_id: str | None):
        if not change_set_id:
            return None
        path = (
            self.change_set_state_root
            / change_set_id
            / "gate-pipeline-receipt.json"
        )
        if not path.is_file():
            return None
        try:
            import json
            from zhimeng_runtime.gate_pipeline_models import GatePipelineReceipt

            payload = json.loads(path.read_text(encoding="utf-8"))
            return GatePipelineReceipt.model_validate(payload)
        except (OSError, TypeError, ValueError):
            return None


__all__ = [
    "FormalDraftAuthorizationConflict",
    "FormalDraftAuthorizationError",
    "FormalDraftAuthorizationInvalid",
    "FormalDraftAuthorizationNotFound",
    "FormalDraftAuthorizationService",
]
