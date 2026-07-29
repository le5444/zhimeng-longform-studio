from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.project_models import (
    ProjectBindingListResponse,
    ProjectBindingSummary,
    ProjectFileContentResponse,
    ProjectFileReadRequest,
    ProjectInspectRequest,
    ProjectInspectionResponse,
    TextProjectImportRequest,
    TextProjectImportResponse,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_reader import (
    InvalidNovelProjectError,
    ProjectChangedDuringReadError,
    ProjectFileNotFoundError,
    ProjectReader,
    ProjectReaderError,
    ProjectScanLimitError,
    UntrustedProjectPathError,
    UnreadableProjectFileError,
)
from zhimeng_runtime.text_project_import_service import (
    TextProjectImportError,
    TextProjectImportService,
)


def create_project_router(
    reader: ProjectReader | None,
    store: SqliteStore,
    *,
    reader_error: str | None = None,
    locks: ProjectLockRegistry | None = None,
    text_import_service: TextProjectImportService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/projects", tags=["projects"])
    project_locks = locks or ProjectLockRegistry()

    def require_reader() -> ProjectReader:
        if reader is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=reader_error or "No trusted project roots are configured",
            )
        return reader

    def binding_payload(record: ProjectBindingRecord) -> ProjectBindingSummary:
        return ProjectBindingSummary(
            project_id=record.project_id,
            display_name=record.display_name,
            root_path=record.root_path,
            revision=record.revision,
            fingerprint=record.fingerprint or None,
            snapshot_ready=bool(record.fingerprint and record.revision >= 1),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def require_binding(project_id: str) -> ProjectBindingRecord:
        record = store.get_project_binding(project_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Project binding was not found",
            )
        return record

    def raise_project_error(exc: ProjectReaderError) -> NoReturn:
        if isinstance(exc, UntrustedProjectPathError):
            status_code = status.HTTP_403_FORBIDDEN
            code = "project_path_untrusted"
            retryable = False
        elif isinstance(exc, ProjectFileNotFoundError):
            status_code = status.HTTP_404_NOT_FOUND
            code = "project_file_not_found"
            retryable = False
        elif isinstance(exc, (InvalidNovelProjectError, UnreadableProjectFileError)):
            status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
            code = "project_file_unreadable" if isinstance(exc, UnreadableProjectFileError) else "invalid_novel_project"
            retryable = False
        elif isinstance(exc, ProjectScanLimitError):
            status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            code = "project_scan_limit"
            retryable = False
        elif isinstance(exc, ProjectChangedDuringReadError):
            status_code = status.HTTP_409_CONFLICT
            code = "project_snapshot_changed"
            retryable = True
        else:
            status_code = status.HTTP_409_CONFLICT
            code = "project_read_conflict"
            retryable = True
        raise ApiError(
            str(exc),
            status_code=status_code,
            code=code,
            retryable=retryable,
        ) from exc

    def etag_matches(header: str | None, etag: str) -> bool:
        if not header:
            return False
        for candidate in header.split(","):
            normalized = candidate.strip()
            if normalized == "*":
                return True
            if normalized.startswith("W/"):
                normalized = normalized[2:].strip()
            if normalized.strip('"') == etag:
                return True
        return False

    def representation_etag(payload: dict[str, object]) -> str:
        representation = {
            "project_id": payload["project_id"],
            "snapshot_revision": payload["snapshot_revision"],
            "project_fingerprint": payload["project_fingerprint"],
            "file": payload["file"],
        }
        return hashlib.sha256(
            json.dumps(
                representation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    async def inspect_root(root_path: str) -> ProjectInspectionResponse:
        active_reader = require_reader()
        try:
            resolved_root = await run_in_threadpool(active_reader.resolve, root_path)
        except ProjectReaderError as exc:
            raise_project_error(exc)
        async with project_locks.for_path(resolved_root):
            observed_at = datetime.now(UTC)
            try:
                payload = await run_in_threadpool(active_reader.inspect, resolved_root)
            except ProjectReaderError as exc:
                raise_project_error(exc)

            project = payload["project"]
            if not isinstance(project, dict):  # pragma: no cover - internal contract guard.
                raise RuntimeError("Project Reader returned an invalid project payload")
            record = await run_in_threadpool(
                store.sync_project_binding,
                project_id=str(project["project_id"]),
                root_path=str(project["root_path"]),
                display_name=str(project["display_name"]),
                fingerprint=str(project["fingerprint"]),
                observed_at=observed_at,
            )
            if record.fingerprint != project["fingerprint"]:
                raise_project_error(
                    ProjectChangedDuringReadError(
                        "a newer project snapshot was committed while this scan was running"
                    )
                )
            project["revision"] = record.revision
            return ProjectInspectionResponse.model_validate(payload)

    async def read_root_file(
        root_path: str,
        file_id: str,
    ) -> ProjectFileContentResponse:
        active_reader = require_reader()
        try:
            resolved_root = await run_in_threadpool(active_reader.resolve, root_path)
        except ProjectReaderError as exc:
            raise_project_error(exc)
        async with project_locks.for_path(resolved_root):
            observed_at = datetime.now(UTC)
            try:
                payload = await run_in_threadpool(
                    active_reader.read_text_file,
                    resolved_root,
                    file_id,
                )
            except ProjectReaderError as exc:
                raise_project_error(exc)

            project_id = str(payload["project_id"])
            record = await run_in_threadpool(
                store.sync_project_binding,
                project_id=project_id,
                root_path=str(resolved_root),
                display_name=resolved_root.name,
                fingerprint=str(payload["project_fingerprint"]),
                observed_at=observed_at,
            )
            if record.fingerprint != payload["project_fingerprint"]:
                raise_project_error(
                    ProjectChangedDuringReadError(
                        "a newer project snapshot was committed while this file was read"
                    )
                )
            payload["snapshot_revision"] = record.revision
            payload["etag"] = representation_etag(payload)
            return ProjectFileContentResponse.model_validate(payload)

    @router.get("", response_model=ProjectBindingListResponse)
    async def list_projects() -> ProjectBindingListResponse:
        records = await run_in_threadpool(store.list_project_bindings)
        return ProjectBindingListResponse(
            projects=[binding_payload(record) for record in records]
        )

    @router.post("/inspect", response_model=ProjectInspectionResponse)
    async def inspect_project(request: ProjectInspectRequest) -> ProjectInspectionResponse:
        return await inspect_root(request.root_path)

    @router.post(
        "/import-text",
        response_model=TextProjectImportResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_text_project(
        request: TextProjectImportRequest,
    ) -> TextProjectImportResponse:
        if text_import_service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="TXT project import is unavailable",
            )
        try:
            imported = await run_in_threadpool(
                text_import_service.import_text,
                file_name=request.file_name,
                content=request.content,
            )
        except TextProjectImportError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="text_project_import_invalid",
                retryable=False,
            ) from exc
        except OSError as exc:
            raise ApiError(
                "The imported work could not be saved",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="text_project_import_unavailable",
                retryable=True,
            ) from exc

        inspection = await inspect_root(str(imported.root_path))
        return TextProjectImportResponse(
            source_file_name=imported.source_file_name,
            title=imported.title,
            chapter_count=imported.chapter_count,
            inspection=inspection,
        )

    @router.get("/{project_id}/inspect", response_model=ProjectInspectionResponse)
    async def inspect_bound_project(project_id: str) -> ProjectInspectionResponse:
        binding = await run_in_threadpool(require_binding, project_id)
        return await inspect_root(binding.root_path)

    @router.post("/files/read", response_model=ProjectFileContentResponse)
    async def read_project_file(
        request: ProjectFileReadRequest,
        response: Response,
    ) -> ProjectFileContentResponse:
        result = await read_root_file(request.root_path, request.file_id)
        response.headers["ETag"] = f'"{result.etag}"'
        return result

    @router.get(
        "/{project_id}/files/{file_id}",
        response_model=ProjectFileContentResponse,
    )
    async def read_bound_project_file(
        project_id: str,
        file_id: str,
        request: Request,
        response: Response,
    ) -> ProjectFileContentResponse | Response:
        binding = await run_in_threadpool(require_binding, project_id)
        result = await read_root_file(binding.root_path, file_id)
        quoted_etag = f'"{result.etag}"'
        if etag_matches(request.headers.get("If-None-Match"), result.etag):
            return Response(
                status_code=status.HTTP_304_NOT_MODIFIED,
                headers={"ETag": quoted_etag},
            )
        response.headers["ETag"] = quoted_etag
        return result

    return router
