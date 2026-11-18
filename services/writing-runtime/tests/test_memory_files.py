"""跨对话记忆（纠偏指令 3.7）：用户记忆《用户记忆.md》（旧名《作者偏好.md》原样改名过来）、
项目记忆 = 打开的文件夹里的 AGENTS.md（也认 CLAUDE.md、老书的《写作笔记.md》），memory_write 的 user / project 两种；
打开文件夹的对话不再每轮编资料包（3.9），开头带用户记忆和说明文件，系统消息只有环境那一段。
老的资料包路径（写作笔记进资料包）后端还在，等 T15 一起删，那几条照旧测。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import (
    AgentToolError,
    AgentToolExecutor,
)
from zhimeng_runtime.memory_files import (
    LEGACY_USER_MEMORY_FILE_NAME,
    USER_MEMORY_FILE_NAME,
    append_user_memory,
    read_project_instructions,
    read_user_memory,
)
from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import ContextCreateRequest, canonical_json_hash
from zhimeng_runtime.context_service import ContextService
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus
from zhimeng_runtime.project_kb_models import ProjectKbRebuildRequest
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader

from test_agent_direct_write_tools import _build_runtime as _build_chat_runtime
from test_agent_direct_write_tools import _call
from test_agent_service import (
    FINGERPRINT,
    PROJECT_ID,
    PROJECT_KB_MANIFEST_HASH,
    PROJECT_KB_STATE_ID,
    CurrentProjectKbVerifier,
    ScriptedProvider,
    wait_terminal,
)
from test_context_api import (
    _capabilities,
    _executor,
    _write_project,
)


def _context_harness(tmp_path: Path, *, notes: str | None = None):
    library = tmp_path / "library"
    library.mkdir()
    project = _write_project(library / "novel")
    if notes is not None:
        (project / "写作笔记.md").write_text(notes, encoding="utf-8")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)
    identity = snapshot["project"]
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    binding = database.sync_project_binding(
        project_id=str(identity["project_id"]),
        root_path=str(project),
        display_name="novel",
        fingerprint=str(identity["fingerprint"]),
    )
    agent_store = AgentStore(database)
    session = agent_store.create_session(
        project_id=binding.project_id,
        title="memory context tests",
        executor=_executor(),
        capabilities=_capabilities(),
    )
    project_kb = ProjectKbService(database, reader, tmp_path / "project-kb")
    project_kb.store.ensure_project_scope(binding.project_id)
    knowledge = project_kb.store.project_state_snapshot(binding.project_id)
    rebuilt = project_kb.rebuild(
        binding.project_id,
        ProjectKbRebuildRequest(
            expected_project_revision=binding.revision,
            expected_project_fingerprint=binding.fingerprint,
            expected_scope_state_revision=knowledge.scope.state_revision,
            expected_knowledge_fingerprint=canonical_json_hash(
                knowledge.fingerprint_material
            ),
        ),
    )
    assert rebuilt.record.manifest is not None
    service = ContextService(
        ContextStore(database),
        agent_store,
        reader,
        ProjectLockRegistry(),
        project_kb_service=project_kb,
    )
    request = ContextCreateRequest(
        objective="聊聊这本书接下来的走向。",
        profile="chat",
        active_chapter_id=None,
        expected_project_revision=binding.revision,
        expected_project_fingerprint=binding.fingerprint,
        project_kb_state_id=rebuilt.record.manifest.state_id,
        project_kb_manifest_hash=rebuilt.record.manifest.manifest_hash,
    )
    return service, session.session_id, request


def _compile(service: ContextService, session_id: str, request: ContextCreateRequest):
    return asyncio.run(
        service.create_pack(session_id, request, idempotency_key="memory-pack-0001")
    )


def test_writing_notes_join_the_pack_when_present(tmp_path: Path) -> None:
    service, session_id, request = _context_harness(
        tmp_path, notes="这本书主角叫沈砚。\n"
    )

    pack = _compile(service, session_id, request).pack

    notes = [
        fragment
        for fragment in pack.included
        if fragment.source_type == "project_writing_notes"
    ]
    assert len(notes) == 1
    assert notes[0].priority == 990
    assert notes[0].mandatory is True
    assert notes[0].title == "写作笔记.md"
    assert "沈砚" in notes[0].content
    assert "沈砚" in pack.exact_provider_messages[0].content


def test_writing_notes_absent_is_silent(tmp_path: Path) -> None:
    service, session_id, request = _context_harness(tmp_path)

    pack = _compile(service, session_id, request).pack

    assert all(
        fragment.source_type != "project_writing_notes" for fragment in pack.included
    )
    assert all(
        fragment.source_type != "project_writing_notes" for fragment in pack.excluded
    )
    # 没有因为笔记冒出阻断（这个夹具本身就有别的 WARNING 门，不算笔记的）。
    assert pack.readiness.value != "blocked"
    assert not any("写作笔记" in gate.message for gate in pack.gates)


def test_writing_notes_blank_is_silent_not_blocked(tmp_path: Path) -> None:
    service, session_id, request = _context_harness(tmp_path, notes="  \n")

    pack = _compile(service, session_id, request).pack

    assert all(
        fragment.source_type != "project_writing_notes" for fragment in pack.included
    )
    assert all(
        fragment.source_type != "project_writing_notes" for fragment in pack.excluded
    )
    assert pack.readiness.value != "blocked"
    assert not any("写作笔记" in gate.message for gate in pack.gates)


def test_memory_write_project_appends_to_agents_md_and_rolls_back(tmp_path: Path) -> None:
    executor, run, _chapter, _database, change_sets, project = _build_chat_runtime(
        tmp_path
    )

    first = asyncio.run(
        _call(
            executor,
            run,
            "memory_write",
            {"scope": "project", "text": "这本书主角叫沈砚"},
            call_id="call-memory-project-0001",
        )
    )
    assert first.output["operation"] == "created"
    agents_path = project / "AGENTS.md"
    assert agents_path.read_text(encoding="utf-8") == "## 记住的事\n\n- 这本书主角叫沈砚\n"

    second = asyncio.run(
        _call(
            executor,
            run,
            "memory_write",
            {"scope": "project", "text": "第一章别让师父出场"},
            call_id="call-memory-project-0002",
        )
    )
    assert second.output["operation"] == "updated"
    assert agents_path.read_text(encoding="utf-8") == (
        "## 记住的事\n\n- 这本书主角叫沈砚\n- 第一章别让师父出场\n"
    )

    # 两次都是 ChangeSet，修改记录里查得到、可撤回。
    record = change_sets.get(second.output["change_set_id"])
    assert record.status.value == "applied"
    assert record.origin.value == "agent"
    rolled_back = change_sets.rollback(
        record.change_set_id,
        ChangeSetRollbackRequest(expected_review_revision=record.review_revision),
    )
    assert rolled_back.status.value == "rolled_back"
    assert agents_path.read_text(encoding="utf-8") == "## 记住的事\n\n- 这本书主角叫沈砚\n"


def test_memory_write_user_lands_in_user_memory(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _cs, _project = _build_chat_runtime(tmp_path)

    result = asyncio.run(
        _call(
            executor,
            run,
            "memory_write",
            {"scope": "user", "text": "我不喜欢每句另起一行"},
            call_id="call-memory-user-0001",
        )
    )
    assert result.output["applied"] is True
    assert result.output["scope"] == "user"
    assert USER_MEMORY_FILE_NAME in str(result.output["path"])
    assert read_user_memory(tmp_path) == "我不喜欢每句另起一行\n"

    # 再记一条是追加，不是覆盖。
    asyncio.run(
        _call(
            executor,
            run,
            "memory_write",
            {"scope": "user", "text": "回答别太长"},
            call_id="call-memory-user-0002",
        )
    )
    assert read_user_memory(tmp_path) == "我不喜欢每句另起一行\n回答别太长\n"


def test_memory_write_still_accepts_the_old_scope_names(tmp_path: Path) -> None:
    """9-26 以前叫 author / book：老对话里的调用、记住旧名字的模型照收，落到新位置。"""
    executor, run, _chapter, _database, _cs, project = _build_chat_runtime(tmp_path)

    asyncio.run(
        _call(executor, run, "memory_write", {"scope": "author", "text": "回答别太长"}, call_id="call-memory-old-author")
    )
    asyncio.run(
        _call(executor, run, "memory_write", {"scope": "book", "text": "主角叫沈砚"}, call_id="call-memory-old-book")
    )

    assert read_user_memory(tmp_path) == "回答别太长\n"
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "## 记住的事\n\n- 主角叫沈砚\n"
    assert not (project / "写作笔记.md").exists()


def test_memory_write_skips_repeating_the_last_line(tmp_path: Path) -> None:
    """模型把同一句连着记两次时，第二次不重复写（真机验收里见过）。"""
    executor, run, _chapter, _database, _cs, project = _build_chat_runtime(tmp_path)

    for scope in ("user", "project"):
        first = asyncio.run(
            _call(
                executor,
                run,
                "memory_write",
                {"scope": scope, "text": "章节名一律四个字"},
                call_id=f"call-memory-dup-{scope}-1",
            )
        )
        second = asyncio.run(
            _call(
                executor,
                run,
                "memory_write",
                {"scope": scope, "text": "章节名一律四个字"},
                call_id=f"call-memory-dup-{scope}-2",
            )
        )
        assert first.output["applied"] is True
        assert second.output["applied"] is False or second.output["operation"] == "unchanged"

    assert read_user_memory(tmp_path) == "章节名一律四个字\n"
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "## 记住的事\n\n- 章节名一律四个字\n"


def test_memory_write_project_needs_an_open_folder(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _cs, _project = _build_chat_runtime(tmp_path)
    casual_run = run.model_copy(update={"project_revision": None})

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                casual_run,
                "memory_write",
                {"scope": "project", "text": "主角叫沈砚"},
                call_id="call-memory-project-nofolder",
            )
        )
    assert excinfo.value.code == "agent_tool_project_not_found"


def test_old_author_preferences_file_is_renamed_to_user_memory_untouched(tmp_path: Path) -> None:
    legacy = tmp_path / LEGACY_USER_MEMORY_FILE_NAME
    legacy.write_bytes("我不喜欢每句另起一行\r\n".encode("utf-8"))

    assert read_user_memory(tmp_path) == "我不喜欢每句另起一行\r\n"
    assert not legacy.exists()
    assert (tmp_path / USER_MEMORY_FILE_NAME).read_bytes() == "我不喜欢每句另起一行\r\n".encode("utf-8")


def test_project_instructions_read_agents_claude_and_old_notes_in_order(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("用两个空格缩进。\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("先跑测试再提交。\n", encoding="utf-8")
    (tmp_path / "写作笔记.md").write_text("主角叫沈砚。\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("不算说明文件。\n", encoding="utf-8")
    big = tmp_path / "big"
    big.mkdir()
    (big / "AGENTS.md").write_text("字" * 20_000, encoding="utf-8")

    assert read_project_instructions(tmp_path) == [
        ("AGENTS.md", "先跑测试再提交。"),
        ("CLAUDE.md", "用两个空格缩进。"),
        ("写作笔记.md", "主角叫沈砚。"),
    ]
    ((name, content),) = read_project_instructions(big)
    assert name == "AGENTS.md" and len(content.encode("utf-8")) < 32 * 1024 + 200
    assert content.endswith("要看全文就读这个文件。）")


class _RecordingProvider(ScriptedProvider):
    """ScriptedProvider 不记工具目录；这个把最近一次请求的工具名也留下来。"""

    def __init__(self) -> None:
        super().__init__([ProviderTextDelta("好。"), ProviderCompleted("stop", None)])
        self.tool_names: list[str] = []

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        self.tool_names = [tool.name for tool in tools]
        async for event in super().stream_chat(
            messages, cancel_event=cancel_event, tools=tools
        ):
            yield event


def _service_with_tools(tmp_path: Path, provider: _RecordingProvider) -> AgentService:
    """build_service 不带工具执行器；随便聊要裁工具目录，这里补一个真的。"""

    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    database.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(tmp_path / "novel"),
        display_name="Novel",
        fingerprint=FINGERPRINT,
    )
    reader = ProjectReader((tmp_path,))
    executor = AgentToolExecutor(
        reader,
        database,
        ProjectLockRegistry(),
        ChangeSetService(reader, database, tmp_path / "change-sets"),
    )
    return AgentService(
        AgentStore(database),
        provider,
        tool_executor=executor,
        project_kb_service=CurrentProjectKbVerifier(),
    )


def test_user_memory_injection_for_new_conversations(tmp_path: Path) -> None:
    async def scenario() -> None:
        # 文件不存在：随便聊发给模型的消息里没有记忆注入。
        provider = _RecordingProvider()
        service = _service_with_tools(tmp_path, provider)
        session = await service.create_session(project_id=None, title="随便聊")
        started = await service.start_run(
            session_id=session.session_id,
            message="今天天气怎么样。",
            expected_project_revision=None,
            expected_project_fingerprint=None,
            active_chapter_id=None,
            idempotency_key="memory-casual-none",
            writer_mode="author_review_change_proposal",
        )
        run = await wait_terminal(service, started.run.run_id)
        assert run.status is RunStatus.SUCCEEDED
        assert not any("用户记忆" in str(message.get("content", "")) for message in provider.messages)

        # 随便聊（完全访问）挂着 memory_write（与技能翻阅、电脑访问、执行命令同组）。
        assert "memory_write" in provider.tool_names

        # 写入记忆后新开的随便聊：第一句请求里带「用户记忆…」，且不是 system。
        append_user_memory(tmp_path, "我不喜欢每句另起一行")
        session2 = await service.create_session(project_id=None, title="随便聊2")
        started2 = await service.start_run(
            session_id=session2.session_id,
            message="又见面了。",
            expected_project_revision=None,
            expected_project_fingerprint=None,
            active_chapter_id=None,
            idempotency_key="memory-casual-pref",
        )
        run2 = await wait_terminal(service, started2.run.run_id)
        assert run2.status is RunStatus.SUCCEEDED
        # 最前面是环境事实（纠偏指令 3.2），记忆紧跟其后。
        assert str(provider.messages[0]["content"]).startswith("系统：")
        assert provider.messages[1] == {
            "role": "user",
            "content": "用户记忆（以前的对话里记下的）：\n我不喜欢每句另起一行",
        }
        assert not any(
            message.get("role") == "system" and "用户记忆" in str(message.get("content", ""))
            for message in provider.messages
        )
        await service.shutdown()

    asyncio.run(scenario())


def test_open_folder_chat_without_a_pack_carries_memory_and_folder_instructions(tmp_path: Path) -> None:
    """打开文件夹的对话不再每轮编资料包（3.9）：开头是环境事实，再是用户记忆、文件夹里的说明文件，
    以前那段「这是和作者的普通对话……」的系统消息没有了。"""

    async def scenario() -> None:
        append_user_memory(tmp_path, "我不喜欢每句另起一行")
        folder = tmp_path / "novel"
        folder.mkdir()
        (folder / "AGENTS.md").write_text("## 记住的事\n\n- 主角叫沈砚\n", encoding="utf-8")
        provider = _RecordingProvider()
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        database.sync_project_binding(
            project_id=PROJECT_ID,
            root_path=str(folder),
            display_name="Novel",
            fingerprint=FINGERPRINT,
        )
        service = AgentService(
            AgentStore(database),
            provider,
            project_kb_service=CurrentProjectKbVerifier(),
        )
        session = await service.create_session(project_id=PROJECT_ID, title="打开文件夹")
        started = await service.start_run(
            session_id=session.session_id,
            message="保留线索，收紧开头。",
            expected_project_revision=1,
            expected_project_fingerprint=FINGERPRINT,
            active_chapter_id=None,
            idempotency_key="memory-open-folder",
        )
        run = await wait_terminal(service, started.run.run_id)
        assert run.status is RunStatus.SUCCEEDED
        assert str(provider.messages[0]["content"]).startswith("系统：")
        assert f"当前文件夹：{folder}" in str(provider.messages[0]["content"])
        assert provider.messages[1] == {
            "role": "user",
            "content": "用户记忆（以前的对话里记下的）：\n我不喜欢每句另起一行",
        }
        assert provider.messages[2] == {
            "role": "user",
            "content": "这个文件夹里的说明文件：\n\n── AGENTS.md ──\n## 记住的事\n\n- 主角叫沈砚",
        }
        assert provider.messages[-1] == {"role": "user", "content": "保留线索，收紧开头。"}
        system_messages = [message for message in provider.messages if message.get("role") == "system"]
        assert len(system_messages) == 1
        assert not any("作者" in str(message.get("content", "")) for message in system_messages)
        await service.shutdown()

    asyncio.run(scenario())
