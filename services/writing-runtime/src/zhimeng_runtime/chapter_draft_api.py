from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from zhimeng_runtime.chapter_draft_service import (
    ChapterDraftError,
    ChapterDraftService,
    ChapterDraftUnavailable,
)
from zhimeng_runtime.errors import ApiError


class ChapterDraftRequest(BaseModel):
    base_revision: int = Field(ge=0)
    base_fingerprint: str = Field(min_length=1, max_length=128)
    target_characters: int = Field(default=2200, ge=200, le=20000)
    instruction: str | None = Field(default=None, max_length=2000)


class ChapterDraftResponse(BaseModel):
    schema_id: str = Field(default="zhimeng.chapter-draft.v1", alias="schema")
    change_set_id: str
    relative_path: str
    chapter_number: int
    characters: int
    model: str
    control_card_path: str | None = None
    ai_smell: dict | None = None
    knowledge: dict | None = None
    # 明确告诉前端：这只是候选，还没写进小说
    applied: bool = False
    requires_author_review: bool = True

    model_config = {"populate_by_name": True}


class PassageRewriteRequest(BaseModel):
    base_revision: int = Field(ge=0)
    base_fingerprint: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(min_length=1, max_length=1000)
    file_id: str = Field(min_length=1, max_length=200)
    passage: str = Field(min_length=1, max_length=20000)
    instruction: str = Field(min_length=1, max_length=2000)


class PassageRewriteResponse(BaseModel):
    schema_id: str = Field(default="zhimeng.passage-rewrite.v1", alias="schema")
    change_set_id: str
    relative_path: str
    original_characters: int
    rewritten_characters: int
    model: str
    ai_smell: dict | None = None
    applied: bool = False
    requires_author_review: bool = True

    model_config = {"populate_by_name": True}


def create_chapter_draft_router(
    service: ChapterDraftService | None,
    *,
    project_root_resolver=None,
) -> APIRouter:
    router = APIRouter(tags=["chapter-draft"])

    @router.post(
        "/api/v1/projects/{project_id}/chapter-drafts",
        response_model=ChapterDraftResponse,
        response_model_by_alias=True,
        status_code=status.HTTP_201_CREATED,
    )
    async def draft_chapter(
        project_id: str, request: ChapterDraftRequest
    ) -> ChapterDraftResponse:
        if service is None or project_root_resolver is None:
            raise ApiError(
                "章节写作服务不可用",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="chapter_draft_unavailable",
                retryable=False,
            )
        try:
            project_root = await run_in_threadpool(project_root_resolver, project_id)
        except FileNotFoundError as exc:
            raise ApiError(
                f"没有找到这本书: {project_id}",
                status_code=status.HTTP_404_NOT_FOUND,
                code="project_binding_not_found",
                retryable=False,
            ) from exc

        try:
            result = await run_in_threadpool(
                service.draft_next_chapter,
                project_id=project_id,
                project_root=Path(project_root),
                base_revision=request.base_revision,
                base_fingerprint=request.base_fingerprint,
                target_characters=request.target_characters,
                instruction=request.instruction,
            )
        except ChapterDraftUnavailable as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=exc.code,
                retryable=False,
            ) from exc
        except ChapterDraftError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_502_BAD_GATEWAY,
                code=exc.code,
                retryable=True,
            ) from exc

        return ChapterDraftResponse(
            change_set_id=result.change_set_id,
            relative_path=result.relative_path,
            chapter_number=result.chapter_number,
            characters=result.characters,
            model=result.model,
            control_card_path=result.control_card_path,
            ai_smell=result.ai_smell,
            knowledge=result.knowledge,
        )

    @router.post(
        "/api/v1/projects/{project_id}/passage-rewrites",
        response_model=PassageRewriteResponse,
        response_model_by_alias=True,
        status_code=status.HTTP_201_CREATED,
    )
    async def rewrite_passage(
        project_id: str, request: PassageRewriteRequest
    ) -> PassageRewriteResponse:
        if service is None or project_root_resolver is None:
            raise ApiError(
                "章节写作服务不可用",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="chapter_draft_unavailable",
                retryable=False,
            )
        try:
            project_root = await run_in_threadpool(project_root_resolver, project_id)
        except FileNotFoundError as exc:
            raise ApiError(
                f"没有找到这本书: {project_id}",
                status_code=status.HTTP_404_NOT_FOUND,
                code="project_binding_not_found",
                retryable=False,
            ) from exc

        try:
            result = await run_in_threadpool(
                service.rewrite_passage,
                project_id=project_id,
                project_root=Path(project_root),
                relative_path=request.relative_path,
                file_id=request.file_id,
                passage=request.passage,
                instruction=request.instruction,
                base_revision=request.base_revision,
                base_fingerprint=request.base_fingerprint,
            )
        except ChapterDraftUnavailable as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=exc.code,
                retryable=False,
            ) from exc
        except ChapterDraftError as exc:
            raise ApiError(
                str(exc),
                status_code=status.HTTP_400_BAD_REQUEST,
                code=exc.code,
                retryable=True,
            ) from exc

        return PassageRewriteResponse(
            change_set_id=result.change_set_id,
            relative_path=result.relative_path,
            original_characters=result.original_characters,
            rewritten_characters=result.rewritten_characters,
            model=result.model,
            ai_smell=result.ai_smell,
        )

    return router
