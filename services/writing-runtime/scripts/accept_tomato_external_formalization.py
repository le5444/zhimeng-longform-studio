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
from zhimeng_runtime.config import RuntimeSettings  # noqa: E402
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    SourceImportExecutionReceipt,
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
                f"Tomato formalization stopped with status={status}: "
                f"{latest.get('error') or latest.get('progress')}"
            )
        time.sleep(0.5)
    raise RuntimeError(f"Tomato formalization acceptance timed out; latest={latest}")


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

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-tomato-formalization-"))
    library = runtime_root / "library"
    project = library / "novel"
    evidence: dict[str, object] | None = None
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
            trusted_project_roots=(library,),
            tomato_skill_root=skill_root,
            tomato_downloader_script=downloader_script,
            tomato_download_dir=download_dir,
            tomato_server_url="http://127.0.0.1:18423",
            tomato_max_wait_minutes=max(5, int(timeout_seconds // 60) or 5),
        )
        with TestClient(create_app(settings)) as client:
            inspected_project = client.post(
                "/api/v1/projects/inspect",
                json={"root_path": str(project)},
            )
            inspected_project.raise_for_status()
            project_id = inspected_project.json()["project"]["project_id"]
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
            receipt = SourceImportExecutionReceipt.model_validate(
                receipt_record["receipt"]
            )
            if receipt.prepare_tool.tool_id != "zhimeng.tomato-source-prepare":
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
            present = {item["card_type"] for item in candidates}
            if not present.issubset(set(builders)) or not present:
                raise RuntimeError(f"unexpected draft candidate types: {present}")
            # Tomato draft may not always emit all three; formalize only present types.
            for item in candidates:
                card_type = item["card_type"]
                if card_type not in builders:
                    raise RuntimeError(f"unsupported card type {card_type}")
                reviewed = client.put(
                    f"{knowledge_path}/candidates/{item['candidate_id']}",
                    json={
                        "expected_revision": item["revision"],
                        "expected_content_hash": item["content_hash"],
                        "card": builders[card_type](item["card_id"]),
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
            decisions = [item["decision"] for item in formalization["decisions"]]
            if set(decisions) != {"promoted"} or len(decisions) != len(candidates):
                raise RuntimeError(f"unexpected formalization decisions: {decisions}")
            job_status = client.get(f"/api/v1/source-imports/{source_import_id}").json()[
                "status"
            ]
            if job_status != "completed":
                raise RuntimeError(f"source import status is {job_status}")
            cards = client.get(f"{knowledge_path}/cards")
            cards.raise_for_status()
            active_types = {
                item["active_version"]["card"]["card_type"]
                for item in cards.json()["cards"]
            }
            if active_types != present:
                raise RuntimeError(
                    f"active formal cards {active_types} != draft types {present}"
                )
            for item in cards.json()["cards"]:
                active = item["active_version"]
                if (
                    active["formalization_status"] != "verified"
                    or active["lineage_status"] != "verified"
                ):
                    raise RuntimeError("formal cards are not verified")

            evidence = {
                "schema": "zhimeng.tomato-external-formalization-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "resolve_book_id",
                    "create",
                    "wait_awaiting_review",
                    "execution_receipt",
                    "candidates",
                    "review_promote",
                    "formalize",
                    "verified_cards",
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
                "draft": {
                    "card_count": draft.get("card_count"),
                    "card_types": sorted(present),
                },
                "source_receipt": {
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": receipt.receipt_hash,
                    "lineage_status": receipt_record.get("lineage_status"),
                    "prepare_tool_id": receipt.prepare_tool.tool_id,
                },
                "formalization": {
                    "schema": formalization["schema"],
                    "receipt_id": formalization["formalization_receipt_id"],
                    "receipt_hash": formalization["receipt_hash"],
                    "decisions": decisions,
                    "active_card_types": sorted(active_types),
                },
                "claims": {
                    "tomato_real_external_download": True,
                    "tomato_external_to_verified_knowledge": True,
                    "writer_apply_rollback": False,
                    "formal_draft_allowed": False,
                    "original_nlcl_usage_closure": False,
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
            remaining = runtime_root.exists()
            if evidence is not None:
                evidence["cleanup"] = {
                    "status": "dirty" if remaining else "clean",
                    "runtime_state_remaining": remaining,
                    "failure_type": None,
                }
        except OSError as exc:
            if evidence is not None:
                evidence["cleanup"] = {
                    "status": "dirty",
                    "runtime_state_remaining": runtime_root.exists(),
                    "failure_type": str(exc),
                }

    if evidence is None:
        raise RuntimeError("acceptance did not produce evidence")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Live-accept Tomato external download through verified knowledge formalization."
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
        / "tomato-external-formalization-live-acceptance-20260726.json",
    )
    args = parser.parse_args(argv)
    evidence = run_acceptance(
        book_id=args.book_id,
        title=args.title,
        author=args.author,
        skill_root=args.skill_root,
        downloader_script=args.downloader_script,
        download_dir=args.download_dir,
        timeout_seconds=args.timeout_seconds,
    )
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    args.evidence_out.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"evidence_written={args.evidence_out}")
    return 0 if evidence.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
