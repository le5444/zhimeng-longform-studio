"""Runtime-owned pipe or ConPTY sessions with masked, durable output.

A runtime restart keeps the log and marks old work interrupted, without
adopting or killing a PID from the previous runtime.
"""

from __future__ import annotations

import codecs
import json
import locale
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable
import uuid

from zhimeng_runtime.secret_mask import mask_secrets

logger = logging.getLogger(__name__)

# Each tool response delivers a complete UTF-8 page. The cursor advances only
# through that page; the durable logs and remaining output stay available.
COMMAND_OUTPUT_PAGE_BYTES = 24_000


class CommandSessionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _OutputDecoder:
    """Prefer UTF-8; Chinese legacy commands keep their GBK output readable."""

    def __init__(self) -> None:
        self.decoder: Any = codecs.getincrementaldecoder("utf-8")("strict")
        self.encoding = "utf-8"

    def decode(self, data: bytes, final: bool = False) -> str:
        pending = self.decoder.getstate()[0]
        try:
            return self.decoder.decode(data, final=final)
        except UnicodeDecodeError:
            self.encoding = "gbk"
            self.decoder = codecs.getincrementaldecoder("gbk")("replace")
            return self.decoder.decode(pending + data, final=final)


# Keep the families aligned with secret_mask._KNOWN_FORMATS. The streaming
# layer holds a possible prefix before the shared complete-value masker runs.
_PREFIXES = (
    "sk-", *(f"gh{kind}_" for kind in "pousr"), "github_pat_", "AKIA",
    *(f"xox{kind}-" for kind in "abposr"), "AIza",
)
_WORD = re.compile(r"[A-Za-z0-9_]")


class _StreamMask:
    """Mask complete values, even when a credential is split across reads.

    Ordinary text and prompt punctuation are emitted immediately. Only a token
    that could still be a recognizable credential, or follows a sensitive key /
    Bearer prefix, waits for its delimiter. No unmasked spool is written to disk.
    """

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.head = ""
        self.visible = False
        self.sensitive = False
        self.expect_value = False
        self.expect_bearer = False
        self.bearer_value = False
        self.previous_word = ""
        self.key_context = ""
        self.key_gap = False
        self.masked_count = 0

    def _finish(self) -> str:
        if not self.head:
            return ""
        word = "".join(self.parts)
        if self.sensitive:
            prefix = "Bearer " if self.bearer_value else "token="
            masked, count = mask_secrets(prefix + word)
            result = masked[len(prefix):]
        else:
            result, count = mask_secrets(word)
        self.masked_count += count
        result = "" if self.visible else result
        self.previous_word = self.head
        self.parts = []
        self.head = ""
        self.visible = False
        self.sensitive = False
        self.bearer_value = False
        return result

    def feed(self, text: str, *, final: bool = False) -> str:
        output: list[str] = []
        for char in text:
            value_char = not (char.isspace() or char in "\"',;{}[]")
            possible_prefix = not self.visible and bool(self.head) and any(prefix.startswith(self.head) or self.head.startswith(prefix) for prefix in _PREFIXES)
            if ((self.sensitive or self.expect_value) and value_char) or (_WORD.fullmatch(char) is not None) or (possible_prefix and char == "-"):
                if self.key_gap:
                    self.key_context = ""
                    self.key_gap = False
                if not self.head and self.expect_value:
                    self.sensitive = True
                    self.expect_value = False
                    self.bearer_value = self.expect_bearer
                    self.expect_bearer = False
                if len(self.head) < 320:
                    self.head += char
                if len(self.key_context) < 320:
                    self.key_context += char
                if self.visible:
                    output.append(char)
                else:
                    self.parts.append(char)
                    candidate = any(prefix.startswith(self.head) or self.head.startswith(prefix) for prefix in _PREFIXES)
                    if not self.sensitive and not candidate:
                        output.extend(self.parts)
                        self.parts = []
                        self.visible = True
                continue
            output.append(self._finish())
            if char in "=:" and self.key_context:
                _sample, count = mask_secrets(self.key_context + "=example_secret_value")
                self.expect_value = bool(count)
                self.expect_bearer = False
            elif char.isspace() and self.previous_word.lower() == "bearer":
                self.expect_value = True
                self.expect_bearer = True
            elif char not in "\"'" and not char.isspace():
                self.expect_value = False
                self.expect_bearer = False
            output.append(char)
            if char in "-." and len(self.key_context) < 320:
                self.key_context += char
            elif char not in "\"'" and not char.isspace():
                self.key_context = ""
            elif char.isspace() and self.key_context:
                # A separator may follow a key ("api_key = value").
                self.key_context = self.key_context.rstrip() + " "
                self.key_gap = True
            if char not in "\"'" and not char.isspace():
                self.previous_word = ""
        if final:
            output.append(self._finish())
        return "".join(output)


class _WindowsJob:
    """A handle-owned process tree; no taskkill of a saved or reused PID."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        self._limits = limits
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        from ctypes import wintypes
        ctypes = self.ctypes
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):  # type: ignore[attr-defined]
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen closes the primary thread handle. Find that still-suspended
        # thread, attach to the job before its first instruction, then resume it.
        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]
        self.api.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.api.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.api.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenThread.restype = wintypes.HANDLE
        self.api.ResumeThread.argtypes = [wintypes.HANDLE]
        self.api.ResumeThread.restype = wintypes.DWORD
        snapshot = self.api.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        resumed = False
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = self.api.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    thread = self.api.OpenThread(2, False, entry.th32ThreadID)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                        resumed = True
                    finally:
                        self.api.CloseHandle(thread)
                found = self.api.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            self.api.CloseHandle(snapshot)
        if not resumed:
            raise OSError("找不到新命令的启动线程。")

    def active(self) -> bool:
        from ctypes import wintypes
        ctypes = self.ctypes
        class Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong), ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong), ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]
        account = Accounting()
        if not self.api.QueryInformationJobObject(self.handle, 1, ctypes.byref(account), ctypes.sizeof(account), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(account.ActiveProcesses)

    def terminate(self) -> None:
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def _member_handles(self) -> list[int]:
        """Hold identities before termination; a process count is not an exit wait."""
        from ctypes import wintypes
        ctypes = self.ctypes
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        capacity = 16
        while True:
            class ProcessIds(ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                            ("ids", ctypes.c_size_t * capacity)]
            info = ProcessIds()
            if self.api.QueryInformationJobObject(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info), None):
                break
            error = ctypes.get_last_error()
            if error != 234:  # ERROR_MORE_DATA: processes may have forked during the query.
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, info.assigned)
        handles = []
        try:
            for pid in info.ids[:info.count]:
                handle = self.api.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
                if not handle:
                    error = ctypes.get_last_error()
                    if error == 87:  # It exited before OpenProcess.
                        continue
                    raise ctypes.WinError(error)
                member = wintypes.BOOL()
                if not self.api.IsProcessInJob(handle, self.handle, ctypes.byref(member)):
                    error = ctypes.get_last_error()
                    self.api.CloseHandle(handle)
                    raise ctypes.WinError(error)
                if member.value:
                    handles.append(handle)
                else:
                    self.api.CloseHandle(handle)  # A reused PID must not become our wait target.
            return handles
        except BaseException:
            for handle in handles:
                self.api.CloseHandle(handle)
            raise

    def terminate_and_wait(self, timeout: float = 10) -> None:
        from ctypes import wintypes
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        deadline = time.monotonic() + timeout
        handles: list[int] = []
        try:
            try:
                handles.extend(self._member_handles())
            finally:
                self.terminate()  # Even a failed snapshot must not leave the command running.
            # Also capture members created between the first snapshot and termination.
            handles.extend(self._member_handles())
            for handle in handles:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                result = self.api.WaitForSingleObject(handle, remaining_ms)
                if result == 258:
                    raise TimeoutError("命令子进程尚未确认退出。")
                if result != 0:
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
            while self.active():
                if time.monotonic() >= deadline:
                    raise TimeoutError("命令进程组尚未确认退出。")
                time.sleep(0.01)
        finally:
            for handle in handles:
                self.api.CloseHandle(handle)

    def release(self) -> None:
        """A normally finished shell may intentionally leave detached services running."""
        self._limits.BasicLimitInformation.LimitFlags = 0
        try:
            if not self.api.SetInformationJobObject(self.handle, 9, self.ctypes.byref(self._limits), self.ctypes.sizeof(self._limits)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
        finally:
            self.close()

    def close(self) -> None:
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def cleanup_command_script(process: subprocess.Popen) -> None:
    """The owning capture/session calls this after the shell exits."""
    script = getattr(process, "_zhimeng_command_script", None)
    if script is not None:
        try:
            script.unlink(missing_ok=True)
            process._zhimeng_command_script = None
        except OSError:
            logger.warning("Temporary command script could not be removed")


def _command_script(command: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="zhimeng-command-", suffix=".cmd")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as script:
            script.write("@echo off\nchcp 65001 >nul\n")
            script.write(command.replace("\r\n", "\n").replace("\r", "\n") + "\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def start_command_process(command: str | list[str], *, cwd: Path, env: dict[str, str] | None = None,
                          stdin: int | None = None, windows_job: bool = True,
                          shell: bool = True,
                          ) -> tuple[subprocess.Popen[bytes], _WindowsJob | None]:
    """Share pipe setup and optional Windows process ownership."""
    job = None
    process = None
    command_script = None
    options: dict[str, Any] = dict(shell=shell, cwd=str(cwd), env=env, stdin=stdin,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, start_new_session=os.name != "nt")
    try:
        if os.name == "nt":
            if isinstance(command, str) and ("\n" in command or "\r" in command):
                # cmd /c silently stops at the first physical newline. Execute
                # actual multiline CMD input as a UTF-8 batch so every line and
                # its real diagnostics reach the caller. Embedded multiline
                # language strings still follow CMD syntax; no source rewriting.
                command_script = _command_script(command)
                command = f'"{command_script}"'
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            flags = subprocess.CREATE_NO_WINDOW
            if windows_job:
                job = _WindowsJob()
                flags |= 4  # Assign the suspended child before its first instruction.
            options.update(creationflags=flags, startupinfo=startup)
        process = subprocess.Popen(command, **options)
        process._zhimeng_command_script = command_script
        if job is not None:
            job.attach_and_resume(process)
        return process, job
    except BaseException:
        if process is not None:
            process.kill()
            process.wait(timeout=10)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        if job is not None:
            job.close()
        if command_script is not None:
            command_script.unlink(missing_ok=True)
        raise


class _TerminalMask:
    """Keep ANSI bytes out of word detection; mask text across SGR styling."""
    def __init__(self):
        self.text = _StreamMask()
        self.escape = ""
        self.control_masks = 0

    @property
    def masked_count(self):
        return self.text.masked_count + self.control_masks

    def feed(self, value: str, *, final: bool = False) -> str:
        parts = []
        for char in value:
            if not self.escape and char != "\x1b":
                parts.append(self.text.feed(char))
                continue
            self.escape += char
            if len(self.escape) < 2:
                continue
            kind = self.escape[1]
            if kind == "[":
                complete = len(self.escape) > 2 and "@" <= char <= "~"
            elif kind in "]PX^_":
                complete = char == "\x07" or self.escape.endswith("\x1b\\")
            else:
                complete = "0" <= char <= "~"
            if complete:
                # SGR only changes styling, so a token split across it remains
                # one token. Cursor/control operations establish a word boundary.
                pending_secret = (bool(self.text.head) and not self.text.visible) or self.text.expect_value
                if not pending_secret and not (kind == "[" and char == "m") and kind not in "]PX^_":
                    parts.append(self.text.feed("\0")[:-1])
                control, count = mask_secrets(self.escape)
                self.control_masks += count
                parts.append(control)
                self.escape = ""
        if final:
            parts.append(self.text.feed("", final=True))
            control, count = mask_secrets(self.escape)
            self.control_masks += count
            parts.append(control)
            self.escape = ""
        return "".join(parts)


class _Session:
    def __init__(self, folder: Path, metadata: dict[str, Any]) -> None:
        self.folder = folder
        self.meta = metadata
        self.condition = threading.Condition(threading.RLock())
        self.process: subprocess.Popen[bytes] | None = None
        self.job: _WindowsJob | None = None
        self.reader_threads: list[threading.Thread] = []
        self.monitor: threading.Thread | None = None
        self.stop_requested = False
        self.timed_out = False
        self.started = time.monotonic()
        self.write_lock = threading.Lock()
        self.completion_lock = threading.Lock()
        self.completion_published = False
        self.terminal_sequence = 0
        self.terminal_acks: dict[int, str | None] = {}

    def persist(self) -> None:
        target = self.folder / "session.json"
        temporary = self.folder / "session.json.tmp"
        temporary.write_bytes(json.dumps(self.meta, ensure_ascii=False).encode("utf-8"))
        os.replace(temporary, target)


class CommandSessionManager:
    def __init__(self, root: Path, *, on_completion: Callable[[dict[str, Any]], object] | None = None,
                 finalize_files: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._cancelled_runs: set[tuple[str, str]] = set()
        self._closed = False
        self.on_completion = on_completion
        self.finalize_files = finalize_files
        for target in self.root.glob("*/session.json"):
            try:
                metadata = json.loads(target.read_bytes())
                if metadata.get("schema") != "zhimeng.command-session.v1":
                    continue
                session = _Session(target.parent, metadata)
                if metadata.get("status") == "running":
                    metadata.update(status="interrupted", ended_at=time.time(), interruption_reason="运行时已重启；旧命令未重新接管，也未自动重放。")
                    if metadata.get("completion_version") == 1:
                        metadata.update(completion_ready=True, completion_eligible=False)
                    if metadata.get("file_tracking"):
                        metadata["file_tracking"] = {**metadata["file_tracking"], "status": "interrupted",
                            "note": "运行时中断，执行前备份保留；尚未核对结束时文件，不自动生成完成或撤回声明。"}
                    session.persist()
                self._sessions[metadata["session_id"]] = session
            except (OSError, ValueError, KeyError, TypeError):
                continue
        self.reconcile_completions()

    @staticmethod
    def _output_state(session: _Session) -> tuple[dict[str, int | None], str | None]:
        sizes: dict[str, int | None] = {}
        errors = [session.meta["output_error"]] if session.meta.get("output_error") else []
        for name in ("stdout", "stderr"):
            try:
                with (session.folder / (name + ".log")).open("rb") as stream:
                    sizes[name] = stream.seek(0, 2)
            except OSError:
                sizes[name] = None
                errors.append(f"{name}.log 缺失或无法读取")
        return sizes, "；".join(errors) or None

    def _publish_completion(self, session: _Session) -> None:
        if self.on_completion is None:
            return
        with session.completion_lock:
            if session.completion_published:
                return
            with session.condition:
                if (session.meta.get("completion_version") != 1 or not session.meta.get("completion_ready")
                        or session.meta["status"] == "running"):
                    return
                payload = dict(session.meta)
                sizes, output_error = self._output_state(session)
                payload["cursor"] = {name: size for name, size in sizes.items() if size is not None}
                if output_error:
                    payload.update(output_error=output_error, completion_eligible=False)
                payload.update({name + "_file": str(session.folder / (name + ".log")) for name in ("stdout", "stderr")})
            # Callback may persist to SQLite and wake the event loop; it never
            # runs under command manager/condition locks or while pipe reads run.
            try:
                self.on_completion(payload)
            except Exception:
                logger.warning("Command completion could not be published: %s", session.meta["session_id"])
                return
            session.completion_published = True

    def reconcile_completions(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            self._publish_completion(session)

    def _get(self, owner_session_id: str, command_session_id: str) -> _Session:
        with self._lock:
            session = self._sessions.get(command_session_id)
        if session is None or session.meta["owner_session_id"] != owner_session_id:
            raise CommandSessionError("command_session_not_found", "这个对话里找不到该命令会话。")
        return session

    def recovery_context(self, owner_session_id: str, command_session_id: str) -> tuple[str, str]:
        session = self._get(owner_session_id, command_session_id)
        with session.condition:
            if session.meta['status'] == 'running':
                raise CommandSessionError('command_still_running', '命令仍在运行，请结束后再核对恢复。')
            observation_id = (session.meta.get('file_tracking') or {}).get('id')
            if not observation_id:
                raise CommandSessionError('command_recovery_unavailable', '这个命令没有执行前文件备份。')
            return observation_id, session.meta['owner_run_id']

    def start(self, *, owner_session_id: str, owner_run_id: str, command: str,
              cwd: Path, yield_time_ms: int = 1000, env: dict[str, str] | None = None,
              timeout_seconds: float | None = None, stop_generation: int = 0,
              tty: bool = False, rows: int = 24, columns: int = 80,
              file_tracking: dict[str, Any] | None = None) -> dict[str, Any]:
        if not command.strip():
            raise CommandSessionError("command_session_invalid", "命令内容为空。")
        if yield_time_ms < 0 or (timeout_seconds is not None and timeout_seconds <= 0):
            raise CommandSessionError("command_session_invalid", "等待时间应为非负数，指定的执行时限应大于零。")
        if tty and os.name != "nt":
            raise CommandSessionError("command_terminal_unavailable", "此运行时尚未提供当前系统的交互终端。")
        if tty and not (1 <= rows <= 32767 and 1 <= columns <= 32767):
            raise CommandSessionError("command_terminal_size_invalid", "终端行列应在1至32767之间。")
        with self._lock:
            if self._closed:
                raise CommandSessionError("command_sessions_closed", "运行时正在关闭。")
            if (owner_session_id, owner_run_id) in self._cancelled_runs:
                raise CommandSessionError("command_run_cancelled", "这一轮已经停止，命令没有启动。")
            session_id = "cmd_" + uuid.uuid4().hex
            folder = self.root / session_id
            folder.mkdir()
            for name in ("stdout", "stderr"):
                (folder / (name + ".log")).touch()
            masked_command, _ = mask_secrets(command)
            session = _Session(folder, dict(schema="zhimeng.command-session.v1", session_id=session_id,
                owner_session_id=owner_session_id, owner_run_id=owner_run_id, command=masked_command,
                cwd=str(cwd), status="running", exit_code=None, created_at=time.time(),
                ended_at=None, masked_secrets=0, timeout_seconds=timeout_seconds,
                completion_version=1, completion_ready=False, completion_eligible=False,
                stop_generation=stop_generation, transport="pty" if tty else "pipes",
                terminal_rows=rows if tty else None, terminal_columns=columns if tty else None,
                terminal_windows_build=sys.getwindowsversion().build if tty and os.name == "nt" else None))
            if file_tracking is not None:
                session.meta["file_tracking"] = file_tracking
            session.persist()
            try:
                if tty:
                    script = _command_script(command)
                    try:
                        session.process, session.job = start_command_process(
                            [sys.executable, "-u", str(Path(__file__).with_name("terminal_host.py"))],
                            cwd=cwd, env=env, stdin=subprocess.PIPE, shell=False)
                        session.process._zhimeng_command_script = script
                    except BaseException:
                        script.unlink(missing_ok=True)
                        raise
                else:
                    session.process, session.job = start_command_process(command, cwd=cwd, env=env, stdin=subprocess.PIPE)
            except BaseException:
                session.meta.update(status="interrupted", ended_at=time.time(), interruption_reason="命令启动失败。")
                session.persist()
                raise
            self._sessions[session_id] = session
            for name in ("stdout", "stderr"):
                thread = threading.Thread(target=self._read_terminal if tty and name == "stdout" else self._read_pipe,
                                          args=(session, name), daemon=True)
                session.reader_threads.append(thread)
                thread.start()
            session.monitor = threading.Thread(target=self._monitor, args=(session,), daemon=True)
            session.monitor.start()
        if tty:
            try:
                self._terminal_request(session, {"script": str(script), "rows": rows, "columns": columns})
            except BaseException:
                self._stop_session(session)
                raise
        result = self.read(owner_session_id, session_id, wait_ms=yield_time_ms)
        with session.condition:
            session.meta.update(completion_ready=True, completion_eligible=result["status"] == "running")
            session.persist()
            result.update(completion_ready=True, completion_eligible=result["status"] == "running")
        self._publish_completion(session)
        return result

    def _read_terminal(self, session: _Session, name: str) -> None:
        masker = _TerminalMask()
        pipe = session.process.stdout
        try:
            with (session.folder / "stdout.log").open("ab", buffering=0) as log:
                for line in iter(pipe.readline, b""):
                    value = json.loads(line)
                    with session.condition:
                        if "ack" in value:
                            session.terminal_acks[value["ack"]] = value.get("error")
                        if "output" in value:
                            log.write(masker.feed(value["output"]).encode("utf-8"))
                        if "resize" in value:
                            rows, columns = value["resize"]
                            log.write(masker.feed(f"\x1b[8;{rows};{columns}t").encode("utf-8"))
                        session.condition.notify_all()
                with session.condition:
                    log.write(masker.feed("", final=True).encode("utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            with session.condition:
                session.meta["output_error"] = f"终端输出读取失败：{type(exc).__name__}"
        finally:
            pipe.close()
            with session.condition:
                session.meta["masked_secrets"] += masker.masked_count
                session.condition.notify_all()

    def _terminal_request(self, session: _Session, request: dict[str, Any]) -> None:
        # Serialize inputs and acknowledge the ConPTY write/resize. A transport
        # failure is explicit; callers must not replay potentially applied input.
        with session.write_lock:
            with session.condition:
                if session.meta["status"] != "running" or session.process is None:
                    raise CommandSessionError("command_session_not_running", "这个终端已经结束。")
                session.terminal_sequence += 1
                sequence = session.terminal_sequence
            try:
                data = memoryview((json.dumps({**request, "id": sequence}, ensure_ascii=False) + "\n").encode("utf-8"))
                while data:
                    written = session.process.stdin.write(data)
                    if not written:
                        raise BrokenPipeError()
                    data = data[written:]
                session.process.stdin.flush()
                deadline = time.monotonic() + 10
                with session.condition:
                    while sequence not in session.terminal_acks and session.process.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        session.condition.wait(min(remaining, 0.1))
                    if sequence not in session.terminal_acks:
                        raise OSError("Terminal acknowledgement unavailable")
                    error = session.terminal_acks.pop(sequence)
                    if error:
                        raise OSError(error)
            except (OSError, ValueError) as exc:
                raise CommandSessionError("command_terminal_unconfirmed", "终端操作未确认，请先核对输出；未自动重发输入。") from exc

    def resize(self, owner_session_id: str, command_session_id: str, *, rows: int, columns: int) -> dict[str, Any]:
        session = self._get(owner_session_id, command_session_id)
        if session.meta.get("transport") != "pty":
            raise CommandSessionError("command_terminal_required", "此命令使用普通管道，没有终端尺寸。")
        if not (1 <= rows <= 32767 and 1 <= columns <= 32767):
            raise CommandSessionError("command_terminal_size_invalid", "终端行列应在1至32767之间。")
        self._terminal_request(session, {"rows": rows, "columns": columns})
        with session.condition:
            session.meta.update(terminal_rows=rows, terminal_columns=columns)
            session.persist()
            return self._summary(session)

    def _read_pipe(self, session: _Session, name: str) -> None:
        assert session.process is not None
        pipe = getattr(session.process, name)
        decoder = _OutputDecoder()
        masker = _StreamMask()
        recorded_masks = 0
        try:
            with (session.folder / (name + ".log")).open("ab", buffering=0) as log:
                while True:
                    data = pipe.read(8192)
                    final = not data
                    text = masker.feed(decoder.decode(data or b"", final=final), final=final)
                    if text:
                        with session.condition:
                            log.write(text.encode("utf-8"))
                            session.meta["masked_secrets"] += masker.masked_count - recorded_masks
                            recorded_masks = masker.masked_count
                            session.condition.notify_all()
                    if final:
                        break
        except (OSError, ValueError) as exc:
            with session.condition:
                session.meta["output_error"] = f"读取 {name} 输出失败：{type(exc).__name__}"
        finally:
            pipe.close()
            with session.condition:
                session.meta["masked_secrets"] += masker.masked_count - recorded_masks
                session.condition.notify_all()

    def _kill(self, session: _Session) -> None:
        assert session.process is not None
        if session.job is not None:
            session.job.terminate()
        else:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _monitor(self, session: _Session) -> None:
        assert session.process is not None
        process = session.process
        try:
            while True:
                if process.poll() is not None and (session.job is None or not session.job.active()):
                    break
                with session.condition:
                    timeout = session.meta["timeout_seconds"]
                    if timeout is not None and time.monotonic() - session.started >= timeout:
                        session.timed_out = True
                        session.stop_requested = True
                    if session.stop_requested:
                        self._kill(session)
                time.sleep(0.025)
            process.wait()
            for reader in session.reader_threads:
                reader.join()
        finally:
            if process.stdin:
                process.stdin.close()
            cleanup_command_script(process)
            tracking = session.meta.get("file_tracking")
            if tracking and self.finalize_files:
                try:
                    session.meta["file_tracking"] = self.finalize_files(tracking)
                except Exception as exc:
                    # Receipt failure must not leave an already-exited process
                    # permanently running or prevent Job handle cleanup.
                    session.meta["file_tracking"] = {**tracking, "status": "unverified",
                        "problems": [*tracking.get("problems", []),
                            {"path": "", "reason": f"文件记录未完成：{type(exc).__name__}"}]}
                if session.meta["file_tracking"].get("change_id"):
                    session.meta["change_id"] = session.meta["file_tracking"]["change_id"]
            with session.condition:
                session.meta.update(status="cancelled" if session.stop_requested else "exited",
                    exit_code=process.returncode, ended_at=time.time(), timed_out=session.timed_out,
                    duration_ms=int((time.monotonic() - session.started) * 1000))
                if session.job is not None:
                    session.job.close()
                session.persist()
                session.condition.notify_all()
            self._publish_completion(session)

    def _payload(self, session: _Session, cursor: dict[str, int] | None = None) -> dict[str, Any]:
        cursor = cursor or {}
        payload = dict(session.meta)
        payload["schema"] = "zhimeng.tool-result.command-session.v1"
        payload["cursor"] = {}
        for name in ("stdout", "stderr"):
            offset = cursor.get(name, 0)
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise CommandSessionError("command_session_cursor_invalid", "命令输出位置无效。")
            target = session.folder / (name + ".log")
            try:
                page = self._log_page(target, offset=offset, before=None, max_bytes=COMMAND_OUTPUT_PAGE_BYTES)
            except (OSError, UnicodeError) as exc:
                raise CommandSessionError("command_output_unavailable", f"{name}.log 缺失、无法读取或编码损坏；命令记录仍保留。") from exc
            payload[name] = page["text"]
            payload["cursor"][name] = page["next_offset"]
            payload[name + "_bytes"] = page["total_bytes"]
            payload[name + "_has_more"] = page["has_more"]
            payload[name + "_file"] = str(target)
        payload["duration_ms"] = session.meta.get("duration_ms", int((time.time() - session.meta["created_at"]) * 1000))
        payload["cancelled"] = session.meta["status"] == "cancelled"
        payload["timed_out"] = session.meta.get("timed_out", False)
        payload["transport"] = session.meta.get("transport", "pipes")
        payload["stdin_encoding"] = "utf-8" if payload["transport"] == "pty" else locale.getencoding()
        if session.meta["status"] == "running":
            payload["note"] = "命令仍在运行；后续可读取新输出、写入标准输入或停止。此会话使用管道，命令自行刷新输出后即可读到。"
            if payload["transport"] == "pty":
                payload["note"] = "交互终端仍在运行，stdout是合并的终端输出，含ANSI控制序列。可发送文字、Enter（\\r）和Ctrl+C（\\u0003），或调整终端尺寸。"
        if payload["stdout_has_more"] or payload["stderr_has_more"]:
            payload["note"] = (payload.get("note", "") + " 输出按页返回，尚有已保存的内容未读；"
                "可用 command_session 的 read 操作和本次返回的 cursor 继续读取，即使命令已经退出。"
                "stdout_file、stderr_file 保留完整日志位置。").strip()
        if session.meta["status"] == "running" or payload["stdout_has_more"] or payload["stderr_has_more"]:
            payload["next_read"] = {"action": "read", "session_id": session.meta["session_id"],
                                    "cursor": dict(payload["cursor"]), "wait_ms": 1000 if session.meta["status"] == "running" else 0}
        return payload

    def read(self, owner_session_id: str, command_session_id: str, *,
             cursor: dict[str, int] | None = None, wait_ms: int = 0) -> dict[str, Any]:
        if wait_ms < 0:
            raise CommandSessionError("command_session_invalid", "等待时间应为非负数。")
        session = self._get(owner_session_id, command_session_id)
        deadline = time.monotonic() + wait_ms / 1000
        with session.condition:
            while True:
                payload = self._payload(session, cursor)
                if payload["stdout"] or payload["stderr"] or payload["status"] != "running":
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return payload
                session.condition.wait(remaining)

    def write(self, owner_session_id: str, command_session_id: str, *, text: str = "",
              close_stdin: bool = False, cursor: dict[str, int] | None = None,
              wait_ms: int = 0, encoding: str | None = None) -> dict[str, Any]:
        session = self._get(owner_session_id, command_session_id)
        self._write_input(session, text=text, close_stdin=close_stdin, encoding=encoding)
        return self.read(owner_session_id, command_session_id, cursor=cursor, wait_ms=wait_ms)

    def input_summary(self, owner_session_id: str, command_session_id: str, *, text: str) -> dict[str, Any]:
        session = self._get(owner_session_id, command_session_id)
        self._write_input(session, text=text)
        with session.condition:
            return self._summary(session)

    def _write_input(self, session: _Session, *, text: str, close_stdin: bool = False, encoding: str | None = None) -> None:
        if session.meta.get("transport") == "pty":
            content = text.replace("\r\n", "\r").replace("\n", "\r")
            self._terminal_request(session, {"input": content + ("\x1a\r" if close_stdin else "")})
            return
        try:
            data = text.encode(encoding or locale.getencoding())
        except (LookupError, UnicodeError) as exc:
            raise CommandSessionError("command_session_encoding_invalid", "这段输入无法用指定编码发送，可为该命令指定合适的编码。") from exc
        with session.write_lock:
            with session.condition:
                if session.meta["status"] != "running" or session.process is None:
                    raise CommandSessionError("command_session_not_running", "这个命令已经结束，无法继续输入。")
                stream = session.process.stdin
                if stream is None or stream.closed:
                    raise CommandSessionError("command_session_stdin_closed", "这个命令的标准输入已经关闭。")
            try:
                if text:
                    remaining = memoryview(data)
                    while remaining:
                        written = stream.write(remaining)
                        if not written:
                            raise BrokenPipeError("标准输入已关闭。")
                        remaining = remaining[written:]
                    stream.flush()
                if close_stdin:
                    stream.close()
            except (OSError, ValueError) as exc:
                raise CommandSessionError("command_session_stdin_closed", "命令已关闭标准输入。") from exc

    def stop(self, owner_session_id: str, command_session_id: str, *,
             cursor: dict[str, int] | None = None) -> dict[str, Any]:
        session = self._get(owner_session_id, command_session_id)
        self._stop_session(session)
        return self.read(owner_session_id, command_session_id, cursor=cursor)

    def _stop_session(self, session: _Session) -> None:
        with session.condition:
            if session.process is not None and session.meta["status"] == "running":
                session.stop_requested = True
                self._kill(session)
                session.condition.notify_all()
        if session.monitor is not None:
            session.monitor.join()

    @staticmethod
    def _summary(session: _Session) -> dict[str, Any]:
        """Browser metadata without loading logs or exposing spool locations."""
        meta = session.meta
        end = meta.get("ended_at") or time.time()
        sizes, output_error = CommandSessionManager._output_state(session)
        return {
            "command_session_id": meta["session_id"],
            "run_id": meta["owner_run_id"],
            "command": meta["command"],
            "cwd": meta["cwd"],
            "status": meta["status"],
            "exit_code": meta.get("exit_code"),
            "created_at": meta["created_at"],
            "ended_at": meta.get("ended_at"),
            "duration_ms": max(0, meta.get("duration_ms", int((end - meta["created_at"]) * 1000))),
            "stdout_bytes": sizes["stdout"],
            "stderr_bytes": sizes["stderr"],
            "output_error": output_error,
            "timed_out": meta.get("timed_out", False),
            "interruption_reason": meta.get("interruption_reason"),
            "transport": meta.get("transport", "pipes"),
            "terminal_rows": meta.get("terminal_rows"),
            "terminal_columns": meta.get("terminal_columns"),
            "terminal_windows_build": meta.get("terminal_windows_build"),
            "file_tracking": meta.get("file_tracking"),
        }

    def summaries(self, owner_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            sessions = [item for item in self._sessions.values() if item.meta["owner_session_id"] == owner_session_id]
        result = []
        for session in sessions:
            with session.condition:
                result.append(self._summary(session))
        return sorted(result, key=lambda item: (item["created_at"], item["command_session_id"]), reverse=True)

    @staticmethod
    def _log_page(target: Path, *, offset: int | None, before: int | None,
                  max_bytes: int) -> dict[str, Any]:
        """Bounded UTF-8 pages; callers can traverse either way without gaps."""
        if offset is not None and before is not None:
            raise CommandSessionError("command_session_cursor_invalid", "同一输出流请选择起点或前一页位置。")
        with target.open("rb") as stream:
            total = stream.seek(0, 2)
            position = offset if offset is not None else before
            if position is not None:
                if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position <= total:
                    raise CommandSessionError("command_session_cursor_invalid", "命令输出位置超过现有日志或无效。")
                if position < total:
                    stream.seek(position)
                    if stream.read(1)[0] & 0xC0 == 0x80:
                        raise CommandSessionError("command_session_cursor_invalid", "命令输出位置不在完整字符的边界。")
            if offset is not None:
                start = offset
                stream.seek(start)
                data = stream.read(max_bytes)
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                text = decoder.decode(data, final=start + len(data) == total)
                next_offset = start + len(data) - len(decoder.getstate()[0])
            else:
                end = total if before is None else before
                start = max(0, end - max_bytes)
                stream.seek(start)
                data = stream.read(end - start)
                # Tail windows can start inside a code point; move forward to
                # its next boundary. The preceding page ends at that boundary.
                trim = 0
                while trim < len(data) and data[trim] & 0xC0 == 0x80:
                    trim += 1
                start += trim
                text = data[trim:].decode("utf-8")
                next_offset = end
        return {"text": text, "start_offset": start, "next_offset": next_offset,
                "total_bytes": total, "truncated_before": start > 0, "has_more": next_offset < total}

    def read_page(self, owner_session_id: str, command_session_id: str, *,
                  stdout_offset: int | None = None, stderr_offset: int | None = None,
                  stdout_before: int | None = None, stderr_before: int | None = None,
                  max_bytes: int = 65536) -> dict[str, Any]:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 4 <= max_bytes <= 1048576:
            raise CommandSessionError("command_session_page_invalid", "每页输出大小应为 4 至 1048576 字节；其余内容可继续翻页读取。")
        session = self._get(owner_session_id, command_session_id)
        with session.condition:
            payload = self._summary(session)
            try:
                payload["stdout"] = self._log_page(session.folder / "stdout.log", offset=stdout_offset,
                    before=stdout_before, max_bytes=max_bytes)
                payload["stderr"] = self._log_page(session.folder / "stderr.log", offset=stderr_offset,
                    before=stderr_before, max_bytes=max_bytes)
            except (OSError, UnicodeError) as exc:
                raise CommandSessionError("command_output_unavailable", "命令日志缺失、无法读取或编码损坏；命令记录仍保留，可修复日志后重新读取。") from exc
            return payload

    def stop_summary(self, owner_session_id: str, command_session_id: str) -> dict[str, Any]:
        session = self._get(owner_session_id, command_session_id)
        self._stop_session(session)
        with session.condition:
            return self._summary(session)

    def list(self, owner_session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            sessions = [session for session in self._sessions.values() if session.meta["owner_session_id"] == owner_session_id]
        result = []
        for session in sessions:
            with session.condition:
                metadata = dict(session.meta)
                sizes, output_error = self._output_state(session)
                metadata["cursor"] = {name: size for name, size in sizes.items() if size is not None}
                if output_error:
                    metadata.update(output_error=output_error, completion_eligible=False)
                metadata.update({name + "_file": str(session.folder / (name + ".log")) for name in ("stdout", "stderr")})
                metadata["transport"] = session.meta.get("transport", "pipes")
                metadata["stdin_encoding"] = "utf-8" if metadata["transport"] == "pty" else locale.getencoding()
                result.append(metadata)
        return result

    def cancel_run(self, owner_session_id: str, owner_run_id: str) -> None:
        with self._lock:
            self._cancelled_runs.add((owner_session_id, owner_run_id))
        for metadata in self.list(owner_session_id):
            if metadata["owner_run_id"] == owner_run_id and metadata["status"] == "running":
                self.stop_summary(owner_session_id, metadata["session_id"])

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.process is not None:
                self.stop_summary(session.meta["owner_session_id"], session.meta["session_id"])
