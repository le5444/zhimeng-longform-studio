from __future__ import annotations

from pathlib import Path

import pytest

from test_change_set_api import _client, _inspect, _file, _accept_or_reject
from zhimeng_runtime.change_set_models import ChangeSetApplyRequest
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_reader import ProjectReader


def _propose(client, project: Path, changes):
    snapshot = _inspect(client, project)
    return client.post(f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets", json={
        "base_revision": snapshot["project"]["revision"],
        "base_fingerprint": snapshot["project"]["fingerprint"],
        "title": "普通文件操作", "summary": "独立临时目录中的文件验收。",
        "changes": changes,
    })


def test_create_python_dotenv_and_extensionless_files_then_undo(tmp_path: Path):
    project = tmp_path / "workspace"
    project.mkdir()
    contents = {"src/app.py": "print('sample')\n", ".env": "APP_MODE=development\n", "Dockerfile": "FROM sample-base\n"}
    with _client(tmp_path, project) as client:
        proposed = _propose(client, project, [{"operation": "create", "relative_path": name, "after_content": text, "rationale": "创建普通文本文件"} for name, text in contents.items()])
        assert proposed.status_code == 201, proposed.text
        record = proposed.json()
        applied = client.post(f"/api/v1/change-sets/{record['change_set_id']}/apply-direct", json={"expected_review_revision": record["review_revision"]})
        assert applied.status_code == 200, applied.text
        for name, text in contents.items():
            assert (project / name).read_bytes() == text.encode("utf-8")
        undone = client.post(f"/api/v1/change-sets/{record['change_set_id']}/rollback", json={"expected_review_revision": applied.json()["review_revision"]})
        assert undone.status_code == 200, undone.text
        assert all(not (project / name).exists() for name in contents)


@pytest.mark.parametrize("original", [b"binary\x00data", b"\x89PNG\r\n\x1a\n"])
def test_binary_existing_file_is_rejected_without_mutation(tmp_path: Path, original: bytes):
    project = tmp_path / "workspace"
    project.mkdir()
    path = project / "data.custom"
    path.write_bytes(original)
    with _client(tmp_path, project) as client:
        snapshot = _inspect(client, project)
        file_id = _file(snapshot, "data.custom")["file_id"]
        proposed = _propose(client, project, [{"operation": "update", "file_id": file_id, "after_content": "replacement text", "rationale": "测试二进制识别"}])
        assert proposed.status_code == 422, proposed.text
        assert path.read_bytes() == original


def test_create_rejects_content_that_the_text_reader_classifies_as_binary(tmp_path: Path):
    project = tmp_path / "workspace"
    project.mkdir()
    with _client(tmp_path, project) as client:
        proposed = _propose(client, project, [{"operation": "create", "relative_path": "data.txt", "after_content": "text\x01binary-control", "rationale": "保持创建与文本读取一致"}])
        assert proposed.status_code == 422, proposed.text
        assert not (project / "data.txt").exists()


def test_unicode_update_checks_actual_encoded_size_before_accepting_candidate(tmp_path: Path, monkeypatch):
    from zhimeng_runtime import change_set_service

    project = tmp_path / "workspace"
    project.mkdir()
    path = project / "data.txt"
    original = b"\xff\xfe\x00\x00" + "a\n".encode("utf-32-le")
    path.write_bytes(original)
    monkeypatch.setattr(change_set_service, "MAX_TEXT_FILE_BYTES", 32)
    with _client(tmp_path, project) as client:
        file_id = _file(_inspect(client, project), "data.txt")["file_id"]
        proposed = _propose(client, project, [{"operation": "update", "file_id": file_id, "after_content": "some longer plain text\n", "rationale": "编码后仍应能读取与撤回"}])
        assert proposed.status_code == 422, proposed.text
        assert path.read_bytes() == original


def test_interrupted_multifile_update_recovers_exact_unicode_bom_bytes(tmp_path: Path, monkeypatch):
    project = tmp_path / "workspace"
    project.mkdir()
    before = {
        "config.ini": b"\xfe\xff" + "name=旧值\r\nvalue=1\r\n".encode("utf-16-be"),
        "script.py": b"\xff\xfe\x00\x00" + "name='旧值'\r\nvalue=1\r\n".encode("utf-32-le"),
    }
    for name, content in before.items():
        (project / name).write_bytes(content)
    with _client(tmp_path, project) as client:
        snapshot = _inspect(client, project)
        changes = [{"operation": "update", "file_id": _file(snapshot, name)["file_id"], "after_content": "name=新值\nvalue=2\n", "rationale": "测试中断恢复"} for name in before]
        proposed = _propose(client, project, changes)
        assert proposed.status_code == 201, proposed.text
        ready = _accept_or_reject(client, proposed.json(), ["accepted", "accepted"])
    service = ChangeSetService(ProjectReader((project,)), SqliteStore(tmp_path / "runtime.sqlite3"), tmp_path / "change-sets")
    original_apply = ChangeSetService._apply_prepared
    calls = 0

    def interrupt_second(item):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("fixture shutdown between file writes")
        original_apply(item)

    with monkeypatch.context() as patch:
        patch.setattr(ChangeSetService, "_apply_prepared", staticmethod(interrupt_second))
        with pytest.raises(KeyboardInterrupt, match="fixture shutdown"):
            service.apply(ready["change_set_id"], ChangeSetApplyRequest(expected_review_revision=ready["review_revision"]))
    assert calls == 2
    assert any((project / name).read_bytes() != data for name, data in before.items())
    with _client(tmp_path, project) as restarted:
        result = restarted.get(f"/api/v1/change-sets/{ready['change_set_id']}")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "failed"
        assert "restored to the pre-write" in result.json()["error"]
    assert all((project / name).read_bytes() == data for name, data in before.items())
