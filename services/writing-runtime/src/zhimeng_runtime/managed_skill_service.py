from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal
from uuid import uuid4

from pydantic import ValidationError

from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.skill_contracts import (
    SkillContractError,
    SkillContractPair,
    SkillContractRegistry,
)
from zhimeng_runtime.skill_models import (
    ManagedSkillActivationRecord,
    ManagedSkillActivationRequest,
    ManagedSkillActivationResponse,
    ManagedSkillActivationReceipt,
    ManagedSkillAdaptationRecord,
    ManagedSkillCatalogEntry,
    ManagedSkillCatalogResponse,
    ManagedSkillImportReceipt,
    ManagedSkillImportResponse,
    ManagedSkillLifecycleCatalogResponse,
    ManagedSkillScope,
    ManagedSkillSourceRecord,
    ManagedSkillValidationRecord,
    ManagedSkillVersionRecord,
    SkillManifest,
)


REPARSE_POINT_ATTRIBUTE: Final = 0x400
MANIFEST_NAME: Final = "skill.manifest.json"
APPLICATION_SCOPE_KEY: Final = "application"
MANAGED_PACKAGE_ADAPTER_ID: Final = "zhimeng.managed-package"
MANAGED_PACKAGE_ADAPTER_VERSION: Final = "1"
MANAGED_PACKAGE_ADAPTER_FINGERPRINT: Final = hashlib.sha256(
    b"zhimeng-managed-package-adapter-v1\0"
    b"strict-manifest\0stable-files\0no-links\0project-snapshot\0run-staging"
).hexdigest()
SNAPSHOT_PUBLISH_RETRY_DELAYS: Final = (0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


class ManagedSkillError(RuntimeError):
    code = "managed_skill_error"
    retryable = False


class ManagedSkillProjectNotFoundError(ManagedSkillError):
    code = "project_not_found"


class ManagedSkillNotFoundError(ManagedSkillError):
    code = "managed_skill_not_found"


class ManagedSkillPackageError(ManagedSkillError):
    code = "managed_skill_package_invalid"


class ManagedSkillPackageLimitError(ManagedSkillPackageError):
    code = "managed_skill_package_limit"


class ManagedSkillVersionConflictError(ManagedSkillPackageError):
    code = "managed_skill_version_conflict"


class ManagedSkillPackageDriftError(ManagedSkillPackageError):
    code = "managed_skill_package_drift"


class ManagedSkillImportNotAllowedError(ManagedSkillPackageError):
    code = "managed_skill_import_not_allowed"


class ManagedSkillAdaptationRequiredError(ManagedSkillPackageError):
    code = "managed_skill_adaptation_required"


class ManagedSkillActivationConflictError(ManagedSkillError):
    code = "managed_skill_activation_conflict"


class ManagedSkillActivationInvalidError(ManagedSkillError):
    code = "managed_skill_activation_invalid"


@dataclass(frozen=True, slots=True)
class ManagedSkillLimits:
    max_files: int = 512
    max_total_bytes: int = 16 * 1024 * 1024
    max_file_bytes: int = 4 * 1024 * 1024
    max_depth: int = 32

    def __post_init__(self) -> None:
        if min(
            self.max_files,
            self.max_total_bytes,
            self.max_file_bytes,
        ) < 1:
            raise ValueError("managed skill file and byte budgets must be positive")
        if self.max_depth < 0:
            raise ValueError("managed skill max_depth cannot be negative")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True, slots=True)
class ManagedSkillVersion:
    skill_version_id: str
    manifest: SkillManifest
    manifest_fingerprint: str
    package_fingerprint: str
    package_root: Path
    manifest_path: Path
    entrypoint_path: Path | None

    @property
    def instruction_map_path(self) -> Path | None:
        if self.manifest.kind != "instruction_source":
            return None
        return self.package_root / "instruction-map.json"

    def public_entry(self) -> ManagedSkillCatalogEntry:
        return ManagedSkillCatalogEntry(
            skill_version_id=self.skill_version_id,
            manifest=self.manifest,
            manifest_fingerprint=self.manifest_fingerprint,
            package_fingerprint=self.package_fingerprint,
            status="active",
        )


@dataclass(frozen=True, slots=True)
class _PackageSnapshot:
    manifest: SkillManifest
    manifest_fingerprint: str
    package_fingerprint: str
    manifest_path: Path
    entrypoint_path: Path | None
    files: tuple[tuple[str, bytes], ...]
    total_bytes: int


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _stat_is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(
        int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE
    )


def _is_junction(path: Path) -> bool:
    checker = getattr(os.path, "isjunction", None)
    return bool(checker is not None and checker(path))


def _stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManagedSkillPackageError("managed skill metadata is not canonical JSON") from exc


def _parse_datetime(value: object) -> datetime:
    rendered = str(value)
    if rendered.endswith("Z"):
        rendered = f"{rendered[:-1]}+00:00"
    parsed = datetime.fromisoformat(rendered)
    if parsed.tzinfo is None:
        raise ManagedSkillPackageDriftError("managed Skill timestamp is not timezone-aware")
    return parsed


class ManagedSkillService:
    """Owns immutable, project-visible Skill packages copied into managed storage."""

    def __init__(
        self,
        store: SqliteStore,
        managed_root: Path,
        *,
        limits: ManagedSkillLimits | None = None,
        trusted_import_roots: tuple[Path, ...] = (),
        contracts_root: Path | None = None,
    ) -> None:
        self._store = store
        candidate = Path(managed_root).expanduser()
        if not candidate.is_absolute():
            raise ManagedSkillPackageError("managed skill root must be absolute")
        candidate.mkdir(parents=True, exist_ok=True)
        self._managed_root = self._validate_directory(candidate, label="managed root")
        self._limits = limits or ManagedSkillLimits()
        self._registered: dict[str, ManagedSkillVersion] = {}
        try:
            self._contracts = SkillContractRegistry(contracts_root)
        except SkillContractError as exc:
            raise ManagedSkillPackageError(str(exc)) from exc
        self._snapshot_root = self._managed_root / "snapshots"
        self._staging_root = self._managed_root / ".staging"
        self._snapshot_root.mkdir(exist_ok=True)
        self._staging_root.mkdir(exist_ok=True)
        self._snapshot_root = self._validate_directory(
            self._snapshot_root,
            label="managed snapshot root",
        )
        self._staging_root = self._validate_directory(
            self._staging_root,
            label="managed import staging root",
        )
        normalized_import_roots = []
        for raw_root in trusted_import_roots:
            candidate_root = Path(raw_root).expanduser()
            if not candidate_root.is_absolute():
                raise ManagedSkillPackageError(
                    "trusted Skill import roots must be absolute"
                )
            root = (
                self._validate_directory(
                    candidate_root,
                    label="trusted Skill import root",
                )
                if candidate_root.exists()
                else candidate_root.resolve(strict=False)
            )
            if (
                _is_within(root, self._managed_root)
                or _is_within(self._managed_root, root)
            ):
                raise ManagedSkillPackageError(
                    "trusted Skill import roots cannot overlap managed storage"
                )
            if root not in normalized_import_roots:
                normalized_import_roots.append(root)
        self._trusted_import_roots = tuple(normalized_import_roots)

    @property
    def managed_root(self) -> Path:
        return self._managed_root

    @property
    def limits(self) -> ManagedSkillLimits:
        return self._limits

    def register_builtin(self, package_root: Path) -> ManagedSkillVersion:
        response = self._import_package(
            package_root,
            scope=self._scope(),
            source_kind="builtin_bundle",
            expected_package_fingerprint=None,
            require_trusted_source=False,
        )
        self._bootstrap_builtin(response.version.manifest.id, response.version.skill_version_id)
        return self._load_version(response.version.skill_version_id)

    def import_package(
        self,
        package_root: Path,
        *,
        project_id: str | None = None,
        expected_package_fingerprint: str | None = None,
    ) -> ManagedSkillImportResponse:
        scope = self._scope(project_id)
        return self._import_package(
            package_root,
            scope=scope,
            source_kind="trusted_import",
            expected_package_fingerprint=expected_package_fingerprint,
            require_trusted_source=True,
        )

    def import_runtime_owned_package(
        self,
        package_root: Path,
        *,
        project_id: str | None = None,
        expected_package_fingerprint: str | None = None,
    ) -> ManagedSkillImportResponse:
        """Import a Runtime-owned adapted package without re-checking trusted roots.

        The package is still content-addressed, validated, and left inert unless
        the author later enables it. Raw SKILL.md is never executed here.
        """

        scope = self._scope(project_id)
        return self._import_package(
            package_root,
            scope=scope,
            source_kind="trusted_import",
            expected_package_fingerprint=expected_package_fingerprint,
            require_trusted_source=False,
        )

    def list_for_project(self, project_id: str) -> ManagedSkillCatalogResponse:
        self._require_project(project_id)
        with self._store.connection() as connection:
            skill_ids = self._visible_skill_ids(connection, project_id=project_id)
            version_ids = []
            for skill_id in skill_ids:
                activation = self._activation_record(
                    connection,
                    skill_id=skill_id,
                    project_id=project_id,
                )
                if activation.effective_state == "enabled":
                    version_ids.append(activation.effective_version_id)
        entries = [
            self._load_version(version_id).public_entry()
            for version_id in version_ids
            if version_id is not None
        ]
        return ManagedSkillCatalogResponse(
            schema="zhimeng.managed-skill-catalog.v1",
            project_id=project_id,
            skills=entries,
        )

    def get_active(self, project_id: str, skill_id: str) -> ManagedSkillVersion:
        self._require_project(project_id)
        with self._store.connection() as connection:
            activation = self._activation_record(
                connection,
                skill_id=skill_id,
                project_id=project_id,
            )
        if (
            activation.effective_state != "enabled"
            or activation.effective_version_id is None
        ):
            raise ManagedSkillNotFoundError("managed Skill is not active for this project")
        loaded = self._load_version(activation.effective_version_id)
        if loaded.manifest.id != skill_id:
            raise ManagedSkillPackageDriftError(
                "Skill activation points at a different managed Skill"
            )
        return loaded

    def get_active_declaration(
        self,
        project_id: str,
        skill_id: str,
    ) -> ManagedSkillVersion:
        """Read the active immutable manifest before current contract revalidation.

        This narrow preflight lets callers detect an explicitly unsupported
        capability in an older managed snapshot and request an author-approved
        upgrade. Execution still uses ``get_active`` and validates the current
        contract fingerprints before starting the subprocess.
        """

        self._require_project(project_id)
        with self._store.connection() as connection:
            activation = self._activation_record(
                connection,
                skill_id=skill_id,
                project_id=project_id,
            )
            if (
                activation.effective_state != "enabled"
                or activation.effective_version_id is None
            ):
                raise ManagedSkillNotFoundError(
                    "managed Skill is not active for this project"
                )
            row = connection.execute(
                "SELECT * FROM skill_version WHERE id = ?",
                (activation.effective_version_id,),
            ).fetchone()
        if row is None:
            raise ManagedSkillPackageDriftError(
                "Skill activation points at a missing managed Skill version"
            )
        loaded = self._load_registered_declaration(row)
        if loaded.manifest.id != skill_id:
            raise ManagedSkillPackageDriftError(
                "Skill activation points at a different managed Skill"
            )
        return loaded

    def list_lifecycle(
        self,
        *,
        project_id: str | None = None,
    ) -> ManagedSkillLifecycleCatalogResponse:
        scope = self._scope(project_id)
        with self._store.connection() as connection:
            receipt_rows = self._visible_import_rows(connection, project_id=project_id)
            skill_ids = self._visible_skill_ids(connection, project_id=project_id)
            activations = [
                self._activation_record(
                    connection,
                    skill_id=skill_id,
                    project_id=project_id,
                )
                for skill_id in skill_ids
            ]
        versions = [self._version_record(row) for row in receipt_rows]
        return ManagedSkillLifecycleCatalogResponse(
            schema="zhimeng.managed-skill-lifecycle-catalog.v1",
            scope=scope,
            versions=versions,
            activations=activations,
        )

    def activate(
        self,
        skill_id: str,
        request: ManagedSkillActivationRequest,
        *,
        project_id: str | None = None,
    ) -> ManagedSkillActivationResponse:
        scope = self._scope(project_id)
        return self._activate(scope, skill_id, request)

    def require_project_binding(self, project_id: str) -> ProjectBindingRecord:
        binding = self._store.get_project_binding(project_id)
        if not project_id or binding is None:
            raise ManagedSkillProjectNotFoundError("project binding does not exist")
        return binding

    def verify_active(self, version: ManagedSkillVersion) -> str:
        current = self._snapshot_package(version.package_root)
        if (
            current.manifest.id != version.manifest.id
            or current.manifest.version != version.manifest.version
            or current.manifest_fingerprint != version.manifest_fingerprint
            or current.package_fingerprint != version.package_fingerprint
            or current.manifest_path != version.manifest_path
            or current.entrypoint_path != version.entrypoint_path
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill package changed after registration"
            )
        return current.package_fingerprint

    def _require_project(self, project_id: str) -> None:
        self.require_project_binding(project_id)

    def recover_imports(self) -> dict[str, int]:
        """Remove interrupted staging trees and unreferenced owned snapshots."""

        removed_staging = 0
        removed_orphans = 0
        with self._store.connection() as connection:
            referenced = {
                Path(str(row["source_path"])).parent.resolve(strict=False)
                for row in connection.execute(
                    """
                    SELECT source_path
                    FROM skill_version
                    WHERE snapshot_status = 'managed'
                    """
                ).fetchall()
            }
        for child in tuple(self._staging_root.iterdir()):
            self._remove_owned_tree(child, parent=self._staging_root)
            removed_staging += 1
        for child in tuple(self._snapshot_root.iterdir()):
            if child.resolve(strict=False) in referenced:
                continue
            self._remove_owned_tree(child, parent=self._snapshot_root)
            removed_orphans += 1
        return {
            "removed_staging": removed_staging,
            "removed_orphans": removed_orphans,
        }

    def _scope(self, project_id: str | None = None) -> ManagedSkillScope:
        if project_id is None:
            return ManagedSkillScope(scope_type="application", project_id=None)
        self._require_project(project_id)
        return ManagedSkillScope(scope_type="project", project_id=project_id)

    @staticmethod
    def _scope_key(scope: ManagedSkillScope) -> str:
        if scope.scope_type == "application":
            return APPLICATION_SCOPE_KEY
        assert scope.project_id is not None
        return f"project:{scope.project_id}"

    def _import_package(
        self,
        package_root: Path,
        *,
        scope: ManagedSkillScope,
        source_kind: Literal["builtin_bundle", "trusted_import"],
        expected_package_fingerprint: str | None,
        require_trusted_source: bool,
    ) -> ManagedSkillImportResponse:
        source_root = self._resolve_source_root(
            package_root,
            require_trusted=require_trusted_source,
        )
        if not (source_root / MANIFEST_NAME).exists():
            raise ManagedSkillAdaptationRequiredError(
                "Skill source requires a managed-package adapter before import"
            )
        captured = self._snapshot_package(source_root)
        contracts = (
            self._load_contract_pair(captured.manifest)
            if captured.manifest.kind == "deterministic_tool"
            else None
        )
        input_contract_fingerprint = (
            None if contracts is None else contracts.input_contract.fingerprint
        )
        output_contract_fingerprint = (
            None if contracts is None else contracts.output_contract.fingerprint
        )
        contract_bundle_fingerprint = (
            None if contracts is None else contracts.bundle_fingerprint
        )
        if (
            expected_package_fingerprint is not None
            and captured.package_fingerprint != expected_package_fingerprint
        ):
            raise ManagedSkillPackageDriftError(
                "Skill source package does not match the expected fingerprint"
            )
        version_id = self._version_id(
            captured.manifest.id,
            captured.manifest.version,
            captured.package_fingerprint,
        )
        snapshot_root, created_snapshot = self._persist_snapshot(version_id, captured)
        snapshot = self._snapshot_package(snapshot_root)
        self._assert_same_snapshot(captured, snapshot)
        now = datetime.now(UTC).isoformat()
        scope_key = self._scope_key(scope)
        source = ManagedSkillSourceRecord(
            kind=source_kind,
            reference=captured.manifest.source.reference,
            locator_hash=hashlib.sha256(str(source_root).encode("utf-8")).hexdigest(),
            package_fingerprint=captured.package_fingerprint,
        )
        adaptation = ManagedSkillAdaptationRecord(
            adapter_id=MANAGED_PACKAGE_ADAPTER_ID,
            adapter_version=MANAGED_PACKAGE_ADAPTER_VERSION,
            adapter_fingerprint=MANAGED_PACKAGE_ADAPTER_FINGERPRINT,
            status="validated",
        )
        validation = ManagedSkillValidationRecord(
            status="passed",
            contract_status=("passed" if contracts is not None else "not_applicable"),
            isolation_status="passed",
            input_contract_fingerprint=input_contract_fingerprint,
            output_contract_fingerprint=output_contract_fingerprint,
            contract_bundle_fingerprint=contract_bundle_fingerprint,
            permissions=captured.manifest.permissions,
        )
        imported = False
        try:
            with self._store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    conflict = connection.execute(
                        """
                        SELECT id
                        FROM skill_version
                        WHERE skill_id = ? AND version = ? AND fingerprint <> ?
                        LIMIT 1
                        """,
                        (
                            captured.manifest.id,
                            captured.manifest.version,
                            captured.package_fingerprint,
                        ),
                    ).fetchone()
                    if conflict is not None:
                        raise ManagedSkillVersionConflictError(
                            "a managed Skill version cannot be replaced with different content"
                        )
                    existing = connection.execute(
                        "SELECT * FROM skill_version WHERE skill_id = ? AND fingerprint = ?",
                        (captured.manifest.id, captured.package_fingerprint),
                    ).fetchone()
                    if existing is None:
                        connection.execute(
                            """
                            INSERT INTO skill_version(
                                id, skill_id, version, source_path, fingerprint,
                                manifest_fingerprint, adapter_id, adapter_version,
                                adapter_fingerprint, input_contract_fingerprint,
                                output_contract_fingerprint, contract_bundle_fingerprint,
                                file_count, total_bytes,
                                snapshot_status, status, registered_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      'managed', 'active', ?)
                            """,
                            (
                                version_id,
                                captured.manifest.id,
                                captured.manifest.version,
                                str(snapshot.manifest_path),
                                captured.package_fingerprint,
                                captured.manifest_fingerprint,
                                MANAGED_PACKAGE_ADAPTER_ID,
                                MANAGED_PACKAGE_ADAPTER_VERSION,
                                MANAGED_PACKAGE_ADAPTER_FINGERPRINT,
                                input_contract_fingerprint,
                                output_contract_fingerprint,
                                contract_bundle_fingerprint,
                                len(captured.files),
                                captured.total_bytes,
                                now,
                            ),
                        )
                    else:
                        if str(existing["id"]) != version_id:
                            raise ManagedSkillVersionConflictError(
                                "managed Skill identity does not match its content"
                            )
                        if str(existing["snapshot_status"]) == "managed" and (
                            str(existing["adapter_fingerprint"])
                            != MANAGED_PACKAGE_ADAPTER_FINGERPRINT
                            or existing["input_contract_fingerprint"]
                            != input_contract_fingerprint
                            or existing["output_contract_fingerprint"]
                            != output_contract_fingerprint
                            or existing["contract_bundle_fingerprint"]
                            != contract_bundle_fingerprint
                        ):
                            raise ManagedSkillVersionConflictError(
                                "a managed Skill version cannot change adapter or contract identity"
                            )
                        connection.execute(
                            """
                            UPDATE skill_version
                            SET source_path = ?, manifest_fingerprint = ?,
                                adapter_id = ?, adapter_version = ?,
                                adapter_fingerprint = ?,
                                input_contract_fingerprint = ?,
                                output_contract_fingerprint = ?,
                                contract_bundle_fingerprint = ?,
                                file_count = ?, total_bytes = ?,
                                snapshot_status = 'managed', status = 'active'
                            WHERE id = ?
                            """,
                            (
                                str(snapshot.manifest_path),
                                captured.manifest_fingerprint,
                                MANAGED_PACKAGE_ADAPTER_ID,
                                MANAGED_PACKAGE_ADAPTER_VERSION,
                                MANAGED_PACKAGE_ADAPTER_FINGERPRINT,
                                input_contract_fingerprint,
                                output_contract_fingerprint,
                                contract_bundle_fingerprint,
                                len(captured.files),
                                captured.total_bytes,
                                version_id,
                            ),
                        )
                    receipt_row = connection.execute(
                        """
                        SELECT * FROM skill_import_receipt
                        WHERE scope_key = ? AND skill_version_id = ?
                        """,
                        (scope_key, version_id),
                    ).fetchone()
                    if receipt_row is None:
                        receipt_id = self._import_receipt_id(scope_key, version_id)
                        receipt_payload = {
                            "receipt_id": receipt_id,
                            "scope": scope.model_dump(mode="json"),
                            "skill_version_id": version_id,
                            "source": source.model_dump(mode="json"),
                            "manifest_fingerprint": captured.manifest_fingerprint,
                            "file_count": len(captured.files),
                            "total_bytes": captured.total_bytes,
                            "adaptation": adaptation.model_dump(mode="json"),
                            "validation": validation.model_dump(mode="json"),
                            "created_at": now,
                        }
                        receipt_hash = hashlib.sha256(
                            _canonical_json(receipt_payload)
                        ).hexdigest()
                        persisted_payload = {
                            **receipt_payload,
                            "receipt_hash": receipt_hash,
                        }
                        connection.execute(
                            """
                            INSERT INTO skill_import_receipt(
                                id, scope_key, scope_type, project_binding_id,
                                skill_version_id, source_kind, source_reference,
                                source_locator_hash, source_package_fingerprint,
                                receipt_hash, payload_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                receipt_id,
                                scope_key,
                                scope.scope_type,
                                scope.project_id,
                                version_id,
                                source.kind,
                                source.reference,
                                source.locator_hash,
                                source.package_fingerprint,
                                receipt_hash,
                                _canonical_json(persisted_payload).decode("utf-8"),
                                now,
                            ),
                        )
                        imported = True
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except Exception:
            if created_snapshot and not self._snapshot_is_referenced(snapshot_root):
                self._remove_owned_tree(snapshot_root, parent=self._snapshot_root)
            raise
        self._registered.pop(version_id, None)
        return self._import_response(version_id, scope, imported=imported)

    def _import_response(
        self,
        version_id: str,
        scope: ManagedSkillScope,
        *,
        imported: bool,
    ) -> ManagedSkillImportResponse:
        scope_key = self._scope_key(scope)
        with self._store.connection() as connection:
            row = self._import_version_row(
                connection,
                version_id=version_id,
                scope_key=scope_key,
            )
            if row is None:
                raise ManagedSkillPackageDriftError("Skill import receipt disappeared")
            activation = self._activation_record(
                connection,
                skill_id=str(row["skill_id"]),
                project_id=scope.project_id,
            )
        receipt = self._import_receipt_from_row(row)
        return ManagedSkillImportResponse(
            schema="zhimeng.managed-skill-import.v1",
            imported=imported,
            version=self._version_record(row),
            activation=activation,
            receipt=receipt,
        )

    def _bootstrap_builtin(self, skill_id: str, version_id: str) -> None:
        scope = self._scope()
        with self._store.connection() as connection:
            existing = connection.execute(
                """
                SELECT state, active_version_id, state_revision, updated_at
                FROM skill_activation
                WHERE scope_key = ? AND skill_id = ?
                """,
                (APPLICATION_SCOPE_KEY, skill_id),
            ).fetchone()
            if existing is not None:
                self._validate_activation_row(
                    connection,
                    scope_key=APPLICATION_SCOPE_KEY,
                    skill_id=skill_id,
                    activation_row=existing,
                )
                return
        request = ManagedSkillActivationRequest(
            action="enable",
            skill_version_id=version_id,
            expected_state_revision=0,
            expected_version_id=None,
        )
        try:
            self._activate(
                scope,
                skill_id,
                request,
                action_override="builtin_bootstrap",
            )
        except ManagedSkillActivationConflictError:
            with self._store.connection() as connection:
                concurrent = connection.execute(
                    """
                    SELECT 1 FROM skill_activation
                    WHERE scope_key = ? AND skill_id = ?
                    """,
                    (APPLICATION_SCOPE_KEY, skill_id),
                ).fetchone()
            if concurrent is None:
                raise

    def _activate(
        self,
        scope: ManagedSkillScope,
        skill_id: str,
        request: ManagedSkillActivationRequest,
        *,
        action_override: Literal["builtin_bootstrap"] | None = None,
    ) -> ManagedSkillActivationResponse:
        if scope.scope_type == "application" and request.action == "inherit":
            raise ManagedSkillActivationInvalidError(
                "application Skill activation cannot inherit another scope"
            )
        scope_key = self._scope_key(scope)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """
                    SELECT state, active_version_id, state_revision, updated_at
                    FROM skill_activation
                    WHERE scope_key = ? AND skill_id = ?
                    """,
                    (scope_key, skill_id),
                ).fetchone()
                if current is not None:
                    self._validate_activation_row(
                        connection,
                        scope_key=scope_key,
                        skill_id=skill_id,
                        activation_row=current,
                    )
                previous_state = (
                    "unconfigured" if current is None else str(current["state"])
                )
                previous_version_id = (
                    None
                    if current is None or current["active_version_id"] is None
                    else str(current["active_version_id"])
                )
                previous_revision = (
                    0 if current is None else int(current["state_revision"])
                )
                if (
                    previous_revision != request.expected_state_revision
                    or previous_version_id != request.expected_version_id
                ):
                    raise ManagedSkillActivationConflictError(
                        "managed Skill activation compare-and-swap precondition failed"
                    )
                target_version_id = request.skill_version_id
                if target_version_id is not None:
                    target = self._require_visible_version(
                        connection,
                        scope=scope,
                        skill_id=skill_id,
                        version_id=target_version_id,
                    )
                    if str(target["snapshot_status"]) != "managed":
                        raise ManagedSkillActivationInvalidError(
                            "legacy unmanaged Skill versions cannot be enabled"
                        )
                elif not self._scope_has_skill(
                    connection,
                    scope=scope,
                    skill_id=skill_id,
                ):
                    raise ManagedSkillNotFoundError(
                        "managed Skill is not imported into this scope"
                    )

                if request.action == "enable":
                    if previous_state == "enabled":
                        raise ManagedSkillActivationInvalidError(
                            "an enabled Skill must use upgrade or rollback to change version"
                        )
                    next_state = "enabled"
                    next_version_id = target_version_id
                elif request.action == "upgrade":
                    if previous_state != "enabled" or previous_version_id == target_version_id:
                        raise ManagedSkillActivationInvalidError(
                            "Skill upgrade requires a different currently enabled version"
                        )
                    next_state = "enabled"
                    next_version_id = target_version_id
                elif request.action == "rollback":
                    if previous_state != "enabled" or previous_version_id == target_version_id:
                        raise ManagedSkillActivationInvalidError(
                            "Skill rollback requires a different currently enabled version"
                        )
                    if not self._historical_version_was_active(
                        connection,
                        scope_key=scope_key,
                        skill_id=skill_id,
                        through_revision=previous_revision,
                        version_id=str(target_version_id),
                    ):
                        raise ManagedSkillActivationInvalidError(
                            "Skill rollback target was never active in this scope"
                        )
                    next_state = "enabled"
                    next_version_id = target_version_id
                elif request.action == "disable":
                    if previous_state == "disabled":
                        raise ManagedSkillActivationInvalidError(
                            "managed Skill is already disabled in this scope"
                        )
                    next_state = "disabled"
                    next_version_id = None
                else:
                    if previous_state in {"unconfigured", "inherited"}:
                        raise ManagedSkillActivationInvalidError(
                            "project Skill already inherits application activation"
                        )
                    next_state = "inherited"
                    next_version_id = None

                next_revision = previous_revision + 1
                action = action_override or request.action
                receipt_id = self._activation_receipt_id(
                    scope_key,
                    skill_id,
                    next_revision,
                    action,
                )
                receipt_payload = {
                    "receipt_id": receipt_id,
                    "scope": scope.model_dump(mode="json"),
                    "skill_id": skill_id,
                    "action": action,
                    "previous_state": previous_state,
                    "previous_version_id": previous_version_id,
                    "next_state": next_state,
                    "next_version_id": next_version_id,
                    "expected_state_revision": request.expected_state_revision,
                    "expected_version_id": request.expected_version_id,
                    "state_revision": next_revision,
                    "created_at": now,
                }
                receipt_hash = hashlib.sha256(
                    _canonical_json(receipt_payload)
                ).hexdigest()
                persisted_receipt = {
                    **receipt_payload,
                    "receipt_hash": receipt_hash,
                }
                if current is None:
                    connection.execute(
                        """
                        INSERT INTO skill_activation(
                            scope_key, scope_type, project_binding_id, skill_id,
                            state, active_version_id, state_revision, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            scope_key,
                            scope.scope_type,
                            scope.project_id,
                            skill_id,
                            next_state,
                            next_version_id,
                            next_revision,
                            now,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        UPDATE skill_activation
                        SET state = ?, active_version_id = ?, state_revision = ?,
                            updated_at = ?
                        WHERE scope_key = ? AND skill_id = ? AND state_revision = ?
                        """,
                        (
                            next_state,
                            next_version_id,
                            next_revision,
                            now,
                            scope_key,
                            skill_id,
                            previous_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ManagedSkillActivationConflictError(
                            "managed Skill activation changed concurrently"
                        )
                connection.execute(
                    """
                    INSERT INTO skill_activation_receipt(
                        id, scope_key, scope_type, project_binding_id, skill_id,
                        action, previous_state, previous_version_id,
                        next_state, next_version_id, expected_state_revision,
                        expected_version_id, state_revision, receipt_hash,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        scope_key,
                        scope.scope_type,
                        scope.project_id,
                        skill_id,
                        action,
                        previous_state,
                        previous_version_id,
                        next_state,
                        next_version_id,
                        request.expected_state_revision,
                        request.expected_version_id,
                        next_revision,
                        receipt_hash,
                        _canonical_json(persisted_receipt).decode("utf-8"),
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        with self._store.connection() as connection:
            activation = self._activation_record(
                connection,
                skill_id=skill_id,
                project_id=scope.project_id,
            )
        receipt = ManagedSkillActivationReceipt.model_validate_json(
            _canonical_json(persisted_receipt),
            strict=True,
        )
        return ManagedSkillActivationResponse(
            schema="zhimeng.managed-skill-activation.v1",
            activation=activation,
            receipt=receipt,
        )

    def _activation_record(
        self,
        connection: object,
        *,
        skill_id: str,
        project_id: str | None,
    ) -> ManagedSkillActivationRecord:
        scope = self._scope() if project_id is None else ManagedSkillScope(
            scope_type="project",
            project_id=project_id,
        )
        scope_key = self._scope_key(scope)
        local = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT state, active_version_id, state_revision, updated_at
            FROM skill_activation
            WHERE scope_key = ? AND skill_id = ?
            """,
            (scope_key, skill_id),
        ).fetchone()
        if local is not None:
            self._validate_activation_row(
                connection,
                scope_key=scope_key,
                skill_id=skill_id,
                activation_row=local,
            )
        if local is None:
            configured_state = "unconfigured" if project_id is None else "inherited"
            configured_version_id = None
            state_revision = 0
            updated_at = None
        else:
            configured_state = str(local["state"])
            configured_version_id = (
                None
                if local["active_version_id"] is None
                else str(local["active_version_id"])
            )
            state_revision = int(local["state_revision"])
            updated_at = str(local["updated_at"])

        effective_row = local
        effective_scope: Literal["application", "project", "none"] = (
            "application" if project_id is None else "project"
        )
        if project_id is not None and (
            local is None or str(local["state"]) == "inherited"
        ):
            effective_row = connection.execute(  # type: ignore[attr-defined]
                """
                SELECT state, active_version_id, state_revision, updated_at
                FROM skill_activation
                WHERE scope_key = ? AND skill_id = ?
                """,
                (APPLICATION_SCOPE_KEY, skill_id),
            ).fetchone()
            if effective_row is not None:
                self._validate_activation_row(
                    connection,
                    scope_key=APPLICATION_SCOPE_KEY,
                    skill_id=skill_id,
                    activation_row=effective_row,
                )
            effective_scope = "application" if effective_row is not None else "none"
        if effective_row is None or str(effective_row["state"]) != "enabled":
            effective_state = "disabled"
            effective_version_id = None
            if effective_row is None:
                effective_scope = "none"
        else:
            effective_state = "enabled"
            effective_version_id = str(effective_row["active_version_id"])
        return ManagedSkillActivationRecord(
            scope=scope,
            skill_id=skill_id,
            configured_state=configured_state,
            configured_version_id=configured_version_id,
            state_revision=state_revision,
            effective_state=effective_state,
            effective_version_id=effective_version_id,
            effective_scope=effective_scope,
            updated_at=None if updated_at is None else _parse_datetime(updated_at),
        )

    def _validate_activation_row(
        self,
        connection: object,
        *,
        scope_key: str,
        skill_id: str,
        activation_row: object,
    ) -> ManagedSkillActivationReceipt:
        revision = int(activation_row["state_revision"])  # type: ignore[index]
        receipt_row = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM skill_activation_receipt
            WHERE scope_key = ? AND skill_id = ? AND state_revision = ?
            """,
            (scope_key, skill_id, revision),
        ).fetchone()
        if receipt_row is None:
            raise ManagedSkillPackageDriftError(
                "managed Skill activation has no immutable decision receipt"
            )
        receipt = self._activation_receipt_from_row(receipt_row)
        active_version_id = (
            None
            if activation_row["active_version_id"] is None  # type: ignore[index]
            else str(activation_row["active_version_id"])  # type: ignore[index]
        )
        if (
            receipt.next_state != str(activation_row["state"])  # type: ignore[index]
            or receipt.next_version_id != active_version_id
            or receipt.state_revision != revision
            or receipt.created_at
            != _parse_datetime(activation_row["updated_at"])  # type: ignore[index]
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill activation does not match its decision receipt"
            )
        return receipt

    def _activation_receipt_from_row(
        self,
        row: object,
    ) -> ManagedSkillActivationReceipt:
        try:
            payload = json.loads(str(row["payload_json"]))  # type: ignore[index]
            if not isinstance(payload, dict):
                raise ValueError("receipt payload must be an object")
            persisted_hash = payload.pop("receipt_hash")
            computed_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
            payload["receipt_hash"] = persisted_hash
            receipt = ManagedSkillActivationReceipt.model_validate(
                payload,
                strict=False,
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ManagedSkillPackageDriftError(
                "managed Skill activation receipt is invalid"
            ) from exc
        expected_project_id = (
            None
            if row["project_binding_id"] is None  # type: ignore[index]
            else str(row["project_binding_id"])  # type: ignore[index]
        )
        if (
            receipt.receipt_id != str(row["id"])  # type: ignore[index]
            or self._scope_key(receipt.scope) != str(row["scope_key"])  # type: ignore[index]
            or receipt.scope.scope_type != str(row["scope_type"])  # type: ignore[index]
            or receipt.scope.project_id != expected_project_id
            or receipt.skill_id != str(row["skill_id"])  # type: ignore[index]
            or receipt.action != str(row["action"])  # type: ignore[index]
            or receipt.previous_state != str(row["previous_state"])  # type: ignore[index]
            or receipt.previous_version_id
            != (
                None
                if row["previous_version_id"] is None  # type: ignore[index]
                else str(row["previous_version_id"])  # type: ignore[index]
            )
            or receipt.next_state != str(row["next_state"])  # type: ignore[index]
            or receipt.next_version_id
            != (
                None
                if row["next_version_id"] is None  # type: ignore[index]
                else str(row["next_version_id"])  # type: ignore[index]
            )
            or receipt.expected_state_revision
            != int(row["expected_state_revision"])  # type: ignore[index]
            or receipt.expected_version_id
            != (
                None
                if row["expected_version_id"] is None  # type: ignore[index]
                else str(row["expected_version_id"])  # type: ignore[index]
            )
            or receipt.state_revision != int(row["state_revision"])  # type: ignore[index]
            or receipt.receipt_hash != str(row["receipt_hash"])  # type: ignore[index]
            or receipt.receipt_hash != computed_hash
            or receipt.created_at != _parse_datetime(row["created_at"])  # type: ignore[index]
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill activation receipt no longer matches its index"
            )
        return receipt

    def _historical_version_was_active(
        self,
        connection: object,
        *,
        scope_key: str,
        skill_id: str,
        through_revision: int,
        version_id: str,
    ) -> bool:
        rows = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT *
            FROM skill_activation_receipt
            WHERE scope_key = ? AND skill_id = ? AND state_revision <= ?
            ORDER BY state_revision
            """,
            (scope_key, skill_id, through_revision),
        ).fetchall()
        for row in rows:
            receipt = self._activation_receipt_from_row(row)
            if (
                receipt.previous_version_id == version_id
                or receipt.next_version_id == version_id
            ):
                return True
        return False

    def _visible_skill_ids(self, connection: object, *, project_id: str | None) -> list[str]:
        scope_keys = [APPLICATION_SCOPE_KEY]
        if project_id is not None:
            scope_keys.append(f"project:{project_id}")
        placeholders = ",".join("?" for _ in scope_keys)
        rows = connection.execute(  # type: ignore[attr-defined]
            f"""
            SELECT DISTINCT v.skill_id
            FROM skill_import_receipt AS r
            JOIN skill_version AS v ON v.id = r.skill_version_id
            WHERE r.scope_key IN ({placeholders})
            UNION
            SELECT DISTINCT skill_id
            FROM skill_activation
            WHERE scope_key IN ({placeholders})
            ORDER BY skill_id
            """,
            (*scope_keys, *scope_keys),
        ).fetchall()
        return [str(row["skill_id"]) for row in rows]

    def _visible_import_rows(
        self,
        connection: object,
        *,
        project_id: str | None,
    ) -> list[object]:
        scope_keys = [APPLICATION_SCOPE_KEY]
        if project_id is not None:
            scope_keys.insert(0, f"project:{project_id}")
        placeholders = ",".join("?" for _ in scope_keys)
        rows = connection.execute(  # type: ignore[attr-defined]
            f"""
            SELECT v.*, r.id AS import_receipt_id, r.scope_key AS import_scope_key,
                   r.scope_type AS import_scope_type,
                   r.project_binding_id AS import_project_id,
                   r.source_kind, r.source_reference, r.source_locator_hash,
                   r.source_package_fingerprint, r.receipt_hash AS import_receipt_hash,
                   r.payload_json, r.created_at AS imported_at
            FROM skill_import_receipt AS r
            JOIN skill_version AS v ON v.id = r.skill_version_id
            WHERE r.scope_key IN ({placeholders})
            ORDER BY v.skill_id, v.version, v.id,
                     CASE r.scope_type WHEN 'project' THEN 0 ELSE 1 END
            """,
            tuple(scope_keys),
        ).fetchall()
        unique = []
        seen = set()
        for row in rows:
            version_id = str(row["id"])
            if version_id in seen:
                continue
            seen.add(version_id)
            unique.append(row)
        return unique

    def _import_version_row(
        self,
        connection: object,
        *,
        version_id: str,
        scope_key: str,
    ) -> object | None:
        return connection.execute(  # type: ignore[attr-defined]
            """
            SELECT v.*, r.id AS import_receipt_id, r.scope_key AS import_scope_key,
                   r.scope_type AS import_scope_type,
                   r.project_binding_id AS import_project_id,
                   r.source_kind, r.source_reference, r.source_locator_hash,
                   r.source_package_fingerprint, r.receipt_hash AS import_receipt_hash,
                   r.payload_json, r.created_at AS imported_at
            FROM skill_import_receipt AS r
            JOIN skill_version AS v ON v.id = r.skill_version_id
            WHERE r.scope_key = ? AND r.skill_version_id = ?
            """,
            (scope_key, version_id),
        ).fetchone()

    def _version_record(self, row: object) -> ManagedSkillVersionRecord:
        version = self._load_version(str(row["id"]))  # type: ignore[index]
        receipt = self._import_receipt_from_row(row)
        if (
            receipt.manifest_fingerprint != version.manifest_fingerprint
            or receipt.file_count != int(row["file_count"])  # type: ignore[index]
            or receipt.total_bytes != int(row["total_bytes"])  # type: ignore[index]
            or receipt.adaptation.adapter_fingerprint
            != MANAGED_PACKAGE_ADAPTER_FINGERPRINT
            or receipt.validation.permissions != version.manifest.permissions
            or receipt.validation.input_contract_fingerprint
            != row["input_contract_fingerprint"]  # type: ignore[index]
            or receipt.validation.output_contract_fingerprint
            != row["output_contract_fingerprint"]  # type: ignore[index]
            or receipt.validation.contract_bundle_fingerprint
            != row["contract_bundle_fingerprint"]  # type: ignore[index]
            or (
                receipt.validation.contract_status == "passed"
            )
            != (version.manifest.kind == "deterministic_tool")
        ):
            raise ManagedSkillPackageDriftError(
                "Skill import receipt does not bind the managed snapshot metadata"
            )
        return ManagedSkillVersionRecord(
            skill_version_id=version.skill_version_id,
            manifest=version.manifest,
            manifest_fingerprint=version.manifest_fingerprint,
            package_fingerprint=version.package_fingerprint,
            file_count=int(row["file_count"]),  # type: ignore[index]
            total_bytes=int(row["total_bytes"]),  # type: ignore[index]
            source=receipt.source,
            adaptation=receipt.adaptation,
            validation=receipt.validation,
            imported_at=receipt.created_at,
        )

    def _import_receipt_from_row(self, row: object) -> ManagedSkillImportReceipt:
        try:
            payload = json.loads(str(row["payload_json"]))  # type: ignore[index]
            if not isinstance(payload, dict):
                raise ValueError("receipt payload must be an object")
            persisted_hash = payload.pop("receipt_hash")
            computed_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
            payload["receipt_hash"] = persisted_hash
            receipt = ManagedSkillImportReceipt.model_validate(payload, strict=False)
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ManagedSkillPackageDriftError(
                "managed Skill import receipt is invalid"
            ) from exc
        expected_scope_key = self._scope_key(receipt.scope)
        if (
            receipt.receipt_id != str(row["import_receipt_id"])  # type: ignore[index]
            or expected_scope_key != str(row["import_scope_key"])  # type: ignore[index]
            or receipt.scope.scope_type != str(row["import_scope_type"])  # type: ignore[index]
            or receipt.scope.project_id
            != (
                None
                if row["import_project_id"] is None  # type: ignore[index]
                else str(row["import_project_id"])  # type: ignore[index]
            )
            or receipt.skill_version_id != str(row["id"])  # type: ignore[index]
            or receipt.source.kind != str(row["source_kind"])  # type: ignore[index]
            or receipt.source.reference != str(row["source_reference"])  # type: ignore[index]
            or receipt.source.locator_hash != str(row["source_locator_hash"])  # type: ignore[index]
            or receipt.source.package_fingerprint
            != str(row["source_package_fingerprint"])  # type: ignore[index]
            or receipt.receipt_hash != str(row["import_receipt_hash"])  # type: ignore[index]
            or receipt.receipt_hash != computed_hash
            or receipt.created_at != _parse_datetime(row["imported_at"])  # type: ignore[index]
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill import receipt no longer matches its index"
            )
        return receipt

    def _scope_has_skill(
        self,
        connection: object,
        *,
        scope: ManagedSkillScope,
        skill_id: str,
    ) -> bool:
        allowed = [APPLICATION_SCOPE_KEY]
        if scope.scope_type == "project":
            allowed.append(self._scope_key(scope))
        placeholders = ",".join("?" for _ in allowed)
        row = connection.execute(  # type: ignore[attr-defined]
            f"""
            SELECT 1
            FROM skill_import_receipt AS r
            JOIN skill_version AS v ON v.id = r.skill_version_id
            WHERE r.scope_key IN ({placeholders}) AND v.skill_id = ?
            LIMIT 1
            """,
            (*allowed, skill_id),
        ).fetchone()
        return row is not None

    def _require_visible_version(
        self,
        connection: object,
        *,
        scope: ManagedSkillScope,
        skill_id: str,
        version_id: str,
    ) -> object:
        allowed = [APPLICATION_SCOPE_KEY]
        if scope.scope_type == "project":
            allowed.append(self._scope_key(scope))
        placeholders = ",".join("?" for _ in allowed)
        row = connection.execute(  # type: ignore[attr-defined]
            f"""
            SELECT v.*
            FROM skill_version AS v
            WHERE v.id = ? AND v.skill_id = ?
              AND EXISTS (
                  SELECT 1 FROM skill_import_receipt AS r
                  WHERE r.skill_version_id = v.id
                    AND r.scope_key IN ({placeholders})
              )
            """,
            (version_id, skill_id, *allowed),
        ).fetchone()
        if row is None:
            raise ManagedSkillNotFoundError(
                "managed Skill version is not imported into this scope"
            )
        return row

    def _load_version(self, version_id: str) -> ManagedSkillVersion:
        with self._store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM skill_version WHERE id = ?",
                (version_id,),
            ).fetchone()
        if row is None:
            raise ManagedSkillNotFoundError("managed Skill version does not exist")
        cached = self._registered.get(version_id)
        if cached is not None and (
            cached.manifest.id == str(row["skill_id"])
            and cached.manifest.version == str(row["version"])
            and cached.package_fingerprint == str(row["fingerprint"])
            and cached.manifest_path == Path(str(row["source_path"]))
        ):
            return cached
        loaded = self._load_registered(row)
        self._registered[version_id] = loaded
        return loaded

    def _load_contract_pair(self, manifest: SkillManifest) -> SkillContractPair:
        if manifest.input_schema is None or manifest.output_schema is None:
            raise ManagedSkillPackageError(
                "non-executable instruction sources do not have execution contracts"
            )
        try:
            return self._contracts.load_pair(
                manifest.input_schema,
                manifest.output_schema,
            )
        except SkillContractError as exc:
            raise ManagedSkillPackageError(
                "managed Skill contracts could not be validated"
            ) from exc

    def _load_registered_declaration(self, row: object) -> ManagedSkillVersion:
        if (
            str(row["snapshot_status"]) != "managed"  # type: ignore[index]
            or str(row["status"]) != "active"  # type: ignore[index]
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill version is not an executable Runtime snapshot"
            )
        source_path = Path(str(row["source_path"]))  # type: ignore[index]
        if source_path.name != MANIFEST_NAME:
            raise ManagedSkillPackageDriftError(
                "registered Skill source_path is not a managed package manifest"
            )
        root = self._resolve_package_root(source_path.parent)
        snapshot = self._snapshot_package(root)
        if (
            str(row["skill_id"]) != snapshot.manifest.id  # type: ignore[index]
            or str(row["version"]) != snapshot.manifest.version  # type: ignore[index]
            or str(row["fingerprint"]) != snapshot.package_fingerprint  # type: ignore[index]
            or str(row["manifest_fingerprint"])  # type: ignore[index]
            != snapshot.manifest_fingerprint
            or str(row["adapter_id"]) != MANAGED_PACKAGE_ADAPTER_ID  # type: ignore[index]
            or str(row["adapter_version"])  # type: ignore[index]
            != MANAGED_PACKAGE_ADAPTER_VERSION
            or str(row["adapter_fingerprint"])  # type: ignore[index]
            != MANAGED_PACKAGE_ADAPTER_FINGERPRINT
            or int(row["file_count"]) != len(snapshot.files)  # type: ignore[index]
            or int(row["total_bytes"]) != snapshot.total_bytes  # type: ignore[index]
            or snapshot.manifest_path != source_path
        ):
            raise ManagedSkillPackageDriftError(
                "registered Skill metadata no longer matches its managed package"
            )
        expected_id = self._version_id(
            snapshot.manifest.id,
            snapshot.manifest.version,
            snapshot.package_fingerprint,
        )
        if str(row["id"]) != expected_id:  # type: ignore[index]
            raise ManagedSkillPackageDriftError(
                "registered Skill version identity is invalid"
            )
        if root != self._snapshot_root / expected_id:
            raise ManagedSkillPackageDriftError(
                "managed Skill snapshot is not stored at its content-addressed path"
            )
        return self._version_from_snapshot(expected_id, root, snapshot)

    def _load_registered(self, row: object) -> ManagedSkillVersion:
        loaded = self._load_registered_declaration(row)
        if loaded.manifest.kind == "deterministic_tool":
            try:
                contracts = self._load_contract_pair(loaded.manifest)
            except ManagedSkillPackageError as exc:
                raise ManagedSkillPackageDriftError(
                    "registered Skill contracts could not be revalidated"
                ) from exc
            if (
                row["input_contract_fingerprint"]  # type: ignore[index]
                != contracts.input_contract.fingerprint
                or row["output_contract_fingerprint"]  # type: ignore[index]
                != contracts.output_contract.fingerprint
                or row["contract_bundle_fingerprint"]  # type: ignore[index]
                != contracts.bundle_fingerprint
            ):
                raise ManagedSkillPackageDriftError(
                    "registered Skill contract metadata no longer matches its contracts"
                )
        elif any(
            row[column] is not None  # type: ignore[index]
            for column in (
                "input_contract_fingerprint",
                "output_contract_fingerprint",
                "contract_bundle_fingerprint",
            )
        ):
            raise ManagedSkillPackageDriftError(
                "instruction source unexpectedly exposes execution contract metadata"
            )
        return loaded

    def _resolve_package_root(self, raw: Path) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ManagedSkillPackageError("managed Skill package paths must be absolute")
        root = self._validate_directory(candidate, label="managed Skill package")
        if root.parent != self._snapshot_root:
            raise ManagedSkillPackageError(
                "managed Skill snapshots must be direct children of the snapshot root"
            )
        return root

    def _resolve_source_root(self, raw: Path, *, require_trusted: bool) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ManagedSkillPackageError("Skill import source paths must be absolute")
        root = self._validate_directory(candidate, label="Skill import source")
        if root == self._snapshot_root or _is_within(root, self._snapshot_root):
            raise ManagedSkillPackageError(
                "managed snapshots cannot be reused as import sources"
            )
        if root == self._staging_root or _is_within(root, self._staging_root):
            raise ManagedSkillPackageError(
                "managed staging cannot be reused as an import source"
            )
        if require_trusted and not any(
            self._source_is_within_available_trusted_root(root, trusted)
            for trusted in self._trusted_import_roots
        ):
            raise ManagedSkillImportNotAllowedError(
                "Skill import source is outside the configured trusted roots"
            )
        return root

    def _source_is_within_available_trusted_root(
        self,
        source_root: Path,
        configured_root: Path,
    ) -> bool:
        try:
            trusted = self._validate_directory(
                configured_root,
                label="trusted Skill import root",
            )
        except ManagedSkillPackageError:
            return False
        return source_root == trusted or _is_within(source_root, trusted)

    def _persist_snapshot(
        self,
        version_id: str,
        snapshot: _PackageSnapshot,
    ) -> tuple[Path, bool]:
        destination = self._snapshot_root / version_id
        if destination.exists():
            current = self._snapshot_package(
                self._validate_directory(destination, label="managed Skill snapshot")
            )
            self._assert_same_snapshot(snapshot, current)
            return destination, False
        staging = self._staging_root / f"{version_id}-{uuid4().hex}"
        try:
            staging.mkdir()
            for relative_text, content in snapshot.files:
                target = staging.joinpath(*PurePosixPath(relative_text).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            staged = self._snapshot_package(staging)
            self._assert_same_snapshot(snapshot, staged)
            try:
                for retry_delay in (*SNAPSHOT_PUBLISH_RETRY_DELAYS, None):
                    try:
                        staging.rename(destination)
                        break
                    except PermissionError:
                        if destination.exists() or retry_delay is None:
                            raise
                        time.sleep(retry_delay)
            except OSError:
                if not destination.exists():
                    raise
                current = self._snapshot_package(
                    self._validate_directory(
                        destination,
                        label="concurrent managed Skill snapshot",
                    )
                )
                self._assert_same_snapshot(snapshot, current)
                self._remove_owned_tree(staging, parent=self._staging_root)
                return destination, False
            return destination, True
        except Exception:
            if staging.exists():
                self._remove_owned_tree(staging, parent=self._staging_root)
            raise

    def _snapshot_is_referenced(self, snapshot_root: Path) -> bool:
        source_path = str(snapshot_root / MANIFEST_NAME)
        with self._store.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM skill_version
                WHERE source_path = ? AND snapshot_status = 'managed'
                LIMIT 1
                """,
                (source_path,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _assert_same_snapshot(
        expected: _PackageSnapshot,
        actual: _PackageSnapshot,
    ) -> None:
        if (
            expected.manifest != actual.manifest
            or expected.manifest_fingerprint != actual.manifest_fingerprint
            or expected.package_fingerprint != actual.package_fingerprint
            or expected.files != actual.files
            or expected.total_bytes != actual.total_bytes
        ):
            raise ManagedSkillPackageDriftError(
                "managed Skill snapshot does not match its captured source"
            )

    @staticmethod
    def _import_receipt_id(scope_key: str, version_id: str) -> str:
        digest = hashlib.sha256(
            f"{scope_key}\0{version_id}".encode("utf-8")
        ).hexdigest()
        return f"skill-import-{digest[:32]}"

    @staticmethod
    def _activation_receipt_id(
        scope_key: str,
        skill_id: str,
        revision: int,
        action: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{scope_key}\0{skill_id}\0{revision}\0{action}".encode("utf-8")
        ).hexdigest()
        return f"skill-activation-{digest[:32]}"

    @staticmethod
    def _remove_owned_tree(path: Path, *, parent: Path) -> None:
        if path.parent != parent or not path.name:
            raise ManagedSkillPackageError("refusing to remove a non-owned Skill path")
        try:
            value = path.lstat()
        except FileNotFoundError:
            return
        if _stat_is_link_or_reparse(value) or _is_junction(path):
            raise ManagedSkillPackageError(
                "owned Skill storage contains a link or reparse point"
            )
        if not stat.S_ISDIR(value.st_mode):
            raise ManagedSkillPackageError("owned Skill storage entry is not a directory")
        shutil.rmtree(path)

    @staticmethod
    def _validate_directory(path: Path, *, label: str) -> Path:
        try:
            value = path.lstat()
        except OSError as exc:
            raise ManagedSkillPackageError(f"{label} is not accessible") from exc
        if _stat_is_link_or_reparse(value) or _is_junction(path):
            raise ManagedSkillPackageError(
                f"{label} cannot be a symlink, junction or reparse point"
            )
        if not stat.S_ISDIR(value.st_mode):
            raise ManagedSkillPackageError(f"{label} must be a directory")
        try:
            return path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ManagedSkillPackageError(f"{label} cannot be resolved") from exc

    def _snapshot_package(self, root: Path) -> _PackageSnapshot:
        files: list[tuple[str, Path, os.stat_result]] = []
        seen_casefolded: set[str] = set()
        stack: list[tuple[Path, PurePosixPath, int]] = [(root, PurePosixPath(), 0)]
        total_bytes = 0

        while stack:
            directory, relative_directory, depth = stack.pop()
            if depth > self._limits.max_depth:
                raise ManagedSkillPackageLimitError(
                    "managed Skill package exceeds the directory depth budget"
                )
            try:
                with os.scandir(directory) as iterator:
                    entries = list(iterator)
            except OSError as exc:
                raise ManagedSkillPackageError(
                    "managed Skill package cannot be enumerated"
                ) from exc
            for entry in entries:
                path = Path(entry.path)
                relative = relative_directory / entry.name
                relative_text = relative.as_posix()
                try:
                    # Windows DirEntry.stat() can omit file identity fields that
                    # are present on fstat(). lstat() gives a comparable snapshot
                    # while still preserving link/reparse detection.
                    value = path.lstat()
                except OSError as exc:
                    raise ManagedSkillPackageError(
                        "managed Skill package metadata cannot be read"
                    ) from exc
                if _stat_is_link_or_reparse(value) or _is_junction(path):
                    raise ManagedSkillPackageError(
                        "managed Skill packages cannot contain links, junctions or reparse points"
                    )
                identity = relative_text.casefold()
                if identity in seen_casefolded:
                    raise ManagedSkillPackageError(
                        "managed Skill package paths must be unique case-insensitively"
                    )
                seen_casefolded.add(identity)
                if stat.S_ISDIR(value.st_mode):
                    stack.append((path, relative, depth + 1))
                    continue
                if not stat.S_ISREG(value.st_mode):
                    raise ManagedSkillPackageError(
                        "managed Skill packages may only contain directories and regular files"
                    )
                if int(getattr(value, "st_nlink", 1)) > 1:
                    raise ManagedSkillPackageError(
                        "managed Skill packages cannot contain hardlinked files"
                    )
                if value.st_size > self._limits.max_file_bytes:
                    raise ManagedSkillPackageLimitError(
                        "managed Skill package contains a file over the byte budget"
                    )
                total_bytes += int(value.st_size)
                if total_bytes > self._limits.max_total_bytes:
                    raise ManagedSkillPackageLimitError(
                        "managed Skill package exceeds the total byte budget"
                    )
                files.append((relative_text, path, value))
                if len(files) > self._limits.max_files:
                    raise ManagedSkillPackageLimitError(
                        "managed Skill package exceeds the file-count budget"
                    )

        if not files:
            raise ManagedSkillPackageError("managed Skill package is empty")
        files.sort(key=lambda item: (item[0].casefold(), item[0]))
        package_hasher = hashlib.sha256(b"zhimeng-managed-skill-package-v1\0")
        manifest_bytes: bytes | None = None
        paths: set[str] = set()
        captured_files: list[tuple[str, bytes]] = []
        for relative_text, path, scanned_stat in files:
            content = self._read_stable_file(path, scanned_stat)
            captured_files.append((relative_text, content))
            content_hash = hashlib.sha256(content).hexdigest()
            package_hasher.update(
                _canonical_json(
                    {
                        "path": relative_text,
                        "size": len(content),
                        "sha256": content_hash,
                    }
                )
            )
            package_hasher.update(b"\0")
            paths.add(relative_text)
            if relative_text == MANIFEST_NAME:
                manifest_bytes = content
        if manifest_bytes is None:
            raise ManagedSkillPackageError(
                f"managed Skill package must contain {MANIFEST_NAME} at its root"
            )
        try:
            manifest = SkillManifest.model_validate_json(manifest_bytes, strict=True)
        except (ValidationError, ValueError) as exc:
            raise ManagedSkillPackageError("managed Skill manifest is invalid") from exc
        entrypoint_path: Path | None = None
        if manifest.kind == "deterministic_tool":
            assert manifest.entrypoint is not None
            if manifest.entrypoint not in paths:
                raise ManagedSkillPackageError(
                    "managed Skill entrypoint is not a regular file inside the package"
                )
            entrypoint_path = root.joinpath(*PurePosixPath(manifest.entrypoint).parts)
            if not _is_within(entrypoint_path, root):
                raise ManagedSkillPackageError(
                    "managed Skill entrypoint escapes its package"
                )
        else:
            missing_resources = [
                resource for resource in manifest.resources if resource not in paths
            ]
            if missing_resources:
                raise ManagedSkillPackageError(
                    "instruction source resources are not regular files inside the package"
                )
            resource_bytes = dict(captured_files)["instruction-map.json"]
            try:
                instruction_map = json.loads(resource_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ManagedSkillPackageError(
                    "instruction-map.json must be a UTF-8 JSON object"
                ) from exc
            if not isinstance(instruction_map, dict):
                raise ManagedSkillPackageError(
                    "instruction-map.json must be a UTF-8 JSON object"
                )
        return _PackageSnapshot(
            manifest=manifest,
            manifest_fingerprint=hashlib.sha256(manifest_bytes).hexdigest(),
            package_fingerprint=package_hasher.hexdigest(),
            manifest_path=root / MANIFEST_NAME,
            entrypoint_path=entrypoint_path,
            files=tuple(captured_files),
            total_bytes=total_bytes,
        )

    @staticmethod
    def _read_stable_file(path: Path, scanned_stat: os.stat_result) -> bytes:
        try:
            with path.open("rb") as handle:
                opened_before = os.fstat(handle.fileno())
                if (
                    _stat_is_link_or_reparse(opened_before)
                    or not stat.S_ISREG(opened_before.st_mode)
                    or int(getattr(opened_before, "st_nlink", 1)) > 1
                    or _stat_key(opened_before) != _stat_key(scanned_stat)
                ):
                    raise ManagedSkillPackageError(
                        "managed Skill file changed before it was read"
                    )
                content = handle.read()
                opened_after = os.fstat(handle.fileno())
            final = path.lstat()
        except ManagedSkillError:
            raise
        except OSError as exc:
            raise ManagedSkillPackageError("managed Skill file cannot be read") from exc
        if (
            _stat_key(opened_before) != _stat_key(opened_after)
            or _stat_key(opened_after) != _stat_key(final)
            or len(content) != int(final.st_size)
            or _stat_is_link_or_reparse(final)
            or not stat.S_ISREG(final.st_mode)
            or int(getattr(final, "st_nlink", 1)) > 1
        ):
            raise ManagedSkillPackageError(
                "managed Skill file changed while it was read"
            )
        return content

    @staticmethod
    def _version_id(skill_id: str, version: str, fingerprint: str) -> str:
        digest = hashlib.sha256(
            f"{skill_id}\0{version}\0{fingerprint}".encode("utf-8")
        ).hexdigest()
        return f"skill-version-{digest[:32]}"

    @staticmethod
    def _version_from_snapshot(
        skill_version_id: str,
        root: Path,
        snapshot: _PackageSnapshot,
    ) -> ManagedSkillVersion:
        return ManagedSkillVersion(
            skill_version_id=skill_version_id,
            manifest=snapshot.manifest,
            manifest_fingerprint=snapshot.manifest_fingerprint,
            package_fingerprint=snapshot.package_fingerprint,
            package_root=root,
            manifest_path=snapshot.manifest_path,
            entrypoint_path=snapshot.entrypoint_path,
        )


__all__ = [
    "MANIFEST_NAME",
    "ManagedSkillAdaptationRequiredError",
    "ManagedSkillActivationConflictError",
    "ManagedSkillActivationInvalidError",
    "ManagedSkillError",
    "ManagedSkillImportNotAllowedError",
    "ManagedSkillLimits",
    "ManagedSkillNotFoundError",
    "ManagedSkillPackageDriftError",
    "ManagedSkillPackageError",
    "ManagedSkillPackageLimitError",
    "ManagedSkillProjectNotFoundError",
    "ManagedSkillService",
    "ManagedSkillVersion",
    "ManagedSkillVersionConflictError",
]
