from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from zhimeng_runtime.provenance_models import (
    KnowledgeProvenanceRef,
    knowledge_formalization_receipt_id,
)


Sha256: TypeAlias = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText: TypeAlias = Annotated[str, StringConstraints(min_length=1)]
ProjectFileId: TypeAlias = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, strip_whitespace=True),
]


def _canonical_manual_file_ids(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(item.strip() for item in value)
    if any(not item for item in normalized):
        raise ValueError("manual_file_ids must not contain blank values")
    if len(normalized) != len(set(normalized)):
        raise ValueError("manual_file_ids must be unique")
    return tuple(sorted(normalized))


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ContextAuthority(StrEnum):
    PROJECT_TRUTH = "project_truth"
    FORMAL_KNOWLEDGE = "formal_knowledge"
    REFERENCE = "reference"
    RISK = "risk"


class ContextReadiness(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class ContextGateSeverity(StrEnum):
    WARNING = "warning"
    BLOCKING = "blocking"


class KnowledgeObservationState(StrEnum):
    MATCHED = "matched"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"


class KnowledgeFingerprintObservation(ContextModel):
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    pack_hash: Sha256
    expected_fingerprint: Sha256 | None = None
    observed_fingerprint: Sha256 | None = None
    state: KnowledgeObservationState
    observed_at: datetime

    @model_validator(mode="after")
    def validate_observation(self) -> KnowledgeFingerprintObservation:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("knowledge observation time must be timezone-aware")
        if self.state is KnowledgeObservationState.MATCHED:
            if self.observed_fingerprint != self.expected_fingerprint:
                raise ValueError("matched knowledge fingerprints must be equal")
        elif self.state is KnowledgeObservationState.CHANGED:
            if (
                self.expected_fingerprint is None
                or self.observed_fingerprint is None
                or self.expected_fingerprint == self.observed_fingerprint
            ):
                raise ValueError("changed knowledge fingerprints must be distinct")
        elif self.observed_fingerprint is not None:
            raise ValueError("unavailable knowledge cannot have an observed fingerprint")
        return self


class FormalKnowledgeCardSelector(ContextModel):
    card_id: str = Field(min_length=1, max_length=160, strict=True)
    version_id: str = Field(
        pattern=r"^knowledge-version-[0-9a-f]{32}$",
        strict=True,
    )
    content_hash: Sha256

    @field_validator("card_id")
    @classmethod
    def normalize_card_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("formal knowledge card_id must not be blank")
        return normalized


class FormalKnowledgeSelector(ContextModel):
    schema_id: Literal["zhimeng.formal-knowledge-selector.v1"] = Field(
        default="zhimeng.formal-knowledge-selector.v1",
        alias="schema",
    )
    source_import_id: str = Field(
        pattern=r"^source-import-[0-9a-f]{32}$",
        strict=True,
    )
    formalization_receipt_id: str = Field(
        pattern=r"^knowledge-formalization-[0-9a-f]{32}$",
        strict=True,
    )
    formalization_receipt_hash: Sha256
    expected_knowledge_fingerprint: Sha256
    cards: tuple[FormalKnowledgeCardSelector, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @model_validator(mode="after")
    def validate_selector(self) -> FormalKnowledgeSelector:
        if self.formalization_receipt_id != knowledge_formalization_receipt_id(
            self.source_import_id
        ):
            raise ValueError(
                "formal knowledge selector receipt belongs to another source import"
            )
        card_ids = tuple(item.card_id for item in self.cards)
        version_ids = tuple(item.version_id for item in self.cards)
        if card_ids != tuple(sorted(card_ids)):
            raise ValueError("formal knowledge selector cards must be sorted by card_id")
        if len(card_ids) != len(set(card_ids)):
            raise ValueError("formal knowledge selector card_ids must be unique")
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("formal knowledge selector version_ids must be unique")
        return self


class ContextCreateRequest(ContextModel):
    objective: str = Field(min_length=1, max_length=100_000, strict=True)
    profile: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
        strict=True,
    )
    active_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        strict=True,
    )
    expected_project_revision: int = Field(ge=1, strict=True)
    expected_project_fingerprint: Sha256
    # 知识库快照绑定可选（2026-09-25 起普通对话不再带）：带了就核对，不带就只按书的版本号走。
    project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
        strict=True,
    )
    project_kb_manifest_hash: Sha256 | None = None
    formal_knowledge_selector: FormalKnowledgeSelector | None = None
    manual_file_ids: tuple[ProjectFileId, ...] = Field(default=(), max_length=20)
    include_session_history: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_project_kb_pair(self) -> ContextCreateRequest:
        if (self.project_kb_state_id is None) != (self.project_kb_manifest_hash is None):
            raise ValueError("project KB state identity and manifest hash must be paired")
        return self

    @field_validator("objective")
    @classmethod
    def normalize_objective(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("objective must not be blank")
        return normalized

    @field_validator("active_chapter_id")
    @classmethod
    def normalize_active_chapter_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("active_chapter_id must not be blank")
        return normalized

    @field_validator("manual_file_ids")
    @classmethod
    def normalize_manual_file_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _canonical_manual_file_ids(value)


class ContextFragmentIdentity(ContextModel):
    source_id: str = Field(min_length=1, max_length=512, strict=True)
    source_type: str = Field(min_length=1, max_length=128, strict=True)
    title: str = Field(min_length=1, max_length=500, strict=True)
    authority: ContextAuthority
    version: str = Field(min_length=1, max_length=256, strict=True)
    source_hash: Sha256
    priority: int = Field(ge=0, le=1_000_000, strict=True)
    mandatory: bool = Field(strict=True)
    knowledge_provenance: KnowledgeProvenanceRef | None = None

    @field_validator("source_id", "source_type", "title", "version")
    @classmethod
    def normalize_identity_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("fragment identity fields must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_knowledge_provenance(self) -> "ContextFragmentIdentity":
        if self.knowledge_provenance is not None:
            if (
                self.authority is not ContextAuthority.FORMAL_KNOWLEDGE
                or not self.source_type.startswith("native_knowledge_card:")
            ):
                raise ValueError(
                    "knowledge provenance is reserved for native formal knowledge"
                )
            if self.source_id != f"knowledge-card:{self.knowledge_provenance.card_id}":
                raise ValueError("knowledge provenance requires a knowledge-card source")
            if (
                self.version != self.knowledge_provenance.version_id
                or self.source_hash != self.knowledge_provenance.version_content_hash
            ):
                raise ValueError("knowledge provenance does not match fragment version")
        return self


class ContextFragmentBase(ContextFragmentIdentity):
    content: str = Field(min_length=1, max_length=500_000, strict=True)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fragment content must not be blank")
        return value


class ContextCandidateFragment(ContextFragmentBase):
    eligible: bool = Field(strict=True)
    exclusion_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        strict=True,
    )

    @model_validator(mode="after")
    def validate_eligibility_reason(self) -> ContextCandidateFragment:
        if self.eligible and self.exclusion_reason is not None:
            raise ValueError("eligible fragments cannot carry an exclusion_reason")
        if not self.eligible and self.exclusion_reason is None:
            raise ValueError("ineligible fragments require an exclusion_reason")
        return self


class CompletedHistoryMessage(ContextModel):
    ordinal: int = Field(ge=1, strict=True)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=500_000, strict=True)
    completed: Literal[True] = True

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("history content must not be blank")
        return value


class ContextIncludedFragment(ContextFragmentBase):
    fragment_hash: Sha256
    estimated_characters: int = Field(ge=1, le=2_000_000, strict=True)
    estimated_tokens: int = Field(ge=1, le=1_000_000, strict=True)
    selection_reason: Literal["mandatory", "priority"]


class ContextExcludedFragment(ContextFragmentIdentity):
    eligible: bool = Field(strict=True)
    candidate_exclusion_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        strict=True,
    )
    fragment_hash: Sha256
    estimated_characters: int = Field(ge=1, le=2_000_000, strict=True)
    estimated_tokens: int = Field(ge=1, le=1_000_000, strict=True)
    exclusion_reason: str = Field(min_length=1, max_length=1024, strict=True)

    @model_validator(mode="after")
    def validate_candidate_reason(self) -> ContextExcludedFragment:
        if not self.eligible and self.candidate_exclusion_reason is None:
            raise ValueError("upstream-ineligible fragments must retain their reason")
        if self.eligible and self.candidate_exclusion_reason is not None:
            raise ValueError("upstream-eligible fragments cannot gain a candidate reason")
        return self


class ContextGate(ContextModel):
    code: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
        strict=True,
    )
    severity: ContextGateSeverity
    message: str = Field(min_length=1, max_length=4096, strict=True)
    source_id: str | None = Field(default=None, min_length=1, max_length=512)


class ContextProviderMessage(ContextModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=4_000_000, strict=True)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider message content must not be blank")
        return value


class ContextBudget(ContextModel):
    character_estimator: Literal["unicode_codepoints_v1"] = "unicode_codepoints_v1"
    token_estimator: Literal["conservative_utf8_bytes_div3_v1"] = (
        "conservative_utf8_bytes_div3_v1"
    )
    max_characters: int = Field(ge=1, strict=True)
    used_characters: int = Field(ge=0, strict=True)
    remaining_characters: int = Field(ge=0, strict=True)
    overflow_characters: int = Field(ge=0, strict=True)
    max_tokens: int = Field(ge=1, strict=True)
    used_tokens: int = Field(ge=0, strict=True)
    remaining_tokens: int = Field(ge=0, strict=True)
    overflow_tokens: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_arithmetic(self) -> ContextBudget:
        expected_remaining = max(0, self.max_characters - self.used_characters)
        expected_overflow = max(0, self.used_characters - self.max_characters)
        if self.remaining_characters != expected_remaining:
            raise ValueError("remaining_characters does not match the budget")
        if self.overflow_characters != expected_overflow:
            raise ValueError("overflow_characters does not match the budget")
        expected_remaining_tokens = max(0, self.max_tokens - self.used_tokens)
        expected_overflow_tokens = max(0, self.used_tokens - self.max_tokens)
        if self.remaining_tokens != expected_remaining_tokens:
            raise ValueError("remaining_tokens does not match the budget")
        if self.overflow_tokens != expected_overflow_tokens:
            raise ValueError("overflow_tokens does not match the budget")
        return self


class ContextPack(ContextModel):
    schema_id: Literal["zhimeng.context-pack.v1"] = Field(
        default="zhimeng.context-pack.v1",
        alias="schema",
    )
    context_pack_id: str = Field(min_length=1, max_length=128, strict=True)
    session_id: str = Field(min_length=1, max_length=128, strict=True)
    project_id: str = Field(min_length=1, max_length=128, strict=True)
    history_through_ordinal: int = Field(ge=0, strict=True)
    history_message_count: int = Field(ge=0, strict=True)
    include_session_history: bool = Field(default=False, strict=True)
    objective: str = Field(min_length=1, max_length=100_000, strict=True)
    profile: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_-]*$",
        strict=True,
    )
    active_chapter_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        strict=True,
    )
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    project_kb_state_id: str | None = Field(
        default=None,
        pattern=r"^project-kb-state-[0-9a-f]{32}$",
        strict=True,
    )
    project_kb_manifest_hash: Sha256 | None = None
    knowledge_fingerprint: Sha256 | None = None
    formal_knowledge_selector: FormalKnowledgeSelector | None = None
    manual_file_ids: tuple[ProjectFileId, ...] = Field(default=(), max_length=20)
    compiler_version: str = Field(min_length=1, max_length=128, strict=True)
    policy_version: str = Field(min_length=1, max_length=128, strict=True)
    budget: ContextBudget
    included: tuple[ContextIncludedFragment, ...]
    excluded: tuple[ContextExcludedFragment, ...]
    gates: tuple[ContextGate, ...]
    exact_provider_messages: tuple[ContextProviderMessage, ...] = Field(min_length=2)
    pack_hash: Sha256
    injection_hash: Sha256
    provider_messages_hash: Sha256
    readiness: ContextReadiness
    created_at: datetime

    @field_validator("manual_file_ids")
    @classmethod
    def normalize_manual_file_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _canonical_manual_file_ids(value)

    @model_validator(mode="after")
    def validate_pack_integrity(self) -> ContextPack:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if (self.project_kb_state_id is None) != (
            self.project_kb_manifest_hash is None
        ):
            raise ValueError("project KB state identity and manifest hash must be paired")

        included_ids = [item.source_id for item in self.included]
        excluded_ids = [item.source_id for item in self.excluded]
        all_ids = included_ids + excluded_ids
        if len(all_ids) > 256:
            raise ValueError("a Context Pack cannot trace more than 256 fragments")
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("fragment source_ids must be unique across the pack")
        traced_manual_file_ids = {
            source_id.removeprefix("project-file:")
            for source_id in all_ids
            if source_id.startswith("project-file:")
        }
        if not set(self.manual_file_ids).issubset(traced_manual_file_ids):
            raise ValueError("manual_file_ids must be traced by Context fragments")
        if tuple(self.included) != tuple(sorted(self.included, key=fragment_sort_key)):
            raise ValueError("included fragments must use canonical selection order")
        if tuple(self.excluded) != tuple(sorted(self.excluded, key=fragment_sort_key)):
            raise ValueError("excluded fragments must use canonical selection order")

        for item in self.included:
            if item.fragment_hash != context_fragment_hash(item.content):
                raise ValueError(f"fragment_hash does not match content: {item.source_id}")
            if item.estimated_characters != len(render_context_fragment(item)):
                raise ValueError(
                    f"estimated_characters does not match fragment: {item.source_id}"
                )
            if item.estimated_tokens != conservative_token_estimate(
                render_context_fragment(item)
            ):
                raise ValueError(
                    f"estimated_tokens does not match fragment: {item.source_id}"
                )
        if any(item.authority is ContextAuthority.RISK for item in self.included):
            raise ValueError("risk-authority fragments cannot be included")
        if (
            self.knowledge_fingerprint is None
            and any(
                item.authority is ContextAuthority.FORMAL_KNOWLEDGE
                for item in self.included
            )
        ):
            raise ValueError(
                "included formal knowledge requires a knowledge_fingerprint"
            )
        if self.formal_knowledge_selector is not None:
            selector = self.formal_knowledge_selector
            if self.knowledge_fingerprint != selector.expected_knowledge_fingerprint:
                raise ValueError(
                    "formal knowledge selector does not match knowledge_fingerprint"
                )
            traced = {
                item.source_id: item for item in (*self.included, *self.excluded)
            }
            for selected in selector.cards:
                fragment = traced.get(f"knowledge-card:{selected.card_id}")
                if fragment is None:
                    raise ValueError(
                        "formal knowledge selector card is not traced by the Context Pack"
                    )
                provenance = fragment.knowledge_provenance
                if (
                    fragment.authority is not ContextAuthority.FORMAL_KNOWLEDGE
                    or fragment.version != selected.version_id
                    or fragment.source_hash != selected.content_hash
                    or provenance is None
                    or provenance.source_import_id != selector.source_import_id
                    or provenance.formalization_receipt_id
                    != selector.formalization_receipt_id
                    or provenance.formalization_receipt_hash
                    != selector.formalization_receipt_hash
                ):
                    raise ValueError(
                        "formal knowledge selector does not match traced card provenance"
                    )

        gate_keys = [(gate.code, gate.source_id) for gate in self.gates]
        if len(self.gates) > 257:
            raise ValueError("a Context Pack cannot contain more than 257 gates")
        if len(gate_keys) != len(set(gate_keys)):
            raise ValueError("context gates must be unique")
        excluded_by_id = {item.source_id: item for item in self.excluded}
        for gate in self.gates:
            if gate.source_id is not None and gate.source_id not in excluded_by_id:
                raise ValueError("source-bound gates must reference an excluded fragment")
        for item in self.excluded:
            item_gates = [gate for gate in self.gates if gate.source_id == item.source_id]
            if len(item_gates) != 1:
                raise ValueError("every excluded fragment must have exactly one gate")
            expected_severity = (
                ContextGateSeverity.BLOCKING
                if item.mandatory
                else ContextGateSeverity.WARNING
            )
            if item_gates[0].severity is not expected_severity:
                raise ValueError("gate severity must reflect fragment mandatory status")

        expected_readiness = readiness_from_gates(self.gates)
        if self.readiness is not expected_readiness:
            raise ValueError("readiness must match gate severities")

        messages = self.exact_provider_messages
        if messages[0].role != "system":
            raise ValueError("the first provider message must be system context")
        if messages[-1].role != "user" or messages[-1].content != self.objective:
            raise ValueError("the final provider message must be the current objective")
        history = messages[1:-1]
        if len(history) != self.history_message_count:
            raise ValueError("history_message_count must match provider messages")
        if self.history_through_ordinal < self.history_message_count:
            raise ValueError("history ordinal cannot be smaller than history message count")
        for index, message in enumerate(history):
            expected_role = "user" if index % 2 == 0 else "assistant"
            if message.role != expected_role:
                raise ValueError("completed provider history must alternate user/assistant")
        if history and history[-1].role != "assistant":
            raise ValueError("completed provider history must end with an assistant message")

        expected_system = render_system_context(
            project_id=self.project_id,
            project_revision=self.project_revision,
            project_fingerprint=self.project_fingerprint,
            project_kb_state_id=self.project_kb_state_id,
            project_kb_manifest_hash=self.project_kb_manifest_hash,
            knowledge_fingerprint=self.knowledge_fingerprint,
            profile=self.profile,
            active_chapter_id=self.active_chapter_id,
            included=self.included,
            include_response_guidance=include_response_guidance_for(
                self.compiler_version
            ),
            response_guidance_version=response_guidance_version_for(
                self.compiler_version
            ),
            human_readable=human_readable_for(self.compiler_version),
        )
        if messages[0].content != expected_system:
            raise ValueError("system context does not match included fragments")

        used_characters = provider_message_characters(messages)
        if self.budget.used_characters != used_characters:
            raise ValueError("budget usage must match exact provider messages")
        used_tokens = provider_message_tokens(messages)
        if self.budget.used_tokens != used_tokens:
            raise ValueError("token budget usage must match exact provider messages")
        if self.provider_messages_hash != context_provider_messages_hash(messages):
            raise ValueError("provider_messages_hash does not match exact messages")
        if self.injection_hash != context_injection_hash(self):
            raise ValueError("injection_hash does not match the injection material")
        if self.pack_hash != context_pack_hash(self):
            raise ValueError("pack_hash does not match the immutable pack material")
        return self


ContextPackCreateRequest = ContextCreateRequest
CandidateFragment = ContextCandidateFragment
ProviderMessage = ContextProviderMessage


def canonical_json(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def knowledge_source_fingerprint(
    *,
    library_fingerprint: str,
    tool_fingerprint: str,
) -> str:
    for name, value in (
        ("library_fingerprint", library_fingerprint),
        ("tool_fingerprint", tool_fingerprint),
    ):
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"{name} must be a lowercase SHA-256 fingerprint")
    return canonical_json_hash(
        {
            "library_fingerprint": library_fingerprint,
            "tool_fingerprint": tool_fingerprint,
        }
    )


def context_fragment_hash(content: str) -> str:
    return canonical_json_hash({"content": content})


def fragment_sort_key(
    fragment: ContextFragmentIdentity,
) -> tuple[bool, int, str]:
    return (not fragment.mandatory, -fragment.priority, fragment.source_id)


def _fragment_injection_payload(fragment: ContextFragmentBase) -> dict[str, object]:
    return {
        "source_id": fragment.source_id,
        "source_type": fragment.source_type,
        "title": fragment.title,
        "authority": fragment.authority.value,
        "version": fragment.version,
        "source_hash": fragment.source_hash,
        "fragment_hash": context_fragment_hash(fragment.content),
        "content": fragment.content,
        "priority": fragment.priority,
        "mandatory": fragment.mandatory,
    }


def render_context_fragment(fragment: ContextFragmentBase) -> str:
    return canonical_json(_fragment_injection_payload(fragment))


HUMAN_READABLE_COMPILER_VERSIONS = frozenset(
    {"context-compiler.v4", "context-compiler.v5", "context-compiler.v6"}
)

# 不带【回答要求】的版本：v1（当时还没这段）和 v6（作者 2026-09-20 把「口吻规矩」全删——
# 模型说什么就是什么，不再教它怎么措辞、怎么自称）。存量 v2–v5 资料包受逐字节校验
# 保护，它们的文字一个字不动；新编的资料包从 v6 起都不带。
VERSIONS_WITHOUT_RESPONSE_GUIDANCE = frozenset({"context-compiler.v1", "context-compiler.v6"})


def include_response_guidance_for(compiler_version: str) -> bool:
    return compiler_version not in VERSIONS_WITHOUT_RESPONSE_GUIDANCE


def response_guidance_version_for(compiler_version: str) -> int:
    """每个编译器版本固定对应一版【回答要求】文字。

    存量 Pack 在校验时会用当前代码重新渲染系统消息并逐字节比对，
    所以老版本对应的文字一个字都不能动；要改措辞只能加新版本。
    """

    if compiler_version == "context-compiler.v2":
        return 1
    if compiler_version == "context-compiler.v5":
        return 3
    if compiler_version == "context-compiler.v6":
        return 0  # v6 没有【回答要求】
    return 2


def human_readable_for(compiler_version: str) -> bool:
    return compiler_version in HUMAN_READABLE_COMPILER_VERSIONS


def _response_guidance_text(version: int) -> str:
    if version <= 0:
        return ""
    if version == 1:
        return "直接用简体中文回答作者。只给最终回答，不要复述任务、解释思考过程或展示草稿。"
    if version >= 3:
        # v3（context-compiler.v5 起）：作者 2026-09-13 反馈回答里夹着「ready / 阻断项 /
        # 工具次数 / 窗口 / 回填」这类话，来源是书里的记账文件和内部说法。这里把它们点名禁掉，
        # 并要求把记账文件里的英文状态词翻成人话。
        return (
            "直接用简体中文回答作者。内部判断和工具使用留在单独的思考与操作记录里；"
            "最终回答只说做了什么、结果、未完成原因和确实需要作者决定的事情。不要复述"
            "任务，不要展示草稿、英文自言自语、内部模式名、字段名、数据结构、工具名或"
            " ChangeSet、Context Pack、Gate、repair-only、author-review 等工程术语；"
            "也不要说「窗口」「通道」「工具次数」「回合」「回填」「写回」「审稿」「阻断项」这类内部说法，"
            "遇到限制就直说做不到什么、下一步作者能做什么。书里的总览、修改记录、进度表之类"
            "记账文件只当参考，其中的英文状态词（如 ready、done、drafting、healthy）一律翻成人话，"
            "不要原样念出来。对作者称自己为 Agent，不自称助手或搭档；把这本小说叫「这本书」，不叫「作品」或「项目」。"
        )
    return (
        "直接用简体中文回答作者。内部判断和工具使用留在单独的思考与操作记录里；"
        "最终回答只说做了什么、结果、未完成原因和确实需要作者决定的事情。不要复述"
        "任务，不要展示草稿、英文自言自语、内部模式名、字段名、数据结构、工具名或"
        " ChangeSet、Context Pack、Gate、repair-only、author-review 等工程术语。"
    )


_V4_AUTHORITY_SECTIONS: tuple[tuple[ContextAuthority, str], ...] = (
    (ContextAuthority.PROJECT_TRUTH, "作品内容"),
    (ContextAuthority.FORMAL_KNOWLEDGE, "写作知识"),
    (ContextAuthority.REFERENCE, "参考资料"),
    (ContextAuthority.RISK, "风险提示"),
)


def _render_system_context_human(
    *,
    project_id: str,
    project_revision: int,
    knowledge_fingerprint: str | None,
    profile: str,
    active_chapter_id: str | None,
    included: tuple[ContextIncludedFragment, ...],
    include_response_guidance: bool,
    response_guidance_version: int,
) -> str:
    blocks = [
        "【上下文说明】\n"
        "以下材料来自这本作品当前的文件与写作资料。「作品内容」是唯一真值；"
        "「写作知识」与「参考资料」只作辅助，与作品内容冲突时以作品内容为准。"
    ]
    project_lines = [f"作品：{project_id}", f"版本：第 {project_revision} 版"]
    if active_chapter_id is not None:
        project_lines.append(f"当前章节：{active_chapter_id}")
    blocks.append("【作品】\n" + "\n".join(project_lines))
    if profile.startswith("chat") and include_response_guidance:
        blocks.append(
            "【回答要求】\n" + _response_guidance_text(response_guidance_version)
        )
    for authority, section_name in _V4_AUTHORITY_SECTIONS:
        fragments = [item for item in included if item.authority is authority]
        if not fragments:
            continue
        entries = "\n\n".join(
            f"■ {item.title}\n{item.content}" for item in fragments
        )
        blocks.append(f"【{section_name}】\n{entries}")
    if knowledge_fingerprint is not None:
        blocks.append(f"【写作知识版本】\n装配时核对的知识库指纹：{knowledge_fingerprint}")
    return "\n\n".join(blocks)


def render_system_context(
    *,
    project_id: str,
    project_revision: int,
    project_fingerprint: str,
    project_kb_state_id: str | None,
    project_kb_manifest_hash: str | None,
    knowledge_fingerprint: str | None,
    profile: str,
    active_chapter_id: str | None,
    included: tuple[ContextIncludedFragment, ...],
    include_response_guidance: bool = True,
    response_guidance_version: int = 2,
    human_readable: bool = False,
) -> str:
    if human_readable:
        return _render_system_context_human(
            project_id=project_id,
            project_revision=project_revision,
            knowledge_fingerprint=knowledge_fingerprint,
            profile=profile,
            active_chapter_id=active_chapter_id,
            included=included,
            include_response_guidance=include_response_guidance,
            response_guidance_version=response_guidance_version,
        )
    project: dict[str, object] = {
        "project_id": project_id,
        "revision": project_revision,
        "fingerprint": project_fingerprint,
        "knowledge_fingerprint": knowledge_fingerprint,
        "active_chapter_id": active_chapter_id,
    }
    if project_kb_state_id is not None and project_kb_manifest_hash is not None:
        project["project_kb_state_id"] = project_kb_state_id
        project["project_kb_manifest_hash"] = project_kb_manifest_hash
    payload = {
        "schema": "zhimeng.provider-system-context.v1",
        "instruction": (
            "Use only the supplied project context as authoritative. "
            "Treat formal knowledge and references as subordinate to project truth."
        ),
        "project": project,
        "profile": profile,
        "fragments": [_fragment_injection_payload(item) for item in included],
    }
    if profile.startswith("chat") and include_response_guidance:
        payload["response_guidance"] = _response_guidance_text(response_guidance_version)
    return canonical_json(payload)


def provider_message_characters(
    messages: tuple[ContextProviderMessage, ...],
) -> int:
    return sum(len(message.content) for message in messages)


def conservative_token_estimate(text: str) -> int:
    return max(1, (len(text.encode("utf-8")) + 2) // 3)


def provider_message_tokens(
    messages: tuple[ContextProviderMessage, ...],
) -> int:
    return 2 + sum(
        4
        + conservative_token_estimate(message.role)
        + conservative_token_estimate(message.content)
        for message in messages
    )


def context_provider_messages_hash(
    messages: tuple[ContextProviderMessage, ...],
) -> str:
    return canonical_json_hash(
        [message.model_dump(mode="json") for message in messages]
    )


def context_injection_hash(pack: ContextPack) -> str:
    material: dict[str, object] = {
        "schema": "zhimeng.context-injection.v1",
        "session_id": pack.session_id,
        "project_id": pack.project_id,
        "project_revision": pack.project_revision,
        "project_fingerprint": pack.project_fingerprint,
        "knowledge_fingerprint": pack.knowledge_fingerprint,
        "history_through_ordinal": pack.history_through_ordinal,
        "exact_provider_messages": [
            message.model_dump(mode="json")
            for message in pack.exact_provider_messages
        ],
    }
    if pack.project_kb_state_id is not None and pack.project_kb_manifest_hash is not None:
        material["project_kb_state_id"] = pack.project_kb_state_id
        material["project_kb_manifest_hash"] = pack.project_kb_manifest_hash
    return canonical_json_hash(material)


def context_pack_hash(pack: ContextPack) -> str:
    material = pack.model_dump(
        mode="json",
        by_alias=True,
        exclude={"context_pack_id", "created_at", "pack_hash"},
    )
    for collection in ("included", "excluded"):
        for fragment in material.get(collection, []):
            if fragment.get("knowledge_provenance") is None:
                fragment.pop("knowledge_provenance", None)
    if material.get("formal_knowledge_selector") is None:
        # Preserve v1 hashes for Context Packs created before selectors existed.
        material.pop("formal_knowledge_selector", None)
    if not material.get("manual_file_ids"):
        # Preserve hashes for Context Packs created before manual file selection.
        material.pop("manual_file_ids", None)
    if not material.get("include_session_history"):
        # Preserve hashes for Context Packs created before explicit history opt-in.
        material.pop("include_session_history", None)
    if (
        material.get("project_kb_state_id") is None
        and material.get("project_kb_manifest_hash") is None
    ):
        # Preserve hashes for v19 Context Packs created before project-KB binding.
        material.pop("project_kb_state_id", None)
        material.pop("project_kb_manifest_hash", None)
    return canonical_json_hash(material)


def readiness_from_gates(
    gates: tuple[ContextGate, ...],
) -> ContextReadiness:
    if any(gate.severity is ContextGateSeverity.BLOCKING for gate in gates):
        return ContextReadiness.BLOCKED
    if gates:
        return ContextReadiness.DEGRADED
    return ContextReadiness.READY


__all__ = [
    "CandidateFragment",
    "CompletedHistoryMessage",
    "ContextAuthority",
    "ContextBudget",
    "ContextCandidateFragment",
    "ContextCreateRequest",
    "ContextExcludedFragment",
    "ContextGate",
    "ContextGateSeverity",
    "ContextIncludedFragment",
    "ContextPack",
    "ContextPackCreateRequest",
    "ContextProviderMessage",
    "ContextReadiness",
    "FormalKnowledgeCardSelector",
    "FormalKnowledgeSelector",
    "KnowledgeFingerprintObservation",
    "KnowledgeObservationState",
    "ProviderMessage",
    "canonical_json",
    "canonical_json_hash",
    "conservative_token_estimate",
    "context_fragment_hash",
    "context_injection_hash",
    "context_pack_hash",
    "context_provider_messages_hash",
    "knowledge_source_fingerprint",
    "provider_message_characters",
    "provider_message_tokens",
    "readiness_from_gates",
    "render_context_fragment",
    "render_system_context",
]
