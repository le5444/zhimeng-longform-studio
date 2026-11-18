"""Real local process coverage for runtime-owned persistent command sessions."""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from zhimeng_runtime.command_sessions import (
    CommandSessionError, CommandSessionManager, _OutputDecoder, _StreamMask,
)
from zhimeng_runtime.secret_mask import mask_secrets


@pytest.fixture
def manager(tmp_path):
    result = CommandSessionManager(tmp_path / "commands")
    yield result
    result.close()


def command(tmp_path: Path, source: str, name: str = "worker.py") -> str:
    script = tmp_path / name
    script.write_bytes(source.encode("utf-8"))
    args = [sys.executable, "-u", str(script)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def start(manager, tmp_path, source, **kwargs):
    return manager.start(owner_session_id=kwargs.pop("owner_session_id", "chat-a"),
        owner_run_id=kwargs.pop("owner_run_id", "run-a"),
        command=command(tmp_path, source), cwd=tmp_path, **kwargs)


def until(manager, initial, predicate, *, owner="chat-a", seconds=8):
    payload = dict(initial)
    stdout, stderr = payload["stdout"], payload["stderr"]
    deadline = time.monotonic() + seconds
    while not predicate(dict(payload, stdout=stdout, stderr=stderr)):
        assert time.monotonic() < deadline, payload
        payload = manager.read(owner, payload["session_id"], cursor=payload["cursor"], wait_ms=100)
        stdout += payload["stdout"]
        stderr += payload["stderr"]
    return dict(payload, stdout=stdout, stderr=stderr)


def test_prompt_input_incremental_output_exit_code_and_later_round(manager, tmp_path):
    initial = start(manager, tmp_path, "import sys\nsys.stdout.write('Name> '); sys.stdout.flush()\nname = input()\nprint('Hello ' + name)\nprint('diagnostic', file=sys.stderr)\nsys.exit(23)\n")
    ready = until(manager, initial, lambda p: "Name> " in p["stdout"])
    assert ready["status"] == "running"
    empty = manager.read("chat-a", ready["session_id"], cursor=ready["cursor"])
    assert empty["stdout"] == empty["stderr"] == ""
    # A later model round in the same chat uses only the saved ID and cursor.
    written = manager.write("chat-a", ready["session_id"], text="Ada\n", cursor=ready["cursor"], wait_ms=1000)
    done = until(manager, written, lambda p: p["status"] != "running")
    assert done["status"] == "exited"
    assert done["exit_code"] == 23
    assert done["stdout"] == "Hello Ada" + os.linesep
    assert done["stderr"] == "diagnostic" + os.linesep
    assert done["transport"] == "pipes"
    assert done["duration_ms"] >= 0
    assert Path(done["stdout_file"]).read_bytes().decode("utf-8") == "Name> Hello Ada" + os.linesep
    assert manager.list("chat-a")[0]["status"] == "exited"
    assert "stdout" not in manager.list("chat-a")[0]
    assert manager.list("chat-a")[0]["stdout_file"] == done["stdout_file"]
    assert manager.list("chat-a")[0]["stdin_encoding"] == done["stdin_encoding"]


def test_session_ownership_is_enforced_on_every_operation(manager, tmp_path):
    payload = start(manager, tmp_path, "print('ready', flush=True)\ninput()\n")
    session_id = payload["session_id"]
    for call in (
        lambda: manager.read("chat-b", session_id),
        lambda: manager.write("chat-b", session_id, text="wrong\n"),
        lambda: manager.stop("chat-b", session_id),
    ):
        with pytest.raises(CommandSessionError, match="找不到"):
            call()
    assert manager.list("chat-b") == []
    assert manager.read("chat-a", session_id)["status"] == "running"


def test_eof_unblocks_input_and_closed_input_is_clear(manager, tmp_path):
    payload = start(manager, tmp_path, "import sys\nprint('ready', flush=True)\nprint('received:' + sys.stdin.read())\n")
    payload = until(manager, payload, lambda p: "ready" in p["stdout"])
    result = manager.write("chat-a", payload["session_id"], text="two lines\nnext", close_stdin=True, cursor=payload["cursor"], wait_ms=1000)
    done = until(manager, result, lambda p: p["status"] != "running")
    assert done["exit_code"] == 0
    assert "received:two lines" + os.linesep + "next" in done["stdout"]
    with pytest.raises(CommandSessionError):
        manager.write("chat-a", done["session_id"], text="late")


def test_no_implicit_execution_timeout_and_explicit_timeout_is_honored(manager, tmp_path):
    payload = start(manager, tmp_path, "import time\nprint('ready', flush=True)\ntime.sleep(60)\n", yield_time_ms=0)
    assert payload["timeout_seconds"] is None
    first = manager.stop("chat-a", payload["session_id"])
    assert first["status"] == "cancelled" and not first["timed_out"]
    other = start(manager, tmp_path, "import time\ntime.sleep(60)\n", owner_run_id="run-timeout", timeout_seconds=0.15)
    done = until(manager, other, lambda p: p["status"] != "running")
    assert done["status"] == "cancelled" and done["timed_out"]
    assert done["exit_code"] is not None


def test_cancel_scope_and_cancel_before_start(manager, tmp_path):
    first = start(manager, tmp_path, "input()\n", owner_run_id="first", yield_time_ms=0)
    second = start(manager, tmp_path, "input()\n", owner_run_id="second", yield_time_ms=0)
    manager.cancel_run("chat-a", "second")
    assert manager.read("chat-a", second["session_id"])["status"] == "cancelled"
    assert manager.read("chat-a", first["session_id"])["status"] == "running"
    manager.cancel_run("chat-a", "not-started")
    with pytest.raises(CommandSessionError) as error:
        start(manager, tmp_path, "raise RuntimeError('must not run')", owner_run_id="not-started", yield_time_ms=0)
    assert error.value.code == "command_run_cancelled"
    assert len(manager.list("chat-a")) == 2


def test_cancel_racing_registration_cannot_leave_a_child(manager, tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    spawning, release = threading.Event(), threading.Event()
    def blocked_popen(*args, **kwargs):
        spawning.set()
        assert release.wait(5)
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", blocked_popen)
    results = []
    worker = threading.Thread(target=lambda: results.append(start(manager, tmp_path, "input()", yield_time_ms=0)))
    worker.start()
    assert spawning.wait(5)
    cancelled = threading.Thread(target=manager.cancel_run, args=("chat-a", "run-a"))
    cancelled.start()
    release.set()
    worker.join(8)
    cancelled.join(8)
    assert not worker.is_alive() and not cancelled.is_alive()
    assert manager.read("chat-a", results[0]["session_id"])["status"] == "cancelled"


def test_runtime_restart_only_marks_stale_work_and_retains_incremental_logs(tmp_path, monkeypatch):
    root = tmp_path / "commands"
    folder = root / "cmd_existing"
    folder.mkdir(parents=True)
    (folder / "stdout.log").write_bytes("旧输出\n".encode("utf-8"))
    (folder / "stderr.log").write_bytes(b"")
    (folder / "session.json").write_bytes(json.dumps(dict(schema="zhimeng.command-session.v1", session_id="cmd_existing", owner_session_id="chat-a", owner_run_id="old-run", status="running", exit_code=None, command="old", cwd=str(tmp_path), created_at=time.time()-10, ended_at=None, pid=os.getpid())).encode())
    def unexpected(*args, **kwargs):
        raise AssertionError("A restored PID must never be adopted or killed")
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    manager = CommandSessionManager(root)
    try:
        payload = manager.read("chat-a", "cmd_existing")
        assert payload["status"] == "interrupted" and payload["exit_code"] is None
        assert payload["stdout"] == "旧输出\n"
        assert "未重新接管" in payload["interruption_reason"]
        assert manager.read("chat-a", "cmd_existing", cursor=payload["cursor"])["stdout"] == ""
        assert manager.stop("chat-a", "cmd_existing")["status"] == "interrupted"
    finally:
        manager.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object process-tree ownership")
def test_stop_kills_parent_and_descendant_by_owned_job(manager, tmp_path):
    source = "import subprocess, sys\nchild = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])\nprint(child.pid, flush=True)\ninput()\n"
    initial = start(manager, tmp_path, source)
    ready = until(manager, initial, lambda p: p["stdout"].strip().isdigit())
    child_pid = int(ready["stdout"].strip())
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.OpenProcess(0x100000, False, child_pid)
    assert handle
    try:
        assert api.WaitForSingleObject(handle, 0) == 258
        result = manager.stop("chat-a", ready["session_id"], cursor=ready["cursor"])
        assert result["status"] == "cancelled"
        assert api.WaitForSingleObject(handle, 3000) == 0
    finally:
        api.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows job retains descendants after shell exit")
def test_child_stays_owned_after_shell_has_exited(manager, tmp_path):
    source = "import subprocess, sys\nchild = subprocess.Popen([sys.executable, '-u', '-c', 'import time; time.sleep(60)'])\nprint('child-started', flush=True)\n"
    payload = start(manager, tmp_path, source)
    ready = until(manager, payload, lambda p: "child-started" in p["stdout"])
    session = manager._get("chat-a", ready["session_id"])
    deadline = time.monotonic() + 5
    while session.process.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert manager.read("chat-a", ready["session_id"])["status"] == "running"
    result = manager.stop("chat-a", ready["session_id"], cursor=ready["cursor"])
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 0  # shell code remains truthful; child was stopped


def test_close_terminates_owned_processes_and_rejects_new_work(manager, tmp_path):
    payload = start(manager, tmp_path, "input()\n", yield_time_ms=0)
    manager.close()
    assert manager.read("chat-a", payload["session_id"])["status"] == "cancelled"
    with pytest.raises(CommandSessionError) as error:
        start(manager, tmp_path, "print('new')")
    assert error.value.code == "command_sessions_closed"


def test_stream_mask_keeps_split_credentials_out_of_every_visible_prefix():
    formatted = "sk-" + "abcdefghijklmnopqrstuvwx012345"
    secrets = (
        (formatted, "prefix/" + formatted + "\n"),
        ("abcdefSecret987654321", "\"ANTHROPIC_API_KEY\": \"abcdefSecret987654321\"\n"),
        ("abcdefSecret987654321", "api-key = abcdefSecret987654321\n"),
        ("abcdefSecret987654321", "Bearer abcdefSecret987654321\n"),
        ("abcdefSecret987654321", "service.api_key=abcdefSecret987654321\n"),
    )
    for secret, text in secrets:
        for split in range(len(text) + 1):
            masker = _StreamMask()
            visible = masker.feed(text[:split])
            assert secret not in visible
            visible += masker.feed(text[split:], final=True)
            assert secret not in visible
            assert "末 4 位 " + secret[-4:] in visible
            assert masker.masked_count >= 1
    masker = _StreamMask()
    assert masker.feed("请输入名称> ") == "请输入名称> "
    assert masker.feed("ready") == "ready"
    assert masker.feed("正常输出。", final=True) == "正常输出。"


def test_all_existing_known_secret_families_match_at_every_stream_boundary():
    values = [
        *("sk-" + variant + "Synthetic_Value-0123456789" for variant in ("", "ant-", "proj-")),
        *(f"gh{kind}_" + "Synthetic01234567890123456789" for kind in "pousr"),
        "github_pat_" + "Synthetic_01234567890123456789",
        "AKIA" + "ABCDEF0123456789",
        *(f"xox{kind}-" + "Synthetic-0123456789" for kind in "abposr"),
        "AIza" + "Synthetic_01234567890123456789012345",
    ]
    for secret in values:
        for leading in ("", "prefix/", "prefix.", "prefix-", "\"", "("):
            text = leading + secret + "\n"
            expected, count = mask_secrets(text)
            assert count == 1
            for split in range(len(text) + 1):
                masker = _StreamMask()
                visible = masker.feed(text[:split])
                assert secret[:-4] not in visible
                visible += masker.feed(text[split:], final=True)
                assert visible == expected
                assert masker.masked_count == count
        # Exercise every boundary at once, as a one-byte-at-a-time producer.
        masker = _StreamMask()
        visible = ""
        for char in secret:
            visible += masker.feed(char)
            assert secret[:-4] not in visible
        visible += masker.feed("", final=True)
        assert visible == mask_secrets(secret)[0]


def test_bearer_and_assignment_context_match_existing_mask_at_every_boundary():
    value = "SyntheticSecret_0123456789"
    cases = [
        f"{key}{separator}{value}{quote}\n"
        for key in ("ANTHROPIC_API_KEY", "api-key", "service.api_key", "sessionToken", "credentials", "private_key")
        for separator, quote in (("=", ""), (" = ", ""), (": \"", "\""))
    ]
    cases += [
        "Authorization: Bearer " + value + "\n",
        "Authorization: bEaReR 12345678901234567890\n",
        "Bearer abcDEF._~+/0123456789==\n",
        "api_key=12345678901234567890\n",  # numeric assignment is not a key
        "max_tokens=12345678901234567890\n",
    ]
    for text in cases:
        expected, count = mask_secrets(text)
        for split in range(len(text) + 1):
            masker = _StreamMask()
            first = masker.feed(text[:split])
            visible = first + masker.feed(text[split:], final=True)
            assert visible == expected
            assert masker.masked_count == count


def test_utf8_and_legacy_chinese_survive_byte_boundaries():
    for encoding in ("utf-8", "gbk"):
        decoder = _OutputDecoder()
        text = "开始：你好，世界！\n"
        output = "".join(decoder.decode(bytes([byte])) for byte in text.encode(encoding))
        output += decoder.decode(b"", final=True)
        assert output == text


def test_logs_are_masked_and_unicode_cursors_are_stable(manager, tmp_path):
    secret = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"
    source = "import os, sys, time\ndata = ('中文提示> token=' + 'sk-abcdefghijklmnop' + 'qrstuvwxyz012345' + '\\n完成\\n').encode('utf-8')\nfor byte in data:\n os.write(sys.stdout.fileno(), bytes([byte])); time.sleep(0.001)\n"
    payload = start(manager, tmp_path, source, yield_time_ms=0)
    done = until(manager, payload, lambda p: p["status"] != "running")
    assert secret not in done["stdout"]
    assert "中文提示> token=" in done["stdout"] and "完成" in done["stdout"]
    raw = Path(done["stdout_file"]).read_bytes()
    assert secret.encode() not in raw
    assert raw.decode() == done["stdout"]
    assert manager.read("chat-a", done["session_id"], cursor=done["cursor"])["stdout"] == ""
    with pytest.raises(CommandSessionError) as error:
        manager.read("chat-a", done["session_id"], cursor={"stdout": 1})
    assert error.value.code == "command_session_cursor_invalid"


def test_long_output_has_no_execution_or_log_truncation(manager, tmp_path):
    payload = start(manager, tmp_path, "import sys\nsys.stdout.write('x' * 250000 + '\\nLAST\\n')\n", yield_time_ms=0)
    done = until(manager, payload, lambda p: p["status"] != "running" and not p["stdout_has_more"] and not p["stderr_has_more"])
    expected = "x" * 250000 + os.linesep + "LAST" + os.linesep
    assert done["stdout"] == expected
    assert Path(done["stdout_file"]).stat().st_size == len(expected)


def test_explicit_environment_and_workdir_are_preserved(manager, tmp_path):
    payload = start(manager, tmp_path, "import os\nprint(os.getcwd())\nprint(os.environ['ZHIMENG_COMMAND_TEST'])\n", env={**os.environ, "ZHIMENG_COMMAND_TEST": "ordinary-value"})
    done = until(manager, payload, lambda p: p["status"] != "running")
    assert str(tmp_path) in done["stdout"]
    assert "ordinary-value" in done["stdout"]


def test_chinese_stdin_uses_native_encoding_and_supports_explicit_utf8(manager, tmp_path):
    source = "import sys\nsys.stdin.reconfigure(errors='strict')\nprint('ready', flush=True)\nprint(input())\n"
    for index, encoding in enumerate((None, "utf-8")):
        env = {**os.environ, "PYTHONIOENCODING": encoding or locale.getencoding()}
        payload = start(manager, tmp_path, source, owner_run_id=f"encoding-{index}", env=env)
        ready = until(manager, payload, lambda p: "ready" in p["stdout"])
        output = manager.write("chat-a", payload["session_id"], text="中文输入\n", encoding=encoding, cursor=ready["cursor"], wait_ms=1000)
        done = until(manager, output, lambda p: p["status"] != "running")
        assert done["exit_code"] == 0, done["stderr"]
        assert done["stdout"].strip() == "中文输入"


def test_invalid_input_encoding_does_not_write_partial_data(manager, tmp_path):
    payload = start(manager, tmp_path, "print('ready', flush=True)\nprint(input())\n")
    ready = until(manager, payload, lambda p: "ready" in p["stdout"])
    with pytest.raises(CommandSessionError) as error:
        manager.write("chat-a", payload["session_id"], text="中文", encoding="ascii")
    assert error.value.code == "command_session_encoding_invalid"
    result = manager.write("chat-a", payload["session_id"], text="good\n", cursor=ready["cursor"], wait_ms=1000)
    done = until(manager, result, lambda p: p["status"] != "running")
    assert done["stdout"].strip() == "good"


def test_large_stdin_is_fully_written(manager, tmp_path):
    import hashlib
    source = "import sys, hashlib\nprint('ready', flush=True)\nprint(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())\n"
    payload = start(manager, tmp_path, source)
    ready = until(manager, payload, lambda p: "ready" in p["stdout"])
    data = "abcdefghij" * 100000
    result = manager.write("chat-a", ready["session_id"], text=data, close_stdin=True, cursor=ready["cursor"], wait_ms=1000)
    done = until(manager, result, lambda p: p["status"] != "running")
    assert done["stdout"].strip() == hashlib.sha256(data.encode()).hexdigest()
