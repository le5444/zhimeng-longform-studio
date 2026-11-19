"""选文件夹用的接口（纠偏指令 P2 第一步）：常用位置、列一层文件夹、新建文件夹。每条都真打一遍接口。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.fs_browse_api import LIST_LIMIT, create_fs_router, folder_name_problem


def _client(trusted: list[Path], projects: list[Path]) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_fs_router(trusted_roots=lambda: trusted, project_roots=lambda: projects))
    return TestClient(app)


def test_places_list_home_folders_trusted_roots_project_parents_and_drives(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    for name in ("Desktop", "Documents"):
        (home / name).mkdir(parents=True)
    library = tmp_path / "写小说工作区"
    library.mkdir()
    project = tmp_path / "projects" / "宗门"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # 打开过的书自己也在受信根里（require_binding 会加）：常用位置里不重复列它。
    places = _client([library, project], [project]).get("/api/v1/fs/places").json()["places"]

    by_kind = {}
    for place in places:
        by_kind.setdefault(place["kind"], []).append(place)
    assert [place["label"] for place in by_kind["home"]] == ["主目录"]
    assert {place["label"] for place in by_kind["desktop"] + by_kind["documents"]} == {"桌面", "文档"}
    assert "downloads" not in by_kind  # 没有的位置不列
    assert by_kind["trusted"] == [{"label": "写小说工作区", "path": str(library), "kind": "trusted"}]
    assert by_kind["recent"][0]["path"] == str(project.parent)
    assert by_kind["drive"]


def test_places_skip_machine_folders_inside_dot_folders(tmp_path: Path, monkeypatch) -> None:
    """织梦自己的 .runtime 里的 author-projects 也在受信根里：不当常用位置（作者那套 9-27 实测框一打开就停在它上面）。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    library = tmp_path / "写小说工作区"
    managed = tmp_path / "新项目" / ".runtime" / "author-projects"
    imported = managed / "导入的书"
    for folder in (library, imported):
        folder.mkdir(parents=True)

    places = _client([managed, library], [imported]).get("/api/v1/fs/places").json()["places"]

    paths = [place["path"] for place in places]
    assert str(library) in paths
    assert str(managed) not in paths
    assert [place["kind"] for place in places if place["path"] == str(library)] == ["trusted"]


def test_list_shows_only_visible_folders_with_project_and_book_marks(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "宗门").mkdir(parents=True)
    (root / "宗门" / "这本书.md").write_text("想法", encoding="utf-8")
    (root / "代码仓库").mkdir()
    (root / ".git").mkdir()
    (root / "隐藏的").mkdir()
    subprocess.run(["attrib", "+h", str(root / "隐藏的")], check=True)
    (root / "说明.txt").write_text("不是文件夹", encoding="utf-8")

    listing = _client([], [root / "代码仓库"]).get("/api/v1/fs/list", params={"path": str(root)}).json()

    assert listing["path"] == str(root)
    assert listing["parent"] == str(tmp_path)
    assert listing["breadcrumbs"][-1] == {"name": "root", "path": str(root)}
    assert [entry["name"] for entry in listing["entries"]] == ["代码仓库", "宗门"]
    entries = {entry["name"]: entry for entry in listing["entries"]}
    assert entries["宗门"]["looks_like_book"] is True and entries["宗门"]["is_project"] is False
    assert entries["代码仓库"]["is_project"] is True and entries["代码仓库"]["looks_like_book"] is False
    assert listing["total"] == 2 and listing["truncated"] is False


def test_list_accepts_pasted_quoted_paths_and_explains_bad_ones(tmp_path: Path) -> None:
    client = _client([], [])
    pasted = client.get("/api/v1/fs/list", params={"path": f'"{tmp_path}"'})
    assert pasted.status_code == 200 and pasted.json()["path"] == str(tmp_path)

    missing = client.get("/api/v1/fs/list", params={"path": str(tmp_path / "没有这个")})
    assert missing.status_code == 404
    assert "不存在" in missing.json()["error"]["message"]

    relative = client.get("/api/v1/fs/list", params={"path": "只有名字"})
    assert relative.status_code == 422


def test_list_stops_at_the_limit_and_says_so(tmp_path: Path) -> None:
    for index in range(LIST_LIMIT + 3):
        (tmp_path / f"folder-{index:04d}").mkdir()

    listing = _client([], []).get("/api/v1/fs/list", params={"path": str(tmp_path)}).json()

    assert len(listing["entries"]) == LIST_LIMIT
    assert listing["total"] == LIST_LIMIT + 3
    assert listing["truncated"] is True


def test_mkdir_creates_and_explains_windows_name_problems(tmp_path: Path) -> None:
    client = _client([], [])

    created = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "宗门群像"})
    assert created.status_code == 201, created.text
    assert Path(created.json()["path"]) == tmp_path / "宗门群像"
    assert (tmp_path / "宗门群像").is_dir()

    again = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": "宗门群像"})
    assert again.status_code == 409
    assert "已经有了" in again.json()["error"]["message"]

    for bad in ("a:b", "结尾有点.", "CON", "aux.txt"):
        response = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path), "name": bad})
        assert response.status_code == 422, bad
        assert not (tmp_path / bad).exists()

    nowhere = client.post("/api/v1/fs/mkdir", json={"parent": str(tmp_path / "没有"), "name": "x"})
    assert nowhere.status_code == 404


@pytest.mark.parametrize(
    ("name", "ok"),
    [("宗门群像", True), ("my-project", True), ("a/b", False), ("问号?", False), ("  ", False), ("LPT1", False), ("结尾空格 ", False)],
)
def test_folder_name_problem(name: str, ok: bool) -> None:
    assert (folder_name_problem(name) is None) is ok


def test_fs_routes_are_wired_into_the_real_app(tmp_path: Path) -> None:
    """真 create_app：常用位置里有书库（受信根），列得出书库里的书并标上「已打开过」，能在书库里建文件夹。"""
    from zhimeng_runtime.app import create_app
    from zhimeng_runtime.config import RuntimeSettings

    library = tmp_path / "写小说工作区"
    book = library / "宗门"
    book.mkdir(parents=True)
    (book / "这本书.md").write_text("# 宗门\n", encoding="utf-8")
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,),
        allowed_origins=(),
    )
    with TestClient(create_app(settings)) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(book)})
        assert inspected.status_code == 200, inspected.text

        places = client.get("/api/v1/fs/places")
        assert places.status_code == 200, places.text
        kinds = {(place["kind"], place["path"]) for place in places.json()["places"]}
        assert ("trusted", str(library)) in kinds
        assert ("trusted", str(book)) not in kinds

        listing = client.get("/api/v1/fs/list", params={"path": str(library)})
        assert listing.status_code == 200, listing.text
        entry = listing.json()["entries"][0]
        assert entry["name"] == "宗门" and entry["is_project"] is True and entry["looks_like_book"] is True

        made = client.post("/api/v1/fs/mkdir", json={"parent": str(library), "name": "新的空文件夹"})
        assert made.status_code == 201, made.text
    assert (library / "新的空文件夹").is_dir()
