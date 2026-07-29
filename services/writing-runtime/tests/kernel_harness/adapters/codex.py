from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from kernel_harness.adapters.base import (
    BoundedEventBuffer,
    EvidenceByteBudget,
    KernelEnvelope,
    KernelRunRef,
    KernelSessionRef,
    KernelSkill,
    ProbeCheck,
    TransportCapabilitySnapshot,
    TransportProbeResult,
)
from kernel_harness.adapters.process_env import (
    isolated_process_environment,
    stage_pinned_binary,
)
from zhimeng_runtime.execution_protocol import CapabilityName, ExecutorIdentity, JsonValue


CODEX_VERSION = "0.138.0"
CODEX_BINARY_SHA256 = "c2e50fa58a6fad1f5be0bbb121d4f161573d8a0f67a14a9dc31027ed0a9b5b9e"
CODEX_V2_SCHEMA_SHA256 = "ce298b555d17f88a6e936425a199c79116e4ccaae082995612c13d62ba46d5bc"
REQUIRED_METHODS = {
    "initialize",
    "thread/start",
    "thread/resume",
    "thread/read",
    "thread/list",
    "turn/start",
    "turn/interrupt",
    "skills/list",
    "skills/extraRoots/set",
}
FORBIDDEN_METHODS = {"thread/shellCommand", "process/spawn"}
HARNESS_SKILL_NAME = "executor-test-skill"
REQUIRED_TRANSPORT_CHECKS = (
    "codex.resource-limits",
    "codex.binary-version",
    "codex.schema",
    "codex.isolation",
    "codex.ephemeral-session",
    "codex.explicit-skill-root",
)


def canonical_json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _evaluate_harness_skill(
    skills: list[KernelSkill],
    *,
    expected_path: Path | None,
    before_hash: str | None,
    after_hash: str | None,
) -> tuple[bool, dict[str, JsonValue]]:
    normalized_expected = (
        None if expected_path is None else expected_path.resolve(strict=False)
    )
    same_name = [skill for skill in skills if skill.name == HARNESS_SKILL_NAME]
    exact_matches = []
    for skill in same_name:
        try:
            observed_path = Path(skill.path).resolve(strict=False)
        except OSError:
            continue
        if observed_path == normalized_expected:
            exact_matches.append(skill)
    enabled_exact_matches = [skill for skill in exact_matches if skill.enabled]
    content_stable = (
        before_hash is not None
        and after_hash is not None
        and before_hash == after_hash
    )
    passed = len(enabled_exact_matches) == 1 and content_stable
    return passed, {
        "expected_path": (
            None if normalized_expected is None else str(normalized_expected)
        ),
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "same_name_count": len(same_name),
        "exact_path_count": len(exact_matches),
        "enabled_exact_path_count": len(enabled_exact_matches),
        "content_stable": content_stable,
    }


def _measured_capabilities_for_skill(
    explicit_skill_ok: bool,
) -> list[CapabilityName]:
    return [CapabilityName.SKILLS] if explicit_skill_ok else []


def _method_names(schema: object) -> set[str]:
    rendered = json.dumps(schema, ensure_ascii=False)
    candidates = {
        *REQUIRED_METHODS,
        *FORBIDDEN_METHODS,
        "command/exec",
        "command/exec/terminate",
    }
    return {method for method in candidates if f'"{method}"' in rendered}


class CodexAppServerAdapter:
    def __init__(
        self,
        *,
        binary: Path,
        project_root: Path,
        skill_roots: list[Path],
        isolation_root: Path,
        evidence_root: Path,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.binary = binary.resolve(strict=True)
        self.project_root = project_root.resolve(strict=True)
        self.skill_roots = [path.resolve(strict=True) for path in skill_roots]
        self.isolation_root = isolation_root.resolve(strict=False)
        self.evidence_root = evidence_root.resolve(strict=False)
        self.timeout_seconds = timeout_seconds
        self.codex_home = self.isolation_root / "codex-home"
        self.schema_root = self.isolation_root / "schema"
        self._environment = isolated_process_environment(
            self.isolation_root,
            extras={
                "CODEX_HOME": str(self.codex_home),
                "LOG_FORMAT": "json",
            },
        )
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_methods: dict[int, str] = {}
        self._events = BoundedEventBuffer()
        self._evidence_budget = EvidenceByteBudget()
        self._request_id = 0
        self._event_sequence = 0
        self._transcript = None
        self._stderr = None
        self._initialize_result: dict[str, Any] | None = None
        self._identity: ExecutorIdentity | None = None
        self._execution_binary: Path | None = None
        self._execution_binary_hash: str | None = None

    @property
    def environment(self) -> dict[str, str]:
        return dict(self._environment)

    @property
    def evidence_secret_values(self) -> tuple[str, ...]:
        return ()

    @property
    def resource_limits(self) -> dict[str, JsonValue]:
        return {
            "adapter_stream_byte_limit": self._evidence_budget.limit_bytes,
            "adapter_stream_bytes_written": self._evidence_budget.bytes_written,
            "adapter_stream_truncated": self._evidence_budget.truncated,
            "event_buffer": self._events.snapshot(),
        }

    def _require_execution_binary(self) -> Path:
        if self._execution_binary is None:
            raise RuntimeError("kernel binary has not passed the pin check")
        return self._execution_binary

    async def _ensure_pinned_binary(self) -> str:
        if self._execution_binary is None:
            path, digest = await asyncio.to_thread(
                stage_pinned_binary,
                self.binary,
                self.isolation_root,
                expected_sha256=CODEX_BINARY_SHA256,
            )
            self._execution_binary = path
            self._execution_binary_hash = digest
        assert self._execution_binary_hash is not None
        return self._execution_binary_hash

    async def _run_command(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(self._require_execution_binary()), *arguments],
                cwd=self.project_root,
                env=self._environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        return await asyncio.to_thread(run)

    async def _generate_schema(self) -> tuple[str, set[str]]:
        if self.schema_root.exists():
            await asyncio.to_thread(shutil.rmtree, self.schema_root)
        self.schema_root.mkdir(parents=True)
        result = await self._run_command(
            "app-server",
            "generate-json-schema",
            "--out",
            str(self.schema_root),
            "--experimental",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Codex schema generation failed: {result.stderr.strip()}")
        schema_file = self.schema_root / "codex_app_server_protocol.v2.schemas.json"
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        return canonical_json_hash(schema), _method_names(schema)

    async def start(self) -> ExecutorIdentity:
        await self._ensure_pinned_binary()
        if self._identity is not None:
            return self._identity
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self._transcript = (self.evidence_root / "native-events.ndjson").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._stderr = (self.evidence_root / "stderr.log").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._process = await asyncio.create_subprocess_exec(
            str(self._require_execution_binary()),
            "app-server",
            "--stdio",
            cwd=self.project_root,
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        response = await self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "zhimeng-kernel-harness",
                    "title": "Zhimeng Kernel Harness",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        if response.get("error"):
            raise RuntimeError(f"Codex initialize failed: {response['error']}")
        self._initialize_result = response.get("result") or {}
        await self._notify("initialized", None)
        self._identity = ExecutorIdentity(
            executor_id="codex-app-server",
            kind="process",
            implementation="OpenAI Codex App Server",
            version=CODEX_VERSION,
            protocol_version="app-server-v2",
            instance_id=f"codex-{self._process.pid}",
        )
        return self._identity

    async def _write_message(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Codex App Server is not running")
        self._process.stdin.write(
            (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
        )
        await self._process.stdin.drain()

    async def _request(self, method: str, params: JsonValue) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self._request_methods[request_id] = method
        await self._write_message({"id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_seconds)
        finally:
            self._pending.pop(request_id, None)
            self._request_methods.pop(request_id, None)

    def _sanitized_transcript_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Keep the protocol shape while excluding discovered Skill instructions."""
        request_id = message.get("id")
        if self._request_methods.get(request_id) != "skills/list":
            return message
        result = message.get("result")
        if not isinstance(result, dict):
            return message
        data = result.get("data")
        if not isinstance(data, list):
            return message
        sanitized_data: list[dict[str, Any]] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            sanitized_entry = dict(entry)
            skills = entry.get("skills")
            if isinstance(skills, list):
                sanitized_entry["skills"] = [
                    {
                        key: item[key]
                        for key in ("name", "path", "scope", "enabled")
                        if key in item
                    }
                    for item in skills
                    if isinstance(item, dict)
                ]
            sanitized_data.append(sanitized_entry)
        sanitized_result = dict(result)
        sanitized_result["data"] = sanitized_data
        sanitized_message = dict(message)
        sanitized_message["result"] = sanitized_result
        return sanitized_message

    async def _notify(self, method: str, params: JsonValue) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write_message(message)

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                decoded = line.decode("utf-8", errors="replace").strip()
                if not decoded:
                    continue
                message = json.loads(decoded)
                if self._transcript is not None:
                    self._evidence_budget.write_text(
                        self._transcript,
                        json.dumps(
                            self._sanitized_transcript_message(message),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                request_id = message.get("id")
                method = message.get("method")
                if request_id is not None and method is None and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(message)
                    continue
                self._event_sequence += 1
                kind = "server_request" if request_id is not None else "notification"
                self._events.put(
                    KernelEnvelope(
                        sequence=self._event_sequence,
                        kind=kind,
                        method=method,
                        request_id=request_id,
                        payload=message,
                    )
                )
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex App Server stdout closed"))
            self._events.close()

    async def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while line := await self._process.stderr.readline():
            if self._stderr is not None:
                self._evidence_budget.write_text(
                    self._stderr,
                    line.decode("utf-8", errors="replace"),
                )

    async def create_session(self, options: JsonValue) -> KernelSessionRef:
        response = await self._request("thread/start", options)
        if response.get("error"):
            raise RuntimeError(f"Codex thread/start failed: {response['error']}")
        thread = response.get("result", {}).get("thread", {})
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError("Codex thread/start returned no thread id")
        return KernelSessionRef(
            upstream_session_id=thread_id,
            normalized_session_id=f"codex:{thread_id}",
            persistent=not bool(options.get("ephemeral")) if isinstance(options, dict) else True,
        )

    async def resume_session(
        self,
        upstream_session_id: str,
        options: JsonValue,
    ) -> KernelSessionRef:
        params = dict(options) if isinstance(options, dict) else {}
        params["threadId"] = upstream_session_id
        response = await self._request("thread/resume", params)
        if response.get("error"):
            raise RuntimeError(f"Codex thread/resume failed: {response['error']}")
        return KernelSessionRef(
            upstream_session_id=upstream_session_id,
            normalized_session_id=f"codex:{upstream_session_id}",
            persistent=True,
        )

    async def start_run(
        self,
        session: KernelSessionRef,
        inputs: JsonValue,
        options: JsonValue,
    ) -> KernelRunRef:
        params = dict(options) if isinstance(options, dict) else {}
        params["threadId"] = session.upstream_session_id
        params["input"] = inputs
        response = await self._request("turn/start", params)
        if response.get("error"):
            raise RuntimeError(f"Codex turn/start failed: {response['error']}")
        turn_id = response.get("result", {}).get("turn", {}).get("id")
        if not turn_id:
            raise RuntimeError("Codex turn/start returned no turn id")
        return KernelRunRef(
            upstream_session_id=session.upstream_session_id,
            upstream_run_id=turn_id,
            normalized_run_id=f"codex:{turn_id}",
        )

    async def cancel_run(self, run: KernelRunRef) -> None:
        response = await self._request(
            "turn/interrupt",
            {"threadId": run.upstream_session_id, "turnId": run.upstream_run_id},
        )
        if response.get("error"):
            raise RuntimeError(f"Codex turn/interrupt failed: {response['error']}")

    async def list_skills(
        self,
        roots: list[str],
        *,
        force_reload: bool = False,
    ) -> list[KernelSkill]:
        set_response = await self._request("skills/extraRoots/set", {"extraRoots": roots})
        if set_response.get("error"):
            raise RuntimeError(f"Codex skills/extraRoots/set failed: {set_response['error']}")
        response = await self._request(
            "skills/list",
            {"cwds": [str(self.project_root)], "forceReload": force_reload},
        )
        if response.get("error"):
            raise RuntimeError(f"Codex skills/list failed: {response['error']}")
        skills: list[KernelSkill] = []
        for entry in response.get("result", {}).get("data", []):
            for item in entry.get("skills", []):
                skills.append(
                    KernelSkill(
                        name=item.get("name", ""),
                        path=item.get("path", ""),
                        enabled=bool(item.get("enabled", True)),
                        metadata=item,
                    )
                )
        return skills

    async def resolve_server_request(
        self,
        request_id: str | int,
        result: JsonValue,
    ) -> None:
        await self._write_message({"id": request_id, "result": result})

    async def events(self):
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def probe(self) -> TransportProbeResult:
        binary_hash = await self._ensure_pinned_binary()
        identity = await self.start()
        version = await self._run_command("--version")
        version_ok = version.returncode == 0 and CODEX_VERSION in (
            version.stdout + version.stderr
        )
        schema_hash, methods = await self._generate_schema()
        schema_ok = schema_hash == CODEX_V2_SCHEMA_SHA256
        method_ok = REQUIRED_METHODS.issubset(methods)
        forbidden_known = FORBIDDEN_METHODS.issubset(methods)
        session = await self.create_session(
            {
                "cwd": str(self.project_root),
                "runtimeWorkspaceRoots": [str(self.project_root)],
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
            }
        )
        expected_skill_candidates = [
            root / "SKILL.md"
            for root in self.skill_roots
            if root.name == HARNESS_SKILL_NAME
        ]
        expected_skill_path = (
            expected_skill_candidates[0]
            if len(expected_skill_candidates) == 1
            else None
        )
        expected_skill_hash_before = (
            None if expected_skill_path is None else _file_sha256(expected_skill_path)
        )
        skills = await self.list_skills(
            [str(path) for path in self.skill_roots],
            force_reload=True,
        )
        expected_skill_hash_after = (
            None if expected_skill_path is None else _file_sha256(expected_skill_path)
        )
        explicit_skill_ok, explicit_skill_evidence = _evaluate_harness_skill(
            skills,
            expected_path=expected_skill_path,
            before_hash=expected_skill_hash_before,
            after_hash=expected_skill_hash_after,
        )
        read = await self._request(
            "thread/read",
            {"threadId": session.upstream_session_id, "includeTurns": False},
        )
        listed = await self._request(
            "thread/list",
            {"cwd": str(self.project_root), "useStateDbOnly": True, "limit": 20},
        )
        listed_ids = {
            item.get("id") for item in listed.get("result", {}).get("data", [])
        }
        initialize_home = (self._initialize_result or {}).get("codexHome")
        allowed_skill_roots = [*self.skill_roots, self.codex_home / "skills" / ".system"]
        unexpected_skill_paths = []
        for skill in skills:
            path = Path(skill.path).resolve(strict=False)
            if not any(
                path == root or path.is_relative_to(root)
                for root in allowed_skill_roots
            ):
                unexpected_skill_paths.append(str(path))
        home_isolated = initialize_home is not None and (
            Path(initialize_home).resolve(strict=False)
            == self.codex_home.resolve(strict=False)
        )
        checks = [
            ProbeCheck(
                check_id="codex.resource-limits",
                passed=(
                    not self._evidence_budget.truncated
                    and not self._events.overflowed
                ),
                summary=(
                    "Shared stream-evidence and bounded event-buffer limits were not exceeded."
                ),
                evidence_ref="evidence://codex/process",
            ),
            ProbeCheck(
                check_id="codex.binary-version",
                passed=version_ok and binary_hash == CODEX_BINARY_SHA256,
                summary="Pinned Codex binary version and SHA-256 match.",
                evidence_ref="evidence://codex/process",
            ),
            ProbeCheck(
                check_id="codex.schema",
                passed=schema_ok and method_ok and forbidden_known,
                summary="Canonical v2 schema matches 0.138 and the allow/deny method set is explicit.",
                evidence_ref="evidence://codex/schema",
            ),
            ProbeCheck(
                check_id="codex.isolation",
                passed=home_isolated and not unexpected_skill_paths,
                summary=(
                    "App Server reports isolated CODEX_HOME and discovers no non-system Skills "
                    "outside explicit harness roots."
                ),
                evidence_ref="evidence://codex/initialize",
            ),
            ProbeCheck(
                check_id="codex.ephemeral-session",
                passed=not read.get("error")
                and session.upstream_session_id not in listed_ids,
                summary="Ephemeral read-only thread is readable and absent from persisted thread list.",
                evidence_ref="evidence://codex/transcript",
            ),
            ProbeCheck(
                check_id="codex.explicit-skill-root",
                passed=explicit_skill_ok,
                summary=(
                    "The enabled harness Skill resolves to the exact configured SKILL.md "
                    "and its bytes remain unchanged during discovery."
                ),
                evidence_ref="evidence://codex/skills",
            ),
        ]
        safe_config = {
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "runtimeWorkspaceRoots": [str(self.project_root)],
            "extraSkillRoots": [str(path) for path in self.skill_roots],
            "forbiddenMethods": sorted(FORBIDDEN_METHODS),
        }
        unexpected_skill_path_fingerprint = canonical_json_hash(
            sorted(unexpected_skill_paths)
        )
        return TransportProbeResult(
            adapter=identity,
            binary_path=str(self._require_execution_binary()),
            binary_hash=binary_hash,
            protocol_schema_hash=schema_hash,
            config_hash=canonical_json_hash(safe_config),
            capabilities=TransportCapabilitySnapshot(
                declared=[
                    CapabilityName.STREAMING,
                    CapabilityName.CANCEL,
                    CapabilityName.RESUME,
                    CapabilityName.FILE_READ,
                    CapabilityName.SKILLS,
                    CapabilityName.APPROVALS,
                    CapabilityName.COMPACTION,
                ],
                measured=_measured_capabilities_for_skill(explicit_skill_ok),
            ),
            session=session,
            checks=checks,
            required_check_ids=list(REQUIRED_TRANSPORT_CHECKS),
            metadata={
                "declared_capability_source": "pinned_app_server_schema",
                "measured_capability_scope": "explicit_skill_discovery_only",
                "required_methods": sorted(REQUIRED_METHODS),
                "forbidden_methods": sorted(FORBIDDEN_METHODS),
                "skill_count": len(skills),
                "explicit_skill_evidence": explicit_skill_evidence,
                "resource_limits": self.resource_limits,
                "unexpected_skill_count": len(unexpected_skill_paths),
                "unexpected_skill_path_fingerprint": unexpected_skill_path_fingerprint,
                "skill_isolation_status": (
                    "pass" if home_isolated and not unexpected_skill_paths else "fail"
                ),
                "process_tree_cleanup_status": "not_measured",
                "provider_used": False,
                "credential_environment_forwarded": False,
            },
        )

    async def close(self) -> None:
        process = self._process
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, RuntimeError):
                    pass
        if self._transcript is not None:
            self._transcript.close()
        if self._stderr is not None:
            self._stderr.close()
        self._process = None
