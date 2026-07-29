from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.managed_skill_service import ManagedSkillService
from zhimeng_runtime.skill_executor import SkillExecutor


PROJECT_ID = "project-executor"
PROJECT_REVISION = 1
PROJECT_FINGERPRINT = "b" * 64
SKILL_ID = "novel-creation-suite.project-audit"


def package_manifest(*, timeout_seconds: int = 5) -> dict[str, object]:
    return {
        "schema": "zhimeng.skill-manifest.v1",
        "id": SKILL_ID,
        "display_name": "Project audit",
        "description": "Deterministic executor fixture.",
        "kind": "deterministic_tool",
        "version": "2026.07.20-m3a.1",
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": False,
        },
        "entrypoint": "scripts/project_audit.py",
        "approval": "none",
        "permissions": {
            "read": ["bound_project_snapshot"],
            "write": ["run_staging"],
            "network": False,
            "secrets": [],
        },
        "timeout_seconds": timeout_seconds,
        "exit_codes": {"0": "success", "1": "domain_blocked"},
        "artifacts": ["project_audit"],
        "input_schema": "https://zhimeng.local/contracts/project-audit-request.schema.json",
        "output_schema": "https://zhimeng.local/contracts/project-audit-output.schema.json",
    }


def write_contracts(root: Path) -> None:
    root.mkdir()
    (root / "project-audit-request.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://zhimeng.local/contracts/project-audit-request.schema.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["mode"],
                "properties": {"mode": {"const": "audit"}},
            }
        ),
        encoding="utf-8",
    )
    (root / "project-audit-output.schema.json").write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://zhimeng.local/contracts/project-audit-output.schema.json",
                "type": "object",
                "additionalProperties": False,
                "required": ["snapshot_name", "request"],
                "properties": {
                    "snapshot_name": {"type": "string"},
                    "request": {"type": "object"},
                },
            }
        ),
        encoding="utf-8",
    )


def setup_executor(
    tmp_path: Path,
    script: str,
    *,
    timeout_seconds: int = 5,
) -> tuple[SkillExecutor, ManagedSkillService, Path, Path, Path]:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project = tmp_path / "project"
    snapshot = tmp_path / "snapshot"
    staging = tmp_path / "staging"
    for directory in (project, snapshot, staging):
        directory.mkdir()
    (snapshot / "chapter.md").write_text("Snapshot truth.\n", encoding="utf-8")
    store.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(project.resolve()),
        display_name="Executor fixture",
        fingerprint=PROJECT_FINGERPRINT,
    )
    managed_root = tmp_path / "managed"
    contracts = tmp_path / "contracts"
    write_contracts(contracts)
    package = managed_root / "project-audit"
    entrypoint = package / "scripts" / "project_audit.py"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(script, encoding="utf-8")
    (package / "PROVENANCE.md").write_text("Fixture.\n", encoding="utf-8")
    (package / "skill.manifest.json").write_text(
        json.dumps(package_manifest(timeout_seconds=timeout_seconds), indent=2),
        encoding="utf-8",
    )
    service = ManagedSkillService(store, managed_root, contracts_root=contracts)
    service.register_builtin(package)
    return (
        SkillExecutor(service, contracts_root=contracts),
        service,
        package,
        snapshot,
        staging,
    )


def setup_instruction_source_executor(
    tmp_path: Path,
) -> tuple[SkillExecutor, Path, Path]:
    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    project = tmp_path / "project"
    snapshot = tmp_path / "snapshot"
    staging = tmp_path / "staging"
    for directory in (project, snapshot, staging):
        directory.mkdir()
    store.sync_project_binding(
        project_id=PROJECT_ID,
        root_path=str(project.resolve()),
        display_name="Instruction source fixture",
        fingerprint=PROJECT_FINGERPRINT,
    )
    managed_root = tmp_path / "managed"
    package = managed_root / "writer-instruction"
    package.mkdir(parents=True)
    (package / "PROVENANCE.md").write_text("Fixture.\n", encoding="utf-8")
    (package / "instruction-map.json").write_text(
        json.dumps({"schema": "zhimeng.instruction-map.v1", "instructions": []}),
        encoding="utf-8",
    )
    (package / "skill.manifest.json").write_text(
        json.dumps(
            {
                "schema": "zhimeng.skill-manifest.v1",
                "id": SKILL_ID,
                "display_name": "Writer instruction source",
                "description": "Non-executable instruction materialization input.",
                "kind": "instruction_source",
                "version": "2026.07.23-o2.2",
                "source": {
                    "type": "builtin_managed",
                    "reference": "PROVENANCE.md",
                    "license": None,
                    "source_url": None,
                    "source_version": None,
                    "modified": True,
                },
                "approval": "none",
                "permissions": {
                    "read": [],
                    "write": [],
                    "network": False,
                    "secrets": [],
                },
                "artifacts": [],
                "capabilities": ["creation_instruction_materialization.v1"],
                "resources": ["instruction-map.json"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    service = ManagedSkillService(store, managed_root, contracts_root=contracts)
    service.register_builtin(package)
    return SkillExecutor(service, contracts_root=contracts), snapshot, staging


def execute(
    executor: SkillExecutor,
    snapshot: Path,
    staging: Path,
    *,
    cancellation_event: asyncio.Event | None = None,
):
    return executor.execute(
        project_id=PROJECT_ID,
        project_revision=PROJECT_REVISION,
        project_fingerprint=PROJECT_FINGERPRINT,
        run_id="run-executor-1",
        tool_call_id="tool-call-executor-1",
        skill_id=SKILL_ID,
        input_payload={"mode": "audit"},
        project_snapshot=snapshot,
        run_staging=staging,
        cancellation_event=cancellation_event,
    )


SUCCESS_SCRIPT = r'''
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
snapshot = Path(sys.argv[1])
assert sys.argv[2:] == ["--json"]
artifact = {
    "snapshot_name": snapshot.name,
    "request": request,
}
Path("audit.json").write_text(json.dumps(artifact), encoding="utf-8")
print(json.dumps({
    "health": "healthy",
    "summary": "Snapshot audit completed.",
    "result": artifact,
    "artifacts": [{
        "artifact_type": "project_audit",
        "relative_path": "audit.json",
        "payload": artifact,
    }],
}))
'''


def test_executes_fixed_python_argv_and_returns_self_contained_receipt(
    tmp_path: Path,
) -> None:
    executor, _, _, snapshot, staging = setup_executor(tmp_path, SUCCESS_SCRIPT)

    outcome = asyncio.run(execute(executor, snapshot, staging))

    receipt = outcome.receipt
    assert receipt.execution_status == "succeeded"
    assert receipt.domain_status == "healthy"
    assert receipt.exit_code == 0
    assert receipt.project_id == PROJECT_ID
    assert receipt.project_revision == PROJECT_REVISION
    assert receipt.project_fingerprint == PROJECT_FINGERPRINT
    assert receipt.skill_id == SKILL_ID
    assert receipt.skill_kind == "deterministic_tool"
    assert receipt.permissions.network is False
    assert outcome.result_summary == "Snapshot audit completed."
    assert outcome.result == {
        "snapshot_name": "snapshot",
        "request": {"mode": "audit"},
    }
    assert outcome.artifacts[0].relative_path == "audit.json"
    assert receipt.artifacts[0].content_hash
    canonical = json.dumps(
        receipt.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert outcome.receipt_hash == hashlib.sha256(canonical).hexdigest()


def test_instruction_source_is_rejected_without_starting_a_process(
    tmp_path: Path,
) -> None:
    executor, snapshot, staging = setup_instruction_source_executor(tmp_path)

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.skill_kind == "instruction_source"
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "skill_not_executable"
    assert outcome.receipt.error.retryable is False
    assert outcome.result is None
    assert list(staging.iterdir()) == []


def test_manifest_input_contract_rejects_payload_before_process_start(
    tmp_path: Path,
) -> None:
    executor, _, _, snapshot, staging = setup_executor(tmp_path, SUCCESS_SCRIPT)

    outcome = asyncio.run(
        executor.execute(
            project_id=PROJECT_ID,
            project_revision=PROJECT_REVISION,
            project_fingerprint=PROJECT_FINGERPRINT,
            run_id="run-invalid-input",
            tool_call_id="tool-call-invalid-input",
            skill_id=SKILL_ID,
            input_payload={"mode": "rewrite"},
            project_snapshot=snapshot,
            run_staging=staging,
        )
    )

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "invalid_skill_input"
    assert outcome.result is None
    assert not (staging / "audit.json").exists()


def test_generic_envelope_can_pass_while_manifest_result_contract_rejects(
    tmp_path: Path,
) -> None:
    script = (
        "import json\n"
        "print(json.dumps({'health':'healthy','summary':'done',"
        "'result':{'snapshot_name':'fixture'},'artifacts':[]}))\n"
    )
    executor, _, _, snapshot, staging = setup_executor(tmp_path, script)

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "invalid_skill_result"
    assert outcome.result is None


def test_contract_id_must_match_the_manifest_uri_without_network_fallback(
    tmp_path: Path,
) -> None:
    executor, _, _, snapshot, staging = setup_executor(tmp_path, SUCCESS_SCRIPT)
    contract = tmp_path / "contracts" / "project-audit-request.schema.json"
    schema = json.loads(contract.read_text(encoding="utf-8"))
    schema["$id"] = "https://zhimeng.local/contracts/a-different-contract.schema.json"
    contract.write_text(json.dumps(schema), encoding="utf-8")

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "invalid_skill_contract"
    assert not (staging / "audit.json").exists()


@pytest.mark.parametrize(
    ("health", "exit_code"),
    [("degraded", 0), ("blocked", 1)],
)
def test_domain_health_exit_semantics_are_execution_success(
    tmp_path: Path,
    health: str,
    exit_code: int,
) -> None:
    script = (
        "import json,sys\n"
        "result={'snapshot_name':'fixture','request':{}}\n"
        f"print(json.dumps({{'health':'{health}','summary':'done',"
        "'result':result,'artifacts':[]}))\n"
        f"sys.exit({exit_code})\n"
    )
    executor, _, _, snapshot, staging = setup_executor(tmp_path, script)

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "succeeded"
    assert outcome.receipt.domain_status == health
    assert outcome.receipt.exit_code == exit_code


@pytest.mark.parametrize(
    ("script", "expected_code"),
    [
        ("print('not json')\n", "invalid_skill_output"),
        (
            "import json,sys\n"
            "result={'snapshot_name':'fixture','request':{}}\n"
            "print(json.dumps({'health':'healthy','summary':'x',"
            "'result':result,'artifacts':[]}))\n"
            "sys.exit(2)\n",
            "unexpected_exit_code",
        ),
        (
            "import json,sys\n"
            "result={'snapshot_name':'fixture','request':{}}\n"
            "print(json.dumps({'health':'blocked','summary':'x',"
            "'result':result,'artifacts':[]}))\n",
            "exit_health_mismatch",
        ),
    ],
)
def test_bad_json_unknown_exit_and_health_mismatch_fail(
    tmp_path: Path,
    script: str,
    expected_code: str,
) -> None:
    executor, _, _, snapshot, staging = setup_executor(tmp_path, script)

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.domain_status is None
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == expected_code
    assert outcome.result is None


def test_timeout_and_explicit_cancel_terminate_without_false_success(
    tmp_path: Path,
) -> None:
    script = "import time\ntime.sleep(30)\n"
    executor, _, _, snapshot, staging = setup_executor(
        tmp_path / "timeout",
        script,
        timeout_seconds=1,
    )
    timed_out = asyncio.run(execute(executor, snapshot, staging))
    assert timed_out.receipt.execution_status == "failed"
    assert timed_out.receipt.error is not None
    assert timed_out.receipt.error.code == "timeout"

    async def cancel_run() -> object:
        other_executor, _, _, other_snapshot, other_staging = setup_executor(
            tmp_path / "cancel",
            script,
        )
        event = asyncio.Event()
        task = asyncio.create_task(
            execute(
                other_executor,
                other_snapshot,
                other_staging,
                cancellation_event=event,
            )
        )
        await asyncio.sleep(0.1)
        event.set()
        return await task

    cancelled = asyncio.run(cancel_run())
    assert cancelled.receipt.execution_status == "cancelled"
    assert cancelled.receipt.error is not None
    assert cancelled.receipt.error.code == "cancelled"


def test_package_drift_fails_before_process_execution(tmp_path: Path) -> None:
    executor, service, _, snapshot, staging = setup_executor(tmp_path, SUCCESS_SCRIPT)
    active = service.get_active(PROJECT_ID, SKILL_ID)
    (active.package_root / "PROVENANCE.md").write_text(
        "Drifted.\n",
        encoding="utf-8",
    )

    outcome = asyncio.run(execute(executor, snapshot, staging))

    assert outcome.receipt.execution_status == "failed"
    assert outcome.receipt.error is not None
    assert outcome.receipt.error.code == "package_drift"
    assert not (staging / "audit.json").exists()


def test_rejects_stale_project_identity_and_overlapping_execution_roots(
    tmp_path: Path,
) -> None:
    executor, _, _, snapshot, staging = setup_executor(tmp_path, SUCCESS_SCRIPT)
    stale = asyncio.run(
        executor.execute(
            project_id=PROJECT_ID,
            project_revision=2,
            project_fingerprint=PROJECT_FINGERPRINT,
            run_id="run-stale",
            tool_call_id="tool-call-stale",
            skill_id=SKILL_ID,
            input_payload={},
            project_snapshot=snapshot,
            run_staging=staging,
        )
    )
    assert stale.receipt.error is not None
    assert stale.receipt.error.code == "project_snapshot_stale"

    overlapping = asyncio.run(execute(executor, snapshot, snapshot))
    assert overlapping.receipt.error is not None
    assert overlapping.receipt.error.code == "invalid_execution_boundary"
