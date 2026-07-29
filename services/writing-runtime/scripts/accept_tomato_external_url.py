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

from zhimeng_runtime.app import create_app  # noqa: E402
from zhimeng_runtime.config import RuntimeSettings  # noqa: E402
from zhimeng_runtime.source_import_receipt_models import (  # noqa: E402
    SourceImportExecutionReceipt,
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
DEFAULT_URL = f"https://fanqienovel.com/page/{DEFAULT_BOOK_ID}"
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
                f"Tomato URL acceptance stopped with status={status}: "
                f"{latest.get('error') or latest.get('progress')}"
            )
        time.sleep(0.5)
    raise RuntimeError(f"Tomato URL acceptance timed out; latest={latest}")


def run_acceptance(
    *,
    url: str,
    expected_book_id: str,
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

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-tomato-url-"))
    evidence: dict[str, object] | None = None
    try:
        settings = RuntimeSettings(
            environment="acceptance",
            database_path=runtime_root / "runtime.sqlite3",
            allowed_origins=(),
            tomato_skill_root=skill_root,
            tomato_downloader_script=downloader_script,
            tomato_download_dir=download_dir,
            tomato_server_url="http://127.0.0.1:18423",
            tomato_max_wait_minutes=max(5, int(timeout_seconds // 60) or 5),
        )
        with TestClient(create_app(settings)) as client:
            resolved = client.post(
                "/api/v1/source-imports/tomato/resolve",
                json={"url": url, "title": title, "author": author},
            )
            resolved.raise_for_status()
            body = resolved.json()
            if body.get("schema") != "zhimeng.tomato-resolve.v1":
                raise RuntimeError("resolve schema mismatch")
            if body.get("locator_kind") != "url":
                raise RuntimeError(f"expected locator_kind=url, got {body.get('locator_kind')}")
            candidate = body["candidate"]
            if candidate.get("book_id") != expected_book_id:
                raise RuntimeError(
                    f"URL resolve book_id mismatch: {candidate.get('book_id')}"
                )

            created = client.post(
                "/api/v1/source-imports",
                json={
                    "provider": "tomato",
                    "query": expected_book_id,
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
            if receipt.request.book_id != expected_book_id:
                raise RuntimeError("execution receipt book_id mismatch")
            if receipt.prepare_tool.tool_id != "zhimeng.tomato-source-prepare":
                raise RuntimeError("prepare tool is not real Tomato prepare")

            evidence = {
                "schema": "zhimeng.tomato-external-url-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "resolve_url",
                    "create",
                    "wait_awaiting_review",
                    "execution_receipt",
                ],
                "locator": {
                    "kind": "url",
                    "url": url,
                    "book_id": expected_book_id,
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
                    "requires_review": draft.get("requires_review"),
                },
                "receipt": {
                    "receipt_id": receipt.receipt_id,
                    "receipt_hash": receipt.receipt_hash,
                    "lineage_status": receipt_record.get("lineage_status"),
                    "prepare_tool_id": receipt.prepare_tool.tool_id,
                },
                "claims": {
                    "tomato_resolve_by_url": True,
                    "tomato_url_external_download_to_awaiting_review": True,
                    "formalization": False,
                    "writer_apply_rollback": False,
                    "formal_draft_allowed": False,
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
        description="Live-accept Tomato URL resolve + external download to awaiting_review."
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--expected-book-id", default=DEFAULT_BOOK_ID)
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
        / "tomato-external-url-live-acceptance-20260726.json",
    )
    args = parser.parse_args(argv)
    evidence = run_acceptance(
        url=args.url,
        expected_book_id=args.expected_book_id,
        title=args.title,
        author=args.author,
        skill_root=args.skill_root,
        downloader_script=args.downloader_script,
        download_dir=args.download_dir,
        timeout_seconds=args.timeout_seconds,
    )
    args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    args.evidence_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    print(f"evidence_written={args.evidence_out}")
    return 0 if evidence.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
