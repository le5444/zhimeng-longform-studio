from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

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
from kernel_harness.adapters.codex import canonical_json_hash
from kernel_harness.adapters.process_env import (
    isolated_process_environment,
    stage_pinned_binary,
)
from zhimeng_runtime.execution_protocol import CapabilityName, ExecutorIdentity, JsonValue


OPENCODE_VERSION = "1.15.11"
OPENCODE_BINARY_SHA256 = "b0f1670ab389e3f80e660aaaad34780b0db3503c74c5410071e1c733511594c6"
OPENCODE_DOC_SHA256 = "811c008a001b2448ed7dfeb13b7f5cb1089d5f567d797df0d735bb94397b2e53"
OPENCODE_START_ATTEMPTS = 3
REQUIRED_TRANSPORT_CHECKS = (
    "opencode.resource-limits",
    "opencode.binary-version",
    "opencode.auth",
    "opencode.openapi",
    "opencode.sse",
    "opencode.session-recovery",
    "opencode.restart-identity",
    "opencode.no-reply-message",
    "opencode.idle-abort",
    "opencode.cleanup",
)
REQUIRED_PATHS = {
    "/global/health",
    "/event",
    "/session",
    "/session/{sessionID}",
    "/session/{sessionID}/message",
    "/session/{sessionID}/prompt_async",
    "/session/{sessionID}/abort",
    "/permission",
    "/permission/{requestID}/reply",
}


def _is_event_stream_content_type(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    return value.split(";", 1)[0].strip().casefold() == "text/event-stream"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _usage_total(value: object, *, inside_usage: bool = False) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) if inside_usage else 0.0
    if isinstance(value, list):
        return sum(_usage_total(item, inside_usage=inside_usage) for item in value)
    if isinstance(value, dict):
        total = 0.0
        for key, item in value.items():
            is_usage = inside_usage or str(key).casefold() in {"cost", "tokens"}
            total += _usage_total(item, inside_usage=is_usage)
        return total
    return 0.0


def _is_user_only_no_reply_message(value: object, *, expected_text: str) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    message = value[0]
    if not isinstance(message, dict):
        return False
    info = message.get("info")
    parts = message.get("parts")
    if not isinstance(info, dict) or info.get("role") != "user":
        return False
    if not isinstance(parts, list) or not parts:
        return False
    text_parts = [
        part.get("text")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return text_parts == [expected_text] and _usage_total(message) == 0


class OpenCodeServerAdapter:
    def __init__(
        self,
        *,
        binary: Path,
        project_root: Path,
        isolation_root: Path,
        evidence_root: Path,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.binary = binary.resolve(strict=True)
        self.project_root = project_root.resolve(strict=True)
        self.isolation_root = isolation_root.resolve(strict=False)
        self.evidence_root = evidence_root.resolve(strict=False)
        self.timeout_seconds = timeout_seconds
        self.port = 0
        self.origin = "http://127.0.0.1"
        self.username = "zhimeng-harness"
        self.password = secrets.token_urlsafe(32)
        self._environment = isolated_process_environment(
            self.isolation_root,
            extras={
                "OPENCODE_CONFIG_DIR": str(self.isolation_root / "opencode-config"),
                "OPENCODE_CONFIG_CONTENT": "{}",
                "OPENCODE_SERVER_USERNAME": self.username,
                "OPENCODE_SERVER_PASSWORD": self.password,
                "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_UPDATE_CHECK": "true",
                "OPENCODE_DISABLE_MODELS_FETCH": "true",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
                "OPENCODE_DISABLE_AUTOSHARE": "true",
            },
        )
        for key in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_STATE_HOME",
            "OPENCODE_CONFIG_DIR",
        ):
            Path(self._environment[key]).mkdir(parents=True, exist_ok=True)
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout = None
        self._stderr = None
        self._transcript = None
        self._event_queue = BoundedEventBuffer()
        self._evidence_budget = EvidenceByteBudget()
        self._event_sequence = 0
        self._identity: ExecutorIdentity | None = None
        self._process_instance_ids: list[str] = []
        self._execution_binary: Path | None = None
        self._execution_binary_hash: str | None = None
        self._port_start_attempts_per_launch: list[int] = []
        self._last_sse_content_type_is_event_stream: bool | None = None

    @property
    def environment(self) -> dict[str, str]:
        rendered = dict(self._environment)
        if "OPENCODE_SERVER_PASSWORD" in rendered:
            rendered["OPENCODE_SERVER_PASSWORD"] = "<redacted>"
        return rendered

    @property
    def evidence_secret_values(self) -> tuple[str, ...]:
        credentials = f"{self.username}:{self.password}"
        basic_token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return (self.password, credentials, f"Basic {basic_token}", basic_token)

    @property
    def resource_limits(self) -> dict[str, JsonValue]:
        return {
            "adapter_stream_byte_limit": self._evidence_budget.limit_bytes,
            "adapter_stream_bytes_written": self._evidence_budget.bytes_written,
            "adapter_stream_truncated": self._evidence_budget.truncated,
            "event_buffer": self._event_queue.snapshot(),
        }

    @property
    def directory_query(self) -> dict[str, str]:
        return {"directory": str(self.project_root)}

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
                expected_sha256=OPENCODE_BINARY_SHA256,
            )
            self._execution_binary = path
            self._execution_binary_hash = digest
        assert self._execution_binary_hash is not None
        return self._execution_binary_hash

    async def _run_version(self) -> subprocess.CompletedProcess[str]:
        def run() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(self._require_execution_binary()), "--version"],
                cwd=self.project_root,
                env=self._environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        return await asyncio.to_thread(run)

    async def _pipe_log(self, stream: asyncio.StreamReader, handle) -> None:
        while line := await stream.readline():
            self._evidence_budget.write_text(
                handle,
                line.decode("utf-8", errors="replace"),
            )

    async def _start_process_once(self) -> None:
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.isolation_root.mkdir(parents=True, exist_ok=True)
        if self._stdout is None:
            self._stdout = (self.evidence_root / "stdout.log").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._stderr = (self.evidence_root / "stderr.log").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._transcript = (self.evidence_root / "http-transcript.ndjson").open(
                "a", encoding="utf-8", newline="\n"
            )
        self._process = await asyncio.create_subprocess_exec(
            str(self._require_execution_binary()),
            "serve",
            "--pure",
            "--print-logs",
            "--log-level",
            "DEBUG",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(self.port),
            cwd=self.project_root,
            env=self._environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
        )
        assert self._process.stdout is not None and self._process.stderr is not None
        self._stdout_task = asyncio.create_task(
            self._pipe_log(self._process.stdout, self._stdout)
        )
        self._stderr_task = asyncio.create_task(
            self._pipe_log(self._process.stderr, self._stderr)
        )
        self._client = httpx.AsyncClient(
            base_url=self.origin,
            auth=(self.username, self.password),
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        for _ in range(80):
            if self._process.returncode is not None:
                raise RuntimeError(f"OpenCode exited early: {self._process.returncode}")
            try:
                response = await self._client.get("/global/health")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError("OpenCode did not become ready")

    async def _start_process(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        last_error: Exception | None = None
        for attempt in range(1, OPENCODE_START_ATTEMPTS + 1):
            self.port = _free_loopback_port()
            self.origin = f"http://127.0.0.1:{self.port}"
            try:
                await self._start_process_once()
                self._port_start_attempts_per_launch.append(attempt)
                self._bind_current_process_identity()
                return
            except Exception as exc:
                last_error = exc
                await self._stop_process()
        raise RuntimeError(
            f"OpenCode did not become ready after {OPENCODE_START_ATTEMPTS} "
            "bounded port-selection attempts"
        ) from last_error

    async def _record_response(self, method: str, path: str, response: httpx.Response) -> None:
        if self._transcript is None:
            return
        record = {
            "method": method,
            "path": path,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body_sha256": hashlib.sha256(response.content).hexdigest(),
            "body_bytes": len(response.content),
        }
        self._evidence_budget.write_text(
            self._transcript,
            json.dumps(record, separators=(",", ":")) + "\n",
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._client is None:
            raise RuntimeError("OpenCode server is not running")
        response = await self._client.request(method, path, **kwargs)
        await self._record_response(method, path, response)
        return response

    def _bind_current_process_identity(self) -> ExecutorIdentity:
        assert self._process is not None
        if self._process.returncode is not None:
            raise RuntimeError("cannot bind identity to an exited OpenCode process")
        self._identity = ExecutorIdentity(
            executor_id="opencode-server",
            kind="process",
            implementation="OpenCode HTTP/SSE Server",
            version=OPENCODE_VERSION,
            protocol_version="legacy-http-v1",
            instance_id=f"opencode-{self._process.pid}",
        )
        assert self._identity.instance_id is not None
        if (
            not self._process_instance_ids
            or self._process_instance_ids[-1] != self._identity.instance_id
        ):
            self._process_instance_ids.append(self._identity.instance_id)
        return self._identity

    async def start(self) -> ExecutorIdentity:
        await self._ensure_pinned_binary()
        if (
            self._identity is not None
            and self._process is not None
            and self._process.returncode is None
        ):
            return self._identity
        await self._start_process()
        assert self._identity is not None
        return self._identity

    async def _first_sse_event(self) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("OpenCode server is not running")
        async with self._client.stream(
            "GET",
            "/event",
            params=self.directory_query,
            headers={"accept": "text/event-stream"},
        ) as response:
            self._last_sse_content_type_is_event_stream = _is_event_stream_content_type(
                response.headers.get("content-type")
            )
            if response.status_code != 200:
                body = await response.aread()
                if self._transcript is not None:
                    record = {
                        "method": "GET",
                        "path": "/event",
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type"),
                        "body_sha256": hashlib.sha256(body).hexdigest(),
                        "body_bytes": len(body),
                    }
                    self._evidence_budget.write_text(
                        self._transcript,
                        json.dumps(record, separators=(",", ":")) + "\n"
                    )
                detail = body.decode("utf-8", errors="replace")[:1024]
                raise RuntimeError(
                    f"OpenCode SSE failed: {response.status_code}: {detail}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line.removeprefix("data:").strip())
                if self._transcript is not None:
                    payload_bytes = json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self._evidence_budget.write_text(
                        self._transcript,
                        json.dumps(
                            {
                                "method": "GET",
                                "path": "/event",
                                "status": response.status_code,
                                "content_type": response.headers.get("content-type"),
                                "sse_event_type": payload.get("type"),
                                "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                                "payload_bytes": len(payload_bytes),
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                self._event_sequence += 1
                self._event_queue.put(
                    KernelEnvelope(
                        sequence=self._event_sequence,
                        kind="notification",
                        method=payload.get("type"),
                        request_id=None,
                        payload=payload,
                    )
                )
                return payload
        raise RuntimeError("OpenCode SSE stream ended without an event")

    async def create_session(self, options: JsonValue) -> KernelSessionRef:
        body = dict(options) if isinstance(options, dict) else {}
        response = await self._request(
            "POST",
            "/session",
            params=self.directory_query,
            json=body,
        )
        response.raise_for_status()
        session = response.json()
        session_id = session.get("id")
        if not session_id:
            raise RuntimeError("OpenCode session create returned no id")
        return KernelSessionRef(
            upstream_session_id=session_id,
            normalized_session_id=f"opencode:{session_id}",
            persistent=True,
        )

    async def resume_session(
        self,
        upstream_session_id: str,
        options: JsonValue,
    ) -> KernelSessionRef:
        response = await self._request(
            "GET",
            f"/session/{quote(upstream_session_id, safe='')}",
            params=self.directory_query,
        )
        response.raise_for_status()
        return KernelSessionRef(
            upstream_session_id=upstream_session_id,
            normalized_session_id=f"opencode:{upstream_session_id}",
            persistent=True,
        )

    async def start_run(
        self,
        session: KernelSessionRef,
        inputs: JsonValue,
        options: JsonValue,
    ) -> KernelRunRef:
        body = dict(options) if isinstance(options, dict) else {}
        body["parts"] = inputs
        response = await self._request(
            "POST",
            f"/session/{quote(session.upstream_session_id, safe='')}/prompt_async",
            params=self.directory_query,
            json=body,
        )
        response.raise_for_status()
        return KernelRunRef(
            upstream_session_id=session.upstream_session_id,
            upstream_run_id=f"prompt-{session.upstream_session_id}",
            normalized_run_id=f"opencode:prompt-{session.upstream_session_id}",
        )

    async def cancel_run(self, run: KernelRunRef) -> None:
        response = await self._request(
            "POST",
            f"/session/{quote(run.upstream_session_id, safe='')}/abort",
            params=self.directory_query,
        )
        response.raise_for_status()

    async def list_skills(
        self,
        roots: list[str],
        *,
        force_reload: bool = False,
    ) -> list[KernelSkill]:
        raise NotImplementedError(
            "OpenCode 1.15.11 transport probe does not expose trusted-root Skill pinning"
        )

    async def resolve_server_request(
        self,
        request_id: str | int,
        result: JsonValue,
    ) -> None:
        body = dict(result) if isinstance(result, dict) else {"reply": "reject"}
        response = await self._request(
            "POST",
            f"/permission/{quote(str(request_id), safe='')}/reply",
            params=self.directory_query,
            json=body,
        )
        response.raise_for_status()

    async def events(self):
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._process = None

    async def probe(self) -> TransportProbeResult:
        binary_hash = await self._ensure_pinned_binary()
        initial_identity = await self.start()
        version = await self._run_version()
        incorrect_header = base64.b64encode(b"wrong:wrong").decode("ascii")
        async with httpx.AsyncClient(timeout=5, trust_env=False) as unauthenticated_client:
            unauthenticated = await unauthenticated_client.get(
                f"{self.origin}/global/health"
            )
            incorrect = await unauthenticated_client.get(
                f"{self.origin}/global/health",
                headers={"authorization": f"Basic {incorrect_header}"},
            )
        health = await self._request("GET", "/global/health")
        doc = await self._request("GET", "/doc")
        doc_json = doc.json()
        doc_paths = set(doc_json.get("paths", {}))
        bootstrap_list = await self._request(
            "GET", "/session", params=self.directory_query
        )
        bootstrap_list.raise_for_status()
        sse = await asyncio.wait_for(self._first_sse_event(), timeout=5.0)
        session = await self.create_session({"title": "Kernel harness transport probe"})
        session_path = f"/session/{quote(session.upstream_session_id, safe='')}"
        read = await self._request("GET", session_path, params=self.directory_query)
        listed = await self._request("GET", "/session", params=self.directory_query)
        listed.raise_for_status()
        permissions = await self._request("GET", "/permission", params=self.directory_query)
        no_reply = await self._request(
            "POST",
            f"{session_path}/message",
            params=self.directory_query,
            json={
                "noReply": True,
                "parts": [{"type": "text", "text": "transport-only"}],
            },
        )
        messages = await self._request(
            "GET",
            f"{session_path}/message",
            params=self.directory_query,
        )
        messages.raise_for_status()
        abort_idle = await self._request(
            "POST",
            f"{session_path}/abort",
            params=self.directory_query,
        )
        await self._stop_process()
        await self._start_process()
        assert self._identity is not None
        final_identity = self._identity
        listed_after_restart = await self._request(
            "GET", "/session", params=self.directory_query
        )
        listed_after_restart.raise_for_status()
        recovered_ids = {item.get("id") for item in listed_after_restart.json()}
        delete = await self._request(
            "DELETE", session_path, params=self.directory_query
        )
        final_list = await self._request("GET", "/session", params=self.directory_query)
        final_list.raise_for_status()
        final_ids = {item.get("id") for item in final_list.json()}

        message_payload = messages.json()
        user_only_no_reply = _is_user_only_no_reply_message(
            message_payload,
            expected_text="transport-only",
        )
        database = self.isolation_root / "xdg-data" / "opencode" / "opencode.db"
        checks = [
            ProbeCheck(
                check_id="opencode.resource-limits",
                passed=(
                    not self._evidence_budget.truncated
                    and not self._event_queue.overflowed
                ),
                summary=(
                    "Shared stream-evidence and bounded event-buffer limits were not exceeded."
                ),
                evidence_ref="evidence://opencode/process",
            ),
            ProbeCheck(
                check_id="opencode.binary-version",
                passed=version.returncode == 0
                and OPENCODE_VERSION in (version.stdout + version.stderr)
                and binary_hash == OPENCODE_BINARY_SHA256,
                summary="Pinned OpenCode binary version and SHA-256 match.",
                evidence_ref="evidence://opencode/process",
            ),
            ProbeCheck(
                check_id="opencode.auth",
                passed=unauthenticated.status_code == 401
                and incorrect.status_code == 401
                and health.status_code == 200,
                summary="Server auth rejects missing and incorrect Basic credentials.",
                evidence_ref="evidence://opencode/http-transcript",
            ),
            ProbeCheck(
                check_id="opencode.openapi",
                passed=hashlib.sha256(doc.content).hexdigest() == OPENCODE_DOC_SHA256
                and REQUIRED_PATHS.issubset(doc_paths),
                summary="Pinned OpenAPI bytes and documented required transport routes match 1.15.11.",
                evidence_ref="evidence://opencode/openapi",
            ),
            ProbeCheck(
                check_id="opencode.sse",
                passed=(
                    sse.get("type") == "server.connected"
                    and self._last_sse_content_type_is_event_stream is True
                ),
                summary=(
                    "Project SSE returns text/event-stream and emits server.connected."
                ),
                evidence_ref="evidence://opencode/sse",
            ),
            ProbeCheck(
                check_id="opencode.session-recovery",
                passed=bootstrap_list.status_code == 200
                and read.status_code == 200
                and session.upstream_session_id
                in {item.get("id") for item in listed.json()}
                and session.upstream_session_id in recovered_ids,
                summary="Sentinel session survives an isolated server restart.",
                evidence_ref="evidence://opencode/session",
            ),
            ProbeCheck(
                check_id="opencode.restart-identity",
                passed=(
                    initial_identity.instance_id is not None
                    and final_identity.instance_id is not None
                    and initial_identity.instance_id != final_identity.instance_id
                    and self._process_instance_ids[-1] == final_identity.instance_id
                ),
                summary=(
                    "The receipt is bound to the distinct OpenCode process that completed "
                    "the post-restart probe."
                ),
                evidence_ref="evidence://opencode/process",
            ),
            ProbeCheck(
                check_id="opencode.no-reply-message",
                passed=no_reply.status_code == 200 and user_only_no_reply,
                summary=(
                    "noReply persists exactly the sentinel user message, with no assistant "
                    "message or model usage."
                ),
                evidence_ref="evidence://opencode/message",
            ),
            ProbeCheck(
                check_id="opencode.idle-abort",
                passed=abort_idle.status_code == 200 and abort_idle.json() is True,
                summary="Idle abort route returns true; active cancellation remains unmeasured.",
                evidence_ref="evidence://opencode/abort",
            ),
            ProbeCheck(
                check_id="opencode.cleanup",
                passed=permissions.status_code == 200
                and delete.status_code == 200
                and session.upstream_session_id not in final_ids
                and database.exists(),
                summary="Sentinel session is deleted and the database stays in isolated XDG data.",
                evidence_ref="evidence://opencode/cleanup",
            ),
        ]
        safe_config = {
            "hostname": "127.0.0.1",
            "pure": True,
            "serverAuth": True,
            "externalSkills": False,
            "projectConfig": False,
            "defaultPlugins": False,
            "modelsFetch": False,
        }
        isolated_environment_roots = all(
            Path(self._environment[key]).resolve(strict=False).is_relative_to(
                self.isolation_root
            )
            for key in (
                "HOME",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
                "XDG_CACHE_HOME",
                "XDG_STATE_HOME",
                "OPENCODE_CONFIG_DIR",
            )
        )
        return TransportProbeResult(
            adapter=final_identity,
            binary_path=str(self._require_execution_binary()),
            binary_hash=binary_hash,
            protocol_schema_hash=hashlib.sha256(doc.content).hexdigest(),
            config_hash=canonical_json_hash(safe_config),
            capabilities=TransportCapabilitySnapshot(
                declared=[
                    CapabilityName.STREAMING,
                    CapabilityName.CANCEL,
                    CapabilityName.RESUME,
                    CapabilityName.TOOL_CALLS,
                    CapabilityName.FILE_READ,
                    CapabilityName.FILE_WRITE,
                    CapabilityName.APPROVALS,
                    CapabilityName.COMPACTION,
                ],
                measured=[],
            ),
            session=session,
            checks=checks,
            required_check_ids=list(REQUIRED_TRANSPORT_CHECKS),
            metadata={
                "declared_capability_source": "pinned_openapi_routes",
                "measured_capability_scope": "none_of_the_product_capability_contracts",
                "openapi_path_count": len(doc_paths),
                "sse_event": sse.get("type"),
                "sse_content_type_is_event_stream": (
                    self._last_sse_content_type_is_event_stream
                ),
                "idle_abort_only": True,
                "active_abort_measured": False,
                "port_allocation_strategy": "bounded_bind_close_spawn_retry",
                "port_start_attempt_limit_per_launch": OPENCODE_START_ATTEMPTS,
                "port_start_attempts_per_launch": self._port_start_attempts_per_launch,
                "port_start_retry_count": sum(
                    attempts - 1 for attempts in self._port_start_attempts_per_launch
                ),
                "process_instance_ids": list(self._process_instance_ids),
                "initial_process_instance_id": initial_identity.instance_id,
                "final_process_instance_id": final_identity.instance_id,
                "resource_limits": self.resource_limits,
                "port_reservation_status": "not_measured",
                "process_tree_cleanup_status": "not_measured",
                "process_root_isolation_passed": isolated_environment_roots,
                "skill_isolation_status": "not_measured",
                "provider_used": False,
                "credential_environment_forwarded": False,
                "server_auth_secret_scan_status": "deferred_to_runner_after_close",
            },
        )

    async def close(self) -> None:
        await self._stop_process()
        self._event_queue.close()
        for handle in (self._stdout, self._stderr, self._transcript):
            if handle is not None:
                handle.close()
        self._stdout = None
        self._stderr = None
        self._transcript = None
