"""Every delivered byte remains reachable without loading an entire command log."""

import hashlib
import json
from pathlib import Path

import pytest

from zhimeng_runtime.command_sessions import CommandSessionManager


@pytest.mark.parametrize("status", ["exited", "cancelled", "interrupted"])
def test_bounded_pages_recover_both_streams_across_restart(tmp_path, monkeypatch, status):
    root = tmp_path / "commands"
    folder = root / "cmd_saved"
    folder.mkdir(parents=True)
    expected = {
        "stdout": ("开始🙂\r\n" + "普通输出 abc🙂\n" * 110_000 + "最后一行\n").encode(),
        "stderr": ("诊断\n" * 13_000 + "done\n").encode(),
    }
    for name, data in expected.items():
        (folder / f"{name}.log").write_bytes(data)
    (folder / "session.json").write_text(json.dumps({
        "schema": "zhimeng.command-session.v1", "session_id": "cmd_saved",
        "owner_session_id": "chat-a", "owner_run_id": "run-a",
        "command": "previous command", "cwd": str(tmp_path),
        "status": status, "exit_code": 0 if status == "exited" else None,
        "created_at": 1.0, "ended_at": 2.0,
    }), encoding="utf-8")
    original_open = Path.open
    read_sizes = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            assert 0 <= size <= 24_000, "output reads must have a bounded window"
            data = self.stream.read(size)
            read_sizes.append(len(data))
            return data

    def bounded_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return BoundedReader(stream) if path.parent == folder and path.suffix == ".log" and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", bounded_open)
    manager = CommandSessionManager(root)
    cursor = {"stdout": 0, "stderr": 0}
    actual = {name: hashlib.sha256() for name in expected}
    try:
        for page_index in range(150):
            page = manager.read("chat-a", "cmd_saved", cursor=cursor)
            assert page["status"] == status
            for name, original in expected.items():
                data = page[name].encode()
                assert len(data) <= 24_000
                assert page["cursor"][name] == cursor[name] + len(data)
                assert page[name + "_bytes"] == len(original)
                assert page[name + "_has_more"] is (page["cursor"][name] < len(original))
                actual[name].update(data)
            cursor = page["cursor"]
            if page_index == 0:
                assert page["stdout_has_more"] and "cursor" in page["note"]
                assert sum(read_sizes) <= 48_002
                manager.close()
                manager = CommandSessionManager(root)
            if not page["stdout_has_more"] and not page["stderr_has_more"]:
                break
        else:
            pytest.fail("output cursor did not reach the end")
        for name, original in expected.items():
            assert actual[name].digest() == hashlib.sha256(original).digest()
            assert cursor[name] == len(original)
        empty = manager.read("chat-a", "cmd_saved", cursor=cursor)
        assert empty["stdout"] == empty["stderr"] == ""
    finally:
        manager.close()
