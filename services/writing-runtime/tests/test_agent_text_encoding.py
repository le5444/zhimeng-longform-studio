"""File tools preserve the encoding and bytes that the editor can already read."""
from __future__ import annotations

import asyncio

import pytest

from test_agent_direct_write_tools import _call
from test_memory_permissions import _harness
from zhimeng_runtime.execution_protocol import RunStatus


FORMATS = [
    ("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
    ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"),
    ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff"),
]


@pytest.mark.parametrize("encoding,bom", FORMATS)
@pytest.mark.parametrize("with_project", [False, True])
@pytest.mark.parametrize("operation", ["edit", "write", "batch"])
def test_read_edit_save_and_undo_preserve_text_format(tmp_path, encoding, bom, with_project, operation):
    async def scenario():
        service, _, binding, folder = _harness(tmp_path)
        target = (folder if with_project else tmp_path) / "config.txt"
        before_text = "name=\u6d4b\u8bd5\r\nvalue=1\r\nkeep=unchanged\r\n"
        before = bom + before_text.encode(encoding)
        target.write_bytes(before)
        after_text = before_text.replace("value=1", "value=2")
        session = await service.create_session(
            project_id=binding.project_id if with_project else None, title="Text formats",
        )
        run, _, _ = service.store.create_run(
            session_id=session.session_id, message="Update the configuration.",
            expected_project_revision=binding.revision if with_project else None,
            expected_project_fingerprint=binding.fingerprint if with_project else None,
            active_chapter_id=None, writer_mode="author_review_change_proposal",
            idempotency_key="encoding",
        )
        service.store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
        run = service.store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
        path = target.name if with_project else str(target)
        tool, arguments = {
            "edit": ("edit_file", {"path": path, "find": "value=1", "replace": "value=2"}),
            "write": ("write_file", {"path": path, "content": after_text.replace("\r\n", "\n")}),
            "batch": ("write_file", {"files": [{"path": path, "content": after_text.replace("\r\n", "\n")}]}),
        }[operation]
        try:
            changed = await _call(service.tool_executor, run, tool, arguments, call_id="edit-encoded")
            assert changed.output["applied"] is True
            assert target.read_bytes() == bom + after_text.encode(encoding)
            read = await _call(service.tool_executor, run, "read_file", {"path": path}, call_id="read-encoded")
            assert "value=2" in read.output["content"]
            assert "\u6d4b\u8bd5" in read.output["content"]
            assert "\ufeff" not in read.output["content"]
            undo = await _call(service.tool_executor, run, "undo_change", {
                "change_id": changed.output.get("change_id") or changed.output["change_set_id"],
            }, call_id="undo-encoded")
            assert target.read_bytes() == before, undo.output
        finally:
            await service.shutdown()

    asyncio.run(scenario())
