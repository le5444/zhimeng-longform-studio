from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.project_reader import SECTION_LABELS


def _create_project(root: Path) -> None:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    (root / "03_人物设定" / "主角.md").write_text("# 青崖\n状态：活跃\n擅长借势破局。关联 [[主线]] 与 [[不存在的证人]]。\n", encoding="utf-8")
    (root / "04_剧情大纲" / "主线.md").write_text("# 主线\n找回证人，并牵动青崖。\n", encoding="utf-8")
    (root / "04_剧情大纲" / "伏笔清单.md").write_text("# 伏笔\n旧港白灯。\n", encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True)
    (volume / "CH001.md").write_text("正文不应进入资产索引。\n", encoding="utf-8")


def test_project_asset_index_rebuild_and_get(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(library,),
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]

        rebuilt = client.post(f"/api/v1/projects/{project_id}/assets/rebuild")
        assert rebuilt.status_code == 200, rebuilt.text
        body = rebuilt.json()
        assert body["schema"] == "zhimeng.project-asset-index-response.v1"
        index = body["index"]
        assert index["asset_count"] >= 3
        assert index.get("status_tracked_count", 0) >= 1
        assert index.get("issue_count", 0) >= 1
        issues = index.get("issue_cards") or []
        assert any(item.get("kind") == "missing_reference" for item in issues)
        assert index.get("domain_issue_cards_complete") is False
        assert any(item.get("status_tracked") for item in index["assets"])
        assert index["relationship_graph_complete"] is False
        assert index["status_tracking_complete"] is False
        assert index["formal_draft_allowed"] is False
        assert index.get("relationship_count", 0) >= 1
        assert any(item.get("relationship_count", 0) >= 1 for item in index["assets"])
        relations = index.get("relationships") or []
        assert any(item.get("relation_type") in {"wiki_link", "title_mention"} for item in relations)
        assert all(item.get("complete") is False for item in relations)
        kinds = {item["kind"] for item in index["assets"]}
        assert "character" in kinds
        assert "plot" in kinds or "foreshadowing" in kinds
        assert all(
            not item["relative_path"].startswith("05_正文内容/")
            for item in index["assets"]
        )
        assert "relationship_graph_complete" in body["not_claiming"]

        fetched = client.get(f"/api/v1/projects/{project_id}/assets")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["index"]["index_hash"] == index["index_hash"]
