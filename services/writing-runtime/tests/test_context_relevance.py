from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.context_models import ContextPack, canonical_json_hash
from zhimeng_runtime.context_service import (
    ContextService,
    OBJECTIVE_UNRELATED_SECTION_PRIORITY,
    _chapter_numbers_in_text,
    _chinese_number_to_int,
    _objective_signals,
    _path_match_tokens,
)
from zhimeng_runtime.context_store import ContextStore
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import (
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_kb_models import ProjectKbRebuildRequest
from zhimeng_runtime.project_kb_service import ProjectKbService
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS


def test_chinese_number_parsing() -> None:
    assert _chinese_number_to_int("4") == 4
    assert _chinese_number_to_int("４") == 4
    assert _chinese_number_to_int("四") == 4
    assert _chinese_number_to_int("十二") == 12
    assert _chinese_number_to_int("二十三") == 23
    assert _chinese_number_to_int("一百零五") == 105
    assert _chinese_number_to_int("两百") == 200
    assert _chinese_number_to_int("章") is None
    assert _chinese_number_to_int("") is None


def test_chapter_number_extraction() -> None:
    assert _chapter_numbers_in_text("第004章多少字") == frozenset({4})
    assert _chapter_numbers_in_text("对照第四章和第五章") == frozenset({4, 5})
    assert _chapter_numbers_in_text("CH007 的控制卡") == frozenset({7})
    assert _chapter_numbers_in_text("VOL001-CH003") == frozenset({3})
    assert _chapter_numbers_in_text("今天怎么样") == frozenset()
    assert _chapter_numbers_in_text("chapter four, no digits") == frozenset()


def test_path_match_tokens() -> None:
    tokens = _path_match_tokens("03_人物设定/人物.json")
    assert "人物设定" in tokens
    assert "人物" in tokens
    assert "03" not in tokens
    assert "json" in tokens  # long enough to count, harmless


def test_empty_objective_does_not_crash() -> None:
    signals = _objective_signals("")
    assert signals.light is True
    assert signals.chapter_numbers == frozenset()
    assert signals.matches("01_项目定位/定位.md", frozenset()) is False


def _write_project(root: Path) -> Path:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    core_files = {
        "01_项目定位/定位.md": "这是一本悬疑长篇，核心承诺是寻找失踪证人。",
        "02_世界观设定/城市.md": "故事发生在临江城，旧港区夜间封闭。",
        "03_人物设定/人物.md": "主角林澈，底线是不牺牲证人。",
        "04_剧情大纲/主线.md": "主线：找到证人，并揭开旧港失火真相。",
        "04_剧情大纲/CH003_细纲.md": "第三章细纲：证人留下带血钥匙。",
        "06_写作规范/风格.md": "对话克制，事实先于抒情。",
        "07_运营数据/project_status.md": "连载中；当前推进到第三章。",
    }
    for relative_path, content in core_files.items():
        (root / Path(relative_path)).write_text(content, encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir()
    (volume / "CH001_定稿.md").write_text("第一章：林澈收到匿名信。", encoding="utf-8")
    (volume / "CH002_定稿.md").write_text("第二章：匿名信指向旧港。", encoding="utf-8")
    (volume / "CH003_定稿.md").write_text("第三章：证人留下了带血钥匙。", encoding="utf-8")
    return root


class _Harness:
    def __init__(self, tmp_path: Path, extra_files: dict[str, str] | None = None) -> None:
        library = tmp_path / "library"
        library.mkdir()
        self.project = _write_project(library / "novel")
        for relative_path, content in (extra_files or {}).items():
            target = self.project / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        reader = ProjectReader([library])
        snapshot = reader.inspect(self.project)
        project_data = snapshot["project"]
        database = SqliteStore(tmp_path / "runtime.sqlite3")
        database.initialize()
        binding = database.sync_project_binding(
            project_id=str(project_data["project_id"]),
            root_path=str(self.project),
            display_name="novel",
            fingerprint=str(project_data["fingerprint"]),
        )
        agent_store = AgentStore(database)
        session = agent_store.create_session(
            project_id=binding.project_id,
            title="Context relevance",
            executor=ExecutorIdentity(
                executor_id="executor.context-relevance-test",
                kind=ExecutorKind.EMBEDDED,
                implementation="context-relevance-test",
                version="1",
                protocol_version="1",
                instance_id=None,
            ),
            capabilities=CapabilityProfile(
                protocol_version="1",
                names=[],
                extensions={},
            ),
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
        from zhimeng_runtime.context_api import create_context_router
        from fastapi import FastAPI

        from zhimeng_runtime.errors import install_exception_handlers

        service = ContextService(
            ContextStore(database),
            agent_store,
            reader,
            ProjectLockRegistry(),
            project_kb_service=project_kb,
        )
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(create_context_router(service))
        self.client = TestClient(app)
        self.session_id = session.session_id
        self.revision = binding.revision
        self.fingerprint = binding.fingerprint
        self.project_kb_state_id = rebuilt.record.manifest.state_id
        self.project_kb_manifest_hash = rebuilt.record.manifest.manifest_hash

    def create_pack(
        self,
        *,
        key: str,
        objective: str,
        manual_file_ids: list[str] | None = None,
    ) -> ContextPack:
        payload: dict[str, object] = {
            "objective": objective,
            "profile": "chat",
            "active_chapter_id": "VOL001-CH003",
            "expected_project_revision": self.revision,
            "expected_project_fingerprint": self.fingerprint,
            "project_kb_state_id": self.project_kb_state_id,
            "project_kb_manifest_hash": self.project_kb_manifest_hash,
        }
        if manual_file_ids:
            payload["manual_file_ids"] = manual_file_ids
        response = self.client.post(
            f"/api/v1/agent/sessions/{self.session_id}/context-packs",
            headers={"Idempotency-Key": key},
            json=payload,
        )
        assert response.status_code == 201, response.text
        return ContextPack.model_validate(response.json()["pack"])

    def file_id_for(self, relative_path: str) -> str:
        snapshot = ProjectReader([self.project.parent]).inspect(self.project)
        for item in snapshot["files"]:
            if item["relative_path"] == relative_path:
                return str(item["file_id"])
        raise AssertionError(f"file not in snapshot: {relative_path}")


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    return _Harness(tmp_path)


def _project_section_titles(pack: ContextPack, container) -> dict[str, int]:
    return {
        item.title: item.priority
        for item in container
        if item.source_type == "project_section"
    }


def _mandatory_titles(pack: ContextPack) -> set[str]:
    return {item.title for item in pack.included if item.mandatory}


def test_chatty_objective_keeps_only_the_minimal_set(harness: _Harness) -> None:
    pack = harness.create_pack(key="rel-casual-1", objective="你好")

    included_sections = _project_section_titles(pack, pack.included)
    # The outline of the active chapter stays: it is in the current chapter's
    # neighborhood, which always keeps its priority.
    assert included_sections == {"04_剧情大纲/CH003_细纲.md": 790}
    excluded_sections = _project_section_titles(pack, pack.excluded)
    assert {
        "01_项目定位/定位.md",
        "02_世界观设定/城市.md",
        "03_人物设定/人物.md",
        "04_剧情大纲/主线.md",
        "06_写作规范/风格.md",
        "07_运营数据/project_status.md",
    } == set(excluded_sections)
    assert all(
        item.candidate_exclusion_reason == "objective_light_unrelated_section"
        for item in pack.excluded
        if item.source_type == "project_section"
    )
    # The mandatory chapter candidates are exactly the pre-change behavior:
    # only the active chapter is mandatory; the two predecessors stay
    # eligible non-mandatory candidates as before.
    assert _mandatory_titles(pack) == {"05_正文内容/第一卷/CH003_定稿.md"}


def test_mandatory_set_is_identical_for_chatty_and_working_objectives(
    harness: _Harness,
) -> None:
    chatty = harness.create_pack(key="rel-mand-1", objective="在吗")
    working = harness.create_pack(
        key="rel-mand-2",
        objective="续写第四章前，核对人物立场和上一章结果。",
    )
    assert _mandatory_titles(chatty) == _mandatory_titles(working)


def test_named_chapter_keeps_related_files_above_unrelated_sections(
    harness: _Harness,
) -> None:
    pack = harness.create_pack(
        key="rel-chapter-1",
        objective="请对照第三章检查人物状态表有没有写错的地方",
    )
    included = _project_section_titles(pack, pack.included)
    # Token match: 人物 appears in the objective.
    assert included["03_人物设定/人物.md"] == 800
    # Chapter-number match: the file references CH003, named in the objective.
    assert included["04_剧情大纲/CH003_细纲.md"] == 790
    # Outline section keeps priority whenever a chapter is named.
    assert included["04_剧情大纲/主线.md"] == 790
    # Unrelated sections are demoted but still eligible and included.
    assert included["02_世界观设定/城市.md"] == OBJECTIVE_UNRELATED_SECTION_PRIORITY
    assert included["07_运营数据/project_status.md"] == (
        OBJECTIVE_UNRELATED_SECTION_PRIORITY
    )
    assert included["03_人物设定/人物.md"] > included["02_世界观设定/城市.md"]


def test_unrelated_files_stay_eligible_when_objective_is_not_light(
    harness: _Harness,
) -> None:
    pack = harness.create_pack(
        key="rel-long-1",
        objective="帮我看看这本书现在的整体情况，有没有什么明显的问题",
    )
    included_sections = _project_section_titles(pack, pack.included)
    excluded_sections = _project_section_titles(pack, pack.excluded)
    assert excluded_sections == {}
    # Active-chapter neighborhood keeps the CH003 outline at full priority.
    assert included_sections.pop("04_剧情大纲/CH003_细纲.md") == 790
    assert included_sections
    assert all(
        priority == OBJECTIVE_UNRELATED_SECTION_PRIORITY
        for priority in included_sections.values()
    )


def test_manual_files_remain_mandatory_under_a_chatty_objective(
    harness: _Harness,
) -> None:
    file_id = harness.file_id_for("02_世界观设定/城市.md")
    pack = harness.create_pack(
        key="rel-manual-1",
        objective="你好",
        manual_file_ids=[file_id],
    )
    manual = next(
        item for item in pack.included if item.source_type == "manual_project_file"
    )
    assert manual.title == "02_世界观设定/城市.md"
    assert manual.mandatory is True
    assert manual.priority == 995


def test_custom_folder_documents_join_section_candidates(tmp_path: Path) -> None:
    """作者自命名夹子（非 0N_ 前缀）里的设定文件照样进上下文。

    以前分区候选只认 01-09 的编号分区，作者自己起名的夹子（section_index=None）
    永远挤不进「核心分区」候选——布局是作者自己的事，不该因此变成查不到的资料。
    """
    harness = _Harness(
        tmp_path,
        extra_files={"我的设定/力量体系.md": "修炼分九境，每境三重，跨境而战必有代价。"},
    )
    pack = harness.create_pack(
        key="rel-custom-folder-1",
        objective="按力量体系把第三章的战斗写对，重写第三章。",
    )
    included = _project_section_titles(pack, pack.included)
    assert "我的设定/力量体系.md" in included
