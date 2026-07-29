from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    LocalFileSourceCandidate,
    LocalSourceImportCreateRequest,
    LocalSourceImportJob,
    PreparedLocalSourcePacket,
    SourceArtifact,
    SourceImportCreateRequest,
    SourceImportJob,
    SourceImportProgress,
    SourceImportStatus,
    TomatoSearchCandidate,
    local_source_locator_hash,
    parse_source_import_create_request,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_receipt_models import (
    LocalSourceImportExecutionReceipt,
    LocalSourceImportRequestEvidence,
    SourceImportArtifactEvidence,
    SourceImportAttemptReceipt,
    SourceImportToolEvidence,
    parse_source_import_execution_receipt,
    source_import_attempt_receipt_hash,
    source_import_attempt_receipt_id,
    source_import_distillation_input_hash,
    source_import_distillation_output_hash,
    source_import_draft_fingerprint,
    source_import_execution_receipt_hash,
    source_import_idempotency_key,
    source_import_packet_fingerprint,
    source_import_prepare_input_hash,
    source_import_prepare_output_hash,
    source_import_receipt_id,
    source_import_request_hash,
    source_import_run_id,
)


SOURCE_PATH = "C:/fixtures/archive-night-sample.txt"
SOURCE_HASH = "3" * 64
IMPORT_ID = "source-import-" + "1" * 32
NOW = datetime(2026, 7, 22, 16, 0, tzinfo=UTC)


def _local_candidate() -> LocalFileSourceCandidate:
    return LocalFileSourceCandidate(
        source_path=SOURCE_PATH,
        title="archive-night-sample",
        author="unknown",
        source_size_bytes=2894,
        source_sha256=SOURCE_HASH,
        source_locator_hash=local_source_locator_hash(SOURCE_PATH),
    )


def _source_artifacts() -> tuple[SourceImportArtifactEvidence, ...]:
    return (
        SourceImportArtifactEvidence(
            kind="packet_manifest",
            relative_path="source-import-1/packet.manifest.json",
            size_bytes=900,
            sha256="1" * 64,
        ),
        SourceImportArtifactEvidence(
            kind="packet_metadata",
            relative_path="source-import-1/book.metadata.json",
            size_bytes=700,
            sha256="2" * 64,
        ),
        SourceImportArtifactEvidence(
            kind="packet_source",
            relative_path="source-import-1/book.txt",
            size_bytes=2894,
            sha256=SOURCE_HASH,
        ),
        SourceImportArtifactEvidence(
            kind="packet_text",
            relative_path="source-import-1/book.txt",
            size_bytes=2894,
            sha256=SOURCE_HASH,
        ),
    )


def _draft_artifacts() -> tuple[SourceImportArtifactEvidence, ...]:
    return (
        SourceImportArtifactEvidence(
            kind="cards_draft_json",
            relative_path="source-import-1/draft/cards.draft.json",
            size_bytes=1200,
            sha256="4" * 64,
        ),
        SourceImportArtifactEvidence(
            kind="distillation_report_md",
            relative_path="source-import-1/draft/distillation_report.md",
            size_bytes=500,
            sha256="5" * 64,
        ),
        SourceImportArtifactEvidence(
            kind="tool_result_envelope_json",
            relative_path="source-import-1/draft/tool_result_envelope.json",
            size_bytes=800,
            sha256="7" * 64,
        ),
        SourceImportArtifactEvidence(
            kind="validation_report_md",
            relative_path="source-import-1/draft/validation_report.md",
            size_bytes=600,
            sha256="6" * 64,
        ),
    )


def _local_receipt() -> LocalSourceImportExecutionReceipt:
    request = LocalSourceImportRequestEvidence(
        source_locator_hash=local_source_locator_hash(SOURCE_PATH),
        title="archive-night-sample",
        author="unknown",
        source_size_bytes=2894,
        source_sha256=SOURCE_HASH,
    )
    request_hash = source_import_request_hash(request)
    source_artifacts = _source_artifacts()
    draft_artifacts = _draft_artifacts()
    packet_fingerprint = source_import_packet_fingerprint(
        request_hash,
        "full",
        source_artifacts,
    )
    distillation_input_hash = source_import_distillation_input_hash(
        packet_fingerprint,
        "1" * 64,
        SOURCE_HASH,
        "2" * 64,
    )
    distillation_tool = SourceImportToolEvidence(
        tool_id="zhimeng.local-source-distillation",
        tool_version="1",
        tool_fingerprint="9" * 64,
    )
    draft_fingerprint = source_import_draft_fingerprint(
        distillation_input_hash,
        distillation_tool.tool_fingerprint,
        draft_artifacts,
    )
    values = {
        "receipt_id": source_import_receipt_id(IMPORT_ID),
        "source_import_id": IMPORT_ID,
        "run_id": source_import_run_id(IMPORT_ID),
        "revision": 1,
        "idempotency_key": source_import_idempotency_key(
            source_import_run_id(IMPORT_ID),
            1,
            request_hash,
        ),
        "lineage_status": "verified",
        "provider": "local_file",
        "request": request,
        "request_hash": request_hash,
        "retry_of": None,
        "prepare_invocation_id": "source-import-invocation-" + "1" * 32,
        "prepare_tool": SourceImportToolEvidence(
            tool_id="zhimeng.local-source-prepare",
            tool_version="1",
            tool_fingerprint="8" * 64,
        ),
        "prepare_input_hash": source_import_prepare_input_hash(
            "local_file",
            request_hash,
        ),
        "prepare_output_hash": source_import_prepare_output_hash(
            packet_fingerprint,
            "1" * 64,
            SOURCE_HASH,
            "2" * 64,
        ),
        "download_scope": "full",
        "source_format": "txt",
        "source_locator_hash": request.source_locator_hash,
        "source_file_size_bytes": 2894,
        "source_file_sha256": SOURCE_HASH,
        "source_packet_fingerprint": packet_fingerprint,
        "packet_manifest_hash": "1" * 64,
        "packet_source_hash": SOURCE_HASH,
        "packet_text_hash": SOURCE_HASH,
        "packet_metadata_hash": "2" * 64,
        "source_artifacts": source_artifacts,
        "distillation_invocation_id": "source-import-invocation-" + "2" * 32,
        "distillation_tool": distillation_tool,
        "distillation_input_hash": distillation_input_hash,
        "distillation_output_hash": source_import_distillation_output_hash(
            draft_fingerprint,
            "4" * 64,
            "7" * 64,
            3,
        ),
        "draft_fingerprint": draft_fingerprint,
        "cards_draft_hash": "4" * 64,
        "envelope_hash": "7" * 64,
        "envelope_schema": "novel_distillation.source_packet_draft.v1",
        "envelope_status": "draft_ready",
        "card_count": 3,
        "draft_artifacts": draft_artifacts,
        "created_at": NOW,
        "receipt_hash": "0" * 64,
    }
    provisional = LocalSourceImportExecutionReceipt.model_construct(**values)
    values["receipt_hash"] = source_import_execution_receipt_hash(provisional)
    return LocalSourceImportExecutionReceipt.model_validate(values)


def test_tomato_v1_defaults_and_json_shape_remain_compatible() -> None:
    candidate = TomatoSearchCandidate(
        index=1,
        book_id="12345",
        title="旧合同",
        author="作者",
    )
    request = SourceImportCreateRequest(query="旧合同", candidate=candidate)
    assert request.model_dump(mode="json") == {
        "provider": "tomato",
        "query": "旧合同",
        "candidate": {
            "index": 1,
            "book_id": "12345",
            "title": "旧合同",
            "author": "作者",
        },
    }
    job = SourceImportJob(
        import_id=IMPORT_ID,
        query=request.query,
        candidate=candidate,
        status=SourceImportStatus.QUEUED,
        progress=SourceImportProgress(stage="queued", message="waiting"),
        created_at=NOW,
        updated_at=NOW,
    )
    payload = job.model_dump(mode="json", by_alias=True)
    assert payload["schema"] == "zhimeng.source-import.v1"
    assert isinstance(parse_source_import_job(payload), SourceImportJob)
    assert isinstance(
        parse_source_import_create_request(request.model_dump(mode="json")),
        SourceImportCreateRequest,
    )


def test_local_file_candidate_packet_and_job_bind_exact_txt_bytes() -> None:
    candidate = _local_candidate()
    artifacts = [
        SourceArtifact(
            kind=item.kind,
            path=f"C:/runtime/{item.relative_path}",
            size_bytes=item.size_bytes,
            sha256=item.sha256,
        )
        for item in _source_artifacts()
    ]
    packet = PreparedLocalSourcePacket(
        title=candidate.title,
        author=candidate.author,
        packet_dir="C:/runtime/source-import-1",
        packet_manifest_path="C:/runtime/source-import-1/packet.manifest.json",
        packet_source_path="C:/runtime/source-import-1/book.txt",
        text_path="C:/runtime/source-import-1/book.txt",
        metadata_path="C:/runtime/source-import-1/book.metadata.json",
        char_count=986,
        chapter_marker_count=3,
        source_file_size_bytes=candidate.source_size_bytes,
        source_file_sha256=candidate.source_sha256,
        packet_source_sha256=candidate.source_sha256,
        text_sha256=candidate.source_sha256,
        artifacts=artifacts,
    )
    draft = DistillationDraft(
        output_dir="C:/runtime/source-import-1/draft",
        cards_path="C:/runtime/source-import-1/draft/cards.draft.json",
        report_path="C:/runtime/source-import-1/draft/distillation_report.md",
        validation_report_path="C:/runtime/source-import-1/draft/validation_report.md",
        envelope_path="C:/runtime/source-import-1/draft/tool_result_envelope.json",
        card_count=3,
        artifacts=[
            SourceArtifact(
                kind=item.kind,
                path=f"C:/runtime/{item.relative_path}",
                size_bytes=item.size_bytes,
                sha256=item.sha256,
            )
            for item in _draft_artifacts()
        ],
    )
    job = LocalSourceImportJob(
        import_id=IMPORT_ID,
        query=SOURCE_PATH,
        candidate=candidate,
        status=SourceImportStatus.AWAITING_REVIEW,
        progress=SourceImportProgress(stage="awaiting_review", message="review"),
        source_packet=packet,
        distillation_draft=draft,
        created_at=NOW,
        updated_at=NOW,
    )
    payload = job.model_dump(mode="json", by_alias=True)
    assert payload["schema"] == "zhimeng.source-import.v2"
    assert isinstance(parse_source_import_job(payload), LocalSourceImportJob)

    request = LocalSourceImportCreateRequest(query=SOURCE_PATH, candidate=candidate)
    assert isinstance(
        parse_source_import_create_request(request.model_dump(mode="json")),
        LocalSourceImportCreateRequest,
    )
    with pytest.raises(ValidationError, match="match the inspected source path"):
        LocalSourceImportCreateRequest(query="C:/other.txt", candidate=candidate)

    tampered_job = payload | {"query": "C:/other.txt"}
    with pytest.raises(ValidationError, match="match its candidate path"):
        LocalSourceImportJob.model_validate(tampered_job)

    tampered_packet = packet.model_dump(mode="json")
    tampered_packet["text_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="hashes differ"):
        PreparedLocalSourcePacket.model_validate(tampered_packet)


def test_local_execution_receipt_is_deterministic_and_rejects_tamper() -> None:
    receipt = _local_receipt()
    payload = receipt.model_dump(mode="json", by_alias=True)
    parsed = parse_source_import_execution_receipt(payload)
    assert isinstance(parsed, LocalSourceImportExecutionReceipt)
    assert parsed.receipt_hash == receipt.receipt_hash
    assert parsed.source_file_sha256 == parsed.packet_source_hash == parsed.packet_text_hash

    tampered = dict(payload)
    tampered["source_file_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="preserve the selected source bytes"):
        parse_source_import_execution_receipt(tampered)

    wrong_provider = dict(payload)
    wrong_provider["provider"] = "tomato"
    with pytest.raises(ValidationError):
        parse_source_import_execution_receipt(wrong_provider)

    wrong_hash = dict(payload)
    wrong_hash["receipt_hash"] = "e" * 64
    with pytest.raises(ValidationError, match="receipt hash does not match"):
        parse_source_import_execution_receipt(wrong_hash)


def _attempt(**updates: object) -> SourceImportAttemptReceipt:
    values: dict[str, object] = {
        "receipt_id": source_import_attempt_receipt_id(IMPORT_ID),
        "source_import_id": IMPORT_ID,
        "attempt_ordinal": 1,
        "status": "success",
        "stage": "awaiting_review",
        "provider": "local_file",
        "request_hash": "1" * 64,
        "execution_receipt_id": source_import_receipt_id(IMPORT_ID),
        "execution_receipt_hash": "2" * 64,
        "prepare_tool_fingerprint": "3" * 64,
        "distillation_tool_fingerprint": "4" * 64,
        "failure_signature": None,
        "cleanup_status": "not_required",
        "cleanup_hash": "5" * 64,
        "created_at": NOW,
        "receipt_hash": "0" * 64,
    }
    values.update(updates)
    provisional = SourceImportAttemptReceipt.model_construct(**values)
    values["receipt_hash"] = source_import_attempt_receipt_hash(provisional)
    return SourceImportAttemptReceipt.model_validate(values)


def test_attempt_receipt_separates_success_and_failure_evidence() -> None:
    success = _attempt()
    assert success.receipt_id == source_import_attempt_receipt_id(IMPORT_ID, 1)
    assert success.receipt_hash == source_import_attempt_receipt_hash(success)

    failure = _attempt(
        status="failed",
        stage="queued",
        request_hash=None,
        execution_receipt_id=None,
        execution_receipt_hash=None,
        prepare_tool_fingerprint=None,
        distillation_tool_fingerprint=None,
        failure_signature="6" * 64,
        cleanup_status="clean",
    )
    assert failure.failure_signature == "6" * 64

    with pytest.raises(ValidationError, match="cannot claim an execution receipt"):
        _attempt(status="failed", failure_signature="6" * 64)
    with pytest.raises(ValidationError, match="do not clean accepted artifacts"):
        _attempt(cleanup_status="clean")


def test_provider_specific_request_parser_rejects_cross_provider_candidate() -> None:
    with pytest.raises(ValidationError):
        parse_source_import_create_request(
            {
                "provider": "local_file",
                "query": SOURCE_PATH,
                "candidate": {
                    "index": 1,
                    "book_id": "12345",
                    "title": "wrong provider",
                    "author": "unknown",
                },
            }
        )
