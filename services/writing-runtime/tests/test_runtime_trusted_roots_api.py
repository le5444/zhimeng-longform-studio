"""GET/POST /api/v1/runtime/trusted-roots (added 2026-09-19, shipped without a test).

The first live call returned 500: the handlers read ``context.project_reader``,
an attribute RuntimeContext never had. These tests call the endpoint the way the
Agent's command tool does, so a wrong attribute name fails here instead of in the
author's browser.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings


def _settings(tmp_path: Path, library: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,),
        allowed_origins=(),
    )


def _norm(value: str | Path) -> str:
    return str(Path(value).resolve()).lower()


def test_trusted_roots_get_lists_configured_roots(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with TestClient(create_app(_settings(tmp_path, library))) as client:
        response = client.get("/api/v1/runtime/trusted-roots")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert _norm(library) in {_norm(root) for root in payload["trusted_roots"]}
    assert payload["persisted_roots"] == []


def test_trusted_roots_post_adds_root_and_persists_across_restart(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    moved = tmp_path / "moved-books"
    moved.mkdir()
    settings = _settings(tmp_path, library)
    with TestClient(create_app(settings)) as client:
        added = client.post("/api/v1/runtime/trusted-roots", json={"path": str(moved)})
        assert added.status_code == 200, added.text
        payload = added.json()
        assert _norm(moved) in {_norm(root) for root in payload["trusted_roots"]}
        assert [_norm(root) for root in payload["persisted_roots"]] == [_norm(moved)]
        # Adding the same root twice must not duplicate the persisted entry.
        again = client.post("/api/v1/runtime/trusted-roots", json={"path": str(moved)})
        assert again.status_code == 200, again.text
        assert len(again.json()["persisted_roots"]) == 1

    state_file = settings.database_path.parent / "trusted-roots.json"
    assert state_file.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))["trusted_roots"]
    assert [_norm(root) for root in persisted] == [_norm(moved)]

    # A fresh process (restart) must pick the persisted root back up.
    with TestClient(create_app(settings)) as client:
        listed = client.get("/api/v1/runtime/trusted-roots")
    assert listed.status_code == 200, listed.text
    assert _norm(moved) in {_norm(root) for root in listed.json()["trusted_roots"]}


def test_trusted_roots_post_rejects_relative_and_missing_paths(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    with TestClient(create_app(_settings(tmp_path, library))) as client:
        relative = client.post("/api/v1/runtime/trusted-roots", json={"path": "books/relative"})
        missing = client.post(
            "/api/v1/runtime/trusted-roots",
            json={"path": str(tmp_path / "does-not-exist")},
        )
    assert relative.status_code == 422, relative.text
    assert "trusted_root_not_absolute" in relative.text
    assert missing.status_code == 422, missing.text
    assert "trusted_root_not_found" in missing.text


def test_trusted_roots_delete_removes_only_persisted_roots(tmp_path: Path) -> None:
    """设置页移除可信文件夹：自己加过的当场删掉并落盘；启动配置里带的根
    与没加过的路径都拒绝（409），不给出「删了但没删」的假状态。"""
    library = tmp_path / "library"
    library.mkdir()
    moved = tmp_path / "moved-books"
    moved.mkdir()
    settings = _settings(tmp_path, library)
    with TestClient(create_app(settings)) as client:
        added = client.post("/api/v1/runtime/trusted-roots", json={"path": str(moved)})
        assert added.status_code == 200, added.text

        removed = client.request(
            "DELETE", "/api/v1/runtime/trusted-roots", json={"path": str(moved)}
        )
        assert removed.status_code == 200, removed.text
        payload = removed.json()
        assert payload["persisted_roots"] == []
        assert _norm(moved) not in {_norm(root) for root in payload["trusted_roots"]}
        assert _norm(library) in {_norm(root) for root in payload["trusted_roots"]}

        # 启动配置里带的根不能在这里删。
        builtin = client.request(
            "DELETE", "/api/v1/runtime/trusted-roots", json={"path": str(library)}
        )
        assert builtin.status_code == 409, builtin.text
        assert "trusted_root_not_removable" in builtin.text
        # 没加过的路径同样拒绝。
        never = client.request(
            "DELETE", "/api/v1/runtime/trusted-roots", json={"path": str(moved)}
        )
        assert never.status_code == 409, never.text

    state_file = settings.database_path.parent / "trusted-roots.json"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))["trusted_roots"]
    assert persisted == []

    # 重启后不再回来。
    with TestClient(create_app(settings)) as client:
        listed = client.get("/api/v1/runtime/trusted-roots")
    assert listed.status_code == 200, listed.text
    assert _norm(moved) not in {_norm(root) for root in listed.json()["trusted_roots"]}


def test_trusted_roots_post_inside_a_trusted_root_does_not_add_an_entry(tmp_path: Path) -> None:
    """选文件夹框打开书库里的一本书（纠偏 P2）：它已经在书库这个受信根里，不再单独记一条。"""
    library = tmp_path / "library"
    book = library / "宗门"
    book.mkdir(parents=True)
    settings = _settings(tmp_path, library)
    with TestClient(create_app(settings)) as client:
        added = client.post("/api/v1/runtime/trusted-roots", json={"path": str(book)})
    assert added.status_code == 200, added.text
    assert added.json()["persisted_roots"] == []
    assert not (settings.database_path.parent / "trusted-roots.json").exists()
