from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_reader import (
    SECTION_LABELS,
    ProjectChangedDuringReadError,
    ProjectReader,
)


def _create_project(root: Path) -> Path:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷文件夹"
    volume.mkdir()
    prose = volume / "CH001.md"
    prose.write_text("可编辑的第一章", encoding="utf-8")
    return prose


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


def test_project_api_exposes_tree_and_etagged_text_content(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(project)})
        assert inspected.status_code == 200
        payload = inspected.json()
        assert payload["schema"] == "zhimeng.project-inspection.v1"
        assert payload["project"]["display_name"] == "book"
        assert payload["project"]["revision"] == 1
        assert payload["volumes"][0]["chapter_ids"] == ["VOL001-CH001"]
        prose_record = next(item for item in payload["files"] if item["kind"] == "prose")
        project_id = payload["project"]["project_id"]

        bindings = client.get("/api/v1/projects")
        rebound = client.get(f"/api/v1/projects/{project_id}/inspect")

        content = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}"
        )
        etag = content.headers["etag"]
        not_modified = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}",
            headers={"If-None-Match": f"W/{etag}"},
        )
        missing_file = client.get(
            f"/api/v1/projects/{project_id}/files/file-{'0' * 32}"
        )

    assert bindings.status_code == 200
    assert bindings.json()["schema"] == "zhimeng.project-bindings.v1"
    assert bindings.json()["projects"][0]["project_id"] == project_id
    assert rebound.status_code == 200
    assert rebound.json()["project"]["revision"] == 1
    assert content.status_code == 200
    assert content.json()["content"] == prose.read_text(encoding="utf-8")
    assert content.json()["snapshot_revision"] == 1
    assert len(content.json()["etag"]) == 64
    assert content.headers["etag"] == f'"{content.json()["etag"]}"'
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == content.headers["etag"]
    assert missing_file.status_code == 404
    assert missing_file.json()["schema"] == "zhimeng.error.v1"


def test_project_revision_survives_runtime_restart_and_same_size_change(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as first_client:
        first = first_client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()

    original = prose.read_text(encoding="utf-8")
    replacement = "已改写的第一章"
    assert len(replacement.encode("utf-8")) == len(original.encode("utf-8"))
    prose.write_text(replacement, encoding="utf-8")

    project_id = first["project"]["project_id"]
    with _client(tmp_path, library) as restarted_client:
        before_refresh = restarted_client.get("/api/v1/projects").json()
        refreshed = restarted_client.get(
            f"/api/v1/projects/{project_id}/inspect"
        ).json()
        after_refresh = restarted_client.get("/api/v1/projects").json()

    assert before_refresh["projects"][0]["revision"] == 1
    assert refreshed["project"]["revision"] == 2
    assert refreshed["project"]["fingerprint"] != first["project"]["fingerprint"]
    assert after_refresh["projects"][0]["revision"] == 2


def test_file_etag_changes_when_snapshot_metadata_changes(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    prose = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        ).json()
        project_id = snapshot["project"]["project_id"]
        prose_record = next(item for item in snapshot["files"] if item["kind"] == "prose")
        first = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}"
        )
        old_etag = first.headers["etag"]

        prose.rename(prose.with_name("CH001_定稿.md"))
        renamed = client.get(
            f"/api/v1/projects/{project_id}/files/{prose_record['file_id']}",
            headers={"If-None-Match": old_etag},
        )

    assert renamed.status_code == 200
    assert renamed.headers["etag"] != old_etag
    assert renamed.json()["snapshot_revision"] == 2
    assert renamed.json()["file"]["relative_path"].endswith("CH001_定稿.md")


def test_project_api_rejects_outside_root_and_requires_configuration(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside"
    _create_project(outside)

    with _client(tmp_path, trusted) as client:
        rejected = client.post("/api/v1/projects/inspect", json={"root_path": str(outside)})
    assert rejected.status_code == 403
    assert rejected.json()["schema"] == "zhimeng.error.v1"

    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "unconfigured.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        unavailable = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(outside)},
        )
    assert unavailable.status_code == 403


def test_text_import_creates_managed_project_and_splits_chapters(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    content = (
        "作品说明\n\n"
        "第一章 山门来客\n沈青云推开山门。\n\n"
        "第2章 青云玉\n柳青烟发现玉佩不见了。\n"
    )

    with TestClient(app) as client:
        imported = client.post(
            "/api/v1/projects/import-text",
            json={
                "schema": "zhimeng.text-project-import-request.v1",
                "file_name": "宗门新篇.txt",
                "content": content,
            },
        )
        assert imported.status_code == 201, imported.text
        payload = imported.json()
        inspection = payload["inspection"]
        listed = client.get("/api/v1/projects")

    assert payload["schema"] == "zhimeng.text-project-import-response.v1"
    assert payload["title"] == "宗门新篇"
    assert payload["chapter_count"] == 2
    assert inspection["project"]["display_name"] == "宗门新篇"
    assert inspection["project"]["root_path"].startswith(
        str((tmp_path / "author-projects").resolve())
    )
    assert len(inspection["chapters"]) == 2
    assert [chapter["number"] for chapter in inspection["chapters"]] == [1, 2]
    assert listed.status_code == 200
    assert [item["display_name"] for item in listed.json()["projects"]] == ["宗门新篇"]
    chapter_paths = [
        Path(item["absolute_path"])
        for item in inspection["files"]
        if item["kind"] == "prose"
    ]
    assert len(chapter_paths) == 2
    assert "作品说明" in chapter_paths[0].read_text(encoding="utf-8")
    assert "柳青烟发现玉佩不见了" in chapter_paths[1].read_text(encoding="utf-8")


def test_text_import_rejects_non_txt_and_empty_content(tmp_path: Path) -> None:
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            allowed_origins=(),
        )
    )
    with TestClient(app) as client:
        wrong_type = client.post(
            "/api/v1/projects/import-text",
            json={"file_name": "作品.docx", "content": "第一章"},
        )
        empty = client.post(
            "/api/v1/projects/import-text",
            json={"file_name": "作品.txt", "content": "   "},
        )

    assert wrong_type.status_code == 422
    assert wrong_type.json()["error"]["code"] == "text_project_import_invalid"
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "text_project_import_invalid"


def test_invalid_configured_root_does_not_kill_runtime_health(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-library"
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=tmp_path / "runtime.sqlite3",
            trusted_project_roots=(missing_root,),
            allowed_origins=(),
        )
    )

    with TestClient(app) as client:
        live = client.get("/health/live")
        project = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(missing_root / "book")},
        )

    assert live.status_code == 200
    assert project.status_code == 503
    assert "does not resolve" in project.json()["error"]["message"]


def test_legacy_binding_without_fingerprint_is_listed_as_pending(tmp_path: Path) -> None:
    database_path = tmp_path / "runtime.sqlite3"
    store = SqliteStore(database_path)
    store.initialize()
    with store.connection() as connection:
        connection.execute(
            """
            INSERT INTO project_binding(
                id, root_path, display_name, fingerprint, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, '', 4, ?, ?)
            """,
            (
                "project-0123456789abcdef01234567",
                "C:\\Novels\\Legacy",
                "Legacy",
                "2026-07-17T00:00:00Z",
                "2026-07-17T00:00:00Z",
            ),
        )
    app = create_app(
        RuntimeSettings(
            environment="test",
            database_path=database_path,
            allowed_origins=(),
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 200
    assert response.json()["projects"][0]["fingerprint"] is None
    assert response.json()["projects"][0]["snapshot_ready"] is False
    assert response.json()["projects"][0]["revision"] == 4


def test_snapshot_change_returns_typed_retryable_conflict(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    def changed(*args, **kwargs):
        del args, kwargs
        raise ProjectChangedDuringReadError("project changed during scan")

    monkeypatch.setattr(ProjectReader, "inspect", changed)
    with _client(tmp_path, library) as client:
        response = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_snapshot_changed"
    assert response.json()["error"]["retryable"] is True
