# -*- coding: utf-8 -*-
"""纠偏指令 P0（2026-09-26）：基础 agent 看得见整台电脑。

- 3.1 / 3.5：电脑读工具的相对路径和命令从打开的文件夹起，没打开文件夹时从用户主目录起（以前落在
  .runtime/author-projects 那堆测试残留里）；绝对路径哪里都能读，.git 这些也能读，.env 不开。
- 3.2：每个对话开头一段环境事实（系统、用户主目录、当前文件夹、今天、挡位），一场对话里不变；
  换了挡位（或跨天）在末尾追加一条，前面的不改。
- 3.3：不打开文件夹也能用写文件工具（给绝对路径），打开的文件夹以外的写入也留记录、能撤回；只读挡照旧不能写。
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from test_agent_direct_write_tools import _build_runtime, _call
from test_context_agent_e2e import AnswerOnlyProvider
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_service import _SESSION_TITLE_PROMPT
from zhimeng_runtime.agent_tools import AgentToolError, ComputerListFilesArguments
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


# ----------------------------------------------------------------------
# 打开的文件夹以外的写入：记在 file_journal 里，能撤回


def test_absolute_path_outside_the_open_folder_is_written_recorded_and_undone(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _change_sets, _project = _build_runtime(tmp_path)
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    target = desktop / "测试.txt"

    written = asyncio.run(
        _call(executor, run, "write_file", {"path": str(target), "content": "一句话。\n"})
    )
    assert written.output["schema"] == "zhimeng.tool-result.file-change.v1"
    assert written.output["operation"] == "created"
    assert written.output["change_id"].startswith("filechange-")
    assert target.read_text(encoding="utf-8") == "一句话。\n"
    assert written.change_set is None  # 不出修改卡：修改卡是打开的文件夹里的修改记录

    undone = asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert undone.output["change_id"] == written.output["change_id"]
    assert "这个对话里的写入都撤完了" in undone.output["message"]
    assert not target.exists()
    with pytest.raises(AgentToolError) as nothing:
        asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0003"))
    assert nothing.value.code == "agent_tool_undo_nothing"


def test_absolute_path_inside_the_open_folder_still_uses_the_change_record(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    target = project / "素材" / "灵感.md"
    written = asyncio.run(
        _call(executor, run, "write_file", {"path": str(target), "content": "灵感一则。\n"})
    )
    assert written.output["schema"] == "zhimeng.tool-result.project-write-file.v1"
    assert written.output["change_set_id"].startswith("changeset-")
    assert written.output["relative_path"] == "素材/灵感.md"
    assert target.read_text(encoding="utf-8") == "灵感一则。\n"


def test_old_relative_path_argument_is_still_accepted(tmp_path: Path) -> None:
    """旧参数名 relative_path、旧工具名 project_write_file（旧对话续跑时会这样调）都照收。"""
    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    written = asyncio.run(
        _call(executor, run, "project_write_file", {"relative_path": "素材\\旧写法.md", "content": "照收。\n"})
    )
    assert written.output["relative_path"] == "素材/旧写法.md"
    assert (project / "素材" / "旧写法.md").read_text(encoding="utf-8") == "照收。\n"


def test_edit_outside_keeps_windows_line_endings_and_undo_restores_the_exact_bytes(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    target = tmp_path / "notes" / "plan.txt"
    target.parent.mkdir()
    original = "第一行\r\n第二行\r\n".encode("utf-8")
    target.write_bytes(original)

    edited = asyncio.run(
        _call(
            executor,
            run,
            "edit_file",
            {"path": str(target), "find": "第一行\n第二行", "replace": "第一行\n改过的第二行"},
        )
    )
    assert edited.output["operation"] == "updated"
    assert target.read_bytes() == "第一行\r\n改过的第二行\r\n".encode("utf-8")

    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert target.read_bytes() == original


def test_undo_leaves_a_file_alone_when_it_changed_after_the_write(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    target = tmp_path / "out" / "a.txt"
    asyncio.run(_call(executor, run, "write_file", {"path": str(target), "content": "写的。"}))
    target.write_text("后来有人改了。", encoding="utf-8")
    with pytest.raises(AgentToolError) as conflict:
        asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert conflict.value.code == "agent_tool_undo_conflict"
    assert target.read_text(encoding="utf-8") == "后来有人改了。"


def test_delete_move_and_mkdir_outside_are_all_undoable(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    folder = tmp_path / "desk"
    folder.mkdir()
    doomed = folder / "old.txt"
    doomed.write_bytes(b"keep me")

    asyncio.run(_call(executor, run, "delete_file", {"path": str(doomed)}))
    assert not doomed.exists()
    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert doomed.read_bytes() == b"keep me"

    moved_to = folder / "sub" / "renamed.txt"
    moved = asyncio.run(
        _call(executor, run, "move_file", {"path": str(doomed), "target_path": str(moved_to)}, call_id="call-0003")
    )
    assert moved.output["operation"] == "moved"
    assert moved_to.read_bytes() == b"keep me" and not doomed.exists()
    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0004"))
    assert doomed.read_bytes() == b"keep me"
    assert not (folder / "sub").exists()  # 挪过去时新建的文件夹，挪回来时空了就收掉

    nested = folder / "a" / "b"
    asyncio.run(_call(executor, run, "make_folder", {"path": str(nested)}, call_id="call-0005"))
    assert nested.is_dir()
    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0006"))
    assert not (folder / "a").exists()


def test_several_files_outside_in_one_call_are_one_undo(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    first, second = tmp_path / "x" / "1.txt", tmp_path / "x" / "2.txt"
    written = asyncio.run(
        _call(
            executor,
            run,
            "write_file",
            {"files": [{"path": str(first), "content": "一"}, {"path": str(second), "content": "二"}]},
        )
    )
    assert written.output["operation"] == "written"
    assert first.exists() and second.exists()
    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert not first.exists() and not second.exists()


def test_undo_takes_the_latest_write_across_the_open_folder_and_elsewhere(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    inside = project / "素材" / "里面.md"
    outside = tmp_path / "外面.txt"
    asyncio.run(_call(executor, run, "write_file", {"path": "素材/里面.md", "content": "里"}))
    asyncio.run(_call(executor, run, "write_file", {"path": str(outside), "content": "外"}, call_id="call-0002"))

    first = asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0003"))
    assert not outside.exists() and inside.exists()
    assert "再撤一次会撤回" in first.output["message"]
    asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0004"))
    assert not inside.exists()


def test_a_write_outside_does_not_lose_the_open_folder_pin(tmp_path: Path) -> None:
    """书里写一份 → 桌面写一份（结果里没有版本号）→ 再给书提案：提案基准仍是书里那次写完的版本。
    以前只看最后一次写入的结果，最后一次是写在外面时钉子丢了，提案会被当成「书被别人改过」拒掉。"""
    from zhimeng_runtime.agent_store import AgentStore

    executor, run, _chapter, database, _cs, _project = _build_runtime(tmp_path, suffix="-pin-outside")
    store = AgentStore(database)

    def finish(call_id: str, output: dict) -> None:
        store.finish_tool_call(run.run_id, tool_call_id=call_id, status="succeeded", output=output, error=None, duration_ms=1)

    inside = asyncio.run(_call(executor, run, "write_file", {"path": "素材/一.md", "content": "一\n"}, call_id="call-inside-01"))
    finish("call-inside-01", inside.output)
    outside = asyncio.run(_call(executor, run, "write_file", {"path": str(tmp_path / "外面.txt"), "content": "外"}, call_id="call-outside-01"))
    finish("call-outside-01", outside.output)
    assert "result_revision" not in outside.output

    listed = asyncio.run(_call(executor, run, "project_list_files", {"query": "CH001"}, "call-list-0001"))
    chapter_id = next(item["file_id"] for item in listed.output["files"] if item["relative_path"].endswith("CH001_正文.md"))
    proposed = asyncio.run(
        _call(
            executor,
            run,
            "change_set_propose",
            {
                "title": "改一行",
                "summary": "改第一行",
                "changes": [{"operation": "update", "file_id": chapter_id, "after_content": "改过的第一行。\n第二行。\n", "rationale": "测试"}],
            },
            call_id="call-propose-01",
        )
    )
    assert proposed.output["base_revision"] == inside.output["result_revision"]


def test_relative_write_does_not_fall_back_to_home_when_the_open_folder_is_gone(tmp_path: Path, monkeypatch) -> None:
    executor, run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(environment_context, "home_folder", lambda: home)
    project.rename(tmp_path / "moved-away")
    with pytest.raises(AgentToolError) as missing:
        asyncio.run(_call(executor, run, "write_file", {"path": "素材/灵感.md", "content": "x"}))
    assert missing.value.code == "agent_tool_project_not_found"
    assert not any(home.rglob("*"))


def test_read_only_tier_cannot_write_outside_either(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path, writer_mode="repair_only", suffix="-ro")
    target = tmp_path / "desktop" / "测试.txt"
    with pytest.raises(AgentToolError) as refused:
        asyncio.run(_call(executor, run, "write_file", {"path": str(target), "content": "x"}))
    assert refused.value.code == "writer_change_set_forbidden"
    assert not target.exists()


def test_undo_with_nothing_recorded_says_commands_are_not_undoable(tmp_path: Path) -> None:
    """命令写的文件不进记录：撤回工具照实说，模型别再答成「没有写过」（9-26 grok-4.6 真机）。"""
    executor, run, *_ = _build_runtime(tmp_path)
    marker = tmp_path / "made-by-command.txt"
    asyncio.run(_call(executor, run, "computer_run_command", {"command": f'echo hi> "{marker}"'}))
    assert marker.exists()
    with pytest.raises(AgentToolError) as nothing:
        asyncio.run(_call(executor, run, "undo_change", {}, call_id="call-0002"))
    assert nothing.value.code == "agent_tool_undo_nothing"
    assert "执行过命令" in str(nothing.value)
    assert "echo hi>" in str(nothing.value)  # 最近跑的命令原样带上（每条最多 120 字）
    assert marker.exists()


# ----------------------------------------------------------------------
# 像密钥的值：读出来交给模型的那一份只留末 4 位，文件本身不动


def test_secret_values_are_masked_in_reads_and_command_output(tmp_path: Path) -> None:
    from zhimeng_runtime.secret_mask import mask_secrets

    settings = tmp_path / "settings.json"
    original = (
        '{\n  "env": {\n    "ANTHROPIC_AUTH_TOKEN": "abcdefghijklmnop1234",\n'
        '    "ANTHROPIC_BASE_URL": "https://relay.example/v1"\n  },\n'
        '  "max_tokens": 4096,\n  "tokenizer": "cl100k_base",\n  "author": "someone-long-name"\n}\n'
    )
    settings.write_text(original, encoding="utf-8")
    executor, run, *_ = _build_runtime(tmp_path)

    read = asyncio.run(_call(executor, run, "computer_read_file", {"path": str(settings)}))
    content = read.output["content"]
    assert "abcdefghijklmnop1234" not in content
    assert "末 4 位 1234" in content
    assert read.output["masked_secrets"] == 1
    # 不是密钥的照原样：网址、数字、tokenizer、author。
    for kept in ("https://relay.example/v1", '"max_tokens": 4096', "cl100k_base", "someone-long-name"):
        assert kept in content
    assert settings.read_text(encoding="utf-8") == original  # 文件本身一个字节没动

    typed = asyncio.run(_call(executor, run, "computer_run_command", {"command": f'type "{settings}"'}, call_id="call-0002"))
    assert "abcdefghijklmnop1234" not in typed.output["stdout"]
    assert typed.output["masked_secrets"] == 1

    for text, secret in (
        ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx", "sk-proj-abcdefghijklmnopqrstuvwx"),
        ("Authorization: Bearer abcdef0123456789abcdef", "abcdef0123456789abcdef"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("随手贴的 ghp_abcdefghijklmnopqrstuvwxyz0123 在这", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ('apiKey: "zyxwvutsrqponmlk"', "zyxwvutsrqponmlk"),
    ):
        masked, count = mask_secrets(text)
        assert secret not in masked and count >= 1, text
    assert mask_secrets("柳青烟抬头看了一眼天。")[1] == 0
    assert mask_secrets('"total_tokens": 123456789')[1] == 0
    # 几十万字的单行文件也得马上遮完（第一版正则在这里是平方级，读长文件直接卡死）。
    started = time.monotonic()
    for line in ("x" * 200_000, "柳" * 200_000, "tokentoken" * 20_000, "abc_def-" * 25_000):
        mask_secrets(line)
    assert time.monotonic() - started < 3.0


# ----------------------------------------------------------------------
# 电脑读工具：从打开的文件夹 / 用户主目录起，哪里都能读


def test_computer_tools_start_at_home_without_a_folder_and_read_git_but_not_env(tmp_path: Path, monkeypatch) -> None:
    executor, _run, _chapter, _database, _change_sets, project = _build_runtime(tmp_path)
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    (home / "Desktop" / "todo.txt").write_text("买菜", encoding="utf-8")
    monkeypatch.setattr(environment_context, "home_folder", lambda: home)

    assert executor._resolve_computer_path("Desktop/todo.txt") == (home / "Desktop" / "todo.txt").resolve()
    listed = executor._list_computer_files(ComputerListFilesArguments(path=""), None)
    assert Path(listed["path"]) == home.resolve()
    assert [entry["name"] for entry in listed["entries"]] == ["Desktop"]
    # 打开了文件夹：相对路径从那个文件夹起。
    assert executor._resolve_computer_path("05_正文内容", book_root=project) == (project / "05_正文内容").resolve()

    git_config = project / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text("[core]\n", encoding="utf-8")
    assert executor._resolve_computer_path(str(git_config)) == git_config.resolve()
    with pytest.raises(AgentToolError) as env_refused:
        executor._resolve_computer_path(str(project / ".env"))
    assert env_refused.value.code == "agent_tool_computer_outside_roots"


def test_recursive_listing_finds_a_memory_folder_a_few_levels_down(tmp_path: Path, monkeypatch) -> None:
    """像 ~/.claude/projects/<项目>/memory/ 这种藏在几层下面的：recursive 一次就找到；工具目录不往里钻。"""
    executor, *_ = _build_runtime(tmp_path)
    home = tmp_path / "home"
    memory = home / ".claude" / "projects" / "C--Users-me" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- 偏好\n", encoding="utf-8")
    (home / ".claude" / "node_modules" / "memory").mkdir(parents=True)
    monkeypatch.setattr(environment_context, "home_folder", lambda: home)

    shallow = executor._list_computer_files(ComputerListFilesArguments(path=".claude", query="memory"), None)
    assert shallow["entries"] == []
    found = executor._list_computer_files(ComputerListFilesArguments(path=".claude", query="memory", recursive=True), None)
    # 文件夹本身和里面的 MEMORY.md 名字都对得上；node_modules 里那个不算（工具目录不往里钻）。
    assert [Path(entry["path"]) for entry in found["entries"]] == [memory, memory / "MEMORY.md"]
    assert found["recursive"] is True and found["truncated"] is False
    # 模型照 glob 写的 *.md 也认（9-26 真机 grok-4.6 这样找，以前当字面「*.md」一条都对不上）。
    globbed = executor._list_computer_files(ComputerListFilesArguments(path=".claude", query="*.md", recursive=True), None)
    assert [Path(entry["path"]) for entry in globbed["entries"]] == [memory / "MEMORY.md"]


def test_computer_tool_descriptions_say_what_the_code_does(tmp_path: Path) -> None:
    executor, *_ = _build_runtime(tmp_path)
    descriptions = {item.name: item.description for item in executor.definitions}
    for name in ("list_files", "read_file", "computer_run_command"):
        text = descriptions[name]
        assert "home folder" in text
        assert "trusted" not in text and "novel" not in text and "book" not in text and "author" not in text
    for name in ("write_file", "edit_file", "delete_file", "move_file", "make_folder", "undo_change"):
        text = descriptions[name].lower()
        assert "novel" not in text and "book" not in text and "author" not in text and "chapter" not in text


# ----------------------------------------------------------------------
# 随便聊（没打开文件夹）：写文件工具、环境事实


class ScriptedProvider(AnswerOnlyProvider):
    """按顺序执行脚本：每一轮先调脚本里的工具（没有就直接回答），工具结果回来后回一句话。"""

    def __init__(self, script: list[tuple[str, dict[str, Any]] | None]) -> None:
        super().__init__()
        self.script = list(script)
        self.catalogs: list[list[str]] = []
        self.requests: list[list[dict[str, Any]]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if any(m.get("role") == "system" and m.get("content") == _SESSION_TITLE_PROMPT for m in messages):
            yield ProviderTextDelta("随便聊")
            yield ProviderCompleted("stop", None)
            return
        self.catalogs.append(sorted(tool.name for tool in tools))
        self.requests.append([dict(m) for m in messages])
        if messages and messages[-1].get("role") == "tool" or not self.script:
            yield ProviderTextDelta("好了。")
            yield ProviderCompleted("stop", None)
            return
        step = self.script.pop(0)
        if step is None:
            yield ProviderTextDelta("好的。")
            yield ProviderCompleted("stop", None)
            return
        name, arguments = step
        yield ProviderToolCall(index=0, tool_call_id=f"call_{len(self.requests):04d}", tool_name=name, arguments=arguments)
        yield ProviderCompleted("tool_calls", None)


def _app(tmp_path: Path, provider: object):
    library = tmp_path / "library"
    library.mkdir()
    return create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
            sse_heartbeat_seconds=0.05,
        ),
        agent_provider=provider,
    )


def _turn(client: TestClient, session_id: str, message: str, mode: str, key: str) -> dict[str, Any]:
    started = client.post(
        f"/api/v1/agent/sessions/{session_id}/runs",
        headers={"Idempotency-Key": key},
        json={"active_chapter_id": None, "message": message, "writer_mode": mode},
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["run"]["run_id"]
    for _ in range(1000):
        run = client.get(f"/api/v1/agent/runs/{run_id}").json()["run"]
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_casual_chat_writes_on_the_desktop_with_the_write_tool_and_undoes_it(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(environment_context, "home_folder", lambda: home)
    target = home / "Desktop" / "测试.txt"
    provider = ScriptedProvider(
        [
            ("write_file", {"path": "Desktop/测试.txt", "content": "这是一句话。"}),
            ("undo_change", {}),
        ]
    )
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()
        first = _turn(client, session["session_id"], "在桌面建一个 测试.txt，写一句话", "author_review_change_proposal", "p0-desk-0001")
        assert first["status"] == "succeeded", first
        assert target.read_text(encoding="utf-8") == "这是一句话。"
        second = _turn(client, session["session_id"], "撤回", "author_review_change_proposal", "p0-desk-0002")
        assert second["status"] == "succeeded", second
    assert not target.exists()
    assert {"write_file", "edit_file", "undo_change", "delete_file"} <= set(provider.catalogs[0])

    opening = provider.requests[0][0]
    assert opening["role"] == "system"
    lines = str(opening["content"]).splitlines()
    assert [line.split("：", 1)[0] for line in lines] == ["系统", "用户主目录", "当前文件夹", "今天", "挡位"]
    assert f"用户主目录：{home}" in lines
    assert "当前文件夹：未打开" in lines
    assert "挡位：完全访问" in lines
    # 第二轮开头那段一字不差（前缀稳定）；挡位没变，末尾不追加。
    later_opening = next(request for request in provider.requests if request[-1].get("role") == "user" and request[-1].get("content") == "撤回")[0]
    assert later_opening == opening


def test_switching_to_read_only_drops_write_tools_and_appends_the_new_tier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = ScriptedProvider([None, None])
    with TestClient(_app(tmp_path, provider)) as client:
        session = client.post("/api/v1/agent/sessions", json={"title": "随便聊聊"}).json()
        _turn(client, session["session_id"], "你好", "author_review_change_proposal", "p0-tier-0001")
        second = _turn(client, session["session_id"], "再聊聊", "repair_only", "p0-tier-0002")
        assert second["status"] == "succeeded", second
    first_request, second_request = provider.requests[0], provider.requests[1]
    assert "write_file" in provider.catalogs[0]
    assert "write_file" not in provider.catalogs[1] and "computer_run_command" not in provider.catalogs[1]
    assert second_request[0] == first_request[0]
    assert second_request[-1] == {"role": "system", "content": "挡位：只读"}
    assert all(m.get("content") != "挡位：只读" for m in first_request)


def test_environment_update_adds_the_new_date_after_midnight() -> None:
    from datetime import datetime, timedelta

    started = datetime(2026, 9, 26, 10, 0).astimezone()  # 本地上午十点
    facts = environment_context.EnvironmentFacts(folder=None, started_at=started, tier="完全访问")
    assert environment_context.update_message(facts, now=started + timedelta(hours=1), tier="完全访问") is None
    later = environment_context.update_message(facts, now=started + timedelta(days=1), tier="完全访问")
    assert later is not None and str(later["content"]).startswith("今天：")
    assert environment_context.date_text(started + timedelta(days=1))[:10] in str(later["content"])
