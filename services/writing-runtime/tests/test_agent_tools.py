from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.agent_tools import AgentToolError, AgentToolExecutor
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
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS
from zhimeng_runtime.search_service import SearchService
from zhimeng_runtime.skill_executor import SkillExecutor


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REAL_PROJECT_AUDIT_PACKAGE = (
    REPOSITORY_ROOT / "adapters" / "builtin" / "creation-suite-project-audit"
)
PROJECT_AUDIT_SKILL_ID = "novel-creation-suite.project-audit"


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

    assert [item.name for item in executor.definitions] == [
        "project_list_files",
        "project_read_file",
        "project_search",
        "change_set_propose",
    ]
    rendered = " ".join(
        item.model_dump_json(by_alias=True) for item in executor.definitions
    ).casefold()
    assert "change_set_apply" not in rendered
    assert "shell" not in rendered
    assert "mytemple" not in rendered
    assert "plugin" not in rendered


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
    long_line = "x" * 120_000
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
