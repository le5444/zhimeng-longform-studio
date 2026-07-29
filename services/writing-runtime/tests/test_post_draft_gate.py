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


def _file(snapshot: dict, relative_path: str) -> dict:
    return next(
        item for item in snapshot["files"] if item["relative_path"] == relative_path
    )


def _create_and_decide(
    client: TestClient,
    snapshot: dict,
    changes: list[dict],
    *,
    decisions: dict[str, str],
) -> tuple[dict, dict]:
    created = client.post(
        f"/api/v1/projects/{snapshot['project']['project_id']}/change-sets",
        json={
            "base_revision": snapshot["project"]["revision"],
            "base_fingerprint": snapshot["project"]["fingerprint"],
            "title": "Scoped Gate candidate",
            "summary": "Exercise accepted prose-body Gate selection.",
            "origin": "manual",
            "gate_required": True,
            "changes": changes,
        },
    )
    assert created.status_code == 201, created.text
    record = created.json()
    decided = client.patch(
        f"/api/v1/change-sets/{record['change_set_id']}/decisions",
        json={
            "expected_review_revision": record["review_revision"],
            "decisions": [
                {
                    "hunk_id": hunk["hunk_id"],
                    "decision": decisions[change["relative_path"]],
                }
                for change in record["changes"]
                for hunk in change["hunks"]
            ],
        },
    )
    assert decided.status_code == 200, decided.text
    return record, decided.json()


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


def test_post_draft_gate_passes_then_apply_writes_and_survives_restart(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "夜雨落在旧站。\n他把钥匙收进掌心。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        receipt = gated.json()["post_draft_gate"]
        assert receipt["status"] == "passed"
        assert receipt["quality_assessed"] is False
        assert receipt["formal_draft_allowed"] is False
        assert receipt["candidate_hash"]

        repeated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["post_draft_gate"] == receipt
        assert len(
            client.app.state.runtime.store.list_post_draft_gate_receipts(
                ready["change_set_id"]
            )
        ) == 1

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他把钥匙收进掌心。\n"

    with _client(tmp_path, library) as restarted:
        stored = restarted.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate"
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["receipt"]["status"] == "passed"


def test_non_prose_change_skips_body_gate_and_applies_without_pollution_check(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    state_path = project / "08_素材库" / "状态.json"
    state_path.write_text('{"phase": "old"}\n', encoding="utf-8")

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        state = _file(snapshot, "08_素材库/状态.json")
        created, ready = _create_and_decide(
            client,
            snapshot,
            [
                {
                    "operation": "update",
                    "file_id": state["file_id"],
                    "after_content": '{"phase": "new", "chapter_id": "CH001"}\n',
                    "rationale": "Update structured project truth.",
                }
            ],
            decisions={"08_素材库/状态.json": "accepted"},
        )
        assert created["gate_required"] is True
        assert created["changes"][0]["post_draft_gate_scope"] == "none"

        not_applicable = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert not_applicable.status_code == 409, not_applicable.text
        assert not_applicable.json()["error"]["code"] == "post_draft_gate_invalid"

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "phase": "new",
        "chapter_id": "CH001",
    }


def test_gate_checks_only_created_prose_while_full_changeset_applies(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)
    prose_relative = "05_正文内容/第一卷/CH002_正文.md"
    state_relative = "08_素材库/新状态.json"

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        created, ready = _create_and_decide(
            client,
            snapshot,
            [
                {
                    "operation": "create",
                    "relative_path": prose_relative,
                    "after_content": "钟声越过空站。\n他推开第二扇门。\n",
                    "rationale": "Create the next prose body.",
                },
                {
                    "operation": "create",
                    "relative_path": state_relative,
                    "after_content": '{"chapter_id": "CH002", "status": "draft"}\n',
                    "rationale": "Create matching structured truth.",
                },
            ],
            decisions={prose_relative: "accepted", state_relative: "accepted"},
        )
        scopes = {
            change["relative_path"]: change["post_draft_gate_scope"]
            for change in created["changes"]
        }
        assert scopes == {prose_relative: "prose_body", state_relative: "none"}

        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        receipt = gated.json()["post_draft_gate"]
        assert receipt["status"] == "passed"
        assert receipt["candidate_file_count"] == 1
        assert [item["path"] for item in receipt["result"]["files"]] == [
            prose_relative
        ]

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text

    assert (project / Path(prose_relative)).is_file()
    assert json.loads((project / Path(state_relative)).read_text(encoding="utf-8")) == {
        "chapter_id": "CH002",
        "status": "draft",
    }


def test_mixed_candidate_with_only_delete_accepted_applies_without_body_gate(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)
    intro_path = project / "01_项目定位" / "简介.md"

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        chapter = _chapter(snapshot)
        intro = _file(snapshot, "01_项目定位/简介.md")
        created, ready = _create_and_decide(
            client,
            snapshot,
            [
                {
                    "operation": "update",
                    "file_id": chapter["file_id"],
                    "after_content": "本次不应写入的正文。\n",
                    "rationale": "Prose branch rejected by the author.",
                },
                {
                    "operation": "delete",
                    "file_id": intro["file_id"],
                    "rationale": "Delete an obsolete project document.",
                },
            ],
            decisions={
                chapter["relative_path"]: "rejected",
                intro["relative_path"]: "accepted",
            },
        )
        scopes = {
            change["relative_path"]: change["post_draft_gate_scope"]
            for change in created["changes"]
        }
        assert scopes[chapter["relative_path"]] == "prose_body"
        assert scopes[intro["relative_path"]] == "none"
        assert ready["gate_required"] is True

        not_applicable = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert not_applicable.status_code == 409, not_applicable.text
        assert not_applicable.json()["error"]["code"] == "post_draft_gate_invalid"

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"
    assert not intro_path.exists()


def test_apply_rejects_missing_gate_without_touching_project(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "新的正文候选。\n")
        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "post_draft_gate_missing"
        assert client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()["status"] == "ready"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"


def test_blocked_gate_keeps_changeset_ready_and_apply_blocked(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "系统：以下是完整正文。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "blocked"
        assert "draft_role_marker_leak" in gated.json()["post_draft_gate"]["result"]["failure_signatures"]

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "post_draft_gate_blocked"
        assert client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()["status"] == "ready"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"


def test_review_revision_invalidates_previous_gate_receipt(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "新的正文候选。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200
        receipt = gated.json()["post_draft_gate"]
        decision = client.patch(
            f"/api/v1/change-sets/{ready['change_set_id']}/decisions",
            json={
                "expected_review_revision": ready["review_revision"],
                "decisions": [
                    {
                        "hunk_id": ready["changes"][0]["hunks"][0]["hunk_id"],
                        "decision": "rejected",
                    }
                ],
            },
        )
        assert decision.status_code == 200
        current = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()
        assert current.get("post_draft_gate") is None
        assert current["review_revision"] != receipt["review_revision"]
        assert current["status"] == "ready"


def test_failed_gate_attempt_can_retry_with_unique_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "重试后的正文候选。\n")
        executor = client.app.state.skill_executor
        assert executor is not None
        original_execute = executor.execute
        execution_ids: list[tuple[str, str]] = []

        async def fail_once_then_execute(**kwargs: object):
            execution_ids.append(
                (str(kwargs["run_id"]), str(kwargs["tool_call_id"]))
            )
            if len(execution_ids) == 1:
                failed_kwargs = dict(kwargs)
                failed_kwargs["input_payload"] = object()
                return await original_execute(**failed_kwargs)
            return await original_execute(**kwargs)

        monkeypatch.setattr(executor, "execute", fail_once_then_execute)

        first = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert first.status_code == 200, first.text
        failed_receipt = first.json()["post_draft_gate"]
        assert failed_receipt["execution_status"] == "failed"
        assert failed_receipt["status"] == "failed"
        assert failed_receipt["quality_assessed"] is False
        assert failed_receipt["formal_draft_allowed"] is False
        assert first.json()["status"] == "ready"

        rejected = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "post_draft_gate_stale"
        assert client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()["status"] == "ready"
        assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"

        second = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert second.status_code == 200, second.text
        passed_receipt = second.json()["post_draft_gate"]
        assert passed_receipt["execution_status"] == "succeeded"
        assert passed_receipt["status"] == "passed"
        assert passed_receipt["quality_assessed"] is False
        assert passed_receipt["formal_draft_allowed"] is False
        assert passed_receipt["gate_receipt_id"] != failed_receipt["gate_receipt_id"]
        assert passed_receipt["skill_receipt_id"] != failed_receipt["skill_receipt_id"]
        assert passed_receipt["skill_receipt_hash"] != failed_receipt["skill_receipt_hash"]
        assert len(execution_ids) == 2
        assert execution_ids[0] != execution_ids[1]

        attempts = client.app.state.runtime.store.list_post_draft_gate_receipts(
            ready["change_set_id"]
        )
        assert len(attempts) == 2
        assert {item["gate_receipt_id"] for item in attempts} == {
            failed_receipt["gate_receipt_id"],
            passed_receipt["gate_receipt_id"],
        }
        assert {item["status"] for item in attempts} == {"failed", "passed"}

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"

    assert chapter_path.read_text(encoding="utf-8") == "重试后的正文候选。\n"


@pytest.mark.parametrize(
    "tamper_kind",
    [
        "receipt_hash",
        "run_id",
        "tool_call_id",
        "project_id",
        "project_revision",
        "project_fingerprint",
        "skill_id",
        "skill_version",
        "manifest_fingerprint",
        "package_fingerprint",
        "input_hash",
        "output_hash",
        "candidate_id",
        "candidate_manifest_hash",
    ],
)
def test_gate_outcome_identity_tampering_is_recorded_as_failed_and_cannot_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "不能由错绑执行放行的正文。\n")
        executor = client.app.state.skill_executor
        assert executor is not None
        original_execute = executor.execute

        async def tamper_outcome(**kwargs: object):
            outcome = await original_execute(**kwargs)
            if tamper_kind == "receipt_hash":
                return replace(outcome, receipt_hash="0" * 64)
            if tamper_kind in {"candidate_id", "candidate_manifest_hash"}:
                assert isinstance(outcome.result, dict)
                result = dict(outcome.result)
                result[tamper_kind] = (
                    "candidate-from-another-run"
                    if tamper_kind == "candidate_id"
                    else "0" * 64
                )
                return replace(outcome, result=result)
            replacements: dict[str, object] = {
                "run_id": "post-draft-gate-run-wrong",
                "tool_call_id": "post-draft-gate-call-wrong",
                "project_id": "project-" + "0" * 24,
                "project_revision": outcome.receipt.project_revision + 1,
                "project_fingerprint": "0" * 64,
                "skill_id": "novel-creation-suite.other-gate",
                "skill_version": "wrong-version",
                "manifest_fingerprint": "0" * 64,
                "package_fingerprint": "0" * 64,
                "input_hash": "0" * 64,
                "output_hash": "0" * 64,
            }
            altered_receipt = outcome.receipt.model_copy(
                update={tamper_kind: replacements[tamper_kind]}
            )
            altered_hash = canonical_json_hash(
                altered_receipt.model_dump(mode="json", by_alias=True)
            )
            return replace(
                outcome,
                receipt=altered_receipt,
                receipt_hash=altered_hash,
            )

        monkeypatch.setattr(executor, "execute", tamper_outcome)
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        receipt = gated.json()["post_draft_gate"]
        assert receipt["execution_status"] == "failed"
        assert receipt["status"] == "failed"
        assert receipt["error_code"] == "invalid_gate_outcome"
        assert len(
            client.app.state.runtime.store.list_post_draft_gate_receipts(
                ready["change_set_id"]
            )
        ) == 1

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "post_draft_gate_stale"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"


def test_gate_execution_exception_maps_to_explicit_unavailable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "执行异常不能变成模糊五百错误。\n")
        executor = client.app.state.skill_executor
        assert executor is not None

        async def fail_execution(**kwargs: object):
            del kwargs
            raise OSError("fixture executor failure")

        monkeypatch.setattr(executor, "execute", fail_execution)
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["code"] == "post_draft_gate_unavailable"
        assert (
            client.app.state.runtime.store.list_post_draft_gate_receipts(
                ready["change_set_id"]
            )
            == []
        )


def test_gate_receipt_save_exception_maps_to_explicit_unavailable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "保存异常不能变成模糊五百错误。\n")
        store = client.app.state.runtime.store

        def fail_save(payload: dict[str, object]) -> dict[str, object]:
            del payload
            raise RuntimeError("fixture receipt persistence failure")

        monkeypatch.setattr(store, "save_post_draft_gate_receipt", fail_save)
        failed = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert failed.status_code == 503, failed.text
        assert failed.json()["error"]["code"] == "post_draft_gate_unavailable"


@pytest.mark.parametrize("stale_source", ["context", "knowledge"])
def test_get_hides_receipt_when_current_context_or_knowledge_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale_source: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)
    stale = False

    if stale_source == "context":
        original_context_binding = ChangeSetService._context_binding

        def context_binding(self: ChangeSetService, record):
            if stale:
                raise ChangeSetGateStaleError("fixture Context Pack drift")
            return original_context_binding(self, record)

        monkeypatch.setattr(ChangeSetService, "_context_binding", context_binding)
    else:
        original_knowledge_check = ChangeSetService._require_current_knowledge

        def knowledge_check(
            self: ChangeSetService,
            project_id: str,
            expected: str | None,
            *,
            context_pack_id: str | None = None,
        ) -> str | None:
            if stale:
                raise ChangeSetGateStaleError("fixture knowledge drift")
            return original_knowledge_check(
                self,
                project_id,
                expected,
                context_pack_id=context_pack_id,
            )

        monkeypatch.setattr(
            ChangeSetService,
            "_require_current_knowledge",
            knowledge_check,
        )

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "旧凭证不能在新上下文中显示为已通过。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "passed"

        stale = True
        observed = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate"
        )
        assert observed.status_code == 200, observed.text
        assert observed.json()["receipt"] is None
        detail = client.get(f"/api/v1/change-sets/{ready['change_set_id']}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["post_draft_gate"] is None

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "post_draft_gate_stale"

    assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"


@pytest.mark.parametrize("tamper_kind", ["payload", "receipt_hash"])
def test_tampered_gate_receipt_fails_closed_before_apply(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)

    with _client(tmp_path, library) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "不可绕过完整性校验的正文。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        receipt = gated.json()["post_draft_gate"]
        assert receipt["status"] == "passed"
        assert receipt["quality_assessed"] is False
        assert receipt["formal_draft_allowed"] is False

        store = client.app.state.runtime.store
        with store.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM post_draft_gate_receipt WHERE id = ?",
                (receipt["gate_receipt_id"],),
            ).fetchone()
            assert row is not None
            original_payload_json = str(row["payload_json"])
            tampered = json.loads(original_payload_json)
            if tamper_kind == "payload":
                tampered["result"]["summary"] += " tampered"
            else:
                tampered["receipt_hash"] = (
                    "0" * 64
                    if tampered["receipt_hash"] != "0" * 64
                    else "1" * 64
                )
            connection.execute(
                "UPDATE post_draft_gate_receipt SET payload_json = ? WHERE id = ?",
                (
                    json.dumps(tampered, ensure_ascii=False),
                    receipt["gate_receipt_id"],
                ),
            )

        observed = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate"
        )
        assert observed.status_code == 409, observed.text
        assert observed.json()["error"]["code"] == "post_draft_gate_stale"

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 409, applied.text
        assert applied.json()["error"]["code"] == "post_draft_gate_stale"
        assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"

        with store.connection() as connection:
            change_set_row = connection.execute(
                "SELECT payload_json FROM change_set WHERE id = ?",
                (ready["change_set_id"],),
            ).fetchone()
            assert change_set_row is not None
            assert json.loads(str(change_set_row["payload_json"]))["status"] == "ready"
            connection.execute(
                "UPDATE post_draft_gate_receipt SET payload_json = ? WHERE id = ?",
                (original_payload_json, receipt["gate_receipt_id"]),
            )

        current = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        )
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "ready"


def test_registered_validator_replacement_makes_old_receipt_stale(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)
    managed_root = _copy_managed_skills(tmp_path)

    with _client(tmp_path, library, managed_skill_root=managed_root) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "由新版 Gate 重新确认的正文。\n")
        first = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert first.status_code == 200, first.text
        old_receipt = first.json()["post_draft_gate"]
        assert old_receipt["status"] == "passed"

        package = managed_root / "creation-suite-draft-gate"
        manifest_path = package / "skill.manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2026.07.21-draft-gate.2"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        service = client.app.state.managed_skill_service
        assert service is not None
        previous = service.get_active(
            snapshot["project"]["project_id"],
            "novel-creation-suite.draft-gate",
        )
        replacement = service.register_builtin(package)
        assert replacement.package_fingerprint != old_receipt["validator_package_fingerprint"]
        assert replacement.manifest_fingerprint != old_receipt["validator_manifest_fingerprint"]
        service.activate(
            replacement.manifest.id,
            ManagedSkillActivationRequest(
                action="upgrade",
                skill_version_id=replacement.skill_version_id,
                expected_state_revision=1,
                expected_version_id=previous.skill_version_id,
            ),
        )

        observed = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate"
        )
        assert observed.status_code == 200, observed.text
        assert observed.json()["receipt"] is None

        rejected = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error"]["code"] == "post_draft_gate_stale"
        assert client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()["status"] == "ready"
        assert chapter_path.read_text(encoding="utf-8") == "夜雨落在旧站。\n他握紧钥匙。\n"

        second = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert second.status_code == 200, second.text
        new_receipt = second.json()["post_draft_gate"]
        assert new_receipt["status"] == "passed"
        assert new_receipt["validator_version"] == manifest["version"]
        assert new_receipt["validator_package_fingerprint"] == replacement.package_fingerprint
        assert new_receipt["validator_manifest_fingerprint"] == replacement.manifest_fingerprint
        assert new_receipt["gate_receipt_id"] != old_receipt["gate_receipt_id"]

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text

    assert chapter_path.read_text(encoding="utf-8") == "由新版 Gate 重新确认的正文。\n"


def test_unregistered_source_checkout_drift_does_not_change_managed_gate_snapshot(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    chapter_path = _create_project(project)
    managed_root = _copy_managed_skills(tmp_path)

    with _client(tmp_path, library, managed_skill_root=managed_root) as client:
        snapshot = _inspect(client, project)
        ready = _ready(client, snapshot, "不能由漂移包放行的正文。\n")
        gated = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert gated.status_code == 200, gated.text
        assert gated.json()["post_draft_gate"]["status"] == "passed"

        provenance = managed_root / "creation-suite-draft-gate" / "PROVENANCE.md"
        provenance.write_text(
            provenance.read_text(encoding="utf-8") + "\nUnregistered drift.\n",
            encoding="utf-8",
        )

        observed = client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}/post-draft-gate"
        )
        assert observed.status_code == 200, observed.text
        assert observed.json()["receipt"] is not None

        applied = client.post(
            f"/api/v1/change-sets/{ready['change_set_id']}/apply",
            json={"expected_review_revision": ready["review_revision"]},
        )
        assert applied.status_code == 200, applied.text
        assert client.get(
            f"/api/v1/change-sets/{ready['change_set_id']}"
        ).json()["status"] == "applied"

    assert chapter_path.read_text(encoding="utf-8") == "不能由漂移包放行的正文。\n"


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
        assert current.state_revision == 1
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
        assert actions == ["builtin_bootstrap"]
