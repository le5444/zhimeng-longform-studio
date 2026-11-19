from __future__ import annotations

from zhimeng_runtime.agent_images import hydrate_images, provider_image_message

import asyncio
from functools import partial
import itertools
import hashlib
import json
import os
import re
import sqlite3
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any, Iterator, Protocol, TypeVar
from uuid import uuid4

from zhimeng_runtime.agent_broker import AgentEventBroker
from zhimeng_runtime.agent_transcript import repair_tool_pairs, tool_result_message
from zhimeng_runtime.run_compaction import compact_tool_results
from zhimeng_runtime.session_goal import GOAL_CONTEXT_PREFIX, GoalAdmission, goal_context
from zhimeng_runtime.agent_candidate_models import AgentCandidateSubmission
from zhimeng_runtime.agent_models import (
    AgentMessageResponse,
    AgentRunResumeRequest,
    AgentRunResumeResponse,
    AgentStatusResponse,
    AgentWorkflowStateResponse,
)
from zhimeng_runtime.agent_provider import (
    PROMPT_CACHE_KEY,
    AgentProvider,
    AgentProviderCancelled,
    AgentProviderError,
    ProviderCompleted,
    ProviderReasoningDelta,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.agent_store import (
    TERMINAL_RUN_STATUSES,
    AgentContextPackConflict,
    AgentContextPackNotFound,
    AgentContextPackSourceUnavailable,
    AgentStore,
    AgentStoreConflict,
    AgentSteeringPending,
    AgentStoreError,
    AgentStoreInvalidState,
    AgentStoreNotFound,
    AgentWorkflowCheckpointConflict,
    ContextPackKnowledgeTarget,
    CurrentProjectKbVerifier,
    RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES,
    StoredToolCall,
    _derive_session_title,
    _title_is_placeholder,
)
from zhimeng_runtime.agent_steering import AgentSteeringStore
from zhimeng_runtime.reasoning_stream import ReasoningStream
from zhimeng_runtime.assistant_stream import AssistantStream
from zhimeng_runtime.agent_tools import (
    CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
    COMMAND_TOOL_NAMES,
    BROWSER_TOOL_NAMES,
    COMPUTER_TOOL_NAMES,
    LEGACY_READ_TOOL_NAMES,
    LEGACY_TOOL_ALIASES,
    CREATION_HANDOFF_SKILL_ID,
    DIRECT_WRITE_TOOL_NAMES,
    MEMORY_TOOL_NAMES,
    HISTORY_TOOL_NAMES,
    PLAN_TOOL_NAMES,
    GOAL_TOOL_NAMES,
    READONLY_TOOL_NAMES,
    AI_SMELL_TOOL_NAMES,
    SKILL_FILE_TOOL_NAMES,
    SKILL_GATED_TOOL_NAMES,
    SKILL_READ_FILE_DESCRIPTION,
    WEB_TOOL_NAMES,
    AgentToolError,
    AgentToolExecutor,
    latest_own_write_pin,
)
from zhimeng_runtime.memory_files import (
    memory_enabled,
    read_user_memory,
    user_memory_path,
    user_memory_transaction,
)
from zhimeng_runtime.memory_updates import parse_memory_proposals, update_user_memory
from zhimeng_runtime.project_instruction_context import (
    ProjectInstructionSnapshot,
    read_project_instruction_snapshot,
)
from zhimeng_runtime.project_reader import WRITING_CONTEXT_FILE_KINDS
from zhimeng_runtime.office_reader import extracted_context_text
from zhimeng_runtime import environment_context
from zhimeng_runtime.cache_shape import RequestShape, compare_shapes, request_shape
from zhimeng_runtime.change_set_models import ChangeSetResponse
from zhimeng_runtime.context_models import (
    KnowledgeFingerprintObservation,
    KnowledgeObservationState,
    canonical_json_hash,
    conservative_token_estimate,
)
from zhimeng_runtime.context_store import (
    ContextStore,
    ContextStoreError,
    ContextStoreIntegrity,
    ContextStoreNotFound,
)
from zhimeng_runtime.errors import ErrorDetail, ErrorEnvelope
from zhimeng_runtime.execution_protocol import (
    ArtifactPayload,
    CapabilityExtension,
    CapabilityName,
    CapabilityProfile,
    ChangeProposalPayload,
    FileChange,
    MessageUsage,
    Run,
    RunStatus,
    SkillCallPayload,
)
from zhimeng_runtime.model_context import resolve_context_window_spec
from zhimeng_runtime.project_models import ProjectInspectionResponse
from zhimeng_runtime.project_kb_models import ProjectKbRebuildRequest, ProjectKbStatus
from zhimeng_runtime.project_kb_service import ProjectKbService, ProjectKbServiceError
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.provider_recovery_models import (
    ProviderRecoveryDecisionRequest,
    ProviderRecoveryResponse,
)
from zhimeng_runtime.provider_turn_models import build_provider_turn_receipt
from zhimeng_runtime.compaction_facts import author_words_block, tool_facts_block
from zhimeng_runtime.session_checkpoint import (
    build_checkpoint_request_messages,
    compose_checkpoint_content,
    missing_checkpoint_sections,
    model_summary_part,
    normalize_checkpoint_text,
)
from zhimeng_runtime.user_skill_folder_service import UserSkillFolderService
from zhimeng_runtime.workflow_models import (
    ProviderIoBoundary,
    WorkflowPlan,
    WorkflowStepCheckpoint,
    build_provider_io_boundary,
)
from zhimeng_runtime.workflow_runner import (
    WorkflowExecutionResult,
    WorkflowRunner,
    workflow_runtime_tool_call_id,
)
from zhimeng_runtime.writer_models import WriterTargetFile
from zhimeng_runtime.writer_trace_models import WriterTraceResponse


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# 一次对话没有轮数、工具次数、连续失败次数、时长、输出 token 的上限。
# 以前是 60 轮 / 240 次 / 8 连败 / 1200 秒 / 6 万 token，到顶切「无工具收尾轮」；
# 作者 2026-09-23 把这些也算作限制（「无论怎么改，都是限制」），停止键归作者。
# 同一批里连续只读工具调用的并行度上限；写/边界调用不参与并行。
READONLY_TOOL_PARALLELISM = 4
# 这一步还没出字、也没有工具调用就失败了，而且是临时的毛病：重来这一步，不让这一场前面的活白干。
# 以前只重来断流和空回答两种，连接中途断了、超时、中转站临时报错都整场失败——9-27 晚查隔离实例库，
# 有 4 场跑到第 5–13 步才这样没了（请求只有 1.3–1.8 万 token，不是太长）。已经吐字后断流走下面的「接着写」。
# 重来时在末尾补一句事实，请求就不是一字不差的同一个（AGENTS.md：中转站会卡住一字不差的同一个请求）；
# 空回答补的那句照 MiMo Code：直接回答，或者调用工具接着做。
MAX_TURN_STREAM_RETRIES = 2
TURN_STREAM_RETRYABLE_CODES = frozenset(
    {
        "agent_provider_incomplete_stream",
        "agent_provider_empty_response",
        "agent_provider_request_failed",
        "agent_provider_timeout",
        "agent_provider_stream_idle_timeout",
    }
)
# 流里的错误事件、HTTP 报错，看得出是临时的才重来（上游忙、并发满、中转站转发失败）；配置不对、额度没了、模型不存在照旧失败。
# HTTP 429 / 快速 502–504 / 520–524 模型层自己已经重试过了，这里是它也没救回来之后再来一次。
TURN_STREAM_TRANSIENT_MARKERS = (
    "concurrency", "rate limit", "try again", "retry later", "overloaded", "temporarily",
    "streaming request failed", "请稍后", "频繁", "繁忙",
)
# 每次重来之前歇一会儿（停止键随时有效）：中转站按账户限并发，刚断掉的那一次上游可能还没停。
TURN_STREAM_RETRY_DELAYS = (3.0, 10.0)


def _transient_before_output(exc: AgentProviderError) -> bool:
    """这一步还没出字就失败了，重来一次有没有指望。"""

    if exc.code in TURN_STREAM_RETRYABLE_CODES:
        return True
    if exc.code in ("agent_provider_error_event", "agent_provider_http_error"):
        text = str(exc).lower()
        return any(marker in text for marker in TURN_STREAM_TRANSIENT_MARKERS)
    return False


def _retry_note(code: str, attempt: int) -> str:
    """重来这一步时补在末尾的那句话：只说发生了什么；带上第几次，每次重来的请求都不一样。"""

    if code == "agent_provider_empty_response":
        return (
            f"上一次回答是空的（第 {attempt} 次）：没有给用户的话，也没有调用工具。"
            "现在直接回答，或者调用工具接着做。"
        )
    return f"（上一次请求还没收到回答就断了，这是第 {attempt} 次重新发送。）"
# 已经吐过字之后流断了（中转站掐长连接、久不出字、连接出错）：已写的留着，让模型从断点接着写
# （9-27 照 Qwen Code #7832；以前整轮失败，作者点「重发这条」，写好的一千多字白写）。
TURN_STREAM_BROKEN_CODES = frozenset(
    {
        "agent_provider_incomplete_stream",
        "agent_provider_timeout",
        "agent_provider_request_failed",
        "agent_provider_stream_idle_timeout",
    }
)
# 自动接着写最多连着这么多次（中间正常收场一次就重新数）：供应商每次吐一点就断时别无限续下去，
# 和上面「零字断流自动重发 2 次」是同一类自动恢复的次数（Qwen Code 续写也是 3 次）。
MAX_CONSECUTIVE_STREAM_RESUMES = 3


# File operations that require refreshing the bound project's disk state.
FILE_CHANGING_TOOL_NAMES = frozenset(
    {
        "write_file", "edit_file", "delete_file", "move_file", "make_folder", "undo_change",
        "change_set_propose", "memory_write", "computer_run_command",
        "project_write_file", "project_edit_file", "project_delete_file", "project_move_file",
        "project_make_folder", "project_undo_change",
    }
)


def _resume_prompt(kind: str, written: str, repeated_segment: str = "") -> str:
    """让模型从断点接着写的那句话：只说停在哪、为什么停、从哪接着写。

    kind 见 turn_resume：truncated / stream_broken / repeating。停在哪引上一段最后 30 个字（照 Qwen 带上文尾巴的做法，
    上一段整段已经作为助手消息放在前面了，这里只点出断点）。
    """

    tail = written.rstrip()[-30:]
    where = f"停在「…{tail}」这里" if tail else "停在开头"
    if kind == "stream_broken":
        return (
            f"你上一段输出写到一半，网络连接断了（不是你停的），{where}。"
            "已经写出来的作者都看到了，从这个字后面直接接着写，写完正常收尾。"
        )
    if kind == "repeating":
        segment = repeated_segment.strip()[:40]
        return (
            f"你上一段输出在同一段话上反复打转：从「{segment}」起的一段一字不差地连着出现了好几遍。"
            f"多出来的几遍已经删掉，只留了第一遍，{where}。"
            "从这里往下写新的内容；这件事已经写完的话，就直接收尾。"
        )
    return (
        f"你上一段输出到了单次输出的长度上限，在半句处被截断了，{where}。"
        "已经写出来的作者都看到了，从这个字后面直接接着写，写完正常收尾。"
    )
PROVIDER_PREFERENCE_EXTENSION = "x-zhimeng.provider-preference"
# 可展示思考是独立过程记录，不混进下一轮正文；兼容中转站包在正文开头的标签。
_TAGGED_REASONING_START = re.compile(r"^\s*<think\b[^>]*>", re.IGNORECASE)
_TAGGED_REASONING_END = re.compile(r"</think\s*>", re.IGNORECASE)
# 连续同参数调用同一工具到这些次数时提醒模型（借鉴 deepseek-harness 的
# repeat-tool-reminder）：只建议不拦截，合理的重复不受阻。失败上限只拦
# 「反复失败」；模型每次都成功却原地打转的循环靠这里打断。
REPEAT_TOOL_REMINDER_THRESHOLDS = (3, 5, 8)
REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS = 500
# 隔着别的调用又回来的一模一样的调用：看最近这么多次（照 Crush loop_detection.go 的窗口，它到 5 次以上硬停，织梦只提醒）。
# 9-27 改稿题里 grok-4.6「读 → 改 → 读 → 改」交替着来，同一个 find 交了 4 次、同一段读了 6 次，连着的计数一次都没响。
REPEAT_TOOL_WINDOW = 10
# 读文件：这一段这一轮读过了、中间没改过文件还在读，就说一声（往后翻页读新的部分不算）。
REPEAT_READ_TOOL_NAMES = frozenset({"read_file", "project_read_file", "computer_read_file"})
REPEAT_READ_DEFAULT_LINES = 800
# 技能目录（挂在 skill_read_file 说明后面）：每条用途截到这么长；整份目录超过预算后，
# 剩下的只列名字（用途用 skill_list_files 看）。工具说明上限 4096 字：说明本身 + 目录预算一共 3,594 字，
# 剩下的留给「只列名字」那一行——说明改长了，目录预算跟着让（说明现在 294 字，预算 3,300，和以前一样）。
SKILL_CATALOG_DESCRIPTION_CHARS = 120
SKILL_CATALOG_BUDGET_CHARS = 3594 - len(SKILL_READ_FILE_DESCRIPTION)
# 用户亲手附上的文件这一轮带内容：每份最多这么多字，这一轮一共最多这么多字，多的只带前面。
OPEN_FOLDER_ATTACHMENT_FILE_CHARS = 20_000
OPEN_FOLDER_ATTACHMENT_TOTAL_CHARS = 60_000
# 这一轮末尾附言的开头（_open_folder_turn_messages）：认得出历史里上一轮补过的那条。
_OPEN_FOLDER_PREFIXES = ("用户这时在编辑器里开着：", "用户附上的文件 ", "用户这时没开着文件")
_OPEN_FOLDER_NOTHING = "用户这时没开着文件。"
_OPENING_UPDATE_PREFIX = "（这场对话开始以后改过）"


def _history_weights(pending: Sequence[Any], transcripts: Mapping[str, int]) -> list[int]:
    """整理对话时每条消息算多少 token（估算：UTF-8 字节 ÷ 3，中文一字一个）。

    存过往来的那几轮（9-28 起下一轮照原样带回去），整段往来——工具调用、结果、补话、回答——算在这一轮最后一条回答上，
    这一轮别的回答算 0；其余照旧只按文字。
    """

    last_answer: dict[str, int] = {}
    for index, item in enumerate(pending):
        run_id = getattr(item, "run_id", None)
        if item.role == "assistant" and run_id in transcripts:
            last_answer[run_id] = index
    weights: list[int] = []
    for index, item in enumerate(pending):
        run_id = getattr(item, "run_id", None)
        if item.role == "assistant" and run_id in transcripts:
            weights.append(transcripts[run_id] if last_answer[run_id] == index else 0)
        else:
            weights.append(4 + conservative_token_estimate(item.role) + conservative_token_estimate(item.content))
    return weights


def _changed_open_folder_note(
    turn_messages: list[dict[str, object]], replayed: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    """「开着哪个文件」这条附言，和历史里上一轮说的一样就不再说；上一轮说开着、这一轮什么都没开，说一声没开着。

    9-28 起前几轮的往来原样带回来（ZCode 档案 12.2），上一轮的附言也在历史里；照旧每轮补一条，同一句话会一轮一轮堆下去。
    历史里没有（前几轮没存往来）就照旧。
    """

    last = next(
        (
            str(message.get("content"))
            for message in reversed(replayed)
            if isinstance(message.get("content"), str)
            and str(message.get("content")).startswith(_OPEN_FOLDER_PREFIXES)
        ),
        None,
    )
    if turn_messages:
        if last is not None and len(turn_messages) == 1 and turn_messages[0].get("content") == last:
            return []
        return turn_messages
    if last is not None and not last.startswith(_OPEN_FOLDER_NOTHING):
        return [{"role": "system", "content": _OPEN_FOLDER_NOTHING}]
    return []
# 对话历史 + 工具结果的 token 预算从「上下文窗口档位」查表（model_context.py），
# 不再按模型名猜，也没有独立常量。env 仍是开发期后门，不是给作者的开关。


def _positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _reasoning_activity_summary(content: str) -> str:
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), content.strip())
    if len(first_line) <= 160:
        return first_line
    return first_line[:157].rstrip() + "..."


def _split_tagged_reasoning(content: str) -> tuple[str, str | None]:
    """Split relay-style ``<think>`` output without touching ordinary prose.

    Only a tag at the beginning of the model output is treated specially, so a
    literal tag mentioned later in fiction or code remains part of the answer.
    An unfinished opening tag is reasoning-only; the empty-answer guard then
    retries/fails instead of persisting scratch text as the assistant reply.
    """

    reasoning_parts: list[str] = []
    answer = content
    while True:
        start = _TAGGED_REASONING_START.match(answer)
        if start is None:
            break
        end = _TAGGED_REASONING_END.search(answer, start.end())
        if end is None:
            reasoning = answer[start.end() :].strip()
            if reasoning:
                reasoning_parts.append(reasoning)
            answer = ""
            break
        reasoning = answer[start.end() : end.start()].strip()
        if reasoning:
            reasoning_parts.append(reasoning)
        answer = answer[end.end() :].lstrip()
    joined_reasoning = "\n\n".join(reasoning_parts)
    return answer, joined_reasoning or None


class RepeatToolChain:
    """单个 Run 内的重复调用记账：连着一模一样的次数、最近几次里隔着别的调用又回来的次数、读过的文件段。

    被拒绝的调用也计数——模型反复尝试被拒的调用，正是需要打断的循环。状态只活在本 Run，不跨 Run 记账。
    只记账、给事实，不拦任何调用（Crush 的 loop_detection 到 5 次以上硬停，织梦不停）。
    """

    def __init__(self) -> None:
        self._key: str | None = None
        self._count = 0
        self._recent: deque[str] = deque(maxlen=REPEAT_TOOL_WINDOW)
        # 读过的文件段：文件 → [(起, 止)]；这一轮一动过文件就清空（改完再读是核对，不算重复）。
        self._read_ranges: dict[str, list[tuple[int, int]]] = {}
        self._read_counts: dict[str, int] = {}
        self._rereads: dict[str, int] = {}

    def observe(self, tool_name: str, canonical_arguments: str) -> int:
        """连着一模一样的第几次（换工具或换参数回到 1）。"""
        key = f"{tool_name}\x00{canonical_arguments}"
        if key == self._key:
            self._count += 1
        else:
            self._key = key
            self._count = 1
        self._recent.append(key)
        return self._count

    def window_count(self, tool_name: str, canonical_arguments: str) -> int:
        """最近 REPEAT_TOOL_WINDOW 次调用里，这一个（同工具同参数）出现了几次，含这一次。"""
        key = f"{tool_name}\x00{canonical_arguments}"
        return sum(1 for item in self._recent if item == key)

    def observe_read(self, target: str, first_line: int, last_line: int) -> tuple[int, int]:
        """记一次读文件：回（这个文件这一轮读了几次，这一段是第几次重读——没读过的段是 0）。"""
        ranges = self._read_ranges.setdefault(target, [])
        self._read_counts[target] = self._read_counts.get(target, 0) + 1
        covered = any(start <= first_line and last_line <= end for start, end in ranges)
        ranges.append((first_line, last_line))
        if not covered:
            return self._read_counts[target], 0
        self._rereads[target] = self._rereads.get(target, 0) + 1
        return self._read_counts[target], self._rereads[target]

    def observe_change(self) -> None:
        """这一轮有调用可能改了文件：读过的段作废，之后再读算核对。"""
        self._read_ranges.clear()
        self._rereads.clear()


def repeat_tool_reminder_text(
    tool_name: str,
    count: int,
    canonical_arguments: str,
) -> str | None:
    """连着一模一样的调用到第 3、5 次，以及第 8 次以后每一次，给模型的提醒；别的次数回 None。

    第一次只说一句；后面列出工具、次数和参数预览（参数截到固定字数，防止写文件的大载荷跟着提醒滚进下一次请求）。
    只说事实和能怎么做，不写「不要」（作者 9-20：提示词里不塞「不要 / 不许」）。
    """
    thresholds = REPEAT_TOOL_REMINDER_THRESHOLDS
    if count not in thresholds and count < thresholds[-1]:
        return None
    if count == thresholds[0]:
        return (
            f"你在用完全相同的参数重复调用同一个工具（连着第 {count} 次）。上一次的结果就在上面："
            "事情还没办成，就换一种做法或者换一组参数；已经办成了就接着往下做。"
        )
    preview = canonical_arguments[:REPEAT_TOOL_ARGUMENT_PREVIEW_CHARACTERS]
    omitted = len(canonical_arguments) - len(preview)
    if omitted > 0:
        preview += f"…（另省略 {omitted} 字符）"
    return (
        "检测到重复的工具调用：\n"
        f"- 工具：{tool_name}\n"
        f"- 连续次数：{count}\n"
        f"- 参数：{preview}\n"
        "这几次一模一样的调用没有带来新进展。读一遍最新的结果，换一个动作、换一组参数，"
        "或者证据已经够了就直接完成。"
    )


def scattered_repeat_reminder_text(tool_name: str, window_count: int) -> str | None:
    """隔着别的调用又回来的一模一样的调用：最近几次里第 3、5 次，以及第 8 次以后每一次。"""
    if window_count not in (3, 5) and window_count < 8:
        return None
    return (
        f"这一步和这一轮前面的调用一模一样（同一个工具 {tool_name}、同一组参数）："
        f"最近 {REPEAT_TOOL_WINDOW} 次调用里这是第 {window_count} 次，中间隔着别的调用。"
        "前几次的结果都在上面；要换个结果，就得换参数或者换一种做法。"
    )


def reread_reminder_text(display: str, first_line: int, last_line: int, reads: int, rereads: int) -> str | None:
    """同一个文件的同一段读过了、这一轮又没改过它还在读：第 2、4 次重读，以及第 6 次以后每一次。"""
    if rereads not in (2, 4) and rereads < 6:
        return None
    return (
        f"「{display}」第 {first_line}–{last_line} 行这一轮已经读过了（这个文件这一轮读了 {reads} 次，"
        "期间没有改过它），读到的内容在上面的结果里。要看整份就一次读完：不写 max_lines，一次给 "
        f"{REPEAT_READ_DEFAULT_LINES} 行。"
    )


def _read_call_range(arguments: Mapping[str, object]) -> tuple[str, str, int, int] | None:
    """读文件调用读的是哪个文件的第几行到第几行：回（记账用的键，给人看的名字，起，止）；从字符位置接着读的不算。"""
    raw = arguments.get("path") or arguments.get("file_id")
    if not isinstance(raw, str) or not raw.strip() or arguments.get("start_offset"):
        return None
    parts = [part for part in raw.strip().replace("\\", "/").rstrip("/").split("/") if part]
    if not parts:
        return None
    # 相对路径和绝对路径（grok 两种混着写）按最后两层对上。
    key = "/".join(parts[-2:]).lower()
    start = arguments.get("start_line")
    count = arguments.get("max_lines")
    first = start if isinstance(start, int) and start > 0 else 1
    lines = count if isinstance(count, int) and count > 0 else REPEAT_READ_DEFAULT_LINES
    return key, parts[-1], first, first + lines - 1


def update_pending_repeat_reminder(
    pending: str | None,
    count: int,
    reminder: str | None,
) -> str | None:
    """一批工具调用里，提醒跟「当前这条链」走。

    链还在继续但没踩到阈值：保留本批已触发的提醒（3 之后紧跟 4 时，
    4 本身不在阈值里，不能把 3 的提醒冲掉）。换工具或换参数（count
    回到 1）：丢掉旧链的提醒，避免刚换做法还被上一轮循环的文案训一通。
    """
    if reminder is not None:
        return reminder
    if count == 1:
        return None
    return pending


T = TypeVar("T")


class AgentUnavailableError(RuntimeError):
    pass


class KnowledgeFingerprintProbe(Protocol):
    def __call__(self, project_id: str | None = None) -> str: ...


@dataclass(frozen=True, slots=True)
class RunStartResult:
    run: Run
    user_message: AgentMessageResponse | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    tool_call: StoredToolCall
    replayed: bool
    candidate_submission: AgentCandidateSubmission | None = None
    candidate_tool_output: object | None = None
    candidate_tool_duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _PreparedToolCall:
    """begin 阶段已落 running 记录、等待执行的工具调用。

    只读并行段先把整批 begin 按原始顺序落库（活动流里「开始」保持
    模型给出的调用顺序），再并行执行与收尾。
    """

    stored_call: StoredToolCall
    runtime_tool_call_id: str
    tool_name: str
    arguments: dict[str, object]
    request_hash: str


def _author_facing_provider_failure(exc: AgentProviderError) -> str:
    """run 失败时要存给作者看的话。

    details 整个 JSON 不甩给作者（它留在错误记录里可查）；
    这里只提炼成一句能行动的人话。前端正则会把不含内部术语的
    中文短句原样透出，所以这里的话术要避免夹带英文技术词。
    """
    if exc.code == "agent_provider_circuit_open":
        # 抛出处的消息本身就是人话（含冷却剩余秒数）
        return str(exc)
    upstream_excerpt = str((exc.details or {}).get("upstream_excerpt", ""))
    upstream_reason = str((exc.details or {}).get("upstream_message") or upstream_excerpt)
    if exc.code == "agent_provider_http_error" and any(
        marker in f"{upstream_excerpt} {upstream_reason}".lower()
        for marker in ("insufficient_user_quota", "insufficient_quota", "额度不足", "余额不足")
    ):
        return f"{exc}\n当前模型渠道的额度不足，请在设置 → 模型服务切换可用渠道，或补足该渠道额度后继续。"
    if exc.code == "agent_provider_http_error" and (
        (exc.details or {}).get("upstream_status") == 403
    ):
        reason = upstream_reason or str(exc)
        return f"模型服务拒绝访问（403）。服务返回：{reason}\n请按返回原因检查账号状态或访问权限。"
    if exc.code == "agent_provider_http_error" and (
        (exc.details or {}).get("upstream_status") == 401
    ):
        suffix = (exc.details or {}).get("api_key_suffix")
        tail = f"（……{suffix}）" if isinstance(suffix, str) and suffix else ""
        return (
            f"{exc}\n"
            f"密钥被拒{tail}，模型服务没有接受这次请求的认证。"
            "请求地址、协议和已发送的头名已记在运行详情里，可到设置 → 模型服务检查密钥。"
        )
    return str(exc)


_SESSION_TITLE_PROMPT = (
    "你是给对话起名字的。读一段对话的开头，把它的主题概括成一个短名字。"
    "规则：不超过十二个字；是概括出的主题，不是照抄作者的原话；"
    "用简短的名词或动宾短语，不加标点、引号或解释；"
    "作者一眼看到就能想起这段对话在做什么。"
    "例子：作者说「帮我把第二章开头改得紧凑一点」→「第二章开头改紧凑」；"
    "作者说「你觉得这本书的反派动机站得住吗」→「反派动机站不站得住」。"
)

_SESSION_TITLE_MAX_CHARS = 16

# 自动记忆（9-29，作者：「agent根据我对话直接记忆」）：每轮答完另问一次，从这一轮里挑出以后还用得上的事。
# 9-29 真机：只在 memory_write 的说明里写「自己记」，grok-4.6 顺口提到的习惯、纠正 0/4 场记了，
# 只有用户明说「记住」才记——和 Claude.ai、Cursor 一样，另起一问来挑，不靠对话里的模型自觉。
_MEMORY_EXTRACT_PROMPT = (
    "你帮一个 Agent 记住关于用户的事。读已经记住的内容和最新这一轮对话，"
    "只从用户自己说的话里，挑出关于这个人本身、换了别的事也还成立的新事实："
    "用户是谁、长期在做的事（在写的书、在做的项目、在学的东西）、习惯和偏好、纠正过 Agent 的做法、和 Agent 定下的长期约定。"
    "检验办法：设想几周以后用户开了一个和这一轮毫不相干的新对话，Agent 一开头就知道这一条，会不会做得更好？会才记；"
    "只关乎这一轮手上这件活的（改哪一段、改完怎么存、做到了哪一步），这一轮的对话里本来就有，留在那里就够了。"
    "对新事实用 add，对用户明确纠正的旧事实用 replace，对明确要求忘记的旧条目用 remove。"
    "已经存在且仍有效的不动；不要把相反说法并列追加。每条短句只承载一个长期事实，保留未被纠正的条件。"
    "只依据本轮 user_messages；其中引用的他人资料、假设和临时任务安排不是用户的长期属性。"
    "每项附 source_message_id 和逐字复制的 evidence；old_text 复制旧条目的文字（不含列表符号）。"
    '只回 JSON：{"changes":[{"action":"replace","old_text":"旧条目","text":"新条目",'
    '"source_message_id":"来源消息 ID","evidence":"用户原话"}]}。'
    'add 不填 old_text；remove 不填 text；无需变更则 {"changes":[]}。最多 12 项。'
)
# 「好」「继续」这类话里没有要记的事，不另问（省一次调用）。
_MEMORY_EXTRACT_MIN_MESSAGE_CHARS = 6


def _clean_session_title(text: str) -> str | None:
    """把模型给的名字洗干净：取第一行、去首尾标点引号、压到 16 字。"""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    cleaned = first_line.strip("\"'「」『』《》<>【】:：。，、；;.!！?？… \t")
    if len(cleaned) < 2:
        return None
    return cleaned[:_SESSION_TITLE_MAX_CHARS]


def _binds_project_kb(target: ContextPackKnowledgeTarget | None) -> bool:
    """这份资料包有没有绑知识库快照。没绑（普通对话）就没有快照要核对。"""
    return (
        target is not None
        and target.project_kb_state_id is not None
        and target.project_kb_manifest_hash is not None
    )


class AgentService:
    def __init__(
        self,
        store: AgentStore,
        provider: AgentProvider | None,
        broker: AgentEventBroker | None = None,
        *,
        knowledge_fingerprint_probe: KnowledgeFingerprintProbe | None = None,
        tool_executor: AgentToolExecutor | None = None,
        context_store: ContextStore | None = None,
        project_kb_service: ProjectKbService | None = None,
        project_locks: ProjectLockRegistry | None = None,
        provider_resolver: Callable[[], AgentProvider | None] | None = None,
        provider_selector: Callable[[str | None, str | None, str | None], AgentProvider | None]
        | None = None,
        user_skills: UserSkillFolderService | None = None,
        auto_memory: bool = False,
    ) -> None:
        self.store = store
        self.provider = provider
        # 每轮答完以后从这一轮里挑出以后还用得上的事记进用户记忆（设置页「记忆」开着时）。
        # 真正的服务（app.py）打开；直接构造 AgentService 的测试默认不开，免得脚本里的模型多挨一问。
        self._auto_memory = auto_memory
        self._provider_resolver = provider_resolver
        self._provider_selector = provider_selector
        self.broker = broker or AgentEventBroker()
        self.knowledge_fingerprint_probe = knowledge_fingerprint_probe
        self.tool_executor = tool_executor
        self.context_store = context_store
        self.project_kb_service = project_kb_service
        # 项目 KB 自动重建的节流表：同个项目 60 秒内只自动重建一次。
        self._kb_auto_rebuild_at: dict[str, float] = {}
        # 重建后的重绑定表：project_id -> ((旧 state_id, 旧 manifest_hash), 重绑定 target)。
        # 重建会换 KB 快照身份，旧资料包里的绑定跟着失效；这里记下新指针，
        # 让旧包在核对外 KNOWLEDGE 卡不变的前提下继续可用。
        self._kb_rebased_bindings: dict[str, tuple[tuple[str, str], ContextPackKnowledgeTarget]] = {}
        self.project_locks = project_locks
        self.user_skills = user_skills
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._launch_lock = asyncio.Lock()
        self._compacting_sessions: set[str] = set()
        # 每个「整理中」会话配一个完成事件：整理期间发来的消息排队等它，
        # 不再 409 顶回去。finally 里 setdefault+set，与 start_run 的
        # setdefault+wait 配对，谁先谁后都不会丢通知。
        self._compaction_done: dict[str, asyncio.Event] = {}
        self._starting_sessions: dict[str, int] = {}
        # 作者用 / 点名的技能（引用标签）：run_id -> 技能名列表，_execute 取出来发一条就丢。
        self._run_skill_names: dict[str, list[str]] = {}
        # 每场对话上一次调模型的请求形状（cache_shape.py）：和这一次比，说得出缓存没命中是哪一段变了。
        # 只在内存里；重启后第一次记成 first_request。
        self._request_shapes: dict[str, RequestShape] = {}
        self._start_operations = 0
        self._start_operations_drained = asyncio.Event()
        self._start_operations_drained.set()
        self._closing = False

    def status(self) -> AgentStatusResponse:
        provider = self._resolve_provider()
        if provider is None:
            return AgentStatusResponse(
                available=False,
                executor=None,
                capabilities=None,
                detail="Writing Agent provider is not configured",
            )
        return AgentStatusResponse(
            available=True,
            executor=provider.identity,
            capabilities=self._capabilities(provider),
            detail=(
                "Writing Agent provider and Runtime project tools are configured"
                if self.tool_executor is not None
                else "Writing Agent provider is configured"
            ),
        )

    async def create_session(self, *, project_id: str | None, title: str):
        provider = self._require_provider()
        return await asyncio.to_thread(
            self.store.create_session,
            project_id=project_id,
            title=title,
            executor=provider.identity,
            capabilities=self._capabilities(provider),
        )

    async def fork_session(
        self,
        source_session_id: str,
        *,
        up_to_message_id: str,
        title: str | None = None,
    ):
        return await asyncio.to_thread(
            self.store.fork_session,
            source_session_id,
            up_to_message_id=up_to_message_id,
            title=title,
        )

    async def compact_session(self, session_id: str):
        # 作者点「压缩对话」也按这场对话用的模型的窗口档位：以前一律按 256K（摘要最多 8,000 字），1M 档的对话也一样。
        provider = await self._require_provider_for_session_id(session_id)
        spec = resolve_context_window_spec(getattr(provider, "context_window", None))
        record = await self._fold_session_history(session_id, run_id=None, force=True, window_spec=spec)
        if record is None:
            raise AgentStoreConflict("当前会话暂无足够历史可供压缩")
        return record

    async def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        rebind_provider: bool = False,
        provider_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        provider = None
        if rebind_provider or provider_id is not None or model is not None or reasoning_effort is not None:
            selected_provider_id = provider_id
            selected_model = model
            selected_effort = reasoning_effort
            if provider_id is not None or model is not None or reasoning_effort is not None:
                current = await asyncio.to_thread(self.store.get_session, session_id)
                current_provider_id, current_model, current_effort = self._session_provider_preference(current)
                selected_provider_id = provider_id or current_provider_id
                selected_model = model or current_model
                selected_effort = reasoning_effort or current_effort
            provider = self._require_provider(
                provider_id=selected_provider_id,
                model=selected_model,
                reasoning_effort=selected_effort,
            )
        return await asyncio.to_thread(
            self.store.update_session,
            session_id,
            title=title,
            pinned=pinned,
            executor=provider.identity if provider is not None else None,
            capabilities=self._capabilities(provider) if provider is not None else None,
        )

    async def start_run(
        self,
        *,
        session_id: str,
        message: str,
        expected_project_revision: int | None,
        expected_project_fingerprint: str | None,
        active_chapter_id: str | None,
        open_file_path: str | None = None,
        writer_mode: str = "repair_only",
        idempotency_key: str,
        context_pack_id: str | None = None,
        expected_context_pack_hash: str | None = None,
        continuation_source_run_id: str | None = None,
        review_source_run_id: str | None = None,
        manual_file_ids: list[str] | None = None,
        manual_skill_names: list[str] | None = None,
        image_ids: list[str] | None = None,
        completion_event_ids: list[str] | None = None,
        goal: GoalAdmission | None = None,
    ) -> RunStartResult:
        session = await asyncio.to_thread(self.store.get_session, session_id)
        admitted = await asyncio.to_thread(
            self.store.find_run_by_idempotency_key, session_id, idempotency_key,
        )
        # A receipt survives provider/configuration changes. create_run below
        # still checks the full request hash inside its transaction on replay.
        provider = None
        current_capabilities = None
        if admitted is None:
            provider = self._require_provider_for_session(session)
            self._dispatch_config_fingerprint(provider)
            current_capabilities = self._capabilities(provider)
        knowledge_observation = None
        workflow_plan_builder = None
        if admitted is None and context_pack_id is not None and expected_context_pack_hash is not None:
            await self._require_current_project_kb_for_context(
                context_pack_id,
                expected_context_pack_hash,
            )
            knowledge_observation = await self._observe_context_pack_knowledge(
                context_pack_id,
                expected_context_pack_hash,
            )
            if (
                knowledge_observation.state is KnowledgeObservationState.MATCHED
                and await self._has_managed_workflow(context_pack_id)
            ):
                workflow_runner = self._workflow_runner()
                if workflow_runner is None:
                    raise AgentUnavailableError(
                        "Reviewed Context execution requires the managed workflow executor"
                    )
                workflow_plan_builder = lambda run: workflow_runner.build_plan(
                    run,
                    context_pack_id=context_pack_id,
                )
        while True:
            async with self._launch_lock:
                if self._closing:
                    raise AgentUnavailableError("Writing Agent Runtime is shutting down")
                if session_id not in self._compacting_sessions:
                    self._starting_sessions[session_id] = self._starting_sessions.get(session_id, 0) + 1
                    self._start_operations += 1
                    self._start_operations_drained.clear()
                    break
                # 整理只持续几十秒：排队等它结束再开工，不再 409 把作者的话顶回去。
                done = self._compaction_done.setdefault(session_id, asyncio.Event())
            await done.wait()
        try:
            create_operation = asyncio.create_task(
                asyncio.to_thread(
                    self.store.create_run,
                    session_id=session_id,
                    message=message,
                    expected_project_revision=expected_project_revision,
                    expected_project_fingerprint=expected_project_fingerprint,
                    active_chapter_id=active_chapter_id,
                    open_file_path=open_file_path,
                    writer_mode=writer_mode,
                    idempotency_key=idempotency_key,
                    context_pack_id=context_pack_id,
                    expected_context_pack_hash=expected_context_pack_hash,
                    knowledge_observation=knowledge_observation,
                    current_executor=provider.identity if provider is not None else None,
                    current_capabilities=current_capabilities,
                    workflow_plan_builder=workflow_plan_builder,
                    continuation_source_run_id=continuation_source_run_id,
                    review_source_run_id=review_source_run_id,
                    manual_file_ids=manual_file_ids,
                    manual_skill_names=manual_skill_names,
                    image_ids=image_ids,
                    completion_event_ids=completion_event_ids,
                    **({"goal": goal} if goal else {}),
                ),
                name=f"writing-agent-create:{session_id}",
            )
            request_cancelled = False
            while True:
                try:
                    run, user_message, replayed = await asyncio.shield(create_operation)
                    break
                except asyncio.CancelledError:
                    # 建记录那一步自己被取消了（关机时事件循环把所有任务都取消）：再等它也只会立刻又报取消，
                    # 这个循环就不让出事件循环、原地空转，关机卡死（9-28 一条测试失败后 asyncio.run 收尾时撞上）。
                    if create_operation.cancelled():
                        raise
                    request_cancelled = True
            if manual_skill_names and run.status not in TERMINAL_RUN_STATUSES:
                self._run_skill_names.setdefault(run.run_id, list(manual_skill_names))
            finalize_operation = asyncio.create_task(
                self._finalize_run_start(run.run_id),
                name=f"writing-agent-finalize:{run.run_id}",
            )
            while True:
                try:
                    run = await asyncio.shield(finalize_operation)
                    break
                except asyncio.CancelledError:
                    if finalize_operation.cancelled():
                        raise
                    request_cancelled = True
            if request_cancelled:
                raise asyncio.CancelledError
            return RunStartResult(
                run=run,
                user_message=user_message,
                replayed=replayed,
            )
        finally:
            starting = self._starting_sessions[session_id] - 1
            if starting:
                self._starting_sessions[session_id] = starting
            else:
                self._starting_sessions.pop(session_id, None)
            release_operation = asyncio.create_task(
                self._release_start_operation(),
                name=f"writing-agent-start-release:{session_id}",
            )
            while True:
                try:
                    await asyncio.shield(release_operation)
                    break
                except asyncio.CancelledError:
                    if release_operation.cancelled():
                        raise
                    continue

    async def cancel_run(self, run_id: str) -> tuple[Run, bool]:
        request_cancelled = False
        async with self._launch_lock:
            before = await asyncio.to_thread(self.store.get_run, run_id)
            if before.status in TERMINAL_RUN_STATUSES:
                return before, False
            if self._closing:
                raise AgentUnavailableError("Writing Agent Runtime is shutting down")
            cancel_operation = asyncio.create_task(
                asyncio.to_thread(self.store.cancel_run, run_id),
                name=f"writing-agent-cancel-claim:{run_id}",
            )
            while True:
                try:
                    run = await asyncio.shield(cancel_operation)
                    break
                except asyncio.CancelledError:
                    if cancel_operation.cancelled():
                        raise
                    request_cancelled = True
            cancel_event = self._cancel_events.get(run_id)
            if cancel_event is not None:
                cancel_event.set()
            task = self._tasks.get(run_id)
            if task is not None and not task.done():
                task.cancel()
            else:
                terminal_operation = asyncio.create_task(
                    asyncio.to_thread(self.store.mark_cancelled, run_id),
                    name=f"writing-agent-cancel-terminal:{run_id}",
                )
                while True:
                    try:
                        run = await asyncio.shield(terminal_operation)
                        break
                    except asyncio.CancelledError:
                        if terminal_operation.cancelled():
                            raise
                        request_cancelled = True
        if self.tool_executor is not None:
            cancel_browser = getattr(self.tool_executor, "cancel_browser_session", None)
            if cancel_browser is not None:
                browser_operation = asyncio.create_task(cancel_browser(run))
                while True:
                    try:
                        await asyncio.shield(browser_operation)
                        break
                    except asyncio.CancelledError:
                        if browser_operation.cancelled():
                            raise
                        request_cancelled = True
            cancel_commands = getattr(self.tool_executor, "cancel_command_sessions", None)
            if cancel_commands is not None:
                command_operation = asyncio.create_task(cancel_commands(run))
                while True:
                    try:
                        await asyncio.shield(command_operation)
                        break
                    except asyncio.CancelledError:
                        if command_operation.cancelled():
                            raise
                        request_cancelled = True
        publish_operation = asyncio.create_task(
            self.broker.publish(run_id),
            name=f"writing-agent-cancel-publish:{run_id}",
        )
        while True:
            try:
                await asyncio.shield(publish_operation)
                break
            except asyncio.CancelledError:
                if publish_operation.cancelled():
                    raise
                request_cancelled = True
        if request_cancelled:
            raise asyncio.CancelledError
        return run, True

    async def get_workflow_state(self, run_id: str) -> AgentWorkflowStateResponse:
        run, plan, chain = await asyncio.to_thread(
            self._load_workflow_snapshot,
            run_id,
        )
        checkpoints = tuple(
            item for item in chain if isinstance(item, WorkflowStepCheckpoint)
        )
        boundaries = tuple(
            item for item in chain if isinstance(item, ProviderIoBoundary)
        )
        boundary = boundaries[0] if boundaries else None
        last = checkpoints[-1] if checkpoints else None

        if run.status is RunStatus.SUCCEEDED:
            resume_state = "terminal"
            reason_code = "workflow_run_terminal"
        elif boundary is not None:
            resume_state = "provider_dispatch_uncertain"
            reason_code = "provider_dispatch_already_started"
        elif run.status is RunStatus.PAUSED:
            resume_blocker = await asyncio.to_thread(
                self.store.workflow_resume_blocker,
                run_id,
            )
            resumable = resume_blocker is None and (
                last is None
                or (
                    last.execution_status == "succeeded"
                    and last.domain_status
                    in plan.steps[last.step_ordinal - 1].continue_on_domain_status
                )
            )
            resume_state = "resumable" if resumable else "blocked"
            reason_code = (
                "workflow_resume_available"
                if resumable
                else resume_blocker or "workflow_step_not_resumable"
            )
        elif run.status in TERMINAL_RUN_STATUSES:
            resume_state = "terminal"
            reason_code = "workflow_run_terminal"
        else:
            resume_state = "running"
            reason_code = "workflow_execution_active"

        can_advance = last is None or (
            last.execution_status == "succeeded"
            and last.domain_status
            in plan.steps[last.step_ordinal - 1].continue_on_domain_status
        )
        next_step_ordinal = (
            len(checkpoints) + 1
            if resume_state in {"running", "resumable"}
            and can_advance
            and len(checkpoints) < len(plan.steps)
            else None
        )

        return AgentWorkflowStateResponse(
            run_id=run.run_id,
            run_status=run.status,
            plan=plan,
            checkpoints=checkpoints,
            provider_boundary=boundary,
            last_checkpoint_id=None if last is None else last.checkpoint_id,
            last_checkpoint_hash=None if last is None else last.checkpoint_hash,
            next_step_ordinal=next_step_ordinal,
            resume_state=resume_state,
            resume_allowed=resume_state == "resumable",
            reason_code=reason_code,
            formal_draft_allowed=False,
        )

    async def get_writer_trace(self, run_id: str) -> WriterTraceResponse:
        workflow = await self.get_workflow_state(run_id)

        async def optional(getter):
            try:
                return await asyncio.to_thread(getter, run_id)
            except AgentStoreNotFound:
                return None

        writer_task = await optional(self.store.get_writer_task)
        activation_binding = await optional(
            self.store.get_creation_instruction_activation_binding
        )
        materialization = await optional(
            self.store.get_creation_instruction_materialization
        )
        dispatch_receipt = await optional(
            self.store.get_creation_instruction_dispatch_receipt
        )
        candidate_output = await optional(self.store.get_agent_candidate_output)
        writer_candidate_result = await optional(
            self.store.get_writer_candidate_result
        )
        provider_recovery = await optional(self.store.get_provider_recovery)
        return WriterTraceResponse(
            run_id=run_id,
            workflow=workflow,
            writer_task=writer_task,
            activation_binding=activation_binding,
            materialization=materialization,
            dispatch_receipt=dispatch_receipt,
            candidate_output=candidate_output,
            writer_candidate_result=writer_candidate_result,
            provider_recovery=provider_recovery,
        )

    async def resume_run(
        self,
        run_id: str,
        request: AgentRunResumeRequest,
    ) -> AgentRunResumeResponse:
        run = await asyncio.to_thread(self.store.get_run, run_id)
        provider = await self._require_provider_for_session_id(run.session_id)
        async with self._launch_lock:
            if self._closing:
                raise AgentUnavailableError("Writing Agent Runtime is shutting down")
            active_task = self._tasks.get(run_id)
            if active_task is not None and not active_task.done():
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Run is already executing",
                    code="agent_run_already_running",
                )

            state = await self.get_workflow_state(run_id)
            if not state.resume_allowed:
                raise AgentWorkflowCheckpointConflict(
                    "Workflow Run is not resumable",
                    code=state.reason_code,
                )
            if (
                request.expected_workflow_plan_id != state.plan.workflow_plan_id
                or request.expected_plan_hash != state.plan.plan_hash
                or request.expected_checkpoint_id != state.last_checkpoint_id
                or request.expected_checkpoint_hash != state.last_checkpoint_hash
            ):
                raise AgentWorkflowCheckpointConflict(
                    "Workflow state changed before Resume",
                    code="workflow_resume_stale",
                )

            workflow_runner = self._workflow_runner()
            tool_executor = self.tool_executor
            if workflow_runner is None or tool_executor is None:
                raise AgentUnavailableError(
                    "Durable workflow execution is not configured"
                )
            try:
                await asyncio.to_thread(
                    workflow_runner.validate_plan_dependencies,
                    state.plan,
                )
            except AgentProviderError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            run = await asyncio.to_thread(self.store.get_run, run_id)
            try:
                self._require_bound_provider(run, provider)
            except AgentProviderError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            try:
                await tool_executor.validate_run_project(run)
            except AgentToolError as exc:
                raise AgentWorkflowCheckpointConflict(
                    str(exc),
                    code=exc.code,
                ) from exc
            knowledge_observation = (
                await self._observe_run_context_pack_knowledge(run_id)
            )
            self._require_resumable_knowledge(knowledge_observation)
            await self._require_current_project_kb_for_run(run_id)

            claim_operation = asyncio.create_task(
                asyncio.to_thread(
                    self.store.claim_workflow_resume,
                    run_id,
                    expected_workflow_plan_id=request.expected_workflow_plan_id,
                    expected_plan_hash=request.expected_plan_hash,
                    expected_checkpoint_id=request.expected_checkpoint_id,
                    expected_checkpoint_hash=request.expected_checkpoint_hash,
                ),
                name=f"writing-agent-resume-claim:{run_id}",
            )
            request_cancelled = False
            while True:
                try:
                    claim = await asyncio.shield(claim_operation)
                    break
                except asyncio.CancelledError:
                    if claim_operation.cancelled():
                        raise
                    request_cancelled = True
            self._launch(run_id, resumed=True)
            await self.broker.publish(run_id)

        if request_cancelled:
            raise asyncio.CancelledError
        last = claim.checkpoints[-1] if claim.checkpoints else None
        return AgentRunResumeResponse(
            run=claim.run,
            accepted=True,
            workflow_plan_id=claim.plan.workflow_plan_id,
            resumed_from_checkpoint_id=(
                None if last is None else last.checkpoint_id
            ),
            resumed_from_checkpoint_hash=(
                None if last is None else last.checkpoint_hash
            ),
            next_action=(
                "workflow_step"
                if claim.next_step_ordinal is not None
                else "provider_io"
            ),
        )

    async def decide_provider_recovery(
        self,
        run_id: str,
        request: ProviderRecoveryDecisionRequest,
    ) -> ProviderRecoveryResponse:
        if request.action == "adopt_persisted_candidate":
            lease = await self._acquire_current_project_kb_lease_for_run(run_id)
            try:
                async with self._terminal_project_lock_for_run(run_id):
                    return await self._await_terminal_store_operation(
                        self.store.decide_provider_recovery,
                        run_id,
                        request,
                        current_project_kb_verifier=(
                            self._require_current_project_kb_target_sync
                        ),
                    )
            finally:
                lease.__exit__(None, None, None)
        return await asyncio.to_thread(
            self.store.decide_provider_recovery,
            run_id,
            request,
        )

    def is_run_executing(self, run_id: str) -> bool:
        """Terminal storage can precede transcript, memory and compaction cleanup."""
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        async with self._launch_lock:
            self._closing = True
            tasks = list(self._tasks.values())
            for cancel_event in self._cancel_events.values():
                cancel_event.set()
            for task in tasks:
                task.cancel()
        await self._start_operations_drained.wait()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.tool_executor is not None:
            close_commands = getattr(self.tool_executor, "shutdown_commands", None)
            if close_commands is not None:
                await close_commands()
        workflow_run_ids = await asyncio.to_thread(
            self.store.list_nonterminal_workflow_run_ids
        )
        for run_id in workflow_run_ids:
            await self._preserve_workflow_on_shutdown(run_id)
        await asyncio.to_thread(self.store.recover_interrupted)

    def reopen_after_restore(self) -> None:
        """Discard references into the previous database after shutdown drained it."""
        if self._tasks or self._start_operations:
            raise RuntimeError("Agent tasks must finish before reopening the runtime")
        self._kb_auto_rebuild_at.clear()
        self._kb_rebased_bindings.clear()
        self._request_shapes.clear()
        self._compaction_done.clear()
        self._run_skill_names.clear()
        self._closing = False

    def _resolve_provider(
        self,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentProvider | None:
        if self._provider_selector is not None and (
            provider_id is not None or model is not None or reasoning_effort is not None
        ):
            return self._provider_selector(provider_id, model, reasoning_effort)
        if self._provider_resolver is not None:
            return self._provider_resolver()
        return self.provider

    def _require_provider(
        self,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AgentProvider:
        provider = self._resolve_provider(
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if provider is None:
            raise AgentUnavailableError("Writing Agent provider is not configured")
        return provider

    @staticmethod
    def _session_provider_preference(session) -> tuple[str | None, str | None, str | None]:
        extension = session.capabilities.extensions.get(PROVIDER_PREFERENCE_EXTENSION)
        config = extension.config if extension is not None else None
        if not isinstance(config, dict):
            return None, None, None
        provider_id = config.get("provider_id")
        model = config.get("model")
        effort = config.get("reasoning_effort")
        return (
            provider_id.strip() if isinstance(provider_id, str) and provider_id.strip() else None,
            model.strip() if isinstance(model, str) and model.strip() else None,
            effort
            if effort in {"auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"}
            else None,
        )

    def _require_provider_for_session(self, session) -> AgentProvider:
        provider_id, model, reasoning_effort = self._session_provider_preference(session)
        return self._require_provider(
            provider_id=provider_id,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    async def _require_provider_for_session_id(
        self,
        session_id: str,
    ) -> AgentProvider:
        if self._provider_selector is None:
            return self._require_provider()
        session = await asyncio.to_thread(self.store.get_session, session_id)
        return self._require_provider_for_session(session)

    @staticmethod
    def _dispatch_config_fingerprint(provider: AgentProvider) -> str:
        fingerprint = provider.dispatch_config_fingerprint
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise AgentProviderError(
                "Writing Agent provider has no valid dispatch configuration fingerprint",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        if provider.identity.instance_id != f"provider-config-{fingerprint}":
            raise AgentProviderError(
                "Writing Agent executor identity is not bound to its dispatch configuration",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        return fingerprint

    @classmethod
    def _require_bound_provider(cls, run: Run, provider: AgentProvider) -> str:
        fingerprint = cls._dispatch_config_fingerprint(provider)
        if provider.identity != run.executor:
            raise AgentProviderError(
                "Writing Agent executor configuration changed after this Run was created",
                code="workflow_executor_changed",
                retryable=False,
            )
        return fingerprint

    def _capabilities(
        self,
        provider: AgentProvider | None = None,
    ) -> CapabilityProfile:
        provider = provider or self._require_provider()
        names = list(provider.capabilities.names)
        if self.tool_executor is not None:
            for capability in (
                CapabilityName.TOOL_CALLS,
                CapabilityName.FILE_READ,
                CapabilityName.ARTIFACTS,
            ):
                if capability not in names:
                    names.append(capability)
            if (
                any(item.name == "skill_execute" for item in self.tool_executor.definitions)
                and CapabilityName.SKILLS not in names
            ):
                names.append(CapabilityName.SKILLS)
        extensions = dict(provider.capabilities.extensions)
        model = getattr(provider, "model", None)
        provider_id = getattr(provider, "provider_id", None)
        reasoning_effort = getattr(provider, "reasoning_effort", None)
        if isinstance(model, str) and model.strip() and reasoning_effort in {
            "auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"
        }:
            preference_config = {
                "model": model.strip(),
                "reasoning_effort": reasoning_effort,
            }
            if isinstance(provider_id, str) and provider_id.strip():
                preference_config["provider_id"] = provider_id.strip()
            extensions[PROVIDER_PREFERENCE_EXTENSION] = CapabilityExtension(
                version="1",
                config=preference_config,
            )
        return provider.capabilities.model_copy(
            update={"names": names, "extensions": extensions}
        )

    @staticmethod
    def _runtime_tool_call_id(run_id: str, provider_tool_call_id: str) -> str:
        """Keep provider call IDs scoped to one Run before persisting them."""
        return workflow_runtime_tool_call_id(run_id, provider_tool_call_id)

    def _launch(self, run_id: str, *, resumed: bool = False) -> None:
        cancel_event = asyncio.Event()
        self._cancel_events[run_id] = cancel_event
        self._tasks[run_id] = asyncio.create_task(
            self._execute(run_id, cancel_event, resumed=resumed),
            name=f"writing-agent:{run_id}",
        )

    async def _finalize_run_start(self, run_id: str) -> Run:
        """Register or preserve a committed Run despite request cancellation."""

        async with self._launch_lock:
            preserve = self._closing
            run = await asyncio.to_thread(self.store.get_run, run_id)
            active_task = self._tasks.get(run_id)
            if (
                not preserve
                and run.status is RunStatus.QUEUED
                and (active_task is None or active_task.done())
            ):
                self._launch(run_id)
        if preserve and run.status not in TERMINAL_RUN_STATUSES:
            await self._preserve_workflow_on_shutdown(run_id)
            run = await asyncio.to_thread(self.store.get_run, run_id)
        await self.broker.publish(run_id)
        return run

    async def _release_start_operation(self) -> None:
        async with self._launch_lock:
            self._start_operations -= 1
            if self._start_operations == 0:
                self._start_operations_drained.set()

    async def _execute(
        self,
        run_id: str,
        cancel_event: asyncio.Event,
        *,
        resumed: bool = False,
    ) -> None:
        project_kb_lease: AbstractContextManager[object] | None = None
        provider_messages: list[dict[str, object]] = []
        transcript_anchor: dict[str, object] | None = None
        content_parts: list[str] = []
        response_start = 0
        try:
            if resumed:
                claimed = await asyncio.to_thread(self.store.get_run, run_id)
                if claimed.status is not RunStatus.RUNNING:
                    raise AgentProviderError(
                        "Workflow Resume claim is no longer running",
                        code="workflow_resume_claim_lost",
                        retryable=False,
                    )
                resume_tool_executor = self.tool_executor
                if resume_tool_executor is None:
                    raise AgentProviderError(
                        "Durable workflow execution is not configured",
                        code="workflow_executor_unavailable",
                        retryable=True,
                    )
                if claimed.project_revision is not None:
                    try:
                        await resume_tool_executor.validate_run_project(claimed)
                    except AgentToolError as exc:
                        raise AgentProviderError(
                            str(exc),
                            code=exc.code,
                            retryable=exc.retryable,
                        ) from exc
            else:
                await self._transition(
                    run_id,
                    RunStatus.STARTING,
                    "Starting provider request",
                )
            knowledge_observation = await self._observe_run_context_pack_knowledge(
                run_id
            )
            await self._require_current_project_kb_for_run(run_id)
            if resumed:
                self._require_resumable_knowledge(knowledge_observation)
            prepared_context = await asyncio.to_thread(
                self.store.prepare_context_injection,
                run_id,
                knowledge_observation=knowledge_observation,
            )
            if prepared_context is None:
                if resumed:
                    raise AgentProviderError(
                        "Durable workflow Resume has no bound Context injection",
                        code="workflow_resume_context_missing",
                        retryable=False,
                    )
                current_run = await asyncio.to_thread(self.store.get_run, run_id)
                # 历史字数上限按这场对话用的模型的窗口档位给（9-27 去掉 64 条窗口时一起改：
                # 以前一律 12 万字，1M 档 24 万字才压缩，窗口先顶上、每轮往后滑）。
                history_spec = resolve_context_window_spec(
                    getattr(
                        await self._require_provider_for_session_id(current_run.session_id),
                        "context_window",
                        None,
                    )
                )
                review_context = await asyncio.to_thread(
                    self.store.review_provider_messages, run_id, max_characters=history_spec.history_max_characters,
                )
                history_parts = getattr(self.store, "provider_history_parts", None)
                if review_context is not None:
                    provider_messages = review_context
                    transcript_anchor = provider_messages[0]
                    source_file_ids = await asyncio.to_thread(self.store.review_source_file_ids, run_id)
                    if source_file_ids and current_run.project_revision is not None:
                        attachments = await self._open_folder_turn_messages(
                            current_run, input_override={'manual_file_ids': source_file_ids},
                        )
                        provider_messages.append({'role': 'user', 'content': (
                            '以下是原任务附件中当前可定位的文件内容，仅作核验依据。'
                            '文件编号会随保存变化，未列出的附件没有完成读取；按来源记录核对路径和当前文件。\n'
                            + '\n\n'.join(str(item.get('content', '')) for item in attachments)
                        )})
                elif callable(history_parts):
                    # 存过往来的几轮照原样放回（ZCode 档案 12.2）；历史的上限按估算的 token，和整理线同一个口径。
                    provider_messages, history_run_id = await asyncio.to_thread(
                        partial(
                            history_parts,
                            current_run.session_id,
                            max_tokens=history_spec.history_max_tokens,
                        )
                    )
                    # 这一轮作者的话是历史的最后一条：答完以后从它后面切下这一轮发给模型的那一段存起来。
                    if (
                        history_run_id == run_id
                        and provider_messages
                        and provider_messages[-1].get("role") == "user"
                    ):
                        transcript_anchor = provider_messages[-1]
                else:
                    provider_messages = await asyncio.to_thread(
                        partial(
                            self.store.provider_history,
                            current_run.session_id,
                            max_characters=history_spec.history_max_characters,
                        )
                    )
                if current_run.origin_kind == "command_completion":
                    # There is no fabricated user message. Anchor the new
                    # transcript after the existing history, before source facts.
                    transcript_anchor = provider_messages[-1] if provider_messages else None
                    provider_messages.extend(await asyncio.to_thread(self.store.completion_start_messages, run_id))
            else:
                provider_messages = list(prepared_context.messages)
                await self.broker.publish(run_id)
            if not self._memory_enabled():
                # 关闭记忆后清掉历史里程序追加的记忆副本；项目说明仍保留。
                kept: list[dict[str, object]] = []
                for message in provider_messages:
                    content = message.get("content")
                    if (
                        message is not transcript_anchor
                        and message.get("role") == "user"
                        and isinstance(content, str)
                        and content.startswith(_OPENING_UPDATE_PREFIX + "\n\n用户记忆现在是：\n")
                    ):
                        project_at = content.find("\n\n这个文件夹里的说明文件")
                        if project_at >= 0:
                            kept.append({**message, "content": _OPENING_UPDATE_PREFIX + content[project_at:]})
                    else:
                        kept.append(message)
                provider_messages = kept
            replayed_history = list(provider_messages)
            # 用户记忆（全局）和打开的文件夹里的说明文件（AGENTS.md / CLAUDE.md / 老书的《写作笔记.md》）：
            # 对话开头各一条 role="user" 消息（纠偏指令 3.7），用这场对话开始时的那份；后来改过的在末尾追加一条。没有就不带。
            memory_messages, opening_update = await self._opening_context_messages(
                await asyncio.to_thread(self.store.get_run, run_id),
                replayed=replayed_history,
            )
            if memory_messages:
                insert_at = (
                    1
                    if provider_messages
                    and provider_messages[0].get("role") == "system"
                    else 0
                )
                provider_messages[insert_at:insert_at] = memory_messages
            if not resumed:
                await self._transition(
                    run_id,
                    RunStatus.RUNNING,
                    "Preparing provider request",
                )
            current_run = await asyncio.to_thread(self.store.get_run, run_id)
            provider = await self._require_provider_for_session_id(
                current_run.session_id
            )
            if transcript_anchor is None:
                transcript_anchor = next((message for message in reversed(provider_messages)
                    if message.get('role') == 'user' and message.get('content') == current_run.objective), None)
            tool_executor = self.tool_executor
            # Candidate submission is a managed-workflow boundary capability;
            # ordinary Runs must never see it in their Provider catalog.
            run_goal = await asyncio.to_thread(self.store.get_run_goal, run_id)
            # Historical runtime reminders are snapshots, not a fresh instruction
            # to keep running after a completed or paused goal.
            provider_messages = [message for message in provider_messages
                                 if not (message.get("role") == "user" and
                                         str(message.get("content", "")).startswith(GOAL_CONTEXT_PREFIX))]
            if run_goal is not None:
                provider_messages.append(goal_context(run_goal))
            all_tool_definitions = (
                tool_executor.definitions if tool_executor is not None else ()
            )
            tool_definitions = tuple(
                definition
                for definition in all_tool_definitions
                if definition.name != CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
            )
            if current_run.project_revision is None:
                # 「随便聊」Run：没有打开文件夹，就是普通对话。
                # 工具照常给（技能翻阅、电脑上的读写、执行命令、记偏好、上网、
                # 列工作计划），模型想用自己调；除了开头那段环境事实，不发场景说明。
                # 作者 2026-09-20 三次反馈：塞进去的内部提示词被弱模型原文念
                # 出来当答案，还把话题硬拽到写小说上。作者原话「不要内部提示词
                # 了」「按需调动，而不是时时刻刻调动 skills」。技能目录挂在 skill_read_file
                # 的说明里（下面 _with_skill_catalog），不另发一段话。
                # 写文件工具不绑书也给（纠偏指令 3.3）：以前没打开文件夹就只能靠命令写，
                # 没有记录、撤不回；现在写到哪都记在 file_journal 里，能撤回。
                tool_definitions = tuple(
                    definition
                    for definition in tool_definitions
                    if definition.name
                    in SKILL_FILE_TOOL_NAMES | COMPUTER_TOOL_NAMES | COMMAND_TOOL_NAMES | BROWSER_TOOL_NAMES | MEMORY_TOOL_NAMES | HISTORY_TOOL_NAMES | WEB_TOOL_NAMES | PLAN_TOOL_NAMES | GOAL_TOOL_NAMES | AI_SMELL_TOOL_NAMES | DIRECT_WRITE_TOOL_NAMES
                )
                # 挡位不绑书也作数：「只读」只读资料、回答，不写文件、不执行命令、不写笔记，
                # 和绑书时的只读一样（以前不绑书时请求里写着只读，命令照样能跑）。
                casual_mode = await asyncio.to_thread(self.store.get_writer_mode, run_id)
                if casual_mode != "author_review_change_proposal":
                    tool_definitions = tuple(
                        definition
                        for definition in tool_definitions
                        if definition.name not in COMMAND_TOOL_NAMES | BROWSER_TOOL_NAMES | MEMORY_TOOL_NAMES | DIRECT_WRITE_TOOL_NAMES
                    )
            # 开关只管跨项目的用户记忆。项目说明仍可按当前文件权限记录。
            if not self._memory_enabled():
                if current_run.project_revision is None:
                    tool_definitions = tuple(
                        definition for definition in tool_definitions if definition.name not in MEMORY_TOOL_NAMES
                    )
                else:
                    tool_definitions = tuple(
                        definition.model_copy(update={"description": (
                            "Remember one sentence for this project with scope 'project'. "
                            "It appends a line to AGENTS.md at the root of the open folder; "
                            "conversations in this folder start with that file. The write is "
                            "recorded and undo_change can undo it. User memory is currently "
                            "turned off in settings; project memory remains available."
                        )}) if definition.name == "memory_write" else definition
                        for definition in tool_definitions
                    )
            # 前缀稳定：工具定义按名字排序，一场对话里每轮发出的字节都相同，
            # 上游提示缓存才能命中。
            tool_definitions = tuple(
                sorted(tool_definitions, key=lambda definition: definition.name)
            )
            tool_definitions = await self._with_skill_catalog(tool_definitions)
            tool_definitions = await self._with_skill_declared_tools(tool_definitions)
            get_context_pack = (
                getattr(self.context_store, "get_pack", None)
                if self.context_store is not None
                else None
            )
            active_pack = None
            if prepared_context is not None and callable(get_context_pack):
                try:
                    active_pack = await asyncio.to_thread(
                        get_context_pack,
                        prepared_context.context_pack_id,
                    )
                except ContextStoreError as exc:
                    raise AgentProviderError(
                        str(exc),
                        code="context_pack_not_found",
                        retryable=False,
                    ) from exc
            if current_run.project_revision is not None and (
                active_pack is None or active_pack.pack.profile.startswith("chat")
            ):
                # 打开了文件夹的普通对话（纠偏指令 3.9）：系统提示只剩开头那段环境事实和挡位，
                # 以前的「这是和作者的普通对话……也可能要你改书」和开新书那段都不发了；工具按挡位给。
                writer_mode = await asyncio.to_thread(
                    self.store.get_writer_mode,
                    run_id,
                )
                # skill_execute 是老的受管技能（四个 family ID），普通对话不给（纠偏指令 3.8，随 T15 删）。
                stripped_tool_names: set[str] = {"skill_execute"}
                if writer_mode != "author_review_change_proposal":
                    stripped_tool_names |= (
                        {"change_set_propose"} | set(DIRECT_WRITE_TOOL_NAMES) | set(COMMAND_TOOL_NAMES) | set(BROWSER_TOOL_NAMES) | set(MEMORY_TOOL_NAMES)
                    )
                tool_definitions = tuple(
                    definition
                    for definition in tool_definitions
                    if definition.name not in stripped_tool_names
                )
                if active_pack is None and current_run.origin_kind != "command_completion":
                    provider_messages.extend(
                        _changed_open_folder_note(
                            await self._open_folder_turn_messages(current_run), replayed_history
                        )
                    )
            # 作者用 / 点名的技能（引用标签）：只在点名的这条 Run 发一条指路消息。
            provider_messages.extend(self._manual_skills_provider_messages(run_id))
            # 环境事实：对话开头一段（这场对话里不变），跨天或换挡时末尾追加一条（纠偏指令 3.2）。
            environment_opening, environment_update = await self._environment_provider_messages(
                current_run, replayed=replayed_history
            )
            provider_messages.insert(0, environment_opening)
            if environment_update is not None:
                provider_messages.append(environment_update)
            if opening_update is not None:
                provider_messages.append(opening_update)
            self._require_bound_provider(current_run, provider)
            workflow_result: WorkflowExecutionResult | None = None
            provider_boundary: ProviderIoBoundary | None = None
            provider_accepted_reported_models: tuple[str, ...] = ()
            candidate_submit_available = False
            total_tool_calls = 0
            managed_workflow = (
                prepared_context is not None
                and await self._has_managed_workflow(
                prepared_context.context_pack_id
                )
            )
            if managed_workflow:
                workflow_runner = self._workflow_runner()
                if workflow_runner is None:
                    raise AgentProviderError(
                        "Reviewed Context execution requires the managed workflow executor",
                        code="workflow_executor_unavailable",
                        retryable=True,
                    )
                workflow_result = await workflow_runner.execute_pre_provider(
                    current_run,
                    cancel_event=cancel_event,
                    execute_tool_call=self._execute_tool_call,
                )
                provider_messages.extend(workflow_result.provider_messages)
                total_tool_calls = workflow_result.tool_call_count
                try:
                    project_snapshot = await tool_executor.validate_run_project(
                        current_run
                    )
                except AgentToolError as exc:
                    raise AgentProviderError(
                        str(exc),
                        code=exc.code,
                        retryable=exc.retryable,
                    ) from exc
                final_knowledge_observation = (
                    await self._observe_run_context_pack_knowledge(run_id)
                )
                self._require_resumable_knowledge(final_knowledge_observation)
                await self._require_current_project_kb_for_run(run_id)
                writer_mode = await asyncio.to_thread(
                    self.store.get_writer_mode,
                    run_id,
                )
                operation_package = await asyncio.to_thread(
                    self.context_store.get_prewrite_operation_package,
                    workflow_result.plan.context_pack_id,
                )
                get_context_pack = getattr(self.context_store, "get_pack", None)
                context_pack = (
                    await asyncio.to_thread(
                        get_context_pack,
                        workflow_result.plan.context_pack_id,
                    )
                    if callable(get_context_pack)
                    else None
                )
                writer_targets = self._writer_target_files(
                    project_snapshot,
                    mode=writer_mode,
                    active_chapter_id=operation_package.active_chapter_id,
                    manual_file_ids=(
                        context_pack.pack.manual_file_ids
                        if context_pack is not None
                        else ()
                    ),
                )
                writer_task = await asyncio.to_thread(
                    self.store.ensure_writer_task,
                    run_id,
                    target_files=writer_targets,
                )
                instruction_materialization = await asyncio.to_thread(
                    self.store.ensure_creation_instruction_materialization,
                    run_id,
                )
                provider_messages.append(
                    self._writer_task_provider_message(writer_task)
                )
                provider_messages.append(
                    self._creation_instruction_provider_message(
                        instruction_materialization
                    )
                )
                # 受管写作任务的工具目录保持原样：写工具（应走 change_set_propose
                # 提案通道）、电脑访问、上网抓取、执行命令与计划清单（与写作任务
                # 无关）都不通告，目录与改动前逐位一致，tool_definitions_hash 不变。
                blocked_tool_names = (
                    {"skill_execute"}
                    | set(DIRECT_WRITE_TOOL_NAMES)
                    | set(COMPUTER_TOOL_NAMES)
                    | set(LEGACY_READ_TOOL_NAMES - {"project_list_files", "project_read_file"})
                    | set(COMMAND_TOOL_NAMES)
                    | set(BROWSER_TOOL_NAMES)
                    | set(WEB_TOOL_NAMES)
                    | set(PLAN_TOOL_NAMES)
                    | set(GOAL_TOOL_NAMES)
                )
                if writer_task.mode == "repair_only":
                    blocked_tool_names.add("change_set_propose")
                # 这条老路照旧用 project_list_files / project_read_file 读书（3.11 以后普通对话只发 list_files / read_file）。
                managed_reads = tuple(getattr(tool_executor, "managed_read_definitions", ()) or ())
                tool_definitions = tuple(
                    definition
                    for definition in (*managed_reads, *all_tool_definitions)
                    if definition.name not in blocked_tool_names
                )
                candidate_definition = getattr(
                    tool_executor,
                    "candidate_output_definition",
                    None,
                )
                # 两种 Writer 模式都以候选提交收尾：repair_only 交修复计划，
                # 编辑模式交改动提案（writer-task 分发的 required_tool_sequence
                # 已这么告知模型，工具必须如实配齐）；是否真的提交由模型按
                # 作者的话自行判断。只回答的运行不再因为没有候选信封而
                # 失败——没有提案就没有可审阅对象，正文一个字节都不会动，
                # 作者审阅制（apply 前必须人点）不受影响。
                # A managed run may be resumed with a lightweight/legacy tool
                # executor that does not implement candidate submission (for
                # example while restoring an old run or when the provider is
                # being used only for a textual answer).  The provider must
                # still be allowed to complete that answer.  Real
                # AgentToolExecutor instances expose the definition and keep
                # the normal proposal -> candidate path unchanged; when it is
                # absent we simply do not advertise the optional final tool.
                if candidate_definition is not None:
                    tool_definitions = (*tool_definitions, candidate_definition)
                    candidate_submit_available = True
                if writer_task.mode != "repair_only":
                    provider_messages.append(
                        self._agent_decision_guidance_provider_message()
                    )
                provider_accepted_reported_models = (
                    self._provider_accepted_reported_models(provider)
                )
                project_kb_lease = (
                    await self._acquire_current_project_kb_lease_for_run(run_id)
                )
                provider_boundary = await self._persist_provider_io_boundary(
                    current_run,
                    workflow_result,
                    provider_messages=provider_messages,
                    tool_definitions=tool_definitions,
                    provider=provider,
                    accepted_reported_models=provider_accepted_reported_models,
                )
            if not managed_workflow:
                continuation_message = await asyncio.to_thread(
                    self.store.continuation_provider_message, run_id
                )
                if continuation_message is not None:
                    provider_messages.append(continuation_message)
            tool_failures = 0
            total_usage: MessageUsage | None = None
            candidate_submission_for_run: AgentCandidateSubmission | None = None
            repeat_chain = RepeatToolChain()
            run_started = monotonic()
            window_spec = resolve_context_window_spec(
                getattr(provider, "context_window", None)
            )
            context_token_budget = _positive_env_float(
                "ZHIMENG_AGENT_CONTEXT_TOKEN_BUDGET",
                window_spec.context_token_budget,
            )
            last_estimated_input_tokens: int | None = None
            last_reported_input_tokens: int | None = None
            # 这一场里复读闸门判到过没有：第一次截掉多出来的几遍、让模型接着写，第二次才整轮停（照 Gemini CLI）。
            loop_corrected = False
            consecutive_stream_resumes = 0
            # 这场运行（自己的任务里）发出去的请求都带上对话 ID：Responses 线拿它当 prompt_cache_key，同一场对话落到同一台服务器上。
            PROMPT_CACHE_KEY.set(current_run.session_id)
            # Keep the existing builtin selection; MCP is refreshed separately
            # for every provider request, including ordinary no-folder chats.
            builtin_tool_definitions = tool_definitions

            for provider_turn in itertools.count(1):
                await self._consume_steering(
                    current_run, provider_messages, transcript_anchor, cancel_event,
                )
                await self._consume_command_completions(
                    current_run, provider_messages, transcript_anchor, cancel_event,
                )
                await self._require_current_project_kb_for_run(run_id)
                mcp_adapter = getattr(tool_executor, "mcp_adapter", None)
                if mcp_adapter is not None:
                    mode = await asyncio.to_thread(self.store.get_writer_mode, run_id)
                    mcp_definitions = mcp_adapter.bind(read_only=mode != "author_review_change_proposal")
                    tool_definitions = (*builtin_tool_definitions, *mcp_definitions)
                turn_tools = tool_definitions
                # Token 感知预算：估算回喂对话的大小，并用上一轮真实的
                # input_tokens 校准估算偏差；超过预算才折叠。
                # 不超阈值时消息序列逐字节不变（收据哈希与上游提示缓存的前提）。
                estimated_tokens = self._estimate_provider_messages_tokens(
                    provider_messages
                )
                calibration = 1.0
                if last_estimated_input_tokens and last_reported_input_tokens:
                    calibration = min(
                        2.0,
                        max(
                            0.5,
                            last_reported_input_tokens / last_estimated_input_tokens,
                        ),
                    )
                # Tool schemas also consume input space. Compaction requests and
                # the next task request must both fit; never dispatch an unchanged
                # oversized request after reporting a zero-result "fold".
                tool_tokens = conservative_token_estimate(json.dumps(
                    [tool.model_dump(mode="json") for tool in turn_tools], ensure_ascii=False,
                )) if turn_tools else 0
                message_budget = int(context_token_budget / calibration) - tool_tokens
                if estimated_tokens > message_budget:
                    await self._compact_active_run(
                        current_run, provider, provider_messages, transcript_anchor,
                        cancel_event, message_budget, provider_turn, int(context_token_budget / calibration),
                    )
                    estimated_tokens = self._estimate_provider_messages_tokens(provider_messages)
                    # Steering arriving during compaction is consumed before dispatch.
                    if await self._consume_steering(current_run, provider_messages, transcript_anchor, cancel_event):
                        continue
                if cancel_event.is_set():
                    raise AgentProviderCancelled()
                # Reported input usage includes the tool catalog. Calibrate like
                # against like; a short user request with a large catalog must not
                # make the next file read look twice its estimated size.
                last_estimated_input_tokens = estimated_tokens + tool_tokens
                shape = request_shape(
                    provider_messages,
                    turn_tools if tool_executor is not None and turn_tools else (),
                )
                cache_shape = compare_shapes(
                    self._request_shapes.get(current_run.session_id), shape
                )
                self._request_shapes[current_run.session_id] = shape
                await self._record_run_event(
                    run_id,
                    "model_turn_started",
                    {
                        "turn": provider_turn,
                        "wrap_up": False,
                        "tools_offered": len(turn_tools),
                        "estimated_input_tokens": estimated_tokens,
                        "cache_shape": cache_shape,
                    },
                )
                turn_request_hash = (
                    self._provider_dispatch_material_hash(
                        provider_messages=provider_messages,
                        tool_definitions=tool_definitions,
                        provider=provider,
                        dispatch_config_fingerprint=self._require_bound_provider(
                            current_run,
                            provider,
                        ),
                        accepted_reported_models=(
                            provider_accepted_reported_models
                        ),
                    )
                    if provider_boundary is not None
                    else None
                )
                turn_stream_attempt = 0
                # 这一步重来时补的那句话在 provider_messages 里的位置：第二次重来换掉它，不一条条往后堆。
                retry_note_index: int | None = None
                while True:
                    turn_stream_attempt += 1
                    message_id = f"message_{uuid4().hex}"
                    content_parts = []
                    response_start = len(provider_messages)
                    reasoning_stream = ReasoningStream(self.store, self.broker, run_id)
                    assistant_stream = AssistantStream(self.store, self.broker, run_id, message_id)
                    tool_calls: list[ProviderToolCall] = []
                    completed: ProviderCompleted | None = None
                    # 这一轮没正常收场、要让模型接着写的原因：truncated（撞上单次输出上限）/ stream_broken（吐字后断流）/
                    # repeating（第一次判到复读，已截到第一遍结尾）；None 是正常收场。
                    turn_resume: str | None = None
                    loop_segment = ""
                    wire_messages = await asyncio.to_thread(hydrate_images, self.store.database, current_run.session_id, provider_messages) if any(
                        isinstance(message.get("content"), list) for message in provider_messages
                    ) else provider_messages
                    stream = (
                        provider.stream_chat(
                            wire_messages,
                            cancel_event=cancel_event,
                            tools=turn_tools,
                        )
                        if tool_executor is not None and turn_tools
                        else provider.stream_chat(
                            wire_messages,
                            cancel_event=cancel_event,
                        )
                    )
                    try:
                        async for event in stream:
                            if not isinstance(event, ProviderTextDelta):
                                await assistant_stream.flush()
                            if isinstance(event, ProviderReasoningDelta):
                                if reasoning_stream.closed:
                                    reasoning_stream = ReasoningStream(self.store, self.broker, run_id)
                                await reasoning_stream.add(
                                    event.text, visibility=getattr(event, "visibility", "full"),
                                )
                            elif isinstance(event, ProviderTextDelta):
                                await reasoning_stream.finish()
                                content_parts.append(event.text)
                                await assistant_stream.add(event.text)
                            elif isinstance(event, ProviderToolCall):
                                await reasoning_stream.finish()
                                tool_calls.append(event)
                            elif isinstance(event, ProviderCompleted):
                                completed = event
                        if completed is None:
                            raise AgentProviderError(
                                "Provider stream ended without a completion event",
                                code="agent_provider_incomplete_stream",
                                retryable=True,
                            )
                    except AgentProviderError as exc:
                        await reasoning_stream.finish_preserving_cancellation("interrupted")
                        if exc.code == "agent_provider_output_truncated" and content_parts:
                            # 输出打满单次 token 上限：已生成的字都在作者屏幕上，
                            # 整轮报废等于白写。按「本轮正常收场但被截断」处理，
                            # 走下面的续写分支让模型从中断处接着写。
                            completed = ProviderCompleted(
                                finish_reason="length",
                                usage=None,
                            )
                            turn_resume = "truncated"
                            break
                        if (
                            exc.code in TURN_STREAM_BROKEN_CODES
                            and content_parts
                            and not tool_calls
                            and not cancel_event.is_set()
                            and consecutive_stream_resumes < MAX_CONSECUTIVE_STREAM_RESUMES
                        ):
                            consecutive_stream_resumes += 1
                            # 已经吐字后断流：已写的在作者屏幕上，留着，走下面的续写分支从断点接着写。
                            completed = ProviderCompleted(finish_reason="length", usage=None)
                            turn_resume = "stream_broken"
                            break
                        if (
                            exc.code == "agent_provider_output_repeating"
                            and content_parts
                            and not tool_calls
                            and not loop_corrected
                        ):
                            # 第一次判到复读：截到循环节第一遍的结尾（多出来的几遍不进对话、作者屏幕上也换成截好的），
                            # 让模型接着往下写；同一场第二次判到再整轮停。
                            loop_corrected = True
                            details = exc.details or {}
                            keep = details.get("keep_chars")
                            written = "".join(content_parts)
                            if isinstance(keep, int) and 0 < keep < len(written) and written[:keep].strip():
                                content_parts[:] = [written[:keep]]
                            loop_segment = str(details.get("repeated_segment") or "")
                            completed = ProviderCompleted(finish_reason="length", usage=None)
                            turn_resume = "repeating"
                            break
                        # 这一步还什么都没出（屏幕上没有字、也没有工具调用）就临时失败了：重来这一步。
                        # 已吐字后断流走上面的续写——重发会把已显示的文字再吐一遍。
                        if (
                            not content_parts
                            and not tool_calls
                            and not cancel_event.is_set()
                            and turn_stream_attempt <= MAX_TURN_STREAM_RETRIES
                            and _transient_before_output(exc)
                        ):
                            note = {"role": "user", "content": _retry_note(exc.code, turn_stream_attempt)}
                            if retry_note_index is None:
                                retry_note_index = len(provider_messages)
                                provider_messages.append(note)
                            else:
                                provider_messages[retry_note_index] = note
                            if provider_boundary is not None:
                                turn_request_hash = self._provider_dispatch_material_hash(
                                    provider_messages=provider_messages,
                                    tool_definitions=tool_definitions,
                                    provider=provider,
                                    dispatch_config_fingerprint=self._require_bound_provider(current_run, provider),
                                    accepted_reported_models=provider_accepted_reported_models,
                                )
                            retry_shape = request_shape(
                                provider_messages,
                                turn_tools if tool_executor is not None and turn_tools else (),
                            )
                            await self._record_run_event(
                                run_id,
                                "model_turn_stream_retry",
                                {
                                    "turn": provider_turn,
                                    "attempt": turn_stream_attempt,
                                    "error_code": exc.code,
                                    "error_details": exc.details,
                                    "cache_shape": compare_shapes(
                                        self._request_shapes.get(current_run.session_id), retry_shape
                                    ),
                                },
                            )
                            self._request_shapes[current_run.session_id] = retry_shape
                            delay = TURN_STREAM_RETRY_DELAYS[
                                min(turn_stream_attempt, len(TURN_STREAM_RETRY_DELAYS)) - 1
                            ]
                            try:
                                await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                            except TimeoutError:
                                pass
                            else:
                                raise AgentProviderCancelled() from exc
                            continue
                        raise
                    finally:
                        try:
                            await assistant_stream.finish_preserving_cancellation()
                        finally:
                            await reasoning_stream.finish_preserving_cancellation(
                                "completed" if completed is not None else "interrupted",
                            )
                    break
                if turn_resume != "stream_broken":
                    consecutive_stream_resumes = 0
                await self._record_run_event(
                    run_id,
                    "model_turn_completed",
                    {
                        "turn": provider_turn,
                        "finish_reason": completed.finish_reason,
                        "tool_calls": len(tool_calls),
                        "reported_model": completed.reported_model,
                        "effective_reasoning_effort": completed.effective_reasoning_effort,
                        "requested_reasoning_effort": getattr(provider, "reasoning_effort", None),
                        "effective_thinking_budget_tokens": getattr(completed, "effective_thinking_budget_tokens", None),
                        "usage": (
                            completed.usage.model_dump(mode="json")
                            if completed.usage is not None
                            else None
                        ),
                    },
                )
                if completed.usage is not None and completed.usage.input_tokens_reported:
                    last_reported_input_tokens = completed.usage.input_tokens
                content, tagged_reasoning = _split_tagged_reasoning(
                    "".join(content_parts)
                )
                content_parts = [content] if content else []
                requested_effort = getattr(provider, "reasoning_effort", None)
                thinking_budget = getattr(completed, "effective_thinking_budget_tokens", None)
                request_note = None
                if getattr(provider, "wire_format", None) == "anthropic_messages":
                    if thinking_budget is not None:
                        request_note = f"本次请求思考预算：{thinking_budget} tokens。"
                    elif requested_effort not in (None, "auto", "none", "off"):
                        request_note = "本次未发送思考预算参数，按服务默认处理。"
                elif requested_effort not in (None, "auto") and completed.effective_reasoning_effort != requested_effort:
                    request_note = (
                        f"本次请求档位由 {requested_effort} 调整为 {completed.effective_reasoning_effort}。"
                        if completed.effective_reasoning_effort is not None
                        else "本次未发送思考档位参数，按服务默认处理。"
                    )
                if request_note:
                    await asyncio.to_thread(
                        self.store.append_reasoning, run_id,
                        summary=request_note,
                        content=request_note + "这描述的是发送参数，不代表模型内部思考强度的测量。",
                        source="runtime",
                    )
                    await self.broker.publish(run_id)
                if tagged_reasoning:
                    summary_text = _reasoning_activity_summary(tagged_reasoning)
                    await asyncio.to_thread(
                        self.store.append_reasoning,
                        run_id,
                        summary=summary_text,
                        content=tagged_reasoning,
                    )
                    await self.broker.publish(run_id)
                if provider_boundary is not None:
                    if turn_request_hash is None:  # pragma: no cover - guarded above.
                        raise RuntimeError("Provider turn request hash was not prepared")
                    receipt = build_provider_turn_receipt(
                        provider_boundary,
                        turn_ordinal=provider_turn,
                        request_hash=turn_request_hash,
                        reported_model=completed.reported_model,
                        accepted_reported_models=provider_accepted_reported_models,
                        finish_reason=completed.finish_reason,
                        usage=completed.usage,
                        created_at=datetime.now(UTC),
                    )
                    await asyncio.to_thread(
                        self.store.create_provider_turn_receipt,
                        receipt,
                    )
                    if receipt.model_identity_status == "rejected_mismatch":
                        # 带上中转实际报的名字：失败卡上的「认可这个名字」按钮
                        # 靠 details.reported_model 把名字加进设置 → 模型服务的
                        # 接受名单（消息文本里的引号名留给旧版前端兜底）。
                        raise AgentProviderError(
                            "Provider reported a model identity that was not explicitly "
                            f'accepted: "{receipt.reported_model}"',
                            code="agent_provider_model_mismatch",
                            retryable=False,
                            details={"reported_model": receipt.reported_model},
                        )
                total_usage = self._merge_usage(
                    total_usage, completed.usage, previous_turns=provider_turn - 1,
                )

                if tool_calls:
                    # 中转站常把 finish_reason 报成 "stop" 却仍带着完整工具调用；
                    # 调用本身已在 Provider 层逐字段校验，这里不再按协议错误终止。
                    if tool_executor is None:
                        raise AgentProviderError(
                            "Provider requested a tool but this Runtime has no project tools",
                            code="agent_provider_tool_call_unsupported",
                            retryable=False,
                        )
                    if content.strip():
                        # Tool-preface text is an assistant progress message. Persist
                        # it before tools start so cancellation/reload keeps its place.
                        await asyncio.to_thread(
                            self.store.finalize_assistant_message,
                            run_id,
                            message_id=message_id,
                            content=content,
                            finish_reason="tool_calls",
                        )
                        await self.broker.publish(run_id)
                        # Managed candidate submission may complete in this branch.
                        # Its final reply is a distinct message from the progress note.
                        message_id = f"message_{uuid4().hex}"
                    advertised_tool_names = {
                        definition.name for definition in tool_definitions
                    }
                    total_tool_calls += len(tool_calls)
                    provider_messages.append(
                        {
                            "role": "assistant",
                            "content": "".join(content_parts) or None,
                            "tool_calls": [
                                {
                                    "id": call.tool_call_id,
                                    "type": "function",
                                    "function": {
                                        "name": call.tool_name,
                                        "arguments": json.dumps(
                                            call.arguments,
                                            ensure_ascii=False,
                                            sort_keys=True,
                                            separators=(",", ":"),
                                        ),
                                    },
                                }
                                for call in tool_calls
                            ],
                        }
                    )
                    # Persist the complete request batch before any tool can change
                    # external state. Restart recovery pairs it with durable receipts.
                    await asyncio.to_thread(
                        self.store.save_run_transcript, run_id,
                        self._current_transcript(provider_messages, transcript_anchor),
                    )
                    current_run = await asyncio.to_thread(self.store.get_run, run_id)
                    pending_repeat_reminder: str | None = None
                    # 隔着别的调用又回来的、重读读过的段：和上面那条连着的提醒分开攒，这一批里后面换了调用也不冲掉。
                    pending_loop_notes: list[str] = []
                    # 串行预处理：重复计数与拒绝判定按原始顺序完成，语义与
                    # 逐个执行完全一致；只有「真正的执行」按只读/写切分。
                    batch_plan: list[tuple[ProviderToolCall, tuple[str, str] | None]] = []
                    for call in tool_calls:
                        if cancel_event.is_set():
                            raise AgentProviderCancelled()
                        canonical_call_arguments = json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        repeat_count = repeat_chain.observe(
                            call.tool_name,
                            canonical_call_arguments,
                        )
                        reminder = repeat_tool_reminder_text(
                            call.tool_name,
                            repeat_count,
                            canonical_call_arguments,
                        )
                        pending_repeat_reminder = update_pending_repeat_reminder(
                            pending_repeat_reminder,
                            repeat_count,
                            reminder,
                        )
                        loop_note: str | None = None
                        # 每次都看最近 10 次里的总数，只有连着那条这一次已经说了才不再说：以前只在「不连着」时看窗口，
                        # A、B、A、A 这样第 3 次恰好紧跟第 2 次（连着第 2 次）时两边都不响。9-28 改稿题 edit6「并段」里
                        # grok-4.6 同一段读了 9 遍，第 13、17 步就是这样（.runtime/absorb-20260927/_replay_repeat.py 回放：旧写法不响）。
                        if reminder is None:
                            loop_note = scattered_repeat_reminder_text(
                                call.tool_name,
                                repeat_chain.window_count(call.tool_name, canonical_call_arguments),
                            )
                        if call.tool_name in REPEAT_READ_TOOL_NAMES and isinstance(call.arguments, Mapping):
                            read_range = _read_call_range(call.arguments)
                            if read_range is not None:
                                target, display, first_line, last_line = read_range
                                reads, rereads = repeat_chain.observe_read(target, first_line, last_line)
                                if loop_note is None and reminder is None:
                                    loop_note = reread_reminder_text(display, first_line, last_line, reads, rereads)
                        elif call.tool_name in FILE_CHANGING_TOOL_NAMES:
                            repeat_chain.observe_change()
                        if loop_note is not None and loop_note not in pending_loop_notes:
                            pending_loop_notes.append(loop_note)
                        # 不可执行的调用不进执行器：把失败原因回给模型，让它在
                        # 失败上限内自我纠正，而不是终止整个 Run。候选提交在
                        # 受管边界前不通告，因此也会落进「未通告」这一支。
                        rejection: tuple[str, str] | None = None
                        if call.arguments_error is not None:
                            # 参数没能解析（非法 JSON / 被网关截断 / 超大小上限）：
                            # 不判整轮失败，把原因作为这次调用的结果回喂模型
                            # 重发，反复失败由失败计数兜底。
                            rejection = (
                                "agent_provider_tool_arguments_invalid",
                                f"这次工具调用没有执行：{call.arguments_error}。"
                                "请重发这次调用。",
                            )
                        elif (
                            call.tool_name not in advertised_tool_names
                            and LEGACY_TOOL_ALIASES.get(call.tool_name) not in advertised_tool_names
                        ):
                            rejection = (
                                "agent_tool_not_advertised",
                                f"这个工具在本回合不可用：{call.tool_name}。"
                                "请只使用系统通告过的工具。",
                            )
                        elif (
                            call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
                            and not candidate_submit_available
                        ):
                            rejection = (
                                "agent_candidate_output_not_allowed",
                                "候选提交只能在受管工作流到达边界后使用。",
                            )
                        elif (
                            call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
                            and len(tool_calls) != 1
                        ):
                            rejection = (
                                "agent_candidate_output_not_final_tool",
                                "候选提交必须单独作为本回合唯一的工具调用，请重新提交。",
                            )
                        batch_plan.append((call, rejection))

                    plan_index = 0
                    while plan_index < len(batch_plan):
                        call, rejection = batch_plan[plan_index]
                        if cancel_event.is_set():
                            raise AgentProviderCancelled()
                        if rejection is not None:
                            tool_failures += 1
                            provider_messages.append(
                                self._rejected_tool_result_message(
                                    call.tool_call_id,
                                    code=rejection[0],
                                    message=rejection[1],
                                )
                            )
                            plan_index += 1
                            continue
                        if call.tool_name in READONLY_TOOL_NAMES:
                            # 连续只读段：段内并行（结果仍按原始顺序回喂），
                            # 段间与写调用严格串行。
                            segment_calls = [call]
                            lookahead = plan_index + 1
                            while (
                                lookahead < len(batch_plan)
                                and batch_plan[lookahead][1] is None
                                and batch_plan[lookahead][0].tool_name
                                in READONLY_TOOL_NAMES
                            ):
                                segment_calls.append(batch_plan[lookahead][0])
                                lookahead += 1
                            if provider_boundary is not None:
                                await self._require_current_project_kb_for_run(run_id)
                            outcomes = await self._execute_readonly_segment(
                                current_run,
                                segment_calls,
                                cancel_event,
                            )
                            for segment_call, outcome in zip(
                                segment_calls, outcomes, strict=True
                            ):
                                if isinstance(outcome, BaseException):
                                    # 取消与未补偿掉的异常按串行语义原样上抛；
                                    # 排在前面的调用结果已按序回喂。
                                    raise outcome
                                executed = outcome
                                stored_call = executed.tool_call
                                if stored_call.status == "failed":
                                    tool_failures += 1
                                provider_messages.append(
                                    self._provider_tool_result_message(
                                        segment_call.tool_call_id,
                                        stored_call,
                                        replayed=executed.replayed,
                                    )
                                )
                                if stored_call.status == "cancelled":
                                    raise AgentProviderCancelled()
                                # retryable 的工具错误（超时、书被外部改过等）
                                # 不再终止整轮 Run：结果已回喂，模型看到错误
                                # 可以重试或换路走；反复失败到顶切收尾轮交代，
                                # 不整轮报废（那会把已执行的副作用盖进失败卡）。
                            plan_index = lookahead
                            continue
                        # 写/边界/未分类调用：串行独占，不与任何只读并行。
                        if provider_boundary is not None:
                            await self._require_current_project_kb_for_run(run_id)
                        executed = await self._execute_tool_call(
                            current_run,
                            provider_tool_call_id=call.tool_call_id,
                            tool_name=call.tool_name,
                            arguments=call.arguments,
                            cancel_event=cancel_event,
                        )
                        stored_call = executed.tool_call
                        if stored_call.status == "failed":
                            tool_failures += 1
                        if call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME:
                            if stored_call.status == "failed":
                                # 候选提交的参数校验失败（真实模型会把 format
                                # 之类的字段放错层）不再终止 Run：把带字段路径
                                # 的校验错误回喂给模型自我纠正，失败到顶切收尾轮。
                                provider_messages.append(
                                    self._provider_tool_result_message(
                                        call.tool_call_id,
                                        stored_call,
                                        replayed=executed.replayed,
                                    )
                                )
                                plan_index += 1
                                continue
                            pending_atomic_completion = (
                                executed.candidate_tool_output is not None
                            )
                            if (
                                pending_atomic_completion
                                and stored_call.status != "running"
                            ) or (
                                not pending_atomic_completion
                                and stored_call.status != "succeeded"
                            ):
                                raise AgentProviderError(
                                    "Candidate submission ToolCall did not succeed",
                                    code="agent_candidate_output_submit_failed",
                                    retryable=False,
                                )
                            if executed.candidate_submission is None:
                                raise AgentProviderError(
                                    "Candidate submission ToolCall has no validated payload",
                                    code="agent_candidate_output_integrity_failed",
                                    retryable=False,
                                )
                            if candidate_submission_for_run is not None:
                                raise AgentProviderError(
                                    "A Run cannot submit more than one candidate",
                                    code="agent_candidate_output_duplicate",
                                    retryable=False,
                                )
                            candidate_submission_for_run = executed.candidate_submission
                        else:
                            provider_messages.append(
                                self._provider_tool_result_message(
                                    call.tool_call_id,
                                    stored_call,
                                    replayed=executed.replayed,
                                )
                            )
                        if stored_call.status == "cancelled":
                            raise AgentProviderCancelled()
                        # 同上：retryable 工具错误回喂模型自我纠正，不整轮报废；
                        # 反复失败到顶切收尾轮交代。
                        plan_index += 1
                    repeat_notes = [pending_repeat_reminder, *pending_loop_notes] if pending_repeat_reminder else pending_loop_notes
                    if repeat_notes:
                        # 跟在本批工具结果之后、下一次模型请求之前，作为合成
                        # 用户消息注入；只提醒，不否决模型接下来的选择。
                        provider_messages.append(
                            {"role": "user", "content": "\n\n".join(repeat_notes)}
                        )
                    if candidate_submission_for_run is not None:
                        async with self._terminal_project_lock_for_run(run_id):
                            await self._require_current_project_kb_for_run(run_id)
                            candidate = await asyncio.to_thread(
                                self.store.build_agent_candidate_output,
                                run_id,
                                candidate_submission_for_run,
                            )
                            await self._await_terminal_store_operation(
                                self.store.complete_run_with_candidate_output,
                                candidate,
                                candidate_tool_call_id=stored_call.tool_call_id,
                                candidate_tool_output=(
                                    executed.candidate_tool_output
                                    if executed.candidate_tool_output is not None
                                    else stored_call.output
                                ),
                                candidate_tool_duration_ms=(
                                    executed.candidate_tool_duration_ms
                                    if executed.candidate_tool_duration_ms is not None
                                    else stored_call.duration_ms
                                ),
                                message_id=message_id,
                                usage=total_usage,
                                current_project_kb_verifier=(
                                    self._require_current_project_kb_target_sync
                                ),
                            )
                        await self.broker.publish(run_id)
                        await self._maybe_name_session(
                            run_id,
                            current_run,
                            "".join(content_parts),
                        )
                        break
                    continue

                content = "".join(content_parts)
                if await self._consume_steering(
                    current_run, provider_messages, transcript_anchor, cancel_event,
                    preceding_message_id=message_id, preceding_content=content,
                ):
                    await self.broker.publish(run_id)
                    continue
                if await self._consume_command_completions(
                    current_run, provider_messages, transcript_anchor, cancel_event,
                    preceding_message_id=message_id, preceding_content=content,
                ):
                    continue
                if not content.strip():
                    raise AgentProviderError(
                        "Provider completed without a final assistant message",
                        code="agent_provider_empty_response",
                        retryable=True,
                    )
                if turn_resume is not None:
                    # 续写：已生成部分先作为一条完整消息落库（作者看得到、断线重连也不丢；
                    # 复读时落的是截好的那段，屏幕上转圈的几遍也换掉），再让模型从中断处接着写，不计入失败。
                    await asyncio.to_thread(
                        self.store.finalize_assistant_message,
                        run_id,
                        message_id=message_id,
                        content=content,
                    )
                    provider_messages.append({"role": "assistant", "content": content})
                    provider_messages.append(
                        {"role": "user", "content": _resume_prompt(turn_resume, content, loop_segment)}
                    )
                    await self._record_run_event(
                        run_id,
                        {
                            "truncated": "model_turn_output_truncated",
                            "stream_broken": "model_turn_stream_resumed",
                            "repeating": "model_turn_output_repeating",
                        }[turn_resume],
                        {
                            "turn": provider_turn,
                            "continued": True,
                            "kept_characters": len(content),
                            **({"repeated_segment": loop_segment[:120]} if loop_segment else {}),
                        },
                    )
                    await self.broker.publish(run_id)
                    continue
                if run_goal is not None:
                    run_goal = await asyncio.to_thread(self.store.get_run_goal, run_id)
                    if run_goal is None or run_goal.status != "active" or cancel_event.is_set():
                        raise AgentProviderCancelled()
                    if run_goal.requested_status is None:
                        next_messages = [*provider_messages, {"role": "assistant", "content": content}, goal_context(run_goal)]
                        await asyncio.to_thread(
                            self.store.finalize_assistant_message, run_id,
                            message_id=message_id, content=content, finish_reason=completed.finish_reason,
                            goal_transcript=self._current_transcript(next_messages, transcript_anchor),
                        )
                        provider_messages = next_messages
                        await self._record_run_event(run_id, "goal_continued", {
                            "goal_id": run_goal.goal_id, "stage": run_goal.stage_count + 1,
                        })
                        await self.broker.publish(run_id)
                        continue
                # 模型没有调用任何修改工具就正常收尾：这次请求按纯对话完成。
                # 不再强制补一个候选信封——「帮你改」类请求若模型只给了文字
                # 建议，作者看到的就是这条建议本身，可以接着说清楚再让它改。
                final_finish_reason = completed.finish_reason
                if final_finish_reason == "length":
                    await self._record_run_event(
                        run_id,
                        "model_turn_output_truncated",
                        {"turn": provider_turn, "continued": False},
                    )
                if provider_boundary is None:
                    try:
                        await asyncio.to_thread(
                            self.store.complete_run,
                            run_id,
                            message_id=message_id,
                            content=content,
                            finish_reason=final_finish_reason,
                            usage=total_usage,
                            allow_truncated_finish=final_finish_reason == "length",
                            transcript=[*self._current_transcript(provider_messages, transcript_anchor),
                                        {'role': 'assistant', 'content': content}],
                        )
                    except AgentSteeringPending:
                        steered_now = await self._consume_steering(
                            current_run, provider_messages, transcript_anchor, cancel_event,
                            preceding_message_id=message_id, preceding_content=content,
                        )
                        await self._consume_command_completions(
                            current_run, provider_messages, transcript_anchor, cancel_event,
                            preceding_message_id=message_id, preceding_content=content if not steered_now else "",
                        )
                        if cancel_event.is_set():
                            raise AgentProviderCancelled()
                        continue
                else:
                    async with self._terminal_project_lock_for_run(run_id):
                        # 预检：直接写过的 Run 走钉子核对，其余走 KB current
                        # （_require_current_project_kb_for_run 内部自分岔）。
                        await self._require_current_project_kb_for_run(run_id)
                        # 事务内 verifier 同理：直接写过的 Run 换钉子核对；
                        # 资料包没绑知识库快照（普通对话）就不核对，回答照存。
                        kb_verifier: CurrentProjectKbVerifier | None = None
                        if await self._run_binds_project_kb(run_id):
                            kb_verifier = (
                                self._direct_write_pin_verifier(run_id)
                                if await asyncio.to_thread(
                                    self._run_applied_direct_writes, run_id
                                )
                                else self._kb_sync_verifier
                            )
                        await self._await_terminal_store_operation(
                            self.store.complete_run,
                            run_id,
                            message_id=message_id,
                            content=content,
                            finish_reason=final_finish_reason,
                            usage=total_usage,
                            current_project_kb_verifier=kb_verifier,
                            allow_truncated_finish=final_finish_reason == "length",
                            transcript=[*self._current_transcript(provider_messages, transcript_anchor),
                                        {"role": "assistant", "content": content}],
                        )
                await self.broker.publish(run_id)
                if current_run.origin_kind == "user":
                    await self._maybe_name_session(run_id, current_run, content)
                    await self._maybe_remember(run_id, current_run, content)
                # 作者已经拿到回答，这次 Run 到此为止。趁收尾把过长的历史
                # 折成摘要，无论是小说资料包还是随便聊，下一轮都能接上摘要。
                await self._maybe_fold_session_history(
                    run_id,
                    current_run.session_id,
                    window_spec=window_spec,
                )
                break
        except AgentProviderCancelled:
            if self._closing:
                await self._preserve_workflow_on_shutdown(run_id)
            else:
                await self._mark_cancelled(run_id)
        except asyncio.CancelledError:
            if self._closing:
                await asyncio.shield(
                    self._preserve_workflow_on_shutdown(run_id)
                )
            else:
                await asyncio.shield(self._mark_cancelled(run_id))
            raise
        except AgentProviderError as exc:
            if self._closing:
                await self._preserve_workflow_on_shutdown(run_id)
            elif cancel_event.is_set():
                await self._mark_cancelled(run_id)
            else:
                await self._fail(
                    run_id,
                    code=exc.code,
                    message=_author_facing_provider_failure(exc),
                    retryable=exc.retryable,
                    details=exc.details,
                )
        except AgentContextPackNotFound as exc:
            await self._fail(
                run_id,
                code="context_pack_not_found",
                message=str(exc),
                retryable=False,
            )
        except AgentContextPackSourceUnavailable as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        except AgentContextPackConflict as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=exc.code in RETRYABLE_AGENT_CONTEXT_PACK_CONFLICT_CODES,
            )
        except AgentWorkflowCheckpointConflict as exc:
            await self._fail(
                run_id,
                code=exc.code,
                message=str(exc),
                retryable=False,
            )
        except AgentStoreConflict as exc:
            await self._fail(
                run_id,
                code=getattr(exc, "code", "agent_store_conflict"),
                message=str(exc),
                retryable=False,
            )
        except AgentStoreInvalidState as exc:
            await self._fail(
                run_id,
                code="agent_state_invalid",
                message=str(exc),
                retryable=False,
            )
        except Exception as exc:
            # 走到这里的是程序自己的 bug（没被上面任何一类接住），重试不会好，
            # 如实说「程序出错了」，别再显示成「可以重试」。
            await self._fail(
                run_id,
                code="agent_execution_failed",
                message=f"程序出错了：{exc}" if str(exc) else "程序出错了（没有更多信息）",
                retryable=False,
            )
        finally:
            try:
                await self._await_terminal_store_operation(
                    self._save_interrupted_transcript, run_id, provider_messages,
                    transcript_anchor,
                    "" if any(message.get("role") == "assistant" for message in provider_messages[response_start:])
                    else "".join(content_parts),
                )
            except Exception as exc:
                await self._record_run_event(
                    run_id, "run_transcript_failed", {"reason": str(exc)[:300] or exc.__class__.__name__}
                )
            finally:
                if project_kb_lease is not None:
                    project_kb_lease.__exit__(None, None, None)
                current = asyncio.current_task()
                if self._tasks.get(run_id) is current:
                    self._tasks.pop(run_id, None)
                    self._cancel_events.pop(run_id, None)

    @staticmethod
    def _writer_target_files(
        snapshot: ProjectInspectionResponse,
        *,
        mode: str,
        active_chapter_id: str | None,
        manual_file_ids: tuple[str, ...] = (),
    ) -> tuple[WriterTargetFile, ...]:
        if mode == "repair_only":
            return ()
        if mode != "author_review_change_proposal":
            raise AgentProviderError(
                "Writer intent mode is unsupported",
                code="writer_task_mode_invalid",
                retryable=False,
            )
        files_by_id = {item.file_id: item for item in snapshot.files}
        selected_manual_ids = tuple(dict.fromkeys(manual_file_ids))
        missing_manual_ids = [
            file_id for file_id in selected_manual_ids if file_id not in files_by_id
        ]
        if missing_manual_ids:
            raise AgentProviderError(
                "作者选中的资料已经不在当前作品快照中",
                code="writer_task_target_invalid",
                retryable=False,
            )
        # The picker sends prose and explicitly selected project files in one
        # list. Supporting chapter files are not included by the chapter tab,
        # so every selected file here is an intentional edit target.
        if selected_manual_ids:
            try:
                targets = tuple(
                    sorted(
                        (
                            WriterTargetFile(
                                file_id=file_id,
                                relative_path=files_by_id[file_id].relative_path,
                                content_hash=files_by_id[file_id].sha256,
                                content_role=(
                                    "prose_body"
                                    if files_by_id[file_id].kind == "prose"
                                    else "project_file"
                                ),
                            )
                            for file_id in selected_manual_ids
                        ),
                        key=lambda item: item.relative_path.casefold(),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise AgentProviderError(
                    "作者选中的资料不适合作为修改目标",
                    code="writer_task_target_invalid",
                    retryable=False,
                ) from exc
            if len(targets) > 32:
                raise AgentProviderError(
                    "一次修改最多关联 32 份资料",
                    code="writer_task_target_limit",
                    retryable=False,
                )
            return targets

        if active_chapter_id is None:
            raise AgentProviderError(
                "Author-review Writer mode requires an active chapter or selected project files",
                code="writer_task_target_missing",
                retryable=False,
            )

        chapters = [
            chapter
            for chapter in snapshot.chapters
            if chapter.chapter_id == active_chapter_id
        ]
        if len(chapters) != 1 or not chapters[0].prose_file_ids:
            raise AgentProviderError(
                "Active chapter has no unambiguous prose target",
                code="writer_task_target_missing",
                retryable=False,
            )
        try:
            targets = tuple(
                sorted(
                    (
                        WriterTargetFile(
                            file_id=file_id,
                            relative_path=files_by_id[file_id].relative_path,
                            content_hash=files_by_id[file_id].sha256,
                            content_role="prose_body",
                        )
                        for file_id in chapters[0].prose_file_ids
                    ),
                    key=lambda item: item.relative_path.casefold(),
                )
            )
        except (KeyError, ValueError) as exc:
            raise AgentProviderError(
                "Active chapter prose target failed Runtime validation",
                code="writer_task_target_invalid",
                retryable=False,
            ) from exc
        if len(targets) > 32:
            raise AgentProviderError(
                "Active chapter exceeds the Writer target-file limit",
                code="writer_task_target_limit",
                retryable=False,
            )
        return targets

    @staticmethod
    def _writer_task_provider_message(writer_task) -> dict[str, object]:
        task_payload = writer_task.model_dump(mode="json", by_alias=True)
        dispatch = {
            "schema": "zhimeng.writer-task-dispatch.v1",
            "writer_task": task_payload,
            "runtime_adds_writer_task_reference": True,
            "required_tool_sequence": (
                ["change_set_propose", "candidate_output_submit"]
                if writer_task.mode == "author_review_change_proposal"
                else ["candidate_output_submit"]
            ),
            "required_candidate_kind": (
                "change_proposal"
                if writer_task.mode == "author_review_change_proposal"
                else "repair_plan"
            ),
            "project_truth_write_allowed": False,
        }
        return {
            "role": "system",
            "content": json.dumps(
                dispatch,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    @staticmethod
    def _agent_decision_guidance_provider_message() -> dict[str, object]:
        # 上面的 writer-task 分发把「提案→提交候选」描述成了 required 序列，
        # 那是能力路径说明，不是强制任务。这条独立系统消息用作者听得懂的
        # 话向模型澄清边界：只回答完全合法；提了案必须收尾。保持原有的
        # 分发 JSON 键位不动（e2e 钉死了它们），职责拆给后到的这条消息。
        return {
            "role": "system",
            "content": (
                "上面的 Writer 任务是你的能力边界，不是强制改稿任务。作者的这句话可能只是提问、"
                "聊天或想了解情况——那就直接用文字回答，不要改动任何文件，也不要调用 candidate_output_submit。"
                "只有当你判断作者明确要求修改或生成正文、设定、大纲等文件时，才先调用 change_set_propose "
                "给出可审阅的完整改动（必须覆盖全部目标文件），成功之后再单独调用一次 "
                "candidate_output_submit（kind=change_proposal）收尾。只有方向性信息缺失（作者意图不明、"
                "目标文件找不到）时，才先用一句话问清关键的那一点再动手；其余情况直接做，不要反复请示。"
                "没有提案就不要提交候选。"
            ),
        }

    @staticmethod
    def _creation_instruction_provider_message(
        materialization,
    ) -> dict[str, object]:
        dispatch = {
            "schema": "zhimeng.creation-instruction-dispatch.v1",
            "materialization": materialization.model_dump(
                mode="json",
                by_alias=True,
            ),
            "runtime_adds_materialization_reference": True,
            "runtime_owns_workflow_and_project_writes": True,
            "formal_draft_allowed": False,
            "requires_author_review": True,
        }
        return {
            "role": "system",
            "content": json.dumps(
                dispatch,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    async def _with_skill_catalog(
        self, tool_definitions: tuple[ProviderToolDefinition, ...]
    ) -> tuple[ProviderToolDefinition, ...]:
        """把技能目录接在 skill_read_file 的说明后面：名字 + 什么时候用，全文模型用到再读。

        所有对话一样（纠偏指令 3.10）；不另发一段话。目录按名字排好，一场对话里技能没变就一字不差，
        工具定义这段前缀的缓存照样命中。
        """
        service = self.user_skills
        if service is None or not any(
            definition.name == "skill_read_file" for definition in tool_definitions
        ):
            return tool_definitions
        try:
            entries = await asyncio.to_thread(service.menu)
        except Exception:
            return tool_definitions
        if not entries:
            return tool_definitions
        lines: list[str] = []
        names_only: list[str] = []
        used = 0
        for entry in entries:
            description = " ".join(entry.description.split())
            if len(description) > SKILL_CATALOG_DESCRIPTION_CHARS:
                description = description[: SKILL_CATALOG_DESCRIPTION_CHARS - 1] + "…"
            line = f"- {entry.name}: {description}"
            if names_only or used + len(line) + 1 > SKILL_CATALOG_BUDGET_CHARS:
                names_only.append(entry.name)
                continue
            lines.append(line)
            used += len(line) + 1
        catalog = "\n".join(lines)
        if names_only:
            catalog += (
                "\nMore skills (skill_list_files shows what they are for): "
                + ", ".join(names_only)
            )
        description = f"{SKILL_READ_FILE_DESCRIPTION}\n\nSkills on this computer:\n{catalog}"
        if len(description) > 4096:  # ProviderToolDefinition.description 的上限；技能上百个时才会碰到
            description = description[:4095] + "…"
        return tuple(
            definition.model_copy(update={"description": description})
            if definition.name == "skill_read_file"
            else definition
            for definition in tool_definitions
        )

    async def _with_skill_declared_tools(
        self, tool_definitions: tuple[ProviderToolDefinition, ...]
    ) -> tuple[ProviderToolDefinition, ...]:
        """随技能来的工具（纠偏指令 3.8）：启用的技能在 SKILL.md 的 allowed-tools 里写了，才给模型。

        ai_smell_score 不再是内核默认工具：《小说创作》声明了它才有，停用这个技能就没了。
        9-26 真机试过「读过技能才给」：grok-4.6 要测 AI 味时不去读《小说创作》，自己编了个分数
        （两次，一次去翻了《小说蒸馏》），本机检测器一次没用上——所以只看声明，不看读没读。
        """
        executor = self.tool_executor
        available = {
            definition.name: definition
            for definition in (executor.definitions if executor is not None else ())
            if definition.name in SKILL_GATED_TOOL_NAMES
        }
        present = {definition.name for definition in tool_definitions if definition.name in SKILL_GATED_TOOL_NAMES}
        if not available and not present:
            return tool_definitions
        wanted: set[str] = set()
        service = self.user_skills
        if service is not None and available:
            try:
                for entry in await asyncio.to_thread(service.menu):
                    wanted |= set(entry.tools) & set(available)
            except Exception:
                wanted = set(present)
        if wanted == present:
            return tool_definitions
        kept = [definition for definition in tool_definitions if definition.name not in SKILL_GATED_TOOL_NAMES]
        kept.extend(available[name] for name in sorted(wanted))
        return tuple(sorted(kept, key=lambda definition: definition.name))

    def _manual_skills_provider_messages(self, run_id: str) -> list[dict[str, object]]:
        """作者在这条消息上用 / 点名的技能（引用标签）：发一条指路消息，发完即弃。

        随便聊同样适用——这是作者主动引用，不是每轮硬塞的菜单
        （作者 2026-09-20：「不应该是引用这个技能吗？就跟 Codex 一样」）。
        消息不进历史，下一条没点名的消息照旧零内部提示词。
        """

        names = self._run_skill_names.pop(run_id, None)
        if not names:
            return []
        name_lines = "\n".join(f"- {name}" for name in names)
        return [
            {
                "role": "system",
                "content": (
                    "用户这条消息点名要参考下面的技能，它们的 SKILL.md 用 skill_read_file 读。\n"
                    f"{name_lines}"
                ),
            }
        ]

    async def _environment_provider_messages(
        self, current_run: Run, *, replayed: Sequence[Mapping[str, object]] = ()
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        """开头那段环境事实（日期、挡位取这场对话第一轮的），和这一轮变了时追加在末尾的一条。

        和偏好文件一样是锦上添花：哪一项查不到就按这一轮自己的算，不让对话因此跑不起来。
        ``replayed`` 是这一轮拼好的历史：前几轮原样带回来的往来里补过的几条也算模型已经知道的，没再变就不补。
        """

        folder: str | None = None
        database = getattr(self.store, "database", None)
        if current_run.project_revision is not None and database is not None:
            try:
                binding = await asyncio.to_thread(
                    database.get_project_binding,
                    current_run.project_revision.project_id,
                )
            except Exception:
                binding = None
            if binding is not None:
                folder = str(binding.root_path)

        async def writer_mode(run_id: str) -> str | None:
            try:
                return await asyncio.to_thread(self.store.get_writer_mode, run_id)
            except Exception:
                return None

        first_run_id, started_at = current_run.run_id, current_run.created_at
        session_first_run = getattr(self.store, "session_first_run", None)
        if callable(session_first_run):
            try:
                first = await asyncio.to_thread(session_first_run, current_run.session_id)
            except Exception:
                first = None
            if first is not None:
                first_run_id, started_at = first
        first_mode = await writer_mode(first_run_id)
        current_mode = first_mode if first_run_id == current_run.run_id else await writer_mode(current_run.run_id)
        facts = environment_context.EnvironmentFacts(
            folder=folder,
            started_at=started_at,
            tier=environment_context.tier_text(first_mode),
        )
        opening = environment_context.opening_message(facts, home=environment_context.home_folder())
        update = environment_context.update_since(
            facts,
            now=datetime.now(UTC),
            tier=environment_context.tier_text(current_mode),
            stated=[
                str(message.get("content"))
                for message in replayed
                if isinstance(message.get("content"), str)
                and environment_context.is_update_content(str(message.get("content")))
            ],
        )
        return opening, update

    def _memory_enabled(self) -> bool:
        """设置页「记忆」的开关；无法确认设置时暂停用户记忆。"""

        try:
            return memory_enabled(Path(self.store.database.path).parent)
        except Exception:
            return False

    def _user_memory_snapshot(self) -> tuple[str, bool]:
        """Return memory text and whether the read was trustworthy.

        An unreadable file is different from an empty file. The former must not
        be persisted as an empty opening context or reported to the Provider as
        a user decision to clear memory.
        """

        try:
            state_root = Path(self.store.database.path).parent
            if not memory_enabled(state_root, strict=True):
                return "", True
            return read_user_memory(state_root, strict=True).strip(), True
        except Exception:
            return "", False

    def _user_memory_text(self) -> str:
        return self._user_memory_snapshot()[0]

    @staticmethod
    def _user_memory_message(content: str) -> dict[str, object] | None:
        if not content:
            return None
        return {"role": "user", "content": f"用户记忆（以前的对话里记下的）：\n{content}"}

    def _user_memory_provider_message(self) -> dict[str, object] | None:
        """《用户记忆.md》的注入消息：一条 role="user" 消息，空文件不注入。"""

        content, readable = self._user_memory_snapshot()
        return self._user_memory_message(content) if readable else None

    async def _project_instruction_snapshot(self, current_run: Run) -> ProjectInstructionSnapshot:
        """Keep missing, empty and unavailable project instructions distinct."""

        if current_run.project_revision is None:
            return ProjectInstructionSnapshot()
        project_id = current_run.project_revision.project_id
        database = getattr(self.store, "database", None)
        if database is None:
            return ProjectInstructionSnapshot(available=False, project_id=project_id)
        try:
            binding = await asyncio.to_thread(
                database.get_project_binding, project_id
            )
            return await asyncio.to_thread(
                read_project_instruction_snapshot, Path(binding.root_path), project_id=project_id
            )
        except Exception:
            return ProjectInstructionSnapshot(available=False, project_id=project_id)

    @staticmethod
    def _project_instructions_message(found: Sequence[Sequence[str]]) -> dict[str, object] | None:
        if not found:
            return None
        blocks = "\n\n".join(f"── {name} ──\n{content}" for name, content in found)
        return {"role": "user", "content": f"这个文件夹里的说明文件：\n\n{blocks}"}

    async def _opening_context_messages(
        self, current_run: Run, *, replayed: Sequence[Mapping[str, object]] = ()
    ) -> tuple[list[dict[str, object]], dict[str, object] | None]:
        """对话开头的用户记忆和说明文件（这场对话开始时的那份），和这一轮在末尾追加的「后来改过」那一条。

        9-27 照 Kimi Code：以前每轮重读、放在开头第 2、3 条，模型写一次记忆（或作者改了 AGENTS.md），
        下一轮开头就变，整场对话的提示缓存作废一次。现在开头那份一场对话里不变，改过的在末尾说。
        9-28 起前几轮的往来原样带回来（``replayed``），上一轮说过的「后来改过」也在里面：和上次说的一样就不再说。
        """

        current_memory, current_memory_readable = self._user_memory_snapshot()
        instruction_snapshot = await self._project_instruction_snapshot(current_run)
        current = {
            "user_memory": current_memory,
            "user_memory_readable": current_memory_readable,
            "project_instructions": [list(item) for item in instruction_snapshot.files],
            "project_instruction_state": instruction_snapshot.metadata(),
        }
        opening: dict[str, Any] | None = None
        read_opening = getattr(self.store, "session_opening_context", None)
        if callable(read_opening):
            try:
                opening = await asyncio.to_thread(read_opening, current_run.session_id)
            except Exception:
                opening = None
            if opening is None:
                try:
                    await asyncio.to_thread(self.store.record_opening_context, current_run.run_id, current)
                except Exception:
                    pass
        if opening is None:
            opening = current
        opening_memory = str(opening.get("user_memory") or "")
        # Older opening_context artifacts predate this flag and were created
        # only after a successful read, so they remain readable by default.
        opening_memory_readable = bool(opening.get("user_memory_readable", True))
        opening_files = opening.get("project_instructions") or []
        opening_instruction_state = opening.get("project_instruction_state")
        opening_instruction_message = (
            ProjectInstructionSnapshot.from_metadata(opening_files, opening_instruction_state).message()
            if isinstance(opening_instruction_state, dict)
            else self._project_instructions_message(opening_files)
        )
        memory_on = self._memory_enabled()
        messages = [
            message
            for message in (
                self._user_memory_message(opening_memory)
                if memory_on and opening_memory_readable
                else None,
                opening_instruction_message,
            )
            if message is not None
        ]
        changes: list[str] = []

        def instructions_now() -> str:
            # Report confirmed absence for previously observed instructions,
            # without introducing default filenames that never existed here.
            known_names = {str(item[0]) for item in opening_files}
            if isinstance(opening_instruction_state, dict):
                states = opening_instruction_state.get("states")
                if isinstance(states, dict):
                    known_names.update(name for name, state in states.items() if state != "missing")
            return str(instruction_snapshot.message(current=True, missing_names=tuple(known_names))["content"])

        def memory_now() -> str:
            if not current_memory_readable:
                return "用户记忆或开关暂时无法读取，不能确认它是空的或已关闭；本轮不依据或覆盖这份记忆。"
            if not memory_on:
                return "用户在设置里把记忆关了：开头那份用户记忆不再作数。"
            return f"用户记忆现在是：\n{current_memory or '（空了）'}"

        memory_changed = (
            current_memory_readable != opening_memory_readable
            or (current_memory_readable and current_memory != opening_memory)
        )
        if memory_changed:
            changes.append(memory_now())
        instructions_changed = (
            current["project_instructions"] != [list(item) for item in opening_files]
            or (
                instruction_snapshot.metadata() != opening_instruction_state
                if isinstance(opening_instruction_state, dict)
                else instruction_snapshot.unreliable
            )
        )
        if instructions_changed:
            changes.append(instructions_now())
        last_stated = next(
            (
                str(message.get("content"))
                for message in reversed(replayed)
                if isinstance(message.get("content"), str)
                and str(message.get("content")).startswith(_OPENING_UPDATE_PREFIX)
            ),
            None,
        )
        if not changes and last_stated is not None:
            # 前面说过改了，现在又回到开头那样：照现在的说一遍，免得模型还按上次说的算。
            changes.append(memory_now())
            changes.append(instructions_now())
        update = (
            {"role": "user", "content": _OPENING_UPDATE_PREFIX + "\n\n" + "\n\n".join(changes)}
            if changes
            else None
        )
        if update is not None and update["content"] == last_stated:
            update = None
        return messages, update

    async def _project_instructions_provider_message(
        self, current_run: Run
    ) -> dict[str, object] | None:
        """打开的文件夹根目录里的说明文件（AGENTS.md、CLAUDE.md、老书的《写作笔记.md》），一条 role="user" 消息。"""

        return (await self._project_instruction_snapshot(current_run)).message()

    async def _open_folder_turn_messages(self, current_run: Run, *, input_override: dict | None = None) -> list[dict[str, object]]:
        """打开了文件夹、没带资料包的这一轮：用户开着哪个文件、亲手附上了哪些文件（纠偏指令 3.9）。

        以前每轮自动编资料包（当前章 + 前两章 + 设定 + 知识卡）塞进去；现在只说开着的是哪个文件，
        内容模型要用自己读。用户亲手附上的文件照 Claude Code 的 @文件 带上内容——这一轮带，不进历史。
        开着的文件 9-29 起前端直接给路径（open_file_path）；只给了章（chapter_id，老界面）时才从快照里找那一章的正文。
        """

        getter = getattr(self.store, "get_writer_focus", None)
        path_getter = getattr(self.store, "get_open_file_path", None)
        executor = self.tool_executor
        if (not callable(getter) and input_override is None) or executor is None:
            return []
        try:
            if input_override is not None:
                chapter_id, file_ids = None, input_override.get('manual_file_ids', [])
                open_path = input_override.get('open_file_path')
            else:
                chapter_id, file_ids = await asyncio.to_thread(getter, current_run.run_id)
                open_path = (
                    await asyncio.to_thread(path_getter, current_run.run_id)
                    if callable(path_getter)
                    else None
                )
        except Exception:
            return []
        if chapter_id is None and not file_ids and open_path is None:
            return []
        open_paths: list[str] = [open_path] if open_path is not None else []
        attached: list[tuple[str, str]] = []
        if file_ids or (chapter_id is not None and not open_paths):
            try:
                binding = await executor._binding(current_run, follow_own_writes=True)
                root = await asyncio.to_thread(executor.reader.resolve, binding.root_path)
                async with executor.locks.for_path(root):
                    snapshot = await executor._current_snapshot(
                        current_run, binding, follow_own_writes=True
                    )
                    raw_snapshot = snapshot.model_dump(mode="python", by_alias=True)
                    files_by_id = {item.file_id: item for item in snapshot.files}
                    if chapter_id is not None and not open_paths:
                        chapter = next(
                            (item for item in snapshot.chapters if item.chapter_id == chapter_id), None
                        )
                        if chapter is not None:
                            open_paths = [
                                files_by_id[file_id].relative_path
                                for file_id in chapter.prose_file_ids
                                if file_id in files_by_id
                            ]
                    budget = OPEN_FOLDER_ATTACHMENT_TOTAL_CHARS
                    for file_id in file_ids:
                        item = files_by_id.get(file_id)
                        if item is None or budget <= 0:
                            continue
                        try:
                            result = await asyncio.to_thread(
                                partial(
                                    executor.reader.read_text_file_from_snapshot,
                                    raw_snapshot,
                                    file_id,
                                    allowed_kinds=WRITING_CONTEXT_FILE_KINDS,
                                )
                            )
                        except Exception:
                            attached.append((item.relative_path, "（这个文件读不出来。）"))
                            continue
                        content = extracted_context_text(str(result.get("content", "")), result.get("extraction"))
                        limit = min(OPEN_FOLDER_ATTACHMENT_FILE_CHARS, budget)
                        if len(content) > limit:
                            content = content[:limit] + "\n（后面还有，没有带上；要看全文就读这个文件。）"
                        budget -= len(content)
                        attached.append((item.relative_path, content))
            except Exception:
                # 快照读不出来：附上的文件带不了；开着哪个文件前端给了路径的照样说。
                if open_path is None:
                    return []
                attached = []
        lines: list[str] = []
        if open_paths:
            lines.append(f"用户这时在编辑器里开着：{'、'.join(open_paths)}")
        for relative_path, content in attached:
            lines.append(f"用户附上的文件 {relative_path}：\n{content}")
        if not lines:
            return []
        return [{"role": "system", "content": "\n\n".join(lines)}]

    async def _has_managed_workflow(self, context_pack_id: str) -> bool:
        # 受管写作流水线（project-audit → prewrite → 候选提交）已经退役：
        # 桌面壳只发 chat 资料包，这里以前按 NLCL 路由的 profile 判断，
        # 前端一处默认值写错就会把整条三步子进程链复活。现在恒为 False，
        # 每一场对话都是普通 Agent 对话。整块流水线代码另行拆除。
        del context_pack_id
        return False

    def _workflow_runner(self) -> WorkflowRunner | None:
        context_store = self.context_store
        tool_executor = self.tool_executor
        skill_executor = (
            tool_executor.skill_executor if tool_executor is not None else None
        )
        if context_store is None or skill_executor is None:
            return None
        return WorkflowRunner(self.store, context_store, skill_executor)

    def _load_workflow_snapshot(
        self,
        run_id: str,
    ) -> tuple[
        Run,
        WorkflowPlan,
        tuple[WorkflowStepCheckpoint | ProviderIoBoundary, ...],
    ]:
        run = self.store.get_run(run_id)
        plan = self.store.get_workflow_plan(run_id)
        chain = tuple(self.store.list_workflow_checkpoints(run_id))
        return run, plan, chain

    async def _preserve_workflow_on_shutdown(self, run_id: str) -> None:
        """Pause pre-Provider evidence owned by this task during graceful shutdown."""

        try:
            run = await asyncio.to_thread(self.store.get_run, run_id)
            await asyncio.to_thread(self.store.get_workflow_plan, run_id)
        except AgentStoreNotFound:
            await self._mark_interrupted(run_id)
            return
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        if run.status is RunStatus.PAUSED:
            return

        try:
            await asyncio.to_thread(self.store.get_provider_io_boundary, run_id)
        except AgentStoreNotFound:
            pass
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        else:
            await self._mark_interrupted(run_id)
            return

        runner = self._workflow_runner()
        if runner is not None:
            try:
                await asyncio.to_thread(runner.reconcile_startup, run_id)
            except (AgentStoreError, ContextStoreError, AgentProviderError):
                await self._mark_interrupted(run_id)
                return
        try:
            await asyncio.to_thread(self.store.pause_workflow_for_restart, run_id)
        except AgentWorkflowCheckpointConflict:
            # A boundary may win the race while its database write finishes.
            await self._mark_interrupted(run_id)
            return
        except AgentStoreError:
            await self._mark_interrupted(run_id)
            return
        await self.broker.publish(run_id)

    @staticmethod
    def _require_resumable_knowledge(
        observation: KnowledgeFingerprintObservation | None,
    ) -> None:
        if observation is None or (
            observation.state is KnowledgeObservationState.UNAVAILABLE
        ):
            raise AgentContextPackSourceUnavailable(
                "Context Pack knowledge source could not be verified before dispatch"
            )
        if observation.state is KnowledgeObservationState.CHANGED:
            raise AgentContextPackConflict(
                "Context Pack knowledge source changed before dispatch",
                code="context_pack_source_changed",
            )

    async def _persist_provider_io_boundary(
        self,
        run: Run,
        workflow: WorkflowExecutionResult,
        *,
        provider_messages: list[dict[str, object]],
        tool_definitions: tuple[ProviderToolDefinition, ...],
        provider: AgentProvider,
        accepted_reported_models: tuple[str, ...],
    ) -> ProviderIoBoundary:
        dispatch_config_fingerprint = self._require_bound_provider(run, provider)
        provider_model = self._provider_requested_model(provider)
        request_hash = self._provider_dispatch_material_hash(
            provider_messages=provider_messages,
            tool_definitions=tool_definitions,
            provider=provider,
            dispatch_config_fingerprint=dispatch_config_fingerprint,
            accepted_reported_models=accepted_reported_models,
        )
        boundary = build_provider_io_boundary(
            workflow.plan,
            workflow.checkpoints,
            request_hash=request_hash,
            provider_model=provider_model,
            provider_dispatch_config_fingerprint=dispatch_config_fingerprint,
            executor=provider.identity,
            created_at=datetime.now(UTC),
        )
        await asyncio.to_thread(
            self.store.create_provider_io_boundary,
            boundary,
            expected_previous_checkpoint_hash=workflow.checkpoints[-1].checkpoint_hash,
            provider_messages_hash=canonical_json_hash(provider_messages),
            tool_definitions_hash=canonical_json_hash(
                [
                    item.model_dump(mode="json", by_alias=True)
                    for item in tool_definitions
                ]
            ),
        )
        await self.broker.publish(run.run_id)
        return boundary

    @staticmethod
    def _provider_requested_model(provider: AgentProvider) -> str | None:
        value = getattr(provider, "model", None)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
            raise AgentProviderError(
                "Writing Agent provider has no valid requested model identity",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        return value.strip()

    @staticmethod
    def _provider_accepted_reported_models(
        provider: AgentProvider,
    ) -> tuple[str, ...]:
        raw = getattr(provider, "accepted_reported_models", ())
        if not isinstance(raw, (tuple, list)):
            raise AgentProviderError(
                "Writing Agent provider has an invalid reported-model allowlist",
                code="provider_dispatch_config_invalid",
                retryable=False,
            )
        normalized: list[str] = []
        for value in raw:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
                raise AgentProviderError(
                    "Writing Agent provider has an invalid reported-model allowlist",
                    code="provider_dispatch_config_invalid",
                    retryable=False,
                )
            candidate = value.strip()
            if candidate not in normalized:
                normalized.append(candidate)
        return tuple(sorted(normalized))

    @classmethod
    def _provider_dispatch_material_hash(
        cls,
        *,
        provider_messages: list[dict[str, object]],
        tool_definitions: tuple[ProviderToolDefinition, ...],
        provider: AgentProvider,
        dispatch_config_fingerprint: str,
        accepted_reported_models: tuple[str, ...],
    ) -> str:
        return canonical_json_hash(
            {
                "schema": "zhimeng.provider-dispatch-material.v1",
                "messages": provider_messages,
                "tools": [
                    item.model_dump(mode="json", by_alias=True)
                    for item in tool_definitions
                ],
                "provider_model": cls._provider_requested_model(provider),
                "accepted_reported_models": list(accepted_reported_models),
                "provider_dispatch_config_fingerprint": dispatch_config_fingerprint,
                "executor": provider.identity.model_dump(mode="json"),
            }
        )

    async def _execute_tool_call(
        self,
        run: Run,
        *,
        provider_tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        cancel_event: asyncio.Event,
    ) -> ToolExecutionResult:
        prepared = await self._prepare_tool_call(
            run,
            provider_tool_call_id=provider_tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        if isinstance(prepared, ToolExecutionResult):
            return prepared
        return await self._finalize_prepared_tool_call(run, prepared, cancel_event)

    async def _prepare_tool_call(
        self,
        run: Run,
        *,
        provider_tool_call_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> ToolExecutionResult | _PreparedToolCall:
        """begin 阶段：幂等回放直接给结果，否则落 running 记录待执行。"""
        tool_executor = self.tool_executor
        if tool_executor is None:
            raise AgentProviderError(
                "Runtime project tools are not configured",
                code="agent_provider_tool_call_unsupported",
                retryable=False,
            )
        tool_definitions = list(tool_executor.definitions)
        candidate_definition = getattr(
            tool_executor,
            "candidate_output_definition",
            None,
        )
        if candidate_definition is not None:
            tool_definitions.append(candidate_definition)
        tool_versions = {item.name: item.version for item in tool_definitions}
        mcp_adapter = getattr(tool_executor, "mcp_adapter", None)
        if mcp_adapter is not None:
            tool_versions.update(mcp_adapter.versions())
        runtime_tool_call_id = self._runtime_tool_call_id(
            run.run_id,
            provider_tool_call_id,
        )
        tool_version = tool_versions.get(tool_name, "1")
        request_hash = tool_executor.request_hash(
            tool_name,
            tool_version,
            arguments,
        )
        stored_call, replayed = await asyncio.to_thread(
            self.store.begin_tool_call,
            run.run_id,
            tool_call_id=runtime_tool_call_id,
            tool_name=tool_name,
            tool_version=tool_version,
            arguments=arguments,
            request_hash=request_hash,
            idempotency_key=runtime_tool_call_id,
        )
        await self.broker.publish(run.run_id)
        if replayed:
            if stored_call.status not in {"succeeded", "failed", "cancelled"}:
                raise AgentProviderError(
                    "Provider repeated an unfinished ToolCall identity",
                    code="agent_tool_call_in_progress",
                    retryable=False,
                )
            candidate_submission = None
            if stored_call.tool_name == CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME:
                try:
                    candidate_submission = AgentCandidateSubmission.model_validate_json(
                        json.dumps(
                            stored_call.arguments,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        strict=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise AgentProviderError(
                        "Persisted candidate submission arguments are invalid",
                        code="agent_candidate_output_integrity_failed",
                        retryable=False,
                    ) from exc
            return ToolExecutionResult(
                tool_call=stored_call,
                replayed=True,
                candidate_submission=candidate_submission,
            )
        return _PreparedToolCall(
            stored_call=stored_call,
            runtime_tool_call_id=runtime_tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            request_hash=request_hash,
        )

    async def _finalize_prepared_tool_call(
        self,
        run: Run,
        prepared: _PreparedToolCall,
        cancel_event: asyncio.Event,
    ) -> ToolExecutionResult:
        tool_executor = self.tool_executor
        if tool_executor is None:  # pragma: no cover - prepare 已校验。
            raise AgentProviderError(
                "Runtime project tools are not configured",
                code="agent_provider_tool_call_unsupported",
                retryable=False,
            )
        stored_call = prepared.stored_call
        runtime_tool_call_id = prepared.runtime_tool_call_id
        tool_name = prepared.tool_name
        arguments = prepared.arguments
        request_hash = prepared.request_hash
        started = monotonic()
        output: object = {}
        error: ErrorEnvelope | None = None
        status: str = "succeeded"
        change_proposal: ChangeProposalPayload | None = None
        skill_call: SkillCallPayload | None = None
        artifacts: tuple[ArtifactPayload, ...] = ()
        candidate_submission: AgentCandidateSubmission | None = None
        try:
            result = await tool_executor.execute(
                run,
                tool_call_id=runtime_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                request_hash=request_hash,
                cancel_event=cancel_event,
            )
            output = result.output
            if getattr(result, "error_code", None):
                status = "failed"
                error = self._tool_error(run.run_id, code=result.error_code,
                                         message=result.error_message or "工具返回了失败结果。", retryable=False)
            # 调用工具成功不等于它执行的命令成功。保留stdout/stderr供模型
            # 自行诊断和继续工作，实际退出结果同时写入工具记录和界面。
            if tool_name in COMMAND_TOOL_NAMES and isinstance(output, dict):
                exit_code = output.get("exit_code")
                timed_out = output.get("timed_out") is True
                process_status = output.get("status")
                if not timed_out and process_status == "cancelled":
                    # Stopping a process (or reading its stopped receipt) is a
                    # completed operation, while its process outcome is cancelled.
                    # ToolCall cancelled is reserved for cancelling this Run: the
                    # main loop deliberately stops before requesting another reply.
                    pass
                elif process_status == "interrupted":
                    status = "failed"
                    error = self._tool_error(
                        run.run_id, code="agent_command_interrupted",
                        message="命令会话已中断，保留的日志可继续查看。", retryable=False,
                    )
                elif timed_out or (isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0):
                    status = "failed"
                    error = self._tool_error(
                        run.run_id,
                        code="agent_command_timeout" if timed_out else "agent_command_nonzero_exit",
                        message="命令等待超时，已停止执行。" if timed_out else f"命令以退出码 {exit_code} 结束。",
                        retryable=False,
                    )
            skill_call = result.skill_call
            artifacts = result.artifacts
            candidate_submission = result.candidate_submission
            if result.change_set is not None:
                change_proposal = self._change_proposal(result.change_set)
        except asyncio.CancelledError as cancellation:
            status = "cancelled"
            output = {"cancelled": True}
            mcp_adapter = getattr(tool_executor, "mcp_adapter", None)
            if mcp_adapter is not None and mcp_adapter.lookup(tool_name) is not None:
                output.update(mcp_adapter.result_metadata(tool_name))
                output["side_effects"] = "unconfirmed"

                async def finish_mcp_cancellation() -> None:
                    # The external request has already been cancelled. This task
                    # only observes local files and persists the observation.
                    if run.project_revision is not None and output["read_only"] is not True:
                        pin = await tool_executor._command_result_pin(run)
                        if pin is not None:
                            output["result_revision"], output["result_fingerprint"] = pin
                    observed = await asyncio.to_thread(self.store.get_tool_call, runtime_tool_call_id)
                    duration = max(0, int((monotonic() - started) * 1000))
                    if observed.status not in {"succeeded", "failed", "cancelled"}:
                        try:
                            await asyncio.to_thread(
                                self.store.finish_tool_call, run.run_id, tool_call_id=runtime_tool_call_id,
                                status="cancelled", output=output, error=None, duration_ms=duration,
                            )
                        except AgentStoreConflict:
                            observed = await asyncio.to_thread(self.store.get_tool_call, runtime_tool_call_id)
                            if observed.status != "cancelled":
                                raise
                    if observed.status == "cancelled":
                        await asyncio.to_thread(
                            self.store.observe_cancelled_mcp_call, run.run_id, tool_call_id=runtime_tool_call_id,
                            output=output, duration_ms=duration,
                        )
                    await self.broker.publish(run.run_id)

                # Repeated Stop requests must not let the outer Run publish its
                # terminal event before this local observation is committed.
                cleanup = asyncio.create_task(finish_mcp_cancellation())
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        continue
                raise cancellation
            try:
                stored_after_cancel = await asyncio.shield(
                    asyncio.to_thread(
                        self.store.get_tool_call,
                        runtime_tool_call_id,
                    )
                )
                if stored_after_cancel.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    await asyncio.shield(
                        asyncio.to_thread(
                            self.store.finish_tool_call,
                            run.run_id,
                            tool_call_id=runtime_tool_call_id,
                            status="cancelled",
                            output=output,
                            error=None,
                            duration_ms=max(
                                0,
                                int((monotonic() - started) * 1000),
                            ),
                            change_proposal=None,
                        )
                    )
            except AgentStoreConflict:
                stored_after_cancel = await asyncio.shield(
                    asyncio.to_thread(
                        self.store.get_tool_call,
                        runtime_tool_call_id,
                    )
                )
                if stored_after_cancel.status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    # 裸 raise 在这里重抛的是 AgentStoreConflict，会把取消
                    # 语义顶掉（Run 被标成 conflict 失败而非 cancelled）；
                    # 必须显式重抛外层的取消。
                    raise cancellation from None
            await asyncio.shield(self.broker.publish(run.run_id))
            raise
        except AgentToolError as exc:
            status = exc.status
            skill_call = exc.skill_call
            artifacts = exc.artifacts
            if status == "failed":
                error = self._tool_error(
                    run.run_id,
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                )
                output = {}
            else:
                error = None
                output = {"cancelled": True}
        except Exception as exc:
            status = "failed"
            error = self._tool_error(
                run.run_id,
                code="agent_tool_execution_failed",
                message=str(exc) or "Runtime tool execution failed",
                retryable=True,
            )
            output = {}

        duration_ms = max(0, int((monotonic() - started) * 1000))
        if candidate_submission is not None and status == "succeeded":
            return ToolExecutionResult(
                tool_call=stored_call,
                replayed=False,
                candidate_submission=candidate_submission,
                candidate_tool_output=output,
                candidate_tool_duration_ms=duration_ms,
            )

        stored_call, _ = await asyncio.to_thread(
            self.store.finish_tool_call,
            run.run_id,
            tool_call_id=runtime_tool_call_id,
            status=status,
            output=output,
            error=error,
            duration_ms=duration_ms,
            change_proposal=change_proposal,
            skill_call=skill_call,
            artifacts=artifacts,
        )
        await self.broker.publish(run.run_id)
        return ToolExecutionResult(
            tool_call=stored_call,
            replayed=False,
            candidate_submission=candidate_submission,
        )

    async def _execute_readonly_segment(
        self,
        run: Run,
        calls: list[ProviderToolCall],
        cancel_event: asyncio.Event,
    ) -> list[ToolExecutionResult | BaseException]:
        """并行执行一批连续的只读工具调用，按原始顺序返回结果。

        begin 仍逐个串行落库（活动流里「开始」保持模型给出的调用顺序），
        只有执行与收尾并行；单个调用的意外失败被补偿成 failed 结果，
        不会拖垮同批其他调用。只读调用不写项目文件、不起子进程，
        并行不改变任何写语义。
        """
        prepared: list[ToolExecutionResult | _PreparedToolCall] = []
        for call in calls:
            prepared.append(
                await self._prepare_tool_call(
                    run,
                    provider_tool_call_id=call.tool_call_id,
                    tool_name=call.tool_name,
                    arguments=call.arguments,
                )
            )
        semaphore = asyncio.Semaphore(READONLY_TOOL_PARALLELISM)

        async def _run_one(
            item: ToolExecutionResult | _PreparedToolCall,
        ) -> ToolExecutionResult:
            if isinstance(item, ToolExecutionResult):
                return item
            async with semaphore:
                try:
                    return await self._finalize_prepared_tool_call(
                        run, item, cancel_event
                    )
                except (asyncio.CancelledError, AgentProviderCancelled):
                    raise
                except Exception as exc:
                    # 收尾路径的意外异常（如写库冲突）：尽力把 tool_call
                    # 补偿成 failed，形状与「执行器抛异常」一致，由调用方
                    # 按串行语义处理（失败计数 + retryable 上抛）。
                    try:
                        stored, _ = await asyncio.to_thread(
                            self.store.finish_tool_call,
                            run.run_id,
                            tool_call_id=item.runtime_tool_call_id,
                            status="failed",
                            output={},
                            error=self._tool_error(
                                run.run_id,
                                code="agent_tool_execution_failed",
                                message=str(exc) or "Runtime tool execution failed",
                                retryable=True,
                            ),
                            duration_ms=None,
                            change_proposal=None,
                        )
                    except Exception:
                        raise exc
                    return ToolExecutionResult(tool_call=stored, replayed=False)

        return await asyncio.gather(
            *(_run_one(item) for item in prepared),
            return_exceptions=True,
        )

    @staticmethod
    def _rejected_tool_result_message(
        provider_tool_call_id: str,
        *,
        code: str,
        message: str,
    ) -> dict[str, object]:
        payload = {
            "status": "failed",
            "output": {},
            "error": {"error": {"code": code, "message": message, "retryable": False}},
        }
        return {
            "role": "tool",
            "tool_call_id": provider_tool_call_id,
            "content": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    async def _compact_active_run(
        self, run, provider, messages, anchor, cancel_event, token_budget, turn, summary_token_budget,
    ) -> None:
        session_id = run.session_id
        self._compacting_sessions.add(session_id)
        self._compaction_done[session_id] = asyncio.Event()
        committed = False
        try:
            # Complete pending tool results before any summary I/O, so a hard exit
            # keeps the original evidence even if no summary was committed.
            await self._await_terminal_store_operation(self.store.save_run_transcript, run.run_id,
                                                      self._current_transcript(messages, anchor))
            await self._record_run_event(run.run_id, "run_context_compaction", {
                "state": "running", "turn": turn,
                "tokens_before": self._estimate_provider_messages_tokens(messages),
            })

            summary_provider = self._quick_provider(provider)
            summary_number = 0

            async def summarize(request):
                nonlocal summary_number
                summary_number += 1
                identity = {"turn": turn, "part": summary_number}
                await self._record_run_event(run.run_id, "run_context_summary_started", {
                    **identity, "estimated_input_tokens": self._estimate_provider_messages_tokens(request),
                    "requested_reasoning_effort": getattr(summary_provider, "reasoning_effort", None),
                })

                async def record_completion(completed):
                    await self._record_run_event(run.run_id, "run_context_summary_completed", {
                        **identity, "finish_reason": completed.finish_reason,
                        "reported_model": completed.reported_model,
                        "effective_reasoning_effort": completed.effective_reasoning_effort,
                        "usage": completed.usage.model_dump(mode="json") if completed.usage is not None else None,
                    })

                text, _ = await self._stream_checkpoint_text(
                    summary_provider, request, cancel_event=cancel_event, on_completed=record_completion,
                )
                return text

            candidate, stats = await compact_tool_results(
                messages, archive_root=Path(self.store.database.path).parent / "context-results",
                objective="\n\n".join(str(message.get("content", "")) for message in messages
                                        if message.get("role") == "user"),
                token_budget=token_budget, estimate=self._estimate_provider_messages_tokens,
                summarize=summarize, cancel_event=cancel_event,
                summary_token_budget=summary_token_budget,
            )
            if cancel_event.is_set():
                raise AgentProviderCancelled()
            operation = asyncio.create_task(asyncio.to_thread(self.store.commit_run_compaction, run.run_id,
                self._current_transcript(candidate, anchor), {**stats, "turn": turn}))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    if operation.cancelled():
                        raise
                    cancelled = True
            # Cancellation cannot let the terminal save overwrite a committed
            # compacted transcript with the previous, oversized in-memory copy.
            messages[:] = candidate
            committed = True
            if cancelled:
                raise asyncio.CancelledError
        except (asyncio.CancelledError, AgentProviderCancelled):
            if not committed:
                await self._record_run_event(run.run_id, "run_context_compaction", {"state": "cancelled", "turn": turn})
            raise
        except Exception as exc:
            await self._record_run_event(run.run_id, "run_context_compaction", {
                "state": "failed", "turn": turn, "reason": "本次整理未完成，原记录仍保留。",
            })
            if isinstance(exc, AgentProviderError):
                raise
            reason = "摘要生成等待超时" if isinstance(exc, TimeoutError) else str(exc) or exc.__class__.__name__
            raise AgentProviderError("运行中整理未完成，原记录仍保留：" + reason,
                                     code="run_context_compaction_failed", retryable=True) from exc
        finally:
            self._compacting_sessions.discard(session_id)
            self._compaction_done.setdefault(session_id, asyncio.Event()).set()

    @staticmethod
    def _estimate_provider_messages_tokens(
        provider_messages: list[dict[str, object]],
    ) -> int:
        """回喂对话的保守 token 估算：只用于预算判断，不进任何收据或哈希。"""
        total = 0
        for message in provider_messages:
            total += 4
            total += conservative_token_estimate(str(message.get("role") or ""))
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            if isinstance(content, str):
                total += conservative_token_estimate(content)
            raw_calls = message.get("tool_calls")
            if raw_calls:
                total += conservative_token_estimate(
                    json.dumps(raw_calls, ensure_ascii=False, default=str)
                )
        return total

    async def _stream_checkpoint_text(
        self,
        provider,
        request_messages: list[dict[str, str]],
        *, cancel_event: asyncio.Event | None = None,
        on_completed: Callable[[ProviderCompleted], Awaitable[None]] | None = None,
    ) -> tuple[str, str | None]:
        """发一次整理请求，收回整段摘要；没正常结束就报可重试的错（调用方记事件、这一次不折）。

        等多久按要整理的字数放宽：以前一次最多折 4 万字、一律 120 秒（9-28 base1 折 4 万字那次按最高思考强度超时了）；
        现在一次折完，6 万字约 240 秒，最多 300 秒。
        """
        size = sum(len(str(message.get("content") or "")) for message in request_messages)
        parts: list[str] = []
        completed = None
        async with asyncio.timeout(min(300, 120 + size // 500)):
            async for event in provider.stream_chat(request_messages, cancel_event=cancel_event if cancel_event is not None else asyncio.Event()):
                if cancel_event is not None and cancel_event.is_set():
                    raise AgentProviderCancelled()
                if isinstance(event, ProviderTextDelta):
                    parts.append(event.text)
                elif isinstance(event, ProviderCompleted):
                    completed = event
                    if on_completed is not None:
                        await on_completed(event)
        if completed is None or completed.finish_reason not in ("stop", "end_turn"):
            raise AgentProviderError("摘要生成未完整结束，请重试", code="session_checkpoint_incomplete", retryable=True)
        return "".join(parts), completed.reported_model

    @staticmethod
    def _current_transcript(provider_messages, anchor) -> list[dict[str, object]]:
        position = next((index for index, message in enumerate(provider_messages) if message is anchor), None)
        return list(provider_messages[position + 1:]) if position is not None else []

    async def _consume_steering(
        self, current_run: Run, provider_messages: list[dict[str, object]], anchor,
        cancel_event: asyncio.Event, *, preceding_message_id: str | None = None,
        preceding_content: str = '',
    ) -> bool:
        if cancel_event.is_set():
            return False
        pending_inputs = getattr(self.store, 'pending_steering_inputs', None)
        if not callable(pending_inputs):
            return False
        pending = await asyncio.to_thread(pending_inputs, current_run.run_id)
        if not pending:
            return False
        steering = AgentSteeringStore(self.store)
        prepared = []
        for item in pending:
            parts: list[dict[str, object]] = [provider_image_message('user', item.payload['message'],
                [{'image_id': image_id} for image_id in item.payload.get('image_ids', [])])]
            parts.extend(await self._open_folder_turn_messages(current_run, input_override=item.payload))
            names = item.payload.get('manual_skill_names', [])
            if names:
                parts.append({'role': 'system', 'content': '用户这条消息点名要参考下面的技能，它们的 SKILL.md 用 skill_read_file 读。\n'
                              + '\n'.join(f'- {name}' for name in names)})
            prepared.append((item.item_id, parts))
        if cancel_event.is_set():
            return False
        if preceding_message_id and preceding_content.strip():
            await asyncio.to_thread(self.store.finalize_assistant_message, current_run.run_id,
                message_id=preceding_message_id, content=preceding_content, finish_reason='stop')
            provider_messages.append({'role': 'assistant', 'content': preceding_content})
        appended = await asyncio.to_thread(steering.consume, current_run.run_id, prepared,
            self._current_transcript(provider_messages, anchor))
        if not appended:
            return False
        provider_messages.extend(appended)
        await self.broker.publish(current_run.run_id)
        return True

    async def _consume_command_completions(
        self, current_run: Run, provider_messages: list[dict[str, object]], anchor,
        cancel_event: asyncio.Event, *, preceding_message_id: str | None = None, preceding_content: str = "",
    ) -> bool:
        if cancel_event.is_set():
            return False
        pending_inputs = getattr(self.store, "pending_command_completions", None)
        if not callable(pending_inputs):
            return False
        pending = await asyncio.to_thread(pending_inputs, current_run.run_id)
        if not pending:
            return False
        if preceding_message_id and preceding_content.strip():
            await asyncio.to_thread(self.store.finalize_assistant_message, current_run.run_id,
                                   message_id=preceding_message_id, content=preceding_content, finish_reason="stop")
            provider_messages.append({"role": "assistant", "content": preceding_content})
        # Completion may have mutated project files. Use the same recorded pin as
        # command reads; the result itself stays an event, not a forged tool call.
        pin = None
        if current_run.project_revision is not None and self.tool_executor is not None:
            pin = await self.tool_executor._command_result_pin(current_run)
        appended = await asyncio.to_thread(self.store.consume_command_completions, current_run.run_id,
                                            self._current_transcript(provider_messages, anchor), pin,
                                            [item["event_id"] for item in pending])
        if not appended:
            return False
        provider_messages.extend(appended)
        await self.broker.publish(current_run.run_id)
        return True

    async def start_completion_run(self, session_id: str) -> Run | None:
        from zhimeng_runtime.command_completion import CommandCompletionStore
        if session_id in self._starting_sessions:
            return None
        pending = await asyncio.to_thread(CommandCompletionStore(self.store.database).pending, session_id)
        if not pending:
            return None
        session = await asyncio.to_thread(self.store.get_session, session_id)
        latest = (await asyncio.to_thread(self.store.list_runs, session_id, limit=1))
        if latest and (self.is_run_executing(latest[0].run_id) or latest[0].status not in TERMINAL_RUN_STATUSES):
            return None
        binding = await asyncio.to_thread(self.store.database.get_project_binding, session.project_id) if session.project_id else None
        event_ids = [item["event_id"] for item in pending]
        source = await asyncio.to_thread(self.store.get_run, pending[0]["owner_run_id"])
        mode = await asyncio.to_thread(self.store.get_writer_mode, latest[0].run_id if latest else source.run_id)
        key = "command-completion:" + hashlib.sha256("\n".join(event_ids).encode()).hexdigest()
        result = await self.start_run(
            session_id=session_id, message=source.objective,
            expected_project_revision=binding.revision if binding else None,
            expected_project_fingerprint=binding.fingerprint if binding else None,
            active_chapter_id=None, writer_mode=mode, idempotency_key=key, completion_event_ids=event_ids,
        )
        return result.run

    def _save_interrupted_transcript(
        self, run_id: str, provider_messages: list[dict[str, object]],
        anchor: dict[str, object] | None, partial_content: str,
    ) -> None:
        if anchor is None or self.store.get_run(run_id).status is RunStatus.SUCCEEDED:
            return
        transcript = self._current_transcript(provider_messages, anchor)
        repaired = repair_tool_pairs(run_id, transcript, self.store.list_tool_calls(run_id))
        if partial_content:
            repaired.append({"role": "assistant", "content": partial_content})
        if repaired:
            self.store.save_run_transcript(run_id, repaired)

    async def _maybe_fold_session_history(
        self,
        run_id: str,
        session_id: str,
        *,
        window_spec=None,
    ) -> None:
        """一轮答完之后：还没折叠的对话太长了，就整理成一份摘要。"""
        try:
            await self._fold_session_history(
                session_id, run_id=run_id, force=False, window_spec=window_spec
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Claim/read failures also cannot change a finished Run.
            await self._record_run_event(
                run_id, "session_checkpoint_failed",
                {"reason": str(exc)[:500] or exc.__class__.__name__},
            )

    async def _history_may_need_folding(self, session_id: str, *, window_spec=None) -> bool:
        """不占「整理中」位子的预检：这条对话有没有多到该折一折的历史。

        真正的折叠要先把会话标成 ``_compacting_sessions``，这期间作者再发话会
        排队等整理完（start_run 里等 ``_compaction_done``），不再吃 409。
        绝大多数轮次根本折不到，却每次都要先占位再发现没东西可折——
        所以先在外面看一眼，没到阈值就直接走人，连位子都不占。
        """
        spec = window_spec or resolve_context_window_spec(None)
        checkpoint = await asyncio.to_thread(self.store.latest_session_checkpoint, session_id)
        cursor = 0 if checkpoint is None else checkpoint.through_ordinal
        transcripts = await self._transcript_estimates(session_id)
        pending: list[AgentMessageResponse] = []
        while True:
            page = await asyncio.to_thread(self.store.list_messages, session_id, after_ordinal=cursor, limit=500)
            for message in page:
                if message.role in ("user", "assistant") and message.content.strip():
                    pending.append(message)
            if len(page) < 500:
                break
            cursor = page[-1].ordinal
        completed_turns = sum(message.role == 'assistant' and (
            index + 1 == len(pending) or pending[index + 1].run_id != message.run_id
        ) for index, message in enumerate(pending))
        # 至少两轮：最后一整轮总是原样留着，前面得还有东西可折。
        return (
            sum(_history_weights(pending, transcripts)) >= spec.trigger_tokens
            and completed_turns >= 2
        )

    async def _transcript_estimates(self, session_id: str) -> dict[str, int]:
        """存过往来的各轮估算多少 token（整理对话按它算历史有多长）；读不到当没有，照旧只算文字。"""
        estimates = getattr(self.store, "transcript_token_estimates", None)
        if not callable(estimates):
            return {}
        try:
            return await asyncio.to_thread(estimates, session_id)
        except Exception:  # noqa: BLE001 - 锦上添花
            return {}

    async def _fold_session_history(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        force: bool = False,
        window_spec=None,
    ):
        spec = window_spec or resolve_context_window_spec(None)
        if not force and not await self._history_may_need_folding(session_id, window_spec=spec):
            return None
        async with self._launch_lock:
            if self._closing:
                if force:
                    raise AgentUnavailableError("Writing Agent Runtime is shutting down")
                return None
            if session_id in self._compacting_sessions or session_id in self._starting_sessions:
                if force:
                    raise AgentStoreConflict("这条对话正在工作或整理，请稍后再试")
                return None
            session = await asyncio.to_thread(self.store.get_session, session_id)
            if session.status == "closed":
                if force:
                    raise AgentStoreConflict("已关闭的对话不能整理")
                return None
            runs = await asyncio.to_thread(self.store.list_runs, session_id)
            if any(item.status not in TERMINAL_RUN_STATUSES for item in runs):
                if force:
                    raise AgentStoreConflict("这条对话正在工作，请结束后再整理")
                return None
            self._compacting_sessions.add(session_id)
            # 每次整理换一个新的「整理完了」：以前一场对话整理过一次，这个事件就一直亮着，第二次整理时作者发的话
            # 在 start_run 里等它——await 一个亮着的事件立刻返回、又看见还在整理，原地空转不让出事件循环，
            # 整个后端卡死、这次整理也永远做不完（9-28 压缩考题：第二次自动整理时发下一句，8788 一核 100%、所有接口不回）。
            self._compaction_done[session_id] = asyncio.Event()
        try:
            checkpoint = await asyncio.to_thread(
                self.store.latest_session_checkpoint,
                session_id,
            )
            folded_through = 0 if checkpoint is None else checkpoint.through_ordinal
            messages = []
            cursor = folded_through
            while True:
                page = await asyncio.to_thread(self.store.list_messages, session_id, after_ordinal=cursor, limit=500)
                messages.extend(page)
                if len(page) < 500:
                    break
                cursor = page[-1].ordinal
            pending = [message for message in messages
                       if message.role in ("user", "assistant") and message.content.strip()]
            # 每条算多长：存过往来的那几轮，整段往来（工具调用、结果、回答）算在这一轮最后一条回答上（9-28 起照原样带回去，
            # 按字数只算文字就差得远）；其余照旧按文字，都换成估算的 token。
            weights = _history_weights(pending, await self._transcript_estimates(session_id))
            pending_tokens = sum(weights)
            if not force and pending_tokens < spec.trigger_tokens:
                return None
            # A turn ends at its LAST answer: an answer that hit the output limit is
            # continued as further assistant messages of the same turn. Never cut a
            # question/answer pair in half.
            ends = [
                index
                for index, item in enumerate(pending)
                if item.role == "assistant" and (index + 1 == len(pending) or pending[index + 1].run_id != item.run_id)
            ]
            if len(ends) < 2:
                if force:
                    raise AgentStoreConflict("当前历史太少；整理会保留最近一轮完整对话")
                return None
            # 原样留下最近的几轮：加起来不超过 keep_recent 字，至少最后一整轮（手动压缩只留最后一整轮）；更早的一次折完。
            # 以前固定留 6 轮、一次最多折 4 万字：几轮长的（贴原文、写整章）加起来就过了线，过线以后每说一句都要整理一次，
            # 摘要每轮都换、缓存每轮不命中（9-28 压缩考题 base1）。
            keep_from = ends[-2] + 1
            if not force:
                kept_tokens = sum(weights[keep_from:])
                for number in range(len(ends) - 2, 0, -1):
                    start = ends[number - 1] + 1
                    size = sum(weights[start:keep_from])
                    if kept_tokens + size > spec.keep_recent_tokens:
                        break
                    kept_tokens += size
                    keep_from = start
            foldable = pending[:keep_from]
            if len(foldable) < 2:
                if force:
                    raise AgentStoreConflict("没有可整理的完整历史")
                return None

            provider = await self._require_provider_for_session_id(session_id)
            # 上一份摘要只交回模型写的那部分：程序附的事实和原话每次按库里的记录重拼。
            previous_summary = None if checkpoint is None else model_summary_part(checkpoint.content)
            source = [(item.role, item.content) for item in foldable]
            content: str | None = None
            reported_model: str | None = None
            missing: list[str] = []
            attempts = 0
            # 摘要闸门（OpenClaw）：七节缺了哪节就说一声重来一次。
            for attempts in (1, 2):
                text, reported_model = await self._stream_checkpoint_text(
                    provider,
                    build_checkpoint_request_messages(
                        previous_checkpoint=previous_summary,
                        messages=source,
                        max_characters=spec.checkpoint_max_characters,
                        retry_missing=missing,
                    ),
                )
                # 摘要上限按档位（以前这里没传，1M 档的摘要也截在 8,000 字）。
                content = normalize_checkpoint_text(text, max_characters=spec.checkpoint_max_characters)
                if content is None:
                    break
                missing = missing_checkpoint_sections(content)
                if not missing:
                    break
            if content is None:
                if run_id:
                    await self._record_run_event(
                        run_id,
                        "session_checkpoint_skipped",
                        {"reason": "model returned a blank summary"},
                    )
                if force:
                    raise AgentStoreConflict("模型返回了空摘要，无法完成压缩")
                return None
            if missing:
                # 重来一次还缺：还没过线太多就先不折（原文都还在上下文里），下一轮再整理；过了 1.5 倍、或是作者点的，照收，缺的写明。
                if not force and pending_tokens < spec.trigger_tokens * 3 // 2:
                    if run_id:
                        await self._record_run_event(
                            run_id,
                            "session_checkpoint_rejected",
                            {"missing_sections": missing, "attempts": attempts},
                        )
                    return None
                content += "\n\n" + "\n".join(f"【{title}】（整理时没写出这一节）" for title in missing)

            through_ordinal = foldable[-1].ordinal
            calls, change_sets, file_changes, author_texts = await asyncio.to_thread(
                partial(self.store.compaction_source_records, session_id, through_ordinal=through_ordinal)
            )
            facts = tool_facts_block(change_sets=change_sets, file_changes=file_changes, tool_calls=calls)
            author_words = author_words_block(
                author_texts, max_characters=spec.checkpoint_author_words_characters
            )
            record = await asyncio.to_thread(
                self.store.append_session_checkpoint,
                session_id,
                run_id=run_id,
                through_ordinal=through_ordinal,
                source_message_count=len(foldable),
                content=compose_checkpoint_content(content, facts, author_words),
                reported_model=reported_model,
                expected_previous_through_ordinal=folded_through,
            )
            if run_id:
                await self._record_run_event(
                    run_id,
                    "session_checkpoint_written",
                    {
                        "checkpoint_id": record.checkpoint_id,
                        "through_ordinal": record.through_ordinal,
                        "source_message_count": record.source_message_count,
                        "folded_characters": sum(
                            len(item.content) for item in foldable
                        ),
                        "kept_characters": sum(len(item.content) for item in pending[keep_from:]),
                        "pending_tokens": pending_tokens,
                        "kept_tokens": sum(weights[keep_from:]),
                        "summary_characters": len(record.content),
                        "facts_characters": len(facts or ""),
                        "author_words_characters": len(author_words or ""),
                        "attempts": attempts,
                        "missing_sections": missing,
                    },
                )
            return record
        except asyncio.CancelledError:
            # 关机/取消要照常向上传，不能被下面的兜底吞掉。
            raise
        except AgentStoreConflict:
            if force:
                raise
            return None
        except Exception as exc:  # noqa: BLE001 - 自动摘要失败绝不影响已完成的 Run
            if run_id:
                await self._record_run_event(
                    run_id,
                    "session_checkpoint_failed",
                    {"reason": str(exc)[:500] or exc.__class__.__name__},
                )
            if force:
                if isinstance(exc, (AgentProviderError, AgentStoreError, AgentUnavailableError)):
                    raise
                raise AgentProviderError("整理对话失败：" + (str(exc) or exc.__class__.__name__), code="session_checkpoint_failed", retryable=True) from exc
            return None
        finally:
            self._compacting_sessions.discard(session_id)
            self._compaction_done.setdefault(session_id, asyncio.Event()).set()

    @staticmethod
    def _quick_provider(provider: AgentProvider) -> AgentProvider:
        """辅助请求（起名、挑记忆、片段摘要）：同线路和模型，支持时请求 low，不改主任务档位。"""

        variant = getattr(provider, "with_reasoning_effort", None)
        if not callable(variant):
            return provider
        try:
            return variant("low")
        except Exception:  # noqa: BLE001 - 换不了就照原样用
            return provider

    async def _maybe_name_session(
        self,
        run_id: str,
        run: Run,
        assistant_content: str,
    ) -> None:
        """对话收尾后，请模型把这段对话的主题概括成短名字。

        和「折叠历史」同一纪律：Run 已经完成、作者已经看到回答，这里发生的
        任何事情都不许影响它——失败一律吞掉并记一条事件。只盖系统起的名字
        （占位标题或从第一句话提炼出来的标题），作者自己改过的名字绝不动
        （守卫在 ``AgentStore.maybe_update_auto_session_title`` 里）。

        不限第一轮：老对话的标题还是占位/提炼的，下次说话收尾时照样起名——
        起名依据始终用第一句话（对话的主题由它定），不是当前这轮。

        Codex 的做法是用第一句话当标题、改名靠手动；这里比它多走一步，
        让模型把「作者想干什么」浓缩成名字——「新对话 · 9/14 18:42」和
        「第二章开头改紧凑」对作者来说不是一回事。
        """
        try:
            # 建会话时就起了正经名字的（不是占位、也不是第一轮提炼出来的那句），
            # 说明作者或上游已经命名过——连起名这一问都不用打。
            session = await asyncio.to_thread(self.store.get_session, run.session_id)
            if _title_is_placeholder(session.title):
                first_objective = run.objective if run.ordinal == 1 else await asyncio.to_thread(
                    self.store.first_session_objective, run.session_id
                )
            else:
                first_objective = await asyncio.to_thread(
                    self.store.first_session_objective, run.session_id
                )
                if first_objective is None:
                    return
                if session.title != _derive_session_title(first_objective):
                    return
            if not first_objective:
                return
            provider = self._quick_provider(await self._require_provider_for_session_id(run.session_id))
            user_excerpt = first_objective.strip()[:160]
            assistant_excerpt = assistant_content.strip()[:240]
            if not user_excerpt:
                return
            request_messages = [
                {"role": "system", "content": _SESSION_TITLE_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"作者说：{user_excerpt}\n"
                        f"Agent 答：{assistant_excerpt or '（还没答完）'}\n\n"
                        "给这段对话起个名字："
                    ),
                },
            ]
            parts: list[str] = []
            async for event in provider.stream_chat(
                request_messages,
                cancel_event=asyncio.Event(),
            ):
                if isinstance(event, ProviderTextDelta):
                    parts.append(event.text)
                elif isinstance(event, ProviderCompleted):
                    break
            name = _clean_session_title("".join(parts))
            if name is None:
                await self._record_run_event(
                    run_id,
                    "session_title_skipped",
                    {"reason": "model returned a blank name"},
                )
                return
            renamed = await asyncio.to_thread(
                self.store.maybe_update_auto_session_title,
                run.session_id,
                first_objective=first_objective,
                new_title=name,
            )
            await self._record_run_event(
                run_id,
                "session_title_named" if renamed else "session_title_kept",
                {"title": name} if renamed else {"reason": "author renamed it already"},
            )
        except asyncio.CancelledError:
            # 关机/取消要照常向上传，不能被下面的兜底吞掉。
            raise
        except Exception as exc:  # noqa: BLE001 - 起名失败绝不影响已完成的 Run
            await self._record_run_event(
                run_id,
                "session_title_failed",
                {"reason": str(exc)[:500] or exc.__class__.__name__},
            )

    async def _maybe_remember(self, run_id: str, run: Run, assistant_content: str) -> None:
        """答完以后从这一轮里挑出以后还用得上的事，记进《用户记忆.md》（设置页「记忆」开着时）。

        和起名同一纪律：Run 已经完成、用户已经看到回答，这里的任何失败都只记一条事件。
        「只读」挡位不记（和那个挡位下没有 memory_write 一样）；模型这一轮自己记过的，
        另问时它就在「已经记住的」里，不会再记一遍。
        """
        if not self._auto_memory or not self._memory_enabled() or run.origin_kind != "user":
            return
        try:
            if await asyncio.to_thread(self.store.get_writer_mode, run_id) != "author_review_change_proposal":
                return
            state_root = Path(self.store.database.path).parent
            existing = await asyncio.to_thread(read_user_memory, state_root, strict=True)

            def read_sources():
                with self.store.database.connection() as connection:
                    rows = connection.execute(
                        "SELECT sm.message_id, json_extract(a.payload_json, '$.payload.content') AS content "
                        "FROM session_message sm JOIN activity a ON a.id=sm.activity_id "
                        "WHERE sm.run_id=? AND sm.role='user' ORDER BY sm.ordinal", (run_id,),
                    ).fetchall()
                return {row["message_id"]: row["content"] for row in rows if isinstance(row["content"], str)}

            sources = await asyncio.to_thread(read_sources)
            if sum(len(text.strip()) for text in sources.values()) < _MEMORY_EXTRACT_MIN_MESSAGE_CHARS:
                return
            provider = self._quick_provider(await self._require_provider_for_session_id(run.session_id))
            request_messages = [
                {"role": "system", "content": _MEMORY_EXTRACT_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps({"existing_memory": existing, "user_messages": sources}, ensure_ascii=False),
                },
            ]
            parts: list[str] = []
            # 中转站按账户限并发：刚答完、起名那一问还没散，这一问偶尔整个被拒（9-29 真机 1/8「Provider request failed」）。
            # 隔几秒再问一次；再不成就记一条事件算了。
            for attempt in range(2):
                parts = []
                try:
                    async for event in provider.stream_chat(request_messages, cancel_event=asyncio.Event()):
                        if isinstance(event, ProviderTextDelta):
                            parts.append(event.text)
                        elif isinstance(event, ProviderCompleted):
                            break
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(3)
            proposals = parse_memory_proposals("".join(parts), sources)
            if not proposals:
                await self._record_run_event(run_id, "memory_auto_none", {})
                return
            path = user_memory_path(state_root)
            locks = getattr(self.tool_executor, "locks", None)
            guard = locks.for_path(path) if locks is not None else nullcontext()

            def save_changes():
                with user_memory_transaction(state_root):
                    if not self._memory_enabled():
                        return [], "memory_disabled"
                    _path, changes, skipped = update_user_memory(state_root, proposals, expected=existing)
                    return changes, skipped

            async with guard:
                # 模型挑记忆期间用户的编辑、删除和开关优先；比较与追加在同一个事务里。
                saved, skipped = await asyncio.to_thread(save_changes)
            if skipped is not None:
                await self._record_run_event(run_id, "memory_auto_skipped", {"reason": skipped})
                return
            await self._record_run_event(run_id, "memory_auto_saved", {"changes": saved})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 记不上绝不影响已完成的 Run
            await self._record_run_event(
                run_id,
                "memory_auto_failed",
                {"reason": str(exc)[:500] or exc.__class__.__name__},
            )

    async def _record_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        """写一条非活动类的 Run 生命周期事件。

        轻量 fake store（恢复链路的测试替身）没有事件日志，跳过即可；
        真实 AgentStore 一定落库。
        """
        append_run_event = getattr(self.store, "append_run_event", None)
        if append_run_event is None:
            return
        await asyncio.to_thread(append_run_event, run_id, event_type, payload)

    @staticmethod
    def _provider_tool_result_message(
        provider_tool_call_id: str,
        tool_call: StoredToolCall,
        *,
        replayed: bool,
    ) -> dict[str, object]:
        return tool_result_message(provider_tool_call_id, tool_call, replayed=replayed)

    @staticmethod
    def _merge_usage(
        accumulated: MessageUsage | None,
        current: MessageUsage | None,
        *,
        previous_turns: int = 0,
    ) -> MessageUsage | None:
        if current is None:
            return accumulated.model_copy(update={
                "input_tokens_reported": False,
                "output_tokens_reported": False,
                "cached_input_tokens_complete": False,
            }) if accumulated is not None else None
        if accumulated is None:
            # None 也可能意味着前几次调用全未上报，而不只是第一次开始累计。
            return current.model_copy(update={
                "input_tokens_reported": False,
                "output_tokens_reported": False,
                "cached_input_tokens_complete": False,
            }) if previous_turns > 0 else current
        input_tokens = accumulated.input_tokens + current.input_tokens
        output_tokens = accumulated.output_tokens + current.output_tokens
        cached_accumulated = accumulated.cached_input_tokens
        cached_current = current.cached_input_tokens
        cached_total = (
            cached_accumulated + cached_current
            if cached_accumulated is not None and cached_current is not None
            else (cached_accumulated if cached_accumulated is not None else cached_current)
        )
        return MessageUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=max(
                accumulated.total_tokens + current.total_tokens,
                input_tokens + output_tokens,
            ),
            cached_input_tokens=cached_total,
            input_tokens_reported=accumulated.input_tokens_reported and current.input_tokens_reported,
            output_tokens_reported=accumulated.output_tokens_reported and current.output_tokens_reported,
            cached_input_tokens_complete=(
                accumulated.cached_input_tokens_complete
                and current.cached_input_tokens_complete
                and cached_accumulated is not None
                and cached_current is not None
            ),
        )

    @staticmethod
    def _tool_error(
        run_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> ErrorEnvelope:
        return ErrorEnvelope(
            error=ErrorDetail(
                code=code,
                message=message,
                request_id=run_id,
                retryable=retryable,
            )
        )

    @staticmethod
    def _change_proposal(change_set: ChangeSetResponse) -> ChangeProposalPayload:
        return ChangeProposalPayload(
            proposal_id=change_set.change_set_id,
            title=change_set.title,
            summary=change_set.summary,
            base_revision=change_set.base_revision,
            base_fingerprint=change_set.base_fingerprint,
            files=[
                FileChange(
                    file_id=change.file_id,
                    path=change.relative_path,
                    operation=change.operation.value,
                    content_hash_before=change.before_sha256,
                    content_hash_after=change.proposed_after_sha256,
                )
                for change in change_set.changes
            ],
        )

    async def _observe_context_pack_knowledge(
        self,
        context_pack_id: str,
        expected_context_pack_hash: str,
    ) -> KnowledgeFingerprintObservation:
        try:
            target = await asyncio.to_thread(
                self.store.context_pack_knowledge_target,
                context_pack_id,
                expected_context_pack_hash=expected_context_pack_hash,
            )
        except AgentStoreError:
            return self._unavailable_knowledge_observation(
                context_pack_id=context_pack_id,
                pack_hash=expected_context_pack_hash,
                expected_fingerprint=None,
            )
        return await self._observe_knowledge_target(target)

    async def _observe_run_context_pack_knowledge(
        self,
        run_id: str,
    ) -> KnowledgeFingerprintObservation | None:
        try:
            target = await asyncio.to_thread(
                self.store.run_context_pack_knowledge_target,
                run_id,
            )
        except AgentStoreError:
            return None
        if target is None:
            return None
        return await self._observe_knowledge_target(target)

    async def _require_current_project_kb_for_context(
        self,
        context_pack_id: str,
        expected_context_pack_hash: str,
    ) -> None:
        target = await asyncio.to_thread(
            self.store.context_pack_knowledge_target,
            context_pack_id,
            expected_context_pack_hash=expected_context_pack_hash,
        )
        if not _binds_project_kb(target):
            return
        await self._require_current_project_kb_target(target)

    async def _run_binds_project_kb(self, run_id: str) -> bool:
        target = await asyncio.to_thread(
            self.store.run_context_pack_knowledge_target,
            run_id,
        )
        return _binds_project_kb(target)

    async def _require_current_project_kb_for_run(self, run_id: str) -> None:
        target = await asyncio.to_thread(
            self.store.run_context_pack_knowledge_target,
            run_id,
        )
        if not _binds_project_kb(target):
            # 普通对话的资料包不绑知识库快照（2026-09-25 起）：没有快照要核对，书在
            # 对话期间被作者改了也不拦这一轮——每次写入各自按文件版本核对。以前这里
            # 每一轮模型请求前都要为核对把整本书扫一遍（九百多章的书一次十秒）。
            return
        if await asyncio.to_thread(self._run_applied_direct_writes, run_id):
            # 这场对话自己用写工具落盘过：KB 快照必然落后（落盘推进了项目
            # 版本），「绑定仍 current」的校验必然误杀。改为核对版本钉子——
            # 钉子随每次落盘同步到最新版本，若钉子和当前绑定还一致，说明
            # 没有这场对话之外的改动；不一致才是真的「书被别人动了」。
            await asyncio.to_thread(self._require_run_project_pin_current, run_id)
            return
        await self._require_current_project_kb_target(target)

    def _run_applied_direct_writes(self, run_id: str) -> bool:
        """这场对话有没有用写工具直接落盘过（ succeeds 的写工具调用）。"""
        database = getattr(self.store, "database", None)
        connect = getattr(database, "connection", None)
        if not callable(connect):
            return False
        with connect() as connection:
            return self._run_direct_write_pin(run_id, connection) is not None

    @staticmethod
    def _run_direct_write_pin(
        run_id: str,
        connection: sqlite3.Connection,
    ) -> tuple[int, str] | None:
        """这场对话最后一次直接落盘（或执行命令）后的版本钉子。

        写工具落盘、命令执行结束时把最新版本号/指纹写进了自己的工具结果
        （result_revision / result_fingerprint），这里取最后一条成功结果的
        钉子。run.project_revision 不动——它是「这场对话消费的资料包」
        的基准，资料包完整性校验靠它。
        """
        return latest_own_write_pin(connection, run_id)

    def _require_run_project_pin_current(self, run_id: str) -> None:
        """直接落盘后的收尾校验：以这场对话最后一次落盘产出的版本钉子为准，
        钉子与当前绑定仍一致，说明这期间没有这场对话之外的改动。
        写工具落盘必然让 KB 快照落后（project_revision_changed 等），
        那条「绑定仍 current」的校验对自己动过手的 Run 必然误杀，所以
        直接写过的 Run 收尾时改用这个钉子核对。"""
        database = getattr(self.store, "database", None)
        connect = getattr(database, "connection", None)
        if not callable(connect):
            return
        with connect() as connection:
            pin = self._run_direct_write_pin(run_id, connection)
        run = self.store.get_run(run_id)
        project_id = run.project_revision.project_id if run.project_revision else None
        if pin is None:
            # 老数据：写工具结果里还没有版本钉子（2026-09-15 上午的格式），
            # 退回用 run 自己的钉子（那批 Run 的钉子在落盘时被同步过）。
            if run.project_revision is None:
                return
            pin = (run.project_revision.revision, run.project_revision.fingerprint)
        if project_id is None:
            return
        binding = self.store.database.get_project_binding(project_id)
        if binding is None or (binding.revision, binding.fingerprint) != pin:
            raise AgentContextPackConflict(
                "书在这场对话期间被其他地方改动了，请确认后再继续。",
                code="agent_run_project_changed",
            )

    def _direct_write_pin_verifier(self, run_id: str) -> CurrentProjectKbVerifier:
        """complete_run 事务内的同款钉子核对（与上面的预检口径一致）。"""

        def verify(
            target: ContextPackKnowledgeTarget,
            connection: sqlite3.Connection,
        ) -> None:
            del target
            pin = AgentService._run_direct_write_pin(run_id, connection)
            run_row = self.store._require_run_row(connection, run_id)
            project_id = run_row["project_binding_id"]
            if pin is None:
                # 老数据退回 run 钉子（见 _require_run_project_pin_current）。
                revision = run_row["project_revision"]
                fingerprint = run_row["project_fingerprint"]
                if revision is None:
                    return
                pin = (int(revision), str(fingerprint))
            if project_id is None:
                return
            binding = self.store.database.get_project_binding_in_connection(
                connection, str(project_id)
            )
            if binding is None or (binding.revision, binding.fingerprint) != pin:
                raise AgentContextPackConflict(
                    "书在这场对话期间被其他地方改动了，请确认后再继续。",
                    code="agent_run_project_changed",
                )

        return verify

    @asynccontextmanager
    async def _terminal_project_lock_for_run(
        self,
        run_id: str,
    ) -> AsyncIterator[None]:
        locks = self.project_locks
        if locks is None:
            yield
            return
        target = await asyncio.to_thread(
            self.store.run_context_pack_knowledge_target,
            run_id,
        )
        if target is None or target.project_id is None:
            raise AgentContextPackConflict(
                "Run has no project binding for terminal validation",
                code="context_pack_project_kb_unbound",
            )
        database = getattr(self.store, "database", None)
        binding_getter = getattr(database, "get_project_binding", None)
        if not callable(binding_getter):
            raise AgentContextPackConflict(
                "Project binding lookup is unavailable for terminal validation",
                code="project_kb_unavailable",
            )
        binding = await asyncio.to_thread(binding_getter, target.project_id)
        if binding is None:
            raise AgentContextPackConflict(
                "Project binding was not found for terminal validation",
                code="project_kb_unavailable",
            )
        async with locks.for_path(binding.root_path):
            yield

    @staticmethod
    async def _await_terminal_store_operation(
        callback: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        operation = asyncio.create_task(
            asyncio.to_thread(callback, *args, **kwargs),
            name="agent-terminal-store-operation",
        )
        request_cancelled = False
        while True:
            try:
                result = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                request_cancelled = True
        if request_cancelled:
            raise asyncio.CancelledError
        return result

    async def _acquire_current_project_kb_lease_for_run(
        self,
        run_id: str,
    ) -> AbstractContextManager[object]:
        operation = asyncio.create_task(
            asyncio.to_thread(
                self._enter_current_project_kb_lease_for_run_sync,
                run_id,
            ),
            name=f"project-kb-lease-acquire:{run_id}",
        )
        request_cancelled = False
        while True:
            try:
                lease = await asyncio.shield(operation)
                break
            except asyncio.CancelledError:
                if operation.cancelled():
                    raise
                request_cancelled = True
        if request_cancelled:
            lease.__exit__(None, None, None)
            raise asyncio.CancelledError
        return lease

    def _enter_current_project_kb_lease_for_run_sync(
        self,
        run_id: str,
    ) -> AbstractContextManager[object]:
        target = self.store.run_context_pack_knowledge_target(run_id)
        if target is None:
            raise AgentContextPackConflict(
                "Run has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        lease = self._current_project_kb_target_lease_sync(target)
        lease.__enter__()
        return lease

    @contextmanager
    def _current_project_kb_target_lease_sync(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> Iterator[object]:
        if (
            target.project_id is None
            or target.project_kb_state_id is None
            or target.project_kb_manifest_hash is None
        ):
            raise AgentContextPackConflict(
                "Context Pack has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        # O2-E packs bind a distillation-index identity, not a published .kb state.
        if self._is_managed_o2e_context_target(target):
            yield {
                "project_id": target.project_id,
                "project_kb_state_id": target.project_kb_state_id,
                "project_kb_manifest_hash": target.project_kb_manifest_hash,
                "source": "external_distillation",
            }
            return
        service = self.project_kb_service
        if service is None:
            raise AgentContextPackConflict(
                "Project KB verification is not configured",
                code="project_kb_unavailable",
            )
        try:
            with service.current_binding_lease(
                target.project_id,
                project_kb_state_id=target.project_kb_state_id,
                project_kb_manifest_hash=target.project_kb_manifest_hash,
                expected_knowledge_fingerprint=(
                    target.project_kb_knowledge_fingerprint
                ),
            ) as manifest:
                yield manifest
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            raise AgentContextPackConflict(
                str(exc),
                code=str(detail.get("code") or "project_kb_unavailable"),
            ) from exc


    @staticmethod
    def _is_managed_o2e_context_target(target: ContextPackKnowledgeTarget) -> bool:
        return (
            target.nlcl_route_source == "external_distillation"
            and target.context_pack_id.startswith("context-pack-o2e-")
            and target.project_id is not None
            and target.project_kb_state_id is not None
            and target.project_kb_manifest_hash is not None
        )

    def _kb_sync_verifier(
        self,
        target: ContextPackKnowledgeTarget,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """收尾校验入口：旧 KB 绑定若已被自动重建重绑，替换后核对。"""
        entry = self._kb_rebased_bindings.get(target.project_id or "")
        if entry is not None and (
            target.project_kb_state_id,
            target.project_kb_manifest_hash,
        ) == entry[0]:
            target = entry[1]
        self._require_current_project_kb_target_sync(target, connection)

    async def _require_current_project_kb_target(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> None:
        try:
            await asyncio.to_thread(self._require_current_project_kb_target_sync, target)
            return
        except AgentContextPackConflict as exc:
            if exc.code != "project_kb_state_stale" or target.project_id is None:
                raise
            # 作者在外部编辑器手改一个字 → KB 快照落后一代 → 以前整轮拒掉，
            # 逼作者先去点重建。现在先自动重建一次（按项目节流）再核对；
            # 重建不了或仍然 stale，才把原错误如实抛出。
            if not await self._auto_rebuild_project_kb(target.project_id):
                raise
            service = self.project_kb_service
            state = await asyncio.to_thread(service.get_state, target.project_id)
            if (
                state.status is ProjectKbStatus.CURRENT
                and state.pointer is not None
                and state.manifest is not None
            ):
                # 重绑定：卡片（知识指纹）没变，只是项目版本推进了。
                # 旧资料包改绑到新快照，等价于「作者点重建后重新确认」。
                self._kb_rebased_bindings[target.project_id] = (
                    (target.project_kb_state_id or "", target.project_kb_manifest_hash or ""),
                    ContextPackKnowledgeTarget(
                        context_pack_id=target.context_pack_id,
                        pack_hash=target.pack_hash,
                        knowledge_fingerprint=target.knowledge_fingerprint,
                        project_kb_knowledge_fingerprint=target.project_kb_knowledge_fingerprint,
                        project_id=target.project_id,
                        project_kb_state_id=state.pointer.state_id,
                        project_kb_manifest_hash=state.pointer.manifest_hash,
                        nlcl_route_source=target.nlcl_route_source,
                    ),
                )
                await asyncio.to_thread(
                    self._require_current_project_kb_target_sync,
                    self._kb_rebased_bindings[target.project_id][1],
                )
                return
            raise

    _KB_AUTO_REBUILD_THROTTLE_SECONDS = 60.0
    _KB_AUTO_REBUILD_RECOVERABLE_REASONS = frozenset(
        {
            "project_revision_changed",
            "project_fingerprint_changed",
            "project_files_changed",
            "knowledge_scope_revision_changed",
            "knowledge_fingerprint_changed",
            "knowledge_projection_changed",
            "knowledge_changed_during_read",
        }
    )

    async def _auto_rebuild_project_kb(self, project_id: str) -> bool:
        service = self.project_kb_service
        if service is None:
            return False
        now = monotonic()
        last = self._kb_auto_rebuild_at.get(project_id)
        if last is not None and now - last < self._KB_AUTO_REBUILD_THROTTLE_SECONDS:
            return False
        self._kb_auto_rebuild_at[project_id] = now
        try:
            state = service.get_state(project_id)
        except Exception:
            # 测试替身或异常态下拿不到状态：如实放弃自动重建。
            return False
        if state.status is not ProjectKbStatus.STALE:
            return False
        reasons = set(state.reasons)
        if not reasons or not reasons.issubset(self._KB_AUTO_REBUILD_RECOVERABLE_REASONS):
            return False
        try:
            binding = service.database.get_project_binding(project_id)
            if binding is None:
                return False
            # 先把绑定推进到书的现状（inspect + sync），否则 rebuild 会以
            # 「live 和库内记录不一致」拒绝——手动重建按钮走的就是这条序列。
            live = service._inspect_project(binding)
            if live.fingerprint != binding.fingerprint:
                service.database.sync_project_binding(
                    project_id=binding.project_id,
                    root_path=binding.root_path,
                    display_name=binding.display_name,
                    fingerprint=live.fingerprint,
                )
            knowledge = service.store.project_state_snapshot(project_id)
            service.rebuild(
                project_id,
                ProjectKbRebuildRequest(
                    expected_project_revision=knowledge.project.revision,
                    expected_project_fingerprint=knowledge.project.fingerprint,
                    expected_scope_state_revision=knowledge.scope.state_revision,
                    expected_knowledge_fingerprint=canonical_json_hash(
                        knowledge.fingerprint_material
                    ),
                ),
            )
            return True
        except Exception:
            # 重建失败（并发冲突、书正在被占用等）：如实让原错误继续，
            # 作者手动重建仍可用。
            return False

    def _require_current_project_kb_target_sync(
        self,
        target: ContextPackKnowledgeTarget,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if (
            target.project_id is None
            or target.project_kb_state_id is None
            or target.project_kb_manifest_hash is None
        ):
            raise AgentContextPackConflict(
                "Context Pack has no verified project KB binding",
                code="context_pack_project_kb_unbound",
            )
        # O2-E managed packs bind a distillation-index identity, not the live .kb snapshot.
        if self._is_managed_o2e_context_target(target):
            return
        service = self.project_kb_service
        if service is None:
            raise AgentContextPackConflict(
                "Project KB verification is not configured",
                code="project_kb_unavailable",
            )
        try:
            connection_verifier = getattr(
                service,
                "require_current_binding_in_connection",
                None,
            )
            if connection is not None and callable(connection_verifier):
                connection_verifier(
                    connection,
                    target.project_id,
                    project_kb_state_id=target.project_kb_state_id,
                    project_kb_manifest_hash=target.project_kb_manifest_hash,
                    expected_knowledge_fingerprint=(
                        target.project_kb_knowledge_fingerprint
                    ),
                )
            else:
                service.require_current_binding(
                    target.project_id,
                    project_kb_state_id=target.project_kb_state_id,
                    project_kb_manifest_hash=target.project_kb_manifest_hash,
                    expected_knowledge_fingerprint=(
                        target.project_kb_knowledge_fingerprint
                    ),
                )
        except ProjectKbServiceError as exc:
            detail = exc.details[0] if exc.details else {}
            raise AgentContextPackConflict(
                str(exc),
                code=str(detail.get("code") or "project_kb_unavailable"),
            ) from exc

    async def _observe_knowledge_target(
        self,
        target: ContextPackKnowledgeTarget,
    ) -> KnowledgeFingerprintObservation:
        observed_at = datetime.now(UTC)
        expected = target.knowledge_fingerprint
        if expected is None:
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=None,
                observed_fingerprint=None,
                state=KnowledgeObservationState.MATCHED,
                observed_at=observed_at,
            )
        # O2-E packs are closed over the managed distillation-index fingerprint.
        if self._is_managed_o2e_context_target(target):
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
                observed_fingerprint=expected,
                state=KnowledgeObservationState.MATCHED,
                observed_at=observed_at,
            )
        probe = self.knowledge_fingerprint_probe
        if probe is None:
            return self._unavailable_knowledge_observation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
            )
        try:
            observed = await asyncio.to_thread(probe, target.project_id)
            state = (
                KnowledgeObservationState.MATCHED
                if observed == expected
                else KnowledgeObservationState.CHANGED
            )
            return KnowledgeFingerprintObservation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
                observed_fingerprint=observed,
                state=state,
                observed_at=observed_at,
            )
        except Exception:
            return self._unavailable_knowledge_observation(
                context_pack_id=target.context_pack_id,
                pack_hash=target.pack_hash,
                expected_fingerprint=expected,
            )

    @staticmethod
    def _unavailable_knowledge_observation(
        *,
        context_pack_id: str,
        pack_hash: str,
        expected_fingerprint: str | None,
    ) -> KnowledgeFingerprintObservation:
        return KnowledgeFingerprintObservation(
            context_pack_id=context_pack_id,
            pack_hash=pack_hash,
            expected_fingerprint=expected_fingerprint,
            observed_fingerprint=None,
            state=KnowledgeObservationState.UNAVAILABLE,
            observed_at=datetime.now(UTC),
        )

    async def _transition(self, run_id: str, status: RunStatus, detail: str) -> Run:
        run = await asyncio.to_thread(
            self.store.transition_run,
            run_id,
            status,
            detail=detail,
        )
        await self.broker.publish(run_id)
        if status in TERMINAL_RUN_STATUSES:
            # 先广播终态、再回收广播器条目；晚到订阅者靠重查库收敛。
            self.broker.forget(run_id)
        return run

    async def _mark_cancelled(self, run_id: str) -> None:
        try:
            await asyncio.to_thread(self.store.mark_cancelled, run_id)
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)
        self.broker.forget(run_id)

    async def _mark_interrupted(self, run_id: str) -> None:
        try:
            await asyncio.to_thread(
                self.store.mark_interrupted,
                run_id,
                detail="Runtime stopped before the Run reached a terminal state",
            )
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)
        self.broker.forget(run_id)

    async def _fail(
        self,
        run_id: str,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: dict | None = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                self.store.fail_run,
                run_id,
                code=code,
                message=message,
                retryable=retryable,
                details=details,
            )
        except AgentStoreInvalidState:
            return
        await self.broker.publish(run_id)
