"""左栏项目的「在资源管理器里打开」（纠偏 P2，专题《项目即文件夹》第 4 节）：只开这个项目自己的文件夹。真走 create_app。"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime import project_api
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def test_reveal_opens_only_the_project_folder(tmp_path: Path, monkeypatch) -> None:
    library = tmp_path / "写小说工作区"
    book = library / "宗门"
    book.mkdir(parents=True)
    (book / "这本书.md").write_text("# 宗门\n", encoding="utf-8")
    opened: list[Path] = []
    monkeypatch.setattr(project_api, "reveal_folder", lambda folder: opened.append(Path(folder)))
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,),
        allowed_origins=(),
    )
    with TestClient(create_app(settings)) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(book)})
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]

        revealed = client.post(f"/api/v1/projects/{project_id}/reveal")
        assert revealed.status_code == 200, revealed.text
        assert Path(revealed.json()["path"]) == book.resolve()
        assert opened == [book.resolve()]

        unknown = client.post("/api/v1/projects/project-000000000000000000000000/reveal")
        assert unknown.status_code == 404

        shutil.rmtree(book)
        gone = client.post(f"/api/v1/projects/{project_id}/reveal")
        assert gone.status_code == 404, gone.text
        assert "不在原来的位置" in gone.json()["error"]["message"]
    assert len(opened) == 1
