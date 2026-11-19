"""历史补话路径退役后的完成行为与文件工具回归。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from test_context_agent_e2e import AnswerOnlyProvider
from test_context_api import _write_project
from test_correction_p0_20260926 import _SESSION_TITLE_PROMPT, _app
from test_open_folder_chat_20260927 import _bound_turn, _open_folder
from zhimeng_runtime import environment_context
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.database import SqliteStore


class StepProvider(AnswerOnlyProvider):
    """每调一次模型走一步：字符串 = 只说这句话收尾；(工具名, 参数) = 调这个工具。"""

    def __init__(self, steps: list[str | tuple[str, dict[str, Any]]]) -> None:
        super().__init__()
        self.steps = list(steps)
        self.requests: list[list[dict[str, Any]]] = []
        self.offered: list[list[str]] = []

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        if any(m.get("role") == "system" and m.get("content") == _SESSION_TITLE_PROMPT for m in messages):
            yield ProviderTextDelta("改稿")
            yield ProviderCompleted("stop", None)
            return
        self.requests.append([dict(m) for m in messages])
        self.offered.append(sorted(tool.name for tool in tools))
        step = self.steps.pop(0) if self.steps else "好了。"
        if isinstance(step, str):
            yield ProviderTextDelta(step)
            yield ProviderCompleted("stop", None)
            return
        name, arguments = step
        yield ProviderToolCall(index=0, tool_call_id=f"call_{len(self.requests):04d}", tool_name=name, arguments=arguments)
        yield ProviderCompleted("tool_calls", None)


def _chat(tmp_path: Path, monkeypatch, steps, *, writer_mode: str = "author_review_change_proposal", prepare=None):
    monkeypatch.setattr(environment_context, "home_folder", lambda: tmp_path)
    provider = StepProvider(steps)
    app = _app(tmp_path, provider)
    project = _write_project(tmp_path / "library" / "novel")
    if prepare is not None:
        prepare(project)
    with TestClient(app) as client:
        snapshot = _open_folder(client, project)
        session = client.post(
            f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions", json={"title": "改稿"}
        ).json()
        run = _bound_turn(
            client,
            session["session_id"],
            snapshot,
            {"message": "第一章那句改狠一点", "active_chapter_id": None, "writer_mode": writer_mode},
            "said-but-not-done-0001",
        )
    with SqliteStore(tmp_path / "runtime.sqlite3").connection() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM run_event WHERE run_id = ? AND event_type = 'model_turn_nudged' ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
    return run, [json.loads(row["payload_json"]) for row in rows], provider, project


def test_announcing_and_stopping_does_not_create_a_user_instruction(tmp_path: Path, monkeypatch) -> None:
    announced = "我去看看第一章。\n\n请稍等，我马上调用工具读取文件……"
    run, nudges, provider, _project = _chat(tmp_path, monkeypatch, [announced, "看完了：第一章写林澈收到匿名信。"])
    assert run["status"] == "succeeded"
    assert nudges == []
    assert len(provider.requests) == 1


def test_claiming_a_change_with_no_file_tool_does_not_invent_a_user_request(tmp_path: Path, monkeypatch) -> None:
    run, nudges, provider, _project = _chat(
        tmp_path, monkeypatch, ["我已经把那句台词改成了更狠的版本，别的没动。", "修改已保存。"]
    )
    assert run["status"] == "succeeded"
    assert nudges == []
    assert len(provider.requests) == 1


def test_a_claim_after_a_real_edit_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    run, nudges, provider, project = _chat(
        tmp_path,
        monkeypatch,
        [
            ("edit_file", {"path": "05_正文内容/第一卷/CH001_定稿.md", "find": "匿名信", "replace": "一封没署名的信"}),
            "已修改完毕。",
        ],
    )
    assert run["status"] == "succeeded"
    assert nudges == []
    assert "一封没署名的信" in (project / "05_正文内容" / "第一卷" / "CH001_定稿.md").read_text(encoding="utf-8")


def test_overwriting_most_of_a_file_preserves_the_actual_tool_result(tmp_path: Path, monkeypatch) -> None:
    chapter = "05_正文内容/第一卷/CH004_定稿.md"
    long_text = "".join(f"第四章第 {n} 段。\n\n" for n in range(1, 121))

    def prepare(project: Path) -> None:
        (project / "05_正文内容" / "第一卷" / "CH004_定稿.md").write_text(long_text, encoding="utf-8")

    run, nudges, provider, project = _chat(
        tmp_path,
        monkeypatch,
        [
            ("write_file", {"path": chapter, "content": "第四章第 1 段。第四章第 2 段。\n"}),
            "已完成，只合并了开头两段，其他内容完全不动。",
            ("undo_change", {}),
            "刚才整章被覆盖了，已经撤回，章节回到原样。",
        ],
        prepare=prepare,
    )
    assert run["status"] == "succeeded"
    assert nudges == []
    assert len(provider.requests) == 2
    assert "整份覆盖" in str(provider.requests[1])
    assert (project / "05_正文内容" / "第一卷" / "CH004_定稿.md").read_text(encoding="utf-8") == "第四章第 1 段。第四章第 2 段。\n"


def test_a_proposal_after_a_direct_write_in_the_same_turn_is_recorded(tmp_path: Path, monkeypatch) -> None:
    """9-27 真机：同一轮先 edit_file（版本往前走了）再「先看修改」，校验「提案基准 = 对话开头的版本」对不上，整轮报「程序出错了」。"""
    run, _nudges, provider, project = _chat(
        tmp_path,
        monkeypatch,
        [
            ("edit_file", {"path": "05_正文内容/第一卷/CH001_定稿.md", "find": "匿名信", "replace": "一封信"}),
            ("change_set_propose", {"title": "再改一句", "summary": "再改一句。", "changes": [{"operation": "edit", "find": "带血钥匙", "replace": "生锈的钥匙"}]}),
            "改了一处、提了一处。",
        ],
    )
    assert run["status"] == "succeeded", run
    assert (project / "05_正文内容" / "第一卷" / "CH001_定稿.md").read_text(encoding="utf-8") == "第一章：林澈收到一封信。"
    # 提案还没保存：第三章原样。
    assert (project / "05_正文内容" / "第一卷" / "CH003_定稿.md").read_text(encoding="utf-8") == "第三章：证人留下了带血钥匙。"


def test_read_file_gives_up_to_800_lines_by_default() -> None:
    from zhimeng_runtime.agent_tools import MAX_READ_LINES, ReadFileArguments

    assert ReadFileArguments(path="05_正文内容/第一卷/第四章.md").max_lines == MAX_READ_LINES == 800


def test_read_only_mode_does_not_nudge_write_claims(tmp_path: Path, monkeypatch) -> None:
    run, nudges, provider, _project = _chat(tmp_path, monkeypatch, ["修改已保存到原文件。"], writer_mode="repair_only")
    assert run["status"] == "succeeded"
    assert "edit_file" not in provider.offered[0]
    # 只读挡没有改文件的工具，「说改好了」补了也做不到，不补。
    assert nudges == [] and len(provider.requests) == 1
