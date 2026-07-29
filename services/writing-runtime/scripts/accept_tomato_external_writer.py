from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient


WRITING_RUNTIME = Path(__file__).resolve().parents[1]
REPO_ROOT = WRITING_RUNTIME.parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(WRITING_RUNTIME / "tests"))

from zhimeng_runtime.app import create_app  # noqa: E402
from zhimeng_runtime.config import DEFAULT_MANAGED_SKILL_ROOT, RuntimeSettings  # noqa: E402
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    SourceImportExecutionReceipt,
)
from local_source_fixture import (  # noqa: E402
    EXPECTED_CARD_TYPE_SET,
    EXPECTED_WORKFLOW_SKILL_IDS,
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


DEFAULT_SKILL_ROOT = (
    Path.home() / ".codex" / "skills" / "tomato-novel-auto-distill"
)
DEFAULT_DOWNLOADER = (
    Path.home()
    / "Documents"
    / "Codex"
    / "tools"
    / "tomato-novel-downloader"
    / "Download-TomatoNovel.ps1"
)
DEFAULT_DOWNLOAD_DIR = Path.home() / "Desktop" / "番茄下载"
DEFAULT_BOOK_ID = "7645931910685805630"
DEFAULT_TITLE = "开局拜入圣地，发现祖上是人皇！"
DEFAULT_AUTHOR = "我爱码字我爱写作"


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
            raise RuntimeError(
                f"Tomato writer acceptance stopped with status={status}: "
                f"{latest.get('error') or latest.get('progress')}"
            )
        time.sleep(0.5)
    raise RuntimeError(f"Tomato writer acceptance timed out; latest={latest}")


def run_acceptance(
    *,
    book_id: str,
    title: str,
    author: str,
    skill_root: Path,
    downloader_script: Path,
    download_dir: Path,
    timeout_seconds: float,
) -> dict[str, object]:
    skill_root = skill_root.expanduser().resolve(strict=True)
    downloader_script = downloader_script.expanduser().resolve(strict=True)
    download_dir = download_dir.expanduser().resolve(strict=False)
    download_dir.mkdir(parents=True, exist_ok=True)

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-tomato-writer-"))
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
        settings = RuntimeSettings(
            environment="acceptance",
            database_path=runtime_root / "runtime.sqlite3",
            allowed_origins=(),
            trusted_project_roots=(library,),
            managed_skill_root=DEFAULT_MANAGED_SKILL_ROOT,
            tomato_skill_root=skill_root,
            tomato_downloader_script=downloader_script,
            tomato_download_dir=download_dir,
            tomato_server_url="http://127.0.0.1:18423",
            tomato_max_wait_minutes=max(5, int(timeout_seconds // 60) or 5),
            sse_heartbeat_seconds=0.05,
        )
        app = create_app(settings, agent_provider=provider)
        with TestClient(app) as client:
            inspection = client.post(
                "/api/v1/projects/inspect",
                json={"root_path": str(project)},
            )
            inspection.raise_for_status()
            snapshot = inspection.json()
            project_id = snapshot["project"]["project_id"]
            knowledge_path = f"/api/v1/projects/{project_id}/knowledge"

            resolved = client.post(
                "/api/v1/source-imports/tomato/resolve",
                json={"book_id": book_id, "title": title, "author": author},
            )
            resolved.raise_for_status()
            candidate = resolved.json()["candidate"]
            created = client.post(
                "/api/v1/source-imports",
                json={
                    "provider": "tomato",
                    "query": book_id,
                    "candidate": candidate,
                },
            )
            if created.status_code != 202:
                raise RuntimeError(
                    f"create source import failed: {created.status_code} {created.text}"
                )
            source_import_id = created.json()["import_id"]
            job = _wait_for_review(client, source_import_id, timeout_seconds)
            packet = job.get("source_packet")
            draft = job.get("distillation_draft")
            if not isinstance(packet, dict) or not isinstance(draft, dict):
                raise RuntimeError("review-ready import omitted packet or draft")
            if packet.get("download_scope") != "full":
                raise RuntimeError("expected full download_scope")

            receipt_response = client.get(
                f"/api/v1/source-imports/{source_import_id}/execution-receipt"
            )
            receipt_response.raise_for_status()
            receipt_record = receipt_response.json()
            if receipt_record.get("lineage_status") != "verified":
                raise RuntimeError("expected verified lineage")
            source_receipt = SourceImportExecutionReceipt.model_validate(
                receipt_record["receipt"]
            )
            if source_receipt.prepare_tool.tool_id != "zhimeng.tomato-source-prepare":
                raise RuntimeError("prepare tool is not real Tomato prepare")

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
            job_status = client.get(f"/api/v1/source-imports/{source_import_id}").json()[
                "status"
            ]
            if job_status != "completed":
                raise RuntimeError(f"source import status is {job_status}")

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
                key_prefix="tomato-external-writer-0001",
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
                raise RuntimeError(
                    f"writer run failed with status={terminal_run['status']}"
                )
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
            if (
                gate_payload["status"] != "passed"
                or gate_payload["formal_draft_allowed"] is not False
            ):
                raise RuntimeError(
                    "post-draft gate did not pass under formal_draft_allowed=false"
                )

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

            usage_after_apply = client.post(
                f"/api/v1/change-sets/{change_set_id}/nlcl-call-usage-continuity",
                json={
                    "expected_nlcl_writeback_sync_receipt_hash": sync_receipt[
                        "receipt_hash"
                    ]
                },
            )
            usage_after_apply.raise_for_status()
            usage_applied = usage_after_apply.json()["receipt"]
            if usage_applied["original_nlcl_call_usage_recorded"] is not False:
                raise RuntimeError("call_usage continuity over-claimed original usage")
            if set(usage_applied["used_card_ids"]) != set(
                usage_applied["selected_card_ids"]
            ):
                raise RuntimeError(
                    "applied call_usage continuity did not mark selected cards as used"
                )

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
            sync_rolled = sync_after_rollback.json()["receipt"]

            usage_after_rollback = client.post(
                f"/api/v1/change-sets/{change_set_id}/nlcl-call-usage-continuity",
                json={
                    "expected_nlcl_writeback_sync_receipt_hash": sync_rolled[
                        "receipt_hash"
                    ]
                },
            )
            usage_after_rollback.raise_for_status()
            usage_rolled = usage_after_rollback.json()["receipt"]
            if usage_rolled["used_card_ids"] != []:
                raise RuntimeError(
                    "rolled_back call_usage continuity must void the used set"
                )

            evidence = {
                "schema": "zhimeng.tomato-external-writer-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "resolve_book_id",
                    "create",
                    "wait_awaiting_review",
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
                    "nlcl_call_usage_continuity",
                    "rollback",
                ],
                "locator": {
                    "kind": "book_id",
                    "book_id": book_id,
                    "title": title,
                    "author": author,
                },
                "source_import_id": source_import_id,
                "packet": {
                    "download_scope": packet.get("download_scope"),
                    "title": packet.get("title"),
                    "author": packet.get("author"),
                    "downloaded_epub_path": packet.get("downloaded_epub_path"),
                    "char_count": packet.get("char_count"),
                    "chapter_marker_count": packet.get("chapter_marker_count"),
                },
                "draft": {"card_count": draft.get("card_count")},
                "source_receipt": {
                    "receipt_id": source_receipt.receipt_id,
                    "receipt_hash": source_receipt.receipt_hash,
                    "lineage_status": receipt_record.get("lineage_status"),
                    "prepare_tool_id": source_receipt.prepare_tool.tool_id,
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
                "nlcl_call_usage_continuity": {
                    "applied_receipt_id": usage_applied["receipt_id"],
                    "rolled_back_receipt_id": usage_rolled["receipt_id"],
                    "applied_used_card_count": len(usage_applied["used_card_ids"]),
                    "rolled_back_used_card_count": len(usage_rolled["used_card_ids"]),
                    "original_nlcl_call_usage_recorded": False,
                },
                "claims": {
                    "tomato_external_to_writer_apply_rollback": True,
                    "formal_draft_allowed": False,
                    "original_nlcl_chapter_ops_usage_synced": False,
                    "original_nlcl_call_usage_recorded": False,
                },
                "cleanup": {
                    "status": "pending",
                    "runtime_state_remaining": True,
                    "failure_type": None,
                },
            }
    finally:
        try:
            shutil.rmtree(runtime_root, ignore_errors=False)
        except OSError as exc:
            cleanup_error = exc.__class__.__name__

    runtime_remaining = runtime_root.exists()
    if evidence is None:
        raise RuntimeError("Tomato writer acceptance produced no evidence")
    evidence["cleanup"] = {
        "status": (
            "clean" if not runtime_remaining and cleanup_error is None else "failed"
        ),
        "runtime_state_remaining": runtime_remaining,
        "failure_type": cleanup_error,
    }
    if runtime_remaining or cleanup_error is not None:
        raise RuntimeError("temporary Runtime state was not fully deleted")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live-accept Tomato external download through Writer Apply/Rollback."
        )
    )
    parser.add_argument("--book-id", default=DEFAULT_BOOK_ID)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--author", default=DEFAULT_AUTHOR)
    parser.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    parser.add_argument("--downloader-script", type=Path, default=DEFAULT_DOWNLOADER)
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=REPO_ROOT
        / "evidence"
        / "tomato-external-writer-live-acceptance-20260726.json",
    )
    args = parser.parse_args(argv)
    try:
        evidence = run_acceptance(
            book_id=args.book_id,
            title=args.title,
            author=args.author,
            skill_root=args.skill_root,
            downloader_script=args.downloader_script,
            download_dir=args.download_dir,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema": "zhimeng.tomato-external-writer-live-acceptance.v1",
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    args.evidence_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    print(f"evidence_written={args.evidence_out}")
    return 0 if evidence.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
