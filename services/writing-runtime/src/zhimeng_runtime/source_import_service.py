from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.local_source_adapter import (
    LocalSourceAdapter,
    LocalSourceAdapterCancelled,
    LocalSourceAdapterError,
)
from zhimeng_runtime.source_import_models import (
    ACTIVE_SOURCE_IMPORT_STATUSES,
    AnySourceImportCreateRequest,
    AnySourceImportJob,
    DistillationDraft,
    LocalFileSourceCandidate,
    LocalSourceImportCreateRequest,
    LocalSourceImportJob,
    PreparedLocalSourcePacket,
    PreparedSourcePacket,
    SourceImportCreateRequest,
    SourceImportJob,
    SourceImportListResponse,
    SourceImportProgress,
    SourceImportStatus,
    SourceArtifact,
    TomatoResolveRequest,
    TomatoResolveResponse,
    TomatoSearchResponse,
    parse_source_import_job,
)
from zhimeng_runtime.source_import_receipt_models import (
    AnySourceImportExecutionReceipt,
    AnySourceImportRequestEvidence,
    LocalSourceImportExecutionReceipt,
    LocalSourceImportRequestEvidence,
    SourceImportAdapterExecutionIdentity,
    SourceImportArtifactEvidence,
    SourceImportAttemptReceipt,
    SourceImportExecutionReceipt,
    SourceImportExecutionReceiptRecord,
    SourceImportLineageStatus,
    SourceImportRequestEvidence,
    canonical_json_hash,
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
    source_import_request_from_job,
    source_import_run_id,
    validate_source_import_receipt_job,
)
from zhimeng_runtime.tomato_skill_adapter import (
    TomatoAdapterCancelled,
    TomatoAdapterError,
    TomatoSourceAdapter,
    extract_tomato_book_id,
    normalized_book_title,
)


class SourceImportError(RuntimeError):
    pass


class SourceImportNotFoundError(SourceImportError):
    pass


class SourceImportStateError(SourceImportError):
    pass


class SourceImportExecutionError(SourceImportError):
    pass


class SourceImportUnavailableError(SourceImportExecutionError):
    pass


class SourceImportIntegrityError(SourceImportError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


ALLOWED_SOURCE_IMPORT_TRANSITIONS: dict[
    SourceImportStatus,
    frozenset[SourceImportStatus],
] = {
    SourceImportStatus.QUEUED: frozenset(
        {
            SourceImportStatus.DOWNLOADING,
            SourceImportStatus.CANCEL_REQUESTED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.DOWNLOADING: frozenset(
        {
            SourceImportStatus.SOURCE_READY,
            SourceImportStatus.CANCEL_REQUESTED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.SOURCE_READY: frozenset(
        {
            SourceImportStatus.DRAFTING,
            SourceImportStatus.CANCEL_REQUESTED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.DRAFTING: frozenset(
        {
            SourceImportStatus.AWAITING_REVIEW,
            SourceImportStatus.CANCEL_REQUESTED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.AWAITING_REVIEW: frozenset(
        {
            SourceImportStatus.FORMALIZING,
            SourceImportStatus.FAILED,
        }
    ),
    SourceImportStatus.FORMALIZING: frozenset(
        {
            SourceImportStatus.COMPLETED,
            SourceImportStatus.CANCEL_REQUESTED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.FAILED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.CANCEL_REQUESTED: frozenset(
        {
            SourceImportStatus.CANCELLED,
            SourceImportStatus.INTERRUPTED,
        }
    ),
    SourceImportStatus.COMPLETED: frozenset(),
    SourceImportStatus.CANCELLED: frozenset(),
    SourceImportStatus.FAILED: frozenset(),
    SourceImportStatus.INTERRUPTED: frozenset(),
}


class SourceImportService:
    def __init__(
        self,
        store: SqliteStore,
        state_root: Path,
        adapter: TomatoSourceAdapter | None,
        *,
        local_adapter: LocalSourceAdapter | None = None,
    ) -> None:
        self.store = store
        self.state_root = state_root.resolve(strict=False)
        self.adapter = adapter
        self.local_adapter = local_adapter
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="zhimeng-source-import",
        )
        self._guard = threading.Lock()
        self._record_guard = threading.RLock()
        self._futures: dict[str, Future[None]] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._closed = False

    def search(self, query: str) -> TomatoSearchResponse:
        candidate_query = query.strip()
        if not candidate_query:
            raise SourceImportExecutionError("Novel name must not be empty")
        if self.adapter is None:
            raise SourceImportUnavailableError("Tomato source adapter is not configured")
        try:
            candidates = self.adapter.search(candidate_query)
        except TomatoAdapterError as exc:
            raise SourceImportExecutionError(str(exc)) from exc
        if not candidates:
            return TomatoSearchResponse(
                query=candidate_query,
                resolution="not_found",
                candidates=[],
            )
        normalized_query = normalized_book_title(candidate_query)
        exact = [
            candidate
            for candidate in candidates
            if normalized_book_title(candidate.title) == normalized_query
        ]
        selected = exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else None
        if selected is not None:
            return TomatoSearchResponse(
                query=candidate_query,
                resolution="auto_selected",
                candidates=candidates,
                selected_candidate=selected,
            )
        return TomatoSearchResponse(
            query=candidate_query,
            resolution="selection_required",
            candidates=candidates,
        )

    def resolve_tomato(self, request: TomatoResolveRequest) -> TomatoResolveResponse:
        if self.adapter is None:
            raise SourceImportUnavailableError("Tomato source adapter is not configured")
        try:
            book_id, locator_kind, source_locator = extract_tomato_book_id(
                book_id=request.book_id,
                url=request.url,
            )
            candidate = self.adapter.resolve(
                book_id=book_id if locator_kind == "book_id" else None,
                url=source_locator if locator_kind == "url" else None,
                title=request.title,
                author=request.author,
            )
        except TomatoAdapterError as exc:
            raise SourceImportExecutionError(str(exc)) from exc
        if candidate.book_id != book_id:
            raise SourceImportIntegrityError(
                "Tomato resolve returned a candidate with a different book_id"
            )
        return TomatoResolveResponse(
            locator_kind=locator_kind,
            source_locator=source_locator,
            candidate=candidate,
        )

    def inspect_local(self, source_path: str) -> LocalFileSourceCandidate:
        if self.local_adapter is None:
            raise SourceImportUnavailableError("Local source adapter is not configured")
        try:
            return self.local_adapter.inspect(source_path)
        except LocalSourceAdapterError as exc:
            raise SourceImportExecutionError(str(exc)) from exc

    def create(
        self,
        request: AnySourceImportCreateRequest,
        *,
        retry_of: str | None = None,
    ) -> AnySourceImportJob:
        with self._guard:
            if self._closed:
                raise SourceImportStateError("Source import service is shutting down")
        created_at = _now()
        if request.provider == "local_file":
            if self.local_adapter is None:
                raise SourceImportUnavailableError("Local source adapter is not configured")
            record: AnySourceImportJob = LocalSourceImportJob(
                import_id=f"source-import-{uuid4().hex}",
                query=request.query.strip(),
                candidate=request.candidate,
                status=SourceImportStatus.QUEUED,
                retry_of=retry_of,
                progress=SourceImportProgress(
                    stage="queued",
                    message="Waiting for the managed local source adapter",
                ),
                created_at=created_at,
                updated_at=created_at,
            )
        else:
            if self.adapter is None:
                raise SourceImportUnavailableError("Tomato source adapter is not configured")
            record = SourceImportJob(
                import_id=f"source-import-{uuid4().hex}",
                query=request.query.strip(),
                candidate=request.candidate,
                status=SourceImportStatus.QUEUED,
                retry_of=retry_of,
                progress=SourceImportProgress(
                    stage="queued",
                    message="Waiting for the local Tomato adapter",
                ),
                created_at=created_at,
                updated_at=created_at,
            )
        self.store.create_source_import_job(self._payload(record))
        cancel_event = threading.Event()
        with self._guard:
            self._cancel_events[record.import_id] = cancel_event
            future = self._executor.submit(self._run, record.import_id, cancel_event)
            self._futures[record.import_id] = future
        return record

    def get(self, import_id: str) -> AnySourceImportJob:
        try:
            payload = self.store.get_source_import_job(import_id)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "stored source import job failed integrity validation"
            ) from exc
        if payload is None:
            raise SourceImportNotFoundError("Source import was not found")
        try:
            return parse_source_import_job(payload)
        except (TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "stored source import job failed integrity validation"
            ) from exc

    def list(self) -> SourceImportListResponse:
        try:
            return SourceImportListResponse(
                imports=[
                    parse_source_import_job(payload)
                    for payload in self.store.list_source_import_jobs()
                ]
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "stored source import list failed integrity validation"
            ) from exc

    def get_execution_receipt(
        self,
        import_id: str,
    ) -> SourceImportExecutionReceiptRecord:
        job = self.get(import_id)
        try:
            payload = self.store.get_source_import_execution_receipt(import_id)
            if payload is None:
                return SourceImportExecutionReceiptRecord(
                    source_import_id=import_id,
                    lineage_status=SourceImportLineageStatus.LEGACY_UNVERIFIED,
                    receipt=None,
                )
            receipt = parse_source_import_execution_receipt(payload)
            validate_source_import_receipt_job(receipt, job)
            return SourceImportExecutionReceiptRecord(
                source_import_id=import_id,
                lineage_status=SourceImportLineageStatus.VERIFIED,
                receipt=receipt,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "Stored source import execution receipt is invalid"
            ) from exc

    def list_attempt_receipts(
        self,
        import_id: str,
    ) -> list[SourceImportAttemptReceipt]:
        self.get(import_id)
        try:
            return [
                SourceImportAttemptReceipt.model_validate(payload)
                for payload in self.store.list_source_import_attempt_receipts(
                    import_id
                )
            ]
        except (RuntimeError, TypeError, ValueError) as exc:
            raise SourceImportIntegrityError(
                "source import attempt receipts failed integrity validation"
            ) from exc

    def cancel(self, import_id: str) -> AnySourceImportJob:
        record = self.get(import_id)
        with self._guard:
            event = self._cancel_events.get(import_id)
            future = self._futures.get(import_id)
            if event is None:
                raise SourceImportStateError(
                    "source import is not owned by this Runtime process"
                )
            event.set()
            cancelled_before_start = (
                record.status is SourceImportStatus.QUEUED
                and future is not None
                and future.cancel()
            )
            if cancelled_before_start:
                self._cancel_events.pop(import_id, None)
                self._futures.pop(import_id, None)
        with self._record_guard:
            record = self.get(import_id)
            if record.status is SourceImportStatus.CANCELLED:
                return record
            if record.status not in ACTIVE_SOURCE_IMPORT_STATUSES:
                raise SourceImportStateError(
                    f"source import cannot be cancelled in state {record.status.value}"
                )
            if cancelled_before_start:
                cleanup_status, cleanup_hash = self._cleanup_unsuccessful(record)
                attempt = self._build_attempt_receipt(
                    record=record,
                    status="cancelled",
                    stage="queued",
                    request_hash=None,
                    identity=None,
                    execution_receipt=None,
                    failure_message="Import cancelled before it started",
                    cleanup_status=cleanup_status,
                    cleanup_hash=cleanup_hash,
                    failure_type="cancelled_before_start",
                )
                return self._transition(
                    record,
                    status=SourceImportStatus.CANCELLED,
                    progress=SourceImportProgress(
                        stage="cancelled",
                        message="Import cancelled before it started",
                    ),
                    error="Import cancelled before it started",
                    attempt_receipt=attempt,
                )
            return self._transition(
                record,
                status=SourceImportStatus.CANCEL_REQUESTED,
                progress=SourceImportProgress(
                    stage="cancel_requested",
                    message="Stopping the downloader and current Skill step",
                    saved_chapters=record.progress.saved_chapters,
                    total_chapters=record.progress.total_chapters,
                    percent=record.progress.percent,
                ),
            )

    def retry(self, import_id: str) -> AnySourceImportJob:
        record = self.get(import_id)
        if record.status not in {
            SourceImportStatus.FAILED,
            SourceImportStatus.CANCELLED,
            SourceImportStatus.INTERRUPTED,
        }:
            raise SourceImportStateError(
                f"source import cannot be retried in state {record.status.value}"
            )
        request: AnySourceImportCreateRequest
        if record.provider == "local_file":
            request = LocalSourceImportCreateRequest(
                query=record.query,
                candidate=record.candidate,
            )
        else:
            request = SourceImportCreateRequest(
                query=record.query,
                candidate=record.candidate,
            )
        return self.create(request, retry_of=record.import_id)

    def recover_interrupted(self) -> None:
        self.store.recover_interrupted_source_imports()


    def build_network_recovery_evidence(self, import_id: str) -> dict:
        """Build recovery evidence after Tomato import retry following network failure.

        Does not claim live external download acceptance. Records failed predecessor,
        retry link, and recovered status for Local v1 residual network-recovery evidence.
        """
        current = self.get(import_id)
        if current.retry_of is None:
            raise SourceImportStateError(
                "network recovery evidence requires an import that is a retry of a prior job"
            )
        prior = self.get(current.retry_of)
        prior_status = getattr(prior.status, "value", str(prior.status))
        current_status = getattr(current.status, "value", str(current.status))
        prior_error = str(prior.error or "")
        networkish = any(
            token in prior_error.lower()
            for token in ("network", "timeout", "timed out", "connection", "dns", "socket")
        )
        prior_failed = prior_status in {"failed", "cancelled", "interrupted"}
        current_ok = current_status in {"awaiting_review", "completed", "source_ready"}
        recovered = bool(prior_failed and networkish and current_ok)
        material = {
            "schema": "zhimeng.source-import-network-recovery-evidence.v1",
            "import_id": current.import_id,
            "retry_of": current.retry_of,
            "prior_status": prior_status,
            "prior_error": prior_error or None,
            "current_status": current_status,
            "network_failure_detected": networkish,
            "recovered": recovered,
            "live_external_download_claimed": False,
            "formal_draft_allowed": False,
            "policy_version": "local-v1-source-network-recovery-1",
        }
        evidence_id = "source-network-recovery-" + canonical_json_hash(material)[:32]
        material["evidence_id"] = evidence_id
        material["evidence_hash"] = canonical_json_hash(material)
        root = self.state_root / current.import_id
        root.mkdir(parents=True, exist_ok=True)
        path = root / "network-recovery-evidence.json"
        path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "schema": "zhimeng.source-import-network-recovery-evidence-response.v1",
            "evidence": material,
            "not_claiming": [
                "live_external_download_acceptance",
                "tomato_production_network_matrix",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }


    def close(self) -> None:
        with self._guard:
            self._closed = True
            for event in self._cancel_events.values():
                event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def reopen(self) -> None:
        """Resume submissions after close has drained the old database's jobs."""
        with self._guard:
            if not self._closed:
                return
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="zhimeng-source-import")
            self._futures.clear()
            self._cancel_events.clear()
            self._closed = False

    def _run(self, import_id: str, cancel_event: threading.Event) -> None:
        request_hash: str | None = None
        initial_identity: SourceImportAdapterExecutionIdentity | None = None
        try:
            if cancel_event.is_set():
                raise LocalSourceAdapterCancelled("Import cancelled before it started")
            record = self.get(import_id)
            adapter = self._adapter_for(record)
            request_evidence = source_import_request_from_job(record)
            request_hash = source_import_request_hash(request_evidence)
            prepare_invocation_id = f"source-import-invocation-{uuid4().hex}"
            initial_identity = self._execution_identity(adapter)
            local_file = record.provider == "local_file"
            record = self._transition(
                record,
                status=SourceImportStatus.DOWNLOADING,
                progress=SourceImportProgress(
                    stage="preparing" if local_file else "downloading",
                    message=(
                        f"Preparing local source {record.candidate.title}"
                        if local_file
                        else f"Downloading {record.candidate.title}"
                    ),
                ),
                error=None,
            )
            workspace = self._workspace(import_id)
            source = adapter.prepare(
                record.candidate,
                workspace,
                cancel_event,
                lambda progress: self._store_progress(import_id, cancel_event, progress),
            )
            after_prepare_identity = self._execution_identity(adapter)
            if after_prepare_identity != initial_identity:
                raise SourceImportExecutionError(
                    "source import tool identity changed during source preparation"
                )
            external_kinds = (
                frozenset()
                if local_file
                else frozenset({"downloaded_epub"})
            )
            source_artifacts = self._verified_artifacts(
                source.artifacts,
                workspace=workspace,
                allow_external_kinds=external_kinds,
            )
            if cancel_event.is_set():
                raise LocalSourceAdapterCancelled("Import cancelled after source preparation")
            record = self._transition(
                self.get(import_id),
                status=SourceImportStatus.SOURCE_READY,
                source_packet=source,
                progress=SourceImportProgress(
                    stage="source_ready",
                    message=(
                        "Local TXT bytes and verified source packet are ready"
                        if local_file
                        else "Full EPUB and verified source packet are ready"
                    ),
                    percent=100,
                ),
            )
            record = self._transition(
                record,
                status=SourceImportStatus.DRAFTING,
                progress=SourceImportProgress(
                    stage="drafting",
                    message="Building an auditable distillation draft",
                    percent=100,
                ),
            )
            distillation_invocation_id = f"source-import-invocation-{uuid4().hex}"
            draft = adapter.draft(source, workspace, cancel_event)
            final_identity = self._execution_identity(adapter)
            if final_identity != initial_identity:
                raise SourceImportExecutionError(
                    "source import tool identity changed during distillation"
                )
            if cancel_event.is_set():
                raise LocalSourceAdapterCancelled("Import cancelled after draft generation")
            source_artifacts_after = self._verified_artifacts(
                source.artifacts,
                workspace=workspace,
                allow_external_kinds=external_kinds,
            )
            if source_artifacts_after != source_artifacts:
                raise SourceImportExecutionError(
                    "source packet changed while distillation was running"
                )
            draft_artifacts = self._verified_artifacts(
                draft.artifacts,
                workspace=workspace,
            )
            envelope, observed_card_count = self._validated_distillation_envelope(
                source,
                draft,
            )
            draft_artifacts_after = self._verified_artifacts(
                draft.artifacts,
                workspace=workspace,
            )
            if draft_artifacts_after != draft_artifacts:
                raise SourceImportExecutionError(
                    "distillation artifacts changed while the receipt was assembled"
                )
            receipt = self._build_execution_receipt(
                record=self.get(import_id),
                request=request_evidence,
                request_hash=request_hash,
                prepare_invocation_id=prepare_invocation_id,
                identity=initial_identity,
                source=source,
                source_artifacts=source_artifacts,
                distillation_invocation_id=distillation_invocation_id,
                draft_artifacts=draft_artifacts,
                envelope=envelope,
                card_count=observed_card_count,
            )
            attempt_receipt = self._build_attempt_receipt(
                record=self.get(import_id),
                status="success",
                stage="awaiting_review",
                request_hash=request_hash,
                identity=initial_identity,
                execution_receipt=receipt,
                failure_message=None,
                cleanup_status="not_required",
                cleanup_hash=self._not_required_cleanup_hash(import_id),
            )
            self._transition(
                self.get(import_id),
                status=SourceImportStatus.AWAITING_REVIEW,
                source_packet=source,
                distillation_draft=draft,
                progress=SourceImportProgress(
                    stage="awaiting_review",
                    message="Draft cards are ready for Agent or author review",
                    percent=100,
                ),
                error=None,
                execution_receipt=receipt,
                attempt_receipt=attempt_receipt,
            )
        except (TomatoAdapterCancelled, LocalSourceAdapterCancelled) as exc:
            self._finish_cancelled(
                import_id,
                str(exc),
                request_hash=request_hash,
                identity=initial_identity,
            )
        except Exception as exc:
            if cancel_event.is_set():
                self._finish_cancelled(
                    import_id,
                    str(exc) or "Import cancelled",
                    request_hash=request_hash,
                    identity=initial_identity,
                )
                return
            message = str(exc) or exc.__class__.__name__
            self._finish_failed(
                import_id,
                message,
                request_hash=request_hash,
                identity=initial_identity,
                failure_type=exc.__class__.__name__,
            )
        finally:
            with self._guard:
                self._cancel_events.pop(import_id, None)
                self._futures.pop(import_id, None)

    def _finish_cancelled(
        self,
        import_id: str,
        message: str,
        *,
        request_hash: str | None,
        identity: SourceImportAdapterExecutionIdentity | None,
    ) -> None:
        try:
            record = self.get(import_id)
            failure_stage = record.progress.stage
            cleanup_status, cleanup_hash = self._cleanup_unsuccessful(record)
            attempt = self._build_attempt_receipt(
                record=record,
                status="cancelled",
                stage=failure_stage if request_hash is not None else "queued",
                request_hash=request_hash,
                identity=identity,
                execution_receipt=None,
                failure_message=message,
                cleanup_status=cleanup_status,
                cleanup_hash=cleanup_hash,
                failure_type="cancelled",
            )
            self._transition(
                record,
                status=SourceImportStatus.CANCELLED,
                progress=SourceImportProgress(
                    stage="cancelled",
                    message="Source import was cancelled",
                    saved_chapters=record.progress.saved_chapters,
                    total_chapters=record.progress.total_chapters,
                    percent=record.progress.percent,
                ),
                error=message,
                attempt_receipt=attempt,
            )
        except Exception:
            pass

    def _finish_failed(
        self,
        import_id: str,
        message: str,
        *,
        request_hash: str | None,
        identity: SourceImportAdapterExecutionIdentity | None,
        failure_type: str,
    ) -> None:
        try:
            record = self.get(import_id)
            failure_stage = record.progress.stage
            cleanup_status, cleanup_hash = self._cleanup_unsuccessful(record)
            attempt = self._build_attempt_receipt(
                record=record,
                status="failed",
                stage=failure_stage if request_hash is not None else "queued",
                request_hash=request_hash,
                identity=identity,
                execution_receipt=None,
                failure_message=message,
                cleanup_status=cleanup_status,
                cleanup_hash=cleanup_hash,
                failure_type=failure_type,
            )
            self._transition(
                record,
                status=SourceImportStatus.FAILED,
                progress=SourceImportProgress(
                    stage="failed",
                    message="Source import failed",
                ),
                error=message,
                attempt_receipt=attempt,
            )
        except Exception:
            pass

    def _cleanup_unsuccessful(
        self,
        record: AnySourceImportJob,
    ) -> tuple[str, str]:
        if record.provider != "local_file" or self.local_adapter is None:
            return "not_required", self._not_required_cleanup_hash(record.import_id)
        try:
            return self.local_adapter.cleanup_failed_workspace(
                self._workspace(record.import_id)
            )
        except Exception as exc:
            return (
                "partial",
                canonical_json_hash(
                    {
                        "schema": "zhimeng.source-import-cleanup.v1",
                        "source_import_id": record.import_id,
                        "status": "partial",
                        "failure_type": exc.__class__.__name__,
                    }
                ),
            )

    @staticmethod
    def _not_required_cleanup_hash(import_id: str) -> str:
        return canonical_json_hash(
            {
                "schema": "zhimeng.source-import-cleanup.v1",
                "source_import_id": import_id,
                "status": "not_required",
            }
        )

    def _build_attempt_receipt(
        self,
        *,
        record: AnySourceImportJob,
        status: str,
        stage: str,
        request_hash: str | None,
        identity: SourceImportAdapterExecutionIdentity | None,
        execution_receipt: AnySourceImportExecutionReceipt | None,
        failure_message: str | None,
        cleanup_status: str,
        cleanup_hash: str,
        failure_type: str | None = None,
    ) -> SourceImportAttemptReceipt:
        failure_signature = None
        if status != "success":
            failure_signature = canonical_json_hash(
                {
                    "schema": "zhimeng.source-import-failure-signature.v1",
                    "source_import_id": record.import_id,
                    "provider": record.provider,
                    "stage": record.progress.stage,
                    "failure_type": failure_type or status,
                    "message": failure_message or status,
                }
            )
        values: dict[str, object] = {
            "receipt_id": source_import_attempt_receipt_id(record.import_id),
            "source_import_id": record.import_id,
            "attempt_ordinal": 1,
            "status": status,
            "stage": stage,
            "provider": record.provider,
            "request_hash": request_hash,
            "execution_receipt_id": (
                execution_receipt.receipt_id if execution_receipt is not None else None
            ),
            "execution_receipt_hash": (
                execution_receipt.receipt_hash if execution_receipt is not None else None
            ),
            "prepare_tool_fingerprint": (
                identity.prepare_tool.tool_fingerprint if identity is not None else None
            ),
            "distillation_tool_fingerprint": (
                identity.distillation_tool.tool_fingerprint
                if identity is not None
                else None
            ),
            "failure_signature": failure_signature,
            "cleanup_status": cleanup_status,
            "cleanup_hash": cleanup_hash,
            "created_at": _now(),
            "receipt_hash": "0" * 64,
        }
        provisional = SourceImportAttemptReceipt.model_construct(**values)
        values["receipt_hash"] = source_import_attempt_receipt_hash(provisional)
        return SourceImportAttemptReceipt.model_validate(values)

    def _store_progress(
        self,
        import_id: str,
        cancel_event: threading.Event,
        raw: dict[str, object],
    ) -> None:
        with self._guard:
            if cancel_event.is_set():
                return
            record = self.get(import_id)
            if record.status is not SourceImportStatus.DOWNLOADING:
                return
            progress = SourceImportProgress.model_validate(raw)
            self._transition(record, progress=progress)

    def _adapter_for(
        self,
        record: AnySourceImportJob,
    ) -> TomatoSourceAdapter | LocalSourceAdapter:
        if record.provider == "local_file":
            if self.local_adapter is None:
                raise SourceImportUnavailableError("Local source adapter is not configured")
            return self.local_adapter
        if self.adapter is None:
            raise SourceImportUnavailableError("Tomato source adapter is not configured")
        return self.adapter

    def _execution_identity(
        self,
        adapter: TomatoSourceAdapter | LocalSourceAdapter,
    ) -> SourceImportAdapterExecutionIdentity:
        try:
            return SourceImportAdapterExecutionIdentity.model_validate(
                adapter.execution_identity()
            )
        except Exception as exc:
            raise SourceImportExecutionError(
                "source import adapter did not provide valid execution identity"
            ) from exc

    def _verified_artifacts(
        self,
        artifacts: list[SourceArtifact],
        *,
        workspace: Path,
        allow_external_kinds: frozenset[str] = frozenset(),
    ) -> tuple[SourceImportArtifactEvidence, ...]:
        workspace = workspace.resolve(strict=True)
        evidence: list[SourceImportArtifactEvidence] = []
        seen_kinds: set[str] = set()
        for artifact in artifacts:
            if artifact.kind in seen_kinds:
                raise SourceImportExecutionError(
                    f"source import artifact kind is duplicated: {artifact.kind}"
                )
            seen_kinds.add(artifact.kind)
            raw_path = Path(artifact.path)
            if self._is_link_or_junction(raw_path):
                raise SourceImportExecutionError(
                    f"source import artifact is a link: {artifact.kind}"
                )
            try:
                path = raw_path.resolve(strict=True)
            except OSError as exc:
                raise SourceImportExecutionError(
                    f"source import artifact is missing: {artifact.kind}"
                ) from exc
            if not path.is_file() or self._is_link_or_junction(path):
                raise SourceImportExecutionError(
                    f"source import artifact is not a regular file: {artifact.kind}"
                )
            if artifact.kind not in allow_external_kinds:
                try:
                    path.relative_to(workspace)
                except ValueError as exc:
                    raise SourceImportExecutionError(
                        f"source import artifact escaped its workspace: {artifact.kind}"
                    ) from exc
            size_bytes = path.stat().st_size
            sha256 = self._sha256_file(path)
            if artifact.size_bytes != size_bytes or artifact.sha256 != sha256:
                raise SourceImportExecutionError(
                    f"source import artifact changed after adapter validation: {artifact.kind}"
                )
            evidence.append(
                SourceImportArtifactEvidence(
                    kind=artifact.kind,
                    relative_path=self._logical_artifact_path(
                        path,
                        kind=artifact.kind,
                        sha256=sha256,
                    ),
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
            )
        return tuple(sorted(evidence, key=lambda item: (item.kind, item.relative_path)))

    def _logical_artifact_path(self, path: Path, *, kind: str, sha256: str) -> str:
        try:
            relative = path.relative_to(self.state_root)
        except ValueError:
            return f"external/{kind}/{sha256}"
        return relative.as_posix()

    def _validated_distillation_envelope(
        self,
        source: PreparedSourcePacket | PreparedLocalSourcePacket,
        draft: DistillationDraft,
    ) -> tuple[dict[str, object], int]:
        try:
            envelope_path = Path(draft.envelope_path).resolve(strict=True)
            envelope_raw = envelope_path.read_text(encoding="utf-8")
            envelope = json.loads(envelope_raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceImportExecutionError(
                "distillation result envelope is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(envelope, dict):
            raise SourceImportExecutionError("distillation result envelope is not an object")
        if (
            envelope.get("tool_result_envelope")
            != "novel_distillation.source_packet_draft.v1"
            or envelope.get("status") != "draft_ready"
        ):
            raise SourceImportExecutionError(
                "distillation result envelope did not report the required draft contract"
            )

        inputs = envelope.get("input_artifacts")
        outputs = envelope.get("output_artifacts")
        checkpoints = envelope.get("checkpoints")
        success_criteria = envelope.get("success_criteria")
        if not all(
            isinstance(value, dict)
            for value in (inputs, outputs, checkpoints, success_criteria)
        ):
            raise SourceImportExecutionError(
                "distillation result envelope is missing structured evidence"
            )
        assert isinstance(inputs, dict)
        assert isinstance(outputs, dict)
        assert isinstance(checkpoints, dict)
        assert isinstance(success_criteria, dict)

        expected_inputs = {
            "packet_manifest": Path(source.packet_manifest_path).resolve(strict=True),
            "packet_text": Path(source.text_path).resolve(strict=True),
            "packet_metadata": Path(source.metadata_path).resolve(strict=True),
        }
        for field, expected_path in expected_inputs.items():
            try:
                observed_path = Path(str(inputs.get(field) or "")).resolve(strict=True)
            except OSError as exc:
                raise SourceImportExecutionError(
                    f"distillation envelope input is missing: {field}"
                ) from exc
            if observed_path != expected_path:
                raise SourceImportExecutionError(
                    f"distillation envelope input does not match source packet: {field}"
                )
        if inputs.get("packet_text_sha256") != self._sha256_file(expected_inputs["packet_text"]):
            raise SourceImportExecutionError(
                "distillation envelope text hash does not match the source packet"
            )

        artifact_paths: dict[str, Path] = {}
        for artifact in draft.artifacts:
            if artifact.kind in artifact_paths:
                raise SourceImportExecutionError(
                    f"distillation artifact kind is duplicated: {artifact.kind}"
                )
            artifact_paths[artifact.kind] = Path(artifact.path).resolve(strict=True)
        if set(outputs) != set(artifact_paths):
            raise SourceImportExecutionError(
                "distillation envelope outputs do not match recorded draft artifacts"
            )
        for kind, expected_path in artifact_paths.items():
            try:
                observed_path = Path(str(outputs.get(kind) or "")).resolve(strict=True)
            except OSError as exc:
                raise SourceImportExecutionError(
                    f"distillation envelope output is missing: {kind}"
                ) from exc
            if observed_path != expected_path:
                raise SourceImportExecutionError(
                    f"distillation envelope output path does not match: {kind}"
                )
        required_output_paths = {
            "cards_draft_json": Path(draft.cards_path).resolve(strict=True),
            "distillation_report_md": Path(draft.report_path).resolve(strict=True),
            "validation_report_md": Path(draft.validation_report_path).resolve(strict=True),
            "tool_result_envelope_json": envelope_path,
        }
        for kind, expected_path in required_output_paths.items():
            if artifact_paths.get(kind) != expected_path:
                raise SourceImportExecutionError(
                    f"distillation draft field does not match its artifact: {kind}"
                )

        required_checkpoints = (
            "packet_manifest_resolved",
            "packet_text_present",
            "draft_cards_written",
            "artifact_contract_complete",
            "cards_are_draft_candidates",
            "requires_human_distillation",
            "no_long_source_prose_stored",
        )
        if any(checkpoints.get(field) is not True for field in required_checkpoints):
            raise SourceImportExecutionError(
                "distillation result envelope did not pass required checkpoints"
            )
        if success_criteria.get("ready_for_formal_ingest") is not False:
            raise SourceImportExecutionError(
                "distillation draft must not claim formal-ingest readiness"
            )

        try:
            cards_payload = json.loads(
                Path(draft.cards_path).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceImportExecutionError(
                "distillation cards artifact is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(cards_payload, list) or not 1 <= len(cards_payload) <= 64:
            raise SourceImportExecutionError(
                "distillation cards artifact must contain 1-64 cards"
            )
        if any(
            not isinstance(card, dict)
            or card.get("draft_candidate") is not True
            or card.get("requires_human_distillation") is not True
            for card in cards_payload
        ):
            raise SourceImportExecutionError(
                "distillation cards must remain explicit human-review candidates"
            )
        observed_card_count = len(cards_payload)
        if (
            draft.card_count != observed_card_count
            or checkpoints.get("card_count") != observed_card_count
        ):
            raise SourceImportExecutionError(
                "distillation card count does not match its receipt evidence"
            )
        return envelope, observed_card_count

    def _build_execution_receipt(
        self,
        *,
        record: AnySourceImportJob,
        request: AnySourceImportRequestEvidence,
        request_hash: str,
        prepare_invocation_id: str,
        identity: SourceImportAdapterExecutionIdentity,
        source: PreparedSourcePacket | PreparedLocalSourcePacket,
        source_artifacts: tuple[SourceImportArtifactEvidence, ...],
        distillation_invocation_id: str,
        draft_artifacts: tuple[SourceImportArtifactEvidence, ...],
        envelope: dict[str, object],
        card_count: int,
    ) -> AnySourceImportExecutionReceipt:
        if request != source_import_request_from_job(record):
            raise SourceImportExecutionError(
                "source import job request changed while execution was running"
            )
        self._validate_source_packet_bindings(source)
        source_by_kind = {item.kind: item for item in source_artifacts}
        draft_by_kind = {item.kind: item for item in draft_artifacts}
        required_source = {
            "packet_manifest",
            "packet_text",
            "packet_metadata",
        }
        if record.provider == "local_file":
            required_source.add("packet_source")
        else:
            required_source.update({"packet_epub", "downloaded_epub"})
        if not required_source.issubset(source_by_kind):
            raise SourceImportExecutionError(
                "source packet is missing required receipt artifacts"
            )
        if not {
            "cards_draft_json",
            "distillation_report_md",
            "validation_report_md",
            "tool_result_envelope_json",
        }.issubset(draft_by_kind):
            raise SourceImportExecutionError(
                "distillation draft is missing required receipt artifacts"
            )
        source_packet_fingerprint = source_import_packet_fingerprint(
            request_hash,
            "full",
            source_artifacts,
        )
        packet_manifest_hash = source_by_kind["packet_manifest"].sha256
        packet_text_hash = source_by_kind["packet_text"].sha256
        packet_metadata_hash = source_by_kind["packet_metadata"].sha256
        prepare_output_hash = source_import_prepare_output_hash(
            source_packet_fingerprint,
            packet_manifest_hash,
            packet_text_hash,
            packet_metadata_hash,
        )
        distillation_input_hash = source_import_distillation_input_hash(
            source_packet_fingerprint,
            packet_manifest_hash,
            packet_text_hash,
            packet_metadata_hash,
        )
        draft_fingerprint = source_import_draft_fingerprint(
            distillation_input_hash,
            identity.distillation_tool.tool_fingerprint,
            draft_artifacts,
        )
        cards_draft_hash = draft_by_kind["cards_draft_json"].sha256
        envelope_hash = draft_by_kind["tool_result_envelope_json"].sha256
        distillation_output_hash = source_import_distillation_output_hash(
            draft_fingerprint,
            cards_draft_hash,
            envelope_hash,
            card_count,
        )
        run_id = source_import_run_id(record.import_id)
        revision = 1
        values: dict[str, object] = {
            "receipt_id": source_import_receipt_id(record.import_id),
            "source_import_id": record.import_id,
            "run_id": run_id,
            "revision": revision,
            "idempotency_key": source_import_idempotency_key(
                run_id,
                revision,
                request_hash,
            ),
            "lineage_status": "verified",
            "provider": record.provider,
            "request": request,
            "request_hash": request_hash,
            "retry_of": record.retry_of,
            "prepare_invocation_id": prepare_invocation_id,
            "prepare_tool": identity.prepare_tool,
            "prepare_input_hash": source_import_prepare_input_hash(
                record.provider,
                request_hash,
            ),
            "prepare_output_hash": prepare_output_hash,
            "download_scope": "full",
            "source_packet_fingerprint": source_packet_fingerprint,
            "packet_manifest_hash": packet_manifest_hash,
            "packet_text_hash": packet_text_hash,
            "packet_metadata_hash": packet_metadata_hash,
            "source_artifacts": source_artifacts,
            "distillation_invocation_id": distillation_invocation_id,
            "distillation_tool": identity.distillation_tool,
            "distillation_input_hash": distillation_input_hash,
            "distillation_output_hash": distillation_output_hash,
            "draft_fingerprint": draft_fingerprint,
            "cards_draft_hash": cards_draft_hash,
            "envelope_hash": envelope_hash,
            "envelope_schema": envelope["tool_result_envelope"],
            "envelope_status": envelope["status"],
            "card_count": card_count,
            "draft_artifacts": draft_artifacts,
            "created_at": _now(),
            "receipt_hash": "0" * 64,
        }
        receipt_model: type[SourceImportExecutionReceipt] | type[
            LocalSourceImportExecutionReceipt
        ]
        if isinstance(record, LocalSourceImportJob) and isinstance(
            source,
            PreparedLocalSourcePacket,
        ) and isinstance(request, LocalSourceImportRequestEvidence):
            values.update(
                {
                    "source_format": source.source_format,
                    "source_locator_hash": request.source_locator_hash,
                    "source_file_size_bytes": source.source_file_size_bytes,
                    "source_file_sha256": source.source_file_sha256,
                    "packet_source_hash": source_by_kind["packet_source"].sha256,
                    "packet_source_size_bytes": source_by_kind[
                        "packet_source"
                    ].size_bytes,
                    "packet_text_size_bytes": source_by_kind["packet_text"].size_bytes,
                }
            )
            receipt_model = LocalSourceImportExecutionReceipt
        elif (
            isinstance(record, SourceImportJob)
            and isinstance(source, PreparedSourcePacket)
            and isinstance(request, SourceImportRequestEvidence)
        ):
            receipt_model = SourceImportExecutionReceipt
        else:
            raise SourceImportExecutionError(
                "source import provider-specific receipt material does not match"
            )
        provisional = receipt_model.model_construct(**values)
        values["receipt_hash"] = source_import_execution_receipt_hash(provisional)
        return receipt_model.model_validate(values)

    def _validate_source_packet_bindings(
        self,
        source: PreparedSourcePacket | PreparedLocalSourcePacket,
    ) -> None:
        if source.download_scope != "full":
            raise SourceImportExecutionError(
                "source packet download_scope must be full"
            )
        artifacts_by_kind = {artifact.kind: artifact for artifact in source.artifacts}
        field_bindings = {
            "packet_manifest_path": "packet_manifest",
            "text_path": "packet_text",
            "metadata_path": "packet_metadata",
        }
        if isinstance(source, PreparedLocalSourcePacket):
            field_bindings["packet_source_path"] = "packet_source"
        else:
            field_bindings["epub_path"] = "packet_epub"
            field_bindings["downloaded_epub_path"] = "downloaded_epub"
        for field_name, artifact_kind in field_bindings.items():
            artifact = artifacts_by_kind.get(artifact_kind)
            if artifact is None:
                raise SourceImportExecutionError(
                    f"source packet field has no matching artifact: {field_name}"
                )
            raw_field_path = Path(getattr(source, field_name))
            if self._is_link_or_junction(raw_field_path):
                raise SourceImportExecutionError(
                    f"source packet field is a link: {field_name}"
                )
            try:
                field_path = raw_field_path.resolve(strict=True)
                artifact_path = Path(artifact.path).resolve(strict=True)
            except OSError as exc:
                raise SourceImportExecutionError(
                    f"source packet field or artifact is missing: {field_name}"
                ) from exc
            if field_path != artifact_path:
                raise SourceImportExecutionError(
                    f"source packet field does not match its artifact: {field_name}"
                )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _is_link_or_junction(path: Path) -> bool:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())

    def _workspace(self, import_id: str) -> Path:
        workspace = (self.state_root / import_id).resolve(strict=False)
        try:
            workspace.relative_to(self.state_root)
        except ValueError as exc:
            raise SourceImportExecutionError("Source import workspace escaped Runtime state") from exc
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _transition(
        self,
        record: AnySourceImportJob,
        **updates: object,
    ) -> AnySourceImportJob:
        with self._record_guard:
            current = self.get(record.import_id)
            raw_receipt = updates.pop("execution_receipt", None)
            raw_attempt_receipt = updates.pop("attempt_receipt", None)
            if isinstance(
                raw_receipt,
                (SourceImportExecutionReceipt, LocalSourceImportExecutionReceipt),
            ):
                execution_receipt: AnySourceImportExecutionReceipt | None = raw_receipt
            elif isinstance(raw_receipt, dict):
                execution_receipt = parse_source_import_execution_receipt(raw_receipt)
            elif raw_receipt is None:
                execution_receipt = None
            else:
                raise SourceImportStateError("source import execution receipt is invalid")
            if isinstance(raw_attempt_receipt, SourceImportAttemptReceipt):
                attempt_receipt: SourceImportAttemptReceipt | None = raw_attempt_receipt
            elif isinstance(raw_attempt_receipt, dict):
                try:
                    attempt_receipt = SourceImportAttemptReceipt.model_validate(
                        raw_attempt_receipt
                    )
                except (TypeError, ValueError) as exc:
                    raise SourceImportStateError(
                        "source import attempt receipt is invalid"
                    ) from exc
            elif raw_attempt_receipt is None:
                attempt_receipt = None
            else:
                raise SourceImportStateError("source import attempt receipt is invalid")
            raw_status = updates.get("status", current.status)
            try:
                next_status = (
                    raw_status
                    if isinstance(raw_status, SourceImportStatus)
                    else SourceImportStatus(str(raw_status))
                )
            except ValueError as exc:
                raise SourceImportStateError(
                    f"unknown source import state: {raw_status}"
                ) from exc
            if (
                next_status is not current.status
                and next_status not in ALLOWED_SOURCE_IMPORT_TRANSITIONS[current.status]
            ):
                raise SourceImportStateError(
                    "source import state changed before commit: "
                    f"{current.status.value} cannot transition to {next_status.value}"
                )
            next_record = current.model_copy(
                update={**updates, "status": next_status, "updated_at": _now()}
            )
            next_record = parse_source_import_job(
                next_record.model_dump(mode="json", by_alias=True)
            )
            if execution_receipt is not None:
                if next_status is not SourceImportStatus.AWAITING_REVIEW:
                    raise SourceImportStateError(
                        "source import execution receipts can commit only at awaiting_review"
                    )
                if execution_receipt.source_import_id != next_record.import_id:
                    raise SourceImportStateError(
                        "source import execution receipt does not match its job"
                    )
                try:
                    validate_source_import_receipt_job(execution_receipt, next_record)
                except ValueError as exc:
                    raise SourceImportStateError(str(exc)) from exc
                try:
                    self.store.save_source_import_job_with_execution_receipt(
                        self._payload(next_record),
                        execution_receipt.model_dump(mode="json", by_alias=True),
                        (
                            attempt_receipt.model_dump(mode="json", by_alias=True)
                            if attempt_receipt is not None
                            else None
                        ),
                    )
                except RuntimeError as exc:
                    raise SourceImportStateError(str(exc)) from exc
            elif attempt_receipt is not None:
                if next_status not in {
                    SourceImportStatus.FAILED,
                    SourceImportStatus.CANCELLED,
                    SourceImportStatus.INTERRUPTED,
                }:
                    raise SourceImportStateError(
                        "unsuccessful source import attempt receipts require a terminal state"
                    )
                if attempt_receipt.source_import_id != next_record.import_id:
                    raise SourceImportStateError(
                        "source import attempt receipt does not match its job"
                    )
                if attempt_receipt.provider != next_record.provider:
                    raise SourceImportStateError(
                        "source import attempt receipt provider does not match its job"
                    )
                if attempt_receipt.status != next_status.value:
                    raise SourceImportStateError(
                        "source import attempt receipt status does not match its job"
                    )
                try:
                    self.store.save_source_import_job_with_attempt_receipt(
                        self._payload(next_record),
                        attempt_receipt.model_dump(mode="json", by_alias=True),
                    )
                except (RuntimeError, ValueError) as exc:
                    raise SourceImportStateError(str(exc)) from exc
            else:
                self.store.save_source_import_job(
                    self._payload(next_record), expected_payload=self._payload(current),
                )
            return next_record

    @staticmethod
    def _payload(record: AnySourceImportJob) -> dict[str, object]:
        return record.model_dump(mode="json", by_alias=True)
