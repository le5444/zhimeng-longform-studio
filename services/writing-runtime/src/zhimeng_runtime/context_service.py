from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from zhimeng_runtime.agent_store import (
    AgentStore,
    AgentStoreConflict,
    AgentStoreError,
    AgentStoreInvalidState,
    AgentStoreNotFound,
)
from zhimeng_runtime.memory_files import LEGACY_PROJECT_NOTES_FILE_NAME as BOOK_WRITING_NOTES_FILE_NAME
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.office_reader import extracted_context_text
from zhimeng_runtime.session_checkpoint import render_checkpoint_block

from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextProviderMessage,
    canonical_json_hash,
    fragment_sort_key,
    knowledge_source_fingerprint,
    provider_message_characters,
    provider_message_tokens,
    render_system_context,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreConflict,
    ContextStoreError,
    ContextStoreIntegrity,
    ContextStoreNotFound,
    StoredContextPack,
)
from zhimeng_runtime.database import ProjectBindingRecord
from zhimeng_runtime.distillation_adapter import (
    DistillationAdapterError,
    DistillationPreviewAdapter,
)
from zhimeng_runtime.distillation_models import DistillationCoverageRequest
from zhimeng_runtime.knowledge_service import (
    KnowledgeConflictError,
    KnowledgeContextSelection,
    KnowledgeService,
    KnowledgeServiceError,
)
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_kb_models import ProjectKbSnapshotManifest
from zhimeng_runtime.project_kb_service import (
    ProjectKbService,
    ProjectKbServiceError,
)
from zhimeng_runtime.project_reader import (
    ProjectChangedDuringReadError,
    ProjectFileNotFoundError,
    ProjectReader,
    ProjectReaderError,
    ProjectScanLimitError,
    UnreadableProjectFileError,
    UntrustedProjectPathError,
    WRITING_CONTEXT_FILE_KINDS,
)


DEFAULT_MAX_CHARACTERS = 48_000
DEFAULT_MAX_TOKENS = 24_000
MAX_CONTEXT_CANDIDATES = 256
CORE_SECTION_INDEXES = frozenset({1, 2, 3, 4, 6, 7})
CORE_FILE_KINDS = frozenset(
    {"document", "control", "context", "status", "structured_state", "asset"}
)
CONTEXT_READ_KINDS = WRITING_CONTEXT_FILE_KINDS

# 书里这些文件是软件或早先的 AI 写的记账（总览、修改记录、进度表、写回记录、审稿报告），
# 不是作者定下的设定和正文。2026-09-13 作者反馈：模型把这些文件里的
# `control_card_ready` / `writeback_done` 之类的词当成事实原样说回来。
# 所以它们只进「参考资料」，不再当「作品内容（唯一真值）」喂给模型。
# 只影响之后新编译的 Context Pack；存量 Pack 存着自己的 authority，不受影响。
REFERENCE_PROJECT_FILE_KINDS = frozenset({"structured_state", "handoff", "context", "review"})
REFERENCE_PROJECT_PATH_MARKERS = (
    "项目总览",
    "正文总览",
    "修改记录",
    "进度表",
    "写作日志",
    "体检",
    "写回",
    "同步",
    "gate",
    "门禁",
    "health",
    "健康",
    "writeback",
)


def project_file_authority(record: Mapping[str, object]) -> ContextAuthority:
    """一份书里的文件进上下文时算「作品内容」还是「参考资料」。

    正文、设定、人物、大纲、控制卡是作品内容；记账类文件是参考资料。
    人物状态表这类带「状态」二字的真设定不受影响：只看记账标记，不看 status 这个大类。
    """

    kind = record.get("kind")
    if kind == "prose":
        return ContextAuthority.PROJECT_TRUTH
    if kind in REFERENCE_PROJECT_FILE_KINDS:
        return ContextAuthority.REFERENCE
    relative_path = str(record.get("relative_path", "")).casefold()
    if any(marker.casefold() in relative_path for marker in REFERENCE_PROJECT_PATH_MARKERS):
        return ContextAuthority.REFERENCE
    return ContextAuthority.PROJECT_TRUTH
CHAPTER_MATERIAL_PRIMARY_KINDS = frozenset(
    {"control", "document", "context", "structured_state"}
)
CHAPTER_MATERIAL_KIND_ORDER = {
    "control": 0,
    "document": 1,
    "context": 2,
    "structured_state": 3,
    "review": 4,
}

# Objective relevance filtering (candidate layer only; the compiler's greedy
# budget loop is untouched). Core-section files unrelated to the author's
# objective are never deleted and never lose eligibility unless the objective
# is a short, chat-like message — they are demoted so they only fill leftover
# budget. Mandatory candidates are never affected.
OBJECTIVE_UNRELATED_SECTION_PRIORITY = 320
LIGHT_OBJECTIVE_MAX_COMPACT_LENGTH = 12
OUTLINE_SECTION_INDEX = 4

_FULLWIDTH_DIGIT_TRANSLATION = str.maketrans("０１２３４５６７８９", "0123456789")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}
_CHAPTER_WORD_PATTERN = re.compile(
    r"第\s*([0-9０-９一二两三四五六七八九零〇十百千]{1,8})\s*章"
)
_CHAPTER_CODE_PATTERN = re.compile(r"ch\s*0*(\d{1,6})", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_PATH_TOKEN_SPLIT_PATTERN = re.compile(r"[_\-—–\s()（）\[\]【】、，,。.·/\\]+")


def _chinese_number_to_int(text: str) -> int | None:
    if not text:
        return None
    text = text.translate(_FULLWIDTH_DIGIT_TRANSLATION)
    if text.isdigit():
        return int(text)
    total = 0
    number = 0
    for char in text:
        digit = _CHINESE_DIGITS.get(char)
        if digit is not None:
            number = digit
            continue
        unit = _CHINESE_UNITS.get(char)
        if unit is None:
            return None
        total += (number or 1) * unit
        number = 0
    return total + number if total + number > 0 else None


def _chapter_numbers_in_text(text: str) -> frozenset[int]:
    numbers: set[int] = set()
    for match in _CHAPTER_WORD_PATTERN.finditer(text):
        number = _chinese_number_to_int(match.group(1))
        if number is not None and number >= 1:
            numbers.add(number)
    for match in _CHAPTER_CODE_PATTERN.finditer(text):
        number = int(match.group(1))
        if number >= 1:
            numbers.add(number)
    return frozenset(numbers)


def _path_match_tokens(relative_path: str) -> frozenset[str]:
    tokens: set[str] = set()
    for segment in _PATH_TOKEN_SPLIT_PATTERN.split(relative_path.casefold()):
        segment = segment.strip().lstrip("0123456789")
        if len(segment) < 2 or segment.isdigit():
            continue
        # Latin words need more length to stay meaningful ("md" / "ch" are noise).
        if segment.isascii() and len(segment) < 3:
            continue
        tokens.add(segment)
    tokens.discard("")
    return frozenset(tokens)


class _ObjectiveSignals:
    __slots__ = ("chapter_numbers", "light", "text")

    def __init__(self, objective: str) -> None:
        self.text = objective.casefold()
        self.chapter_numbers = _chapter_numbers_in_text(objective)
        compact = _WHITESPACE_PATTERN.sub("", objective)
        self.light = len(compact) <= LIGHT_OBJECTIVE_MAX_COMPACT_LENGTH

    def matches(self, relative_path: str, relevant_chapters: frozenset[int]) -> bool:
        if _chapter_numbers_in_text(relative_path) & relevant_chapters:
            return True
        return any(token in self.text for token in _path_match_tokens(relative_path))


def _objective_signals(objective: str) -> _ObjectiveSignals:
    return _ObjectiveSignals(objective)


class ContextServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "context_unavailable",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ContextNotFoundError(ContextServiceError):
    pass


class ContextConflictError(ContextServiceError):
    pass


class ContextUnavailableError(ContextServiceError):
    pass


class ContextService:
    def __init__(
        self,
        store: ContextStore,
        agent_store: AgentStore,
        reader: ProjectReader,
        locks: ProjectLockRegistry,
        distillation_adapter: DistillationPreviewAdapter | None = None,
        native_knowledge: KnowledgeService | None = None,
        project_kb_service: ProjectKbService | None = None,
        *,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        budget_resolver: Callable[[], tuple[int, int]] | None = None,
    ) -> None:
        if isinstance(max_characters, bool) or max_characters < 1:
            raise ValueError("max_characters must be a positive integer")
        if isinstance(max_tokens, bool) or max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        self.store = store
        self.agent_store = agent_store
        self.reader = reader
        self.locks = locks
        self.distillation = distillation_adapter
        self.native_knowledge = native_knowledge
        self.project_kb_service = project_kb_service
        self.max_characters = max_characters
        self.max_tokens = max_tokens
        # 档位预算解析器：接线后每次装箱前问一次当前档位（设置页改了立刻生效），
        # 没接线就用构造时的固定值。
        self._budget_resolver = budget_resolver
        # 构造时的预算：之后有人显式改写 max_characters/max_tokens（测试常用这个
        # 手法压出一个超预算包），显式改写优先于档位解析。
        self._constructor_budget = (max_characters, max_tokens)

    def _current_budget(self) -> tuple[int, int]:
        if (self.max_characters, self.max_tokens) != self._constructor_budget:
            return self.max_characters, self.max_tokens
        if self._budget_resolver is not None:
            try:
                return self._budget_resolver()
            except Exception:
                pass
        return self.max_characters, self.max_tokens

    async def create_pack(
        self,
        session_id: str,
        request: ContextCreateRequest,
        *,
        idempotency_key: str,
    ) -> StoredContextPack:
        if not idempotency_key:
            raise ContextConflictError("Idempotency key must not be blank")
        context_pack_id = self._context_pack_id(session_id, idempotency_key)
        _, binding = await asyncio.to_thread(
            self._resolve_database_truth,
            session_id,
        )
        await asyncio.to_thread(
            self._require_project_kb_binding,
            binding.project_id,
            request,
        )
        replay = await asyncio.to_thread(
            self._matching_replay,
            context_pack_id,
            session_id,
            request,
        )
        if replay is not None:
            await asyncio.to_thread(
                self._require_project_kb_binding,
                binding.project_id,
                request,
            )
            return replay
        try:
            root = await asyncio.to_thread(self.reader.resolve, binding.root_path)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc

        (
            knowledge_candidates,
            knowledge_fingerprint,
            route_source,
            route_cards,
        ) = await asyncio.to_thread(
            self._knowledge_candidates,
            request,
            binding.project_id,
        )
        await asyncio.to_thread(
            self._require_project_kb_binding,
            binding.project_id,
            request,
            expected_knowledge_fingerprint=(
                knowledge_fingerprint
                if route_source is NlclRouteSource.NATIVE_KNOWLEDGE
                else None
            ),
        )

        async with self.locks.for_path(root):
            return await asyncio.to_thread(
                self._create_pack_locked,
                session_id,
                request,
                context_pack_id,
                root,
                knowledge_candidates,
                knowledge_fingerprint,
                route_source,
                route_cards,
            )

    def get_pack(self, context_pack_id: str) -> StoredContextPack:
        try:
            return self.store.get_pack(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_nlcl_route(self, context_pack_id: str):
        try:
            return self.store.get_nlcl_route(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_prewrite_operation_package(self, context_pack_id: str):
        try:
            return self.store.get_prewrite_operation_package(context_pack_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_route_execution_state(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int = 1,
    ):
        try:
            return self.store.get_route_execution_state(
                context_pack_id,
                call_ordinal=call_ordinal,
            )
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def list_session_packs(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[StoredContextPack]:
        try:
            return self.store.list_session_packs(session_id, limit=limit)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def get_run_pack(self, run_id: str) -> StoredContextPack:
        try:
            return self.store.get_run_pack(run_id)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc

    def _create_pack_locked(
        self,
        session_id: str,
        request: ContextCreateRequest,
        context_pack_id: str,
        locked_root: Path,
        knowledge_candidates: Sequence[ContextCandidateFragment],
        knowledge_fingerprint: str | None,
        route_source: NlclRouteSource,
        route_cards: Sequence[NlclSelectedCard],
    ) -> StoredContextPack:
        session, binding = self._resolve_database_truth(session_id)
        expected_native_fingerprint = (
            knowledge_fingerprint
            if route_source is NlclRouteSource.NATIVE_KNOWLEDGE
            else None
        )
        self._require_project_kb_binding(
            binding.project_id,
            request,
            expected_knowledge_fingerprint=expected_native_fingerprint,
        )
        try:
            current_root = self.reader.resolve(binding.root_path)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc
        if current_root != locked_root:
            raise ContextConflictError(
                "Project binding changed before Context compilation",
                retryable=True,
            )
        self._verify_request_revision(request, binding)

        try:
            initial = self.reader.inspect(locked_root)
        except ProjectReaderError as exc:
            raise self._project_error(exc) from exc
        self._verify_snapshot(initial, binding, request)

        history_through_ordinal = 0
        history: Sequence[Any] = ()
        candidates: list[ContextCandidateFragment] = []
        pending_error: Exception | None = None
        include_history = (
            request.include_session_history
            or request.profile.startswith("chat")
            or request.profile == "chapter_with_history"
        )
        try:
            session_checkpoint = self.agent_store.latest_session_checkpoint(session_id) if include_history else None
            folded_through = 0 if session_checkpoint is None else session_checkpoint.through_ordinal
            pack_max_characters, pack_max_tokens = self._current_budget()
            if include_history:
                base_messages = (
                    ContextProviderMessage(
                        role="system",
                        content=render_system_context(
                            project_id=session.project_revision.project_id,
                            project_revision=request.expected_project_revision,
                            project_fingerprint=request.expected_project_fingerprint,
                            project_kb_state_id=request.project_kb_state_id,
                            project_kb_manifest_hash=request.project_kb_manifest_hash,
                            knowledge_fingerprint=knowledge_fingerprint,
                            profile=request.profile,
                            active_chapter_id=request.active_chapter_id,
                            included=(),
                        ),
                    ),
                    ContextProviderMessage(role="user", content=request.objective),
                )
                history_character_budget = min(
                    pack_max_characters // 4,
                    max(
                        0,
                        pack_max_characters
                        - provider_message_characters(base_messages),
                    ),
                )
                history_token_budget = min(
                    pack_max_tokens // 4,
                    max(0, pack_max_tokens - provider_message_tokens(base_messages)),
                )
                if history_character_budget > 0 and history_token_budget > 0:
                    history_through_ordinal, history = (
                        self.agent_store.completed_history(
                            session_id,
                            max_runs=8,
                            after_ordinal=folded_through,
                            max_characters=history_character_budget,
                            max_tokens=history_token_budget,
                        )
                    )
                else:
                    history_through_ordinal = self.agent_store.get_session(
                        session_id
                    ).last_message_ordinal
            else:
                history_through_ordinal = self.agent_store.get_session(
                    session_id
                ).last_message_ordinal
            candidates.extend(self._project_candidates(initial, request))
            candidates.extend(knowledge_candidates)
            if session_checkpoint is not None:
                candidates.append(
                    ContextCandidateFragment(
                        source_id="session:checkpoint",
                        source_type="session_summary",
                        title="前序对话历史摘要",
                        authority=ContextAuthority.REFERENCE,
                        version=session_checkpoint.checkpoint_id,
                        source_hash=session_checkpoint.content_hash,
                        content=render_checkpoint_block(
                            through_ordinal=session_checkpoint.through_ordinal,
                            content=session_checkpoint.content,
                        ),
                        priority=95,
                        mandatory=True,
                        eligible=True,
                    )
                )
            if include_history:
                for message in self.agent_store.fork_context_messages(session_id, after_ordinal=folded_through):
                    candidates.append(ContextCandidateFragment(
                        source_id=f"session:fork-message:{message.message_id}",
                        source_type="session_fork_author_message",
                        title="分支带入的原始作者消息",
                        authority=ContextAuthority.REFERENCE,
                        version=message.message_id,
                        source_hash=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
                        content=("以下是分支位置的作者原话，原对话在该位置尚无回答；"
                                 "这是历史资料，当前请求优先。\n" + message.content),
                        priority=96, mandatory=True, eligible=True,
                    ))
        except Exception as exc:
            pending_error = exc

        final: Mapping[str, object] | None = None
        final_error: Exception | None = None
        try:
            final = self.reader.inspect(locked_root)
        except Exception as exc:
            final_error = exc

        if final_error is not None:
            if isinstance(final_error, ProjectReaderError):
                raise self._project_error(final_error) from final_error
            raise ContextUnavailableError(
                "Final project inspection failed during Context compilation",
                retryable=True,
            ) from final_error
        if final is None:  # pragma: no cover - guarded by the branch above.
            raise ContextUnavailableError("Final project inspection produced no snapshot")
        self._verify_final_snapshot(initial, final)

        if pending_error is not None:
            if isinstance(pending_error, ContextServiceError):
                raise pending_error
            if isinstance(pending_error, ProjectReaderError):
                raise self._project_error(pending_error) from pending_error
            if isinstance(pending_error, AgentStoreError):
                raise self._agent_error(pending_error) from pending_error
            raise ContextUnavailableError(
                "Context candidates could not be assembled",
                retryable=True,
            ) from pending_error

        candidates = self._bounded_candidates(candidates)
        try:
            self._require_project_kb_binding(
                binding.project_id,
                request,
                expected_knowledge_fingerprint=expected_native_fingerprint,
            )
            pack = compile_context_pack(
                context_pack_id=context_pack_id,
                session_id=session_id,
                project_id=session.project_revision.project_id,
                history_through_ordinal=history_through_ordinal,
                request=request,
                history=history,
                candidates=candidates,
                max_characters=pack_max_characters,
                max_tokens=pack_max_tokens,
                max_fragments=MAX_CONTEXT_CANDIDATES,
                created_at=datetime.now(UTC),
                knowledge_fingerprint=knowledge_fingerprint,
            )
            self._require_project_kb_binding(
                binding.project_id,
                request,
                expected_knowledge_fingerprint=expected_native_fingerprint,
            )
            # NLCL 路由回执不再为每条对话白建（受管流水线已退役，没人消费它）；
            # 存储层自带的占位回执足够让老的读取路径不报错。
            del route_source, route_cards
            return self.store.create_pack(pack)
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextUnavailableError(
                "Context compiler rejected the assembled inputs"
            ) from exc

    def _matching_replay(
        self,
        context_pack_id: str,
        session_id: str,
        request: ContextCreateRequest,
    ) -> StoredContextPack | None:
        try:
            stored = self.store.get_pack(context_pack_id)
        except ContextStoreNotFound:
            return None
        except ContextStoreError as exc:
            raise self._store_error(exc) from exc
        pack = stored.pack
        if (
            pack.session_id == session_id
            and pack.objective == request.objective
            and pack.profile == request.profile
            and pack.active_chapter_id == request.active_chapter_id
            and pack.project_revision == request.expected_project_revision
            and pack.project_fingerprint == request.expected_project_fingerprint
            and pack.project_kb_state_id == request.project_kb_state_id
            and pack.project_kb_manifest_hash == request.project_kb_manifest_hash
            and pack.formal_knowledge_selector == request.formal_knowledge_selector
            and pack.manual_file_ids == request.manual_file_ids
            and pack.include_session_history == request.include_session_history
        ):
            return stored
        return None

    def _require_project_kb_binding(
        self,
        project_id: str,
        request: ContextCreateRequest,
        *,
        expected_knowledge_fingerprint: str | None = None,
    ) -> ProjectKbSnapshotManifest | None:
        """核对这本书的知识库绑定；没有绑定就当「没有额外知识」继续建包。

        以前缺绑定直接 503，新书或刚放进书架的老书一句话都聊不了（2026-09-22
        审阅 A1）。作者 9-18 已定「一书一库」是默认模板不是准入条件。
        绑定存在但和请求对不上（被重建过）仍报冲突，前端据此重绑。
        """
        service = self.project_kb_service
        if service is None or request.project_kb_state_id is None:
            # 普通对话的资料包不绑知识库快照：没有要核对的，也就不用为核对整本书再扫一遍。
            return None
        try:
            return service.require_current_binding(
                project_id,
                project_kb_state_id=request.project_kb_state_id,
                project_kb_manifest_hash=request.project_kb_manifest_hash,
                expected_knowledge_fingerprint=expected_knowledge_fingerprint,
            )
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            code = str(detail.get("code") or "project_kb_unavailable")
            if code == "project_kb_state_missing":
                return None
            raise ContextConflictError(
                str(exc),
                code=code,
                retryable=exc.retryable,
            ) from exc

    @staticmethod
    def _bounded_candidates(
        candidates: Sequence[ContextCandidateFragment],
    ) -> list[ContextCandidateFragment]:
        ordered = sorted(candidates, key=fragment_sort_key)
        if len(ordered) <= MAX_CONTEXT_CANDIDATES:
            return ordered

        retained = ordered[: MAX_CONTEXT_CANDIDATES - 1]
        omitted = ordered[MAX_CONTEXT_CANDIDATES - 1 :]
        omitted_mandatory = any(item.mandatory for item in omitted)
        reason = (
            "mandatory_candidate_limit_exceeded"
            if omitted_mandatory
            else "candidate_limit_exceeded"
        )
        retained.append(
            ContextCandidateFragment(
                source_id="context:candidate-limit",
                source_type="context_selection_limit",
                title=f"{len(omitted)} context candidates omitted by the hard limit",
                authority=(
                    ContextAuthority.PROJECT_TRUTH
                    if omitted_mandatory
                    else ContextAuthority.REFERENCE
                ),
                version="context-selection-v1",
                source_hash=canonical_json_hash(
                    [
                        {
                            "source_id": item.source_id,
                            "source_hash": item.source_hash,
                            "mandatory": item.mandatory,
                        }
                        for item in omitted
                    ]
                ),
                content="Additional Context candidates were omitted by the bounded compiler input.",
                priority=0,
                mandatory=omitted_mandatory,
                eligible=False,
                exclusion_reason=reason,
            )
        )
        return retained

    def _resolve_database_truth(
        self,
        session_id: str,
    ) -> tuple[Any, ProjectBindingRecord]:
        try:
            session = self.agent_store.get_session(session_id)
        except AgentStoreError as exc:
            raise self._agent_error(exc) from exc
        project_id = session.project_revision.project_id
        binding = self.agent_store.database.get_project_binding(project_id)
        if binding is None:
            raise ContextNotFoundError("Context project binding was not found")
        return session, binding

    @staticmethod
    def _verify_request_revision(
        request: ContextCreateRequest,
        binding: ProjectBindingRecord,
    ) -> None:
        if (
            request.expected_project_revision != binding.revision
            or request.expected_project_fingerprint != binding.fingerprint
        ):
            raise ContextConflictError(
                "Project revision changed; refresh before compiling Context",
                retryable=True,
            )

    @staticmethod
    def _snapshot_project(snapshot: Mapping[str, object]) -> Mapping[str, object]:
        project = snapshot.get("project")
        if not isinstance(project, Mapping):
            raise ContextUnavailableError("Project Reader returned an invalid snapshot")
        return project

    def _verify_snapshot(
        self,
        snapshot: Mapping[str, object],
        binding: ProjectBindingRecord,
        request: ContextCreateRequest,
    ) -> None:
        project = self._snapshot_project(snapshot)
        if project.get("project_id") != binding.project_id:
            raise ContextConflictError(
                "Inspected project identity does not match the session binding"
            )
        if project.get("fingerprint") != request.expected_project_fingerprint:
            raise ContextConflictError(
                "Project files changed after the requested revision",
                retryable=True,
            )

    def _verify_final_snapshot(
        self,
        initial: Mapping[str, object],
        final: Mapping[str, object],
    ) -> None:
        initial_project = self._snapshot_project(initial)
        final_project = self._snapshot_project(final)
        if (
            final_project.get("project_id") != initial_project.get("project_id")
            or final_project.get("fingerprint") != initial_project.get("fingerprint")
        ):
            raise ContextConflictError(
                "Project snapshot changed while Context was being compiled",
                retryable=True,
            )

    def _project_candidates(
        self,
        snapshot: Mapping[str, object],
        request: ContextCreateRequest,
    ) -> list[ContextCandidateFragment]:
        raw_files = snapshot.get("files")
        raw_chapters = snapshot.get("chapters")
        if not isinstance(raw_files, list) or not isinstance(raw_chapters, list):
            raise ContextUnavailableError("Project snapshot is missing files or chapters")
        files = [item for item in raw_files if isinstance(item, Mapping)]
        chapters = [item for item in raw_chapters if isinstance(item, Mapping)]
        files_by_id = {
            str(item["file_id"]): item
            for item in files
            if isinstance(item.get("file_id"), str)
        }
        candidates: list[ContextCandidateFragment] = []
        selected_file_ids: set[str] = set()

        for file_id in request.manual_file_ids:
            record = files_by_id.get(file_id)
            if record is None:
                raise ContextConflictError(
                    "A manually selected file is no longer in the project snapshot",
                    retryable=True,
                )
            if record.get("kind") not in CONTEXT_READ_KINDS:
                raise ContextConflictError(
                    "A manually selected file cannot be used as writing context"
                )
            selected_file_ids.add(file_id)
            candidates.append(
                self._read_project_candidate(
                    snapshot,
                    record,
                    source_type="manual_project_file",
                    priority=995,
                    mandatory=True,
                    project_revision=request.expected_project_revision,
                )
            )

        # 书根《写作笔记.md》是这本书的跨对话记忆：存在就作为必带候选装进
        # 资料包（优先级 990，排在手选文件之后）；不存在、空白或读不出就
        # 不装、不报错，也不让它再走后面的普通分区候选。
        notes_record = next(
            (
                record
                for record in files
                if str(record.get("relative_path", "")).replace("\\", "/")
                == BOOK_WRITING_NOTES_FILE_NAME
            ),
            None,
        )
        if notes_record is not None:
            notes_file_id = notes_record.get("file_id")
            if isinstance(notes_file_id, str) and notes_file_id not in selected_file_ids:
                selected_file_ids.add(notes_file_id)
                notes_candidate = self._read_project_candidate(
                    snapshot,
                    notes_record,
                    source_type="project_writing_notes",
                    priority=990,
                    mandatory=True,
                    project_revision=request.expected_project_revision,
                )
                if notes_candidate.eligible:
                    candidates.append(notes_candidate)

        if request.active_chapter_id is not None:
            active_index = next(
                (
                    index
                    for index, chapter in enumerate(chapters)
                    if chapter.get("chapter_id") == request.active_chapter_id
                ),
                None,
            )
            if active_index is None:
                candidates.append(
                    self._unavailable_chapter_candidate(
                        request,
                        request.active_chapter_id,
                        mandatory=True,
                        reason="active_chapter_not_found",
                        priority=1_000,
                    )
                )
            else:
                chapter_targets = [
                    (chapters[active_index], True, 1_000, "active_chapter"),
                    *[
                        (chapters[index], False, priority, "previous_chapter")
                        for index, priority in zip(
                            range(active_index - 1, max(-1, active_index - 3), -1),
                            (950, 900),
                            strict=False,
                        )
                    ],
                ]
                for chapter, mandatory, priority, source_type in chapter_targets:
                    chapter_id = str(chapter.get("chapter_id", "unknown"))
                    prose_ids = chapter.get("prose_file_ids")
                    if not isinstance(prose_ids, list) or not prose_ids:
                        materials = self._chapter_material_candidates(
                            snapshot,
                            chapter,
                            files_by_id,
                            source_type=f"{source_type}_material",
                            priority=priority,
                            mandatory=mandatory,
                            project_revision=request.expected_project_revision,
                            selected_file_ids=selected_file_ids,
                        )
                        candidates.extend(materials)
                        if not any(item.eligible for item in materials):
                            candidates.append(
                                self._unavailable_chapter_candidate(
                                    request,
                                    chapter_id,
                                    mandatory=mandatory,
                                    reason=(
                                        "active_chapter_material_not_found"
                                        if mandatory
                                        else "previous_chapter_material_not_found"
                                    ),
                                    priority=priority,
                                )
                            )
                        continue
                    for raw_file_id in prose_ids:
                        if (
                            not isinstance(raw_file_id, str)
                            or raw_file_id in selected_file_ids
                        ):
                            continue
                        record = files_by_id.get(raw_file_id)
                        if record is None:
                            raise ContextConflictError(
                                "Chapter references a file outside its project snapshot",
                                retryable=True,
                            )
                        selected_file_ids.add(raw_file_id)
                        candidates.append(
                            self._read_project_candidate(
                                snapshot,
                                record,
                                source_type=source_type,
                                priority=priority,
                                mandatory=mandatory,
                                project_revision=request.expected_project_revision,
                            )
                        )

        section_priorities = {1: 760, 2: 770, 3: 800, 4: 790, 6: 750, 7: 720}
        # 作者自命名夹子（非 0N_ 前缀，section_index=None）与书根文件和
        # 默认模板分区同权参选：布局是作者自己的事，不因名字进不了上下文。
        unsectioned_priority = 700
        signals = _objective_signals(request.objective)
        relevant_chapters = set(signals.chapter_numbers)
        for number in signals.chapter_numbers:
            relevant_chapters.update((number - 1, number + 1))
        if request.active_chapter_id is not None:
            for number in _chapter_numbers_in_text(request.active_chapter_id):
                relevant_chapters.update((number - 1, number, number + 1))
        relevant_chapters.discard(0)
        for record in files:
            file_id = record.get("file_id")
            section_index = record.get("section_index")
            kind = record.get("kind")
            if (
                not isinstance(file_id, str)
                or file_id in selected_file_ids
                or (section_index is not None and section_index not in CORE_SECTION_INDEXES)
                or kind not in CORE_FILE_KINDS
            ):
                continue
            priority = (
                unsectioned_priority
                if section_index is None
                else section_priorities[int(section_index)]
            )
            eligible = True
            exclusion_reason = None
            relative_path = record.get("relative_path")
            related = signals.matches(
                str(relative_path) if isinstance(relative_path, str) else "",
                frozenset(relevant_chapters),
            )
            if not related and (
                signals.chapter_numbers
                and section_index is not None
                and int(section_index) == OUTLINE_SECTION_INDEX
            ):
                # Chapter outlines span every chapter and cannot be located by
                # filename; keep them whenever the objective names a chapter.
                related = True
            if not related:
                if signals.light:
                    # Short, chat-like objectives get the minimal set: mandatory
                    # chapters and hand-picked files only. The file stays
                    # observable through the excluded list and the gate feed.
                    eligible = False
                    exclusion_reason = "objective_light_unrelated_section"
                priority = min(priority, OBJECTIVE_UNRELATED_SECTION_PRIORITY)
            candidates.append(
                self._read_project_candidate(
                    snapshot,
                    record,
                    source_type="project_section",
                    priority=priority,
                    mandatory=False,
                    project_revision=request.expected_project_revision,
                )
                if eligible
                else self._unavailable_file_candidate(
                    record,
                    source_type="project_section",
                    priority=priority,
                    mandatory=False,
                    reason=exclusion_reason or "objective_light_unrelated_section",
                )
            )
        return candidates

    def _chapter_material_candidates(
        self,
        snapshot: Mapping[str, object],
        chapter: Mapping[str, object],
        files_by_id: Mapping[str, Mapping[str, object]],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        project_revision: int,
        selected_file_ids: set[str],
    ) -> list[ContextCandidateFragment]:
        raw_file_ids = chapter.get("file_ids")
        if not isinstance(raw_file_ids, list):
            return []

        records: list[Mapping[str, object]] = []
        seen_chapter_ids: set[str] = set()
        for raw_file_id in raw_file_ids:
            if (
                not isinstance(raw_file_id, str)
                or raw_file_id in seen_chapter_ids
                or raw_file_id in selected_file_ids
            ):
                continue
            seen_chapter_ids.add(raw_file_id)
            record = files_by_id.get(raw_file_id)
            if record is None:
                raise ContextConflictError(
                    "Chapter references a file outside its project snapshot",
                    retryable=True,
                )
            if record.get("kind") in CHAPTER_MATERIAL_KIND_ORDER:
                records.append(record)

        records.sort(
            key=lambda item: (
                CHAPTER_MATERIAL_KIND_ORDER[str(item.get("kind"))],
                str(item.get("relative_path", "")).casefold(),
                str(item.get("file_id", "")),
            )
        )
        primary_records = [
            item
            for item in records
            if item.get("kind") in CHAPTER_MATERIAL_PRIMARY_KINDS
        ]
        review_records = [item for item in records if item.get("kind") == "review"]

        candidates: list[ContextCandidateFragment] = []

        def read_records(items: Sequence[Mapping[str, object]]) -> None:
            for record in items:
                file_id = str(record["file_id"])
                if file_id in selected_file_ids:
                    continue
                selected_file_ids.add(file_id)
                candidates.append(
                    self._read_project_candidate(
                        snapshot,
                        record,
                        source_type=source_type,
                        priority=priority,
                        mandatory=False,
                        project_revision=project_revision,
                    )
                )

        read_records(primary_records)
        if not any(item.eligible for item in candidates):
            read_records(review_records)

        if mandatory:
            primary_index = next(
                (index for index, item in enumerate(candidates) if item.eligible),
                None,
            )
            if primary_index is not None:
                values = candidates[primary_index].model_dump(mode="python")
                values["mandatory"] = True
                candidates[primary_index] = ContextCandidateFragment.model_validate(
                    values
                )
        return candidates

    def _read_project_candidate(
        self,
        snapshot: Mapping[str, object],
        record: Mapping[str, object],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        project_revision: int,
    ) -> ContextCandidateFragment:
        file_id = record.get("file_id")
        relative_path = record.get("relative_path")
        source_hash = record.get("sha256")
        if (
            not isinstance(file_id, str)
            or not isinstance(relative_path, str)
            or not isinstance(source_hash, str)
        ):
            raise ContextUnavailableError("Project snapshot contains an invalid file record")
        try:
            payload = self.reader.read_text_file_from_snapshot(
                snapshot,
                file_id,
                allowed_kinds=CONTEXT_READ_KINDS,
            )
            content = payload.get("content")
            extraction = payload.get("extraction")
            if isinstance(content, str):
                content = extracted_context_text(content, extraction)
            if not isinstance(content, str) or not content.strip():
                return self._unavailable_file_candidate(
                    record,
                    source_type=source_type,
                    priority=priority,
                    mandatory=mandatory,
                    reason="project_file_blank",
                )
            return ContextCandidateFragment(
                source_id=f"project-file:{file_id}",
                source_type=source_type,
                title=relative_path[:500],
                authority=project_file_authority(record),
                version=f"project-revision-{project_revision}",
                source_hash=source_hash,
                content=content,
                priority=priority,
                mandatory=mandatory,
                eligible=True,
                exclusion_reason=None,
            )
        except (UnreadableProjectFileError, ProjectScanLimitError):
            return self._unavailable_file_candidate(
                record,
                source_type=source_type,
                priority=priority,
                mandatory=mandatory,
                reason="project_file_unreadable",
            )

    def _unavailable_file_candidate(
        self,
        record: Mapping[str, object],
        *,
        source_type: str,
        priority: int,
        mandatory: bool,
        reason: str,
    ) -> ContextCandidateFragment:
        file_id = str(record.get("file_id", "unknown"))
        relative_path = str(record.get("relative_path", file_id))
        source_hash = str(record.get("sha256", ""))
        if len(source_hash) != 64:
            source_hash = canonical_json_hash(
                {"file_id": file_id, "relative_path": relative_path, "reason": reason}
            )
        return ContextCandidateFragment(
            source_id=f"project-file:{file_id}",
            source_type=source_type,
            title=relative_path[:500],
            authority=project_file_authority(record),
            version="project-file-unavailable",
            source_hash=source_hash,
            content="Project file was unavailable for this Context Pack.",
            priority=priority,
            mandatory=mandatory,
            eligible=False,
            exclusion_reason=reason,
        )

    @staticmethod
    def _unavailable_chapter_candidate(
        request: ContextCreateRequest,
        chapter_id: str,
        *,
        mandatory: bool,
        reason: str,
        priority: int,
    ) -> ContextCandidateFragment:
        return ContextCandidateFragment(
            source_id=f"project-chapter:{chapter_id}",
            source_type="active_chapter" if mandatory else "previous_chapter",
            title=chapter_id[:500],
            authority=ContextAuthority.PROJECT_TRUTH,
            version=f"project-revision-{request.expected_project_revision}",
            source_hash=canonical_json_hash(
                {
                    "chapter_id": chapter_id,
                    "project_fingerprint": request.expected_project_fingerprint,
                    "reason": reason,
                }
            ),
            content="Requested chapter prose was unavailable for this Context Pack.",
            priority=priority,
            mandatory=mandatory,
            eligible=False,
            exclusion_reason=reason,
        )

    def _knowledge_candidates(
        self,
        request: ContextCreateRequest,
        project_id: str | None = None,
    ) -> tuple[
        list[ContextCandidateFragment],
        str | None,
        NlclRouteSource,
        tuple[NlclSelectedCard, ...],
    ]:
        if request.formal_knowledge_selector is not None:
            if self.native_knowledge is None:
                raise ContextUnavailableError(
                    "Native knowledge service is required for an explicit formal batch"
                )
            if project_id is None:
                raise ContextConflictError(
                    "Explicit formal knowledge requires a bound project"
                )
            try:
                selection = self.native_knowledge.select_formalized_batch_for_context(
                    request.formal_knowledge_selector,
                    project_id=project_id,
                )
            except KnowledgeConflictError as exc:
                raise ContextConflictError(str(exc)) from exc
            except KnowledgeServiceError as exc:
                raise ContextUnavailableError(
                    "Explicit formal knowledge could not be verified",
                    retryable=exc.retryable,
                ) from exc
            candidates, route_cards = self._native_selection_material(
                selection,
                mandatory=True,
            )
            return (
                candidates,
                selection.fingerprint,
                NlclRouteSource.NATIVE_KNOWLEDGE,
                route_cards,
            )

        if self.native_knowledge is not None:
            try:
                if self.native_knowledge.has_active_cards(project_id=project_id):
                    query = " ".join(request.objective.split())[:1000]
                    selection = self.native_knowledge.select_for_context(
                        query,
                        request.profile,
                        project_id=project_id,
                    )
                    candidates, route_cards = self._native_selection_material(
                        selection,
                        mandatory=False,
                    )
                    if not candidates:
                        candidates.append(
                            self._unavailable_knowledge_candidate(
                                "native_knowledge_no_matching_cards",
                                version=selection.fingerprint,
                            )
                        )
                    return (
                        candidates,
                        selection.fingerprint,
                        NlclRouteSource.NATIVE_KNOWLEDGE,
                        route_cards,
                    )
            except KnowledgeServiceError:
                return [
                    self._unavailable_knowledge_candidate(
                        "native_knowledge_unavailable"
                    )
                ], None, NlclRouteSource.UNAVAILABLE, ()

        if self.distillation is None:
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )

        try:
            query = " ".join(request.objective.split())[:1000]
            coverage_request = DistillationCoverageRequest(
                query=query,
                profile=request.profile,
            )
            coverage = self.distillation.coverage(coverage_request)
        except (DistillationAdapterError, ValidationError, TypeError, ValueError):
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )
        except Exception:
            # Optional knowledge must fail closed into an excluded fragment, not block
            # otherwise valid project context.
            return (
                [self._unavailable_knowledge_candidate("distillation_unavailable")],
                None,
                NlclRouteSource.UNAVAILABLE,
                (),
            )

        knowledge_fingerprint = knowledge_source_fingerprint(
            library_fingerprint=coverage.library_fingerprint,
            tool_fingerprint=coverage.tool_fingerprint,
        )
        candidates: list[ContextCandidateFragment] = []
        route_cards: list[NlclSelectedCard] = []
        for card in coverage.selected_cards:
            eligible = bool(
                coverage.formal_injection_allowed and card.hard_rule_eligible
            )
            if card.hard_rule_eligible:
                authority = ContextAuthority.FORMAL_KNOWLEDGE
                exclusion_reason = None if eligible else "formal_injection_not_allowed"
            else:
                authority = ContextAuthority.RISK
                exclusion_reason = "knowledge_card_risk_only"
            content = card.summary if card.summary.strip() else card.title
            candidates.append(
                ContextCandidateFragment(
                    source_id=f"knowledge-card:{card.card_id}",
                    source_type=f"distillation_card:{card.card_type}",
                    title=card.title,
                    authority=authority,
                    version=coverage.library_fingerprint,
                    source_hash=canonical_json_hash(card.model_dump(mode="json")),
                    content=content,
                    priority=600,
                    mandatory=False,
                    eligible=eligible,
                    exclusion_reason=exclusion_reason,
                )
            )
            if eligible:
                route_cards.append(
                    NlclSelectedCard(
                        card_id=card.card_id,
                        card_type=str(card.card_type),
                        title=card.title,
                        version_id=coverage.library_fingerprint,
                        content_hash=canonical_json_hash(card.model_dump(mode="json")),
                        route_terms=tuple(dict.fromkeys(card.tags)),
                        recommended_profiles=(request.profile,),
                        negative_routes=(),
                        safety_summary=(
                            "External distillation card passed formal-injection coverage."
                        ),
                        feedback_layer="external_distillation_review",
                    )
                )
        if not candidates:
            candidates.append(
                self._unavailable_knowledge_candidate(
                    "distillation_no_matching_cards",
                    version=knowledge_fingerprint,
                )
            )
        return (
            candidates,
            knowledge_fingerprint,
            NlclRouteSource.EXTERNAL_DISTILLATION,
            tuple(route_cards),
        )

    @staticmethod
    def _native_selection_material(
        selection: KnowledgeContextSelection,
        *,
        mandatory: bool,
    ) -> tuple[list[ContextCandidateFragment], tuple[NlclSelectedCard, ...]]:
        candidates = [
            ContextCandidateFragment(
                source_id=f"knowledge-card:{card.card_id}",
                source_type=f"native_knowledge_card:{card.card_type}",
                title=card.title,
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                version=card.version_id,
                source_hash=card.content_hash,
                content=card.content,
                priority=950 if mandatory else min(900, 600 + card.score),
                mandatory=mandatory,
                eligible=True,
                exclusion_reason=None,
                knowledge_provenance=card.provenance,
            )
            for card in selection.cards
        ]
        route_cards = tuple(
            NlclSelectedCard(
                card_id=card.card_id,
                card_type=card.card_type,
                title=card.title,
                version_id=card.version_id,
                content_hash=card.content_hash,
                route_terms=card.route_terms,
                recommended_profiles=card.recommended_profiles,
                negative_routes=card.negative_routes,
                safety_summary=card.safety_summary,
                feedback_layer=card.feedback_layer,
                knowledge_provenance=card.provenance,
            )
            for card in selection.cards
        )
        return candidates, route_cards

    @staticmethod
    def _unavailable_knowledge_candidate(
        reason: str,
        *,
        version: str = "unavailable",
    ) -> ContextCandidateFragment:
        return ContextCandidateFragment(
            source_id="knowledge:distillation",
            source_type="distillation_coverage",
            title="Distillation knowledge coverage",
            authority=ContextAuthority.FORMAL_KNOWLEDGE,
            version=version,
            source_hash=canonical_json_hash({"reason": reason, "version": version}),
            content="Formal distillation knowledge was unavailable for this Context Pack.",
            priority=500,
            mandatory=False,
            eligible=False,
            exclusion_reason=reason,
        )

    @staticmethod
    def _context_pack_id(session_id: str, idempotency_key: str) -> str:
        digest = hashlib.sha256(
            f"{session_id}\x00{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return f"context-pack-{digest[:32]}"

    @staticmethod
    def _agent_error(exc: AgentStoreError) -> ContextServiceError:
        if isinstance(exc, AgentStoreNotFound):
            return ContextNotFoundError(str(exc))
        if isinstance(exc, (AgentStoreConflict, AgentStoreInvalidState)):
            return ContextConflictError(str(exc), retryable=True)
        return ContextUnavailableError(str(exc), retryable=True)

    @staticmethod
    def _store_error(exc: ContextStoreError) -> ContextServiceError:
        if isinstance(exc, ContextStoreNotFound):
            return ContextNotFoundError(str(exc))
        if isinstance(exc, ContextStoreConflict):
            return ContextConflictError(str(exc))
        if isinstance(exc, ContextStoreIntegrity):
            return ContextUnavailableError(str(exc))
        return ContextUnavailableError(str(exc), retryable=True)

    @staticmethod
    def _project_error(exc: ProjectReaderError) -> ContextServiceError:
        if isinstance(exc, ProjectChangedDuringReadError):
            message = "Project changed during Context compilation"
            return ContextConflictError(message, retryable=True)
        if isinstance(exc, ProjectFileNotFoundError):
            message = "A project file disappeared during Context compilation"
            return ContextConflictError(message, retryable=True)
        if isinstance(exc, UntrustedProjectPathError):
            message = "Project trust boundary rejected Context compilation"
            return ContextConflictError(message, retryable=False)
        return ContextUnavailableError(
            "Project could not be read for Context compilation",
            retryable=True,
        )


__all__ = [
    "ContextConflictError",
    "ContextNotFoundError",
    "ContextService",
    "ContextServiceError",
    "ContextUnavailableError",
    "DEFAULT_MAX_CHARACTERS",
    "DEFAULT_MAX_TOKENS",
    "MAX_CONTEXT_CANDIDATES",
]
