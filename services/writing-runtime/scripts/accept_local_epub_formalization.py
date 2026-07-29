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
REPO_ROOT = WRITING_RUNTIME.parents[1]
sys.path.insert(0, str(WRITING_RUNTIME / "src"))
sys.path.insert(0, str(WRITING_RUNTIME / "tests"))

from zhimeng_runtime.app import create_app  # noqa: E402
from zhimeng_runtime.config import RuntimeSettings  # noqa: E402
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    LocalSourceImportExecutionReceipt,
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
    raise RuntimeError("local EPUB formalization acceptance timed out")


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

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-epub-formalization-"))
    library = runtime_root / "library"
    project = library / "novel"
    evidence: dict[str, object] | None = None
    cleanup_error: str | None = None
    try:
        for index, label in {
            1: "项目定位",
            2: "世界观设定",
            3: "人物设定",
            4: "剧情大纲",
            5: "正文内容",
            6: "写作规范",
            7: "运营数据",
            8: "素材库",
            9: "完本复盘",
        }.items():
            (project / f"{index:02d}_{label}").mkdir(parents=True, exist_ok=True)
        (project / "05_正文内容" / "第一卷").mkdir(parents=True, exist_ok=True)
        (project / "05_正文内容" / "第一卷" / "CH001_正文.md").write_text(
            "placeholder chapter\n",
            encoding="utf-8",
        )
        settings = RuntimeSettings(
            environment="acceptance",
            database_path=runtime_root / "runtime.sqlite3",
            allowed_origins=(),
            trusted_source_roots=(source_path.parent,),
            trusted_project_roots=(library,),
        )
        with TestClient(create_app(settings)) as client:
            inspected_project = client.post(
                "/api/v1/projects/inspect",
                json={"root_path": str(project)},
            )
            inspected_project.raise_for_status()
            project_id = inspected_project.json()["project"]["project_id"]
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
            receipt = LocalSourceImportExecutionReceipt.model_validate(
                receipt_response.json()["receipt"]
            )
            if (
                receipt.source_format != "epub"
                or receipt.packet_source_hash != before_sha256
                or receipt.packet_text_hash == before_sha256
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
                    "expected_source_receipt_hash": receipt.receipt_hash,
                    "expected_draft_hash": receipt.cards_draft_hash,
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
                raise RuntimeError(f"source import status is {job_status}, expected completed")
            cards = client.get(f"{knowledge_path}/cards")
            cards.raise_for_status()
            active_types = {
                item["active_version"]["card"]["card_type"]
                for item in cards.json()["cards"]
            }
            if active_types != set(builders):
                raise RuntimeError("active formal cards did not cover work/voice/validator")
            for item in cards.json()["cards"]:
                active = item["active_version"]
                if (
                    active["formalization_status"] != "verified"
                    or active["lineage_status"] != "verified"
                ):
                    raise RuntimeError("formal cards are not verified")

            evidence = {
                "schema": "zhimeng.local-epub-formalization-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "inspect",
                    "create",
                    "wait",
                    "execution_receipt",
                    "candidates",
                    "review_promote",
                    "formalize",
                    "verified_cards",
                ],
                "source": {
                    "size_bytes": before_size,
                    "sha256": before_sha256,
                    "unchanged": False,
                },
                "packet": {
                    "source_sha256": receipt.packet_source_hash,
                    "text_sha256": receipt.packet_text_hash,
                    "hashes_independent": (
                        receipt.packet_source_hash != receipt.packet_text_hash
                    ),
                    "char_count": packet.get("char_count"),
                    "chapter_marker_count": packet.get("chapter_marker_count"),
                },
                "draft": {"card_count": draft.get("card_count")},
                "source_receipt": {
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": receipt.receipt_hash,
                    "lineage_status": receipt.lineage_status,
                },
                "formalization": {
                    "schema": formalization["schema"],
                    "receipt_id": formalization["formalization_receipt_id"],
                    "receipt_hash": formalization["receipt_hash"],
                    "decisions": ["promoted", "promoted", "promoted"],
                    "active_card_types": sorted(active_types),
                },
                "claims": {
                    "external_epub_to_verified_knowledge": True,
                    "writer_apply_rollback": False,
                    "formal_draft_allowed": False,
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
        raise RuntimeError("local EPUB formalization acceptance produced no evidence")
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
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
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
                    "schema": "zhimeng.local-epub-formalization-live-acceptance.v1",
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
