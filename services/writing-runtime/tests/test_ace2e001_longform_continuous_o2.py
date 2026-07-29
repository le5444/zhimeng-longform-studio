from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from test_four_skills_continuous_api import _write_creation_success  # type: ignore[import-not-found]
from test_skill_handoff_api import _formalize_three_card_batch  # type: ignore[import-not-found]


CHAIN_PREFIX = (
    "/api/v1/projects/{project_id}/adjacent-skill-chains/"
    "novel-distillation-kb-to-context"
)


def _expand_to_longform(project_root: Path) -> None:
    """Turn the formalized single-chapter project into multi-chapter longform evidence."""
    volume = project_root / "05_正文内容" / "第一卷"
    volume.mkdir(parents=True, exist_ok=True)
    (volume / "CH001_正文.md").write_text(
        "第一章。青崖在旧港找到钥匙，却没有立刻开门。\n",
        encoding="utf-8",
    )
    (volume / "CH002_正文.md").write_text(
        "第二章。白灯仍亮着，证人的名字被雨洗掉一半。\n",
        encoding="utf-8",
    )
    (volume / "CH003_正文.md").write_text(
        "第三章。他把钥匙放回袖中，决定先核对旧账。\n",
        encoding="utf-8",
    )
    foreshadow = project_root / "04_剧情大纲" / "伏笔清单.md"
    foreshadow.parent.mkdir(parents=True, exist_ok=True)
    foreshadow.write_text("# 伏笔\n旧港白灯尚未点破。\n", encoding="utf-8")
    character = project_root / "03_人物设定" / "主角.md"
    character.parent.mkdir(parents=True, exist_ok=True)
    character.write_text("# 青崖\n擅长借势破局，拒绝无代价力量。\n", encoding="utf-8")




def _seed_ready_instruction_chain_handoff(runtime_root: Path) -> dict:
    from zhimeng_runtime.four_skills_raw_adaptation_models import (
        FourSkillsInstructionChainHandoffReceipt,
        FourSkillsInstructionChainHandoffStep,
        FourSkillsRawFamily,
        four_skills_instruction_chain_handoff_receipt_hash,
    )

    families = (
        FourSkillsRawFamily.SOURCE_INGEST,
        FourSkillsRawFamily.DISTILLATION,
        FourSkillsRawFamily.KNOWLEDGE_CONTEXT,
        FourSkillsRawFamily.CREATION_SUITE,
    )
    package_fp = "a" * 64
    batch_hash = "b" * 64
    binding_hash = "c" * 64
    orchestration_hash = "d" * 64
    batch_id = "four-skills-raw-adaptation-" + ("1" * 32)
    binding_id = "four-skills-workflow-binding-" + ("2" * 32)
    orchestration_id = "four-skills-workflow-orchestration-" + ("3" * 32)
    handoff_id = "four-skills-instruction-chain-handoff-" + ("4" * 32)
    steps = [
        FourSkillsInstructionChainHandoffStep(
            family=family,
            ordinal=index,
            skill_id=f"adapted.{family.value}",
            skill_version_id=f"skill-version-{index:02d}" + ("0" * 30),
            package_fingerprint=package_fp,
            runner="instruction_chain_handoff",
            status="prepared",
            managed_skill_execution=False,
            raw_skill_md_execution=False,
            detail="seeded prepared instruction step",
        )
        for index, family in enumerate(families, start=1)
    ]
    handed_off_at = datetime.now(UTC)
    temp = FourSkillsInstructionChainHandoffReceipt.model_construct(
        schema_id="zhimeng.four-skills-instruction-chain-handoff-receipt.v1",
        handoff_id=handoff_id,
        batch_id=batch_id,
        batch_hash=batch_hash,
        binding_id=binding_id,
        binding_hash=binding_hash,
        orchestration_id=orchestration_id,
        orchestration_hash=orchestration_hash,
        status="ready",
        workflow_surface="generic_instruction_workflow",
        steps=steps,
        step_count=4,
        prepared_step_count=4,
        instruction_chain_ready=True,
        managed_skill_execution=False,
        raw_skill_md_execution=False,
        four_skills_generic_exactly_once=False,
        formal_draft_allowed=False,
        writes_project_truth=False,
        policy_version="local-v1-four-skills-instruction-chain-handoff-1",
        detail="seeded ready instruction chain handoff",
        handed_off_at=handed_off_at,
        handoff_hash="0" * 64,
    )
    handoff_hash = four_skills_instruction_chain_handoff_receipt_hash(temp)
    receipt = FourSkillsInstructionChainHandoffReceipt(
        handoff_id=handoff_id,
        batch_id=batch_id,
        batch_hash=batch_hash,
        binding_id=binding_id,
        binding_hash=binding_hash,
        orchestration_id=orchestration_id,
        orchestration_hash=orchestration_hash,
        status="ready",
        workflow_surface="generic_instruction_workflow",
        steps=steps,
        step_count=4,
        prepared_step_count=4,
        instruction_chain_ready=True,
        managed_skill_execution=False,
        raw_skill_md_execution=False,
        four_skills_generic_exactly_once=False,
        formal_draft_allowed=False,
        writes_project_truth=False,
        policy_version="local-v1-four-skills-instruction-chain-handoff-1",
        detail="seeded ready instruction chain handoff",
        handed_off_at=handed_off_at,
        handoff_hash=handoff_hash,
    )
    target = runtime_root / "four-skills-raw-adaptations" / batch_id / "instruction-chain-handoff.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(receipt.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "batch_id": batch_id,
        "batch_hash": batch_hash,
        "handoff": receipt.model_dump(mode="json", by_alias=True),
    }


def _bind_instruction_chain_handoff(client, project: dict, prepared: dict) -> dict:
    response = client.post(
        f"/api/v1/projects/{project['project_id']}/instruction-chain-handoff-bindings",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "batch_id": prepared["batch_id"],
            "batch_hash": prepared["batch_hash"],
            "handoff_id": prepared["handoff"]["handoff_id"],
            "handoff_hash": prepared["handoff"]["handoff_hash"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["receipt"]

def test_ace2e001_longform_continuous_o2_authorization_input(tmp_path: Path) -> None:
    """AC-E2E-001 residual slice:

    long-form multi-chapter project
    -> source/knowledge formalization
    -> adjacent O2 chain + creation-suite continuous evidence
    -> four-skills continuous + continuous O2 authorization input
    -> domain asset index rebuild
    -> formal draft remains denied

    Does not claim four_skills_generic_exactly_once, formal unlock, or
    relationship_graph_complete.
    """
    source_import_id = "source-import-" + ("a" * 32)
    client, project, finalized, _terminal = _formalize_three_card_batch(
        tmp_path,
        source_import_id=source_import_id,
    )
    project_id = project["project_id"]
    try:
        # Expand the bound project into multi-chapter longform, then re-inspect.
        binding_root = Path(project["root_path"])
        _expand_to_longform(binding_root)
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(binding_root)},
        )
        assert inspected.status_code == 200, inspected.text
        snapshot = inspected.json()
        project = snapshot["project"]
        project_id = project["project_id"]
        prose_files = [
            item
            for item in snapshot["files"]
            if "05_正文内容" in item["relative_path"]
            and item["relative_path"].endswith(".md")
        ]
        assert len(prose_files) >= 3

        assets = client.post(f"/api/v1/projects/{project_id}/assets/rebuild")
        assert assets.status_code == 200, assets.text
        asset_index = assets.json()["index"]
        assert asset_index["asset_count"] >= 2
        assert asset_index["relationship_graph_complete"] is False
        assert asset_index["formal_draft_allowed"] is False
        assert all(
            not item["relative_path"].startswith("05_正文内容/")
            for item in asset_index["assets"]
        )

        base = CHAIN_PREFIX.format(project_id=project_id)
        prepared = client.post(
            f"{base}/prepare",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "profile": "chapter",
                "stage": "draft",
                "chapter_id": "CH003",
                "task_intent": "AC-E2E-001 longform continuous O2 residual",
            },
        )
        assert prepared.status_code == 200, prepared.text
        chain_id = prepared.json()["record"]["chain_id"]
        approved = client.post(
            f"{base}/approve",
            json={"expected_chain_id": chain_id},
        )
        assert approved.status_code == 200, approved.text
        ran = client.post(
            f"{base}/run",
            json={"expected_chain_id": chain_id},
        )
        assert ran.status_code == 200, ran.text
        assert ran.json()["record"]["status"] == "succeeded"

        runtime_root = Path(client.app.state.runtime.store.path).parent
        creation_id = _write_creation_success(
            runtime_root,
            project_id=project_id,
            revision=project["revision"],
            fingerprint=project["fingerprint"],
        )
        prepared_handoff = _seed_ready_instruction_chain_handoff(runtime_root)
        handoff_binding = _bind_instruction_chain_handoff(client, project, prepared_handoff)

        continuous = client.post(
            f"/api/v1/projects/{project_id}/four-skills-continuous/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "adjacent_chain_id": chain_id,
                "managed_skill_chain_id": creation_id,
            },
        )
        assert continuous.status_code == 200, continuous.text
        continuous_receipt = continuous.json()["receipt"]
        assert continuous_receipt["status"] == "complete"
        assert continuous_receipt["authorization_input_ready"] is True
        assert continuous_receipt["formal_draft_allowed"] is False
        assert continuous_receipt["four_skills_generic_exactly_once"] is False
        assert continuous_receipt["formalization_receipt_id"] == finalized[
            "formalization_receipt_id"
        ]

        o2 = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/continuous-o2/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
                "source_import_id": source_import_id,
                "adjacent_chain_id": chain_id,
                "managed_skill_chain_id": creation_id,
            },
        )
        assert o2.status_code == 200, o2.text
        o2_payload = o2.json()
        o2_receipt = o2_payload["receipt"]
        assert o2_payload["schema"] == "zhimeng.formal-draft-continuous-o2-response.v1"
        assert o2_receipt["status"] == "complete"
        assert o2_receipt["authorization_input_ready"] is True
        assert o2_receipt["authorization_unlock"] is False
        assert o2_receipt["formal_draft_allowed"] is False
        assert o2_receipt["four_skills_generic_exactly_once"] is False
        assert o2_receipt["missing_required"] == []
        assert o2_receipt["adjacent_chain_id"] == chain_id
        assert o2_receipt["managed_skill_chain_id"] == creation_id
        assert o2_receipt["creation_suite_chain_id"] == creation_id
        assert o2_receipt["four_skills_continuous_receipt_id"]
        assert o2_receipt["instruction_chain_handoff_binding_id"] == handoff_binding["binding_id"]
        assert o2_receipt["instruction_chain_handoff_id"] == prepared_handoff["handoff"]["handoff_id"]
        assert any(
            item["code"] == "instruction_chain_handoff_bound" and item["present"] is True
            for item in o2_receipt["checks"]
        )
        assert "formal_draft_authorization_unlock" in o2_payload["not_claiming"]
        assert "four_skills_generic_exactly_once" in o2_payload["not_claiming"]

        formal = client.post(
            f"/api/v1/projects/{project_id}/formal-draft-authorization/evaluate",
            json={
                "expected_project_revision": project["revision"],
                "expected_project_fingerprint": project["fingerprint"],
            },
        )
        assert formal.status_code == 200, formal.text
        formal_receipt = formal.json()["receipt"]
        assert formal_receipt["formal_draft_allowed"] is False
        assert formal_receipt["decision"] == "denied"
        prereq = next(
            item
            for item in formal_receipt["prerequisites"]
            if item["code"] == "four_skills_continuous_o2"
        )
        assert prereq["satisfied"] is True
        assert all(
            item["code"] != "four_skills_chain_incomplete"
            for item in formal_receipt["denials"]
        )
    finally:
        client.__exit__(None, None, None)
