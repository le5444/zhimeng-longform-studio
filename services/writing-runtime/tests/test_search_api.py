from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import ContextAuthority, canonical_json_hash
from zhimeng_runtime.knowledge_models import (
    KnowledgeCard,
    KnowledgeCardStatus,
    KnowledgeCardVersion,
    KnowledgeLineageStatus,
    KnowledgeVersionFormalizationStatus,
)
from zhimeng_runtime.project_reader import SECTION_LABELS, ProjectReader
from zhimeng_runtime.search_models import (
    SearchQueryRequest,
    SearchRebuildRequest,
    SearchSourceType,
)
from zhimeng_runtime.search_service import SearchService
from zhimeng_runtime.database import SqliteStore


def _create_project(root: Path, *, prose: str = "可编辑的第一章") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir(exist_ok=True)
    volume = root / "05_正文内容" / "第一卷文件夹"
    volume.mkdir(parents=True, exist_ok=True)
    prose_path = volume / "CH001.md"
    prose_path.write_text(prose, encoding="utf-8")
    character = root / "03_人物设定" / "主角档案.md"
    character.write_text("主角名叫青崖，擅长借势破局。", encoding="utf-8")
    return prose_path


def _client(tmp_path: Path, trusted_root: Path) -> TestClient:
    return TestClient(
        create_app(
            RuntimeSettings(
                environment="test",
                database_path=tmp_path / "runtime.sqlite3",
                trusted_project_roots=(trusted_root,),
                allowed_origins=(),
            )
        )
    )


def _fake_card(*, card_id: str, title: str, summary: str) -> KnowledgeCard:
    now = datetime.now(UTC)
    payload = {
        "card_id": card_id,
        "card_type": "work",
        "title": title,
        "summary": summary,
        "keywords": ["阶段兑现"],
        "tags": ["玄幻"],
    }
    content_hash = canonical_json_hash(payload)
    version_id = f"knowledge-version-{'a' * 32}"
    version = KnowledgeCardVersion(
        version_id=version_id,
        card_id=card_id,
        version_number=1,
        content_hash=content_hash,
        card=payload,
        source_candidate_id=f"knowledge-candidate-{'b' * 32}",
        lineage_status=KnowledgeLineageStatus.LEGACY_UNVERIFIED,
        formalization_status=KnowledgeVersionFormalizationStatus.LEGACY_UNVERIFIED,
        created_at=now,
    )
    return KnowledgeCard(
        card_id=card_id,
        status=KnowledgeCardStatus.ACTIVE,
        state_revision=1,
        active_version_id=version_id,
        active_version=version,
        versions=[version],
        created_at=now,
        updated_at=now,
    )


class _FakeKnowledge:
    def __init__(self, cards: list[KnowledgeCard]) -> None:
        self.cards = cards
        self.list_candidates_calls = 0

    def list_cards(self, *, project_id: str | None = None, scope_id: str | None = None):
        return SimpleNamespace(cards=list(self.cards))

    def observe_knowledge_fingerprint(self, project_id: str | None = None) -> str:
        material = [
            {
                "card_id": card.card_id,
                "version_id": card.active_version_id,
                "content_hash": card.active_version.content_hash,
            }
            for card in sorted(self.cards, key=lambda item: item.card_id)
        ]
        return canonical_json_hash({"cards": material, "project_id": project_id})

    def list_candidates(self, *args, **kwargs):
        self.list_candidates_calls += 1
        raise AssertionError("search must not read unpromoted candidates")


def test_project_search_rebuild_and_query_project_truth(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project, prose="青崖在断桥上完成第一次阶段兑现。")

    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]

        rebuilt = client.post(
            f"/api/v1/projects/{project_id}/search/rebuild",
            json={"force": True},
        )
        assert rebuilt.status_code == 200, rebuilt.text
        body = rebuilt.json()
        assert body["schema"] == "zhimeng.project-search-rebuild-result.v1"
        assert body["rebuilt"] is True
        assert body["manifest"]["document_count"] >= 1

        searched = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "阶段兑现"},
        )
        assert searched.status_code == 200, searched.text
        payload = searched.json()
        assert payload["schema"] == "zhimeng.project-search-query-response.v1"
        assert payload["hit_count"] >= 1
        hit = payload["hits"][0]
        assert hit["source_type"] == "project_file"
        assert hit["authority"] == "project_truth"
        assert hit["source_id"]
        assert hit["source_hash"]
        assert hit["version"]
        assert "阶段兑现" in hit["snippet"]
        assert hit["reason"]

        filtered = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={
                "query": "阶段兑现",
                "source_types": ["formal_knowledge"],
            },
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["hit_count"] == 0


def test_project_search_excludes_paths_marked_as_non_truth(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project, prose="当前正文只采用青崖这个名字。")
    archived = project / "00_归档_旧结构_勿作为真值" / "旧设定.md"
    archived.parent.mkdir()
    archived.write_text("废弃设定：主角曾经叫玄河。", encoding="utf-8")

    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        project_id = inspected.json()["project"]["project_id"]
        searched = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "玄河", "source_types": ["project_file"]},
        )

        assert searched.status_code == 200, searched.text
        assert searched.json()["hit_count"] == 0


def test_project_search_auto_rebuild_drops_stale_content(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project, prose="旧文里只有青崖旧名。")

    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        project_id = inspected.json()["project"]["project_id"]
        first = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "旧名"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["hit_count"] >= 1
        assert first.json()["rebuilt_before_search"] is True

        prose.write_text("新文只写青崖破局与借势反击。", encoding="utf-8")
        # Touch another inspect so binding revision/fingerprint moves with ProjectReader.
        client.post("/api/v1/projects/inspect", json={"root_path": str(project)})

        second = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "旧名"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["rebuilt_before_search"] is True
        assert second.json()["hit_count"] == 0

        third = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "破局"},
        )
        assert third.status_code == 200, third.text
        assert third.json()["hit_count"] >= 1
        assert any("破局" in hit["snippet"] for hit in third.json()["hits"])


def test_search_service_indexes_formal_cards_not_candidates(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project, prose="正文没有候选秘密短语。")
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    reader = ProjectReader((library,))
    inspection = reader.inspect(project)
    project_id = str(inspection["project"]["project_id"])
    store.sync_project_binding(
        project_id=project_id,
        display_name="book",
        root_path=str(project.resolve()),
        fingerprint=str(inspection["project"]["fingerprint"]),
    )

    formal = _fake_card(
        card_id="formal-card-search-1",
        title="正式升级循环",
        summary="正式知识卡写明阶段兑现必须付出代价。",
    )
    knowledge = _FakeKnowledge([formal])
    service = SearchService(
        store,
        reader,
        knowledge,
        tmp_path / "project-search",
    )
    rebuilt = service.rebuild(project_id, SearchRebuildRequest(force=True))
    assert rebuilt.rebuilt is True
    assert rebuilt.manifest.document_count >= 2

    result = service.search(
        project_id,
        SearchQueryRequest(query="阶段兑现", authorities=[ContextAuthority.FORMAL_KNOWLEDGE]),
    )
    assert result.hit_count == 1
    hit = result.hits[0]
    assert hit.source_type.value == "formal_knowledge"
    assert hit.authority is ContextAuthority.FORMAL_KNOWLEDGE
    assert hit.source_id == formal.card_id
    assert hit.version == formal.active_version_id
    assert hit.source_hash == formal.active_version.content_hash
    assert "阶段兑现" in hit.snippet
    assert knowledge.list_candidates_calls == 0


def test_search_marks_full_text_without_vector_enhancement(tmp_path: Path) -> None:
    library = tmp_path / "library"
    project = library / "book"
    _create_project(project, prose="青崖在旧港完成阶段兑现。")
    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]
        rebuilt = client.post(
            f"/api/v1/projects/{project_id}/search/rebuild",
            json={"force": True},
        )
        assert rebuilt.status_code == 200, rebuilt.text
        manifest = rebuilt.json()["manifest"]
        assert manifest.get("full_text_indexed") is True
        assert manifest.get("vector_index_ready") is False
        searched = client.post(
            f"/api/v1/projects/{project_id}/search",
            json={"query": "阶段兑现"},
        )
        assert searched.status_code == 200, searched.text
        body = searched.json()
        assert body.get("match_mode") == "full_text"
        assert body.get("vector_enhanced") is False
        assert body["hit_count"] >= 1


class _FakeAuthorizedSources:
    def __init__(self, records: list) -> None:
        self.records = list(records)

    def list_authorized_sources(self, project_id: str):
        return list(self.records)

    def observe_authorized_source_fingerprint(self, project_id: str) -> str:
        material = [
            {
                "source_id": item.source_id,
                "source_hash": item.source_hash,
                "version": item.version,
            }
            for item in sorted(self.records, key=lambda row: row.source_id)
        ]
        return canonical_json_hash(
            {
                "schema": "zhimeng.project-search-authorized-source-fingerprint.v1",
                "project_id": project_id,
                "sources": material,
            }
        )


def test_search_indexes_authorized_sources(tmp_path: Path) -> None:
    from zhimeng_runtime.search_service import AuthorizedSourceRecord

    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project, prose="项目正文并不包含外部秘钥。")
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    reader = ProjectReader((library,))
    inspection = reader.inspect(project)
    project_id = str(inspection["project"]["project_id"])
    store.sync_project_binding(
        project_id=project_id,
        display_name="book",
        root_path=str(project.resolve()),
        fingerprint=str(inspection["project"]["fingerprint"]),
    )
    source_text = "授权来源写着青崖在旧港完成阶段兑现。"
    source_hash = canonical_json_hash({"content": source_text})
    catalog = _FakeAuthorizedSources(
        [
            AuthorizedSourceRecord(
                source_id="source-import-" + ("c" * 32),
                title="旧港参考书",
                version="status-completed",
                source_hash=source_hash,
                content=source_text,
            )
        ]
    )
    service = SearchService(
        store,
        reader,
        _FakeKnowledge([]),
        tmp_path / "project-search",
        authorized_source_catalog=catalog,
    )
    rebuilt = service.rebuild(project_id, SearchRebuildRequest(force=True))
    assert rebuilt.rebuilt is True
    assert rebuilt.manifest.document_count >= 1
    response = service.search(
        project_id,
        SearchQueryRequest(
            query="阶段兑现",
            source_types=[SearchSourceType.AUTHORIZED_SOURCE],
        ),
    )
    assert response.hit_count == 1
    hit = response.hits[0]
    assert hit.source_type is SearchSourceType.AUTHORIZED_SOURCE
    assert hit.authority is ContextAuthority.REFERENCE
    assert "阶段兑现" in hit.snippet
    assert hit.source_id.startswith("source-import-")
    assert response.match_mode == "full_text"
    assert response.vector_enhanced is False
