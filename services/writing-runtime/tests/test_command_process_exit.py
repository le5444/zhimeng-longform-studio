"""A stopped command must finish its owned children, even after its shell exits."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from zhimeng_runtime import command_capture
from zhimeng_runtime.command_sessions import start_command_process

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process handle lifetime")


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "test process did not reach the requested state"
        time.sleep(0.01)


def child_command(tmp_path: Path, *, detached: bool) -> str:
    child = tmp_path / "child.py"
    child.write_text(
        "import time\nfrom pathlib import Path\n"
        "Path('ready').touch()\n"
        "end = time.monotonic() + 40\n"
        "while not Path('release').exists() and time.monotonic() < end:\n"
        " Path('heartbeat').write_text(str(time.monotonic()))\n time.sleep(.02)\n"
        "Path('finished').touch()\n", encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    redirection = ",stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL" if detached else ""
    parent.write_text(
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable,'-u',{str(child)!r}]{redirection})\n", encoding="utf-8",
    )
    return subprocess.list2cmdline([sys.executable, "-u", str(parent)])


def test_synchronous_cancel_waits_for_child_after_shell_exit(tmp_path, monkeypatch):
    command = child_command(tmp_path, detached=False)
    processes = []
    original = command_capture.start_command_process

    def start(*args, **kwargs):
        result = original(*args, **kwargs)
        processes.append(result[0])
        return result

    monkeypatch.setattr(command_capture, "start_command_process", start)
    stop, done = threading.Event(), threading.Event()
    results, errors = [], []

    def run():
        try:
            results.append(command_capture.capture_command(command, tmp_path, timeout_seconds=30,
                stop_command=stop, folder=tmp_path / "logs", limit=24000, prune=lambda _: None))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        wait_until(lambda: processes and processes[0].poll() is not None and (tmp_path / "heartbeat").exists())
        assert not done.is_set(), "the inherited child pipe must still be open"
        stop.set()
        assert done.wait(5), "cancellation left a child running after the shell had exited"
        assert not errors
        assert results[0]["cancelled"] is True
        before = (tmp_path / "heartbeat").read_bytes()
        time.sleep(.1)
        assert (tmp_path / "heartbeat").read_bytes() == before
    finally:
        # The failing baseline still owns this child; let it exit cooperatively.
        stop.set()
        (tmp_path / "release").touch()
        worker.join(timeout=15)
        assert not worker.is_alive()


def test_successful_shell_can_leave_an_intentional_background_process(tmp_path):
    try:
        result = command_capture.capture_command(child_command(tmp_path, detached=True), tmp_path,
            timeout_seconds=20, stop_command=None, folder=tmp_path / "logs", limit=24000, prune=lambda _: None)
        assert result["exit_code"] == 0 and not result["cancelled"]
        wait_until(lambda: (tmp_path / "heartbeat").exists())
        before = (tmp_path / "heartbeat").read_bytes()
        wait_until(lambda: (tmp_path / "heartbeat").read_bytes() != before)
    finally:
        (tmp_path / "release").touch()
        if (tmp_path / "ready").exists():
            wait_until(lambda: (tmp_path / "finished").exists())


@pytest.mark.parametrize("times_out", [False, True])
def test_job_waits_for_real_handles_even_if_active_count_is_already_zero(tmp_path, monkeypatch, times_out):
    process, job = start_command_process(
        subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(30)"]), cwd=tmp_path)
    assert job is not None
    requested, done = threading.Event(), threading.Event()
    native_terminate = job.api.TerminateJobObject
    errors = []

    def delayed_request(*args):
        requested.set()
        return 1  # Termination is asynchronous; the handles have not signalled yet.

    def wait():
        try:
            job.terminate_and_wait(timeout=.2 if times_out else 10)
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(job.api, "TerminateJobObject", delayed_request)
    monkeypatch.setattr(job, "active", lambda: False)
    worker = threading.Thread(target=wait)
    worker.start()
    try:
        assert requested.wait(5)
        if times_out:
            assert done.wait(5)
            assert len(errors) == 1 and isinstance(errors[0], TimeoutError)
        else:
            assert not done.wait(.1), "a zero job count cannot replace waiting for process exit"
            assert native_terminate(job.handle, 1)
            assert done.wait(5)
            assert not errors
        assert process.poll() is not None or times_out
    finally:
        native_terminate(job.handle, 1)
        worker.join(timeout=10)
        job.close()
        process.wait(timeout=10)
        process.stdout.close()
        process.stderr.close()
