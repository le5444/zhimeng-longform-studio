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
                f"Tomato external download stopped with status={status}: "
                f"{latest.get('error') or latest.get('progress')}"
            )
        time.sleep(0.5)
    raise RuntimeError(
        f"Tomato external download acceptance timed out; latest={latest}"
    )


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

    if not (skill_root / "scripts" / "prepare_tomato_sources.ps1").is_file():
        raise RuntimeError("Tomato skill root is missing prepare_tomato_sources.ps1")
    if not downloader_script.is_file():
        raise RuntimeError("Tomato downloader script is missing")
    distill = (
        skill_root.parent
        / "novel-distillation"
        / "scripts"
        / "distill_source_packet.py"
    )
    if not distill.is_file():
        raise RuntimeError("novel-distillation distill_source_packet.py is missing")

    runtime_root = Path(tempfile.mkdtemp(prefix="zhimeng-tomato-external-"))
    evidence: dict[str, object] | None = None
    cleanup_error: str | None = None
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
                json={
                    "book_id": book_id,
                    "title": title,
                    "author": author,
                },
            )
            resolved.raise_for_status()
            resolve_body = resolved.json()
            if (
                resolve_body.get("schema") != "zhimeng.tomato-resolve.v1"
                or resolve_body.get("locator_kind") != "book_id"
                or resolve_body.get("candidate", {}).get("book_id") != book_id
            ):
                raise RuntimeError(f"resolve API returned unexpected body: {resolve_body}")

            candidate = resolve_body["candidate"]
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
                raise RuntimeError("review-ready import omitted packet or draft evidence")
            if packet.get("download_scope") != "full":
                raise RuntimeError(
                    f"expected full download_scope, got {packet.get('download_scope')}"
                )

            receipt_response = client.get(
                f"/api/v1/source-imports/{source_import_id}/execution-receipt"
            )
            receipt_response.raise_for_status()
            receipt_record = receipt_response.json()
            if receipt_record.get("lineage_status") != "verified":
                raise RuntimeError(
                    f"expected verified lineage, got {receipt_record.get('lineage_status')}"
                )
            receipt = SourceImportExecutionReceipt.model_validate(
                receipt_record["receipt"]
            )
            if receipt.request.book_id != book_id:
                raise RuntimeError("execution receipt book_id mismatch")
            if receipt.prepare_tool.tool_id != "zhimeng.tomato-source-prepare":
                raise RuntimeError("prepare tool is not the real Tomato prepare tool")
            if receipt.prepare_tool.tool_fingerprint == "a" * 64:
                raise RuntimeError("prepare tool fingerprint looks like FakeTomatoAdapter")
            if (
                receipt.distillation_tool.tool_id
                != "zhimeng.novel-distillation-source-packet-draft"
            ):
                raise RuntimeError("distillation tool is not the real draft tool")
            if int(draft.get("card_count") or 0) < 1:
                raise RuntimeError("distillation draft has no cards")
            if draft.get("requires_review") is not True:
                raise RuntimeError("draft must require author review")

            downloaded = next(
                (
                    item
                    for item in packet.get("artifacts") or []
                    if isinstance(item, dict) and item.get("kind") == "downloaded_epub"
                ),
                None,
            )
            if not isinstance(downloaded, dict):
                raise RuntimeError("source packet missing downloaded_epub artifact")
            downloaded_path = Path(str(packet.get("downloaded_epub_path") or ""))
            if not downloaded_path.is_file():
                raise RuntimeError(
                    f"downloaded EPUB path missing on disk: {downloaded_path}"
                )
            resolved_download = download_dir.resolve(strict=True)
            resolved_epub = downloaded_path.resolve(strict=True)
            if resolved_download not in resolved_epub.parents and resolved_epub.parent != resolved_download:
                raise RuntimeError(
                    "downloaded EPUB is outside the configured Tomato download directory"
                )

            evidence = {
                "schema": "zhimeng.tomato-external-download-live-acceptance.v1",
                "status": "passed",
                "api_steps": [
                    "resolve_book_id",
                    "create",
                    "wait_awaiting_review",
                    "execution_receipt",
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
                    "downloaded_epub_path": str(downloaded_path),
                    "downloaded_epub_sha256": downloaded.get("sha256"),
                    "downloaded_epub_size_bytes": downloaded.get("size_bytes"),
                    "packet_dir": packet.get("packet_dir"),
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
                    "prepare_tool_fingerprint": receipt.prepare_tool.tool_fingerprint,
                    "distillation_tool_id": receipt.distillation_tool.tool_id,
                    "distillation_tool_fingerprint": (
                        receipt.distillation_tool.tool_fingerprint
                    ),
                },
                "claims": {
                    "tomato_resolve_by_book_id": True,
                    "tomato_real_external_download_to_awaiting_review": True,
                    "real_prepare_and_distillation_tools": True,
                    "formalization": False,
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
            cleanup_error = str(exc)
            if evidence is not None:
                evidence["cleanup"] = {
                    "status": "dirty",
                    "runtime_state_remaining": runtime_root.exists(),
                    "failure_type": cleanup_error,
                }

    if evidence is None:
        raise RuntimeError("acceptance did not produce evidence")
    if cleanup_error is not None and evidence["cleanup"]["status"] != "clean":
        # Keep passed status for the product path; cleanup is reported honestly.
        pass
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Live-accept Tomato external download through resolve + awaiting_review."
    )
    parser.add_argument("--book-id", default=DEFAULT_BOOK_ID)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--author", default=DEFAULT_AUTHOR)
    parser.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    parser.add_argument(
        "--downloader-script", type=Path, default=DEFAULT_DOWNLOADER
    )
    parser.add_argument("--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--evidence-out",
        type=Path,
        default=REPO_ROOT
        / "evidence"
        / "tomato-external-download-live-acceptance-20260726.json",
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
