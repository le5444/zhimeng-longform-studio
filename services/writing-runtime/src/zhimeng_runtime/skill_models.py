from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from zhimeng_runtime.prewrite_models import PrewriteOperationPackage


SkillId = str
SkillRootId = str


class SkillApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SkillDiagnostic(SkillApiModel):
    severity: Literal["info", "warning", "error"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    message: str = Field(min_length=1, max_length=4096)
    relative_path: str | None = None


class SkillScanLimits(SkillApiModel):
    max_entries: int = Field(ge=1)
    max_directories: int = Field(ge=1)
    max_depth: int = Field(ge=0)
    max_manifests: int = Field(ge=1)
    max_manifest_bytes: int = Field(ge=1)
    max_total_manifest_bytes: int = Field(ge=1)
    excluded_directory_names: list[str]


class SkillRootInventory(SkillApiModel):
    directories_visited: int = Field(ge=0)
    entries_seen: int = Field(ge=0)
    excluded_directories: int = Field(ge=0)
    manifests_found: int = Field(ge=0)
    manifests_loaded: int = Field(ge=0)
    manifest_bytes_read: int = Field(ge=0)
    content_files_read: Literal[0] = 0
    content_bytes_read: Literal[0] = 0
    package_fingerprint: None = None
    package_fingerprint_status: Literal["not_scanned"] = "not_scanned"


class SkillSource(SkillApiModel):
    root_id: SkillRootId = Field(pattern=r"^skill-root-[0-9a-f]{24}$")
    root_priority: int = Field(ge=0)
    root_path: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)


class DiscoveredSkill(SkillApiModel):
    skill_id: SkillId = Field(pattern=r"^skill-[0-9a-f]{32}$")
    name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=16_384)
    source: SkillSource
    manifest_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    manifest_revision: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    revision_status: Literal["manifest_only", "unavailable"]
    declared_version: str | None = Field(default=None, max_length=128)
    effective_version: str | None = Field(default=None, max_length=128)
    version_status: Literal[
        "declared",
        "derived_from_manifest_fingerprint",
        "unavailable",
    ]
    validation_status: Literal["valid", "invalid"]
    readiness: Literal["discovered", "loadable"]
    execution_status: Literal["not_assessed"] = "not_assessed"
    package_fingerprint: None = None
    package_fingerprint_status: Literal["not_scanned"] = "not_scanned"
    diagnostics: list[SkillDiagnostic]

    @model_validator(mode="after")
    def validate_status_coherence(self) -> "DiscoveredSkill":
        if self.validation_status == "valid":
            if self.readiness != "loadable" or not self.name or not self.description:
                raise ValueError("valid skills must be loadable and have name/description")
            if self.manifest_fingerprint is None or self.manifest_revision is None:
                raise ValueError("valid skills must have a manifest fingerprint")
        elif self.readiness != "discovered":
            raise ValueError("invalid skills can only be marked discovered")

        if self.manifest_fingerprint is None:
            if self.manifest_revision is not None or self.revision_status != "unavailable":
                raise ValueError("unread manifests cannot expose a revision")
        elif (
            self.manifest_revision != f"sha256:{self.manifest_fingerprint}"
            or self.revision_status != "manifest_only"
        ):
            raise ValueError("manifest revision must bind exactly to the manifest hash")

        if self.version_status == "declared":
            if not self.declared_version or self.effective_version != self.declared_version:
                raise ValueError("declared version status requires the declared version")
        elif self.version_status == "derived_from_manifest_fingerprint":
            expected = (
                None
                if self.manifest_fingerprint is None
                else f"sha256:{self.manifest_fingerprint[:12]}"
            )
            if self.declared_version is not None or self.effective_version != expected:
                raise ValueError("derived versions must use the manifest fingerprint")
        elif self.effective_version is not None or self.declared_version is not None:
            raise ValueError("unavailable versions cannot expose a value")
        return self


class SkillRootSnapshot(SkillApiModel):
    root_id: SkillRootId = Field(pattern=r"^skill-root-[0-9a-f]{24}$")
    priority: int = Field(ge=0)
    display_name: str = Field(min_length=1)
    root_path: str = Field(min_length=1)
    scan_status: Literal["complete", "partial", "blocked"]
    manifest_set_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    inventory: SkillRootInventory
    diagnostics: list[SkillDiagnostic]


class SkillRegistryResponse(SkillApiModel):
    schema_id: Literal["zhimeng.skill-registry.v1"] = Field(
        default="zhimeng.skill-registry.v1",
        alias="schema",
    )
    read_only: Literal[True] = True
    configured: bool
    registry_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: SkillScanLimits
    roots: list[SkillRootSnapshot]
    skills: list[DiscoveredSkill]
    diagnostics: list[SkillDiagnostic]

    @model_validator(mode="after")
    def validate_references(self) -> "SkillRegistryResponse":
        root_ids = [root.root_id for root in self.roots]
        skill_ids = [skill.skill_id for skill in self.skills]
        if len(root_ids) != len(set(root_ids)):
            raise ValueError("skill root IDs must be unique")
        if len(skill_ids) != len(set(skill_ids)):
            raise ValueError("skill IDs must be unique")
        if any(skill.source.root_id not in root_ids for skill in self.skills):
            raise ValueError("discovered skill references an unknown source root")
        if self.configured != bool(self.roots):
            raise ValueError("configured must match the presence of trusted roots")
        return self


class ManagedSkillModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        strict=True,
    )


class SkillManifestSource(ManagedSkillModel):
    type: Literal["builtin_managed"]
    reference: str = Field(min_length=1, max_length=512)
    license: str | None = Field(max_length=256)
    source_url: str | None = Field(max_length=2048)
    source_version: str | None = Field(max_length=256)
    modified: bool


class SkillPermissions(ManagedSkillModel):
    read: list[Literal["bound_project_snapshot"]] = Field(max_length=1)
    write: list[Literal["run_staging"]] = Field(max_length=1)
    network: Literal[False]
    secrets: list[str] = Field(max_length=0)

    @model_validator(mode="after")
    def validate_fixed_capabilities(self) -> "SkillPermissions":
        valid_profiles = (
            (["bound_project_snapshot"], ["run_staging"]),
            ([], []),
        )
        if (self.read, self.write) not in valid_profiles:
            raise ValueError(
                "managed skills must use either deterministic execution permissions "
                "or the empty instruction-source permission profile"
            )
        return self


class SkillExitCodes(ManagedSkillModel):
    success: Literal["success"] = Field(alias="0")
    domain_blocked: Literal["domain_blocked"] = Field(alias="1")


class SkillManifest(ManagedSkillModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        strict=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"kind": {"const": "deterministic_tool"}},
                        "required": ["kind"],
                    },
                    "then": {
                        "required": [
                            "entrypoint",
                            "timeout_seconds",
                            "exit_codes",
                            "artifacts",
                            "input_schema",
                            "output_schema",
                        ],
                        "properties": {
                            "entrypoint": {"type": "string"},
                            "timeout_seconds": {"type": "integer"},
                            "exit_codes": {"type": "object"},
                            "artifacts": {"minItems": 1},
                            "input_schema": {"type": "string"},
                            "output_schema": {"type": "string"},
                            "permissions": {
                                "properties": {
                                    "read": {
                                        "const": ["bound_project_snapshot"]
                                    },
                                    "write": {"const": ["run_staging"]},
                                }
                            },
                        },
                    },
                },
                {
                    "if": {
                        "properties": {"kind": {"const": "instruction_source"}},
                        "required": ["kind"],
                    },
                    "then": {
                        "required": ["resources"],
                        "properties": {
                            "entrypoint": {"type": "null"},
                            "timeout_seconds": {"type": "null"},
                            "exit_codes": {"type": "null"},
                            "artifacts": {"maxItems": 0},
                            "input_schema": {"type": "null"},
                            "output_schema": {"type": "null"},
                            "resources": {
                                "contains": {"const": "instruction-map.json"},
                                "minContains": 1,
                                "uniqueItems": True,
                            },
                            "permissions": {
                                "properties": {
                                    "read": {"maxItems": 0},
                                    "write": {"maxItems": 0},
                                }
                            },
                        },
                    },
                },
            ]
        },
    )

    schema_id: Literal["zhimeng.skill-manifest.v1"] = Field(alias="schema")
    id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    display_name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=16_384)
    kind: Literal["deterministic_tool", "instruction_source"]
    version: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]*$",
    )
    source: SkillManifestSource
    entrypoint: str | None = Field(default=None, min_length=4, max_length=1024)
    approval: Literal["none"]
    permissions: SkillPermissions
    timeout_seconds: int | None = Field(default=None, ge=1, le=900)
    exit_codes: SkillExitCodes | None = None
    artifacts: list[str] = Field(default_factory=list, max_length=32)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    resources: list[str] = Field(default_factory=list, max_length=32)
    input_schema: str | None = Field(
        default=None,
        pattern=r"^https://zhimeng\.local/contracts/[A-Za-z0-9._/-]+\.schema\.json$",
        max_length=2048,
    )
    output_schema: str | None = Field(
        default=None,
        pattern=r"^https://zhimeng\.local/contracts/[A-Za-z0-9._/-]+\.schema\.json$",
        max_length=2048,
    )

    @model_validator(mode="after")
    def validate_manifest_paths_and_sets(self) -> "SkillManifest":
        from pathlib import PurePosixPath

        if self.kind == "deterministic_tool":
            if (
                self.entrypoint is None
                or self.timeout_seconds is None
                or self.exit_codes is None
                or self.input_schema is None
                or self.output_schema is None
                or not self.artifacts
            ):
                raise ValueError(
                    "deterministic tools require an entrypoint, timeout, exit codes, "
                    "artifacts, and input/output contracts"
                )
            if self.permissions.read != ["bound_project_snapshot"] or (
                self.permissions.write != ["run_staging"]
            ):
                raise ValueError(
                    "deterministic tools require the fixed execution permission profile"
                )
        else:
            if any(
                value is not None
                for value in (
                    self.entrypoint,
                    self.timeout_seconds,
                    self.exit_codes,
                    self.input_schema,
                    self.output_schema,
                )
            ) or self.artifacts:
                raise ValueError(
                    "instruction sources cannot declare execution fields or artifacts"
                )
            if self.permissions.read or self.permissions.write:
                raise ValueError(
                    "instruction sources require the empty permission profile"
                )
            if "instruction-map.json" not in self.resources:
                raise ValueError(
                    "instruction sources must declare instruction-map.json"
                )

        if self.entrypoint is not None:
            if "\\" in self.entrypoint:
                raise ValueError("entrypoint must use portable forward slashes")
            entrypoint = PurePosixPath(self.entrypoint)
            if (
                entrypoint.is_absolute()
                or not entrypoint.parts
                or any(part in {"", ".", ".."} for part in entrypoint.parts)
                or entrypoint.suffix.lower() != ".py"
            ):
                raise ValueError("entrypoint must be a package-relative Python file")
        if len(self.artifacts) != len(set(self.artifacts)):
            raise ValueError("artifact declarations must be unique")
        if any(
            not value
            or len(value) > 128
            or not value[0].isalnum()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in value)
            for value in self.artifacts
        ):
            raise ValueError("artifact declarations must use portable lowercase IDs")
        if self.capabilities != sorted(set(self.capabilities)):
            raise ValueError(
                "capability declarations must be unique and sorted"
            )
        if any(
            not value
            or len(value) > 128
            or not value[0].isalnum()
            or not value[-1].isalnum()
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
                for character in value
            )
            for value in self.capabilities
        ):
            raise ValueError(
                "capability declarations must use portable lowercase IDs"
            )
        if self.resources != sorted(set(self.resources)):
            raise ValueError("resource declarations must be unique and sorted")
        for resource in self.resources:
            if "\\" in resource:
                raise ValueError("resource paths must use portable forward slashes")
            path = PurePosixPath(resource)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("resources must be package-relative files")
        for contract_uri in (self.input_schema, self.output_schema):
            if contract_uri is None:
                continue
            suffix = contract_uri.removeprefix("https://zhimeng.local/contracts/")
            if ".." in PurePosixPath(suffix).parts:
                raise ValueError("contract URIs cannot traverse outside the contract root")
        return self


class ManagedSkillCatalogEntry(ManagedSkillModel):
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    manifest: SkillManifest
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active"]


class ManagedSkillCatalogResponse(ManagedSkillModel):
    schema_id: Literal["zhimeng.managed-skill-catalog.v1"] = Field(alias="schema")
    project_id: str = Field(min_length=1, max_length=256)
    skills: list[ManagedSkillCatalogEntry]


class ManagedSkillScope(ManagedSkillModel):
    scope_type: Literal["application", "project"]
    project_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_scope(self) -> "ManagedSkillScope":
        if (self.scope_type == "project") != (self.project_id is not None):
            raise ValueError("project Skill scopes require exactly one project_id")
        return self


class ManagedSkillSourceRecord(ManagedSkillModel):
    kind: Literal["builtin_bundle", "trusted_import"]
    reference: str = Field(min_length=1, max_length=512)
    locator_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedSkillAdaptationRecord(ManagedSkillModel):
    adapter_id: Literal["zhimeng.managed-package"]
    adapter_version: Literal["1"]
    adapter_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["validated"]


class ManagedSkillValidationRecord(ManagedSkillModel):
    status: Literal["passed"]
    contract_status: Literal["passed", "not_applicable"]
    isolation_status: Literal["passed"]
    input_contract_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    output_contract_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    contract_bundle_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    permissions: SkillPermissions

    @model_validator(mode="after")
    def validate_contract_identity(self) -> "ManagedSkillValidationRecord":
        fingerprints = (
            self.input_contract_fingerprint,
            self.output_contract_fingerprint,
            self.contract_bundle_fingerprint,
        )
        if self.contract_status == "passed" and any(
            value is None for value in fingerprints
        ):
            raise ValueError("passed contract validation requires all fingerprints")
        if self.contract_status == "not_applicable" and any(
            value is not None for value in fingerprints
        ):
            raise ValueError(
                "non-executable instruction sources cannot expose contract fingerprints"
            )
        return self


class ManagedSkillVersionRecord(ManagedSkillModel):
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    manifest: SkillManifest
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    total_bytes: int = Field(ge=1)
    source: ManagedSkillSourceRecord
    adaptation: ManagedSkillAdaptationRecord
    validation: ManagedSkillValidationRecord
    imported_at: datetime


class ManagedSkillUnavailableVersionRecord(ManagedSkillModel):
    """Historical import retained in the database after its snapshot disappeared."""

    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    version: str = Field(min_length=1, max_length=128)
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_at: datetime
    reason: Literal["snapshot_missing"] = "snapshot_missing"


class ManagedSkillActivationRecord(ManagedSkillModel):
    scope: ManagedSkillScope
    skill_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$",
    )
    configured_state: Literal["unconfigured", "inherited", "enabled", "disabled"]
    configured_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    state_revision: int = Field(ge=0)
    effective_state: Literal["enabled", "disabled"]
    effective_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    effective_scope: Literal["application", "project", "none"]
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_activation(self) -> "ManagedSkillActivationRecord":
        if (self.configured_state == "enabled") != (
            self.configured_version_id is not None
        ):
            raise ValueError("only enabled Skill activations bind a configured version")
        if (self.effective_state == "enabled") != (
            self.effective_version_id is not None
        ):
            raise ValueError("only effective enabled Skills bind a version")
        if self.effective_scope == "none" and self.effective_state != "disabled":
            raise ValueError("an unconfigured effective scope cannot be enabled")
        if self.scope.scope_type == "application" and self.configured_state in {
            "inherited",
            "unconfigured",
        } and self.state_revision != 0:
            raise ValueError("application Skill activations cannot inherit")
        return self


class ManagedSkillLifecycleCatalogResponse(ManagedSkillModel):
    schema_id: Literal["zhimeng.managed-skill-lifecycle-catalog.v1"] = Field(
        alias="schema"
    )
    scope: ManagedSkillScope
    versions: list[ManagedSkillVersionRecord]
    unavailable_versions: list[ManagedSkillUnavailableVersionRecord] = Field(
        default_factory=list
    )
    activations: list[ManagedSkillActivationRecord]


class ManagedSkillImportRequest(ManagedSkillModel):
    source_path: str = Field(min_length=1, max_length=4096)
    expected_package_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ManagedSkillImportReceipt(ManagedSkillModel):
    receipt_id: str = Field(pattern=r"^skill-import-[0-9a-f]{32}$")
    scope: ManagedSkillScope
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    source: ManagedSkillSourceRecord
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    total_bytes: int = Field(ge=1)
    adaptation: ManagedSkillAdaptationRecord
    validation: ManagedSkillValidationRecord
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ManagedSkillImportResponse(ManagedSkillModel):
    schema_id: Literal["zhimeng.managed-skill-import.v1"] = Field(alias="schema")
    imported: bool
    version: ManagedSkillVersionRecord
    activation: ManagedSkillActivationRecord
    receipt: ManagedSkillImportReceipt


class ManagedSkillActivationRequest(ManagedSkillModel):
    action: Literal["enable", "disable", "upgrade", "rollback", "inherit"]
    skill_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    expected_state_revision: int = Field(ge=0)
    expected_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def validate_action_target(self) -> "ManagedSkillActivationRequest":
        needs_version = self.action in {"enable", "upgrade", "rollback"}
        if needs_version != (self.skill_version_id is not None):
            raise ValueError(f"{self.action} has an invalid Skill version target")
        return self


class ManagedSkillActivationReceipt(ManagedSkillModel):
    receipt_id: str = Field(pattern=r"^skill-activation-[0-9a-f]{32}$")
    scope: ManagedSkillScope
    skill_id: str = Field(min_length=3, max_length=128)
    action: Literal[
        "builtin_bootstrap",
        "enable",
        "disable",
        "upgrade",
        "rollback",
        "inherit",
    ]
    previous_state: Literal["unconfigured", "inherited", "enabled", "disabled"]
    previous_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    next_state: Literal["inherited", "enabled", "disabled"]
    next_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    expected_state_revision: int = Field(ge=0)
    expected_version_id: str | None = Field(
        default=None,
        pattern=r"^skill-version-[0-9a-f]{32}$",
    )
    state_revision: int = Field(ge=1)
    receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime

    @model_validator(mode="after")
    def validate_receipt_versions(self) -> "ManagedSkillActivationReceipt":
        if (self.previous_state == "enabled") != (
            self.previous_version_id is not None
        ):
            raise ValueError("previous enabled state must bind its version")
        if (self.next_state == "enabled") != (self.next_version_id is not None):
            raise ValueError("next enabled state must bind its version")
        if self.expected_state_revision + 1 != self.state_revision:
            raise ValueError("Skill activation receipt revision must advance exactly once")
        if self.expected_version_id != self.previous_version_id:
            raise ValueError("Skill activation receipt must bind the prior version")
        return self


class ManagedSkillActivationResponse(ManagedSkillModel):
    schema_id: Literal["zhimeng.managed-skill-activation.v1"] = Field(
        alias="schema"
    )
    activation: ManagedSkillActivationRecord
    receipt: ManagedSkillActivationReceipt


class SkillArtifactOutput(ManagedSkillModel):
    artifact_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    relative_path: str | None = Field(default=None, max_length=1024)
    payload: JsonValue = None

    @model_validator(mode="after")
    def validate_artifact_content(self) -> "SkillArtifactOutput":
        from pathlib import PurePosixPath

        if self.relative_path is None and self.payload is None:
            raise ValueError("an artifact must provide a relative path or payload")
        if self.relative_path is not None:
            if "\\" in self.relative_path:
                raise ValueError("artifact paths must use portable forward slashes")
            path = PurePosixPath(self.relative_path)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("artifact paths must remain inside run staging")
        return self


class CreationProjectAuditRequest(ManagedSkillModel):
    schema_id: Literal["zhimeng.creation-project-audit-request.v1"] = Field(
        default="zhimeng.creation-project-audit-request.v1",
        alias="schema",
    )


class CreationProjectAuditCounts(ManagedSkillModel):
    p0: int = Field(alias="P0", ge=0)
    p1: int = Field(alias="P1", ge=0)
    p2: int = Field(alias="P2", ge=0)


class CreationProjectAuditFinding(ManagedSkillModel):
    severity: Literal["P0", "P1", "P2"]
    code: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=4096)
    message: str = Field(min_length=1, max_length=16_384)
    route: str = Field(min_length=1, max_length=4096)
    details: dict[str, JsonValue] | None = None


class CreationProjectAuditResult(ManagedSkillModel):
    project: str = Field(min_length=1, max_length=4096)
    health: Literal["healthy", "degraded", "blocked"]
    counts: CreationProjectAuditCounts
    expected_files: int = Field(ge=0)
    chapter_files: int = Field(ge=0)
    issue_cards: int = Field(ge=0)
    continuation_gate_results: list[JsonValue]
    ai_smell_reports: list[JsonValue]
    ai_smell_bridge_proofs: list[JsonValue]
    ai_smell_gate_results: list[JsonValue]
    findings: list[CreationProjectAuditFinding]
    next_actions: list[str]


class CreationPrewriteGateRequest(ManagedSkillModel):
    schema_id: Literal["zhimeng.creation-prewrite-gate-request.v1"] = Field(
        default="zhimeng.creation-prewrite-gate-request.v1",
        alias="schema",
    )
    operation_package: PrewriteOperationPackage
    project_audit_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_skill_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_tool_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_domain_status: Literal["healthy", "degraded", "blocked"]
    project_audit_finding_count: int = Field(ge=0)


class CreationPrewriteGateCheck(ManagedSkillModel):
    code: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    status: Literal["passed", "warning", "blocked"]
    message: str = Field(min_length=1, max_length=4096)
    evidence_refs: list[str] = Field(max_length=64)


class CreationPrewriteGateResult(ManagedSkillModel):
    schema_id: Literal["zhimeng.creation-prewrite-gate-result.v1"] = Field(
        default="zhimeng.creation-prewrite-gate-result.v1",
        alias="schema",
    )
    gate_status: Literal["blocked", "risk_draft_only"]
    formal_draft_allowed: Literal[False] = False
    allowed_output: Literal["repair_plan_or_risk_draft_only"] = (
        "repair_plan_or_risk_draft_only"
    )
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks: list[CreationPrewriteGateCheck] = Field(min_length=1, max_length=64)
    failure_signatures: list[str] = Field(max_length=64)
    next_actions: list[str] = Field(max_length=64)

    @model_validator(mode="after")
    def validate_gate_result(self) -> CreationPrewriteGateResult:
        codes = [check.code for check in self.checks]
        if len(codes) != len(set(codes)):
            raise ValueError("prewrite gate check codes must be unique")
        if len(self.failure_signatures) != len(set(self.failure_signatures)):
            raise ValueError("prewrite failure signatures must be unique")
        if self.gate_status == "blocked" and not any(
            check.status == "blocked" for check in self.checks
        ):
            raise ValueError("blocked prewrite results require a blocked check")
        return self


class CreationHandoffRequest(ManagedSkillModel):
    schema_id: Literal["zhimeng.creation-handoff-request.v1"] = Field(
        default="zhimeng.creation-handoff-request.v1",
        alias="schema",
    )
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    operation_package: PrewriteOperationPackage
    project_audit_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_skill_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_tool_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_audit_domain_status: Literal["healthy", "degraded", "blocked"]
    project_audit_finding_count: int = Field(ge=0)
    prewrite_gate_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prewrite_gate_skill_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prewrite_gate_tool_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prewrite_gate_checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prewrite_gate_domain_status: Literal["healthy", "degraded", "blocked"]
    prewrite_gate_status: Literal["blocked", "risk_draft_only"]
    prewrite_gate_formal_draft_allowed: Literal[False] = False
    prewrite_gate_failure_signatures: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_handoff_request(self) -> "CreationHandoffRequest":
        if len(self.prewrite_gate_failure_signatures) != len(
            set(self.prewrite_gate_failure_signatures)
        ):
            raise ValueError("handoff failure signatures must be unique")
        if self.prewrite_gate_status == "blocked" and not self.prewrite_gate_failure_signatures:
            raise ValueError("blocked handoff requests require failure signatures")
        return self


class CreationHandoffResult(ManagedSkillModel):
    schema_id: Literal["zhimeng.creation-handoff-result.v1"] = Field(
        default="zhimeng.creation-handoff-result.v1",
        alias="schema",
    )
    handoff_status: Literal["blocked", "risk_draft_only"]
    formal_draft_allowed: Literal[False] = False
    allowed_output: Literal["repair_plan_or_risk_draft_only"] = (
        "repair_plan_or_risk_draft_only"
    )
    workflow_plan_id: str = Field(pattern=r"^workflow-plan-[0-9a-f]{32}$")
    workflow_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_pack_id: str = Field(min_length=1, max_length=128)
    context_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_receipt_id: str = Field(pattern=r"^nlcl-route-[0-9a-f]{32}$")
    route_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_package_id: str = Field(pattern=r"^prewrite-package-[0-9a-f]{32}$")
    operation_package_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=128)
    project_revision: int = Field(ge=1)
    project_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_chapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    task_goal: str = Field(min_length=1, max_length=100_000)
    knowledge_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evidence_ref_count: int = Field(ge=0)
    must_obey_truth_count: int = Field(ge=0)
    current_character_state_count: int = Field(ge=0)
    project_audit_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prewrite_gate_execution_receipt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_checkpoint_hashes: list[str] = Field(min_length=2, max_length=32)
    prewrite_gate_status: Literal["blocked", "risk_draft_only"]
    failure_signatures: list[str] = Field(default_factory=list, max_length=64)
    next_actions: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_handoff_result(self) -> "CreationHandoffResult":
        if len(self.workflow_checkpoint_hashes) != len(set(self.workflow_checkpoint_hashes)):
            raise ValueError("handoff checkpoint hashes must be unique")
        if any(len(value) != 64 for value in self.workflow_checkpoint_hashes):
            raise ValueError("handoff checkpoint hashes must be SHA-256 values")
        if self.handoff_status != self.prewrite_gate_status:
            raise ValueError("handoff status must mirror the prewrite gate status")
        if self.handoff_status == "blocked" and not self.failure_signatures:
            raise ValueError("blocked handoff results require failure signatures")
        if self.current_character_state_count > self.must_obey_truth_count:
            raise ValueError("character-state refs must be a subset of truth refs")
        if self.must_obey_truth_count > self.evidence_ref_count:
            raise ValueError("truth refs must be a subset of evidence refs")
        return self


class DeterministicSkillOutput(ManagedSkillModel):
    health: Literal["healthy", "degraded", "blocked"]
    summary: str = Field(min_length=1, max_length=16_384)
    result: JsonValue
    artifacts: list[SkillArtifactOutput] = Field(max_length=128)


class SkillExecutionError(ManagedSkillModel):
    code: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    message: str = Field(min_length=1, max_length=4096)
    retryable: bool


class SkillArtifactReceipt(ManagedSkillModel):
    artifact_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    relative_path: str | None = Field(default=None, max_length=1024)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillExecutionReceipt(ManagedSkillModel):
    schema_id: Literal["zhimeng.skill-execution-receipt.v1"] = Field(alias="schema")
    receipt_id: str = Field(pattern=r"^skill-receipt-[0-9a-f]{32}$")
    run_id: str = Field(min_length=1, max_length=256)
    tool_call_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=256)
    project_revision: int = Field(ge=1)
    project_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_version_id: str = Field(pattern=r"^skill-version-[0-9a-f]{32}$")
    skill_id: str = Field(min_length=3, max_length=128)
    skill_version: str = Field(min_length=1, max_length=128)
    skill_kind: Literal["deterministic_tool", "instruction_source"]
    manifest_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    permissions: SkillPermissions
    permissions_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_status: Literal["succeeded", "failed", "cancelled"]
    domain_status: Literal["healthy", "degraded", "blocked"] | None
    exit_code: int | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    artifacts: list[SkillArtifactReceipt]
    error: SkillExecutionError | None

    @model_validator(mode="after")
    def validate_execution_outcome(self) -> "SkillExecutionReceipt":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.execution_status == "succeeded":
            if self.error is not None or self.domain_status is None:
                raise ValueError("successful execution requires a domain status and no error")
            if self.exit_code == 0 and self.domain_status not in {"healthy", "degraded"}:
                raise ValueError("exit 0 only represents healthy or degraded results")
            if self.exit_code == 1 and self.domain_status != "blocked":
                raise ValueError("exit 1 only represents a blocked domain result")
            if self.exit_code not in {0, 1}:
                raise ValueError("successful execution requires a declared exit code")
        else:
            if self.domain_status is not None or self.error is None:
                raise ValueError("unsuccessful execution requires an error and no domain status")
            if self.execution_status == "cancelled" and self.error.code != "cancelled":
                raise ValueError("cancelled receipts require the cancelled error code")
        return self
