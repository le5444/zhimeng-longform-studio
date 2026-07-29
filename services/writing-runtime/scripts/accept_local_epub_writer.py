from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient


WRITING_RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(WRITING_RUNTIME / "tests"))

from zhimeng_runtime.app import create_app  # noqa: E402
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings  # noqa: E402
from zhimeng_runtime.local_source_adapter import ManagedLocalSourceAdapter  # noqa: E402
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    LocalSourceImportExecutionReceipt,
)
from local_source_fixture import (  # noqa: E402
    EXPECTED_CARD_TYPE_SET,
    EXPECTED_WORKFLOW_SKILL_IDS,
    SNAPSHOT_ROOT,
    WRITER_OUTPUT_CONTRACT,
)
from test_context_agent_e2e import (  # noqa: E402
    ToolCallingProvider,
    _start_tool_run,
    _wait_for_terminal,
    _write_project,
)
from test_knowledge_api import (  # noqa: E402
    _reviewed_validator_card,
    _reviewed_voice_card,
    _reviewed_work_card,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wait_for_review(
    client: TestClient,
    source_import_id: str,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/source-imports/{source_import_id}")
        response.raise_for_status()
        latest = response.json()
        status = latest.get("status")
        if status == "awaiting_review":
            return latest
        if status in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(f"local EPUB import stopped with status={status}")
        time.sleep(0.05)
    raise RuntimeError("local EPUB writer acceptance timed out waiting for review")


def run_acceptance(
    source_path: Path,
    *,
    expected_sha256: str | None,
    timeout_seconds: float,
) -> dict[str, object]:
    source_path = source_path.expanduser().resolve(strict=True)
    if not source_path.is_file() or source_path.suffix.lower() != ".epub":
        raise RuntimeError("acceptance source must be a regular .epub file")
    before_size = source_path.stat().st_size
    before_sha256 = _sha256_file(source_path)
    if expected_sha256 is not None and before_sha256 != expected_sha256:
        raise RuntimeError("acceptance source SHA-256 does not match the expected value")

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-epub-writer-acceptance-"))
    library = runtime_root / "library"
    project = library / "novel"
    evidence: dict[str, object] | None = None
    cleanup_error: str | None = None
    try:
        library.mkdir(parents=True, exist_ok=True)
        _write_project(project)
        chapter_path = project / "05_正文内容" / "第一卷" / "CH001_正文.md"
        original_chapter = chapter_path.read_bytes()
        provider = ToolCallingProvider()
        source_adapter = ManagedLocalSourceAdapter(
            snapshot_root=SNAPSHOT_ROOT,
            state_root=runtime_root / "source-imports",
            trusted_source_roots=(source_path.parent.resolve(),),
        )
        settings = RuntimeSettings(
            environment="acceptance",
            database_path=runtime_root / "runtime.sqlite3",
            allowed_origins=(),
            trusted_source_roots=(source_path.parent.resolve(),),
            trusted_project_roots=(library,),
            managed_skill_root=DEFAULT_MANAGED_SKILL_ROOT,
            sse_heartbeat_seconds=0.05,
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
            inspection.raise_for_status()
            snapshot = inspection.json()
            project_id = snapshot["project"]["project_id"]
            knowledge_path = f"/api/v1/projects/{project_id}/knowledge"

            inspected = client.post(
                "/api/v1/source-imports/local/inspect",
                json={"source_path": str(source_path)},
            )
            inspected.raise_for_status()
            candidate = inspected.json()
            if (
                candidate.get("source_format") != "epub"
                or candidate.get("source_sha256") != before_sha256
                or candidate.get("source_size_bytes") != before_size
            ):
                raise RuntimeError("inspect API did not bind the selected EPUB bytes")

            created = client.post(
                "/api/v1/source-imports",
                json={
                    "provider": "local_file",
                    "query": candidate["source_path"],
                    "candidate": candidate,
                },
            )
            created.raise_for_status()
            source_import_id = created.json()["import_id"]
            job = _wait_for_review(client, source_import_id, timeout_seconds)
            packet = job.get("source_packet")
            draft = job.get("distillation_draft")
            if not isinstance(packet, dict) or not isinstance(draft, dict):
                raise RuntimeError("review-ready import omitted packet or draft evidence")

            receipt_response = client.get(
                f"/api/v1/source-imports/{source_import_id}/execution-receipt"
            )
            receipt_response.raise_for_status()
            source_receipt = LocalSourceImportExecutionReceipt.model_validate(
                receipt_response.json()["receipt"]
            )
            if (
                source_receipt.source_format != "epub"
                or source_receipt.packet_source_hash != before_sha256
                or source_receipt.packet_text_hash == before_sha256
            ):
                raise RuntimeError("execution receipt did not preserve independent EPUB evidence")

            imported = client.post(
                f"{knowledge_path}/source-imports/{source_import_id}/candidates"
            )
            imported.raise_for_status()
            candidates = imported.json()["candidates"]
            builders = {
                "work": _reviewed_work_card,
                "voice": _reviewed_voice_card,
                "validator": _reviewed_validator_card,
            }
            if {item["card_type"] for item in candidates} != set(builders):
                raise RuntimeError("draft candidates did not provide work/voice/validator")
            for item in candidates:
                reviewed = client.put(
                    f"{knowledge_path}/candidates/{item['candidate_id']}",
                    json={
                        "expected_revision": item["revision"],
                        "expected_content_hash": item["content_hash"],
                        "card": builders[item["card_type"]](item["card_id"]),
                    },
                )
                reviewed.raise_for_status()
                promoted = client.post(
                    f"{knowledge_path}/candidates/{item['candidate_id']}/promote",
                    json={
                        "expected_revision": reviewed.json()["revision"],
                        "expected_content_hash": reviewed.json()["content_hash"],
                        "replace_active": False,
                    },
                )
                promoted.raise_for_status()

            terminal = client.get(
                f"{knowledge_path}/candidates",
                params={"source_import_id": source_import_id},
            )
            terminal.raise_for_status()
            finalized = client.post(
                f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
                json={
                    "expected_source_receipt_hash": source_receipt.receipt_hash,
                    "expected_draft_hash": source_receipt.cards_draft_hash,
                    "expected_candidate_set_hash": terminal.json()["candidate_set_hash"],
                },
            )
            finalized.raise_for_status()
            formalization = finalized.json()
            if formalization.get("schema") != "zhimeng.knowledge-formalization-receipt.v2":
                raise RuntimeError("formalization receipt schema mismatch")
            if {item["decision"] for item in formalization["decisions"]} != {"promoted"}:
                raise RuntimeError("formalization did not promote all three card types")

            cards_response = client.get(f"{knowledge_path}/cards")
            cards_response.raise_for_status()
            scoped_cards = cards_response.json()
            active_types = {
                item["active_version"]["card"]["card_type"]
                for item in scoped_cards["cards"]
            }
            if active_types != EXPECTED_CARD_TYPE_SET:
                raise RuntimeError("active formal cards did not cover work/voice/validator")

            objective = (
                "玄幻章节要用人物压力和自然叙事完成阶段兑现，保留 blood-stained key，"
                "并检查来源污染。"
            )
            run_id, pack = _start_tool_run(
                client,
                snapshot,
                objective=objective,
                key_prefix="external-epub-writer-0001",
                writer_mode="author_review_change_proposal",
            )
            if pack["knowledge_fingerprint"] != scoped_cards["fingerprint"]:
                raise RuntimeError("context pack knowledge fingerprint mismatch")

            route_response = client.get(
                f"/api/v1/agent/context-packs/{pack['context_pack_id']}/nlcl-route"
            )
            route_response.raise_for_status()
            route = route_response.json()
            if {
                item["card_type"] for item in route["selected_cards"]
            } != EXPECTED_CARD_TYPE_SET:
                raise RuntimeError("NLCL route did not select all three formal cards")

            terminal_run = _wait_for_terminal(client, run_id)
            if terminal_run["status"] != "succeeded":
                raise RuntimeError(f"writer run failed with status={terminal_run['status']}")
            if chapter_path.read_bytes() != original_chapter:
                raise RuntimeError("project chapter changed before author Apply")

            workflow = client.get(f"/api/v1/agent/runs/{run_id}/workflow").json()
            if [step["skill_id"] for step in workflow["plan"]["steps"]] != list(
                EXPECTED_WORKFLOW_SKILL_IDS
            ):
                raise RuntimeError("writer workflow skill chain mismatch")
            if workflow["formal_draft_allowed"] is not False:
                raise RuntimeError("formal_draft_allowed must remain false")

            writer_result = client.get(
                f"/api/v1/agent/runs/{run_id}/writer-candidate-result"
            ).json()
            if writer_result["candidate_kind"] != WRITER_OUTPUT_CONTRACT["candidate_kind"]:
                raise RuntimeError("writer candidate kind mismatch")
            change_set_id = writer_result["change_set_id"]
            change_set = client.get(f"/api/v1/change-sets/{change_set_id}").json()
            hunks = [
                hunk for change in change_set["changes"] for hunk in change["hunks"]
            ]
            if not hunks:
                raise RuntimeError("writer change set has no hunks")
            decisions = client.patch(
                f"/api/v1/change-sets/{change_set_id}/decisions",
                json={
                    "expected_review_revision": change_set["review_revision"],
                    "decisions": [
                        {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
                        for hunk in hunks
                    ],
                },
            )
            decisions.raise_for_status()
            reviewed = decisions.json()

            gate = client.post(
                f"/api/v1/change-sets/{change_set_id}/post-draft-gate",
                json={"expected_review_revision": reviewed["review_revision"]},
            )
            gate.raise_for_status()
            gate_payload = gate.json()["post_draft_gate"]
            if gate_payload["status"] != "passed" or gate_payload["formal_draft_allowed"] is not False:
                raise RuntimeError("post-draft gate did not pass under formal_draft_allowed=false")

            applied = client.post(
                f"/api/v1/change-sets/{change_set_id}/apply",
                json={"expected_review_revision": reviewed["review_revision"]},
            )
            applied.raise_for_status()
            applied_payload = applied.json()
            if chapter_path.read_bytes() == original_chapter:
                raise RuntimeError("apply did not change chapter bytes")
            if b"before the old port closes" not in chapter_path.read_bytes():
                raise RuntimeError("applied chapter missing expected candidate text")

            evidence_after_apply = client.get(
                f"/api/v1/change-sets/{change_set_id}/writeback-evidence"
            )
            evidence_after_apply.raise_for_status()
            applied_evidence = evidence_after_apply.json()["receipt"]
            if applied_evidence["outcome"] != "applied":
                raise RuntimeError("writeback evidence outcome is not applied")

            sync_after_apply = client.post(
                f"/api/v1/change-sets/{change_set_id}/nlcl-writeback-sync",
                json={
                    "expected_writeback_evidence_receipt_hash": applied_evidence[
                        "receipt_hash"
                    ]
                },
            )
            sync_after_apply.raise_for_status()
            sync_receipt = sync_after_apply.json()["receipt"]
            if sync_receipt["original_nlcl_chapter_ops_usage_synced"] is not False:
                raise RuntimeError("sync over-claimed original NLCL semantics")

            rolled = client.post(
                f"/api/v1/change-sets/{change_set_id}/rollback",
                json={"expected_review_revision": applied_payload["review_revision"]},
            )
            rolled.raise_for_status()
            if chapter_path.read_bytes() != original_chapter:
                raise RuntimeError("rollback did not restore original chapter bytes")

            evidence_after_rollback = client.get(
                f"/api/v1/change-sets/{change_set_id}/writeback-evidence"
            )
            evidence_after_rollback.raise_for_status()
            rolled_evidence = evidence_after_rollback.json()["receipt"]
            if rolled_evidence["outcome"] != "rolled_back":
                raise RuntimeError("writeback evidence outcome is not rolled_back")

            sync_after_rollback = client.post(
                f"/api/v1/change-sets/{change_set_id}/nlcl-writeback-sync",
                json={
                    "expected_writeback_evidence_receipt_hash": rolled_evidence[
                        "receipt_hash"
                    ]
                },
            )
            sync_after_rollback.raise_for_status()

            evidence = {
                "schema": "zhimeng.local-epub-writer-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "inspect",
                    "create",
                    "wait",
                    "execution_receipt",
                    "candidates",
                    "review_promote",
                    "formalize",
                    "context_nlcl",
                    "writer_candidate",
                    "gate",
                    "apply",
                    "writeback_evidence",
                    "nlcl_writeback_sync",
                    "rollback",
                ],
                "source": {
                    "size_bytes": before_size,
                    "sha256": before_sha256,
                    "unchanged": False,
                },
                "packet": {
                    "source_sha256": source_receipt.packet_source_hash,
                    "text_sha256": source_receipt.packet_text_hash,
                    "hashes_independent": (
                        source_receipt.packet_source_hash
                        != source_receipt.packet_text_hash
                    ),
                    "char_count": packet.get("char_count"),
                    "chapter_marker_count": packet.get("chapter_marker_count"),
                },
                "draft": {"card_count": draft.get("card_count")},
                "source_receipt": {
                    "receipt_id": source_receipt.receipt_id,
                    "receipt_hash": source_receipt.receipt_hash,
                    "lineage_status": source_receipt.lineage_status,
                },
                "formalization": {
                    "schema": formalization["schema"],
                    "receipt_id": formalization["formalization_receipt_id"],
                    "receipt_hash": formalization["receipt_hash"],
                    "active_card_types": sorted(active_types),
                },
                "writer": {
                    "run_id": run_id,
                    "workflow_skill_ids": list(EXPECTED_WORKFLOW_SKILL_IDS),
                    "candidate_kind": writer_result["candidate_kind"],
                    "change_set_id": change_set_id,
                    "formal_draft_allowed": False,
                    "gate_status": gate_payload["status"],
                    "apply_result_revision": applied_payload["writeback"][
                        "result_revision"
                    ],
                    "rollback_result_revision": rolled.json()["rollback"][
                        "result_revision"
                    ],
                    "chapter_restored": True,
                },
                "writeback_evidence": {
                    "applied_receipt_id": applied_evidence["receipt_id"],
                    "rolled_back_receipt_id": rolled_evidence["receipt_id"],
                },
                "nlcl_writeback_sync": {
                    "schema": sync_receipt["schema"],
                    "receipt_id": sync_receipt["receipt_id"],
                    "sync_scope": sync_receipt["sync_scope"],
                    "original_nlcl_chapter_ops_usage_synced": False,
                    "novel_state_synced": False,
                },
                "claims": {
                    "external_epub_to_writer_apply_rollback": True,
                    "formal_draft_allowed": False,
                    "original_nlcl_chapter_ops_usage_synced": False,
                    "tomato_external_download": False,
                },
            }
    finally:
        try:
            shutil.rmtree(runtime_root)
        except OSError as exc:
            cleanup_error = exc.__class__.__name__

    runtime_remaining = runtime_root.exists()
    after_size = source_path.stat().st_size
    after_sha256 = _sha256_file(source_path)
    if evidence is None:
        raise RuntimeError("local EPUB writer acceptance produced no evidence")
    source_unchanged = before_size == after_size and before_sha256 == after_sha256
    evidence["source"]["unchanged"] = source_unchanged
    evidence["cleanup"] = {
        "status": "clean" if not runtime_remaining and cleanup_error is None else "failed",
        "runtime_state_remaining": runtime_remaining,
        "failure_type": cleanup_error,
    }
    if not source_unchanged:
        raise RuntimeError("acceptance source bytes changed")
    if runtime_remaining or cleanup_error is not None:
        raise RuntimeError("temporary Runtime state was not fully deleted")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epub", type=Path, required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = run_acceptance(
            args.epub,
            expected_sha256=args.expected_sha256,
            timeout_seconds=args.timeout_seconds,
        )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "zhimeng.local-epub-writer-live-acceptance.v1",
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
