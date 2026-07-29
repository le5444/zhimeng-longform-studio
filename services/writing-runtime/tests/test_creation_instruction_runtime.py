from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_agent_candidate_output import _completed_workflow
from test_workflow_checkpoint_store import append_full_chain, build_fixture
from zhimeng_runtime.agent_store import CreationInstructionConflict
from zhimeng_runtime.creation_instruction_models import CREATION_INSTRUCTION_SKILL_ID
from zhimeng_runtime.skill_models import ManagedSkillActivationRequest


def _pre_materialization_fixture(tmp_path: Path):
    fixture = build_fixture(tmp_path)
    append_full_chain(fixture)
    fixture.store.ensure_writer_task(fixture.run.run_id, target_files=())
    return fixture


@pytest.mark.parametrize("resource", ["map", "source", "package"])
def test_instruction_materialization_rejects_managed_snapshot_drift(
    tmp_path: Path,
    resource: str,
) -> None:
    fixture = _pre_materialization_fixture(tmp_path)
    version = fixture.versions[CREATION_INSTRUCTION_SKILL_ID]
    map_path = version.instruction_map_path
    assert map_path is not None
    instruction_map = json.loads(map_path.read_text(encoding="utf-8"))
    if resource == "map":
        target = map_path
    elif resource == "source":
        target = version.package_root / instruction_map["source_path"]
    else:
        target = version.package_root / "PROVENANCE.md"
    target.write_bytes(target.read_bytes() + b"\nchanged-after-plan\n")

    with pytest.raises(CreationInstructionConflict) as drift:
        fixture.store.ensure_creation_instruction_materialization(
            fixture.run.run_id
        )

    assert drift.value.code == "creation_instruction_package_tampered"


def test_instruction_materialization_rejects_activation_change_after_plan(
    tmp_path: Path,
) -> None:
    fixture = _pre_materialization_fixture(tmp_path)
    assert fixture.tool_executor.skill_executor is not None
    fixture.tool_executor.skill_executor._service.activate(
        CREATION_INSTRUCTION_SKILL_ID,
        ManagedSkillActivationRequest(
            action="disable",
            expected_state_revision=0,
            expected_version_id=None,
        ),
        project_id=fixture.plan.project_id,
    )

    with pytest.raises(CreationInstructionConflict) as stale:
        fixture.store.ensure_creation_instruction_materialization(
            fixture.run.run_id
        )

    assert stale.value.code == "creation_instruction_activation_disabled"


def test_instruction_materialization_is_singleton_under_concurrency(
    tmp_path: Path,
) -> None:
    fixture = _pre_materialization_fixture(tmp_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        materializations = list(
            pool.map(
                lambda _: fixture.store.ensure_creation_instruction_materialization(
                    fixture.run.run_id
                ),
                range(4),
            )
        )

    assert materializations == [materializations[0]] * 4
    with fixture.database.connection() as connection:
        count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM artifact
                WHERE run_id = ?
                  AND artifact_type = 'creation_instruction_materialization'
                """,
                (fixture.run.run_id,),
            ).fetchone()[0]
        )
    assert count == 1


def test_materialization_and_dispatch_receipt_tampering_fail_closed(
    tmp_path: Path,
) -> None:
    materialization_fixture = _pre_materialization_fixture(tmp_path / "materialization")
    materialization = (
        materialization_fixture.store.ensure_creation_instruction_materialization(
            materialization_fixture.run.run_id
        )
    )
    with materialization_fixture.database.connection() as connection:
        connection.execute(
            "UPDATE artifact SET content_hash = ? WHERE id = ?",
            ("0" * 64, materialization.materialization_id),
        )
    with pytest.raises(CreationInstructionConflict) as materialization_tamper:
        materialization_fixture.store.get_creation_instruction_materialization(
            materialization_fixture.run.run_id
        )
    assert (
        materialization_tamper.value.code
        == "creation_instruction_materialization_tampered"
    )

    dispatch_fixture, _, _ = _completed_workflow(tmp_path / "dispatch")
    dispatch = dispatch_fixture.store.get_creation_instruction_dispatch_receipt(
        dispatch_fixture.run.run_id
    )
    with dispatch_fixture.database.connection() as connection:
        connection.execute(
            "UPDATE artifact SET content_hash = ? WHERE id = ?",
            ("0" * 64, dispatch.dispatch_receipt_id),
        )
    with pytest.raises(CreationInstructionConflict) as dispatch_tamper:
        dispatch_fixture.store.get_creation_instruction_dispatch_receipt(
            dispatch_fixture.run.run_id
        )
    assert (
        dispatch_tamper.value.code
        == "creation_instruction_dispatch_receipt_tampered"
    )
