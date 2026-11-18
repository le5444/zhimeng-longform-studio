from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import (
    AgentToolError,
    AgentToolExecutor,
)
from zhimeng_runtime.change_set_models import ChangeSetRollbackRequest
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import ContextCreateRequest
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS


def _build_runtime(
    tmp_path: Path,
    *,
    chapter_content: str = "第一行。\n第二行。\n",
    writer_mode: str = "author_review_change_proposal",
    suffix: str = "",
):
    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    project.mkdir()
    for index, label in SECTION_LABELS.items():
        (project / f"{index:02d}_{label}").mkdir()
    chapter = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    chapter.parent.mkdir()
    chapter.write_text(chapter_content, encoding="utf-8")

    reader = ProjectReader((library,))
    snapshot = reader.inspect(project)
    identity = snapshot["project"]
    database = SqliteStore(tmp_path / "runtime.sqlite3")
    database.initialize()
    binding = database.sync_project_binding(
        project_id=str(identity["project_id"]),
        root_path=str(identity["root_path"]),
        display_name=str(identity["display_name"]),
        fingerprint=str(identity["fingerprint"]),
    )
    store = AgentStore(database)
    session = store.create_session(
        project_id=binding.project_id,
        title="Direct write tests",
        executor=ExecutorIdentity(
            executor_id="executor.direct-write-tests",
            kind=ExecutorKind.EMBEDDED,
            implementation="direct-write-tests",
            version="1",
            protocol_version="1",
            instance_id=None,
        ),
        capabilities=CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.TOOL_CALLS],
            extensions={},
        ),
    )
    # 真实聊天链的 Run 都绑着 Context Pack（create_agent_candidate 硬性要求），
    # 这里编一个最小的 chat Pack 挂上；writer_mode 决定 writer_intent 的 mode。
    pack = compile_context_pack(
        context_pack_id=f"context-pack-direct-write{suffix}",
        session_id=session.session_id,
        project_id=binding.project_id,
        history_through_ordinal=0,
        request=ContextCreateRequest(
            objective="Write a note.",
            profile="chat",
            active_chapter_id="VOL001-CH001",
            expected_project_revision=binding.revision,
            expected_project_fingerprint=binding.fingerprint,
            project_kb_state_id="project-kb-state-" + "0" * 32,
            project_kb_manifest_hash="0" * 64,
        ),
        history=(),
        candidates=(),
        max_characters=20_000,
        created_at=datetime.now(UTC),
    )
    ContextStore(database).create_pack(pack)
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message="Write a note.",
        expected_project_revision=binding.revision,
        expected_project_fingerprint=binding.fingerprint,
        active_chapter_id="VOL001-CH001",
        writer_mode=writer_mode,
        idempotency_key=f"direct-write-tests-turn-0001{suffix}",
        context_pack_id=pack.context_pack_id,
        expected_context_pack_hash=pack.pack_hash,
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
    running = store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
    locks = ProjectLockRegistry()
    change_sets = ChangeSetService(
        reader,
        database,
        tmp_path / "change-sets",
    )
    executor = AgentToolExecutor(
        reader,
        database,
        locks,
        change_sets,
    )
    return executor, running, chapter, database, change_sets, project


async def _call(executor, run, tool_name, arguments, call_id="call-0001"):
    definition = executor._definitions_by_name[tool_name]
    request_hash = executor.request_hash(tool_name, definition.version, arguments)
    # 与真实链路一致：agent_service 先 begin_tool_call 落 provenance 记录，
    # create_agent_candidate 靠它建档。
    AgentStore(executor.store).begin_tool_call(
        run.run_id,
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_version=definition.version,
        arguments=arguments,
        request_hash=request_hash,
        idempotency_key=call_id,
    )
    return await executor.execute(
        run,
        tool_call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        request_hash=request_hash,
        cancel_event=asyncio.Event(),
    )


def test_write_file_refused_in_repair_only(tmp_path: Path) -> None:
    executor, run, _chapter, _database, _cs, _project = _build_runtime(
        tmp_path, writer_mode="repair_only", suffix="-ro"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_write_file",
                {"relative_path": "08_素材库/灵感.md", "content": "x"},
            )
        )
    assert excinfo.value.code == "writer_change_set_forbidden"


def test_write_file_creates_new_file_applies_and_rolls_back(tmp_path: Path) -> None:
    executor, run, _chapter, _db, change_sets, project = _build_runtime(tmp_path)

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "relative_path": "08_素材库/灵感.md",
                "content": "# 灵感\n\n一句题记。\n",
                "note": "作者让建一个灵感文件",
            },
        )
    )

    assert result.output["operation"] == "created"
    assert result.output["applied"] is True
    assert (project / "08_素材库" / "灵感.md").read_text(encoding="utf-8") == (
        "# 灵感\n\n一句题记。\n"
    )
    # 直接落盘不把 change_set 交给服务层（那会触发 CHANGE_PROPOSAL 活动的
    # 版本校验把 Run 判死）；修改记录里按输出里的 change_set_id 查。
    assert result.change_set is None
    change_set = change_sets.get(result.output["change_set_id"])
    assert change_set.status.value == "applied"

    rolled_back = change_sets.rollback(
        change_set.change_set_id,
        ChangeSetRollbackRequest(
            expected_review_revision=change_set.review_revision
        ),
    )
    assert rolled_back.status.value == "rolled_back"
    assert not (project / "08_素材库" / "灵感.md").exists()


def test_write_file_updates_existing_and_second_write_same_run(
    tmp_path: Path,
) -> None:
    executor, run, chapter, database, _cs, project = _build_runtime(
        tmp_path
    )

    first = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "content": "全新的第一章。\n",
            },
        )
    )
    assert first.output["operation"] == "updated"
    assert chapter.read_text(encoding="utf-8") == "全新的第一章。\n"

    # 关键回归：第一次落盘把 binding 推进了一格，同一场对话里的第二次
    # 写（以及它内部的重取快照）不能被「书变了」挡住。
    second = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "relative_path": "08_素材库/摘抄.md",
                "content": "摘抄一句。\n",
            },
            call_id="call-0002",
        )
    )
    assert second.output["operation"] == "created"
    assert (project / "08_素材库" / "摘抄.md").exists()

    store = AgentStore(database)
    refreshed = store.get_run(run.run_id)
    binding = database.get_project_binding(refreshed.project_revision.project_id)
    # run 的版本钉子不动（它是资料包基准，完整性校验靠它）；
    # 落盘后的新版本号带在工具结果里。
    assert refreshed.project_revision.revision == run.project_revision.revision
    assert second.output["result_revision"] == binding.revision
    assert second.output["result_fingerprint"] == binding.fingerprint


def test_agent_reads_back_what_it_just_wrote_in_the_same_run(
    tmp_path: Path,
) -> None:
    """写完能读回来核对。

    落盘必然把书的版本往前推一格，而 run.project_revision 是这场对话资料包的
    基准、不能动。以前读工具拿它当基准比对，结果 Agent 自己写完再列目录/读文件
    就被「Project changed after this Run was created」挡死——写完无法自查。
    """

    executor, run, _chapter, database, _cs, _project = _build_runtime(
        tmp_path, suffix="-read-back"
    )
    store = AgentStore(database)

    def record(tool_name: str, arguments: dict, call_id: str):
        """像真实链路那样把成功的工具结果写回 tool_call（钉子的来源）。"""

        execution = asyncio.run(
            _call(executor, run, tool_name, arguments, call_id=call_id)
        )
        if execution is not None:
            store.finish_tool_call(
                run.run_id,
                tool_call_id=call_id,
                status="succeeded",
                output=execution.output,
                error=None,
                duration_ms=1,
            )
        return execution

    written = record(
        "project_write_file",
        {
            "relative_path": "05_正文内容/第一卷/CH002_新章.md",
            "content": "第二章开头。\n",
        },
        "call-write-1",
    )
    assert written.output["applied"] is True

    listed = record("project_list_files", {"query": "CH002"}, "call-list-1")
    files = listed.output["files"]
    assert [item["relative_path"] for item in files] == ["05_正文内容/第一卷/CH002_新章.md"]

    read_back = record(
        "project_read_file", {"file_id": files[0]["file_id"]}, "call-read-1"
    )
    assert read_back.output["content"] == "第二章开头。\n"

    # 反过来：这场对话之外有人（作者本人保存）又把书改了一格，读工具必须仍然拦，
    # 不能因为「跟着自己的落盘走」就变成永远放行。
    database.sync_project_binding(
        project_id=run.project_revision.project_id,
        root_path=database.get_project_binding(
            run.project_revision.project_id
        ).root_path,
        display_name="novel",
        fingerprint="f" * 64,
    )
    with pytest.raises(AgentToolError) as refused:
        asyncio.run(
            _call(executor, run, "project_list_files", {"query": "CH002"}, "call-list-2")
        )
    assert refused.value.code == "agent_tool_project_changed"


def test_agent_can_propose_after_direct_write_in_the_same_run(
    tmp_path: Path,
) -> None:
    """同一场对话：先直接保存一份，再把后续改法提案给作者看。

    直接落盘会把书推进一格；提案以前拿资料包钉子（run.project_revision）当
    基准，同场对话里第二次改动必然被「Project changed after this Run was
    created」拦死。现在提案基准跟随本场自己的落盘钉子，这条工作流走得通。
    """
    executor, run, _chapter, database, _cs, _project = _build_runtime(
        tmp_path, suffix="-propose-after-write"
    )
    store = AgentStore(database)

    written = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "relative_path": "05_正文内容/第一卷/CH002_新章.md",
                "content": "第二章开头。\n",
            },
            call_id="call-write-1",
        )
    )
    assert written.output["applied"] is True
    store.finish_tool_call(
        run.run_id,
        tool_call_id="call-write-1",
        status="succeeded",
        output=written.output,
        error=None,
        duration_ms=1,
    )

    listed = asyncio.run(
        _call(executor, run, "project_list_files", {"query": "CH001"}, "call-list-1")
    )
    file_ids = {
        item["relative_path"]: item["file_id"] for item in listed.output["files"]
    }
    chapter_id = file_ids["05_正文内容/第一卷/CH001_正文.md"]

    proposed = asyncio.run(
        _call(
            executor,
            run,
            "change_set_propose",
            {
                "title": "把开头改顺",
                "summary": "作者要求的开头润色",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter_id,
                        "after_content": "改顺的第一行。\n第二行。\n",
                        "rationale": "作者要求把开头改顺",
                    }
                ],
            },
            call_id="call-propose-1",
        )
    )
    assert proposed.output["applied"] is False
    assert proposed.output["base_revision"] == written.output["result_revision"]
    assert proposed.output["base_fingerprint"] == written.output["result_fingerprint"]


def test_write_file_unchanged_content_is_noop(tmp_path: Path) -> None:
    executor, run, chapter, _db, _cs, _project = _build_runtime(tmp_path)

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "content": chapter.read_text(encoding="utf-8"),
            },
        )
    )
    assert result.output["operation"] == "unchanged"
    assert result.change_set is None


def test_edit_file_replaces_one_passage(
    tmp_path: Path,
) -> None:
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path
    )

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_edit_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "find": "第二行。",
                "replace": "改过的第二行。",
            },
        )
    )
    assert result.output["operation"] == "updated"
    assert result.output["occurrences"] == 1
    assert chapter.read_text(encoding="utf-8") == "第一行。\n改过的第二行。\n"


def test_edit_file_missing_and_ambiguous_find_fail_closed(
    tmp_path: Path,
) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content="同一句。\n同一句。\n"
    )

    with pytest.raises(AgentToolError) as missing:
        asyncio.run(
            _call(
                executor,
                run,
                "project_edit_file",
                {
                    "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                    "find": "根本不存在的句子",
                    "replace": "x",
                },
            )
        )
    assert missing.value.code == "agent_tool_edit_find_missing"

    with pytest.raises(AgentToolError) as ambiguous:
        asyncio.run(
            _call(
                executor,
                run,
                "project_edit_file",
                {
                    "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                    "find": "同一句。",
                    "replace": "x",
                },
                call_id="call-0002",
            )
        )
    assert ambiguous.value.code == "agent_tool_edit_find_ambiguous"

    replaced_all = asyncio.run(
        _call(
            executor,
            run,
            "project_edit_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "find": "同一句。",
                "replace": "换掉了。",
                "replace_all": True,
            },
            call_id="call-0003",
        )
    )
    assert replaced_all.output["occurrences"] == 2


@pytest.mark.parametrize(
    "bad_path",
    [
        "../隔壁书/正文.md",
        "05_正文内容/../秘密.md",
        ".git/config",
        "08_素材库/.env",
        # 9-26 起绝对路径（打开的文件夹以外记进 file_journal）和反斜杠都照收，
        # 见 test_correction_p0_20260926.py。
    ],
)
def test_write_file_rejects_unsafe_paths(tmp_path: Path, bad_path: str) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path)

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_write_file",
                {"relative_path": bad_path, "content": "x"},
            )
        )
    assert excinfo.value.code == "agent_tool_validation_failed"


def test_write_file_follows_the_authors_own_folder_layout(tmp_path: Path) -> None:
    """作者怎么摆文件夹，Agent 就写到哪。

    以前合同的相对路径必须写成 01_~09_ 打头，还不许只有一个路径段：
    一本按自己方式摆的书（或者书根放一份「作者的话」），Agent 落盘会被
    路径校验挡死。01_~09_ 只是新建书时的默认夹子，不是准入条件。
    """

    executor, run, _chapter, _db, change_sets, project = _build_runtime(tmp_path)

    in_new_folder = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {"relative_path": "设定/功法体系.md", "content": "剑气三式。\n"},
            call_id="call-layout-1",
        )
    )
    at_book_root = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {"relative_path": "作者的话.md", "content": "写给读者。\n"},
            call_id="call-layout-2",
        )
    )
    assert in_new_folder.output["operation"] == "created"
    assert at_book_root.output["operation"] == "created"
    assert (project / "设定" / "功法体系.md").read_text(encoding="utf-8") == "剑气三式。\n"
    assert (project / "作者的话.md").read_text(encoding="utf-8") == "写给读者。\n"

    for execution in (in_new_folder, at_book_root):
        record = change_sets.get(execution.output["change_set_id"])
        rolled_back = change_sets.rollback(
            record.change_set_id,
            ChangeSetRollbackRequest(expected_review_revision=record.review_revision),
        )
        assert rolled_back.status.value == "rolled_back"
    assert not (project / "设定" / "功法体系.md").exists()
    assert not (project / "作者的话.md").exists()


def test_computer_tools_read_anywhere_but_never_env_files(tmp_path: Path) -> None:
    executor, run, chapter, _db, _cs, project = _build_runtime(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("书库外面的文件\n", encoding="utf-8")

    # 空路径列打开的文件夹（9-26 以前列可信根；没打开文件夹时列用户主目录）。
    opened = asyncio.run(_call(executor, run, "computer_list_files", {"path": ""}))
    assert opened.output["path"] == str(project.resolve())
    assert "05_正文内容" in [entry["name"] for entry in opened.output["entries"]]

    listing = asyncio.run(
        _call(
            executor,
            run,
            "computer_list_files",
            {"path": str(project), "query": "正文"},
            call_id="call-0002",
        )
    )
    names = [entry["name"] for entry in listing.output["entries"]]
    assert "05_正文内容" in names
    assert "01_项目定位" not in names  # 被 query 过滤

    read = asyncio.run(
        _call(
            executor,
            run,
            "computer_read_file",
            {"path": str(chapter)},
            call_id="call-0003",
        )
    )
    assert "第一行。" in read.output["content"]

    # 绝对路径电脑上哪里都能读：读的范围不比能执行命令的范围窄（2026-09-22 审阅 §3）。
    outside_read = asyncio.run(
        _call(
            executor,
            run,
            "computer_read_file",
            {"path": str(outside)},
            call_id="call-0004",
        )
    )
    assert "书库外面的文件" in outside_read.output["content"]

    env_file = tmp_path / "library" / ".env"
    env_file.write_text("SECRET=1\n", encoding="utf-8")
    with pytest.raises(AgentToolError) as env_exc:
        asyncio.run(
            _call(
                executor,
                run,
                "computer_read_file",
                {"path": str(env_file)},
                call_id="call-0005",
            )
        )
    assert env_exc.value.code == "agent_tool_computer_outside_roots"


def test_computer_read_file_reads_any_text_suffix_and_hints_binary(tmp_path: Path) -> None:
    """读电脑文件不再按后缀白名单拒（2026-09-20 P2 第 28 条）：未列名的文本
    后缀照常读；损坏的 Office 文件给明确解析错误，不冒充已读内容。"""
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path)
    library = tmp_path / "library"
    ini = library / "笔记.ini"
    ini.write_text("[设定]\n力量体系=借势\n", encoding="utf-8")
    docx = library / "课件.docx"
    docx.write_bytes(b"PK\x03\x04" + b"\x00" * 32)

    read = asyncio.run(
        _call(executor, run, "computer_read_file", {"path": str(ini)}, call_id="call-0011")
    )
    assert "力量体系" in read.output["content"]

    with pytest.raises(AgentToolError) as binary_exc:
        asyncio.run(
            _call(
                executor,
                run,
                "computer_read_file",
                {"path": str(docx)},
                call_id="call-0012",
            )
        )
    assert binary_exc.value.code == "agent_tool_office_unreadable"
    assert "损坏" in str(binary_exc.value)


def test_run_command_executes_and_reports_exit_code(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(tmp_path)

    result = asyncio.run(
        _call(executor, run, "computer_run_command", {"command": "echo hello-agent"})
    )

    assert result.output["exit_code"] == 0
    assert "hello-agent" in result.output["stdout"]
    assert result.output["timed_out"] is False
    # 默认工作目录是当前这本书的文件夹
    assert Path(result.output["workdir"]) == project.resolve()
    # 绑了书的对话：结果带最新版本钉子，收尾校验以它为准
    assert isinstance(result.output["result_revision"], int)
    assert result.output["result_fingerprint"]


def test_run_command_reports_nonzero_exit_without_raising(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-exit"
    )

    result = asyncio.run(
        _call(executor, run, "computer_run_command", {"command": "exit 3"})
    )

    assert result.output["exit_code"] == 3
    assert result.output["timed_out"] is False


def test_run_command_times_out_and_kills_the_tree(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-to"
    )

    result = asyncio.run(
        _call(
            executor,
            run,
            "computer_run_command",
            {"command": "ping -n 10 127.0.0.1 >nul", "timeout_seconds": 1},
        )
    )

    assert result.output["timed_out"] is True


def test_run_command_refused_in_repair_only(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, writer_mode="repair_only", suffix="-ro-cmd"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(executor, run, "computer_run_command", {"command": "echo x"})
        )
    assert excinfo.value.code == "writer_change_set_forbidden"


def test_run_command_rejects_missing_workdir(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-wd"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "computer_run_command",
                {"command": "echo x", "workdir": str(tmp_path / "不存在的目录")},
            )
        )
    assert excinfo.value.code == "agent_tool_computer_path_not_found"


def test_run_command_that_touches_the_book_refreshes_the_pin(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, suffix="-pin"
    )
    before = run.project_revision
    assert before is not None

    result = asyncio.run(
        _call(
            executor,
            run,
            "computer_run_command",
            {"command": 'echo note> "08_素材库\\命令建的.md"'},
        )
    )

    assert result.output["exit_code"] == 0
    assert (project / "08_素材库" / "命令建的.md").exists()
    # 命令动过书：结果里的钉子已经前进，这场对话后续读写不会被「书变了」误杀
    assert result.output["result_revision"] > before.revision


@pytest.mark.parametrize("cancel_kind", ["task", "event", "timeout"])
def test_running_command_ends_its_child_tree_before_return(tmp_path, monkeypatch, cancel_kind):
    import ctypes
    import json
    import os
    import subprocess
    import sys
    import threading
    if os.name != "nt":
        pytest.skip("Windows process-tree regression")
    executor, run, _chapter, _db, _cs, _project = _build_runtime(tmp_path)
    work = tmp_path / "command-work"
    work.mkdir()
    child = work / "child.py"
    child.write_text(
        "import json,os,time\nfrom pathlib import Path\n"
        "Path('ready.tmp').write_text(json.dumps([os.getpid(),os.getppid()]))\n"
        "Path('ready.tmp').replace('ready.json')\n"
        "while True:\n Path('heartbeat').write_text(str(time.monotonic()))\n time.sleep(.05)\n",
        encoding="utf-8",
    )
    parent = work / "parent.py"
    parent.write_text(
        "import subprocess,sys\nsubprocess.Popen([sys.executable,'child.py']).wait()\n",
        encoding="utf-8",
    )
    joined = threading.Event()
    original = executor._run_command
    def tracked(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            joined.set()
    monkeypatch.setattr(executor, "_run_command", tracked)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    async def scenario():
        arguments = {"command": subprocess.list2cmdline([sys.executable, str(parent)]),
                     "workdir": str(work), "timeout_seconds": 2 if cancel_kind == "timeout" else 30}
        definition = executor._definitions_by_name["computer_run_command"]
        cancel_event = asyncio.Event()
        task = asyncio.create_task(executor.execute(
            run, tool_call_id="call-cancel-tree", tool_name="computer_run_command", arguments=arguments,
            request_hash=executor.request_hash("computer_run_command", definition.version, arguments),
            cancel_event=cancel_event))
        try:
            for _ in range(150):
                if (work / "ready.json").exists():
                    break
                await asyncio.sleep(.02)
            assert (work / "ready.json").exists(), "test child did not start"
            for pid in json.loads((work / "ready.json").read_text()):
                handle = kernel.OpenProcess(0x00100000, False, pid)
                assert handle, f"test process {pid} was not running"
                handles.append(handle)
            if cancel_kind == "task":
                task.cancel()
                await asyncio.sleep(.01)
                task.cancel()  # A second stop must not detach the cleanup worker.
            elif cancel_kind == "event":
                cancel_event.set()
            if cancel_kind == "timeout":
                result = await asyncio.wait_for(task, timeout=10)
                assert result.output["timed_out"] is True
                assert result.output["cancelled"] is False
            else:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
            assert joined.is_set(), "command worker outlived its cancelled awaiter"
            assert all(kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles)
            heartbeat = (work / "heartbeat").read_bytes()
            await asyncio.sleep(.15)
            assert (work / "heartbeat").read_bytes() == heartbeat
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            for handle in handles:
                kernel.CloseHandle(handle)
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["cancel", "interrupt", "fail"])
def test_applied_direct_write_is_recovered_when_tool_result_was_lost(tmp_path, terminal):
    executor, run, _chapter, database, _cs, project = _build_runtime(tmp_path)
    result = asyncio.run(_call(executor, run, "project_write_file",
                               {"relative_path": "08_素材库/recovered.md", "content": "saved exactly once"},
                               call_id="call-recover-write"))
    store = AgentStore(database)
    # _call executes the write but deliberately does not finish_tool_call: this
    # reproduces the crash window after atomic file apply and before result save.
    assert store.get_tool_call("call-recover-write").status == "running"
    if terminal == "cancel":
        store.cancel_run(run.run_id)
        store.mark_cancelled(run.run_id)
    elif terminal == "interrupt":
        store.mark_interrupted(run.run_id, detail="test restart")
    else:
        store.fail_run(run.run_id, code="test_lost_result", message="result persistence failed", retryable=True)
    recovered = store.get_tool_call("call-recover-write")
    assert recovered.status == "succeeded"
    assert recovered.output["change_set_id"] == result.output["change_set_id"]
    assert recovered.output["recovered"] is True
    assert recovered.output["applied"] is True
    assert recovered.output["result_revision"] == result.output["result_revision"]
    assert recovered.output["result_fingerprint"] == result.output["result_fingerprint"]
    assert (project / "08_素材库/recovered.md").read_text(encoding="utf-8") == "saved exactly once"
    assert not any(x.root.kind.value == "change_proposal" for x in store.list_activities(run.run_id))
    with database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM change_set").fetchone()[0] == 1


def test_recovered_direct_write_reports_already_rolled_back(tmp_path):
    executor, run, _chapter, database, changes, project = _build_runtime(tmp_path)
    result = asyncio.run(_call(executor, run, "project_write_file",
                               {"relative_path": "08_素材库/undo.md", "content": "temporary"},
                               call_id="call-rollback-write"))
    record = changes.get(result.output["change_set_id"])
    rolled = changes.rollback(record.change_set_id, ChangeSetRollbackRequest(expected_review_revision=record.review_revision))
    store = AgentStore(database)
    store.mark_interrupted(run.run_id, detail="test recovery after rollback")
    recovered = store.get_tool_call("call-rollback-write")
    assert recovered.status == "succeeded"
    assert recovered.output["applied"] is False
    assert recovered.output["status"] == "rolled_back"
    assert recovered.output["result_revision"] == rolled.rollback.result_revision
    assert not (project / "08_素材库/undo.md").exists()


def test_interrupted_command_with_unknown_result_is_not_retryable(tmp_path):
    executor, run, _chapter, database, _cs, _project = _build_runtime(tmp_path)
    store = AgentStore(database)
    args = {"command": "echo no-real-execution"}
    definition = executor._definitions_by_name["computer_run_command"]
    store.begin_tool_call(run.run_id, tool_call_id="call-unknown-command", tool_name="computer_run_command",
                          tool_version=definition.version, arguments=args,
                          request_hash=executor.request_hash("computer_run_command", definition.version, args),
                          idempotency_key="call-unknown-command")
    store.mark_interrupted(run.run_id, detail="Runtime stopped before result persistence")
    result = store.get_tool_call("call-unknown-command")
    assert result.status == "failed"
    assert result.error.error.retryable is False


def test_cancel_during_file_apply_waits_and_recovers_after_early_tool_close(tmp_path, monkeypatch):
    import threading
    executor, run, _chapter, database, changes, project = _build_runtime(tmp_path)
    store = AgentStore(database)
    release, joined = threading.Event(), threading.Event()
    original_apply = changes.apply_direct
    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed_apply(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(20), "test did not release atomic write"
            try:
                return original_apply(*args, **kwargs)
            finally:
                joined.set()

        monkeypatch.setattr(changes, "apply_direct", delayed_apply)
        task = asyncio.create_task(_call(executor, run, "project_write_file",
                                        {"relative_path": "08_素材库/race.md", "content": "committed after stop request"},
                                        call_id="call-cancel-apply"))
        try:
            # Wait for the actual race boundary without occupying a worker just
            # to wait. Project preparation under load is not a three-second SLA.
            await asyncio.wait_for(started.wait(), timeout=15)
            store.cancel_run(run.run_id)  # Closes the ToolCall before the apply commits.
            assert store.get_tool_call("call-cancel-apply").status == "cancelled"
            task.cancel()
            await asyncio.sleep(.03)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert joined.is_set()
            store.mark_cancelled(run.run_id)
            recovered = store.get_tool_call("call-cancel-apply")
            assert recovered.status == "succeeded"
            assert recovered.output["recovered"] is True and recovered.output["applied"] is True
            assert store.get_run(run.run_id).status is RunStatus.CANCELLED
            assert (project / "08_素材库/race.md").read_text(encoding="utf-8") == "committed after stop request"
        finally:
            release.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    asyncio.run(scenario())


def test_cancelled_command_does_not_claim_no_side_effects(tmp_path):
    executor, run, _chapter, database, _cs, _project = _build_runtime(tmp_path)
    store = AgentStore(database)
    arguments = {"command": "echo not-executed-in-test"}
    definition = executor._definitions_by_name["computer_run_command"]
    store.begin_tool_call(run.run_id, tool_call_id="call-cancelled-command", tool_name="computer_run_command",
                          tool_version=definition.version, arguments=arguments,
                          request_hash=executor.request_hash("computer_run_command", definition.version, arguments),
                          idempotency_key="call-cancelled-command")
    store.cancel_run(run.run_id)
    output = store.get_tool_call("call-cancelled-command").output
    assert output["outcome"] == "unknown"
    assert output["may_have_side_effects"] is True
    assert output["retryable"] is False


def test_direct_write_recovery_rejects_a_mismatched_source_request(tmp_path):
    import json
    executor, run, _chapter, database, _cs, _project = _build_runtime(tmp_path)
    result = asyncio.run(_call(executor, run, "project_write_file",
                               {"relative_path": "08_素材库/check-source.md", "content": "one request"},
                               call_id="call-check-source"))
    with database.connection() as connection:
        row = connection.execute("SELECT payload_json FROM change_set WHERE id = ?", (result.output["change_set_id"],)).fetchone()
        payload = json.loads(row["payload_json"])
        payload["source_request_hash"] = "f" * 64
        connection.execute("UPDATE change_set SET payload_json = ? WHERE id = ?", (json.dumps(payload), result.output["change_set_id"]))
    store = AgentStore(database)
    store.mark_interrupted(run.run_id, detail="test mismatched record")
    assert store.get_tool_call("call-check-source").status == "failed"


def test_write_files_multi_one_changeset(tmp_path: Path) -> None:
    """一次 files:[...] 调用建一个多文件 ChangeSet，直接落盘、可一键撤回。"""
    executor, run, chapter, _db, change_sets, project = _build_runtime(
        tmp_path, suffix="-multi"
    )

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {
                "files": [
                    {"relative_path": "08_素材库/灵感一.md", "content": "一。\n"},
                    {"relative_path": "08_素材库/灵感二.md", "content": "二。\n"},
                    {
                        "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                        "content": "多文件改写后的第一章。\n",
                    },
                ],
                "note": "作者让一次建三份",
            },
        )
    )

    assert result.output["operation"] == "written"
    assert result.output["applied"] is True
    assert result.output["changes_count"] == 3
    assert [item["operation"] for item in result.output["files"]] == [
        "created",
        "created",
        "updated",
    ]
    assert (project / "08_素材库" / "灵感一.md").read_text(encoding="utf-8") == "一。\n"
    assert (project / "08_素材库" / "灵感二.md").read_text(encoding="utf-8") == "二。\n"
    assert chapter.read_text(encoding="utf-8") == "多文件改写后的第一章。\n"
    change_set = change_sets.get(result.output["change_set_id"])
    assert change_set.status.value == "applied"
    assert len(change_set.changes) == 3

    rolled_back = change_sets.rollback(
        change_set.change_set_id,
        ChangeSetRollbackRequest(
            expected_review_revision=change_set.review_revision
        ),
    )
    assert rolled_back.status.value == "rolled_back"
    assert not (project / "08_素材库" / "灵感一.md").exists()
    assert not (project / "08_素材库" / "灵感二.md").exists()
    assert chapter.read_text(encoding="utf-8") == "第一行。\n第二行。\n"


def test_write_files_all_unchanged_skips(tmp_path: Path) -> None:
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-unchanged"
    )
    original = chapter.read_text(encoding="utf-8")

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_write_file",
            {"files": [{"relative_path": "05_正文内容/第一卷/CH001_正文.md", "content": original}]},
        )
    )

    assert result.output["applied"] is False
    assert result.output["operation"] == "unchanged"
    assert result.output["files"][0]["operation"] == "unchanged"


def test_write_file_rejects_mixed_shapes(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-shape"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_write_file",
                {
                    "relative_path": "08_素材库/x.md",
                    "content": "x",
                    "files": [{"relative_path": "08_素材库/y.md", "content": "y"}],
                },
            )
        )
    assert excinfo.value.code == "agent_tool_validation_failed"


def test_delete_file_tombstone_and_rollback(tmp_path: Path) -> None:
    """删除走 ChangeSet delete（墓碑+备份，可撤回）；书根里不许出现 .trash 之类的文件夹。"""
    executor, run, chapter, _db, change_sets, project = _build_runtime(
        tmp_path, suffix="-delete"
    )

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_delete_file",
            {"relative_path": "05_正文内容/第一卷/CH001_正文.md"},
        )
    )

    assert result.output["operation"] == "deleted"
    assert result.output["applied"] is True
    assert not chapter.exists()
    assert not (project / ".trash").exists()
    change_set = change_sets.get(result.output["change_set_id"])
    assert change_set.status.value == "applied"
    assert change_set.changes[0].operation.value == "delete"

    rolled_back = change_sets.rollback(
        change_set.change_set_id,
        ChangeSetRollbackRequest(
            expected_review_revision=change_set.review_revision
        ),
    )
    assert rolled_back.status.value == "rolled_back"
    assert chapter.read_text(encoding="utf-8") == "第一行。\n第二行。\n"
    assert list(chapter.parent.glob("*.deleted")) == []


def test_delete_missing_file_fails(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-delete-missing"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_delete_file",
                {"relative_path": "05_正文内容/第一卷/不存在.md"},
            )
        )
    assert excinfo.value.code == "agent_tool_delete_target_missing"


def test_move_file_renames_and_rolls_back(tmp_path: Path) -> None:
    """移动/改名＝目标改名，撤回挪回。"""
    executor, run, chapter, _db, change_sets, project = _build_runtime(
        tmp_path, suffix="-move"
    )

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_move_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "target_relative_path": "05_正文内容/第一卷/第 003 章 试剑.md",
            },
        )
    )

    moved = project / "05_正文内容" / "第一卷" / "第 003 章 试剑.md"
    assert result.output["operation"] == "moved"
    assert result.output["applied"] is True
    assert not chapter.exists()
    assert moved.read_text(encoding="utf-8") == "第一行。\n第二行。\n"
    change_set = change_sets.get(result.output["change_set_id"])
    assert change_set.status.value == "applied"
    assert change_set.changes[0].operation.value == "move"
    assert (
        change_set.changes[0].target_relative_path
        == "05_正文内容/第一卷/第 003 章 试剑.md"
    )

    rolled_back = change_sets.rollback(
        change_set.change_set_id,
        ChangeSetRollbackRequest(
            expected_review_revision=change_set.review_revision
        ),
    )
    assert rolled_back.status.value == "rolled_back"
    assert chapter.read_text(encoding="utf-8") == "第一行。\n第二行。\n"
    assert not moved.exists()


def test_move_same_path_fails(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-move-same"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_move_file",
                {
                    "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                    "target_relative_path": "05_正文内容/第一卷/CH001_正文.md",
                },
            )
        )
    assert excinfo.value.code == "agent_tool_move_same_path"


def test_move_missing_source_fails(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, _project = _build_runtime(
        tmp_path, suffix="-move-missing"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_move_file",
                {
                    "relative_path": "05_正文内容/第一卷/不存在.md",
                    "target_relative_path": "05_正文内容/第一卷/新名字.md",
                },
            )
        )
    assert excinfo.value.code == "agent_tool_move_source_missing"


def test_move_target_exists_fails(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, suffix="-move-exists"
    )
    other = project / "08_素材库" / "已占用.md"
    other.write_text("占位\n", encoding="utf-8")

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_move_file",
                {
                    "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                    "target_relative_path": "08_素材库/已占用.md",
                },
            )
        )
    assert excinfo.value.code == "agent_tool_move_target_exists"
    assert other.read_text(encoding="utf-8") == "占位\n"


def test_mkdir_creates_nested_and_is_idempotent(tmp_path: Path) -> None:
    executor, run, _chapter, database, _cs, project = _build_runtime(
        tmp_path, suffix="-mkdir"
    )
    store = AgentStore(database)

    result = asyncio.run(
        _call(
            executor,
            run,
            "project_mkdir",
            {"relative_path": "08_素材库/导入/批次一"},
        )
    )

    folder = project / "08_素材库" / "导入" / "批次一"
    assert result.output["created"] is True
    assert folder.is_dir()
    # 建文件夹改变项目指纹（目录也算指纹），版本钉子写进结果。
    refreshed = store.get_run(run.run_id)
    binding = database.get_project_binding(refreshed.project_revision.project_id)
    assert result.output["result_revision"] == binding.revision
    assert result.output["result_fingerprint"] == binding.fingerprint

    again = asyncio.run(
        _call(
            executor,
            run,
            "project_mkdir",
            {"relative_path": "08_素材库/导入/批次一"},
            call_id="call-0002",
        )
    )
    assert again.output["created"] is False
    assert again.output["applied"] is False


def test_mkdir_refuses_file_collision(tmp_path: Path) -> None:
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, suffix="-mkdir-collision"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_mkdir",
                {"relative_path": "05_正文内容/第一卷/CH001_正文.md"},
            )
        )
    assert excinfo.value.code == "agent_tool_mkdir_target_exists"
    assert (project / "05_正文内容" / "第一卷" / "CH001_正文.md").is_file()


def test_mkdir_rejects_file_suffix(tmp_path: Path) -> None:
    """文件夹名不带文本文件后缀；要建文件去 project_write_file。"""
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, suffix="-mkdir-suffix"
    )

    with pytest.raises(AgentToolError) as excinfo:
        asyncio.run(
            _call(
                executor,
                run,
                "project_mkdir",
                {"relative_path": "08_素材库/灵感A.md"},
            )
        )
    assert excinfo.value.code == "agent_tool_mkdir_target_exists"
    assert not (project / "08_素材库" / "灵感A.md").exists()


def test_agent_proposal_no_longer_forces_the_prose_gate(
    tmp_path: Path,
) -> None:
    """写前门禁只按请求本身来：Agent 提的修改不再被强制过门禁（2026-09-22 审阅 A1，
    真机 7 条回执拦过「系统：」「无法完成」这类正常句子）。"""
    executor, run, _chapter, _db, _cs, project = _build_runtime(
        tmp_path, chapter_content="系统：宿主请注意。\n无法完成的任务。\n"
    )
    files = ProjectReader((tmp_path / "library",)).inspect(project)["files"]
    chapter_file = next(item for item in files if item["relative_path"].endswith("CH001_正文.md"))
    proposed = asyncio.run(
        _call(
            executor,
            run,
            "change_set_propose",
            {
                "title": "改一句",
                "summary": "把第二行改掉",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter_file["file_id"],
                        "after_content": "系统：宿主请注意。\n稍后重试也无法完成的任务。\n",
                        "rationale": "测试",
                    }
                ],
            },
        )
    )
    assert proposed.change_set is not None
    assert proposed.change_set.gate_required is False
    assert proposed.change_set.ai_smell is None  # 提案不再自动打 AI 味分


def test_edit_file_keeps_crlf_line_endings_and_a_single_hunk(
    tmp_path: Path,
) -> None:
    """Windows 上记事本 / Word 导出的 CRLF 章节只改一个词，整份文件的换行符不能跟着变
    （2026-09-22 审阅 2.4）。"""
    executor, run, chapter, _db, _cs, _project = _build_runtime(
        tmp_path, chapter_content="第一行。\n第二行。\n第三行。\n"
    )
    if b"\r\n" not in chapter.read_bytes():
        # 非 Windows 环境 write_text 不做换行翻译，这个用例只在 CRLF 文件上有意义。
        pytest.skip("fixture wrote LF on this platform")
    result = asyncio.run(
        _call(
            executor,
            run,
            "project_edit_file",
            {
                "relative_path": "05_正文内容/第一卷/CH001_正文.md",
                "find": "第二行。",
                "replace": "改过的第二行。",
            },
        )
    )
    assert result.output["operation"] == "updated"
    assert chapter.read_bytes() == "第一行。\r\n改过的第二行。\r\n第三行。\r\n".encode("utf-8")
    record = _cs.get(result.output["change_set_id"])
    assert sum(len(change.hunks) for change in record.changes) == 1


def test_direct_save_keeps_manually_rejected_hunks_rejected_instead_of_bouncing(
    tmp_path: Path,
) -> None:
    """作者在修改记录里手动拒掉一块后再点「直接保存」：按取舍后的结果落盘，
    不再整批弹回审阅页（2026-09-22 审阅 2.10）。"""
    from zhimeng_runtime.change_set_models import (
        ChangeSetApplyRequest,
        ChangeSetDecisionRequest,
        ChangeSetStatus,
        HunkDecision,
        HunkDecisionUpdate,
    )

    middle = "中间一行。\n" * 12
    executor, run, chapter, _db, change_sets, project = _build_runtime(
        tmp_path, chapter_content="开头一行。\n" + middle + "结尾一行。\n"
    )
    files = ProjectReader((tmp_path / "library",)).inspect(project)["files"]
    chapter_file = next(item for item in files if item["relative_path"].endswith("CH001_正文.md"))
    proposed = asyncio.run(
        _call(
            executor,
            run,
            "change_set_propose",
            {
                "title": "改头改尾",
                "summary": "开头和结尾各改一句",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": chapter_file["file_id"],
                        "after_content": "改过的开头。\n" + middle + "改过的结尾。\n",
                        "rationale": "测试",
                    }
                ],
            },
        )
    )
    record = proposed.change_set
    assert record is not None
    hunks = [hunk for change in record.changes for hunk in change.hunks]
    assert len(hunks) == 2, [hunk.hunk_id for hunk in hunks]
    decided = change_sets.decide(
        record.change_set_id,
        ChangeSetDecisionRequest(
            expected_review_revision=record.review_revision,
            decisions=[HunkDecisionUpdate(hunk_id=hunks[0].hunk_id, decision=HunkDecision.REJECTED)],
        ),
    )
    applied = change_sets.apply_direct(
        record.change_set_id,
        ChangeSetApplyRequest(expected_review_revision=decided.review_revision),
    )
    assert applied.status is ChangeSetStatus.APPLIED
    assert chapter.read_text(encoding="utf-8") == "开头一行。\n" + middle + "改过的结尾。\n"
