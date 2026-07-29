from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.agent_candidate_models import AgentCandidateSubmission
from zhimeng_runtime.agent_provider import ProviderToolDefinition
from zhimeng_runtime.change_set_models import (
    ChangeOperation,
    ChangeSetCreateRequest,
    ChangeSetOrigin,
    ChangeSetResponse,
    ProposedFileChangeInput,
    change_set_payload_hash,
)
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.change_set_service import ChangeSetError, ChangeSetService
from zhimeng_runtime.database import ProjectBindingRecord, SqliteStore
from zhimeng_runtime.execution_protocol import ArtifactPayload, Run, SkillCallPayload
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillError,
    REPARSE_POINT_ATTRIBUTE,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_models import ProjectInspectionResponse
from zhimeng_runtime.project_reader import (
    PUBLIC_TEXT_FILE_KINDS,
    ProjectReader,
    ProjectReaderError,
)
from zhimeng_runtime.search_models import SearchQueryRequest, SearchSourceType
from zhimeng_runtime.search_service import SearchService, SearchServiceError
from zhimeng_runtime.skill_executor import SkillExecutionOutcome, SkillExecutor
from zhimeng_runtime.skill_models import (
    CreationHandoffRequest,
    CreationHandoffResult,
    CreationProjectAuditRequest,
    CreationProjectAuditResult,
    CreationPrewriteGateRequest,
    CreationPrewriteGateResult,
)
from zhimeng_runtime.writer_models import WriterTask, writer_task_hash


MAX_TOOL_ARGUMENT_BYTES = 2 * 1024 * 1024
MAX_TOOL_RESULT_CHARACTERS = 256 * 1024
MAX_READ_LINES = 400
MAX_READ_CHARACTERS = 96 * 1024
MAX_SKILL_ARTIFACT_BYTES = 8 * 1024 * 1024
CREATION_PROJECT_AUDIT_SKILL_ID = "novel-creation-suite.project-audit"
CREATION_PREWRITE_GATE_SKILL_ID = "novel-creation-suite.prewrite-gate"
CREATION_HANDOFF_SKILL_ID = "novel-creation-suite.creation-handoff"
CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME = "candidate_output_submit"
_T = TypeVar("_T")


class _ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectListFilesArguments(_ToolArguments):
    query: str | None = Field(default=None, min_length=1, max_length=256)
    kind: Literal[
        "prose",
        "document",
        "control",
        "handoff",
        "context",
        "review",
        "status",
        "structured_state",
        "asset",
    ] | None = None
    limit: int = Field(default=50, ge=1, le=100)


class ProjectReadFileArguments(_ToolArguments):
    file_id: str = Field(pattern=r"^file-[0-9a-f]{32}$")
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=MAX_READ_LINES)
    start_offset: int = Field(default=0, ge=0, le=MAX_READ_CHARACTERS * 1024)


class ProjectSearchArguments(_ToolArguments):
    query: str = Field(min_length=1, max_length=512)
    source_types: list[
        Literal["project_file", "formal_knowledge", "authorized_source"]
    ] | None = Field(default=None, max_length=3)
    limit: int = Field(default=20, ge=1, le=100)


class ChangeSetProposeArguments(_ToolArguments):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=8000)
    changes: list[ProposedFileChangeInput] = Field(min_length=1, max_length=32)


class SkillExecuteArguments(_ToolArguments):
    """Only application-managed deterministic Creation capabilities are allowed."""

    skill_id: Literal[
        "novel-creation-suite.project-audit",
        "novel-creation-suite.prewrite-gate",
        "novel-creation-suite.creation-handoff",
    ]
    input: (
        CreationProjectAuditRequest
        | CreationPrewriteGateRequest
        | CreationHandoffRequest
    )

    @model_validator(mode="after")
    def validate_skill_input_pair(self) -> SkillExecuteArguments:
        expected_type = {
            CREATION_PROJECT_AUDIT_SKILL_ID: CreationProjectAuditRequest,
            CREATION_PREWRITE_GATE_SKILL_ID: CreationPrewriteGateRequest,
            CREATION_HANDOFF_SKILL_ID: CreationHandoffRequest,
        }[self.skill_id]
        if not isinstance(self.input, expected_type):
            raise ValueError("managed Skill ID and input contract do not match")
        return self


@dataclass(frozen=True, slots=True)
class AgentToolExecution:
    output: dict[str, Any]
    change_set: ChangeSetResponse | None = None
    skill_call: SkillCallPayload | None = None
    artifacts: tuple[ArtifactPayload, ...] = ()
    candidate_submission: AgentCandidateSubmission | None = None


class AgentToolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        skill_call: SkillCallPayload | None = None,
        artifacts: tuple[ArtifactPayload, ...] = (),
        status: Literal["failed", "cancelled"] = "failed",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.skill_call = skill_call
        self.artifacts = artifacts
        self.status = status


class AgentToolExecutor:
    """Runtime-owned project tools; none can apply changes to the novel."""

    def __init__(
        self,
        reader: ProjectReader,
        store: SqliteStore,
        locks: ProjectLockRegistry,
        change_sets: ChangeSetService,
        *,
        search_service: SearchService | None = None,
        skill_executor: SkillExecutor | None = None,
    ) -> None:
        self.reader = reader
        self.store = store
        self.locks = locks
        self.change_sets = change_sets
        self.search_service = search_service
        self.skill_executor = skill_executor
        self._skill_workspace_root = Path(store.path).parent / "skill-runs"
        self._skill_artifact_root = Path(store.path).parent / "skill-artifacts"
        self.definitions = (
            self._definition(
                "project_list_files",
                "1",
                "List bounded files in the current novel project. Paths outside the "
                "Run-bound project are never available.",
                ProjectListFilesArguments,
            ),
            self._definition(
                "project_read_file",
                "1",
                "Read a bounded range from one stable file_id in the current novel project. "
                "When next_offset is returned, pass it as start_offset to continue a long line.",
                ProjectReadFileArguments,
            ),
        )
        if self.search_service is not None:
            self.definitions = self.definitions + (
                self._definition(
                    "project_search",
                    "1",
                    "Search text inside the current Run-bound novel project, its active formal "
                    "knowledge cards, and sources already authorized for this project. Project "
                    "file hits return stable file_id values for project_read_file.",
                    ProjectSearchArguments,
                ),
            )
        self.definitions = self.definitions + (
            self._definition(
                "change_set_propose",
                "1",
                "Create a reviewable candidate ChangeSet in Runtime staging. This never applies "
                "changes to project files.",
                ChangeSetProposeArguments,
            ),
        )
        if self.skill_executor is not None:
            self.definitions = self.definitions + (
                self._definition(
                    "skill_execute",
                    "2",
                    "Execute an application-managed, read-only Creation capability against "
                    "an isolated snapshot of the Run-bound novel project. The Skill may only "
                    "write Runtime staging artifacts; it can never write project truth.",
                    SkillExecuteArguments,
                ),
            )
        # Keep this out of the ordinary catalog. AgentService appends it only
        # after the managed pre-Provider workflow has reached its boundary.
        self.candidate_output_definition = self._definition(
            CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
            "2",
            "Submit exactly one Runtime-validated author-review candidate. "
            "This is the final tool call for a managed Writing Agent Run; it "
            "cannot write formal draft text to project truth.",
            AgentCandidateSubmission,
        )
        self._definitions_by_name = {
            item.name: item
            for item in (*self.definitions, self.candidate_output_definition)
        }

    @staticmethod
    def _definition(
        name: str,
        version: str,
        description: str,
        arguments_model: type[BaseModel],
    ) -> ProviderToolDefinition:
        return ProviderToolDefinition(
            name=name,
            version=version,
            description=description,
            parameters=arguments_model.model_json_schema(),
        )

    @staticmethod
    def request_hash(
        tool_name: str,
        tool_version: str,
        arguments: dict[str, Any],
    ) -> str:
        encoded = json.dumps(
            {
                "tool_name": tool_name,
                "tool_version": tool_version,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_TOOL_ARGUMENT_BYTES:
            raise AgentToolError(
                "tool arguments exceed the Runtime byte limit",
                code="agent_tool_arguments_too_large",
                retryable=False,
            )
        return hashlib.sha256(encoded).hexdigest()

    async def execute(
        self,
        run: Run,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        request_hash: str,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if tool_name not in self._definitions_by_name:
            raise AgentToolError(
                f"tool is not registered for this Runtime: {tool_name}",
                code="agent_tool_not_allowed",
                retryable=False,
            )
        definition = self._definitions_by_name[tool_name]
        if request_hash != self.request_hash(
            tool_name,
            definition.version,
            arguments,
        ):
            raise AgentToolError(
                "tool request hash does not match its validated arguments",
                code="agent_tool_request_mismatch",
                retryable=False,
            )

        try:
            if tool_name == "project_list_files":
                parsed = ProjectListFilesArguments.model_validate(arguments)
                return await self._list_files(run, parsed, cancel_event)
            if tool_name == "project_read_file":
                parsed = ProjectReadFileArguments.model_validate(arguments)
                return await self._read_file(run, parsed, cancel_event)
            if tool_name == "project_search":
                parsed = ProjectSearchArguments.model_validate(arguments)
                return await self._search(run, parsed, cancel_event)
            if tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME:
                parsed = AgentCandidateSubmission.model_validate_json(
                    self._canonical_json(arguments),
                    strict=True,
                )
                # Recheck the bound project at the submission boundary. A
                # Provider may have spent several turns after the initial
                # pre-Provider validation.
                await self.validate_run_project(run)
                return AgentToolExecution(
                    output={
                        "schema": "zhimeng.tool-result.candidate-output-submit.v1",
                        "accepted": True,
                        "candidate_kind": parsed.kind,
                        "requires_author_review": True,
                        "formal_draft_allowed": False,
                    },
                    candidate_submission=parsed,
                )
            if tool_name == "skill_execute":
                parsed = SkillExecuteArguments.model_validate_json(
                    self._canonical_json(arguments),
                    strict=True,
                )
                return await self._execute_skill(
                    run,
                    parsed,
                    tool_call_id=tool_call_id,
                    request_hash=request_hash,
                    cancel_event=cancel_event,
                )
            parsed = ChangeSetProposeArguments.model_validate(arguments)
            return await self._propose_change_set(
                run,
                parsed,
                tool_call_id=tool_call_id,
                request_hash=request_hash,
                cancel_event=cancel_event,
            )
        except AgentToolError:
            raise
        except SearchServiceError as exc:
            raise AgentToolError(
                str(exc) or "Project search failed",
                code="agent_tool_search_failed",
                retryable=exc.retryable,
            ) from exc
        except (ProjectReaderError, ChangeSetError, ValueError) as exc:
            raise AgentToolError(
                str(exc) or "Runtime tool validation failed",
                code="agent_tool_validation_failed",
                retryable=False,
            ) from exc

    async def _search(
        self,
        run: Run,
        arguments: ProjectSearchArguments,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        if self.search_service is None:
            raise AgentToolError(
                "Project search is unavailable",
                code="agent_tool_search_unavailable",
                retryable=True,
            )
        binding = await self._binding(run)
        root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        async with self.locks.for_path(root):
            await self.validate_run_project(run)
            if cancel_event.is_set():
                raise asyncio.CancelledError
            response = await asyncio.to_thread(
                self.search_service.search,
                run.project_revision.project_id,
                SearchQueryRequest(
                    query=arguments.query,
                    source_types=(
                        [SearchSourceType(item) for item in arguments.source_types]
                        if arguments.source_types is not None
                        else None
                    ),
                    limit=arguments.limit,
                    auto_rebuild=True,
                ),
            )
            # External editors do not share the Runtime lock. Recheck after
            # indexing so changed project truth cannot leak into an old Run.
            await self.validate_run_project(run)
            return self._bounded_result(
                {
                    "schema": "zhimeng.tool-result.project-search.v1",
                    "project_id": run.project_revision.project_id,
                    "project_revision": run.project_revision.revision,
                    "project_fingerprint": run.project_revision.fingerprint,
                    "query": response.query,
                    "index_hash": response.index_hash,
                    "rebuilt_before_search": response.rebuilt_before_search,
                    "hit_count": response.hit_count,
                    "hits": [
                        {
                            "source_id": hit.source_id,
                            "source_type": hit.source_type.value,
                            "authority": hit.authority.value,
                            "title": hit.title,
                            "file_id": (
                                hit.source_id
                                if hit.source_type is SearchSourceType.PROJECT_FILE
                                else None
                            ),
                            "relative_path": hit.relative_path,
                            "version": hit.version,
                            "source_hash": hit.source_hash,
                            "snippet": hit.snippet,
                            "reason": hit.reason,
                            "score": hit.score,
                            "updated_at": (
                                hit.updated_at.isoformat()
                                if hit.updated_at is not None
                                else None
                            ),
                        }
                        for hit in response.hits
                    ],
                }
            )

    async def validate_run_project(self, run: Run) -> ProjectInspectionResponse:
        """Re-read project truth before resuming durable workflow execution."""

        binding = await self._binding(run)
        return await self._current_snapshot(run, binding)

    async def _list_files(
        self,
        run: Run,
        arguments: ProjectListFilesArguments,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        binding = await self._binding(run)
        root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        async with self.locks.for_path(root):
            snapshot = await self._current_snapshot(run, binding)
            if cancel_event.is_set():
                raise asyncio.CancelledError
            query = arguments.query.casefold() if arguments.query else None
            matches = [
                item
                for item in snapshot.files
                if (arguments.kind is None or item.kind == arguments.kind)
                and (query is None or query in item.relative_path.casefold())
            ]
            selected = matches[: arguments.limit]
            return self._bounded_result(
                {
                    "schema": "zhimeng.tool-result.project-list-files.v1",
                    "project_id": run.project_revision.project_id,
                    "project_revision": run.project_revision.revision,
                    "project_fingerprint": run.project_revision.fingerprint,
                    "matched_count": len(matches),
                    "truncated": len(selected) < len(matches),
                    "files": [
                        {
                            "file_id": item.file_id,
                            "relative_path": item.relative_path,
                            "kind": item.kind,
                            "chapter_id": item.chapter_id,
                            "volume_id": item.volume_id,
                            "size_bytes": item.size_bytes,
                            "sha256": item.sha256,
                        }
                        for item in selected
                    ],
                }
            )

    async def _read_file(
        self,
        run: Run,
        arguments: ProjectReadFileArguments,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        binding = await self._binding(run)
        root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        async with self.locks.for_path(root):
            snapshot = await self._current_snapshot(run, binding)
            if cancel_event.is_set():
                raise asyncio.CancelledError
            raw_snapshot = snapshot.model_dump(mode="python", by_alias=True)
            result = await asyncio.to_thread(
                self.reader.read_text_file_from_snapshot,
                raw_snapshot,
                arguments.file_id,
                allowed_kinds=PUBLIC_TEXT_FILE_KINDS,
            )
            content = str(result["content"])
            lines = content.splitlines(keepends=True)
            if arguments.start_offset:
                offset = min(arguments.start_offset, len(content))
                line_start = content.count("\n", 0, offset) + 1
                selected = content[offset:].splitlines(keepends=True)[
                    : arguments.max_lines
                ]
                base_offset = offset
            else:
                start_index = min(arguments.start_line - 1, len(lines))
                line_start = start_index + 1
                selected = lines[start_index : start_index + arguments.max_lines]
                base_offset = sum(len(line) for line in lines[:start_index])
            excerpt = "".join(selected)
            character_truncated = len(excerpt) > MAX_READ_CHARACTERS
            if character_truncated:
                excerpt = excerpt[:MAX_READ_CHARACTERS]
            file_record = result["file"]
            if not isinstance(file_record, dict):
                raise AgentToolError(
                    "Project Reader returned an invalid file record",
                    code="agent_tool_result_invalid",
                    retryable=False,
                )
            consumed_offset = base_offset + len(excerpt)
            has_more = consumed_offset < len(content)
            ends_with_newline = excerpt.endswith(("\n", "\r"))
            end_line = line_start + excerpt.count("\n") - (
                1 if ends_with_newline and excerpt else 0
            )
            return self._bounded_result(
                {
                    "schema": "zhimeng.tool-result.project-read-file.v1",
                    "project_id": run.project_revision.project_id,
                    "project_revision": run.project_revision.revision,
                    "project_fingerprint": run.project_revision.fingerprint,
                    "file_id": arguments.file_id,
                    "relative_path": file_record.get("relative_path"),
                    "sha256": file_record.get("sha256"),
                    "start_line": line_start,
                    "end_line": max(line_start, end_line),
                    "total_lines": len(lines),
                    "truncated": character_truncated or has_more,
                    "next_offset": consumed_offset if has_more else None,
                    "content": excerpt,
                }
            )

    async def _propose_change_set(
        self,
        run: Run,
        arguments: ChangeSetProposeArguments,
        *,
        tool_call_id: str,
        request_hash: str,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        binding = await self._binding(run)
        root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        async with self.locks.for_path(root):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            await asyncio.to_thread(
                self._validate_writer_change_policy,
                run,
                arguments,
            )
            request = ChangeSetCreateRequest(
                base_revision=run.project_revision.revision,
                base_fingerprint=run.project_revision.fingerprint,
                title=arguments.title,
                summary=arguments.summary,
                origin=ChangeSetOrigin.AGENT,
                source_run_id=run.run_id,
                source_tool_call_id=tool_call_id,
                source_request_hash=request_hash,
                changes=arguments.changes,
            )
            change_set = await asyncio.to_thread(
                self.change_sets.create_agent_candidate,
                run.project_revision.project_id,
                request,
            )
            hunk_count = sum(len(change.hunks) for change in change_set.changes)
            output = self._bounded_result(
                {
                    "schema": "zhimeng.tool-result.change-set-propose.v1",
                    "change_set_id": change_set.change_set_id,
                    "change_set_hash": change_set_payload_hash(change_set),
                    "status": change_set.status.value,
                    "title": change_set.title,
                    "file_count": len(change_set.changes),
                    "hunk_count": hunk_count,
                    "base_revision": change_set.base_revision,
                    "base_fingerprint": change_set.base_fingerprint,
                    "requires_author_review": True,
                    "applied": False,
                }
            )
            return AgentToolExecution(output=output.output, change_set=change_set)

    def _validate_writer_change_policy(
        self,
        run: Run,
        arguments: ChangeSetProposeArguments,
    ) -> None:
        """Fail closed when a Run owns an explicit Runtime Writer intent."""

        with self.store.connection() as connection:
            intent_rows = connection.execute(
                """
                SELECT content_hash, metadata_json FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_intent'
                ORDER BY id
                """,
                (run.run_id,),
            ).fetchall()
            task_rows = connection.execute(
                """
                SELECT * FROM artifact
                WHERE run_id = ? AND artifact_type = 'writer_task'
                ORDER BY id
                """,
                (run.run_id,),
            ).fetchall()
            existing_change_sets = connection.execute(
                """
                SELECT COUNT(*) FROM change_set_provenance
                WHERE run_id = ?
                """,
                (run.run_id,),
            ).fetchone()

        if len(intent_rows) > 1 or len(task_rows) > 1:
            raise AgentToolError(
                "Writer policy evidence is ambiguous",
                code="writer_task_integrity_failed",
                retryable=False,
            )

        intent_mode: str | None = None
        if intent_rows:
            try:
                intent = json.loads(str(intent_rows[0]["metadata_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AgentToolError(
                    "Writer intent failed integrity validation",
                    code="writer_task_integrity_failed",
                    retryable=False,
                ) from exc
            if (
                not isinstance(intent, dict)
                or intent.get("schema") != "zhimeng.writer-intent.v1"
                or intent.get("mode")
                not in {"repair_only", "author_review_change_proposal"}
                or canonical_json_hash(intent) != str(intent_rows[0]["content_hash"])
            ):
                raise AgentToolError(
                    "Writer intent failed integrity validation",
                    code="writer_task_integrity_failed",
                    retryable=False,
                )
            intent_mode = str(intent["mode"])

        if not task_rows:
            if intent_mode == "repair_only":
                raise AgentToolError(
                    "Repair-only Writer mode forbids ChangeSet proposals",
                    code="writer_change_set_forbidden",
                    retryable=False,
                )
            if intent_mode is not None:
                raise AgentToolError(
                    "Writer ChangeSet proposals require a persisted Writer task",
                    code="writer_task_missing",
                    retryable=False,
                )
            return

        try:
            task = WriterTask.model_validate_json(str(task_rows[0]["metadata_json"]))
        except (TypeError, ValueError) as exc:
            raise AgentToolError(
                "Writer task failed integrity validation",
                code="writer_task_integrity_failed",
                retryable=False,
            ) from exc
        if (
            str(task_rows[0]["id"]) != task.writer_task_id
            or str(task_rows[0]["content_hash"]) != task.task_hash
            or writer_task_hash(task) != task.task_hash
            or task.run_id != run.run_id
            or task.project_id != run.project_revision.project_id
            or task.project_revision != run.project_revision.revision
            or task.project_fingerprint != run.project_revision.fingerprint
            or (intent_mode is not None and task.mode != intent_mode)
        ):
            raise AgentToolError(
                "Writer task failed integrity validation",
                code="writer_task_integrity_failed",
                retryable=False,
            )
        if task.mode == "repair_only":
            raise AgentToolError(
                "Repair-only Writer mode forbids ChangeSet proposals",
                code="writer_change_set_forbidden",
                retryable=False,
            )
        if existing_change_sets is not None and int(existing_change_sets[0]) != 0:
            raise AgentToolError(
                "Writer Run already owns a ChangeSet proposal",
                code="writer_change_set_duplicate",
                retryable=False,
            )

        proposed_file_ids = tuple(
            getattr(change, "file_id", None) for change in arguments.changes
        )
        target_file_ids = tuple(target.file_id for target in task.target_files)
        if (
            any(change.operation is not ChangeOperation.UPDATE for change in arguments.changes)
            or len(proposed_file_ids) != len(set(proposed_file_ids))
            or set(proposed_file_ids) != set(target_file_ids)
            or len(proposed_file_ids) != len(target_file_ids)
        ):
            raise AgentToolError(
                "Writer ChangeSet must update exactly the task prose targets",
                code="writer_change_set_target_invalid",
                retryable=False,
            )

    async def _execute_skill(
        self,
        run: Run,
        arguments: SkillExecuteArguments,
        *,
        tool_call_id: str,
        request_hash: str,
        cancel_event: asyncio.Event,
    ) -> AgentToolExecution:
        executor = self.skill_executor
        if executor is None:
            raise AgentToolError(
                "managed Skill execution is not configured",
                code="agent_skill_unavailable",
                retryable=False,
            )

        binding = await self._binding(run)
        root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        workspace: Path | None = None
        outcome: SkillExecutionOutcome | None = None
        try:
            # Creating the directory is a tiny local operation and must finish before
            # this coroutine can own (and therefore clean) the returned path.
            workspace = self._create_skill_workspace(
                run.run_id,
                tool_call_id,
                request_hash,
            )
            project_snapshot = workspace / "project-snapshot"
            run_staging = workspace / "run-staging"
            async with self.locks.for_path(root):
                baseline = await self._current_snapshot(run, binding)
                await self._await_owned_workspace_io(
                    self._copy_project_snapshot,
                    root,
                    baseline,
                    project_snapshot,
                )
                if cancel_event.is_set():
                    raise asyncio.CancelledError

                outcome = await self._await_skill_outcome(
                    executor,
                    run=run,
                    arguments=arguments,
                    tool_call_id=tool_call_id,
                    project_snapshot=project_snapshot,
                    run_staging=run_staging,
                    cancel_event=cancel_event,
                )

                postcondition_error: AgentToolError | None = None
                try:
                    await self._current_snapshot(run, binding)
                except AgentToolError as exc:
                    postcondition_error = exc

                try:
                    artifacts = await self._await_owned_workspace_io(
                        self._persist_skill_outcome,
                        run,
                        tool_call_id,
                        outcome,
                        run_staging,
                    )
                except AgentToolError as exc:
                    skill_call = self._skill_call(
                        run,
                        tool_call_id,
                        arguments,
                        outcome,
                        status="failed",
                    )
                    raise AgentToolError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                        skill_call=skill_call,
                        artifacts=exc.artifacts,
                    ) from exc

                if postcondition_error is not None:
                    skill_call = self._skill_call(
                        run,
                        tool_call_id,
                        arguments,
                        outcome,
                        status="failed",
                        result_summary=(
                            "The managed Skill finished, but the bound project changed "
                            "before the Runtime could accept its result."
                        ),
                    )
                    raise AgentToolError(
                        str(postcondition_error),
                        code=postcondition_error.code,
                        retryable=postcondition_error.retryable,
                        skill_call=skill_call,
                        artifacts=artifacts,
                    ) from postcondition_error

                receipt = outcome.receipt
                if receipt.execution_status != "succeeded":
                    skill_status: Literal["failed", "cancelled"] = (
                        "cancelled"
                        if receipt.execution_status == "cancelled"
                        else "failed"
                    )
                    skill_call = self._skill_call(
                        run,
                        tool_call_id,
                        arguments,
                        outcome,
                        status=skill_status,
                    )
                    error = receipt.error
                    raise AgentToolError(
                        (
                            error.message
                            if error is not None
                            else "managed Skill execution did not succeed"
                        ),
                        code=(
                            f"agent_skill_{error.code}"
                            if error is not None
                            else "agent_skill_execution_failed"
                        ),
                        retryable=bool(error and error.retryable),
                        skill_call=skill_call,
                        artifacts=artifacts,
                        status=skill_status,
                    )

                try:
                    domain_result = self._managed_skill_result(
                        arguments.skill_id,
                        outcome.result,
                    )
                except ValueError as exc:
                    skill_call = self._skill_call(
                        run,
                        tool_call_id,
                        arguments,
                        outcome,
                        status="failed",
                        result_summary=(
                            "The managed Skill output did not match the declared "
                            "managed Creation result contract."
                        ),
                    )
                    raise AgentToolError(
                        "managed Skill result failed its declared output contract",
                        code="agent_skill_result_invalid",
                        retryable=False,
                        skill_call=skill_call,
                        artifacts=artifacts,
                    ) from exc

                skill_call = self._skill_call(
                    run,
                    tool_call_id,
                    arguments,
                    outcome,
                    status="succeeded",
                )
                try:
                    bounded = self._bounded_result(
                        self._skill_tool_result(
                            run,
                            tool_call_id,
                            outcome,
                            domain_result,
                            artifacts,
                        )
                    )
                    await self._current_snapshot(run, binding)
                except AgentToolError as exc:
                    raise AgentToolError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                        skill_call=skill_call,
                        artifacts=artifacts,
                    ) from exc
                return AgentToolExecution(
                    output=bounded.output,
                    skill_call=skill_call,
                    artifacts=artifacts,
                )
        except AgentToolError:
            raise
        except ManagedSkillError as exc:
            raise AgentToolError(
                str(exc) or "managed Skill is unavailable",
                code=f"agent_skill_{exc.code}",
                retryable=exc.retryable,
            ) from exc
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise AgentToolError(
                str(exc) or "managed Skill execution boundary failed",
                code="agent_skill_execution_boundary_failed",
                retryable=False,
            ) from exc
        finally:
            if workspace is not None and workspace.exists():
                try:
                    await self._await_owned_workspace_io(
                        self._remove_isolated_tree,
                        workspace,
                        self._skill_workspace_root,
                    )
                except OSError as exc:
                    raise AgentToolError(
                        "Runtime could not remove the managed Skill workspace",
                        code="agent_skill_cleanup_failed",
                        retryable=True,
                    ) from exc

    async def _await_skill_outcome(
        self,
        executor: SkillExecutor,
        *,
        run: Run,
        arguments: SkillExecuteArguments,
        tool_call_id: str,
        project_snapshot: Path,
        run_staging: Path,
        cancel_event: asyncio.Event,
    ) -> SkillExecutionOutcome:
        task = asyncio.create_task(
            executor.execute(
                project_id=run.project_revision.project_id,
                project_revision=run.project_revision.revision,
                project_fingerprint=run.project_revision.fingerprint,
                run_id=run.run_id,
                tool_call_id=tool_call_id,
                skill_id=arguments.skill_id,
                input_payload=arguments.input.model_dump(
                    mode="json",
                    by_alias=True,
                ),
                project_snapshot=project_snapshot,
                run_staging=run_staging,
                cancellation_event=cancel_event,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # AgentService sets the shared event and cancels its Run task together.
            # Let the executor kill its child and return the durable cancellation
            # receipt before this tool reports cancellation.
            cancel_event.set()
            return await asyncio.shield(task)

    @staticmethod
    def _managed_skill_result(
        skill_id: str,
        result: object,
    ) -> (
        CreationProjectAuditResult
        | CreationPrewriteGateResult
        | CreationHandoffResult
    ):
        if skill_id == CREATION_PROJECT_AUDIT_SKILL_ID:
            return CreationProjectAuditResult.model_validate(result, strict=True)
        if skill_id == CREATION_PREWRITE_GATE_SKILL_ID:
            return CreationPrewriteGateResult.model_validate(result, strict=True)
        if skill_id == CREATION_HANDOFF_SKILL_ID:
            return CreationHandoffResult.model_validate(result, strict=True)
        raise ValueError("managed Skill result has no Runtime contract")

    @staticmethod
    def _skill_call(
        run: Run,
        tool_call_id: str,
        arguments: SkillExecuteArguments,
        outcome: SkillExecutionOutcome,
        *,
        status: Literal["succeeded", "failed", "cancelled"],
        result_summary: str | None = None,
    ) -> SkillCallPayload:
        identity = hashlib.sha256(
            f"{run.run_id}\0{tool_call_id}\0{arguments.skill_id}".encode("utf-8")
        ).hexdigest()
        return SkillCallPayload(
            skill_call_id=f"skill-call-{identity[:32]}",
            skill_id=arguments.skill_id,
            skill_version=outcome.receipt.skill_version,
            status=status,
            input=arguments.input.model_dump(mode="json", by_alias=True),
            output={
                "receipt": outcome.receipt.model_dump(mode="json", by_alias=True),
                "result_summary": (
                    result_summary
                    if result_summary is not None
                    else outcome.result_summary
                ),
            },
        )

    @staticmethod
    def _skill_tool_result(
        run: Run,
        tool_call_id: str,
        outcome: SkillExecutionOutcome,
        domain_result: (
            CreationProjectAuditResult
            | CreationPrewriteGateResult
            | CreationHandoffResult
        ),
        artifacts: tuple[ArtifactPayload, ...],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": "zhimeng.tool-result.skill-execute.v1",
            "tool_call_id": tool_call_id,
            "skill_id": outcome.receipt.skill_id,
            "skill_version": outcome.receipt.skill_version,
            "skill_version_id": outcome.receipt.skill_version_id,
            "receipt_id": outcome.receipt.receipt_id,
            "receipt_hash": outcome.receipt_hash,
            "execution_status": outcome.receipt.execution_status,
            "domain_status": outcome.receipt.domain_status,
            "result_summary": outcome.result_summary,
            "project_id": run.project_revision.project_id,
            "project_revision": run.project_revision.revision,
            "project_fingerprint": run.project_revision.fingerprint,
            "project_unchanged": True,
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "name": artifact.name,
                    "uri": artifact.uri,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                }
                for artifact in artifacts
            ],
            "writes_project_truth": False,
        }
        if isinstance(domain_result, CreationProjectAuditResult):
            payload["audit"] = {
                "health": domain_result.health,
                "counts": domain_result.counts.model_dump(mode="json", by_alias=True),
                "expected_files": domain_result.expected_files,
                "chapter_files": domain_result.chapter_files,
                "issue_cards": domain_result.issue_cards,
                "finding_count": len(domain_result.findings),
                "next_actions": domain_result.next_actions[:32],
            }
        elif isinstance(domain_result, CreationPrewriteGateResult):
            payload["prewrite_gate"] = domain_result.model_dump(
                mode="json",
                by_alias=True,
            )
        elif isinstance(domain_result, CreationHandoffResult):
            payload["creation_handoff"] = domain_result.model_dump(
                mode="json",
                by_alias=True,
            )
        return payload

    @staticmethod
    async def _await_owned_workspace_io(
        operation: Callable[..., _T],
        *args: Any,
    ) -> _T:
        """Do not release workspace ownership while its worker still uses it."""

        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # ``to_thread`` workers cannot be cancelled. Wait for the worker before
            # propagating cancellation so the caller's finally block can safely
            # remove the workspace without racing late writes.
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise

    def _create_skill_workspace(
        self,
        run_id: str,
        tool_call_id: str,
        request_hash: str,
    ) -> Path:
        root = self._ensure_runtime_directory(self._skill_workspace_root)
        identity = hashlib.sha256(
            f"{run_id}\0{tool_call_id}\0{request_hash}".encode("utf-8")
        ).hexdigest()[:24]
        workspace = Path(tempfile.mkdtemp(prefix=f"skill-{identity}-", dir=str(root)))
        try:
            self._ensure_regular_directory(workspace, label="Skill workspace")
            (workspace / "project-snapshot").mkdir()
            (workspace / "run-staging").mkdir()
            self._ensure_regular_directory(
                workspace / "project-snapshot",
                label="project snapshot",
            )
            self._ensure_regular_directory(
                workspace / "run-staging",
                label="run staging",
            )
            return workspace
        except Exception:
            if workspace.exists():
                self._remove_isolated_tree(workspace, root)
            raise

    def _copy_project_snapshot(
        self,
        source_root: Path,
        snapshot: ProjectInspectionResponse,
        destination: Path,
    ) -> None:
        source_root = self._ensure_regular_directory(
            source_root,
            label="bound project",
        )
        destination = self._ensure_regular_directory(destination, label="project snapshot")
        if self._paths_overlap(destination, source_root):
            raise AgentToolError(
                "isolated project snapshot cannot overlap the bound project",
                code="agent_skill_snapshot_boundary_failed",
                retryable=False,
            )

        # Empty 01-09 section directories are part of the project shape even
        # though ProjectReader has no file entry for them.
        for section in snapshot.sections:
            if section.relative_path is None:
                continue
            self._make_relative_directory(destination, section.relative_path)

        seen_paths: set[str] = set()
        for record in snapshot.files:
            relative = self._safe_relative_path(record.relative_path)
            identity = relative.as_posix().casefold()
            if identity in seen_paths:
                raise AgentToolError(
                    "project snapshot contains duplicate paths",
                    code="agent_skill_snapshot_invalid",
                    retryable=False,
                )
            seen_paths.add(identity)
            source = source_root.joinpath(*relative.parts)
            destination_file = destination.joinpath(*relative.parts)
            self._make_relative_directory(destination, relative.parent.as_posix())
            expected = (record.size_bytes, record.sha256)
            self._copy_regular_file(
                source,
                destination_file,
                source_root=source_root,
                expected_size=expected[0],
                expected_sha256=expected[1],
            )

        self._validate_tree(destination, label="project snapshot")
        self._make_snapshot_read_only(destination)

    def _persist_skill_outcome(
        self,
        run: Run,
        tool_call_id: str,
        outcome: SkillExecutionOutcome,
        staging: Path,
    ) -> tuple[ArtifactPayload, ...]:
        root = self._ensure_runtime_directory(self._skill_artifact_root)
        run_key = hashlib.sha256(
            f"{run.run_id}\0{tool_call_id}".encode("utf-8")
        ).hexdigest()[:32]
        target_root = root / run_key
        if target_root.exists():
            self._ensure_regular_directory(target_root, label="Skill artifact directory")
        else:
            target_root.mkdir()
            self._ensure_regular_directory(target_root, label="Skill artifact directory")

        receipt_bytes = self._canonical_json(
            outcome.receipt.model_dump(mode="json", by_alias=True)
        )
        if hashlib.sha256(receipt_bytes).hexdigest() != outcome.receipt_hash:
            raise AgentToolError(
                "managed Skill receipt hash is internally inconsistent",
                code="agent_skill_receipt_invalid",
                retryable=False,
            )
        receipt_payload = self._write_runtime_artifact(
            target_root,
            run,
            tool_call_id,
            artifact_type="skill_execution_receipt",
            filename="skill-execution-receipt.json",
            content=receipt_bytes,
        )
        persisted: list[ArtifactPayload] = [receipt_payload]

        if outcome.receipt.execution_status != "succeeded":
            return tuple(persisted)
        try:
            persisted.append(
                self._persist_managed_result_artifact(
                    target_root,
                    run,
                    tool_call_id,
                    outcome,
                    staging,
                )
            )
        except AgentToolError as exc:
            if exc.artifacts:
                raise
            raise AgentToolError(
                str(exc),
                code=exc.code,
                retryable=exc.retryable,
                artifacts=tuple(persisted),
            ) from exc
        return tuple(persisted)

    def _persist_managed_result_artifact(
        self,
        target_root: Path,
        run: Run,
        tool_call_id: str,
        outcome: SkillExecutionOutcome,
        staging: Path,
    ) -> ArtifactPayload:
        result_model: (
            type[CreationProjectAuditResult]
            | type[CreationPrewriteGateResult]
            | type[CreationHandoffResult]
        )
        if outcome.receipt.skill_id == CREATION_PROJECT_AUDIT_SKILL_ID:
            result_model = CreationProjectAuditResult
            artifact_type = "project_audit"
            filename = "project-audit.json"
        elif outcome.receipt.skill_id == CREATION_PREWRITE_GATE_SKILL_ID:
            result_model = CreationPrewriteGateResult
            artifact_type = "prewrite_gate"
            filename = "prewrite-gate.json"
        elif outcome.receipt.skill_id == CREATION_HANDOFF_SKILL_ID:
            result_model = CreationHandoffResult
            artifact_type = "creation_handoff"
            filename = "creation-handoff.json"
        else:
            raise AgentToolError(
                "managed Skill has no declared Runtime artifact contract",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        if len(outcome.artifacts) != 1:
            raise AgentToolError(
                "managed Creation Skill must return exactly one result artifact",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        output_artifact = outcome.artifacts[0]
        if output_artifact.artifact_type != artifact_type:
            raise AgentToolError(
                "managed Creation Skill returned an unexpected artifact type",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        result_content, file_sha256, payload_sha256 = self._read_skill_artifact(
            output_artifact,
            staging,
        )
        expected_receipt_hash = self._artifact_receipt_hash(
            file_sha256,
            payload_sha256,
        )
        matching_receipts = [
            item
            for item in outcome.receipt.artifacts
            if item.artifact_type == output_artifact.artifact_type
            and item.relative_path == output_artifact.relative_path
        ]
        if (
            len(matching_receipts) != 1
            or matching_receipts[0].content_hash != expected_receipt_hash
        ):
            raise AgentToolError(
                "managed Skill result artifact does not match its receipt",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        try:
            parsed = json.loads(result_content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentToolError(
                "managed Skill result artifact is not UTF-8 JSON",
                code="agent_skill_artifact_invalid",
                retryable=False,
            ) from exc
        try:
            result_model.model_validate(parsed, strict=True)
        except ValueError as exc:
            raise AgentToolError(
                "managed Skill result artifact failed its declared result contract",
                code="agent_skill_artifact_invalid",
                retryable=False,
            ) from exc
        if self._canonical_json(parsed) != self._canonical_json(outcome.result):
            raise AgentToolError(
                "managed Skill result artifact differs from its stdout result",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        if (
            output_artifact.payload is not None
            and self._canonical_json(parsed)
            != self._canonical_json(output_artifact.payload)
        ):
            raise AgentToolError(
                "managed Skill result file differs from its declared artifact payload",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        return self._write_runtime_artifact(
            target_root,
            run,
            tool_call_id,
            artifact_type=artifact_type,
            filename=filename,
            content=result_content,
        )

    def _read_skill_artifact(
        self,
        artifact: Any,
        staging: Path,
    ) -> tuple[bytes, str | None, str | None]:
        file_sha256: str | None = None
        content: bytes
        if artifact.relative_path is not None:
            relative = self._safe_relative_path(artifact.relative_path)
            target = staging.joinpath(*relative.parts)
            content = self._read_regular_file(
                target,
                root=staging,
                max_bytes=MAX_SKILL_ARTIFACT_BYTES,
            )
            file_sha256 = hashlib.sha256(content).hexdigest()
        elif artifact.payload is not None:
            content = self._canonical_json(artifact.payload)
        else:
            raise AgentToolError(
                "managed Skill artifact has neither a file nor a payload",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        if len(content) > MAX_SKILL_ARTIFACT_BYTES:
            raise AgentToolError(
                "managed Skill artifact exceeds the Runtime byte limit",
                code="agent_skill_artifact_too_large",
                retryable=False,
            )
        payload_sha256 = (
            None
            if artifact.payload is None
            else hashlib.sha256(self._canonical_json(artifact.payload)).hexdigest()
        )
        return content, file_sha256, payload_sha256

    def _write_runtime_artifact(
        self,
        target_root: Path,
        run: Run,
        tool_call_id: str,
        *,
        artifact_type: str,
        filename: str,
        content: bytes,
    ) -> ArtifactPayload:
        if len(content) > MAX_SKILL_ARTIFACT_BYTES:
            raise AgentToolError(
                "Runtime artifact exceeds the byte limit",
                code="agent_skill_artifact_too_large",
                retryable=False,
            )
        target = target_root / filename
        self._ensure_regular_directory(target_root, label="Skill artifact directory")
        self._write_atomic_bytes(target, content)
        content_hash = hashlib.sha256(content).hexdigest()
        artifact_identity = hashlib.sha256(
            f"{run.run_id}\0{tool_call_id}\0{artifact_type}\0{content_hash}".encode(
                "utf-8"
            )
        ).hexdigest()
        return ArtifactPayload(
            artifact_id=f"artifact-{artifact_identity[:32]}",
            artifact_type=artifact_type,
            name=filename,
            uri=f"runtime://skill-runs/{run.run_id}/{tool_call_id}/{filename}",
            media_type="application/json",
            content_hash=content_hash,
            size_bytes=len(content),
        )

    @staticmethod
    def _artifact_receipt_hash(
        file_sha256: str | None,
        payload_sha256: str | None,
    ) -> str:
        return hashlib.sha256(
            AgentToolExecutor._canonical_json(
                {
                    "file_sha256": file_sha256,
                    "payload_sha256": payload_sha256,
                }
            )
        ).hexdigest()

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @staticmethod
    def _safe_relative_path(value: str) -> PurePosixPath:
        if not value or "\\" in value:
            raise AgentToolError(
                "managed Skill paths must use portable relative paths",
                code="agent_skill_snapshot_invalid",
                retryable=False,
            )
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise AgentToolError(
                "managed Skill paths must remain inside their execution root",
                code="agent_skill_snapshot_invalid",
                retryable=False,
            )
        return path

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        value = path.lstat()
        return stat.S_ISLNK(value.st_mode) or bool(
            int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE
        )

    @staticmethod
    def _ensure_regular_directory(path: Path, *, label: str) -> Path:
        value = path.lstat()
        junction = getattr(os.path, "isjunction", lambda candidate: False)
        if (
            stat.S_ISLNK(value.st_mode)
            or bool(int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE)
            or junction(path)
            or not stat.S_ISDIR(value.st_mode)
        ):
            raise OSError(f"{label} is not an isolated regular directory: {path}")
        return path.resolve(strict=True)

    @classmethod
    def _ensure_runtime_directory(cls, path: Path) -> Path:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        return cls._ensure_regular_directory(path, label="Runtime artifact root")

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        first_resolved = first.resolve(strict=True)
        second_resolved = second.resolve(strict=True)
        try:
            first_resolved.relative_to(second_resolved)
            return True
        except ValueError:
            pass
        try:
            second_resolved.relative_to(first_resolved)
            return True
        except ValueError:
            return False

    @classmethod
    def _make_relative_directory(cls, root: Path, relative: str) -> Path:
        if not relative or relative == ".":
            return root
        path = cls._safe_relative_path(relative)
        current = root
        for part in path.parts:
            current = current / part
            if current.exists():
                cls._ensure_regular_directory(current, label="snapshot directory")
            else:
                current.mkdir()
                cls._ensure_regular_directory(current, label="snapshot directory")
        return current

    @classmethod
    def _copy_regular_file(
        cls,
        source: Path,
        destination: Path,
        *,
        source_root: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        cls._ensure_regular_directory(source.parent, label="bound project directory")
        if cls._is_link_or_reparse(source):
            raise AgentToolError(
                f"bound project contains a linked file: {source}",
                code="agent_skill_snapshot_linked",
                retryable=False,
            )
        before = source.stat()
        if int(getattr(before, "st_nlink", 1)) > 1:
            raise AgentToolError(
                f"bound project contains a hardlinked file: {source}",
                code="agent_skill_snapshot_hardlinked",
                retryable=False,
            )
        resolved = source.resolve(strict=True)
        if not resolved.is_file() or not cls._within(resolved, source_root):
            raise AgentToolError(
                "bound project file escaped its trusted root",
                code="agent_skill_snapshot_boundary_failed",
                retryable=False,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        cls._ensure_regular_directory(destination.parent, label="snapshot directory")
        digest = hashlib.sha256()
        bytes_read = 0
        try:
            with resolved.open("rb") as source_handle, destination.open("xb") as target_handle:
                for block in iter(lambda: source_handle.read(1024 * 1024), b""):
                    bytes_read += len(block)
                    if bytes_read > MAX_SKILL_ARTIFACT_BYTES * 64:
                        raise AgentToolError(
                            "bound project snapshot exceeds the copy budget",
                            code="agent_skill_snapshot_too_large",
                            retryable=False,
                        )
                    digest.update(block)
                    target_handle.write(block)
        except FileExistsError as exc:
            raise AgentToolError(
                "isolated project snapshot path collision",
                code="agent_skill_snapshot_invalid",
                retryable=False,
            ) from exc
        after = resolved.stat()
        if (
            bytes_read != expected_size
            or digest.hexdigest() != expected_sha256
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or int(getattr(after, "st_nlink", 1)) > 1
        ):
            raise AgentToolError(
                "bound project changed while its isolated snapshot was copied",
                code="agent_skill_snapshot_changed",
                retryable=True,
            )
        copied = destination.stat()
        if copied.st_size != expected_size or int(getattr(copied, "st_nlink", 1)) > 1:
            raise AgentToolError(
                "isolated project snapshot is not a regular file",
                code="agent_skill_snapshot_invalid",
                retryable=False,
            )

    @classmethod
    def _read_regular_file(
        cls,
        path: Path,
        *,
        root: Path,
        max_bytes: int,
    ) -> bytes:
        cls._reject_linked_ancestors(path, root)
        resolved = path.resolve(strict=True)
        if not cls._within(resolved, root):
            raise AgentToolError(
                "managed Skill artifact escaped its staging root",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        value = resolved.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or cls._is_link_or_reparse(resolved)
            or int(getattr(value, "st_nlink", 1)) > 1
            or value.st_size > max_bytes
        ):
            raise AgentToolError(
                "managed Skill artifact is not a bounded regular file",
                code="agent_skill_artifact_invalid",
                retryable=False,
            )
        content = resolved.read_bytes()
        after = resolved.stat()
        if len(content) != value.st_size or (
            after.st_size != value.st_size
            or after.st_mtime_ns != value.st_mtime_ns
        ):
            raise AgentToolError(
                "managed Skill artifact changed while it was copied",
                code="agent_skill_artifact_changed",
                retryable=False,
            )
        return content

    @classmethod
    def _validate_tree(cls, root: Path, *, label: str) -> None:
        cls._ensure_regular_directory(root, label=label)
        for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_raw)
            cls._ensure_regular_directory(current, label=label)
            for name in (*dir_names, *file_names):
                path = current / name
                value = path.lstat()
                if cls._is_link_or_reparse(path):
                    raise AgentToolError(
                        f"{label} contains a link or reparse point",
                        code="agent_skill_snapshot_linked",
                        retryable=False,
                    )
                if stat.S_ISREG(value.st_mode) and int(getattr(value, "st_nlink", 1)) > 1:
                    raise AgentToolError(
                        f"{label} contains a hardlinked file",
                        code="agent_skill_snapshot_hardlinked",
                        retryable=False,
                    )
                if not stat.S_ISREG(value.st_mode) and not stat.S_ISDIR(value.st_mode):
                    raise AgentToolError(
                        f"{label} contains a non-regular entry",
                        code="agent_skill_snapshot_invalid",
                        retryable=False,
                    )

    @classmethod
    def _make_snapshot_read_only(cls, root: Path) -> None:
        for current_raw, dir_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_raw)
            for name in file_names:
                path = current / name
                path.chmod(stat.S_IREAD)
            current.chmod(stat.S_IREAD | stat.S_IEXEC)

    @classmethod
    def _reject_linked_ancestors(cls, path: Path, root: Path) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise AgentToolError(
                "managed Skill path is outside its execution root",
                code="agent_skill_artifact_invalid",
                retryable=False,
            ) from exc
        current = root
        for part in relative.parts:
            current = current / part
            if cls._is_link_or_reparse(current):
                raise AgentToolError(
                    "managed Skill path contains a link or reparse point",
                    code="agent_skill_artifact_invalid",
                    retryable=False,
                )

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _write_atomic_bytes(cls, target: Path, content: bytes) -> None:
        if target.exists() or target.is_symlink():
            cls._ensure_regular_file(target, label="Runtime artifact")
            if target.read_bytes() != content:
                raise AgentToolError(
                    "Runtime artifact path already contains different bytes",
                    code="agent_skill_artifact_conflict",
                    retryable=False,
                )
            return
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        if temporary.exists():
            cls._remove_isolated_tree(temporary, target.parent)
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                cls._remove_isolated_tree(temporary, target.parent)
        cls._ensure_regular_file(target, label="Runtime artifact")

    @staticmethod
    def _ensure_regular_file(path: Path, *, label: str) -> None:
        value = path.lstat()
        if (
            not stat.S_ISREG(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or bool(int(getattr(value, "st_file_attributes", 0)) & REPARSE_POINT_ATTRIBUTE)
            or int(getattr(value, "st_nlink", 1)) > 1
        ):
            raise OSError(f"{label} is not a regular file: {path}")

    @classmethod
    def _remove_isolated_tree(cls, path: Path, parent: Path) -> None:
        path = Path(path)
        parent = Path(parent).resolve(strict=True)
        absolute = Path(os.path.abspath(path))
        if not cls._within(absolute, parent) or absolute == parent:
            raise OSError("refusing to remove a path outside the Runtime workspace")
        if not path.exists() and not path.is_symlink():
            return
        value = path.lstat()
        if cls._is_link_or_reparse(path):
            if stat.S_ISDIR(value.st_mode):
                os.rmdir(path)
            else:
                path.unlink()
            return
        if stat.S_ISDIR(value.st_mode):
            path.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            for entry in os.scandir(path):
                cls._remove_isolated_tree(Path(entry.path), path)
            path.rmdir()
        else:
            path.chmod(stat.S_IREAD | stat.S_IWRITE)
            path.unlink()

    async def _binding(self, run: Run) -> ProjectBindingRecord:
        binding = await asyncio.to_thread(
            self.store.get_project_binding,
            run.project_revision.project_id,
        )
        if binding is None:
            raise AgentToolError(
                "Run project binding no longer exists",
                code="agent_tool_project_not_found",
                retryable=False,
            )
        if (
            binding.revision != run.project_revision.revision
            or binding.fingerprint != run.project_revision.fingerprint
        ):
            raise AgentToolError(
                "Project changed after this Run was created",
                code="agent_tool_project_changed",
                retryable=True,
            )
        return binding

    async def _current_snapshot(
        self,
        run: Run,
        binding: ProjectBindingRecord,
    ) -> ProjectInspectionResponse:
        observed_at = datetime.now(UTC)
        payload = await asyncio.to_thread(self.reader.inspect, binding.root_path)
        project = payload.get("project")
        if not isinstance(project, dict):
            raise AgentToolError(
                "Project Reader returned an invalid snapshot",
                code="agent_tool_result_invalid",
                retryable=False,
            )
        current = await asyncio.to_thread(
            self.store.sync_project_binding,
            project_id=str(project["project_id"]),
            root_path=str(project["root_path"]),
            display_name=str(project["display_name"]),
            fingerprint=str(project["fingerprint"]),
            observed_at=observed_at,
        )
        if current.fingerprint != project["fingerprint"]:
            raise AgentToolError(
                "A newer project snapshot was committed during tool execution",
                code="agent_tool_project_changed",
                retryable=True,
            )
        if (
            current.project_id != run.project_revision.project_id
            or current.revision != run.project_revision.revision
            or current.fingerprint != run.project_revision.fingerprint
        ):
            raise AgentToolError(
                "Project changed after this Run was created",
                code="agent_tool_project_changed",
                retryable=True,
            )
        project["revision"] = current.revision
        return ProjectInspectionResponse.model_validate(payload)

    @staticmethod
    def _bounded_result(payload: dict[str, Any]) -> AgentToolExecution:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded) > MAX_TOOL_RESULT_CHARACTERS:
            raise AgentToolError(
                "tool result exceeds the Runtime character limit",
                code="agent_tool_result_too_large",
                retryable=False,
            )
        return AgentToolExecution(output=payload)
