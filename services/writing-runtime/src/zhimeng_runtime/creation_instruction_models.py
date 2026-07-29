from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash
from zhimeng_runtime.execution_protocol import StableId

if TYPE_CHECKING:
    from zhimeng_runtime.workflow_models import ProviderIoBoundary
    from zhimeng_runtime.writer_models import WriterTask


CREATION_INSTRUCTION_SKILL_ID = "novel-creation-suite.writer-instruction"
CREATION_INSTRUCTION_MAP_PATH = "instruction-map.json"


class CreationInstructionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class CreationInstructionActivationScope(CreationInstructionModel):
    scope_type: Literal["application", "project"]
    scope_key: str = Field(min_length=1, max_length=512)
    configured_state: Literal["unconfigured", "inherited", "enabled", "disabled"]
    state_revision: int = Field(ge=0, strict=True)
    activation_receipt_id: str | None = Field(
        default=None,
        pattern=r"^skill-activation-[0-9a-f]{32}$",
    )
    activation_receipt_hash: Sha256 | None = None
    configured_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def validate_scope_state(self) -> "CreationInstructionActivationScope":
        if self.scope_key != (
            "application"
            if self.scope_type == "application"
            else self.scope_key
        ):
            raise ValueError("application activation scope must use its canonical key")
        if self.scope_type == "project" and not self.scope_key.startswith("project:"):
            raise ValueError("project activation scope must use its canonical key")
        if (self.activation_receipt_id is None) != (
            self.activation_receipt_hash is None
        ):
            raise ValueError("activation receipt identity and hash must pair")
        if self.configured_state == "unconfigured":
            if (
                self.state_revision != 0
                or self.activation_receipt_id is not None
                or self.configured_version_id is not None
            ):
                raise ValueError("unconfigured activation scopes cannot claim evidence")
        elif self.state_revision < 1 or self.activation_receipt_id is None:
            raise ValueError("configured activation scopes require a decision receipt")
        if (self.configured_state == "enabled") != (
            self.configured_version_id is not None
        ):
            raise ValueError("only enabled activation scopes bind a version")
        if self.scope_type == "application" and self.configured_state in {
            "unconfigured",
            "inherited",
        }:
            raise ValueError("application activation scopes cannot inherit")
        return self


class CreationInstructionActivationBinding(CreationInstructionModel):
    schema_id: Literal["zhimeng.creation-instruction-activation.v1"] = Field(
        alias="schema"
    )
    activation_binding_id: str = Field(
        pattern=r"^creation-instruction-activation-[0-9a-f]{32}$"
    )
    run_id: StableId
    project_id: StableId
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    skill_id: Literal["novel-creation-suite.writer-instruction"]
    scope_chain: tuple[CreationInstructionActivationScope, ...] = Field(
        min_length=1,
        max_length=2,
    )
    effective_scope: Literal["application", "project"]
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    manifest_fingerprint: Sha256
    package_fingerprint: Sha256
    instruction_map_path: Literal["instruction-map.json"]
    instruction_map_hash: Sha256
    created_at: datetime
    binding_hash: Sha256

    @model_validator(mode="after")
    def validate_binding(self) -> "CreationInstructionActivationBinding":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("instruction activation binding time must be timezone-aware")
        if self.scope_chain[0].scope_type != "project":
            raise ValueError("instruction activation scope chain must start at project scope")
        effective = self.scope_chain[-1]
        if effective.scope_type != self.effective_scope:
            raise ValueError("instruction activation effective scope does not match its chain")
        if effective.configured_state != "enabled":
            raise ValueError("instruction activation effective scope must be enabled")
        if effective.configured_version_id != self.skill_version_id:
            raise ValueError("instruction activation effective version does not match")
        if self.effective_scope == "project":
            if len(self.scope_chain) != 1:
                raise ValueError("project activation cannot include an application fallback")
        else:
            if (
                len(self.scope_chain) != 2
                or self.scope_chain[1].scope_type != "application"
                or self.scope_chain[0].configured_state
                not in {"unconfigured", "inherited"}
            ):
                raise ValueError("application activation requires a project fallback state")
        expected_id = creation_instruction_activation_binding_id(
            run_id=self.run_id,
            workflow_plan_id=self.workflow_plan_id,
            workflow_plan_hash=self.workflow_plan_hash,
            scope_chain=self.scope_chain,
            skill_version_id=self.skill_version_id,
            package_fingerprint=self.package_fingerprint,
            instruction_map_hash=self.instruction_map_hash,
        )
        if self.activation_binding_id != expected_id:
            raise ValueError("instruction activation binding identity does not match")
        if self.binding_hash != creation_instruction_activation_binding_hash(self):
            raise ValueError("instruction activation binding hash does not match")
        return self


class CreationInstructionMapBlock(CreationInstructionModel):
    block_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    title: str = Field(min_length=1, max_length=256)
    line_start: int = Field(ge=1, strict=True)
    line_end: int = Field(ge=1, strict=True)
    include: bool
    exclusion_reason: str | None = Field(default=None, min_length=1, max_length=512)
    content_hash: Sha256

    @model_validator(mode="after")
    def validate_map_block(self) -> "CreationInstructionMapBlock":
        if self.line_end < self.line_start:
            raise ValueError("instruction map block line range is reversed")
        if self.include == (self.exclusion_reason is not None):
            raise ValueError("only excluded instruction blocks require a reason")
        return self


class CreationInstructionMap(CreationInstructionModel):
    schema_id: Literal["zhimeng.creation-instruction-map.v1"] = Field(alias="schema")
    skill_id: Literal["novel-creation-suite.writer-instruction"]
    source_path: str = Field(min_length=1, max_length=1024)
    source_hash: Sha256
    source_line_count: int = Field(ge=1, strict=True)
    max_included_utf8_bytes: int = Field(ge=1, le=200_000, strict=True)
    blocks: tuple[CreationInstructionMapBlock, ...] = Field(
        min_length=2,
        max_length=64,
    )

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        if "\\" in value:
            raise ValueError("instruction source paths must use forward slashes")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("instruction source path must remain inside its package")
        return value

    @model_validator(mode="after")
    def validate_map_coverage(self) -> "CreationInstructionMap":
        if len({block.block_id for block in self.blocks}) != len(self.blocks):
            raise ValueError("instruction map block identities must be unique")
        expected_start = 1
        for block in self.blocks:
            if block.line_start != expected_start:
                raise ValueError("instruction map blocks must cover contiguous source lines")
            expected_start = block.line_end + 1
        if expected_start != self.source_line_count + 1:
            raise ValueError("instruction map blocks must classify the complete source")
        if not any(block.include for block in self.blocks):
            raise ValueError("instruction map must include at least one source block")
        if not any(not block.include for block in self.blocks):
            raise ValueError("instruction map must record excluded source blocks")
        return self


class CreationInstructionBlock(CreationInstructionModel):
    block_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    title: str = Field(min_length=1, max_length=256)
    source_path: str = Field(min_length=1, max_length=1024)
    source_line_start: int = Field(ge=1, strict=True)
    source_line_end: int = Field(ge=1, strict=True)
    source_byte_start: int = Field(ge=0, strict=True)
    source_byte_end: int = Field(ge=1, strict=True)
    utf8_bytes: int = Field(ge=1, strict=True)
    content_hash: Sha256
    included: bool
    content: str | None = None
    exclusion_reason: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_materialized_block(self) -> "CreationInstructionBlock":
        if self.source_line_end < self.source_line_start:
            raise ValueError("instruction block line range is reversed")
        if self.source_byte_end <= self.source_byte_start:
            raise ValueError("instruction block byte range is empty")
        if self.source_byte_end - self.source_byte_start != self.utf8_bytes:
            raise ValueError("instruction block byte range does not match its byte count")
        if self.included:
            if self.content is None or self.exclusion_reason is not None:
                raise ValueError("included instruction blocks require content only")
            encoded = self.content.encode("utf-8")
            if len(encoded) != self.utf8_bytes:
                raise ValueError("instruction block content byte count does not match")
            if hashlib.sha256(encoded).hexdigest() != self.content_hash:
                raise ValueError("instruction block content hash does not match")
        elif self.content is not None or self.exclusion_reason is None:
            raise ValueError("excluded instruction blocks require only an exclusion reason")
        return self


class CreationInstructionMaterialization(CreationInstructionModel):
    schema_id: Literal["zhimeng.creation-instruction-materialization.v1"] = Field(
        alias="schema"
    )
    materialization_id: str = Field(
        pattern=r"^creation-instruction-materialization-[0-9a-f]{32}$"
    )
    run_id: StableId
    project_id: StableId
    project_revision: int = Field(ge=1, strict=True)
    project_fingerprint: Sha256
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    final_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    final_checkpoint_hash: Sha256
    writer_task_id: str = Field(pattern=r"^writer-task-[0-9a-f]{32}$")
    writer_task_hash: Sha256
    activation_binding_id: str = Field(
        pattern=r"^creation-instruction-activation-[0-9a-f]{32}$"
    )
    activation_binding_hash: Sha256
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    manifest_fingerprint: Sha256
    package_fingerprint: Sha256
    instruction_map_path: Literal["instruction-map.json"]
    instruction_map_hash: Sha256
    source_path: str = Field(min_length=1, max_length=1024)
    source_hash: Sha256
    blocks: tuple[CreationInstructionBlock, ...] = Field(min_length=2, max_length=64)
    blocks_hash: Sha256
    included_block_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    provider_instruction: str = Field(min_length=1, max_length=200_000)
    provider_instruction_hash: Sha256
    provider_instruction_utf8_bytes: int = Field(ge=1, le=200_000, strict=True)
    max_included_utf8_bytes: int = Field(ge=1, le=200_000, strict=True)
    formal_draft_allowed: Literal[False]
    writes_project_truth: Literal[False]
    requires_author_review: Literal[True]
    created_at: datetime
    materialization_hash: Sha256

    @model_validator(mode="after")
    def validate_materialization(self) -> "CreationInstructionMaterialization":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("instruction materialization time must be timezone-aware")
        identities = tuple(block.block_id for block in self.blocks)
        if len(identities) != len(set(identities)):
            raise ValueError("instruction materialization block identities must be unique")
        included = tuple(block.block_id for block in self.blocks if block.included)
        if self.included_block_ids != included:
            raise ValueError("included instruction block order does not match materialization")
        if self.blocks_hash != creation_instruction_blocks_hash(self.blocks):
            raise ValueError("instruction materialization blocks hash does not match")
        assembled = "".join(block.content or "" for block in self.blocks if block.included)
        if self.provider_instruction != assembled:
            raise ValueError("Provider instruction does not match its included blocks")
        encoded = self.provider_instruction.encode("utf-8")
        if len(encoded) != self.provider_instruction_utf8_bytes:
            raise ValueError("Provider instruction byte count does not match")
        if len(encoded) > self.max_included_utf8_bytes:
            raise ValueError("Provider instruction exceeds its frozen byte budget")
        if hashlib.sha256(encoded).hexdigest() != self.provider_instruction_hash:
            raise ValueError("Provider instruction hash does not match")
        expected_id = creation_instruction_materialization_id(
            run_id=self.run_id,
            writer_task_id=self.writer_task_id,
            activation_binding_id=self.activation_binding_id,
            blocks_hash=self.blocks_hash,
            provider_instruction_hash=self.provider_instruction_hash,
        )
        if self.materialization_id != expected_id:
            raise ValueError("instruction materialization identity does not match")
        if self.materialization_hash != creation_instruction_materialization_hash(self):
            raise ValueError("instruction materialization hash does not match")
        return self


class CreationInstructionDispatchReceipt(CreationInstructionModel):
    schema_id: Literal["zhimeng.creation-instruction-dispatch-receipt.v1"] = Field(
        alias="schema"
    )
    dispatch_receipt_id: str = Field(
        pattern=r"^creation-instruction-dispatch-[0-9a-f]{32}$"
    )
    run_id: StableId
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: Sha256
    final_checkpoint_id: str = Field(
        pattern=r"^workflow-checkpoint-[0-9a-f]{32}$"
    )
    final_checkpoint_hash: Sha256
    writer_task_id: str = Field(pattern=r"^writer-task-[0-9a-f]{32}$")
    writer_task_hash: Sha256
    materialization_id: str = Field(
        pattern=r"^creation-instruction-materialization-[0-9a-f]{32}$"
    )
    materialization_hash: Sha256
    provider_boundary_id: str = Field(pattern=r"^provider-io-[0-9a-f]{32}$")
    provider_boundary_hash: Sha256
    provider_request_hash: Sha256
    provider_messages_hash: Sha256
    tool_definitions_hash: Sha256
    provider_dispatch_config_fingerprint: Sha256
    automatic_provider_replay_count: Literal[0]
    status: Literal["committed_before_dispatch"]
    formal_draft_allowed: Literal[False]
    writes_project_truth: Literal[False]
    created_at: datetime
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_receipt(self) -> "CreationInstructionDispatchReceipt":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("instruction dispatch receipt time must be timezone-aware")
        expected_id = creation_instruction_dispatch_receipt_id(
            run_id=self.run_id,
            provider_boundary_id=self.provider_boundary_id,
            materialization_id=self.materialization_id,
        )
        if self.dispatch_receipt_id != expected_id:
            raise ValueError("instruction dispatch receipt identity does not match")
        if self.receipt_hash != creation_instruction_dispatch_receipt_hash(self):
            raise ValueError("instruction dispatch receipt hash does not match")
        return self


def _derived_id(prefix: str, material: object) -> str:
    return f"{prefix}-{canonical_json_hash(material)[:32]}"


def creation_instruction_activation_binding_id(
    *,
    run_id: str,
    workflow_plan_id: str,
    workflow_plan_hash: str,
    scope_chain: tuple[CreationInstructionActivationScope, ...],
    skill_version_id: str,
    package_fingerprint: str,
    instruction_map_hash: str,
) -> str:
    return _derived_id(
        "creation-instruction-activation",
        {
            "schema": "zhimeng.creation-instruction-activation.v1",
            "run_id": run_id,
            "workflow_plan_id": workflow_plan_id,
            "workflow_plan_hash": workflow_plan_hash,
            "scope_chain": [scope.model_dump(mode="json") for scope in scope_chain],
            "skill_version_id": skill_version_id,
            "package_fingerprint": package_fingerprint,
            "instruction_map_hash": instruction_map_hash,
        },
    )


def creation_instruction_activation_binding_hash(
    binding: CreationInstructionActivationBinding,
) -> str:
    return canonical_json_hash(
        binding.model_dump(mode="json", by_alias=True, exclude={"binding_hash"})
    )


def build_creation_instruction_activation_binding(
    *,
    run_id: str,
    project_id: str,
    workflow_plan_id: str,
    workflow_plan_hash: str,
    scope_chain: tuple[CreationInstructionActivationScope, ...],
    effective_scope: Literal["application", "project"],
    skill_version_id: str,
    skill_version: str,
    manifest_fingerprint: str,
    package_fingerprint: str,
    instruction_map_hash: str,
    created_at: datetime,
) -> CreationInstructionActivationBinding:
    binding_id = creation_instruction_activation_binding_id(
        run_id=run_id,
        workflow_plan_id=workflow_plan_id,
        workflow_plan_hash=workflow_plan_hash,
        scope_chain=scope_chain,
        skill_version_id=skill_version_id,
        package_fingerprint=package_fingerprint,
        instruction_map_hash=instruction_map_hash,
    )
    material = {
        "schema_id": "zhimeng.creation-instruction-activation.v1",
        "activation_binding_id": binding_id,
        "run_id": run_id,
        "project_id": project_id,
        "workflow_plan_id": workflow_plan_id,
        "workflow_plan_hash": workflow_plan_hash,
        "skill_id": CREATION_INSTRUCTION_SKILL_ID,
        "scope_chain": scope_chain,
        "effective_scope": effective_scope,
        "skill_version_id": skill_version_id,
        "skill_version": skill_version,
        "manifest_fingerprint": manifest_fingerprint,
        "package_fingerprint": package_fingerprint,
        "instruction_map_path": CREATION_INSTRUCTION_MAP_PATH,
        "instruction_map_hash": instruction_map_hash,
        "created_at": created_at,
    }
    draft = CreationInstructionActivationBinding.model_construct(
        **material,
        binding_hash="0" * 64,
    )
    return CreationInstructionActivationBinding(
        **material,
        binding_hash=creation_instruction_activation_binding_hash(draft),
    )


def parse_creation_instruction_map(payload: bytes) -> CreationInstructionMap:
    try:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("creation instruction map must be a JSON object")
        return CreationInstructionMap.model_validate_json(payload, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("creation instruction map is not valid UTF-8 JSON") from exc


def creation_instruction_blocks_hash(
    blocks: tuple[CreationInstructionBlock, ...],
) -> str:
    return canonical_json_hash(
        [block.model_dump(mode="json", by_alias=True) for block in blocks]
    )


def creation_instruction_materialization_id(
    *,
    run_id: str,
    writer_task_id: str,
    activation_binding_id: str,
    blocks_hash: str,
    provider_instruction_hash: str,
) -> str:
    return _derived_id(
        "creation-instruction-materialization",
        {
            "schema": "zhimeng.creation-instruction-materialization.v1",
            "run_id": run_id,
            "writer_task_id": writer_task_id,
            "activation_binding_id": activation_binding_id,
            "blocks_hash": blocks_hash,
            "provider_instruction_hash": provider_instruction_hash,
        },
    )


def creation_instruction_materialization_hash(
    materialization: CreationInstructionMaterialization,
) -> str:
    return canonical_json_hash(
        materialization.model_dump(
            mode="json",
            by_alias=True,
            exclude={"materialization_hash"},
        )
    )


def build_creation_instruction_materialization(
    binding: CreationInstructionActivationBinding,
    writer_task: "WriterTask",
    *,
    instruction_map_bytes: bytes,
    source_bytes: bytes,
    created_at: datetime,
) -> CreationInstructionMaterialization:
    if (
        writer_task.run_id != binding.run_id
        or writer_task.project_id != binding.project_id
        or writer_task.workflow_plan_id != binding.workflow_plan_id
        or writer_task.workflow_plan_hash != binding.workflow_plan_hash
    ):
        raise ValueError("instruction activation does not bind the Writer task")
    if hashlib.sha256(instruction_map_bytes).hexdigest() != binding.instruction_map_hash:
        raise ValueError("instruction map changed after Run activation was frozen")
    instruction_map = parse_creation_instruction_map(instruction_map_bytes)
    if instruction_map.skill_id != binding.skill_id:
        raise ValueError("instruction map Skill identity does not match activation")
    if hashlib.sha256(source_bytes).hexdigest() != instruction_map.source_hash:
        raise ValueError("instruction source bytes do not match the managed map")
    lines = source_bytes.splitlines(keepends=True)
    if len(lines) != instruction_map.source_line_count:
        raise ValueError("instruction source line count does not match the managed map")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    materialized: list[CreationInstructionBlock] = []
    for block in instruction_map.blocks:
        byte_start = offsets[block.line_start - 1]
        byte_end = offsets[block.line_end]
        raw = source_bytes[byte_start:byte_end]
        if hashlib.sha256(raw).hexdigest() != block.content_hash:
            raise ValueError(f"instruction source block {block.block_id} changed")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("instruction source block is not valid UTF-8") from exc
        materialized.append(
            CreationInstructionBlock(
                block_id=block.block_id,
                title=block.title,
                source_path=instruction_map.source_path,
                source_line_start=block.line_start,
                source_line_end=block.line_end,
                source_byte_start=byte_start,
                source_byte_end=byte_end,
                utf8_bytes=len(raw),
                content_hash=block.content_hash,
                included=block.include,
                content=content if block.include else None,
                exclusion_reason=block.exclusion_reason,
            )
        )

    blocks = tuple(materialized)
    blocks_hash = creation_instruction_blocks_hash(blocks)
    included_ids = tuple(block.block_id for block in blocks if block.included)
    provider_instruction = "".join(
        block.content or "" for block in blocks if block.included
    )
    provider_bytes = provider_instruction.encode("utf-8")
    if len(provider_bytes) > instruction_map.max_included_utf8_bytes:
        raise ValueError("materialized instruction exceeds its managed byte budget")
    provider_hash = hashlib.sha256(provider_bytes).hexdigest()
    materialization_id = creation_instruction_materialization_id(
        run_id=writer_task.run_id,
        writer_task_id=writer_task.writer_task_id,
        activation_binding_id=binding.activation_binding_id,
        blocks_hash=blocks_hash,
        provider_instruction_hash=provider_hash,
    )
    material = {
        "schema_id": "zhimeng.creation-instruction-materialization.v1",
        "materialization_id": materialization_id,
        "run_id": writer_task.run_id,
        "project_id": writer_task.project_id,
        "project_revision": writer_task.project_revision,
        "project_fingerprint": writer_task.project_fingerprint,
        "workflow_plan_id": writer_task.workflow_plan_id,
        "workflow_plan_hash": writer_task.workflow_plan_hash,
        "final_checkpoint_id": writer_task.final_checkpoint_id,
        "final_checkpoint_hash": writer_task.final_checkpoint_hash,
        "writer_task_id": writer_task.writer_task_id,
        "writer_task_hash": writer_task.task_hash,
        "activation_binding_id": binding.activation_binding_id,
        "activation_binding_hash": binding.binding_hash,
        "skill_version_id": binding.skill_version_id,
        "manifest_fingerprint": binding.manifest_fingerprint,
        "package_fingerprint": binding.package_fingerprint,
        "instruction_map_path": binding.instruction_map_path,
        "instruction_map_hash": binding.instruction_map_hash,
        "source_path": instruction_map.source_path,
        "source_hash": instruction_map.source_hash,
        "blocks": blocks,
        "blocks_hash": blocks_hash,
        "included_block_ids": included_ids,
        "provider_instruction": provider_instruction,
        "provider_instruction_hash": provider_hash,
        "provider_instruction_utf8_bytes": len(provider_bytes),
        "max_included_utf8_bytes": instruction_map.max_included_utf8_bytes,
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "requires_author_review": True,
        "created_at": created_at,
    }
    draft = CreationInstructionMaterialization.model_construct(
        **material,
        materialization_hash="0" * 64,
    )
    return CreationInstructionMaterialization(
        **material,
        materialization_hash=creation_instruction_materialization_hash(draft),
    )


def creation_instruction_dispatch_receipt_id(
    *,
    run_id: str,
    provider_boundary_id: str,
    materialization_id: str,
) -> str:
    return _derived_id(
        "creation-instruction-dispatch",
        {
            "schema": "zhimeng.creation-instruction-dispatch-receipt.v1",
            "run_id": run_id,
            "provider_boundary_id": provider_boundary_id,
            "materialization_id": materialization_id,
        },
    )


def creation_instruction_dispatch_receipt_hash(
    receipt: CreationInstructionDispatchReceipt,
) -> str:
    return canonical_json_hash(
        receipt.model_dump(mode="json", by_alias=True, exclude={"receipt_hash"})
    )


def build_creation_instruction_dispatch_receipt(
    boundary: "ProviderIoBoundary",
    materialization: CreationInstructionMaterialization,
    *,
    provider_messages_hash: str,
    tool_definitions_hash: str,
    created_at: datetime,
) -> CreationInstructionDispatchReceipt:
    if (
        boundary.run_id != materialization.run_id
        or boundary.workflow_plan_id != materialization.workflow_plan_id
        or boundary.plan_hash != materialization.workflow_plan_hash
        or boundary.last_step_checkpoint_id != materialization.final_checkpoint_id
        or boundary.last_step_checkpoint_hash != materialization.final_checkpoint_hash
    ):
        raise ValueError("Provider boundary does not bind instruction materialization")
    receipt_id = creation_instruction_dispatch_receipt_id(
        run_id=boundary.run_id,
        provider_boundary_id=boundary.boundary_id,
        materialization_id=materialization.materialization_id,
    )
    material = {
        "schema_id": "zhimeng.creation-instruction-dispatch-receipt.v1",
        "dispatch_receipt_id": receipt_id,
        "run_id": boundary.run_id,
        "workflow_plan_id": boundary.workflow_plan_id,
        "workflow_plan_hash": boundary.plan_hash,
        "final_checkpoint_id": boundary.last_step_checkpoint_id,
        "final_checkpoint_hash": boundary.last_step_checkpoint_hash,
        "writer_task_id": materialization.writer_task_id,
        "writer_task_hash": materialization.writer_task_hash,
        "materialization_id": materialization.materialization_id,
        "materialization_hash": materialization.materialization_hash,
        "provider_boundary_id": boundary.boundary_id,
        "provider_boundary_hash": boundary.boundary_hash,
        "provider_request_hash": boundary.request_hash,
        "provider_messages_hash": provider_messages_hash,
        "tool_definitions_hash": tool_definitions_hash,
        "provider_dispatch_config_fingerprint": (
            boundary.provider_dispatch_config_fingerprint
        ),
        "automatic_provider_replay_count": 0,
        "status": "committed_before_dispatch",
        "formal_draft_allowed": False,
        "writes_project_truth": False,
        "created_at": created_at,
    }
    draft = CreationInstructionDispatchReceipt.model_construct(
        **material,
        receipt_hash="0" * 64,
    )
    return CreationInstructionDispatchReceipt(
        **material,
        receipt_hash=creation_instruction_dispatch_receipt_hash(draft),
    )


__all__ = [
    "CREATION_INSTRUCTION_MAP_PATH",
    "CREATION_INSTRUCTION_SKILL_ID",
    "CreationInstructionActivationBinding",
    "CreationInstructionActivationScope",
    "CreationInstructionBlock",
    "CreationInstructionDispatchReceipt",
    "CreationInstructionMap",
    "CreationInstructionMapBlock",
    "CreationInstructionMaterialization",
    "build_creation_instruction_activation_binding",
    "build_creation_instruction_dispatch_receipt",
    "build_creation_instruction_materialization",
    "creation_instruction_activation_binding_hash",
    "creation_instruction_blocks_hash",
    "creation_instruction_dispatch_receipt_hash",
    "creation_instruction_materialization_hash",
    "parse_creation_instruction_map",
]
