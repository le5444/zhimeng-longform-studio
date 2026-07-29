from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
GateReceiptId = Annotated[str, Field(pattern=r"^post-draft-gate-[0-9a-f]{32}$")]


def canonical_json_hash(value: object) -> str:
    import json

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PostDraftGateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class KnowledgeValidatorRule(PostDraftGateModel):
    rule_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    kind: Literal["forbid_terms", "require_any_term"]
    terms: tuple[
        Annotated[str, Field(min_length=1, max_length=160)]
    , ...] = Field(min_length=1, max_length=16)
    case_sensitive: bool = False
    failure_signature: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    message: str = Field(min_length=8, max_length=1000)

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(value != value.strip() for value in values):
            raise ValueError("knowledge validator terms must not have edge whitespace")
        if any(any(ord(character) < 32 for character in value) for value in values):
            raise ValueError("knowledge validator terms cannot contain control characters")
        return values

    @model_validator(mode="after")
    def validate_rule(self) -> "KnowledgeValidatorRule":
        comparison = (
            self.terms
            if self.case_sensitive
            else tuple(value.casefold() for value in self.terms)
        )
        if len(comparison) != len(set(comparison)):
            raise ValueError("knowledge validator terms must be unique")
        return self


def knowledge_validator_rules_hash(
    rules: tuple[KnowledgeValidatorRule, ...],
    configuration_errors: tuple[str, ...],
) -> str:
    return canonical_json_hash(
        {
            "rules": [rule.model_dump(mode="json") for rule in rules],
            "configuration_errors": list(configuration_errors),
        }
    )


class KnowledgeValidatorBinding(PostDraftGateModel):
    card_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    content_hash: Sha256
    validator_name: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    provenance: KnowledgeProvenanceRef
    rules: tuple[KnowledgeValidatorRule, ...] = Field(default=(), max_length=16)
    configuration_errors: tuple[
        Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")]
    , ...] = Field(default=(), max_length=16)
    rules_hash: Sha256

    @model_validator(mode="after")
    def validate_binding(self) -> "KnowledgeValidatorBinding":
        if (
            self.provenance.card_id != self.card_id
            or self.provenance.version_id != self.version_id
            or self.provenance.version_content_hash != self.content_hash
        ):
            raise ValueError("knowledge validator provenance does not match its version")
        rule_ids = tuple(rule.rule_id for rule in self.rules)
        if rule_ids != tuple(sorted(rule_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("knowledge validator rules must be unique and sorted")
        signatures = tuple(rule.failure_signature for rule in self.rules)
        if len(signatures) != len(set(signatures)):
            raise ValueError("knowledge validator rule failure signatures must be unique")
        if (
            self.configuration_errors != tuple(sorted(self.configuration_errors))
            or len(self.configuration_errors) != len(set(self.configuration_errors))
        ):
            raise ValueError(
                "knowledge validator configuration errors must be unique and sorted"
            )
        if not self.rules and not self.configuration_errors:
            raise ValueError(
                "knowledge validator bindings require rules or a fail-closed error"
            )
        if self.rules_hash != knowledge_validator_rules_hash(
            self.rules,
            self.configuration_errors,
        ):
            raise ValueError("knowledge validator rules hash does not match")
        return self


class KnowledgeValidatorBundle(PostDraftGateModel):
    schema_id: Literal["zhimeng.knowledge-validator-bundle.v1"] = Field(
        default="zhimeng.knowledge-validator-bundle.v1",
        alias="schema",
    )
    context_pack_id: str = Field(min_length=1, max_length=256)
    context_pack_hash: Sha256
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: Sha256
    knowledge_fingerprint: Sha256
    validators: tuple[KnowledgeValidatorBinding, ...] = Field(min_length=1, max_length=8)
    bundle_hash: Sha256

    @model_validator(mode="after")
    def validate_bundle(self) -> "KnowledgeValidatorBundle":
        card_ids = tuple(item.card_id for item in self.validators)
        if card_ids != tuple(sorted(card_ids)) or len(card_ids) != len(set(card_ids)):
            raise ValueError("knowledge validators must be unique and sorted")
        if sum(len(item.rules) for item in self.validators) > 16:
            raise ValueError("knowledge validator bundle exceeds the rule limit")
        if self.bundle_hash != knowledge_validator_bundle_hash(self):
            raise ValueError("knowledge validator bundle hash does not match")
        return self


def knowledge_validator_bundle_hash(bundle: KnowledgeValidatorBundle) -> str:
    return canonical_json_hash(
        bundle.model_dump(
            mode="json",
            by_alias=True,
            exclude={"bundle_hash"},
        )
    )


class KnowledgeValidatorRuleResult(PostDraftGateModel):
    rule_id: str = Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    kind: Literal["forbid_terms", "require_any_term"]
    failure_signature: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    status: Literal["passed", "blocked"]
    match_count: int = Field(ge=0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=128)


class KnowledgeValidatorExecutionResult(PostDraftGateModel):
    card_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    content_hash: Sha256
    rules_hash: Sha256
    status: Literal["passed", "blocked"]
    configuration_errors: tuple[
        Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")]
    , ...] = Field(default=(), max_length=16)
    rule_results: tuple[KnowledgeValidatorRuleResult, ...] = Field(
        default=(),
        max_length=16,
    )
    failure_signatures: tuple[
        Annotated[str, Field(min_length=3, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")]
    , ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_execution(self) -> "KnowledgeValidatorExecutionResult":
        rule_ids = tuple(item.rule_id for item in self.rule_results)
        if rule_ids != tuple(sorted(rule_ids)) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("knowledge validator rule results must be unique and sorted")
        if (
            self.configuration_errors != tuple(sorted(self.configuration_errors))
            or len(self.configuration_errors) != len(set(self.configuration_errors))
        ):
            raise ValueError(
                "knowledge validator execution errors must be unique and sorted"
            )
        expected_failures = tuple(
            dict.fromkeys(
                [
                    *self.configuration_errors,
                    *[
                        item.failure_signature
                        for item in self.rule_results
                        if item.status == "blocked"
                    ],
                ]
            )
        )
        if self.failure_signatures != expected_failures:
            raise ValueError(
                "knowledge validator failures must match configuration and rule results"
            )
        if (self.status == "blocked") != bool(self.failure_signatures):
            raise ValueError("knowledge validator status must match its failures")
        return self


class GatePoint(PostDraftGateModel):
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    offset: int = Field(ge=0)


class GateLocation(PostDraftGateModel):
    start: GatePoint
    end: GatePoint


class GateFinding(PostDraftGateModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    failure_signature: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    severity: Literal["blocked"] = "blocked"
    message: str = Field(min_length=1, max_length=4096)
    evidence: str = Field(max_length=256)
    location: GateLocation


class GateFileResult(PostDraftGateModel):
    path: str = Field(min_length=1, max_length=512)
    status: Literal["passed", "blocked"]
    content_hash: Sha256
    byte_length: int = Field(ge=0)
    body_line_count: int = Field(ge=0)
    findings: list[GateFinding] = Field(max_length=32)


class GateCheck(PostDraftGateModel):
    code: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    status: Literal["passed", "blocked"]
    message: str = Field(min_length=1, max_length=4096)
    evidence_refs: list[str] = Field(max_length=128)


class PostDraftGateResult(PostDraftGateModel):
    schema_id: Literal["zhimeng.creation-draft-gate-result.v1"] = Field(
        default="zhimeng.creation-draft-gate-result.v1",
        alias="schema",
    )
    status: Literal["passed", "blocked"]
    summary: str = Field(min_length=1, max_length=4096)
    candidate_id: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    candidate_manifest_hash: Sha256 | None = None
    knowledge_validator_bundle_hash: Sha256 | None = None
    knowledge_validator_results: tuple[
        KnowledgeValidatorExecutionResult, ...
    ] = Field(default=(), max_length=8)
    quality_assessed: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    global_findings: list[GateFinding] = Field(max_length=64)
    checks: list[GateCheck] = Field(min_length=1, max_length=16)
    files: list[GateFileResult] = Field(min_length=1, max_length=128)
    failure_signatures: list[
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")]
    ] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_result(self) -> "PostDraftGateResult":
        signatures = {
            finding.failure_signature
            for finding in self.global_findings
        }
        signatures.update(
            finding.failure_signature
            for item in self.files
            for finding in item.findings
        )
        if list(dict.fromkeys(self.failure_signatures)) != self.failure_signatures:
            raise ValueError("post-draft Gate failure signatures must be unique")
        if set(self.failure_signatures) != signatures:
            raise ValueError(
                "post-draft Gate failure signatures must match its findings"
            )
        has_findings = bool(signatures)
        if (self.status == "blocked") != has_findings:
            raise ValueError("post-draft Gate status must match its findings")
        if len({check.code for check in self.checks}) != len(self.checks):
            raise ValueError("post-draft Gate checks must be unique")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("post-draft Gate file paths must be unique")
        validator_card_ids = tuple(
            item.card_id for item in self.knowledge_validator_results
        )
        if (
            validator_card_ids != tuple(sorted(validator_card_ids))
            or len(validator_card_ids) != len(set(validator_card_ids))
        ):
            raise ValueError(
                "post-draft Gate knowledge validator results must be unique and sorted"
            )
        if (self.knowledge_validator_bundle_hash is None) != (
            not self.knowledge_validator_results
        ):
            raise ValueError(
                "knowledge validator results and their immutable bundle hash must be paired"
            )
        validator_failures = {
            signature
            for item in self.knowledge_validator_results
            for signature in item.failure_signatures
        }
        if not validator_failures.issubset(signatures):
            raise ValueError(
                "knowledge validator failures must be represented by Gate findings"
            )
        return self


class PostDraftGateReceipt(PostDraftGateModel):
    schema_id: Literal["zhimeng.post-draft-gate-receipt.v1"] = Field(
        default="zhimeng.post-draft-gate-receipt.v1",
        alias="schema",
    )
    gate_receipt_id: GateReceiptId
    change_set_id: str = Field(pattern=r"^changeset-[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    review_revision: int = Field(ge=0)
    base_revision: int = Field(ge=1)
    base_fingerprint: Sha256
    candidate_hash: Sha256
    validator_id: str = Field(min_length=3, max_length=128)
    validator_version: str = Field(min_length=1, max_length=128)
    validator_manifest_fingerprint: Sha256
    validator_package_fingerprint: Sha256
    context_pack_id: str | None = Field(default=None, min_length=1, max_length=256)
    context_pack_hash: Sha256 | None = None
    knowledge_fingerprint: Sha256 | None = None
    route_receipt_id: str | None = Field(
        default=None,
        pattern=r"^nlcl-route-[0-9a-f]{32}$",
    )
    route_receipt_hash: Sha256 | None = None
    knowledge_validator_bundle_hash: Sha256 | None = None
    knowledge_validator_count: int = Field(default=0, ge=0, le=8)
    knowledge_validator_card_ids: tuple[str, ...] = Field(default=(), max_length=8)
    candidate_file_count: int = Field(ge=1)
    skill_receipt_id: str | None = Field(default=None, min_length=1, max_length=256)
    skill_receipt_hash: Sha256 | None = None
    skill_output_hash: Sha256 | None = None
    execution_status: Literal["succeeded", "failed"]
    status: Literal["passed", "blocked", "failed"]
    result: PostDraftGateResult | None = None
    error_code: str | None = Field(default=None, min_length=2, max_length=128)
    error_message: str | None = Field(default=None, min_length=1, max_length=4096)
    quality_assessed: Literal[False] = False
    formal_draft_allowed: Literal[False] = False
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "PostDraftGateReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("post-draft Gate receipt created_at must be timezone-aware")
        if (self.skill_receipt_id is None) != (self.skill_receipt_hash is None):
            raise ValueError("Skill receipt identity and hash must be paired")
        if self.execution_status == "succeeded":
            if self.skill_receipt_id is None or self.skill_output_hash is None:
                raise ValueError("successful Gate execution requires a Skill receipt")
            if self.result is None or self.status != self.result.status:
                raise ValueError("successful Gate receipt must carry its result")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful Gate receipt cannot carry an error")
        else:
            if self.result is not None or self.status != "failed":
                raise ValueError("failed Gate execution cannot carry a domain result")
            if (self.error_code is None) != (self.error_message is None):
                raise ValueError("Gate error code and message must be paired")
            if self.error_code is None:
                raise ValueError("failed Gate execution requires an error")
        if self.context_pack_id is None and self.context_pack_hash is not None:
            raise ValueError("context_pack_hash requires context_pack_id")
        if (self.route_receipt_id is None) != (self.route_receipt_hash is None):
            raise ValueError("NLCL route receipt identity and hash must be paired")
        if self.route_receipt_id is not None and self.context_pack_id is None:
            raise ValueError("NLCL route receipt requires a Context Pack")
        if self.knowledge_validator_count != len(self.knowledge_validator_card_ids):
            raise ValueError("knowledge validator count must match its card IDs")
        if (
            self.knowledge_validator_card_ids
            != tuple(sorted(self.knowledge_validator_card_ids))
            or len(self.knowledge_validator_card_ids)
            != len(set(self.knowledge_validator_card_ids))
        ):
            raise ValueError("knowledge validator card IDs must be unique and sorted")
        if self.knowledge_validator_bundle_hash is None:
            if self.knowledge_validator_count or self.knowledge_validator_card_ids:
                raise ValueError(
                    "knowledge validator identities require their bundle hash"
                )
        elif (
            self.context_pack_id is None
            or self.route_receipt_id is None
            or self.knowledge_fingerprint is None
        ):
            raise ValueError(
                "knowledge validator bundles require Context, route, and knowledge bindings"
            )
        if self.result is not None:
            if (
                self.result.knowledge_validator_bundle_hash
                != self.knowledge_validator_bundle_hash
            ):
                raise ValueError(
                    "Gate result knowledge validator bundle does not match its receipt"
                )
            result_card_ids = tuple(
                item.card_id for item in self.result.knowledge_validator_results
            )
            if result_card_ids != self.knowledge_validator_card_ids:
                raise ValueError(
                    "Gate result knowledge validators do not match its receipt"
                )
        if self.receipt_hash != post_draft_gate_receipt_hash(self):
            raise ValueError("post-draft Gate receipt hash does not match immutable material")
        return self


class PostDraftGateRequest(PostDraftGateModel):
    schema_id: Literal["zhimeng.post-draft-gate-request.v1"] = Field(
        default="zhimeng.post-draft-gate-request.v1",
        alias="schema",
    )
    expected_review_revision: int = Field(ge=0)


class PostDraftGateResponse(PostDraftGateModel):
    schema_id: Literal["zhimeng.post-draft-gate-response.v1"] = Field(
        default="zhimeng.post-draft-gate-response.v1",
        alias="schema",
    )
    change_set_id: str = Field(pattern=r"^changeset-[0-9a-f]{32}$")
    project_id: str = Field(pattern=r"^project-[0-9a-f]{24}$")
    gate_required: bool
    review_revision: int = Field(ge=0)
    receipt: PostDraftGateReceipt | None = None


def post_draft_gate_receipt_hash(receipt: PostDraftGateReceipt) -> str:
    payload = receipt.model_dump(
        mode="json",
        by_alias=True,
        exclude={"receipt_hash"},
    )
    return canonical_json_hash(payload)


__all__ = [
    "GateCheck",
    "GateFileResult",
    "GateFinding",
    "GateLocation",
    "GatePoint",
    "KnowledgeValidatorBinding",
    "KnowledgeValidatorBundle",
    "KnowledgeValidatorExecutionResult",
    "KnowledgeValidatorRule",
    "KnowledgeValidatorRuleResult",
    "PostDraftGateReceipt",
    "PostDraftGateRequest",
    "PostDraftGateResponse",
    "PostDraftGateResult",
    "canonical_json_hash",
    "knowledge_validator_bundle_hash",
    "knowledge_validator_rules_hash",
    "post_draft_gate_receipt_hash",
]
