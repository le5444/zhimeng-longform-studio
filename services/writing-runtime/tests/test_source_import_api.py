from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    PreparedSourcePacket,
    SourceArtifact,
    SourceImportJob,
    SourceImportProgress,
    SourceImportStatus,
    TomatoSearchCandidate,
)
from zhimeng_runtime.source_import_service import SourceImportService
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportAdapterExecutionIdentity,
    SourceImportExecutionReceipt,
    SourceImportToolEvidence,
    source_import_idempotency_key,
    source_import_run_id,
)
from zhimeng_runtime.tomato_skill_adapter import (
    TomatoAdapterCancelled,
    parse_search_candidates,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(kind: str, path: Path) -> SourceArtifact:
    return SourceArtifact(
        kind=kind,
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
    )


class FakeTomatoAdapter:
    def __init__(
        self,
        *,
        block_prepare: bool = False,
        mismatched_distillation_input: bool = False,
        change_identity_after_prepare: bool = False,
        external_download_root: Path | None = None,
        diverged_source_field: str | None = None,
        download_scope: str = "full",
    ) -> None:
        self.block_prepare = block_prepare
        self.mismatched_distillation_input = mismatched_distillation_input
        self.change_identity_after_prepare = change_identity_after_prepare
        self.external_download_root = external_download_root
        self.diverged_source_field = diverged_source_field
        self.download_scope = download_scope
        self.network_failures_remaining = 0
        self.identity_calls = 0
        self.search_queries: list[str] = []
        self.prepare_started = threading.Event()

    def execution_identity(self) -> SourceImportAdapterExecutionIdentity:
        self.identity_calls += 1
        prepare_fingerprint = (
            "c" * 64
            if self.change_identity_after_prepare and self.identity_calls > 1
            else "a" * 64
        )
        return SourceImportAdapterExecutionIdentity(
            prepare_tool=SourceImportToolEvidence(
                tool_id="fixture.tomato-source-prepare",
                tool_version="1",
                tool_fingerprint=prepare_fingerprint,
            ),
            distillation_tool=SourceImportToolEvidence(
                tool_id="fixture.source-packet-distillation",
                tool_version="1",
                tool_fingerprint="b" * 64,
            ),
        )

    def search(self, query: str) -> list[TomatoSearchCandidate]:
        self.search_queries.append(query)
        import re
        from zhimeng_runtime.tomato_skill_adapter import TomatoAdapterError

        if re.fullmatch(r"[0-9]+", query.strip()) or re.search(
            r"(?i)https?://|(?:book_id|bookId)=",
            query,
        ):
            raise TomatoAdapterError(
                "The name-search endpoint only accepts a novel title, not a book ID or URL"
            )
        if query == "missing":
            return []
        if query == "同名书":
            return [
                TomatoSearchCandidate(index=1, book_id="1001", title="同名书", author="甲"),
                TomatoSearchCandidate(index=2, book_id="1002", title="同名书", author="乙"),
            ]
        return [
            TomatoSearchCandidate(index=1, book_id="2001", title=query, author="作者甲"),
            TomatoSearchCandidate(index=2, book_id="2002", title=f"{query}续", author="作者乙"),
        ]

    def resolve(
        self,
        *,
        book_id: str | None = None,
        url: str | None = None,
        title: str,
        author: str,
    ) -> TomatoSearchCandidate:
        from zhimeng_runtime.tomato_skill_adapter import extract_tomato_book_id

        resolved_id, _kind, _locator = extract_tomato_book_id(book_id=book_id, url=url)
        return TomatoSearchCandidate(
            index=1,
            book_id=resolved_id,
            title=title,
            author=author,
        )

    def prepare(
        self,
        candidate: TomatoSearchCandidate,
        workspace: Path,
        cancel_event: threading.Event,
        on_progress,
    ) -> PreparedSourcePacket:
        self.prepare_started.set()
        while self.block_prepare and not cancel_event.wait(0.02):
            on_progress(
                {
                    "stage": "downloading",
                    "message": "running fixture download",
                    "saved_chapters": 2,
                    "total_chapters": 10,
                    "percent": 20,
                }
            )
        if cancel_event.is_set():
            raise TomatoAdapterCancelled("fixture cancelled")
        if self.network_failures_remaining > 0:
            from zhimeng_runtime.tomato_skill_adapter import TomatoAdapterError
            self.network_failures_remaining -= 1
            raise TomatoAdapterError("network timeout simulating external download")

        packet = workspace / "source-library" / "raw" / "tomato-downloads" / "packet"
        packet.mkdir(parents=True)
        if self.external_download_root is None:
            downloaded = workspace / "downloaded.epub"
        else:
            self.external_download_root.mkdir(parents=True, exist_ok=True)
            downloaded = self.external_download_root / f"{candidate.book_id}.epub"
        epub = packet / "book.epub"
        text = packet / "book.txt"
        metadata = packet / "book.metadata.json"
        manifest = packet / "packet.manifest.json"
        downloaded.write_bytes(b"fixture epub")
        epub.write_bytes(downloaded.read_bytes())
        text.write_text("第1章 开始\n测试正文。\n", encoding="utf-8")
        metadata.write_text('{"title":"测试书","author":"作者甲"}', encoding="utf-8")
        manifest.write_text('{"schema":"source.packet.v1"}', encoding="utf-8")
        artifacts = [
            _artifact("packet_manifest", manifest),
            _artifact("packet_epub", epub),
            _artifact("packet_text", text),
            _artifact("packet_metadata", metadata),
            _artifact("downloaded_epub", downloaded),
        ]
        source = PreparedSourcePacket(
            title=candidate.title,
            author=candidate.author,
            download_scope=self.download_scope,
            packet_dir=str(packet),
            packet_manifest_path=str(manifest),
            epub_path=str(epub),
            downloaded_epub_path=str(downloaded),
            text_path=str(text),
            metadata_path=str(metadata),
            char_count=14,
            chapter_marker_count=1,
            artifacts=artifacts,
        )
        if self.diverged_source_field is not None:
            original = Path(str(getattr(source, self.diverged_source_field)))
            divergent = packet / f"diverged-{self.diverged_source_field}{original.suffix}"
            divergent.write_bytes(original.read_bytes())
            source = source.model_copy(
                update={self.diverged_source_field: str(divergent)}
            )
        return source

    def draft(
        self,
        source: PreparedSourcePacket,
        workspace: Path,
        cancel_event: threading.Event,
    ) -> DistillationDraft:
        if cancel_event.is_set():
            raise TomatoAdapterCancelled("fixture cancelled")
        output = workspace / "distillation-draft"
        output.mkdir()
        cards = output / "cards.draft.json"
        report = output / "distillation_report.md"
        validation = output / "validation_report.md"
        envelope = output / "tool_result_envelope.json"
        cards.write_text(
            (
                '[{"card_id":"fixture-voice-card","card_type":"voice",'
                '"draft_candidate":true,"requires_human_distillation":true}]'
            ),
            encoding="utf-8",
        )
        report.write_text("# fixture report\n", encoding="utf-8")
        validation.write_text("# fixture validation\n", encoding="utf-8")
        envelope_payload = {
            "tool_result_envelope": "novel_distillation.source_packet_draft.v1",
            "status": "draft_ready",
            "input_artifacts": {
                "packet_manifest": source.packet_manifest_path,
                "packet_text": (
                    source.metadata_path
                    if self.mismatched_distillation_input
                    else source.text_path
                ),
                "packet_metadata": source.metadata_path,
                "packet_text_sha256": _sha256(Path(source.text_path)),
            },
            "output_artifacts": {
                "cards_draft_json": str(cards),
                "distillation_report_md": str(report),
                "validation_report_md": str(validation),
                "tool_result_envelope_json": str(envelope),
            },
            "checkpoints": {
                "packet_manifest_resolved": True,
                "packet_text_present": True,
                "draft_cards_written": True,
                "artifact_contract_complete": True,
                "card_count": 1,
                "cards_are_draft_candidates": True,
                "requires_human_distillation": True,
                "no_long_source_prose_stored": True,
            },
            "success_criteria": {"ready_for_formal_ingest": False},
        }
        envelope.write_text(
            __import__("json").dumps(envelope_payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return DistillationDraft(
            output_dir=str(output),
            cards_path=str(cards),
            report_path=str(report),
            validation_report_path=str(validation),
            envelope_path=str(envelope),
            card_count=1,
            artifacts=[
                _artifact("cards_draft_json", cards),
                _artifact("distillation_report_md", report),
                _artifact("validation_report_md", validation),
                _artifact("tool_result_envelope_json", envelope),
            ],
        )


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
    )


def _wait_for_status(
    client: TestClient,
    import_id: str,
    expected: str,
    *,
    timeout: float = 5.0,
) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/source-imports/{import_id}")
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] == expected:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"source import did not reach {expected}: {latest}")


def test_tomato_search_auto_selects_one_exact_title_and_pauses_on_same_name(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter()
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        exact = client.post(
            "/api/v1/source-imports/tomato/search",
            json={"query": "目标书"},
        )
        ambiguous = client.post(
            "/api/v1/source-imports/tomato/search",
            json={"query": "同名书"},
        )
        missing = client.post(
            "/api/v1/source-imports/tomato/search",
            json={"query": "missing"},
        )

    assert exact.status_code == 200, exact.text
    assert exact.json()["resolution"] == "auto_selected"
    assert exact.json()["selected_candidate"]["book_id"] == "2001"
    assert ambiguous.json()["resolution"] == "selection_required"
    assert [item["author"] for item in ambiguous.json()["candidates"]] == ["甲", "乙"]
    assert missing.json()["resolution"] == "not_found"


def test_tomato_resolve_accepts_book_id_and_url_without_using_name_search(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter()
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        by_id = client.post(
            "/api/v1/source-imports/tomato/resolve",
            json={
                "book_id": "778899",
                "title": "直接定位书",
                "author": "作者丙",
            },
        )
        by_url = client.post(
            "/api/v1/source-imports/tomato/resolve",
            json={
                "url": "https://fanqienovel.com/page/556677",
                "title": "URL定位书",
                "author": "作者丁",
            },
        )
        rejected_search = client.post(
            "/api/v1/source-imports/tomato/search",
            json={"query": "778899"},
        )
        both = client.post(
            "/api/v1/source-imports/tomato/resolve",
            json={
                "book_id": "1",
                "url": "https://fanqienovel.com/page/2",
                "title": "冲突",
                "author": "作者",
            },
        )
        bad_url = client.post(
            "/api/v1/source-imports/tomato/resolve",
            json={
                "url": "https://example.com/novel/no-id-here",
                "title": "坏链",
                "author": "作者",
            },
        )

    assert by_id.status_code == 200, by_id.text
    assert by_id.json()["schema"] == "zhimeng.tomato-resolve.v1"
    assert by_id.json()["locator_kind"] == "book_id"
    assert by_id.json()["candidate"]["book_id"] == "778899"
    assert by_id.json()["candidate"]["title"] == "直接定位书"
    assert by_url.status_code == 200, by_url.text
    assert by_url.json()["locator_kind"] == "url"
    assert by_url.json()["candidate"]["book_id"] == "556677"
    assert rejected_search.status_code == 502, rejected_search.text
    assert both.status_code == 422, both.text
    assert bad_url.status_code == 502, bad_url.text
    assert adapter.search_queries == ["778899"]


def test_skill_search_envelope_parses_from_the_right_and_deduplicates_book_ids() -> None:
    candidates = parse_search_candidates(
        {
            "ok": True,
            "mode": "list_only",
            "count": 1,
            "items": [
                {
                    "preview_lines": [
                        "Search results:",
                        "1. 书名 / 副标题 / 作者甲 / 123456",
                        "2. 另一部 / 作者乙 / 789012",
                        "3. 重复结果 / 作者甲 / 123456",
                    ]
                }
            ],
        }
    )

    assert [(item.book_id, item.title, item.author) for item in candidates] == [
        ("123456", "书名 / 副标题", "作者甲"),
        ("789012", "另一部", "作者乙"),
    ]


def test_source_import_runs_to_auditable_draft_and_persists_receipts(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter()
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "tomato",
                "query": "目标书",
                "candidate": {
                    "index": 1,
                    "book_id": "2001",
                    "title": "目标书",
                    "author": "作者甲",
                },
            },
        )
        assert created.status_code == 202, created.text
        finished = _wait_for_status(
            client,
            created.json()["import_id"],
            "awaiting_review",
        )
        listed = client.get("/api/v1/source-imports")
        receipt_response = client.get(
            f"/api/v1/source-imports/{finished['import_id']}/execution-receipt"
        )

    assert finished["source_packet"]["download_scope"] == "full"
    assert finished["distillation_draft"]["card_count"] == 1
    assert finished["distillation_draft"]["requires_review"] is True
    assert finished["status"] != "completed"
    assert listed.status_code == 200
    assert listed.json()["imports"][0]["import_id"] == finished["import_id"]
    assert receipt_response.status_code == 200, receipt_response.text
    receipt_record = receipt_response.json()
    assert receipt_record["lineage_status"] == "verified"
    receipt = SourceImportExecutionReceipt.model_validate(receipt_record["receipt"])
    assert receipt.source_import_id == finished["import_id"]
    assert receipt.run_id == source_import_run_id(finished["import_id"])
    assert receipt.revision == 1
    assert receipt.idempotency_key == source_import_idempotency_key(
        receipt.run_id,
        receipt.revision,
        receipt.request_hash,
    )
    assert receipt.request.book_id == "2001"
    assert receipt.prepare_tool.tool_fingerprint == "a" * 64
    assert receipt.distillation_tool.tool_fingerprint == "b" * 64
    assert receipt.cards_draft_hash == next(
        item["sha256"]
        for item in finished["distillation_draft"]["artifacts"]
        if item["kind"] == "cards_draft_json"
    )
    assert receipt.prepare_invocation_id != receipt.distillation_invocation_id
    persisted_store = SqliteStore(tmp_path / "runtime.sqlite3")
    persisted = persisted_store.get_source_import_job(finished["import_id"])
    assert persisted is not None
    assert persisted["status"] == "awaiting_review"
    persisted_receipt = persisted_store.get_source_import_execution_receipt(
        finished["import_id"]
    )
    assert persisted_receipt is not None
    assert persisted_receipt["receipt_hash"] == receipt.receipt_hash


def test_external_download_is_sealed_by_packet_copy_before_formalization(
    tmp_path: Path,
) -> None:
    external_download_root = tmp_path / "external-downloads"
    adapter = FakeTomatoAdapter(external_download_root=external_download_root)
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "tomato",
                "query": "外部下载封存书",
                "candidate": {
                    "index": 1,
                    "book_id": "2010",
                    "title": "外部下载封存书",
                    "author": "作者甲",
                },
            },
        )
        assert created.status_code == 202, created.text
        import_id = created.json()["import_id"]
        _wait_for_status(client, import_id, "awaiting_review")

        receipt_response = client.get(
            f"/api/v1/source-imports/{import_id}/execution-receipt"
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = SourceImportExecutionReceipt.model_validate(
            receipt_response.json()["receipt"]
        )
        source_artifacts = {item.kind: item for item in receipt.source_artifacts}
        downloaded = source_artifacts["downloaded_epub"]
        packet_copy = source_artifacts["packet_epub"]
        assert downloaded.relative_path.startswith("external/downloaded_epub/")
        assert (downloaded.size_bytes, downloaded.sha256) == (
            packet_copy.size_bytes,
            packet_copy.sha256,
        )

        Path(external_download_root / "2010.epub").unlink()
        candidates = client.post(
            f"/api/v1/knowledge/source-imports/{import_id}/candidates"
        )
        assert candidates.status_code == 201, candidates.text
        candidate_batch = candidates.json()
        assert candidate_batch["candidates"][0]["status"] == "pending"

        candidate = candidate_batch["candidates"][0]
        rejected = client.post(
            f"/api/v1/knowledge/candidates/{candidate['candidate_id']}/reject",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
            },
        )
        assert rejected.status_code == 200, rejected.text
        candidate_batch = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": import_id},
        ).json()

        finalized = client.post(
            f"/api/v1/knowledge/source-imports/{import_id}/finalize",
            json={
                "expected_source_receipt_hash": receipt.receipt_hash,
                "expected_draft_hash": receipt.cards_draft_hash,
                "expected_candidate_set_hash": candidate_batch["candidate_set_hash"],
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert finalized.json()["decisions"][0]["decision"] == "rejected"
        completed = client.get(f"/api/v1/source-imports/{import_id}")
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "completed"


@pytest.mark.parametrize(
    "field_name",
    (
        "packet_manifest_path",
        "text_path",
        "metadata_path",
        "epub_path",
        "downloaded_epub_path",
    ),
)
def test_source_packet_named_path_divergence_fails_without_publishing_receipt(
    tmp_path: Path,
    field_name: str,
) -> None:
    adapter = FakeTomatoAdapter(diverged_source_field=field_name)
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": f"路径分叉-{field_name}",
                "candidate": {
                    "index": 1,
                    "book_id": "2011",
                    "title": f"路径分叉-{field_name}",
                    "author": "作者甲",
                },
            },
        )
        assert created.status_code == 202, created.text
        failed = _wait_for_status(client, created.json()["import_id"], "failed")

    assert "source packet field does not match its artifact" in failed["error"]
    assert field_name in failed["error"]
    assert (
        SqliteStore(tmp_path / "runtime.sqlite3").get_source_import_execution_receipt(
            failed["import_id"]
        )
        is None
    )


def test_non_full_source_packet_fails_without_publishing_receipt(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter(download_scope="sample")
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "非全量来源",
                "candidate": {
                    "index": 1,
                    "book_id": "2012",
                    "title": "非全量来源",
                    "author": "作者甲",
                },
            },
        )
        assert created.status_code == 202, created.text
        failed = _wait_for_status(client, created.json()["import_id"], "failed")

    assert failed["error"] == "source packet download_scope must be full"
    assert (
        SqliteStore(tmp_path / "runtime.sqlite3").get_source_import_execution_receipt(
            failed["import_id"]
        )
        is None
    )


def test_distillation_input_mismatch_fails_without_publishing_receipt(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter(mismatched_distillation_input=True)
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "错绑书",
                "candidate": {
                    "index": 1,
                    "book_id": "2002",
                    "title": "错绑书",
                    "author": "作者乙",
                },
            },
        )
        assert created.status_code == 202, created.text
        failed = _wait_for_status(client, created.json()["import_id"], "failed")
        receipt_response = client.get(
            f"/api/v1/source-imports/{failed['import_id']}/execution-receipt"
        )

    assert "input does not match" in failed["error"]
    assert receipt_response.status_code == 200
    assert receipt_response.json()["lineage_status"] == "legacy_unverified"
    assert receipt_response.json()["receipt"] is None
    assert (
        SqliteStore(tmp_path / "runtime.sqlite3").get_source_import_execution_receipt(
            failed["import_id"]
        )
        is None
    )


def test_tool_identity_drift_fails_without_publishing_receipt(tmp_path: Path) -> None:
    adapter = FakeTomatoAdapter(change_identity_after_prepare=True)
    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "工具漂移书",
                "candidate": {
                    "index": 1,
                    "book_id": "2004",
                    "title": "工具漂移书",
                    "author": "作者丁",
                },
            },
        )
        assert created.status_code == 202, created.text
        failed = _wait_for_status(client, created.json()["import_id"], "failed")

    assert "tool identity changed" in failed["error"]
    assert (
        SqliteStore(tmp_path / "runtime.sqlite3").get_source_import_execution_receipt(
            failed["import_id"]
        )
        is None
    )


def test_tampered_execution_receipt_fails_integrity_validation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, source_import_adapter=FakeTomatoAdapter())
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "收据篡改书",
                "candidate": {
                    "index": 1,
                    "book_id": "2003",
                    "title": "收据篡改书",
                    "author": "作者丙",
                },
            },
        )
        assert created.status_code == 202, created.text
        finished = _wait_for_status(client, created.json()["import_id"], "awaiting_review")
        with client.app.state.runtime.store.connection() as connection:
            connection.execute(
                """
                UPDATE source_import_execution_receipt
                SET payload_json = '{}'
                WHERE source_import_id = ?
                """,
                (finished["import_id"],),
            )
        response = client.get(
            f"/api/v1/source-imports/{finished['import_id']}/execution-receipt"
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "source_import_receipt_invalid"
    assert response.json()["error"]["retryable"] is False


def test_tampered_execution_receipt_after_restart_without_adapter_maps_integrity_error(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, source_import_adapter=FakeTomatoAdapter())
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "无适配器重启篡改书",
                "candidate": {
                    "index": 1,
                    "book_id": "2013",
                    "title": "无适配器重启篡改书",
                    "author": "作者丁",
                },
            },
        )
        assert created.status_code == 202, created.text
        import_id = _wait_for_status(
            client,
            created.json()["import_id"],
            "awaiting_review",
        )["import_id"]

    store = SqliteStore(settings.database_path)
    with store.connection() as connection:
        connection.execute(
            """
            UPDATE source_import_execution_receipt
            SET run_id = ?
            WHERE source_import_id = ?
            """,
            ("source-import-run-" + "f" * 32, import_id),
        )

    with TestClient(create_app(settings)) as restarted:
        response = restarted.get(
            f"/api/v1/source-imports/{import_id}/execution-receipt"
        )

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "source_import_receipt_invalid"
    assert error["message"] == "Stored source import execution receipt is invalid"
    assert error["retryable"] is False


def test_tampered_execution_identity_columns_fail_integrity_validation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, source_import_adapter=FakeTomatoAdapter())
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "执行身份篡改书",
                "candidate": {
                    "index": 1,
                    "book_id": "2005",
                    "title": "执行身份篡改书",
                    "author": "作者戊",
                },
            },
        ).json()
        finished = _wait_for_status(client, created["import_id"], "awaiting_review")
        receipt = SourceImportExecutionReceipt.model_validate(
            client.get(
                f"/api/v1/source-imports/{finished['import_id']}/execution-receipt"
            ).json()["receipt"]
        )
        tampered_values = {
            "run_id": "source-import-run-" + "f" * 32,
            "revision": 2,
            "idempotency_key": "source-import-idempotency-" + "e" * 32,
        }
        original_values = {
            "run_id": receipt.run_id,
            "revision": receipt.revision,
            "idempotency_key": receipt.idempotency_key,
        }
        for column, value in tampered_values.items():
            with client.app.state.runtime.store.connection() as connection:
                connection.execute(
                    f"UPDATE source_import_execution_receipt SET {column} = ? "
                    "WHERE source_import_id = ?",
                    (value, finished["import_id"]),
                )
            response = client.get(
                f"/api/v1/source-imports/{finished['import_id']}/execution-receipt"
            )
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "source_import_receipt_invalid"
            with client.app.state.runtime.store.connection() as connection:
                connection.execute(
                    f"UPDATE source_import_execution_receipt SET {column} = ? "
                    "WHERE source_import_id = ?",
                    (original_values[column], finished["import_id"]),
                )


def test_tampered_job_candidate_cannot_be_bound_to_verified_receipt(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, source_import_adapter=FakeTomatoAdapter())
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "任务错绑书",
                "candidate": {
                    "index": 1,
                    "book_id": "2006",
                    "title": "任务错绑书",
                    "author": "作者己",
                },
            },
        ).json()
        finished = _wait_for_status(client, created["import_id"], "awaiting_review")
        with client.app.state.runtime.store.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_import_job WHERE id = ?",
                (finished["import_id"],),
            ).fetchone()
            assert row is not None
            payload = json.loads(str(row["payload_json"]))
            payload["candidate"]["title"] = "被替换的另一部书"
            connection.execute(
                "UPDATE source_import_job SET payload_json = ? WHERE id = ?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    finished["import_id"],
                ),
            )

        receipt_response = client.get(
            f"/api/v1/source-imports/{finished['import_id']}/execution-receipt"
        )
        candidates_response = client.post(
            f"/api/v1/knowledge/source-imports/{finished['import_id']}/candidates"
        )
        formalization_response = client.get(
            f"/api/v1/knowledge/source-imports/{finished['import_id']}/formalization"
        )

    assert receipt_response.status_code == 500
    assert candidates_response.status_code == 422
    assert candidates_response.json()["error"]["code"] == "knowledge_source_invalid"
    assert formalization_response.status_code == 422
    assert formalization_response.json()["error"]["code"] == "knowledge_source_invalid"


def test_job_payload_must_match_duplicated_columns_on_get_and_list(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    with TestClient(
        create_app(settings, source_import_adapter=FakeTomatoAdapter())
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "任务列校验书",
                "candidate": {
                    "index": 1,
                    "book_id": "2008",
                    "title": "任务列校验书",
                    "author": "作者辛",
                },
            },
        ).json()
        finished = _wait_for_status(client, created["import_id"], "awaiting_review")
        store = client.app.state.runtime.store
        with store.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_import_job WHERE id = ?",
                (finished["import_id"],),
            ).fetchone()
            assert row is not None
            original_payload_json = str(row["payload_json"])
        original = json.loads(original_payload_json)
        mutations = (
            ("query", "另一条查询"),
            ("book_id", "99999"),
            ("status", "completed"),
        )
        for field, value in mutations:
            payload = json.loads(original_payload_json)
            if field == "book_id":
                payload["candidate"]["book_id"] = value
            else:
                payload[field] = value
            with store.connection() as connection:
                connection.execute(
                    "UPDATE source_import_job SET payload_json = ? WHERE id = ?",
                    (
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        finished["import_id"],
                    ),
                )
            get_response = client.get(
                f"/api/v1/source-imports/{finished['import_id']}"
            )
            list_response = client.get("/api/v1/source-imports")
            assert get_response.status_code == 500
            assert list_response.status_code == 500
            assert get_response.json()["error"]["code"] == "source_import_receipt_invalid"
            assert list_response.json()["error"]["code"] == "source_import_receipt_invalid"
            with pytest.raises(RuntimeError, match="columns do not match"):
                store.list_source_import_jobs_with_status((original["status"],))
            with store.connection() as connection:
                connection.execute(
                    "UPDATE source_import_job SET payload_json = ? WHERE id = ?",
                    (original_payload_json, finished["import_id"]),
                )


def test_job_request_tamper_during_execution_prevents_receipt_publish(
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter(block_prepare=True)
    with TestClient(
        create_app(_settings(tmp_path), source_import_adapter=adapter)
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "发布前防错绑",
                "candidate": {
                    "index": 1,
                    "book_id": "2007",
                    "title": "发布前防错绑",
                    "author": "作者庚",
                },
            },
        ).json()
        assert adapter.prepare_started.wait(2)
        with client.app.state.runtime.store.connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM source_import_job WHERE id = ?",
                (created["import_id"],),
            ).fetchone()
            assert row is not None
            payload = json.loads(str(row["payload_json"]))
            payload["candidate"]["index"] = 2
            connection.execute(
                "UPDATE source_import_job SET payload_json = ? WHERE id = ?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created["import_id"],
                ),
            )
        adapter.block_prepare = False
        failed = _wait_for_status(client, created["import_id"], "failed")

    assert "job request changed" in failed["error"]
    assert (
        SqliteStore(tmp_path / "runtime.sqlite3").get_source_import_execution_receipt(
            created["import_id"]
        )
        is None
    )


def test_running_source_import_can_cancel_and_retry(tmp_path: Path) -> None:
    adapter = FakeTomatoAdapter(block_prepare=True)
    app = create_app(_settings(tmp_path), source_import_adapter=adapter)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "慢书",
                "candidate": {
                    "index": 1,
                    "book_id": "3001",
                    "title": "慢书",
                    "author": "作者",
                },
            },
        ).json()
        assert adapter.prepare_started.wait(2)
        cancelled_response = client.post(
            f"/api/v1/source-imports/{created['import_id']}/cancel"
        )
        assert cancelled_response.status_code == 200, cancelled_response.text
        cancelled = _wait_for_status(client, created["import_id"], "cancelled")

        adapter.block_prepare = False
        retried_response = client.post(
            f"/api/v1/source-imports/{created['import_id']}/retry"
        )
        assert retried_response.status_code == 202, retried_response.text
        retried = _wait_for_status(
            client,
            retried_response.json()["import_id"],
            "awaiting_review",
        )
        retried_receipt_response = client.get(
            f"/api/v1/source-imports/{retried['import_id']}/execution-receipt"
        )

    assert cancelled["error"] == "fixture cancelled"
    assert retried["retry_of"] == created["import_id"]
    assert retried_receipt_response.status_code == 200
    retried_receipt = SourceImportExecutionReceipt.model_validate(
        retried_receipt_response.json()["receipt"]
    )
    assert retried_receipt.retry_of == created["import_id"]
    assert retried_receipt.run_id == source_import_run_id(retried["import_id"])
    assert retried_receipt.idempotency_key == source_import_idempotency_key(
        retried_receipt.run_id,
        1,
        retried_receipt.request_hash,
    )


def test_cancel_requested_wins_at_draft_commit_boundary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    adapter = FakeTomatoAdapter()
    draft_commit_started = threading.Event()
    release_draft_commit = threading.Event()
    original_transition = SourceImportService._transition

    def pause_before_awaiting_review(self, record, **updates):
        if (
            updates.get("status") is SourceImportStatus.AWAITING_REVIEW
            and threading.current_thread().name.startswith("zhimeng-source-import")
        ):
            draft_commit_started.set()
            if not release_draft_commit.wait(5):
                raise AssertionError("draft commit was not released")
        return original_transition(self, record, **updates)

    monkeypatch.setattr(
        SourceImportService,
        "_transition",
        pause_before_awaiting_review,
    )

    with TestClient(create_app(_settings(tmp_path), source_import_adapter=adapter)) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "边界书",
                "candidate": {
                    "index": 1,
                    "book_id": "3002",
                    "title": "边界书",
                    "author": "作者",
                },
            },
        ).json()
        try:
            assert draft_commit_started.wait(2)
            cancelling = client.post(
                f"/api/v1/source-imports/{created['import_id']}/cancel"
            )
            assert cancelling.status_code == 200, cancelling.text
            assert cancelling.json()["status"] == "cancel_requested"
        finally:
            release_draft_commit.set()

        cancelled = _wait_for_status(client, created["import_id"], "cancelled")

    assert cancelled["status"] == "cancelled"
    assert cancelled["distillation_draft"] is None


def test_startup_marks_unfinished_import_interrupted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    now = datetime.now(UTC)
    record = SourceImportJob(
        import_id="source-import-0123456789abcdef0123456789abcdef",
        query="中断书",
        candidate=TomatoSearchCandidate(
            index=1,
            book_id="4001",
            title="中断书",
            author="作者",
        ),
        status=SourceImportStatus.DOWNLOADING,
        progress=SourceImportProgress(
            stage="downloading",
            message="old Runtime was downloading",
            saved_chapters=4,
            total_chapters=20,
            percent=20,
        ),
        created_at=now,
        updated_at=now,
    )
    store.create_source_import_job(record.model_dump(mode="json", by_alias=True))

    with TestClient(create_app(settings, source_import_adapter=FakeTomatoAdapter())) as client:
        recovered = client.get(f"/api/v1/source-imports/{record.import_id}")
        legacy_receipt = client.get(
            f"/api/v1/source-imports/{record.import_id}/execution-receipt"
        )

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "interrupted"
    assert recovered.json()["progress"]["saved_chapters"] == 4
    assert "retry" in recovered.json()["error"]
    assert legacy_receipt.status_code == 200
    assert legacy_receipt.json() == {
        "schema": "zhimeng.source-import-execution-receipt-record.v1",
        "source_import_id": record.import_id,
        "lineage_status": "legacy_unverified",
        "receipt": None,
    }


def test_source_import_endpoints_are_explicitly_unavailable_without_adapter(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/source-imports/tomato/search",
            json={"query": "目标书"},
        )

    assert response.status_code == 503


def test_tomato_book_id_resolve_and_network_recovery_evidence(tmp_path: Path) -> None:
    adapter = FakeTomatoAdapter()
    adapter.network_failures_remaining = 1
    app = create_app(_settings(tmp_path), source_import_adapter=adapter)
    with TestClient(app) as client:
        resolved = client.post(
            "/api/v1/source-imports/tomato/resolve",
            json={"book_id": "778899", "title": "网络恢复书", "author": "作者"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["locator_kind"] == "book_id"
        candidate = resolved.json()["candidate"]

        created = client.post(
            "/api/v1/source-imports",
            json={
                "query": "网络恢复书",
                "candidate": {
                    "index": candidate["index"],
                    "book_id": candidate["book_id"],
                    "title": candidate["title"],
                    "author": candidate["author"],
                },
            },
        )
        assert created.status_code in {200, 201, 202}, created.text
        failed = _wait_for_status(client, created.json()["import_id"], "failed")
        assert "network timeout" in (failed.get("error") or "")

        retried_response = client.post(
            f"/api/v1/source-imports/{failed['import_id']}/retry"
        )
        assert retried_response.status_code == 202, retried_response.text
        recovered = _wait_for_status(
            client,
            retried_response.json()["import_id"],
            "awaiting_review",
        )
        assert recovered["retry_of"] == failed["import_id"]

        evidence = client.post(
            f"/api/v1/source-imports/{recovered['import_id']}/network-recovery-evidence"
        )
        assert evidence.status_code == 201, evidence.text
        body = evidence.json()
        assert body["schema"] == "zhimeng.source-import-network-recovery-evidence-response.v1"
        payload = body["evidence"]
        assert payload["network_failure_detected"] is True
        assert payload["recovered"] is True
        assert payload["live_external_download_claimed"] is False
        assert payload["formal_draft_allowed"] is False
        assert "live_external_download_acceptance" in body["not_claiming"]
