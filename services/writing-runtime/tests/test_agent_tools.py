from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import (
    MAX_READ_CHARACTERS,
    READONLY_TOOL_NAMES,
    AgentToolError,
    AgentToolExecution,
    AgentToolExecutor,
    ChangeSetProposeArguments,
    SPILL_THRESHOLD_CHARACTERS,
)
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    RunStatus,
)
from zhimeng_runtime.knowledge_service import KnowledgeService
from zhimeng_runtime.managed_skill_service import ManagedSkillService
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_models import ProjectInspectionResponse
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS
from zhimeng_runtime.search_service import SearchService
from zhimeng_runtime.skill_executor import SkillExecutor


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REAL_PROJECT_AUDIT_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-project-audit"
)
REAL_FAMILY_CREATION_SUITE_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "four-skills-family-creation-suite"
)
PROJECT_AUDIT_SKILL_ID = "novel-creation-suite.project-audit"
FAMILY_CREATION_SUITE_SKILL_ID = "four-skills.family-creation-suite"


def _build_runtime(
    tmp_path: Path,
    *,
    chapter_content: str = "first line\nsecond line\n",
    skill_package: Path | None = None,
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
        title="Tool tests",
        executor=ExecutorIdentity(
            executor_id="executor.tool-tests",
            kind=ExecutorKind.EMBEDDED,
            implementation="tool-tests",
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
    run, _, _ = store.create_run(
        session_id=session.session_id,
        message="Inspect the chapter.",
        expected_project_revision=binding.revision,
        expected_project_fingerprint=binding.fingerprint,
        active_chapter_id="VOL001-CH001",
        idempotency_key="tool-tests-turn-0001",
    )
    store.transition_run(run.run_id, RunStatus.STARTING, detail="start")
    running = store.transition_run(run.run_id, RunStatus.RUNNING, detail="run")
    locks = ProjectLockRegistry()
    change_sets = ChangeSetService(
        reader,
        database,
        tmp_path / "change-sets",
    )
    search_service = SearchService(
        database,
        reader,
        KnowledgeService(database, tmp_path / "source-imports"),
        tmp_path / "project-search",
    )
    skill_executor = None
    if skill_package is not None:
        managed_service = ManagedSkillService(database, skill_package.parent)
        managed_service.register_builtin(skill_package)
        skill_executor = SkillExecutor(managed_service)
    executor = AgentToolExecutor(
        reader,
        database,
        locks,
        change_sets,
        search_service=search_service,
        skill_executor=skill_executor,
    )
    return executor, running, chapter


def test_tool_catalog_is_project_local_and_has_no_apply_or_external_tool(
    tmp_path: Path,
) -> None:
    executor, _, _ = _build_runtime(tmp_path)

    # 3.11（9-27）：读和列只有 list_files / read_file 一套，旧的四个只在执行层认。
    assert [item.name for item in executor.definitions] == [
        "list_files",
        "read_file",
        "project_search",
        "write_file",
        "edit_file",
        "delete_file",
        "move_file",
        "make_folder",
        "undo_change",
        "web_fetch",
        "search_web",
        "ai_smell_score",
        "memory_write",
        "session_search",
        "session_read",
        "plan_read",
        "plan_update",
        "computer_run_command",
        "command_session",
        "change_set_propose",
    ]
    rendered = " ".join(
        item.model_dump_json(by_alias=True) for item in executor.definitions
    ).casefold()
    assert "change_set_apply" not in rendered
    assert "mytemple" not in rendered
    assert "plugin" not in rendered


def test_normal_chat_may_update_any_existing_file_in_the_book(
    tmp_path: Path,
) -> None:
    """聊天提案不再限定「当前章或手选文件」：提案不落盘，作者点头前文件不动，
    改哪份由作者的话决定。只剩正确性核对：文件要在书里、同一批不重复。"""
    executor, _, chapter = _build_runtime(tmp_path)
    project = chapter.parents[2]
    people = project / "03_人物设定" / "人物.md"
    outline = project / "04_剧情大纲" / "全书大纲.md"
    people.write_text("# 人物\n", encoding="utf-8")
    outline.write_text("# 全书大纲\n", encoding="utf-8")

    payload = executor.reader.inspect(project)
    payload["project"]["revision"] = 1
    snapshot = ProjectInspectionResponse.model_validate(payload)
    files_by_path = {item.relative_path: item for item in snapshot.files}
    people_file = files_by_path["03_人物设定/人物.md"]
    outline_file = files_by_path["04_剧情大纲/全书大纲.md"]
    prose_file = files_by_path["05_正文内容/第一卷/CH001_正文.md"]
    narrow_pack = SimpleNamespace(
        manual_file_ids=(people_file.file_id,), active_chapter_id=None
    )

    def _update(file_id: str) -> ChangeSetProposeArguments:
        return ChangeSetProposeArguments.model_validate(
            {
                "title": "改一份资料",
                "summary": "按作者的话改",
                "changes": [
                    {
                        "operation": "update",
                        "file_id": file_id,
                        "after_content": "改后的内容。\n",
                        "rationale": "作者要求",
                    }
                ],
            }
        )

    # 手选的、没手选的、当前章之外的正文——都能提。
    executor._validate_chat_changes(_update(people_file.file_id), snapshot, pack=narrow_pack)
    executor._validate_chat_changes(_update(outline_file.file_id), snapshot, pack=narrow_pack)
    executor._validate_chat_changes(_update(prose_file.file_id), snapshot, pack=narrow_pack)

    # 书里没有的文件不能改。
    with pytest.raises(AgentToolError) as unknown_error:
        executor._validate_chat_changes(
            _update("file-" + "f" * 32), snapshot, pack=narrow_pack
        )
    assert unknown_error.value.code == "writer_change_set_target_invalid"

    # 同一批里同一份文件改两次不行。
    once = _update(people_file.file_id)
    twice = once.model_copy(update={"changes": [once.changes[0], once.changes[0]]})
    with pytest.raises(AgentToolError) as duplicate_error:
        executor._validate_chat_changes(twice, snapshot, pack=narrow_pack)
    assert duplicate_error.value.code == "writer_change_set_target_invalid"


def test_normal_chat_may_delete_any_existing_file_in_the_book(
    tmp_path: Path,
) -> None:
    """「把这份旧草稿删掉」：删除同样不限范围，提案不落盘，作者点头前文件不动。"""
    executor, _, chapter = _build_runtime(tmp_path)
    project = chapter.parents[2]
    people = project / "03_人物设定" / "人物.md"
    outline = project / "04_剧情大纲" / "全书大纲.md"
    people.write_text("# 人物\n", encoding="utf-8")
    outline.write_text("# 全书大纲\n", encoding="utf-8")

    payload = executor.reader.inspect(project)
    payload["project"]["revision"] = 1
    snapshot = ProjectInspectionResponse.model_validate(payload)
    files_by_path = {item.relative_path: item for item in snapshot.files}
    people_file = files_by_path["03_人物设定/人物.md"]
    outline_file = files_by_path["04_剧情大纲/全书大纲.md"]

    delete_people = ChangeSetProposeArguments.model_validate(
        {
            "title": "删掉旧人物稿",
            "summary": "作者要求删掉旧稿",
            "changes": [
                {
                    "operation": "delete",
                    "file_id": people_file.file_id,
                    "rationale": "作者明确要求删除",
                }
            ],
        }
    )
    # 资料包只手选了大纲，删人物稿照样可以提。
    executor._validate_chat_changes(
        delete_people,
        snapshot,
        pack=SimpleNamespace(manual_file_ids=(outline_file.file_id,), active_chapter_id=None),
    )

    # 书里没有的文件删不了。
    missing = delete_people.model_copy(
        update={
            "changes": [
                delete_people.changes[0].model_copy(update={"file_id": "file-" + "e" * 32})
            ]
        }
    )
    with pytest.raises(AgentToolError) as missing_error:
        executor._validate_chat_changes(
            missing,
            snapshot,
            pack=SimpleNamespace(manual_file_ids=(), active_chapter_id=None),
        )
    assert missing_error.value.code == "writer_change_set_target_invalid"


def test_normal_chat_may_create_any_new_file_in_the_book(
    tmp_path: Path,
) -> None:
    """新建不再限「最多两个正文 .md」：一次开三章、顺手补人物卡和章纲都能提。
    只剩正确性：路径不能空、同一批不重复、不能和已有文件撞名（撞名该用 update）。"""
    executor, _, chapter = _build_runtime(tmp_path)
    project = chapter.parents[2]

    payload = executor.reader.inspect(project)
    payload["project"]["revision"] = 1
    snapshot = ProjectInspectionResponse.model_validate(payload)
    pack = SimpleNamespace(manual_file_ids=(), active_chapter_id="VOL001-CH001")

    new_chapter = ChangeSetProposeArguments.model_validate(
        {
            "title": "写第 2 章",
            "summary": "作者要求接着写下一章",
            "changes": [
                {
                    "operation": "create",
                    "relative_path": "05_正文内容/第一卷/CH002_正文.md",
                    "after_content": "# 第二章\n\n正文。\n",
                    "rationale": "作者要求写新章",
                }
            ],
        }
    )
    executor._validate_chat_changes(new_chapter, snapshot, pack=pack)

    mixed = ChangeSetProposeArguments.model_validate(
        {
            "title": "补第 1 章结尾再写第 2 章",
            "summary": "更新当前章并新建下一章",
            "changes": [
                {
                    "operation": "update",
                    "file_id": {item.relative_path: item for item in snapshot.files}[
                        "05_正文内容/第一卷/CH001_正文.md"
                    ].file_id,
                    "after_content": "first line\nsecond line\n补一个结尾。\n",
                    "rationale": "作者要求",
                },
                new_chapter.changes[0],
            ],
        }
    )
    executor._validate_chat_changes(mixed, snapshot, pack=pack)

    def _variant(**update: object) -> ChangeSetProposeArguments:
        return new_chapter.model_copy(
            update={"changes": [new_chapter.changes[0].model_copy(update=update)]}
        )

    # 以前被拒、现在都能提：人物卡、章纲控制卡、空文件、一次三章。
    executor._validate_chat_changes(
        _variant(relative_path="03_人物设定/柳青烟.md"), snapshot, pack=pack
    )
    executor._validate_chat_changes(
        _variant(relative_path="05_正文内容/第一卷/章纲/CH002_控制卡.md"), snapshot, pack=pack
    )
    executor._validate_chat_changes(_variant(after_content="  \n"), snapshot, pack=pack)
    three_chapters = new_chapter.model_copy(
        update={
            "changes": [
                new_chapter.changes[0],
                new_chapter.changes[0].model_copy(
                    update={"relative_path": "05_正文内容/第一卷/CH003_正文.md"}
                ),
                new_chapter.changes[0].model_copy(
                    update={"relative_path": "05_正文内容/第一卷/CH004_正文.md"}
                ),
            ]
        }
    )
    executor._validate_chat_changes(three_chapters, snapshot, pack=pack)

    # 撞名：已有文件要用 update，不能再 create 一份。
    with pytest.raises(AgentToolError) as overwrite_error:
        executor._validate_chat_changes(
            _variant(relative_path="05_正文内容/第一卷/CH001_正文.md"), snapshot, pack=pack
        )
    assert overwrite_error.value.code == "writer_change_set_target_invalid"

    # 同一批里同一路径建两次不行。
    same_twice = new_chapter.model_copy(
        update={"changes": [new_chapter.changes[0], new_chapter.changes[0]]}
    )
    with pytest.raises(AgentToolError) as duplicate_error:
        executor._validate_chat_changes(same_twice, snapshot, pack=pack)
    assert duplicate_error.value.code == "writer_change_set_target_invalid"


def test_list_and_read_use_stable_ids_without_exposing_absolute_paths(
    tmp_path: Path,
) -> None:
    executor, run, _ = _build_runtime(tmp_path)

    async def scenario() -> None:
        list_args = {"query": "CH001", "limit": 10}
        listed = await executor.execute(
            run,
            tool_call_id="call_list_tool_0001",
            tool_name="project_list_files",
            arguments=list_args,
            request_hash=executor.request_hash("project_list_files", "1", list_args),
            cancel_event=asyncio.Event(),
        )
        assert listed.output["matched_count"] == 1
        assert "absolute_path" not in str(listed.output)
        file_id = listed.output["files"][0]["file_id"]

        read_args = {"file_id": file_id, "start_line": 2, "max_lines": 1}
        read = await executor.execute(
            run,
            tool_call_id="call_read_tool_0002",
            tool_name="project_read_file",
            arguments=read_args,
            request_hash=executor.request_hash("project_read_file", "1", read_args),
            cancel_event=asyncio.Event(),
        )
        assert str(read.output["content"]).splitlines() == ["second line"]
        assert read.output["start_line"] == 2
        assert read.output["end_line"] == 2
        assert "absolute_path" not in str(read.output)

    asyncio.run(scenario())


def test_search_returns_project_file_ids_without_exposing_absolute_paths(
    tmp_path: Path,
) -> None:
    executor, run, chapter = _build_runtime(
        tmp_path,
        chapter_content="沈青云推开山门。\n柳青烟发现青云玉不见了。\n",
    )

    async def scenario() -> None:
        arguments = {
            "query": "青云玉",
            "source_types": ["project_file"],
            "limit": 10,
        }
        result = await executor.execute(
            run,
            tool_call_id="call_search_tool_0001",
            tool_name="project_search",
            arguments=arguments,
            request_hash=executor.request_hash("project_search", "1", arguments),
            cancel_event=asyncio.Event(),
        )

        assert result.output["schema"] == "zhimeng.tool-result.project-search.v1"
        assert result.output["hit_count"] == 1
        hit = result.output["hits"][0]
        assert hit["source_type"] == "project_file"
        assert hit["file_id"].startswith("file-")
        assert hit["source_id"] == hit["file_id"]
        assert hit["relative_path"].endswith("CH001_正文.md")
        assert "青云玉" in hit["snippet"]
        rendered = json.dumps(result.output, ensure_ascii=False)
        assert str(chapter.parents[2]) not in rendered
        assert "absolute_path" not in rendered

    asyncio.run(scenario())


def test_long_single_line_can_continue_with_character_offset(tmp_path: Path) -> None:
    long_line = "x" * (MAX_READ_CHARACTERS + 22_000)
    executor, run, _ = _build_runtime(
        tmp_path,
        chapter_content=f"{long_line}\nsecond line\n",
    )

    async def scenario() -> None:
        list_args = {"query": "CH001", "limit": 1}
        listed = await executor.execute(
            run,
            tool_call_id="call_long_list_0001",
            tool_name="project_list_files",
            arguments=list_args,
            request_hash=executor.request_hash("project_list_files", "1", list_args),
            cancel_event=asyncio.Event(),
        )
        file_id = listed.output["files"][0]["file_id"]

        first_args = {"file_id": file_id, "start_line": 1, "max_lines": 1}
        first = await executor.execute(
            run,
            tool_call_id="call_long_read_0002",
            tool_name="project_read_file",
            arguments=first_args,
            request_hash=executor.request_hash("project_read_file", "1", first_args),
            cancel_event=asyncio.Event(),
        )
        assert first.output["truncated"] is True
        assert isinstance(first.output["next_offset"], int)

        second_args = {
            "file_id": file_id,
            "start_offset": first.output["next_offset"],
            "max_lines": 1,
        }
        second = await executor.execute(
            run,
            tool_call_id="call_long_read_0003",
            tool_name="project_read_file",
            arguments=second_args,
            request_hash=executor.request_hash("project_read_file", "1", second_args),
            cancel_event=asyncio.Event(),
        )
        reconstructed = f"{first.output['content']}{second.output['content']}".rstrip(
            "\r\n"
        )
        assert reconstructed == long_line
        assert isinstance(second.output["next_offset"], int)

        third_args = {
            "file_id": file_id,
            "start_offset": second.output["next_offset"],
            "max_lines": 1,
        }
        third = await executor.execute(
            run,
            tool_call_id="call_long_read_0004",
            tool_name="project_read_file",
            arguments=third_args,
            request_hash=executor.request_hash("project_read_file", "1", third_args),
            cancel_event=asyncio.Event(),
        )
        assert str(third.output["content"]).splitlines() == ["second line"]

    asyncio.run(scenario())


def test_unknown_tool_and_stale_project_fail_closed(tmp_path: Path) -> None:
    executor, run, chapter = _build_runtime(tmp_path)

    async def scenario() -> None:
        unknown_args: dict[str, object] = {}
        with pytest.raises(AgentToolError) as unknown:
            await executor.execute(
                run,
                tool_call_id="call_unknown_tool_0001",
                tool_name="shell",
                arguments=unknown_args,
                request_hash=executor.request_hash("shell", "1", unknown_args),
                cancel_event=asyncio.Event(),
            )
        assert unknown.value.code == "agent_tool_not_allowed"

        chapter.write_text("changed outside the Run\n", encoding="utf-8")
        list_args = {"limit": 10}
        with pytest.raises(AgentToolError) as stale:
            await executor.execute(
                run,
                tool_call_id="call_stale_tool_0002",
                tool_name="project_list_files",
                arguments=list_args,
                request_hash=executor.request_hash(
                    "project_list_files",
                    "1",
                    list_args,
                ),
                cancel_event=asyncio.Event(),
            )
        assert stale.value.code == "agent_tool_project_changed"
        assert stale.value.retryable is True

        search_args = {"query": "changed", "limit": 10}
        with pytest.raises(AgentToolError) as stale_search:
            await executor.execute(
                run,
                tool_call_id="call_stale_search_0003",
                tool_name="project_search",
                arguments=search_args,
                request_hash=executor.request_hash(
                    "project_search",
                    "1",
                    search_args,
                ),
                cancel_event=asyncio.Event(),
            )
        assert stale_search.value.code == "agent_tool_project_changed"
        assert stale_search.value.retryable is True

    asyncio.run(scenario())


def _write_failing_project_audit_package(
    tmp_path: Path,
    *,
    script: str = "print('not-json')\n",
) -> Path:
    package = tmp_path / "managed-skills" / "project-audit-failure"
    package.mkdir(parents=True)
    (package / "run.py").write_text(script, encoding="utf-8")
    (package / "PROVENANCE.md").write_text("Test fixture.\n", encoding="utf-8")
    manifest = {
        "schema": "zhimeng.skill-manifest.v1",
        "id": PROJECT_AUDIT_SKILL_ID,
        "display_name": "Failing project audit",
        "description": "Exercise the durable failure receipt path.",
        "kind": "deterministic_tool",
        "version": "2026.07.20-m3a.failure-test",
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": True,
        },
        "entrypoint": "run.py",
        "approval": "none",
        "permissions": {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        },
        "timeout_seconds": 5,
        "exit_codes": {"0": "success", "1": "domain_blocked"},
        "artifacts": ["project_audit"],
        "input_schema": (
            "https://zhimeng.local/contracts/"
            "creation-project-audit-request.schema.json"
        ),
        "output_schema": (
            "https://zhimeng.local/contracts/"
            "creation-project-audit-result.schema.json"
        ),
    }
    (package / "skill.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return package


def _write_family_creation_suite_package(tmp_path: Path) -> Path:
    package = tmp_path / "managed-skills" / "family-creation-suite"
    package.mkdir(parents=True)
    (package / "run.py").write_text(
        """import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
result = {
    \"schema\": \"zhimeng.four-skills-family-step-result.v1\",
    \"family\": request[\"family\"],
    \"batch_id\": request[\"batch_id\"],
    \"probe_id\": request[\"probe_id\"],
    \"executed\": True,
    \"writes_project_truth\": False,
}
artifact = {
    **result,
    \"adapter_status\": \"succeeded\",
    \"detail\": \"family creation-suite test completed\",
    \"tool\": {\"name\": \"test-family-cli\", \"status\": \"succeeded\"},
}
Path(\"family-step-result.json\").write_text(
    json.dumps(artifact, separators=(\",\", \":\")),
    encoding=\"utf-8\",
)
print(json.dumps({
    \"health\": \"healthy\",
    \"summary\": \"family creation-suite test completed\",
    \"result\": result,
    \"artifacts\": [{
        \"artifact_type\": \"four_skills_family_step\",
        \"relative_path\": \"family-step-result.json\",
        \"payload\": artifact,
    }],
}, separators=(\",\", \":\")))
""",
        encoding="utf-8",
    )
    (package / "PROVENANCE.md").write_text("Test fixture.\n", encoding="utf-8")
    manifest = {
        "schema": "zhimeng.skill-manifest.v1",
        "id": FAMILY_CREATION_SUITE_SKILL_ID,
        "display_name": "Family creation-suite test",
        "description": "Exercise the real family adapter Tool boundary.",
        "kind": "deterministic_tool",
        "version": "2026.08.07-agent-family-test",
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": True,
        },
        "entrypoint": "run.py",
        "approval": "none",
        "permissions": {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        },
        "timeout_seconds": 5,
        "exit_codes": {"0": "success", "1": "domain_blocked"},
        "artifacts": ["four_skills_family_step"],
        "input_schema": (
            "https://zhimeng.local/contracts/"
            "four-skills-family-step-request.schema.json"
        ),
        "output_schema": (
            "https://zhimeng.local/contracts/"
            "four-skills-family-step-result.schema.json"
        ),
    }
    (package / "skill.manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return package


def _write_fake_ai_smell_cli(tmp_path: Path) -> tuple[Path, Path]:
    script = tmp_path / "fake-detect-ai-smell.py"
    script.write_text(
        """import json
import sys
from pathlib import Path

output = Path(sys.argv[sys.argv.index("--output") + 1])
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({
    "content_hash": "a" * 64,
    "overall_score": {"score": 4.2, "level": "low"},
}), encoding="utf-8")
""",
        encoding="utf-8",
    )
    library = tmp_path / "fake-distillation-library"
    library.mkdir()
    return script, library


def _project_identity(executor: AgentToolExecutor, chapter: Path) -> tuple[int, str]:
    project = chapter.parents[2]
    inspected = executor.reader.inspect(project)["project"]
    binding = executor.store.get_project_binding(str(inspected["project_id"]))
    assert binding is not None
    assert binding.fingerprint == inspected["fingerprint"]
    return binding.revision, binding.fingerprint


def test_real_managed_project_audit_is_isolated_and_persists_only_receipts(
    tmp_path: Path,
) -> None:
    executor, run, chapter = _build_runtime(
        tmp_path,
        skill_package=REAL_PROJECT_AUDIT_PACKAGE,
    )
    assert [item.name for item in executor.definitions][-1] == "skill_execute"
    before_identity = _project_identity(executor, chapter)
    before_bytes = chapter.read_bytes()
    before_sha256 = hashlib.sha256(before_bytes).hexdigest()

    async def scenario():
        arguments = {
            "skill_id": PROJECT_AUDIT_SKILL_ID,
            "input": {"schema": "zhimeng.creation-project-audit-request.v1"},
        }
        return await executor.execute(
            run,
            tool_call_id="call_skill_audit_0001",
            tool_name="skill_execute",
            arguments=arguments,
            request_hash=executor.request_hash("skill_execute", "2", arguments),
            cancel_event=asyncio.Event(),
        )

    result = asyncio.run(scenario())

    assert result.output["schema"] == "zhimeng.tool-result.skill-execute.v1"
    assert result.output["project_unchanged"] is True
    assert result.output["writes_project_truth"] is False
    assert result.output["execution_status"] == "succeeded"
    assert result.output["domain_status"] in {"healthy", "degraded", "blocked"}
    assert result.skill_call is not None
    assert result.skill_call.status.value == "succeeded"
    assert result.skill_call.output["receipt"]["package_fingerprint"]
    assert [artifact.artifact_type for artifact in result.artifacts] == [
        "skill_execution_receipt",
        "project_audit",
    ]
    assert all(
        artifact.uri.startswith("runtime://skill-runs/")
        for artifact in result.artifacts
    )

    artifact_files = sorted(
        path
        for path in (tmp_path / "skill-artifacts").rglob("*.json")
        if path.is_file()
    )
    assert [path.name for path in artifact_files] == [
        "project-audit.json",
        "skill-execution-receipt.json",
    ]
    persisted = {path.name: path.read_bytes() for path in artifact_files}
    receipt = json.loads(persisted["skill-execution-receipt.json"])
    audit = json.loads(persisted["project-audit.json"])
    assert receipt["receipt_id"] == result.output["receipt_id"]
    assert audit["project"] == "bound_project_snapshot"
    assert hashlib.sha256(chapter.read_bytes()).hexdigest() == before_sha256
    assert chapter.read_bytes() == before_bytes
    assert _project_identity(executor, chapter) == before_identity
    assert list((tmp_path / "skill-runs").iterdir()) == []
    assert not any(
        "CH001" in path.name for path in (tmp_path / "skill-artifacts").rglob("*")
    )


def test_agent_can_call_a_four_skills_family_adapter_without_writing_project_truth(
    tmp_path: Path,
) -> None:
    executor, run, chapter = _build_runtime(
        tmp_path,
        skill_package=_write_family_creation_suite_package(tmp_path),
    )
    before_identity = _project_identity(executor, chapter)
    before_bytes = chapter.read_bytes()
    arguments = {
        "skill_id": FAMILY_CREATION_SUITE_SKILL_ID,
        "input": {
            "schema": "zhimeng.four-skills-family-step-request.v1",
            "family": "creation_suite",
            "batch_id": "batch-agent-0001",
            "probe_id": "probe-agent-0001",
        },
    }

    async def scenario():
        return await executor.execute(
            run,
            tool_call_id="call_family_creation_0001",
            tool_name="skill_execute",
            arguments=arguments,
            request_hash=executor.request_hash("skill_execute", "2", arguments),
            cancel_event=asyncio.Event(),
        )

    result = asyncio.run(scenario())

    assert result.output["execution_status"] == "succeeded"
    assert result.output["project_unchanged"] is True
    assert result.output["writes_project_truth"] is False
    assert result.output["family_step"] == {
        "schema": "zhimeng.four-skills-family-step-result.v1",
        "family": "creation_suite",
        "batch_id": "batch-agent-0001",
        "probe_id": "probe-agent-0001",
        "executed": True,
        "writes_project_truth": False,
    }
    assert result.skill_call is not None
    assert result.skill_call.skill_id == FAMILY_CREATION_SUITE_SKILL_ID
    assert result.skill_call.status.value == "succeeded"
    assert [artifact.artifact_type for artifact in result.artifacts] == [
        "skill_execution_receipt",
        "four_skills_family_step",
    ]
    assert chapter.read_bytes() == before_bytes
    assert _project_identity(executor, chapter) == before_identity
    assert list((tmp_path / "skill-runs").iterdir()) == []


def test_agent_executes_real_family_adapter_and_preserves_project_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script, library = _write_fake_ai_smell_cli(tmp_path)
    monkeypatch.delenv("ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT", raising=False)
    monkeypatch.setenv("ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT", str(script))
    monkeypatch.setenv("ZHIMENG_FOUR_SKILLS_CREATION_LIBRARY", str(library))
    executor, run, chapter = _build_runtime(
        tmp_path,
        skill_package=REAL_FAMILY_CREATION_SUITE_PACKAGE,
    )
    before_identity = _project_identity(executor, chapter)
    before_bytes = chapter.read_bytes()
    arguments = {
        "skill_id": FAMILY_CREATION_SUITE_SKILL_ID,
        "input": {
            "schema": "zhimeng.four-skills-family-step-request.v1",
            "family": "creation_suite",
            "batch_id": "batch-real-agent-0001",
            "probe_id": "probe-real-agent-0001",
        },
    }

    async def scenario():
        return await executor.execute(
            run,
            tool_call_id="call_real_family_creation_0001",
            tool_name="skill_execute",
            arguments=arguments,
            request_hash=executor.request_hash("skill_execute", "2", arguments),
            cancel_event=asyncio.Event(),
        )

    result = asyncio.run(scenario())

    assert result.output["family_step"]["family"] == "creation_suite"
    assert result.output["writes_project_truth"] is False
    assert result.output["project_unchanged"] is True
    assert chapter.read_bytes() == before_bytes
    assert _project_identity(executor, chapter) == before_identity
    persisted = list(
        (tmp_path / "skill-artifacts").rglob("family-step-result.json")
    )
    assert len(persisted) == 1
    artifact = json.loads(persisted[0].read_text(encoding="utf-8"))
    assert artifact["adapter_status"] == "succeeded"
    assert artifact["tool"]["source"] == "configured"
    assert artifact["tool"]["score"] == 4.2
    assert list((tmp_path / "skill-runs").iterdir()) == []


def test_skill_failure_keeps_receipt_and_cleans_snapshot_staging(
    tmp_path: Path,
) -> None:
    package = _write_failing_project_audit_package(tmp_path)
    executor, run, chapter = _build_runtime(
        tmp_path,
        skill_package=package,
    )
    before_identity = _project_identity(executor, chapter)
    arguments = {
        "skill_id": PROJECT_AUDIT_SKILL_ID,
        "input": {"schema": "zhimeng.creation-project-audit-request.v1"},
    }

    async def scenario() -> None:
        with pytest.raises(AgentToolError) as failure:
            await executor.execute(
                run,
                tool_call_id="call_skill_failure_0001",
                tool_name="skill_execute",
                arguments=arguments,
                request_hash=executor.request_hash("skill_execute", "2", arguments),
                cancel_event=asyncio.Event(),
            )
        assert failure.value.code == "agent_skill_invalid_skill_output"
        assert failure.value.skill_call is not None
        assert failure.value.skill_call.status.value == "failed"
        receipt = failure.value.skill_call.output["receipt"]
        assert receipt["execution_status"] == "failed"
        assert receipt["error"]["code"] == "invalid_skill_output"
        assert [item.artifact_type for item in failure.value.artifacts] == [
            "skill_execution_receipt"
        ]

    asyncio.run(scenario())

    receipt_files = list(
        (tmp_path / "skill-artifacts").rglob("skill-execution-receipt.json")
    )
    assert len(receipt_files) == 1
    assert json.loads(receipt_files[0].read_text(encoding="utf-8"))[
        "execution_status"
    ] == "failed"
    assert list((tmp_path / "skill-runs").iterdir()) == []
    assert _project_identity(executor, chapter) == before_identity


def test_skill_cancellation_keeps_receipt_and_cleans_snapshot_staging(
    tmp_path: Path,
) -> None:
    package = _write_failing_project_audit_package(
        tmp_path,
        script="import time\ntime.sleep(30)\n",
    )
    executor, run, chapter = _build_runtime(
        tmp_path,
        skill_package=package,
    )
    before_identity = _project_identity(executor, chapter)
    arguments = {
        "skill_id": PROJECT_AUDIT_SKILL_ID,
        "input": {"schema": "zhimeng.creation-project-audit-request.v1"},
    }

    async def scenario() -> AgentToolError:
        cancel_event = asyncio.Event()
        task = asyncio.create_task(
            executor.execute(
                run,
                tool_call_id="call_skill_cancel_0001",
                tool_name="skill_execute",
                arguments=arguments,
                request_hash=executor.request_hash("skill_execute", "2", arguments),
                cancel_event=cancel_event,
            )
        )
        await asyncio.sleep(1.0)
        cancel_event.set()
        with pytest.raises(AgentToolError) as cancelled:
            await task
        return cancelled.value

    cancelled = asyncio.run(scenario())

    assert cancelled.status == "cancelled"
    assert cancelled.code == "agent_skill_cancelled"
    assert cancelled.retryable is False
    assert cancelled.skill_call is not None
    assert cancelled.skill_call.status.value == "cancelled"
    receipt = cancelled.skill_call.output["receipt"]
    assert receipt["execution_status"] == "cancelled"
    assert receipt["domain_status"] is None
    assert receipt["error"]["code"] == "cancelled"
    assert [item.artifact_type for item in cancelled.artifacts] == [
        "skill_execution_receipt"
    ]
    receipt_files = list(
        (tmp_path / "skill-artifacts").rglob("skill-execution-receipt.json")
    )
    assert len(receipt_files) == 1
    assert list((tmp_path / "skill-runs").iterdir()) == []
    assert _project_identity(executor, chapter) == before_identity


def test_skill_execute_rejects_unmanaged_ids_and_extra_arguments(
    tmp_path: Path,
) -> None:
    executor, run, _ = _build_runtime(
        tmp_path,
        skill_package=REAL_PROJECT_AUDIT_PACKAGE,
    )

    async def scenario() -> None:
        for arguments in (
            {"skill_id": "novel-distillation", "input": {}},
            {
                "skill_id": FAMILY_CREATION_SUITE_SKILL_ID,
                "input": {
                    "schema": "zhimeng.four-skills-family-step-request.v1",
                    "family": "distillation",
                    "batch_id": "batch-agent-0001",
                    "probe_id": "probe-agent-0001",
                },
            },
            {
                "skill_id": PROJECT_AUDIT_SKILL_ID,
                "input": {},
                "command": "anything",
            },
        ):
            with pytest.raises(AgentToolError) as rejected:
                await executor.execute(
                    run,
                    tool_call_id="call_skill_rejected_0001",
                    tool_name="skill_execute",
                    arguments=arguments,
                    request_hash=executor.request_hash(
                        "skill_execute",
                        "2",
                        arguments,
                    ),
                    cancel_event=asyncio.Event(),
                )
            assert rejected.value.code == "agent_tool_validation_failed"

    asyncio.run(scenario())


def test_tool_call_times_out_with_structured_error(tmp_path: Path) -> None:
    """挂起的工具调用按表超时：返回结构化 agent_tool_timeout，由模型自决重试。"""
    import asyncio as _asyncio

    executor, running, _ = _build_runtime(tmp_path)
    run = SimpleNamespace(
        id="run_timeout_test",
        project_id=running,
        project_revision=1,
        project_fingerprint="a" * 64,
    )

    async def scenario() -> None:
        executor._tool_timeout_seconds["project_list_files"] = 0.05
        cancel_event = _asyncio.Event()
        started = _asyncio.Event()

        async def hang(*_args, **_kwargs):
            started.set()
            await _asyncio.sleep(5)

        executor._dispatch = hang  # type: ignore[method-assign]
        with pytest.raises(AgentToolError) as exc_info:
            await executor.execute(
                run,
                tool_call_id="call_timeout_0001",
                tool_name="project_list_files",
                arguments={},
                request_hash=executor.request_hash("project_list_files", "1", {}),
                cancel_event=cancel_event,
            )
        assert exc_info.value.code == "agent_tool_timeout"
        assert "timed out" in str(exc_info.value)
        cancel_event.set()

    _asyncio.run(scenario())


def test_large_tool_output_spills_to_disk_and_replaces_with_preview(
    tmp_path: Path,
) -> None:
    executor, _, _ = _build_runtime(tmp_path)
    big_output = {
        "hits": [{"chapter": f"CH{i:03d}", "snippet": "上下文片段" * 40} for i in range(400)],
    }
    execution = AgentToolExecution(output=big_output)

    spilled = executor._spill_large_output(
        execution,
        tool_call_id="call_01spill0000000000000000aa",
        tool_name="project_search",
    )

    assert spilled is not execution
    # frozen dataclass 语义：原对象不被原地改动，附属载荷原样保留。
    assert execution.output == big_output
    assert spilled.change_set is None
    assert spilled.output["schema"] == "zhimeng.tool-result.spilled.v1"
    assert spilled.output["full_size_bytes"] >= SPILL_THRESHOLD_CHARACTERS
    spill_path = Path(spilled.output["spill_path"])
    assert spill_path.parent == tmp_path / "spill"
    assert json.loads(spill_path.read_text(encoding="utf-8")) == big_output
    assert "全文存在" in spilled.output["preview"]
    assert str(spill_path) in spilled.output["preview"]
    # 读工具 9-26 起哪里都能读（9-27 起只有 read_file 一个）：预览照实说可以用 read_file 分段读回全文。
    assert "read_file 可以分段读" in spilled.output["preview"]
    assert "project_read_file" not in spilled.output["preview"]
    assert "computer_read_file" not in spilled.output["preview"]


def test_spill_skips_small_outputs_reads_and_change_set_propose(
    tmp_path: Path,
) -> None:
    executor, _, _ = _build_runtime(tmp_path)

    small = AgentToolExecution(output={"ok": True})
    assert (
        executor._spill_large_output(
            small, tool_call_id="call_small", tool_name="project_search"
        )
        is small
    )

    big_text = "x" * (SPILL_THRESHOLD_CHARACTERS * 2)
    read = AgentToolExecution(output={"content": big_text})
    assert (
        executor._spill_large_output(
            read, tool_call_id="call_read", tool_name="project_read_file"
        )
        is read
    )

    # change_set_propose 的结果模型必须完整读：截断会卡死候选提交。
    propose = AgentToolExecution(output={"content": big_text})
    assert (
        executor._spill_large_output(
            propose, tool_call_id="call_propose", tool_name="change_set_propose"
        )
        is propose
    )


def test_execute_path_spills_large_tool_output_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从 execute() 进去的完整链路：dispatch → spill → 返回信封。"""
    import zhimeng_runtime.agent_tools as agent_tools_module

    executor, run, _ = _build_runtime(tmp_path)
    monkeypatch.setattr(agent_tools_module, "SPILL_THRESHOLD_CHARACTERS", 128)

    async def scenario() -> None:
        list_args = {"query": "CH001", "limit": 10}
        listed = await executor.execute(
            run,
            tool_call_id="call_spill_e2e_0001",
            tool_name="project_list_files",
            arguments=list_args,
            request_hash=executor.request_hash("project_list_files", "1", list_args),
            cancel_event=asyncio.Event(),
        )
        assert listed.output["schema"] == "zhimeng.tool-result.spilled.v1"
        spill_path = Path(listed.output["spill_path"])
        assert spill_path.parent == tmp_path / "spill"
        archived = json.loads(spill_path.read_text(encoding="utf-8"))
        assert archived["matched_count"] == 1
        assert "全文存在" in listed.output["preview"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# web_fetch：httpx 取页面剥正文，超长截头尾；请求只带 URL，不捎带本地内容。

import http.server
import threading


class _WebPageHandler(http.server.BaseHTTPRequestHandler):
    captured: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).captured.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }
        )
        if self.path.startswith("/binary"):
            body = b"\x89PNG\r\n\x1a\n"
            content_type = "image/png"
        elif self.path.startswith("/plain"):
            body = "纯文本第一段。\n\n第二段。".encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif self.path.startswith("/long"):
            # 正文约 27k 个 ASCII 字符：超过取回模型的 24k 头尾截取，
            # 又小于 32KB 落盘阈值（阈值按 UTF-8 字节算，中文一个字占三个），
            # 模型拿到的是头尾预览而不是 spill 定位符。
            filler = "<p>" + "long paragraph line. " * 1300 + "</p>"
            body = (
                "<html><head><title>Long page</title></head><body>" + filler + "</body></html>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        else:
            body = (
                "<html><head><title>示例页</title>"
                "<style>body{color:red}</style></head>"
                "<body><h1>示例标题</h1><script>alert(1)</script>"
                "<p>第一段 &amp; 实体。</p><div>第二段。</div></body></html>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def web_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _WebPageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _WebPageHandler.captured = []
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def _executor(tmp_path: Path) -> AgentToolExecutor:
    executor, *_rest = _build_runtime(tmp_path)
    return executor


def test_web_fetch_strips_html_to_text(tmp_path: Path, web_server) -> None:
    executor = _executor(tmp_path)

    execution = asyncio.run(_call_tool(executor, "web_fetch", {"url": f"{web_server}/"}))

    output = execution.output
    assert output["schema"] == "zhimeng.tool-result.web-fetch.v1"
    assert output["status_code"] == 200
    assert output["title"] == "示例页"
    content = output["content"]
    assert "示例标题" in content
    assert "第一段 & 实体。" in content
    assert "第二段。" in content
    assert "alert" not in content
    assert "color:red" not in content
    assert output["truncated"] is False
    # 请求只带 URL 与通用头：不带 cookie、不带授权、不带任何本地路径。
    sent = _WebPageHandler.captured[0]["headers"]
    assert "cookie" not in sent
    assert "authorization" not in sent
    assert "zhimeng-local-workbench" in sent.get("user-agent", "")
    assert str(tmp_path) not in json.dumps(sent, ensure_ascii=False)


def test_web_fetch_plain_text(tmp_path: Path, web_server) -> None:
    executor = _executor(tmp_path)

    execution = asyncio.run(_call_tool(executor, "web_fetch", {"url": f"{web_server}/plain"}))

    assert execution.output["content"] == "纯文本第一段。\n\n第二段。"
    assert execution.output["media_type"] == "text/plain"
    assert execution.output["title"] is None


def test_web_fetch_rejects_non_text(tmp_path: Path, web_server) -> None:
    executor = _executor(tmp_path)

    error = _expect_tool_error(executor, "web_fetch", {"url": f"{web_server}/binary"})

    assert error.code == "agent_tool_web_not_text"


def test_web_fetch_rejects_non_http_scheme(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    error = _expect_tool_error(executor, "web_fetch", {"url": "ftp://example.com/x"})

    assert error.code == "agent_tool_web_url_invalid"


def test_web_fetch_truncates_head_and_tail(tmp_path: Path, web_server) -> None:
    executor = _executor(tmp_path)

    execution = asyncio.run(_call_tool(executor, "web_fetch", {"url": f"{web_server}/long"}))

    output = execution.output
    assert output["truncated"] is True
    assert "…（中间省略" in output["content"]
    assert len(output["content"]) < 30_000


async def _call_tool(executor: AgentToolExecutor, tool_name: str, arguments: dict):
    definition = executor._definitions_by_name[tool_name]
    request_hash = executor.request_hash(tool_name, definition.version, arguments)
    return await executor.execute(
        SimpleNamespace(run_id=f"run-{tool_name}"),
        tool_call_id=f"call-{tool_name}-0001",
        tool_name=tool_name,
        arguments=arguments,
        request_hash=request_hash,
        cancel_event=asyncio.Event(),
    )


def _expect_tool_error(executor, tool_name, arguments) -> AgentToolError:
    with pytest.raises(AgentToolError) as info:
        asyncio.run(_call_tool(executor, tool_name, arguments))
    return info.value


# plan_update：工作计划只记活动流（界面渲染成可勾清单），不落任何文件。


def test_plan_update_echoes_items_and_writes_nothing(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    execution = asyncio.run(
        _call_tool(
            executor,
            "plan_update",
            {
                "items": [
                    {"content": "读第 3 章", "status": "completed"},
                    {"content": "改第 4 章开头", "status": "in_progress"},
                    {"content": "通读检查"},
                ],
            },
        )
    )

    assert execution.output["schema"] == "zhimeng.tool-result.plan-update.v1"
    # 没写 status 的那条按 pending 记；内容原样带回给界面渲染。
    assert execution.output["items"] == [
        {"content": "读第 3 章", "status": "completed"},
        {"content": "改第 4 章开头", "status": "in_progress"},
        {"content": "通读检查", "status": "pending"},
    ]
    assert execution.change_set is None


def test_plan_update_rejects_empty_items_and_unknown_status(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    error = _expect_tool_error(executor, "plan_update", {"items": []})
    assert error.code == "agent_tool_validation_failed"
    error = _expect_tool_error(
        executor, "plan_update", {"items": [{"content": "做一件事", "status": "doing"}]}
    )
    assert error.code == "agent_tool_validation_failed"
    error = _expect_tool_error(executor, "plan_update", {"items": [{"content": "  "}]})
    assert error.code == "agent_tool_validation_failed"


def test_plan_reads_are_parallel_but_updates_preserve_order(tmp_path: Path) -> None:
    executor = _executor(tmp_path)

    # An update must be committed before a following plan_read in the same batch.
    assert "plan_update" not in READONLY_TOOL_NAMES
    assert "plan_read" in READONLY_TOOL_NAMES
    assert "plan_update" in executor._definitions_by_name


def test_ai_smell_score_is_a_plain_tool_the_model_calls_itself(tmp_path: Path) -> None:
    """AI 味打分从 change_set_propose 里的强制钩子改成普通工具（2026-09-24）：
    模型自己决定写完要不要打分；提案不再自动起子进程、不再往摘要里拼分数。"""

    class FakeAiSmell:
        available = True

        def __init__(self) -> None:
            self.texts: list[str] = []

        def evaluate_text(self, text: str, **_kwargs):
            from zhimeng_runtime.ai_smell_service import AiSmellReport

            self.texts.append(text)
            return AiSmellReport(
                score=61.0,
                level="偏油",
                content_hash="a" * 64,
                detector_version="test",
                top_components=(("cliche", 0.4),),
                sample_characters=len(text),
            )

    executor, _, _ = _build_runtime(tmp_path)
    fake = FakeAiSmell()
    executor.ai_smell = fake
    assert "ai_smell_score" in executor._definitions_by_name
    assert "ai_smell_score" in READONLY_TOOL_NAMES

    execution = asyncio.run(
        executor._dispatch(
            SimpleNamespace(run_id="run-ai-smell", project_revision=None),
            "ai_smell_score",
            "call_ai_smell_0001",
            "hash",
            {"text": "沈青云放下茶碗。院外的脚步声停了。"},
            asyncio.Event(),
        )
    )
    assert execution.output["schema"] == "zhimeng.tool-result.ai-smell-score.v1"
    assert execution.output["score"] == 61.0
    assert execution.output["level"] == "偏油"
    assert execution.output["note"].startswith("AI 味 61 分")
    assert fake.texts and "沈青云放下茶碗" in fake.texts[0]

    # 没装检测器：给一条明确的工具错误，不是静默 None。
    executor.ai_smell = None
    with pytest.raises(AgentToolError) as info:
        asyncio.run(
            executor._dispatch(
                SimpleNamespace(run_id="run-ai-smell", project_revision=None),
                "ai_smell_score",
                "call_ai_smell_0002",
                "hash",
                {"text": "随便一句。"},
                asyncio.Event(),
            )
        )
    assert info.value.code == "agent_tool_ai_smell_unavailable"
    # 钩子已经不存在。
    assert not hasattr(executor, "_ai_smell_evidence_for_changes")


def test_spill_skips_tools_that_already_trim_their_own_output(tmp_path: Path) -> None:
    """web_fetch / 命令 / 电脑读文件已经按字符取过头尾；大结果落盘层不再截第二次，
    且阈值按字符数算（中文一字三字节，按字节算会把 24k 字正文再截掉三分之二）。"""
    executor, _, _ = _build_runtime(tmp_path)
    big_chinese = "字" * 24_000  # 72KB UTF-8，但只有 24k 字符
    execution = AgentToolExecution(output={"content": big_chinese})
    for tool_name in ("web_fetch", "computer_run_command", "computer_read_file", "read_file"):
        assert (
            executor._spill_large_output(
                execution, tool_call_id="call_spill_skip_00000000000000", tool_name=tool_name
            )
            is execution
        )
    # 其它工具：24k 中文字符也低于 32k 字符阈值，原样回喂。
    assert (
        executor._spill_large_output(
            execution, tool_call_id="call_spill_chars_0000000000000", tool_name="project_search"
        )
        is execution
    )
