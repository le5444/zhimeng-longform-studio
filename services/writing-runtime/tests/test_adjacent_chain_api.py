from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from test_skill_handoff_api import _formalize_three_card_batch  # type: ignore[import-not-found]


CHAIN_PREFIX = "/api/v1/projects/{project_id}/adjacent-skill-chains/novel-distillation-kb-to-context"


def test_adjacent_chain_o2d_to_o2e_single_approval(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "a" * 32
    client, project, finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        initial = client.get(base)
        assert initial.status_code == 200, initial.text
        assert initial.json()["record"]["status"] == "awaiting_upstream"
        assert initial.json()["not_claiming"] == [
            "four_skills_generic_exactly_once",
            "creation_suite_drafting",
            "reader_gate",
            "local_v1_exe",
        ]

        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "profile": "chapter",
                "stage": "draft",
                "chapter_id": "CH001",
                "task_intent": "连续执行 O2-D 到 O2-E，只生成写前 Context Pack。",
            },
        )
        assert prepared.status_code == 200, prepared.text
        prepared_record = prepared.json()["record"]
        assert prepared_record["status"] == "prepared"
        assert prepared_record["chain_key"] == "novel-distillation-kb-to-context"
        assert prepared_record["formal_draft_allowed"] is False
        assert prepared_record["skill_handoff_id"].startswith("skill-handoff-")
        assert [step["step_id"] for step in prepared_record["steps"]] == [
            "o2d-kb-handoff",
            "o2e-context-handoff",
        ]
        assert prepared_record["steps"][0]["status"] == "prepared"
        assert prepared_record["steps"][1]["status"] == "pending"
        assert prepared_record["source_import_id"] == source_import_id
        assert (
            prepared_record["steps"][0]["child_id"] == prepared_record["skill_handoff_id"]
        )
        dossier = prepared_record["risk_dossier"]
        assert dossier is not None
        assert dossier["formal_draft_allowed"] is False
        assert dossier["network"] is False
        assert dossier["secrets"] == []
        assert [item["skill_id"] for item in dossier["skills"]] == [
            "novel-distillation.kb-handoff",
            "novel-kb.context-handoff",
        ]
        assert dossier["skills"][0]["input_contract"].endswith(
            "novel-kb-handoff-request.schema.json"
        )
        assert dossier["skills"][1]["output_contract"].endswith(
            "novel-kb-context-handoff-result.schema.json"
        )
        assert prepared_record["risk_approval_token"] is None

        # Chain-level approval is the single author risk gate for both steps.
        approved = client.post(
            f"{base}/approve",
            json={"expected_chain_id": prepared_record["chain_id"]},
        )
        assert approved.status_code == 200, approved.text
        approved_record = approved.json()["record"]
        assert approved_record["status"] == "approved"
        assert approved_record["approval"] == {
            "approved": True,
            "approved_by": "author",
            "approved_at": approved_record["approval"]["approved_at"],
            "risk_acknowledged": True,
        }
        assert approved_record["steps"][0]["status"] == "approved"
        token = approved_record["risk_approval_token"]
        assert token is not None
        assert token["token_id"].startswith("risk-token-")
        assert token["consumed"] is False
        assert token["chain_id"] == prepared_record["chain_id"]
        assert token["project_revision"] == project["revision"]
        assert token["project_fingerprint"] == project["fingerprint"]
        assert len(token["token_hash"]) == 64
        assert token["dossier"]["skills"][0]["package_fingerprint"]
        assert token["dossier"]["skills"][1]["package_fingerprint"]
        plan = approved_record["workflow_plan"]
        assert plan is not None
        assert plan["workflow_plan_id"].startswith("adjacent-workflow-plan-")
        assert plan["risk_token_id"] == token["token_id"]
        assert plan["risk_token_hash"] == token["token_hash"]
        assert plan["formal_draft_allowed"] is False
        assert [step["skill_id"] for step in plan["steps"]] == [
            "novel-distillation.kb-handoff",
            "novel-kb.context-handoff",
        ]
        assert all(step["workflow_step_id"].startswith("workflow-step-") for step in plan["steps"])
        assert all(step["runner"] == "managed_skill" for step in plan["steps"])
        assert approved_record["workflow_checkpoints"] == []

        ran = client.post(
            f"{base}/run",
            json={"expected_chain_id": prepared_record["chain_id"]},
        )
        assert ran.status_code == 200, ran.text
        record = ran.json()["record"]
        assert record["status"] == "succeeded"
        assert record["risk_approval_token"]["consumed"] is True
        assert record["risk_approval_token"]["token_id"] == token["token_id"]
        assert record["current_step_ordinal"] == 2
        assert record["steps"][0]["status"] == "succeeded"
        assert record["steps"][1]["status"] == "succeeded"
        assert record["skill_handoff_id"].startswith("skill-handoff-")
        assert record["context_handoff_id"].startswith("context-handoff-")
        assert record["context_pack_id"].startswith("context-pack-o2e-")
        assert record["context_pack_hash"] and len(record["context_pack_hash"]) == 64
        assert record["route_receipt_id"].startswith("nlcl-route-")
        assert record["route_receipt_hash"] and len(record["route_receipt_hash"]) == 64
        assert record["formal_draft_allowed"] is False

        # Child O2-D/O2-E records stay recoverable through existing endpoints.
        o2d = client.get(
            f"/api/v1/projects/{project_id}/skill-handoffs/novel-distillation-kb"
        )
        assert o2d.status_code == 200, o2d.text
        assert o2d.json()["record"]["status"] == "succeeded"
        assert o2d.json()["record"]["handoff_id"] == record["skill_handoff_id"]

        o2e = client.get(
            f"/api/v1/projects/{project_id}/context-handoffs/novel-kb-assemble"
        )
        assert o2e.status_code == 200, o2e.text
        assert o2e.json()["record"]["status"] == "succeeded"
        assert o2e.json()["record"]["context_handoff_id"] == record["context_handoff_id"]
        assert o2e.json()["record"]["context_pack_id"] == record["context_pack_id"]

        recovered = client.get(base)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["record"]["chain_id"] == record["chain_id"]
        assert recovered.json()["record"]["status"] == "succeeded"
        assert recovered.json()["record"]["context_pack_hash"] == record["context_pack_hash"]

        # Idempotent re-run of a succeeded chain returns the same success.
        rerun = client.post(
            f"{base}/run",
            json={"expected_chain_id": record["chain_id"]},
        )
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()["record"]["status"] == "succeeded"
        assert rerun.json()["record"]["context_pack_id"] == record["context_pack_id"]

        # Formalization identity is still the upstream truth used by O2-D.
        assert finalized["formalization_receipt_id"]
    finally:
        client.__exit__(None, None, None)


def test_adjacent_chain_requires_approval_before_run(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "b" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
            },
        )
        assert prepared.status_code == 200, prepared.text
        chain_id = prepared.json()["record"]["chain_id"]
        blocked = client.post(
            f"{base}/run",
            json={"expected_chain_id": chain_id},
        )
        assert blocked.status_code == 409, blocked.text
        assert "批准" in blocked.json()["error"]["message"]
    finally:
        client.__exit__(None, None, None)


def test_adjacent_chain_risk_token_required_and_bound(tmp_path: Path) -> None:
    source_import_id = "source-import-" + "c" * 32
    client, project, _finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    base = CHAIN_PREFIX.format(project_id=project_id)
    try:
        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
            },
        )
        assert prepared.status_code == 200, prepared.text
        chain_id = prepared.json()["record"]["chain_id"]
        dossier = prepared.json()["record"]["risk_dossier"]
        assert dossier["risk_summary"]
        assert "formal_draft_allowed 保持 false" in dossier["risk_summary"]

        approved = client.post(
            f"{base}/approve",
            json={"expected_chain_id": chain_id},
        )
        assert approved.status_code == 200, approved.text
        token = approved.json()["record"]["risk_approval_token"]
        assert token["consumed"] is False
        assert token["dossier"]["not_claiming"]
        assert "four_skills_generic_exactly_once" in token["dossier"]["not_claiming"]

        # Tampered token hash must fail closed before execution.
        chain_path = (
            Path(client.app.state.runtime.settings.database_path).parent
            / "adjacent-chains"
            / f"{chain_id}.json"
        )
        payload = chain_path.read_text(encoding="utf-8")
        import json
        data = json.loads(payload)
        data["risk_approval_token"]["token_hash"] = "f" * 64
        chain_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        blocked = client.post(
            f"{base}/run",
            json={"expected_chain_id": chain_id},
        )
        assert blocked.status_code == 409, blocked.text
        assert "token" in blocked.json()["error"]["message"].lower() or "哈希" in blocked.json()["error"]["message"]
    finally:
        client.__exit__(None, None, None)
