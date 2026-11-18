from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from pydantic import JsonValue, TypeAdapter, ValidationError

from zhimeng_runtime.managed_skill_service import (
    ManagedSkillPackageDriftError,
    ManagedSkillService,
    ManagedSkillVersion,
    REPARSE_POINT_ATTRIBUTE,
)
from zhimeng_runtime.skill_contracts import (
    SkillContractError as _SkillContractError,
    SkillContractRegistry,
)
from zhimeng_runtime.skill_models import (
    DeterministicSkillOutput,
    SkillArtifactOutput,
    SkillArtifactReceipt,
    SkillExecutionError,
    SkillExecutionReceipt,
)


_JSON_VALUE = TypeAdapter(JsonValue)


class _SkillPayloadError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SkillExecutionOutcome:
    receipt: SkillExecutionReceipt
    receipt_hash: str
    result: JsonValue
    result_summary: str | None
    artifacts: tuple[SkillArtifactOutput, ...]
    output_bytes: bytes = b""


class SkillExecutor:
    def __init__(
        self,
        service: ManagedSkillService,
        *,
        python_executable: str | os.PathLike[str] = sys.executable,
        contracts_root: Path | None = None,
        four_skills_bundle_root: Path | None = None,
        max_stdout_bytes: int = 2 * 1024 * 1024,
        max_stderr_bytes: int = 64 * 1024,
        max_artifact_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._service = service
        executable = Path(python_executable)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("python_executable must be an absolute existing file")
        if min(max_stdout_bytes, max_stderr_bytes, max_artifact_bytes) < 1:
            raise ValueError("Skill execution byte budgets must be positive")
        contract_directory = (
            Path(__file__).resolve().parents[4] / "packages" / "contracts"
            if contracts_root is None
            else Path(contracts_root)
        )
        self._python_executable = executable.resolve(strict=True)
        self._contract_registry = SkillContractRegistry(contract_directory)
        self._four_skills_bundle_root: Path | None = None
        if four_skills_bundle_root is not None:
            candidate = Path(four_skills_bundle_root).expanduser()
            if (
                not candidate.is_absolute()
                or not candidate.is_dir()
                or candidate.is_symlink()
            ):
                raise ValueError("four_skills_bundle_root must be an absolute regular directory")
            self._four_skills_bundle_root = candidate.resolve(strict=True)
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._max_artifact_bytes = max_artifact_bytes

    def active_skill(self, project_id: str, skill_id: str) -> ManagedSkillVersion:
        """Return the currently registered immutable package identity.

        ChangeSet post-draft receipts use this small read-only probe to reject
        receipts produced by a package that has since drifted or been replaced.
        Execution itself still goes through ``execute`` and its normal contract
        checks.
        """

        skill = self._service.get_active(project_id, skill_id)
        self._service.verify_active(skill)
        return skill

    def active_skill_declaration(
        self,
        project_id: str,
        skill_id: str,
    ) -> ManagedSkillVersion:
        """Return the immutable active manifest for capability preflight."""

        return self._service.get_active_declaration(project_id, skill_id)

    async def execute(
        self,
        *,
        project_id: str,
        project_revision: int,
        project_fingerprint: str,
        run_id: str,
        tool_call_id: str,
        skill_id: str,
        input_payload: JsonValue,
        project_snapshot: Path,
        run_staging: Path,
        cancellation_event: asyncio.Event | None = None,
    ) -> SkillExecutionOutcome:
        skill = self._service.get_active(project_id, skill_id)
        started_at = datetime.now(UTC)
        started_clock = monotonic()
        input_is_json = True
        try:
            input_value = _JSON_VALUE.validate_python(input_payload, strict=True)
            input_bytes = self._canonical_json(input_value)
        except (TypeError, ValueError, ValidationError):
            input_is_json = False
            input_value = None
            input_bytes = b""
        input_hash = hashlib.sha256(input_bytes).hexdigest()

        if skill.manifest.kind != "deterministic_tool":
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="skill_not_executable",
                message=(
                    "instruction sources are immutable materialization inputs and "
                    "cannot be executed as managed tools"
                ),
                retryable=False,
            )

        if not input_is_json:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_input",
                message="Skill input must be a strict JSON value",
                retryable=False,
            )

        binding = self._service.require_project_binding(project_id)
        if (
            binding is None
            or binding.revision != project_revision
            or binding.fingerprint != project_fingerprint
        ):
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="project_snapshot_stale",
                message="project revision or fingerprint no longer matches the execution request",
                retryable=True,
            )

        try:
            snapshot = self._validate_execution_directory(
                project_snapshot,
                label="project snapshot",
            )
            staging = self._validate_execution_directory(
                run_staging,
                label="run staging",
            )
            self._validate_directory_boundaries(skill, snapshot, staging)
            self._service.verify_active(skill)
        except (OSError, ValueError, ManagedSkillPackageDriftError) as exc:
            code = (
                "package_drift"
                if isinstance(exc, ManagedSkillPackageDriftError)
                else "invalid_execution_boundary"
            )
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code=code,
                message=(
                    "managed Skill package changed after registration"
                    if code == "package_drift"
                    else "Skill execution directories are not isolated regular directories"
                ),
                retryable=False,
            )

        try:
            self._validate_contract_payload(skill.manifest.input_schema, input_value)
        except _SkillPayloadError as exc:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_input",
                message=str(exc),
                retryable=False,
            )
        except _SkillContractError as exc:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_contract",
                message=str(exc),
                retryable=False,
            )

        if cancellation_event is not None and cancellation_event.is_set():
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="cancelled",
                message="Skill execution was cancelled before process start",
                retryable=False,
                execution_status="cancelled",
            )

        process: asyncio.subprocess.Process | None = None
        communicate_task: asyncio.Task[tuple[bytes, bytes]] | None = None
        cancel_task: asyncio.Task[bool] | None = None
        creationflags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        )
        environment = {
            key: os.environ[key]
            for key in (
                "SYSTEMROOT",
                "WINDIR",
                # External read-only writing Skills resolve their configured
                # default roots through Path.home(). These are profile roots,
                # not credentials; secrets and provider variables remain
                # deliberately excluded from the child environment.
                "USERPROFILE",
                "HOME",
                "HOMEDRIVE",
                "HOMEPATH",
                # Optional explicit roots for the bundled four-family CLI
                # adapters. These are path pointers only; no credential or
                # provider environment variable is forwarded.
                "ZHIMENG_FOUR_SKILLS_SOURCE_INGEST_SCRIPT",
                "ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT",
                "ZHIMENG_FOUR_SKILLS_DISTILLATION_SCRIPT",
                "ZHIMENG_FOUR_SKILLS_DISTILLATION_LIBRARY",
                "ZHIMENG_FOUR_SKILLS_KB_MANAGER_SCRIPT",
                "ZHIMENG_FOUR_SKILLS_KB_LIBRARY",
                "ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT",
                "ZHIMENG_FOUR_SKILLS_CREATION_LIBRARY",
            )
            if key in os.environ
        }
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONNOUSERSITE": "1",
                "PYTHONUTF8": "1",
            }
        )
        if self._four_skills_bundle_root is not None:
            environment["ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT"] = str(
                self._four_skills_bundle_root
            )
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._python_executable),
                str(skill.entrypoint_path),
                str(snapshot),
                "--json",
                cwd=str(staging),
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
            communicate_task = asyncio.create_task(
                process.communicate(input=input_bytes + b"\n")
            )
            waiters: set[asyncio.Task[object]] = {communicate_task}  # type: ignore[arg-type]
            if cancellation_event is not None:
                cancel_task = asyncio.create_task(cancellation_event.wait())
                waiters.add(cancel_task)  # type: ignore[arg-type]
            done, _ = await asyncio.wait(
                waiters,
                timeout=skill.manifest.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if communicate_task not in done:
                cancelled = cancel_task is not None and cancel_task in done
                process.kill()
                stdout, stderr = await communicate_task
                return self._failure(
                    skill=skill,
                    project_id=project_id,
                    project_revision=project_revision,
                    project_fingerprint=project_fingerprint,
                    run_id=run_id,
                    tool_call_id=tool_call_id,
                    input_hash=input_hash,
                    output=stdout[: self._max_stdout_bytes],
                    started_at=started_at,
                    started_clock=started_clock,
                    code="cancelled" if cancelled else "timeout",
                    message=(
                        "Skill execution was cancelled"
                        if cancelled
                        else "Skill execution exceeded its manifest timeout"
                    ),
                    retryable=not cancelled,
                    execution_status="cancelled" if cancelled else "failed",
                )
            stdout, stderr = communicate_task.result()
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.kill()
            if communicate_task is not None:
                await asyncio.gather(communicate_task, return_exceptions=True)
            raise
        except (OSError, RuntimeError) as exc:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=b"",
                started_at=started_at,
                started_clock=started_clock,
                code="process_start_failed",
                message="managed Skill process could not be started",
                retryable=True,
            )
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                await asyncio.gather(cancel_task, return_exceptions=True)

        if len(stdout) > self._max_stdout_bytes or len(stderr) > self._max_stderr_bytes:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout[: self._max_stdout_bytes],
                started_at=started_at,
                started_clock=started_clock,
                code="output_limit_exceeded",
                message="managed Skill output exceeded its byte budget",
                retryable=False,
                exit_code=process.returncode,
            )

        try:
            self._service.verify_active(skill)
        except ManagedSkillPackageDriftError:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="package_drift",
                message="managed Skill package changed during execution",
                retryable=False,
                exit_code=process.returncode,
            )

        try:
            parsed = json.loads(stdout.decode("utf-8"))
            output = DeterministicSkillOutput.model_validate(parsed, strict=True)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError):
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_output",
                message="managed Skill stdout is not the declared strict JSON result",
                retryable=False,
                exit_code=process.returncode,
            )

        if process.returncode not in {0, 1}:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="unexpected_exit_code",
                message="managed Skill returned an undeclared exit code",
                retryable=False,
                exit_code=process.returncode,
            )
        if (
            process.returncode == 0
            and output.health not in {"healthy", "degraded"}
        ) or (process.returncode == 1 and output.health != "blocked"):
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="exit_health_mismatch",
                message="managed Skill exit code does not match its domain health",
                retryable=False,
                exit_code=process.returncode,
            )

        try:
            self._validate_contract_payload(skill.manifest.output_schema, output.result)
        except _SkillPayloadError as exc:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_result",
                message=str(exc),
                retryable=False,
                exit_code=process.returncode,
            )
        except _SkillContractError as exc:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_contract",
                message=str(exc),
                retryable=False,
                exit_code=process.returncode,
            )

        try:
            artifact_receipts = self._inspect_artifacts(skill, staging, output)
        except ValueError:
            return self._failure(
                skill=skill,
                project_id=project_id,
                project_revision=project_revision,
                project_fingerprint=project_fingerprint,
                run_id=run_id,
                tool_call_id=tool_call_id,
                input_hash=input_hash,
                output=stdout,
                started_at=started_at,
                started_clock=started_clock,
                code="invalid_skill_artifact",
                message="managed Skill returned an undeclared or unsafe artifact",
                retryable=False,
                exit_code=process.returncode,
            )

        receipt = self._receipt(
            skill=skill,
            project_id=project_id,
            project_revision=project_revision,
            project_fingerprint=project_fingerprint,
            run_id=run_id,
            tool_call_id=tool_call_id,
            input_hash=input_hash,
            output_hash=hashlib.sha256(stdout).hexdigest(),
            started_at=started_at,
            started_clock=started_clock,
            execution_status="succeeded",
            domain_status=output.health,
            exit_code=process.returncode,
            artifacts=artifact_receipts,
            error=None,
        )
        return self._outcome(
            receipt,
            result=output.result,
            result_summary=output.summary,
            artifacts=tuple(output.artifacts),
            output_bytes=stdout,
        )

    def _validate_contract_payload(self, contract_uri: str, payload: JsonValue) -> None:
        """Validate a Skill domain payload against its manifest-bound local contract.

        Contracts are deliberately resolved from the checked-in contract root. A
        manifest URI is an identifier, not a permission to fetch arbitrary URLs.
        The generic Skill envelope remains Pydantic-validated separately; this
        method only validates the domain ``input_payload`` or ``result`` value.
        """

        validator = self._contract_registry.load(contract_uri).validator
        error = next(iter(validator.iter_errors(payload)), None)
        if error is not None:
            path = "$"
            for part in list(error.absolute_path)[:8]:
                path += f"[{part!r}]"
            raise _SkillPayloadError(
                f"payload does not match {contract_uri} at {path}: {error.message}"
            )

    def _failure(
        self,
        *,
        skill: ManagedSkillVersion,
        project_id: str,
        project_revision: int,
        project_fingerprint: str,
        run_id: str,
        tool_call_id: str,
        input_hash: str,
        output: bytes,
        started_at: datetime,
        started_clock: float,
        code: str,
        message: str,
        retryable: bool,
        execution_status: str = "failed",
        exit_code: int | None = None,
    ) -> SkillExecutionOutcome:
        receipt = self._receipt(
            skill=skill,
            project_id=project_id,
            project_revision=project_revision,
            project_fingerprint=project_fingerprint,
            run_id=run_id,
            tool_call_id=tool_call_id,
            input_hash=input_hash,
            output_hash=hashlib.sha256(output).hexdigest(),
            started_at=started_at,
            started_clock=started_clock,
            execution_status=execution_status,
            domain_status=None,
            exit_code=exit_code,
            artifacts=[],
            error=SkillExecutionError(code=code, message=message, retryable=retryable),
        )
        return self._outcome(
            receipt,
            result=None,
            result_summary=None,
            artifacts=(),
            output_bytes=output,
        )

    def _receipt(
        self,
        *,
        skill: ManagedSkillVersion,
        project_id: str,
        project_revision: int,
        project_fingerprint: str,
        run_id: str,
        tool_call_id: str,
        input_hash: str,
        output_hash: str,
        started_at: datetime,
        started_clock: float,
        execution_status: str,
        domain_status: str | None,
        exit_code: int | None,
        artifacts: list[SkillArtifactReceipt],
        error: SkillExecutionError | None,
    ) -> SkillExecutionReceipt:
        identity = hashlib.sha256(
            f"{run_id}\0{tool_call_id}\0{skill.skill_version_id}".encode("utf-8")
        ).hexdigest()
        finished_at = datetime.now(UTC)
        return SkillExecutionReceipt.model_validate(
            {
                "schema": "zhimeng.skill-execution-receipt.v1",
                "receipt_id": f"skill-receipt-{identity[:32]}",
                "run_id": run_id,
                "tool_call_id": tool_call_id,
                "project_id": project_id,
                "project_revision": project_revision,
                "project_fingerprint": project_fingerprint,
                "skill_version_id": skill.skill_version_id,
                "skill_id": skill.manifest.id,
                "skill_version": skill.manifest.version,
                "skill_kind": skill.manifest.kind,
                "manifest_fingerprint": skill.manifest_fingerprint,
                "package_fingerprint": skill.package_fingerprint,
                "permissions": skill.manifest.permissions,
                "permissions_hash": hashlib.sha256(
                    self._canonical_json(
                        skill.manifest.permissions.model_dump(mode="json")
                    )
                ).hexdigest(),
                "input_hash": input_hash,
                "output_hash": output_hash,
                "execution_status": execution_status,
                "domain_status": domain_status,
                "exit_code": exit_code,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": max(0, round((monotonic() - started_clock) * 1000)),
                "artifacts": artifacts,
                "error": error,
            },
            strict=True,
        )

    def _outcome(
        self,
        receipt: SkillExecutionReceipt,
        *,
        result: JsonValue,
        result_summary: str | None,
        artifacts: tuple[SkillArtifactOutput, ...],
        output_bytes: bytes,
    ) -> SkillExecutionOutcome:
        receipt_bytes = self._canonical_json(
            receipt.model_dump(mode="json", by_alias=True)
        )
        return SkillExecutionOutcome(
            receipt=receipt,
            receipt_hash=hashlib.sha256(receipt_bytes).hexdigest(),
            result=result,
            result_summary=result_summary,
            artifacts=artifacts,
            output_bytes=output_bytes,
        )

    def _inspect_artifacts(
        self,
        skill: ManagedSkillVersion,
        staging: Path,
        output: DeterministicSkillOutput,
    ) -> list[SkillArtifactReceipt]:
        declared = set(skill.manifest.artifacts)
        receipts: list[SkillArtifactReceipt] = []
        seen: set[tuple[str, str | None]] = set()
        for artifact in output.artifacts:
            if artifact.artifact_type not in declared:
                raise ValueError("undeclared artifact")
            key = (artifact.artifact_type, artifact.relative_path)
            if key in seen:
                raise ValueError("duplicate artifact")
            seen.add(key)
            file_hash: str | None = None
            if artifact.relative_path is not None:
                target = staging.joinpath(*artifact.relative_path.split("/"))
                self._reject_linked_ancestors(target, staging)
                resolved = target.resolve(strict=True)
                if not self._is_within(resolved, staging):
                    raise ValueError("artifact escapes staging")
                value = resolved.lstat()
                if (
                    not stat.S_ISREG(value.st_mode)
                    or self._is_link_or_reparse(value)
                    or int(getattr(value, "st_nlink", 1)) > 1
                    or value.st_size > self._max_artifact_bytes
                ):
                    raise ValueError("artifact is not a bounded regular file")
                content = resolved.read_bytes()
                if len(content) != value.st_size:
                    raise ValueError("artifact changed while read")
                file_hash = hashlib.sha256(content).hexdigest()
            payload_hash = (
                None
                if artifact.payload is None
                else hashlib.sha256(self._canonical_json(artifact.payload)).hexdigest()
            )
            content_hash = hashlib.sha256(
                self._canonical_json(
                    {"file_sha256": file_hash, "payload_sha256": payload_hash}
                )
            ).hexdigest()
            receipts.append(
                SkillArtifactReceipt(
                    artifact_type=artifact.artifact_type,
                    relative_path=artifact.relative_path,
                    content_hash=content_hash,
                )
            )
        return receipts

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _is_link_or_reparse(value: os.stat_result) -> bool:
        return stat.S_ISLNK(value.st_mode) or bool(
            int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE
        )

    @classmethod
    def _validate_execution_directory(cls, raw: Path, *, label: str) -> Path:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError(f"{label} must be absolute")
        value = path.lstat()
        junction = getattr(os.path, "isjunction", lambda candidate: False)
        if cls._is_link_or_reparse(value) or junction(path) or not stat.S_ISDIR(value.st_mode):
            raise ValueError(f"{label} must be a regular directory")
        return path.resolve(strict=True)

    @classmethod
    def _reject_linked_ancestors(cls, path: Path, root: Path) -> None:
        relative = path.relative_to(root)
        current = root
        junction = getattr(os.path, "isjunction", lambda candidate: False)
        for part in relative.parts:
            current = current / part
            value = current.lstat()
            if cls._is_link_or_reparse(value) or junction(current):
                raise ValueError("artifact path contains a linked component")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _validate_directory_boundaries(
        cls,
        skill: ManagedSkillVersion,
        snapshot: Path,
        staging: Path,
    ) -> None:
        if snapshot == staging or cls._is_within(snapshot, staging) or cls._is_within(staging, snapshot):
            raise ValueError("project snapshot and run staging must not overlap")
        for directory in (snapshot, staging):
            if (
                cls._is_within(directory, skill.package_root)
                or cls._is_within(skill.package_root, directory)
            ):
                raise ValueError("execution directories cannot overlap the managed Skill package")


__all__ = ["SkillExecutionOutcome", "SkillExecutor"]
