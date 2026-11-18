"""新建技能把实际方法一次存好；用隔离目录验证 HTTP、磁盘和模型按需读取。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_correction_p0_20260926 import _app, _turn
from test_skill_folders_20260926 import CatalogRecordingProvider
from zhimeng_runtime.user_skill_api import create_user_skill_router
from zhimeng_runtime.user_skill_folder_service import MAX_USER_SKILL_MD_BYTES, UserSkillFolderService


def _client(tmp_path: Path) -> tuple[TestClient, UserSkillFolderService]:
    service = UserSkillFolderService(tmp_path / "skills", tmp_path / "state")
    app = FastAPI()
    app.include_router(create_user_skill_router(service))
    return TestClient(app), service


def test_create_then_discover_and_read_body_in_a_real_agent_run(tmp_path: Path, monkeypatch) -> None:
    skill_root = tmp_path / "skills"
    monkeypatch.setenv("ZHIMENG_USER_SKILLS_ROOT", str(skill_root))
    monkeypatch.setenv("ZHIMENG_USER_SKILL_EXTRA_ROOTS", "")
    body = "# 整理方法\n\n读取工作记录，逐条保留原始来源。\n\n输出分成已完成、进行中和待解决三栏。\n"
    provider = CatalogRecordingProvider([("skill_read_file", {"skill": "周报整理", "path": "SKILL.md"})])
    with TestClient(_app(tmp_path, provider)) as client:
        # 已存在的对话也能在下一轮发现新技能，无需先创建一个空模板再编辑。
        session = client.post("/api/v1/agent/sessions", json={"title": "整理工作记录"}).json()
        created = client.post("/api/v1/user-skills", json={
            "name": "周报整理", "description": "整理工作记录时使用", "content": body,
        })
        assert created.status_code == 200, created.text
        skill = created.json()["skills"]["skills"][0]
        assert skill["name"] == "周报整理" and skill["enabled"] is True
        saved = (skill_root / "周报整理" / "SKILL.md").read_bytes().decode("utf-8")
        assert saved.endswith(body)
        assert "请在这里写清楚" not in saved
        read = client.get("/api/v1/user-skills/周报整理/instructions")
        assert read.status_code == 200
        assert read.json()["content"] == saved and read.json()["editable"] is True
        run = _turn(client, session["session_id"], "按周报整理技能阅读使用方法。", "repair_only", "skill-create-body")
        assert run["status"] == "succeeded", run
        assert "周报整理: 整理工作记录时使用" in provider.tool_descriptions[0]["skill_read_file"]
        assert "逐条保留原始来源" not in str(provider.requests[0])  # 正文没有常驻灌进开头。
        results = [message["content"] for request in provider.requests for message in request if message.get("role") == "tool"]
        assert results and "逐条保留原始来源" in str(results)
        assert "输出分成已完成、进行中和待解决三栏" in str(results)

        changed = saved.replace("三栏", "三栏并标明日期")
        assert client.put("/api/v1/user-skills/周报整理/instructions", json={"content": changed}).status_code == 200
        assert client.get("/api/v1/user-skills/周报整理/instructions").json()["content"] == changed
        assert client.post("/api/v1/user-skills/周报整理/disable").status_code == 200
        assert client.get("/api/v1/user-skills").json()["skills"][0]["enabled"] is False
        assert client.post("/api/v1/user-skills/周报整理/enable").status_code == 200


@pytest.mark.parametrize("body", ["", " \n\t "])
def test_supplied_empty_body_is_rejected_before_any_folder_is_created(tmp_path: Path, body: str) -> None:
    client, service = _client(tmp_path)
    with client:
        response = client.post("/api/v1/user-skills", json={"name": "empty", "description": "用途", "content": body})
    assert response.status_code == 422
    assert service.scan() == []
    assert not (service.skills_root / "empty").exists()


def test_old_api_template_and_handwritten_skills_keep_their_enabled_behavior(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    with client:
        old = client.post("/api/v1/user-skills", json={"name": "legacy", "description": "旧调用"})
        assert old.status_code == 200
        assert old.json()["skills"]["skills"][0]["enabled"] is True
        assert "# 使用方法" in client.get("/api/v1/user-skills/legacy/instructions").json()["content"]
        body = "统计 CSV 每列的空值和重复项，结果保留列名。"
        new = client.post("/api/v1/user-skills", json={"name": "csv-check", "description": "检查表格", "content": body})
        assert new.status_code == 200
        assert {item.name for item in service.menu()} == {"legacy", "csv-check"}
        schema = client.get("/openapi.json").json()["components"]["schemas"]["CreateSkillRequest"]
        assert "content" in schema["properties"]
        assert "content" not in schema["required"]


def test_duplicate_creation_never_overwrites_a_saved_skill(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    with client:
        first = {"name": "weekly", "description": "第一版", "content": "读取本周记录。"}
        assert client.post("/api/v1/user-skills", json=first).status_code == 200
        saved = (service.skills_root / "weekly" / "SKILL.md").read_bytes()
        second = client.post("/api/v1/user-skills", json={**first, "content": "另一份正文"})
        assert second.status_code == 409
        assert (service.skills_root / "weekly" / "SKILL.md").read_bytes() == saved


def test_failed_atomic_create_leaves_no_template_and_can_be_retried(tmp_path: Path, monkeypatch) -> None:
    from zhimeng_runtime import user_skill_folder_service as module

    client, service = _client(tmp_path)
    original = module.replace_with_retry

    def fail(*_args):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module, "replace_with_retry", fail)
    payload = {"name": "weekly", "description": "整理周报", "content": "读取本周记录。"}
    with client:
        assert client.post("/api/v1/user-skills", json=payload).status_code == 409
        assert service.scan() == []
        assert not (service.skills_root / "weekly").exists()
        monkeypatch.setattr(module, "replace_with_retry", original)
        assert client.post("/api/v1/user-skills", json=payload).status_code == 200


def test_file_too_large_for_existing_scanner_is_rejected_before_creation(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    with client:
        response = client.post("/api/v1/user-skills", json={
            "name": "large", "description": "大小一致性", "content": "汉" * (MAX_USER_SKILL_MD_BYTES // 3),
        })
    assert response.status_code == 409
    assert not (service.skills_root / "large").exists()
