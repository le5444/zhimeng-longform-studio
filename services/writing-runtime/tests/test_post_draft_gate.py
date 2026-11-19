from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.change_set_service import (
    ChangeSetGateStaleError,
    ChangeSetService,
)
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings
from zhimeng_runtime.post_draft_gate_models import canonical_json_hash
from zhimeng_runtime.project_reader import SECTION_LABELS
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest


def _create_project(root: Path) -> Path:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    chapter = root / "05_正文内容" / "第一卷" / "CH001_正文.md"
    chapter.parent.mkdir()
    chapter.write_text("夜雨落在旧站。\n他握紧钥匙。\n", encoding="utf-8")
    (root / "01_项目定位" / "简介.md").write_text("一个长篇项目。\n", encoding="utf-8")
    return chapter


def _client(
    tmp_path: Path,
    library: Path,
    *,
    managed_skill_root: Path | None = None,
) -> TestClient:
    settings: dict[str, object] = {
        "environment": "test",
        "database_path": tmp_path / "runtime.sqlite3",
        "trusted_project_roots": (library,),
        "allowed_origins": (),
    }
    if managed_skill_root is not None:
        settings["managed_skill_root"] = managed_skill_root
    return TestClient(
        create_app(RuntimeSettings(**settings))
    )


def _copy_managed_skills(tmp_path: Path) -> Path:
    managed_root = tmp_path / "managed-skills"
    shutil.copytree(DEFAULT_MANAGED_SKILL_ROOT, managed_root)
    return managed_root


def _inspect(client: TestClient, project: Path) -> dict:
    response = client.post(
        "/api/v1/projects/inspect",
        json={"root_path": str(project)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _chapter(snapshot: dict) -> dict:
    return next(
        item
        for item in snapshot["files"]
        if item["relative_path"].endswith("CH001_正文.md")
    )


def _ready(client: TestClient, snapshot: dict, after: str) -> dict:
    chapter = _chapter(snapshot)
    created = client.post(
        f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
        json={
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "title": "Gate candidate",
            "summary": "Exercise the post-draft candidate boundary.",
            "origin": "manual",
            "gate_required": True,
            "changes": [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": after,
                    "rationale": "Gate integration test.",
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["changes"][0]["post_draft_gate_scope"] == "prose_body"
    hunks = [hunk for change in record["changes"] for hunk in change["hunks"]]
    decided = client.patch(
        f"/api/v1/change-sets/{record['change_set_id']}/decisions",
        json={
            "expected_review_revision": record["review_revision"],
            "decisions": [
                {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                for hunk in hunks
            ],
        },
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "ready"
    return decided.json()


@pytest.mark.parametrize("apply_route", ["apply", "apply-direct"])
def test_legacy_gate_required_proposal_saves_without_running_the_check(
    tmp_path: Path, apply_route: str,
) -> None:
    """保存前的写作检查已退役（2026-09-25）：老修改记录上残留的「要检查」标记不再卡住保存。

    作者库里有 6 份 8—9 月的待确认修改带着这个标记；检查器 9-24 起不再装进服务，
    以前这几份跑不了检查、也就永远保存不了，只能放弃。
    """
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter = _create_project(project)
    with _client(tmp_path, library) as client:
        ready = _ready(client, _inspect(client, project), "夜雨落在旧站。\n他握紧了那把铜钥匙。\n")
        assert ready["gate_required"] is True
        assert ready["post_draft_gate"] is None
        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/{apply_route}",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
    assert chapter.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧了那把铜钥匙。\n"


def test_lifespan_discovers_new_gate_without_overwriting_legacy_activation(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    managed_root = _copy_managed_skills(tmp_path)
    gate_package = managed_root / "creation-suite-draft-gate"
    manifest_path = gate_package / "skill.manifest.json"
    legacy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["version"] = "2026.07.22-draft-gate.1"
    legacy_manifest.pop("capabilities", None)
    manifest_path.write_text(
        json.dumps(legacy_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with _client(tmp_path, library, managed_skill_root=managed_root) as client:
        snapshot = _inspect(client, project)
        project_id = snapshot["project"]["project_id"]
        service = client.app.state.managed_skill_service
        assert service is not None
        legacy = service.get_active_declaration(
            project_id,
            "novel-creation-suite.draft-gate",
        )
        assert legacy.manifest.version == "2026.07.22-draft-gate.1"
        assert legacy.manifest.capabilities == []
        legacy_version_id = legacy.skill_version_id
        # 作者显式把旧版 Gate 固定下来（禁用再启用会留下作者收据）。
        # 未被作者动过的捆绑默认激活会在下次启动发现新内置包时自动跟进，
        # 这个测试保护的正是"作者显式选择不被启动发现覆盖"。
        service.activate(
            "novel-creation-suite.draft-gate",
            ManagedSkillActivationRequest(
                action="disable",
                skill_version_id=None,
                expected_state_revision=1,
                expected_version_id=legacy_version_id,
            ),
        )
        service.activate(
            "novel-creation-suite.draft-gate",
            ManagedSkillActivationRequest(
                action="enable",
                skill_version_id=legacy_version_id,
                expected_state_revision=2,
                expected_version_id=None,
            ),
        )

    shutil.rmtree(gate_package)
    shutil.copytree(
        DEFAULT_MANAGED_SKILL_ROOT / "creation-suite-draft-gate",
        gate_package,
    )

    with _client(tmp_path, library, managed_skill_root=managed_root) as client:
        snapshot = _inspect(client, project)
        service = client.app.state.managed_skill_service
        assert service is not None
        still_active = service.get_active_declaration(
            snapshot["project"]["project_id"],
            "novel-creation-suite.draft-gate",
        )
        assert still_active.skill_version_id == legacy_version_id
        assert still_active.manifest.capabilities == []

        lifecycle = service.list_lifecycle()
        gate_versions = [
            item
            for item in lifecycle.versions
            if item.manifest.id == "novel-creation-suite.draft-gate"
        ]
        assert {item.manifest.version for item in gate_versions} == {
            "2026.07.22-draft-gate.1",
            "2026.07.23-draft-gate.2",
        }
        current = next(
            item
            for item in lifecycle.activations
            if item.skill_id == "novel-creation-suite.draft-gate"
        )
        assert current.state_revision == 3
        assert current.effective_version_id == legacy_version_id

        with client.app.state.runtime.store.connection() as connection:
            actions = [
                str(row["action"])
                for row in connection.execute(
                    """
                    SELECT action
                    FROM skill_activation_receipt
                    WHERE skill_id = ?
                    ORDER BY state_revision
                    """,
                    ("novel-creation-suite.draft-gate",),
                ).fetchall()
            ]
        # 第二次启动发现新内置包后没有追加任何收据——作者的显式选择原样保留。
        assert actions == ["builtin_bootstrap", "disable", "enable"]
