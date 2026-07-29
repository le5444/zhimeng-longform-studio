from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings
from zhimeng_runtime.local_source_adapter import ManagedLocalSourceAdapter
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest
from zhimeng_runtime.source_import_receipt_models import (
    LocalSourceImportExecutionReceipt,
)

from test_context_agent_e2e import (
    ToolCallingProvider,
    _start_tool_run,
    _wait_for_terminal,
    _write_project,
)
from test_knowledge_api import (
    _reviewed_validator_card,
    _reviewed_voice_card,
    _reviewed_work_card,
)
from local_source_fixture import (
    DISTILLATION_OUTPUT_CONTRACT,
    DRAFT_ARTIFACT_KINDS,
    EXPECTED_CARD_TYPE_SET,
    EXPECTED_WORKFLOW_SKILL_IDS,
    PREPARE_OUTPUT_CONTRACT,
    SNAPSHOT_ROOT,
    SOURCE_ARTIFACT_KINDS,
    SOURCE_CHAPTER_MARKER_COUNT,
    SOURCE_FIXTURE,
    SOURCE_SHA256,
    SOURCE_SIZE_BYTES,
    WRITER_OUTPUT_CONTRACT,
    assert_fixture_manifest_static_contract,
)
from test_local_source_e2e import _wait_for_terminal_source_status


@pytest.mark.parametrize("legacy_gate", [False, True])
def test_real_local_txt_reaches_reviewed_writer_apply_and_rollback(
    tmp_path: Path,
    legacy_gate: bool,
) -> None:
    assert_fixture_manifest_static_contract()
    source_before = SOURCE_FIXTURE.read_bytes()
    assert len(source_before) == SOURCE_SIZE_BYTES
    assert hashlib.sha256(source_before).hexdigest() == SOURCE_SHA256

    library = tmp_path / "library"
    library.mkdir()
    project = library / "novel"
    _write_project(project)
    chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
    original_chapter = chapter_path.read_bytes()

    managed_skill_root = DEFAULT_MANAGED_SKILL_ROOT
    if legacy_gate:
        managed_skill_root = tmp_path / "managed-skills"
        shutil.copytree(DEFAULT_MANAGED_SKILL_ROOT, managed_skill_root)
        manifest_path = (
            managed_skill_root
            / "creation-suite-draft-gate"
            / "skill.manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "2026.07.22-draft-gate.1"
        manifest.pop("capabilities", None)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    provider = ToolCallingProvider()
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        trusted_project_roots=(library,),
        trusted_source_roots=(SOURCE_FIXTURE.parent.resolve(),),
        managed_skill_root=managed_skill_root,
        allowed_origins=(),
        sse_heartbeat_seconds=0.05,
    )
    source_adapter = ManagedLocalSourceAdapter(
        snapshot_root=SNAPSHOT_ROOT,
        state_root=tmp_path / "source-imports",
        trusted_source_roots=(SOURCE_FIXTURE.parent.resolve(),),
    )
    app = create_app(
        settings,
        local_source_adapter=source_adapter,
        agent_provider=provider,
    )

    with TestClient(app) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        snapshot = inspection.json()
        project_id = snapshot["project"]["project_id"]
        knowledge_path = f"/api/v1/projects/{project_id}/knowledge"

        inspected_source = client.post(
            "/api/v1/source-imports/local/inspect",
            json={"source_path": str(SOURCE_FIXTURE.resolve())},
        )
        assert inspected_source.status_code == 200, inspected_source.text
        source_candidate = inspected_source.json()
        assert source_candidate["source_sha256"] == SOURCE_SHA256
        assert source_candidate["source_size_bytes"] == SOURCE_SIZE_BYTES
        assert source_candidate["source_format"] == PREPARE_OUTPUT_CONTRACT[
            "source_format"
        ]

        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "local_file",
                "query": source_candidate["source_path"],
                "candidate": source_candidate,
            },
        )
        assert created.status_code == 202, created.text
        source_import_id = created.json()["import_id"]
        source_job = _wait_for_terminal_source_status(client, source_import_id)
        assert source_job["status"] == "awaiting_review", source_job
        source_packet = source_job["source_packet"]
        assert source_packet["source_backend"] == PREPARE_OUTPUT_CONTRACT[
            "source_backend"
        ]
        assert source_packet["source_format"] == PREPARE_OUTPUT_CONTRACT[
            "source_format"
        ]
        assert source_packet["download_scope"] == PREPARE_OUTPUT_CONTRACT[
            "download_scope"
        ]
        assert source_packet["chapter_marker_count"] == SOURCE_CHAPTER_MARKER_COUNT
        assert source_packet["source_file_size_bytes"] == SOURCE_SIZE_BYTES
        for field_name in PREPARE_OUTPUT_CONTRACT["sha256_binding_fields"]:
            assert source_packet[field_name] == SOURCE_SHA256
        assert SOURCE_ARTIFACT_KINDS <= {
            item["kind"] for item in source_packet["artifacts"]
        }

        source_receipt_response = client.get(
            f"/api/v1/source-imports/{source_import_id}/execution-receipt"
        )
        assert source_receipt_response.status_code == 200, source_receipt_response.text
        source_receipt = LocalSourceImportExecutionReceipt.model_validate(
            source_receipt_response.json()["receipt"]
        )
        assert source_receipt.envelope_schema == DISTILLATION_OUTPUT_CONTRACT[
            "envelope_schema"
        ]
        assert source_receipt.envelope_status == DISTILLATION_OUTPUT_CONTRACT["status"]
        assert source_receipt.card_count == len(EXPECTED_CARD_TYPE_SET)
        assert SOURCE_ARTIFACT_KINDS <= {
            item.kind for item in source_receipt.source_artifacts
        }
        assert DRAFT_ARTIFACT_KINDS <= {
            item.kind for item in source_receipt.draft_artifacts
        }

        imported = client.post(
            f"{knowledge_path}/source-imports/{source_import_id}/candidates"
        )
        assert imported.status_code == 201, imported.text
        candidates = imported.json()["candidates"]
        assert len(candidates) == len(EXPECTED_CARD_TYPE_SET)
        assert {
            candidate["card_type"] for candidate in candidates
        } == EXPECTED_CARD_TYPE_SET
        for candidate in candidates:
            assert candidate["source_import_id"] == source_import_id
            assert candidate["status"] == "pending"
            assert candidate["lineage_status"] == "verified"
            assert candidate["card"]["draft_candidate"] is True
            assert candidate["card"]["requires_human_distillation"] is True
            assert candidate["source_draft_hash"] == source_receipt.cards_draft_hash
            assert candidate["origin"]["source_receipt_hash"] == source_receipt.receipt_hash
            assert candidate["origin"]["source_packet_fingerprint"] == (
                source_receipt.source_packet_fingerprint
            )
            assert candidate["origin"]["draft_fingerprint"] == (
                source_receipt.draft_fingerprint
            )

        reviewed_card_builders = {
            "work": _reviewed_work_card,
            "voice": _reviewed_voice_card,
            "validator": _reviewed_validator_card,
        }
        assert set(reviewed_card_builders) == EXPECTED_CARD_TYPE_SET
        for candidate in candidates:
            reviewed = client.put(
                f"{knowledge_path}/candidates/{candidate['candidate_id']}",
                json={
                    "expected_revision": candidate["revision"],
                    "expected_content_hash": candidate["content_hash"],
                    "card": reviewed_card_builders[candidate["card_type"]](
                        candidate["card_id"]
                    ),
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            reviewed_candidate = reviewed.json()
            assert reviewed_candidate["issues"] == []
            assert reviewed_candidate["card"]["draft_candidate"] is False
            assert reviewed_candidate["card"]["requires_human_distillation"] is False

            promoted = client.post(
                f"{knowledge_path}/candidates/{candidate['candidate_id']}/promote",
                json={
                    "expected_revision": reviewed_candidate["revision"],
                    "expected_content_hash": reviewed_candidate["content_hash"],
                    "replace_active": False,
                },
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["active_version"]["card"]["card_type"] == (
                candidate["card_type"]
            )

        terminal_candidates = client.get(
            f"{knowledge_path}/candidates",
            params={"source_import_id": source_import_id},
        )
        assert terminal_candidates.status_code == 200, terminal_candidates.text
        assert {item["status"] for item in terminal_candidates.json()["candidates"]} == {
            "promoted"
        }

        finalized = client.post(
            f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
            json={
                "expected_source_receipt_hash": source_receipt.receipt_hash,
                "expected_draft_hash": source_receipt.cards_draft_hash,
                "expected_candidate_set_hash": terminal_candidates.json()[
                    "candidate_set_hash"
                ],
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert {item["decision"] for item in finalized.json()["decisions"]} == {
            "promoted"
        }
        assert client.get(f"/api/v1/source-imports/{source_import_id}").json()[
            "status"
        ] == "completed"

        scoped_cards_response = client.get(f"{knowledge_path}/cards")
        assert scoped_cards_response.status_code == 200, scoped_cards_response.text
        scoped_cards = scoped_cards_response.json()
        assert {
            item["active_version"]["card"]["card_type"]
            for item in scoped_cards["cards"]
        } == EXPECTED_CARD_TYPE_SET
        for card in scoped_cards["cards"]:
            active_version = card["active_version"]
            assert active_version["formalization_status"] == "verified"
            assert active_version["lineage_status"] == "verified"
            assert active_version["lineage"]["source_import_id"] == source_import_id

        objective = (
            "玄幻章节要用人物压力和自然叙事完成阶段兑现，保留 blood-stained key，"
            "并检查来源污染。"
        )
        run_id, pack = _start_tool_run(
            client,
            snapshot,
            objective=objective,
            key_prefix="real-local-source-writer-0001",
            writer_mode="author_review_change_proposal",
        )
        assert pack["knowledge_fingerprint"] == scoped_cards["fingerprint"]
        included_types = {
            item["source_type"].removeprefix("native_knowledge_card:")
            for item in pack["included"]
            if item["source_type"].startswith("native_knowledge_card:")
        }
        assert included_types == EXPECTED_CARD_TYPE_SET

        route_response = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}/nlcl-route"
        )
        assert route_response.status_code == 200, route_response.text
        route = route_response.json()
        assert route["source"] == "native_knowledge"
        assert route["knowledge_fingerprint"] == scoped_cards["fingerprint"]
        assert {
            item["card_type"] for item in route["selected_cards"]
        } == EXPECTED_CARD_TYPE_SET
        assert all(
            item["knowledge_provenance"]["source_import_id"] == source_import_id
            for item in route["selected_cards"]
        )

        change_sets = app.state.agent_service.tool_executor.change_sets
        (
            resolved_route_receipt_id,
            resolved_route_receipt_hash,
            validator_bundle,
        ) = change_sets.knowledge_validator_resolver(
            project_id,
            pack["context_pack_id"],
            pack["pack_hash"],
            scoped_cards["fingerprint"],
        )
        assert resolved_route_receipt_id == route["route_receipt_id"]
        assert resolved_route_receipt_hash == route["receipt_hash"]
        assert validator_bundle is not None
        assert validator_bundle.context_pack_id == pack["context_pack_id"]
        assert validator_bundle.context_pack_hash == pack["pack_hash"]
        assert validator_bundle.knowledge_fingerprint == scoped_cards["fingerprint"]
        assert len(validator_bundle.validators) == 1
        validator_binding = validator_bundle.validators[0]
        validator_route_card = next(
            item for item in route["selected_cards"] if item["card_type"] == "validator"
        )
        assert validator_binding.card_id == validator_route_card["card_id"]
        assert validator_binding.version_id == validator_route_card["version_id"]
        assert validator_binding.content_hash == validator_route_card["content_hash"]
        assert validator_binding.provenance.model_dump(mode="json", by_alias=True) == (
            validator_route_card["knowledge_provenance"]
        )
        assert [rule.rule_id for rule in validator_binding.rules] == [
            "forbid_source_terms",
            "require_visible_pressure",
        ]
        assert validator_binding.configuration_errors == ()

        runtime_propose_tool_id = app.state.agent_service._runtime_tool_call_id(
            run_id,
            "call_propose_change_0003",
        )
        terminal_run = _wait_for_terminal(client, run_id)
        assert terminal_run["status"] == "succeeded", terminal_run
        assert chapter_path.read_bytes() == original_chapter

        workflow_response = client.get(f"/api/v1/agent/runs/{run_id}/workflow")
        assert workflow_response.status_code == 200, workflow_response.text
        workflow = workflow_response.json()
        assert [
            step["skill_id"] for step in workflow["plan"]["steps"]
        ] == list(EXPECTED_WORKFLOW_SKILL_IDS)
        assert len(workflow["checkpoints"]) == 3
        assert {item["execution_status"] for item in workflow["checkpoints"]} == {
            "succeeded"
        }
        assert workflow["plan"]["knowledge_fingerprint"] == scoped_cards["fingerprint"]
        assert workflow["provider_boundary"] is not None
        assert workflow["formal_draft_allowed"] is WRITER_OUTPUT_CONTRACT[
            "formal_draft_allowed"
        ]

        writer_task_response = client.get(
            f"/api/v1/agent/runs/{run_id}/writer-task"
        )
        assert writer_task_response.status_code == 200, writer_task_response.text
        writer_task = writer_task_response.json()
        assert writer_task["run_id"] == run_id
        assert writer_task["workflow_plan_id"] == workflow["plan"]["workflow_plan_id"]
        assert writer_task["workflow_plan_hash"] == workflow["plan"]["plan_hash"]
        assert writer_task["context_pack_id"] == pack["context_pack_id"]
        assert writer_task["context_pack_hash"] == pack["pack_hash"]
        assert writer_task["project_fingerprint"] == pack["project_fingerprint"]
        assert writer_task["knowledge_fingerprint"] == scoped_cards["fingerprint"]
        assert set(writer_task["selected_card_ids"]) == {
            item["card_id"] for item in route["selected_cards"]
        }
        assert writer_task["mode"] == "author_review_change_proposal"
        assert writer_task["formal_draft_allowed"] is WRITER_OUTPUT_CONTRACT[
            "formal_draft_allowed"
        ]
        assert writer_task["writes_project_truth"] is WRITER_OUTPUT_CONTRACT[
            "writes_project_truth"
        ]
        assert writer_task["requires_author_review"] is WRITER_OUTPUT_CONTRACT[
            "requires_author_review"
        ]

        writer_result_response = client.get(
            f"/api/v1/agent/runs/{run_id}/writer-candidate-result"
        )
        assert writer_result_response.status_code == 200, writer_result_response.text
        writer_result = writer_result_response.json()
        assert writer_result["run_id"] == run_id
        assert writer_result["project_id"] == project_id
        assert writer_result["project_fingerprint"] == pack["project_fingerprint"]
        assert writer_result["writer_task_id"] == writer_task["writer_task_id"]
        assert writer_result["writer_task_hash"] == writer_task["task_hash"]
        assert writer_result["candidate_kind"] == WRITER_OUTPUT_CONTRACT[
            "candidate_kind"
        ]
        assert writer_result["formal_draft_allowed"] is WRITER_OUTPUT_CONTRACT[
            "formal_draft_allowed"
        ]
        assert writer_result["writes_project_truth"] is WRITER_OUTPUT_CONTRACT[
            "writes_project_truth"
        ]
        assert writer_result["requires_author_review"] is WRITER_OUTPUT_CONTRACT[
            "requires_author_review"
        ]

        change_set_id = writer_result["change_set_id"]
        change_set_response = client.get(f"/api/v1/change-sets/{change_set_id}")
        assert change_set_response.status_code == 200, change_set_response.text
        change_set = change_set_response.json()
        assert change_set["origin"] == "agent"
        assert change_set["source_run_id"] == run_id
        assert change_set["source_tool_call_id"] == runtime_propose_tool_id
        assert change_set["project_id"] == project_id
        assert change_set["base_fingerprint"] == pack["project_fingerprint"]
        assert change_set["status"] == "reviewing"
        assert chapter_path.read_bytes() == original_chapter

        hunks = [
            hunk for change in change_set["changes"] for hunk in change["hunks"]
        ]
        assert hunks
        decisions_response = client.patch(
            f"/api/v1/change-sets/{change_set_id}/decisions",
            json={
                "expected_review_revision": change_set["review_revision"],
                "decisions": [
                    {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                    for hunk in hunks
                ],
            },
        )
        assert decisions_response.status_code == 200, decisions_response.text
        reviewed_change_set = decisions_response.json()
        assert chapter_path.read_bytes() == original_chapter

        managed_skills = app.state.managed_skill_service
        assert managed_skills is not None
        active_gate = managed_skills.get_active_declaration(
            project_id,
            "novel-creation-suite.draft-gate",
        )
        if legacy_gate:
            assert active_gate.manifest.version == "2026.07.22-draft-gate.1"
            assert active_gate.manifest.capabilities == []
            legacy_version_id = active_gate.skill_version_id
            replacement = managed_skills.register_builtin(
                DEFAULT_MANAGED_SKILL_ROOT / "creation-suite-draft-gate"
            )
            assert replacement.manifest.version == "2026.07.23-draft-gate.2"
            assert managed_skills.get_active_declaration(
                project_id,
                replacement.manifest.id,
            ).skill_version_id == legacy_version_id

            upgrade_required = client.post(
                f"/api/v1/change-sets/{change_set_id}/post-draft-gate",
                json={
                    "expected_review_revision": reviewed_change_set[
                        "review_revision"
                    ]
                },
            )
            assert upgrade_required.status_code == 409, upgrade_required.text
            assert upgrade_required.json()["error"] == {
                "code": "post_draft_gate_upgrade_required",
                "message": (
                    "active post-draft Gate does not declare "
                    "knowledge_validator_bundle.v1; the author must explicitly "
                    "upgrade it in the Skill lifecycle controls"
                ),
                "request_id": upgrade_required.json()["error"]["request_id"],
                "retryable": False,
            }
            current_gate = client.get(
                f"/api/v1/change-sets/{change_set_id}/post-draft-gate"
            )
            assert current_gate.status_code == 200, current_gate.text
            assert current_gate.json()["receipt"] is None
            rejected_apply = client.post(
                f"/api/v1/change-sets/{change_set_id}/apply",
                json={
                    "expected_review_revision": reviewed_change_set[
                        "review_revision"
                    ]
                },
            )
            assert rejected_apply.status_code == 409, rejected_apply.text
            assert rejected_apply.json()["error"]["code"] == (
                "post_draft_gate_upgrade_required"
            )
            assert chapter_path.read_bytes() == original_chapter

            upgraded = managed_skills.activate(
                replacement.manifest.id,
                ManagedSkillActivationRequest(
                    action="upgrade",
                    skill_version_id=replacement.skill_version_id,
                    expected_state_revision=1,
                    expected_version_id=legacy_version_id,
                ),
            )
            assert upgraded.activation.state_revision == 2
            assert upgraded.activation.effective_version_id == (
                replacement.skill_version_id
            )
            active_gate = managed_skills.get_active_declaration(
                project_id,
                replacement.manifest.id,
            )

        assert active_gate.manifest.version == "2026.07.23-draft-gate.2"
        assert active_gate.manifest.capabilities == [
            "knowledge_validator_bundle.v1"
        ]

        gate_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/post-draft-gate",
            json={
                "expected_review_revision": reviewed_change_set["review_revision"]
            },
        )
        assert gate_response.status_code == 200, gate_response.text
        gate = gate_response.json()["post_draft_gate"]
        assert gate["status"] == "passed"
        assert gate["execution_status"] == "succeeded"
        assert gate["context_pack_id"] == pack["context_pack_id"]
        assert gate["context_pack_hash"] == pack["pack_hash"]
        assert gate["knowledge_fingerprint"] == scoped_cards["fingerprint"]
        assert gate["route_receipt_id"] == route["route_receipt_id"]
        assert gate["route_receipt_hash"] == route["receipt_hash"]
        assert gate["knowledge_validator_bundle_hash"] == (
            validator_bundle.bundle_hash
        )
        assert gate["knowledge_validator_count"] == 1
        assert gate["knowledge_validator_card_ids"] == [validator_binding.card_id]
        assert gate["validator_id"] == "novel-creation-suite.draft-gate"
        assert gate["validator_version"] == active_gate.manifest.version
        assert gate["quality_assessed"] is False
        assert gate["formal_draft_allowed"] is False
        gate_result = gate["result"]
        assert gate_result["knowledge_validator_bundle_hash"] == (
            validator_bundle.bundle_hash
        )
        assert gate_result["quality_assessed"] is False
        assert gate_result["formal_draft_allowed"] is False
        assert gate_result["failure_signatures"] == []
        assert next(
            check
            for check in gate_result["checks"]
            if check["code"] == "formal_knowledge_validators"
        ) == {
            "code": "formal_knowledge_validators",
            "status": "passed",
            "message": "已执行 Context/NLCL 绑定的正式 validator 词项规则。",
            "evidence_refs": [
                f"{validator_binding.card_id}:{validator_binding.rules_hash}"
            ],
        }
        assert len(gate_result["knowledge_validator_results"]) == 1
        validator_execution = gate_result["knowledge_validator_results"][0]
        assert validator_execution["card_id"] == validator_binding.card_id
        assert validator_execution["version_id"] == validator_binding.version_id
        assert validator_execution["content_hash"] == validator_binding.content_hash
        assert validator_execution["rules_hash"] == validator_binding.rules_hash
        assert validator_execution["status"] == "passed"
        assert validator_execution["configuration_errors"] == []
        assert validator_execution["failure_signatures"] == []
        rule_results = {
            item["rule_id"]: item for item in validator_execution["rule_results"]
        }
        assert set(rule_results) == {
            "forbid_source_terms",
            "require_visible_pressure",
        }
        assert rule_results["forbid_source_terms"] == {
            "rule_id": "forbid_source_terms",
            "kind": "forbid_terms",
            "failure_signature": "validator_source_term_leak",
            "status": "passed",
            "match_count": 0,
            "evidence_refs": [],
        }
        assert rule_results["require_visible_pressure"]["kind"] == "require_any_term"
        assert rule_results["require_visible_pressure"]["failure_signature"] == (
            "validator_visible_pressure_missing"
        )
        assert rule_results["require_visible_pressure"]["status"] == "passed"
        assert rule_results["require_visible_pressure"]["match_count"] == 1
        assert rule_results["require_visible_pressure"]["evidence_refs"] == [
            "05_正文内容/第一卷/CH001_正文.md:1:33"
        ]
        assert chapter_path.read_bytes() == original_chapter

        if legacy_gate:
            with app.state.runtime.store.connection() as connection:
                route_row = connection.execute(
                    """
                    SELECT payload_json
                    FROM nlcl_route_receipt
                    WHERE context_pack_id = ?
                    """,
                    (pack["context_pack_id"],),
                ).fetchone()
                assert route_row is not None
                original_route_payload = str(route_row["payload_json"])
                connection.execute(
                    """
                    UPDATE nlcl_route_receipt
                    SET payload_json = '{}'
                    WHERE context_pack_id = ?
                    """,
                    (pack["context_pack_id"],),
                )

            hidden = client.get(
                f"/api/v1/change-sets/{change_set_id}/post-draft-gate"
            )
            assert hidden.status_code == 200, hidden.text
            assert hidden.json()["receipt"] is None
            stale_apply = client.post(
                f"/api/v1/change-sets/{change_set_id}/apply",
                json={
                    "expected_review_revision": reviewed_change_set[
                        "review_revision"
                    ]
                },
            )
            assert stale_apply.status_code == 409, stale_apply.text
            assert stale_apply.json()["error"]["code"] == "post_draft_gate_stale"
            assert chapter_path.read_bytes() == original_chapter

            with app.state.runtime.store.connection() as connection:
                connection.execute(
                    """
                    UPDATE nlcl_route_receipt
                    SET payload_json = ?
                    WHERE context_pack_id = ?
                    """,
                    (original_route_payload, pack["context_pack_id"]),
                )
            restored = client.get(
                f"/api/v1/change-sets/{change_set_id}/post-draft-gate"
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["receipt"]["receipt_hash"] == gate["receipt_hash"]

        applied_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/apply",
            json={
                "expected_review_revision": reviewed_change_set["review_revision"]
            },
        )
        assert applied_response.status_code == 200, applied_response.text
        applied = applied_response.json()
        assert chapter_path.read_bytes() != original_chapter
        assert b"before the old port closes" in chapter_path.read_bytes()

        target_file_id = applied["writeback"]["files"][0]["result_file_id"]
        assert target_file_id is not None
        readback = client.get(
            f"/api/v1/projects/{project_id}/files/{target_file_id}"
        )
        assert readback.status_code == 200, readback.text
        assert "before the old port closes" in readback.json()["content"]

        rollback_response = client.post(
            f"/api/v1/change-sets/{change_set_id}/rollback",
            json={"expected_review_revision": applied["review_revision"]},
        )
        assert rollback_response.status_code == 200, rollback_response.text
        rolled_back = rollback_response.json()
        assert rolled_back["status"] == "rolled_back"
        assert chapter_path.read_bytes() == original_chapter

        rollback_file_id = rolled_back["rollback"]["files"][0]["result_file_id"]
        assert rollback_file_id is not None
        rollback_readback = client.get(
            f"/api/v1/projects/{project_id}/files/{rollback_file_id}"
        )
        assert rollback_readback.status_code == 200, rollback_readback.text
        assert rollback_readback.json()["content"].encode("utf-8") == original_chapter

    assert SOURCE_FIXTURE.read_bytes() == source_before
    assert SOURCE_FIXTURE.stat().st_size == SOURCE_SIZE_BYTES
    assert hashlib.sha256(SOURCE_FIXTURE.read_bytes()).hexdigest() == SOURCE_SHA256

    with app.state.runtime.store.connection() as connection:
        provenance = connection.execute(
            "SELECT * FROM change_set_provenance WHERE change_set_id = ?",
            (change_set_id,),
        ).fetchone()
    assert provenance is not None
    assert str(provenance["run_id"]) == run_id
    assert str(provenance["tool_call_id"]) == runtime_propose_tool_id
    assert str(provenance["context_pack_id"]) == pack["context_pack_id"]
    assert str(provenance["project_fingerprint"]) == pack["project_fingerprint"]
