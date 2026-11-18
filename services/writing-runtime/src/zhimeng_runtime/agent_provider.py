from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import threading
import time
from collections.abc import AsyncIterator, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar

import httpx
from uuid import uuid4
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from zhimeng_runtime import __version__
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.provider_error_messages import upstream_message
from zhimeng_runtime.provider_url_resolver import (
    CHAT_COMPLETIONS_PATH,
    RESPONSES_PATH,
    resolve_endpoint,
)
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    MessageUsage,
)


# 单次工具调用入参的兜底上限：正常写作调用（整章正文也不过几十 KB）永远碰不到，
# 它只是防「中转站失控狂吐」的保险丝。撞到也不判整轮失败——这次调用带着
# arguments_error 进场，由服务层回喂模型「缩短或分次重发」。
MAX_TOOL_ARGUMENT_BYTES = 8 * 1024 * 1024
MIN_TOOL_CALL_ID_LENGTH = 8
MAX_TOOL_CALL_ID_LENGTH = 128
MAX_TOOL_NAME_LENGTH = 64
# 复读熔断：同一个短句连续吐这么多次就掐掉，别让它烧穿整份输出预算。
# 真实事故（run_91cdc08f84，2026-09-06）：模型把「请直接改正文。」重复 2054 段、
# 22528 字，撞满 max_tokens=16384，作者干等 537 秒只换回一句「输出超上限」。
REPETITION_SEGMENT_LIMIT = 12
# 只盯短句。正常正文不会把同一个短句连着说十几遍，而退化循环几乎总是这个形态；
# 长段落重复是另一类问题（比如模型抄原文），不在这里管。
REPETITION_SEGMENT_MAX_CHARS = 120
# RFC 7230 token：HTTP 头名的合法字符集（自定义请求头用）。
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]{1,100}$")
# 这一次请求属于哪一场对话：运行循环在自己的任务里设上对话 ID，Responses 线把它当 prompt_cache_key 发出去。
# xAI 官方文档：缓存按服务器存，Responses 接口靠请求体里的 prompt_cache_key 把同一场对话钉到同一台服务器
# （Chat 接口用的是 x-grok-conv-id 头）；Codex 每个 Responses 请求都带、值是会话 ID（core/src/client.rs:575）。
# 用 ContextVar、不挂在 provider 上：同一个 provider 同时在服务好几场对话。
PROMPT_CACHE_KEY: ContextVar[str | None] = ContextVar("zhimeng_prompt_cache_key", default=None)
# 两条线路的端点。Responses 是现代主协议（Codex 等原生用它）；
# Chat Completions 保留为老渠道和受限模型的兼容回退。
# 实际拼地址统一走 provider_url_resolver；这两个名字保留给指纹与端点选择的可读性。
CHAT_ENDPOINT_PATH = CHAT_COMPLETIONS_PATH
RESPONSES_ENDPOINT_PATH = RESPONSES_PATH
# Anthropic thinking 的 effort→budget 保守映射（auto/none 不发 thinking 字段）。
# 官方要求 budget_tokens ≥ 1024 且 < max_tokens，装配时会再收敛一次。
_ANTHROPIC_THINKING_BUDGETS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32768,
    "ultra": 65536,
}
_ANTHROPIC_TOOL_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def _anthropic_tool_id(call_id: str) -> str:
    """Anthropic 的工具调用 id 只认字母、数字、`_`、`-`：别家给的（Kimi 的 `Bash:256`、`functions.read_file:0`）换成 `_`。"""
    return _ANTHROPIC_TOOL_ID_DISALLOWED.sub("_", call_id)


def _retry_after_seconds(response: httpx.Response, default: float = 5.0) -> float:
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return default
    try:
        return min(max(float(raw), 0.0), 30.0)
    except ValueError:
        return default


class ProviderToolDefinition(BaseModel):
    """Provider-neutral, versioned function tool definition."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_id: Literal["zhimeng.provider-tool-definition.v1"] = Field(
        default="zhimeng.provider-tool-definition.v1",
        alias="schema",
    )
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)*$")
    description: str = Field(min_length=1, max_length=4096)
    parameters: dict[str, JsonValue]

    @field_validator("name", "description")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tool definition text must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_parameters(self) -> "ProviderToolDefinition":
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object JSON Schema")
        return self


@dataclass(frozen=True, slots=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ProviderReasoningDelta:
    """A provider-emitted reasoning fragment, kept separate from the answer."""

    text: str
    visibility: Literal["summary", "full"] = "full"


@dataclass(frozen=True, slots=True)
class ProviderToolCall:
    index: int
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    # 参数没能解析成合法 JSON 时不再整轮判死：调用照常进场，
    # 由服务层把「请重发这次调用」作为工具结果回喂模型（计入失败计数）。
    arguments_error: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    finish_reason: str
    usage: MessageUsage | None
    reported_model: str | None = None
    # 最终成功请求里发出的参数，不是服务端对内部推理强度的确认。
    # None 表示未发送档位参数；和显式请求 none（关闭）不同。
    effective_reasoning_effort: str | None = None
    effective_thinking_budget_tokens: int | None = None


ProviderEvent = ProviderTextDelta | ProviderReasoningDelta | ProviderToolCall | ProviderCompleted


@dataclass(frozen=True, slots=True)
class _ProviderToolCallFragment:
    index: int
    id_fragment: str | None
    name_fragment: str | None
    arguments_fragment: str | None
    # True 表示这是 Responses 的 *.done 事件给出的完整入参：一段 delta 都没
    # 收到时用它兜底，收到过就只做核对，不重复追加。
    arguments_final: bool = False


@dataclass(slots=True)
class _ProviderToolCallAccumulator:
    index: int
    tool_call_id: str
    tool_name: str
    arguments_parts: list[str]
    arguments_bytes: int = 0
    arguments_error: str | None = None


@dataclass(frozen=True, slots=True)
class _DecodedProviderChunk:
    text_events: tuple[ProviderTextDelta, ...]
    reasoning_events: tuple[ProviderReasoningDelta, ...]
    tool_fragments: tuple[_ProviderToolCallFragment, ...]
    stream_done: bool
    finish_reason: str | None
    usage: MessageUsage | None
    reported_model: str | None = None


class _InvalidJsonValueError(ValueError):
    pass


class _AnthropicEventDecoder:
    """把 Anthropic Messages 的类型化 SSE 事件流映射到内部事件。

    有状态：`message_start` 里的 input_tokens 要留到 `message_delta` 合并
    （Anthropic 的 message_delta 只带 output_tokens），所以每条流一个实例。
    映射表：

    - message_start → 上报模型 + usage 起点（input_tokens）
    - content_block_start（text/thinking/tool_use）→ 块分流；tool_use 给 id/name
    - content_block_delta（text_delta/thinking_delta/input_json_delta）
      → 正文 / 思考 / 工具入参片段（signature_delta 忽略）
    - content_block_stop → 无事件
    - message_delta → stop_reason（end_turn|stop_sequence→stop、
      max_tokens→length、tool_use→tool_calls）+ 合并完整 usage
    - message_stop → 流结束；ping 忽略；error → error_event
    """

    def __init__(self) -> None:
        self._input_tokens: int | None = None
        self._cached_input_tokens: int | None = None
        self._cache_creation_tokens: int | None = None

    @staticmethod
    def _block_index(payload: Mapping[str, Any]) -> int:
        raw_index = payload.get("index")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0:
            raise AgentProviderError(
                "Anthropic stream event is missing a valid content block index",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        return raw_index

    @staticmethod
    def _usage_int(payload: Mapping[str, Any], key: str) -> int | None:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _final_usage(self, payload: Mapping[str, Any]) -> MessageUsage | None:
        raw_usage = payload.get("usage")
        raw_usage = raw_usage if isinstance(raw_usage, dict) else {}
        output_tokens = self._usage_int(raw_usage, "output_tokens")
        # 有的中转站把输入和缓存的数放在 message_delta 里（累计值），有就以它为准。
        for key, attribute in (
            ("input_tokens", "_input_tokens"),
            ("cache_read_input_tokens", "_cached_input_tokens"),
            ("cache_creation_input_tokens", "_cache_creation_tokens"),
        ):
            value = self._usage_int(raw_usage, key)
            if value is not None:
                setattr(self, attribute, value)
        if self._input_tokens is None or output_tokens is None:
            return None
        # Anthropic 的 input_tokens 不含读缓存、写缓存那两部分（另报 cache_read_input_tokens /
        # cache_creation_input_tokens）：总输入是三者相加，命中率 = 读缓存 / 总输入。以前拿不含缓存的
        # input_tokens 当分母，命中率常被压成 99%。
        total_input = self._input_tokens + (self._cached_input_tokens or 0) + (self._cache_creation_tokens or 0)
        try:
            return MessageUsage(
                input_tokens=total_input,
                output_tokens=output_tokens,
                total_tokens=total_input + output_tokens,
                cached_input_tokens=self._cached_input_tokens,
                cached_input_tokens_complete=self._cached_input_tokens is not None,
            )
        except ValueError:
            return None

    def decode(self, data: str) -> _DecodedProviderChunk:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                "Provider emitted malformed SSE JSON",
                code="agent_provider_protocol_error",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProviderError(
                "Provider emitted a non-object SSE payload",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise AgentProviderError(
                "Anthropic stream event is missing its type",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if event_type == "error":
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise AgentProviderError(
                str(message or "Provider returned an error event"),
                code="agent_provider_error_event",
                retryable=True,
            )

        text_events: list[ProviderTextDelta] = []
        reasoning_events: list[ProviderReasoningDelta] = []
        tool_fragments: list[_ProviderToolCallFragment] = []
        finish_reason: str | None = None
        stream_done = False
        usage: MessageUsage | None = None
        reported_model: str | None = None

        if event_type == "message_start":
            message = payload.get("message")
            message = message if isinstance(message, dict) else {}
            raw_model = message.get("model")
            if raw_model is not None:
                if not isinstance(raw_model, str):
                    raise AgentProviderError(
                        "Provider emitted a non-string reported model identity",
                        code="agent_provider_reported_model_invalid",
                        retryable=False,
                    )
                reported_model = raw_model.strip()
                if not reported_model or len(reported_model) > 512:
                    raise AgentProviderError(
                        "Provider emitted an invalid reported model identity",
                        code="agent_provider_reported_model_invalid",
                        retryable=False,
                    )
            raw_usage = message.get("usage")
            if isinstance(raw_usage, dict):
                self._input_tokens = (
                    self._usage_int(raw_usage, "input_tokens") or self._input_tokens
                )
                self._cached_input_tokens = self._usage_int(
                    raw_usage, "cache_read_input_tokens"
                )
                self._cache_creation_tokens = self._usage_int(
                    raw_usage, "cache_creation_input_tokens"
                )
        elif event_type == "content_block_start":
            index = self._block_index(payload)
            block = payload.get("content_block")
            block = block if isinstance(block, dict) else {}
            block_type = block.get("type")
            if block_type == "text":
                initial = block.get("text")
                if isinstance(initial, str) and initial:
                    text_events.append(ProviderTextDelta(initial))
            elif block_type == "thinking":
                initial = block.get("thinking")
                if isinstance(initial, str) and initial:
                    reasoning_events.append(ProviderReasoningDelta(initial))
            elif block_type == "tool_use":
                call_id = block.get("id")
                name = block.get("name")
                raw_input = block.get("input")
                tool_fragments.append(
                    _ProviderToolCallFragment(
                        index=index,
                        id_fragment=call_id if isinstance(call_id, str) else None,
                        name_fragment=name if isinstance(name, str) else None,
                        arguments_fragment=(
                            json.dumps(raw_input, ensure_ascii=False)
                            if isinstance(raw_input, dict) and raw_input
                            else None
                        ),
                    )
                )
        elif event_type == "content_block_delta":
            index = self._block_index(payload)
            delta = payload.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    text_events.append(ProviderTextDelta(text))
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking")
                if isinstance(thinking, str) and thinking:
                    reasoning_events.append(ProviderReasoningDelta(thinking))
            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json")
                if isinstance(partial, str) and partial:
                    tool_fragments.append(
                        _ProviderToolCallFragment(
                            index=index,
                            id_fragment=None,
                            name_fragment=None,
                            arguments_fragment=partial,
                        )
                    )
            # signature_delta 等其它增量与正文/工具无关，忽略
        elif event_type == "message_delta":
            delta = payload.get("delta")
            delta = delta if isinstance(delta, dict) else {}
            stop_reason = delta.get("stop_reason")
            if stop_reason is not None:
                finish_reason = {
                    "end_turn": "stop",
                    "stop_sequence": "stop",
                    "max_tokens": "length",
                    "tool_use": "tool_calls",
                }.get(str(stop_reason), "stop")
            usage = self._final_usage(payload)
        elif event_type == "message_stop":
            stream_done = True
        # ping / content_block_stop / 未知类型：不产生事件

        return _DecodedProviderChunk(
            tuple(text_events),
            tuple(reasoning_events),
            tuple(tool_fragments),
            stream_done,
            finish_reason,
            usage,
            reported_model,
        )


class _RepetitionGuard:
    """盯流式正文里的「循环复读」，转起来就让调用方掐掉。

    三次真实事故形态不同，都要认出来：
    - run_91cdc08f84：同一短句「请直接改正文。」连吐 2054 段、22528 字；
    - run_25eb704cd2：把整段系统指引（每遍 700+ 字、十来句）复读 40 遍、30992 字；
    - run_2bdf0af522（9-27「接着写第 5 章」）：写了 1200 字正文后，把一段 31 句、493 字的正文转了 25 圈、1.3 万字，
      旧闸门只找 20 句以内的循环节，没认出来，一直吐到中转站 482 秒掐断连接。
    所以不能只看「相邻两句一样」，要看**句子序列有没有周期**：
    末尾若干句能被一个长度为 p 的循环节整除地铺满，就是转起来了。

    起因不在本机（同一份逐字载荷、同一套工具、同样参数，本地 5 种配置都复现
    不出来，证据指向中转/上游），但让模型复读到烧穿预算是我们自己的处置问题。
    与协议无关，Chat 和 Responses 两条线共用。
    """

    __slots__ = ("_tail", "_segments", "_ends", "_consumed", "_repeated", "_keep_chars")

    # 单句复读要连着这么多次才算数（模型偶尔强调两三遍是正常的）
    SINGLE_LINE_CYCLES = REPETITION_SEGMENT_LIMIT
    # 多句循环节转满这么多圈就算数——正常正文不会把同一组句子一字不差地转三圈
    MULTI_LINE_CYCLES = 3
    # 只找不超过这么长（句数）的循环节，也只回看这么多句。每凑齐一句才判断一次，
    # 每个周期先比最后一句和一圈之前那句，对不上就跳过，所以循环节放长也几乎不费事。
    MAX_PERIOD = 120
    HISTORY = MAX_PERIOD * MULTI_LINE_CYCLES

    def __init__(self) -> None:
        self._tail = ""
        self._segments: list[str] = []
        # 每一句在整段输出里的结尾位置（字符数），判到复读时用来算「第一遍到哪儿为止」。
        self._ends: list[int] = []
        self._consumed = 0
        self._repeated = ""
        self._keep_chars = 0

    def observe(self, text: str) -> bool:
        """喂一段增量；返回是否已经判定为循环复读。"""
        self._tail += text
        added = False
        while True:
            boundary = -1
            for marker in ("\n", "。", "！", "？", "…", ".", "!", "?"):
                index = self._tail.find(marker)
                if index >= 0 and (boundary < 0 or index < boundary):
                    boundary = index
            if boundary < 0:
                break
            segment = self._tail[: boundary + 1].strip()
            self._tail = self._tail[boundary + 1 :]
            self._consumed += boundary + 1
            # 只数带字的句子。纯标点段——「......」「！！！」「……」这类省略号、
            # 感叹连打——是网文里到处都有的正常写法，不是循环复读，不能计数；
            # 否则一串 12 个点就会把整次回答掐掉（实测会误伤）。
            if segment and any(ch.isalnum() for ch in segment):
                self._segments.append(segment)
                self._ends.append(self._consumed)
                added = True
                if len(self._segments) > self.HISTORY:
                    del self._segments[: len(self._segments) - self.HISTORY]
                    del self._ends[: len(self._ends) - self.HISTORY]
        if len(self._tail) > REPETITION_SEGMENT_MAX_CHARS * 20:
            # 一直没有句读的超长输出：不是本闸门要管的形态，别攒着占内存
            self._consumed += len(self._tail) - REPETITION_SEGMENT_MAX_CHARS
            self._tail = self._tail[-REPETITION_SEGMENT_MAX_CHARS:]
        return added and self._detect()

    def _detect(self) -> bool:
        segments = self._segments
        count = len(segments)
        for period in range(1, self.MAX_PERIOD + 1):
            cycles = self.SINGLE_LINE_CYCLES if period == 1 else self.MULTI_LINE_CYCLES
            needed = period * cycles
            if count < needed or segments[-1] != segments[-1 - period]:
                continue
            start = count - needed
            if all(
                segments[index] == segments[index + period]
                for index in range(start, count - period)
            ):
                self._repeated = segments[start]
                # 往前找这个循环节最早从哪一句开始转（判定只看了最后几圈），留下第一遍、后面的都算多出来的。
                first = start
                while first > 0 and segments[first - 1] == segments[first - 1 + period]:
                    first -= 1
                self._keep_chars = self._ends[first + period - 1]
                return True
        return False

    @property
    def repeated_segment(self) -> str:
        return self._repeated

    @property
    def keep_chars(self) -> int:
        """判到复读时，整段输出里到第一遍循环节结尾为止的字符数（后面是转圈转出来的）。"""
        return self._keep_chars


class AgentProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        # 诊断信息（绝不含密钥/头值）：进 run 的错误记录可查，不直接甩给作者。
        self.details = details


class AgentProviderCancelled(AgentProviderError):
    def __init__(self) -> None:
        super().__init__(
            "Agent run was cancelled",
            code="agent_run_cancelled",
            retryable=False,
        )


# ---------------------------------------------------------------------------
# transport 韧性：熔断器 + 并发限流。
#
# 状态放在进程内的模块级注册表（不落库）：多 run 共享同一 provider 的熔断
# 状态正是它的意义——一个坏掉的中转不该被多个 run 轮番打爆。进程重启即
# 复位，这对「中转恢复了没」也是合理的默认。
# 这些参数（熔断/并发/重试/空闲超时）是运行时行为，不改变发给 provider 的
# 派发配置，所以一律不进 dispatch 指纹。
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    """按 provider 维度的熔断器。所有方法同步、带锁，供异步代码快速调用。"""

    __slots__ = ("_failures", "_opened_at", "_probe_inflight")

    def __init__(self) -> None:
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_inflight = False

    def before_request(self, *, threshold: int, cooldown_seconds: float) -> None:
        """开路期快速失败；冷却到点放行一个试探请求（半开）。"""
        with _breaker_lock:
            if self._opened_at is None:
                return
            remaining = cooldown_seconds - (time.monotonic() - self._opened_at)
            if remaining > 0 or self._probe_inflight:
                cooldown = max(1, math.ceil(remaining))
                raise AgentProviderError(
                    f"模型服务连续失败，已暂时停试，约 {cooldown} 秒后再试。",
                    code="agent_provider_circuit_open",
                    retryable=True,
                    details={
                        "cooldown_remaining_seconds": cooldown,
                        "consecutive_failures": self._failures,
                    },
                )
            self._probe_inflight = True

    def on_success(self) -> None:
        with _breaker_lock:
            self._failures = 0
            self._opened_at = None
            self._probe_inflight = False

    def on_failure(self, *, countable: bool, threshold: int) -> None:
        with _breaker_lock:
            if self._probe_inflight:
                self._probe_inflight = False
                if countable:
                    # 试探失败：重新开路，冷却从头算起
                    self._opened_at = time.monotonic()
                return
            if not countable:
                return
            self._failures += 1
            if self._failures >= threshold:
                self._opened_at = time.monotonic()

    def on_abort(self) -> None:
        """取消/消费者提前关闭：既不是成功也不是失败，只还试探位。"""
        with _breaker_lock:
            self._probe_inflight = False


_breaker_lock = threading.Lock()
_breakers: dict[str, _CircuitBreaker] = {}

_limiter_lock = threading.Lock()
# key → (创建时所在的 event loop, 信号量, 上限)。
# asyncio.Semaphore 会绑定到第一次使用它的 loop；本运行时是单 loop 的
# FastAPI 进程，而测试会反复 asyncio.run（每次一个新 loop），所以按 loop
# 记录：发现调用方在另一个 loop 上时重建信号量。旧 loop 已关闭（测试场景）
# 时这完全安全；配置上限变化也会重建，旧等待者随旧 loop 一起退出。
_limiters: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore, int]] = {}


def _circuit_breaker_for(key: str) -> _CircuitBreaker:
    with _breaker_lock:
        breaker = _breakers.get(key)
        if breaker is None:
            breaker = _CircuitBreaker()
            _breakers[key] = breaker
        return breaker


def _concurrency_limiter_for(key: str, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _limiter_lock:
        entry = _limiters.get(key)
        if entry is not None and entry[0] is loop and entry[2] == limit:
            return entry[1]
        semaphore = asyncio.Semaphore(limit)
        _limiters[key] = (loop, semaphore, limit)
        return semaphore


def _reset_resilience_registries() -> None:
    """测试隔离用：清空熔断与限流状态。生产代码不调用。"""
    with _breaker_lock:
        _breakers.clear()
    with _limiter_lock:
        _limiters.clear()


async def _acquire_limiter(
    semaphore: asyncio.Semaphore,
    cancel_event: asyncio.Event,
) -> None:
    """排队等并发额度；等待中被取消要抛 AgentProviderCancelled，不能挂死。"""
    if cancel_event.is_set():
        raise AgentProviderCancelled()
    acquire_task = asyncio.create_task(semaphore.acquire())
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {acquire_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        cancelled = cancel_task in done and cancel_task.result()
        if cancelled or cancel_event.is_set():
            raise AgentProviderCancelled()
        acquire_task.result()
        cancel_task.cancel()
        await asyncio.gather(cancel_task, return_exceptions=True)
        if cancel_event.is_set():
            raise AgentProviderCancelled()
    except BaseException:
        # Task.cancel / wait_for 超时也会离开这里。先终止两个子任务，再确认
        # acquire 是否已经拿到了额度；只有成功返回才把额度交给调用方。
        acquire_task.cancel()
        cancel_task.cancel()
        try:
            await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)
        finally:
            if (
                acquire_task.done()
                and not acquire_task.cancelled()
                and acquire_task.exception() is None
                and acquire_task.result()
            ):
                semaphore.release()
        raise


class AgentProvider(Protocol):
    @property
    def identity(self) -> ExecutorIdentity: ...

    @property
    def capabilities(self) -> CapabilityProfile: ...

    @property
    def dispatch_config_fingerprint(self) -> str: ...

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]: ...


def _image_message_parts(parts: list[Any], wire: str) -> list[dict[str, Any]]:
    """Translate hydrated user text/images without reducing images to text."""
    result: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            result.append({"type": "input_text" if wire == "responses" else "text", "text": part["text"]})
            continue
        image = part.get("image_url") if isinstance(part, dict) and part.get("type") == "image_url" else None
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not url:
            raise AgentProviderError("图片消息尚未准备好，无法发送给模型。", code="agent_provider_protocol_error", retryable=False)
        if wire == "responses":
            block: dict[str, Any] = {"type": "input_image", "image_url": url}
            if image.get("detail") in {"auto", "low", "high", "original"}:
                block["detail"] = image["detail"]
            result.append(block)
        else:
            if url.startswith("data:"):
                header, comma, data = url.partition(",")
                media_type = header[5:].removesuffix(";base64")
                if not comma or not data or not header.endswith(";base64") or not media_type.startswith("image/"):
                    raise AgentProviderError("图片消息的编码格式无效。", code="agent_provider_protocol_error", retryable=False)
                source = {"type": "base64", "media_type": media_type, "data": data}
            elif url.startswith(("https://", "http://")):
                source = {"type": "url", "url": url}
            else:
                raise AgentProviderError("图片消息尚未准备好，无法发送给模型。", code="agent_provider_protocol_error", retryable=False)
            result.append({"type": "image", "source": source})
    return result


class OpenAICompatibleAgentProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        provider_id: str | None = None,
        api_key: str | None,
        credential_reference: str | None,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        reasoning_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"] = "auto",
        wire_format: Literal["chat", "responses", "anthropic_messages"] = "chat",
        auth_type: Literal["bearer", "x-api-key", "custom", "none"] = "bearer",
        extra_headers: Mapping[str, str] | None = None,
        # 10-07 作者：默认伪装 codex-cli；author 显式 extra_headers 里的 User-Agent 优先。
        default_user_agent: str = "codex-cli/0.55.0",
        max_retries: int | None = None,
        stream_idle_timeout_seconds: float | None = None,
        max_concurrent_requests: int = 4,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        max_tool_argument_bytes: int = MAX_TOOL_ARGUMENT_BYTES,
        accepted_reported_models: Sequence[str] = (),
        context_window: str = "256k",
        is_probe: bool = False,
    ) -> None:
        if (
            not isinstance(max_tool_argument_bytes, int)
            or isinstance(max_tool_argument_bytes, bool)
            or max_tool_argument_bytes < 2
        ):
            raise ValueError("max_tool_argument_bytes must be at least 2")
        normalized_reference = (
            None if credential_reference is None else credential_reference.strip()
        )
        if normalized_reference == "":
            normalized_reference = None
        if (api_key is None) != (normalized_reference is None):
            raise ValueError(
                "api_key and credential_reference must be configured together"
            )
        if normalized_reference is not None and (
            len(normalized_reference) > 256
            or any(character.isspace() for character in normalized_reference)
        ):
            raise ValueError("credential_reference must be a compact non-secret identifier")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider_id = provider_id.strip() if isinstance(provider_id, str) and provider_id.strip() else None
        self.api_key = api_key
        self.credential_reference = normalized_reference
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.temperature = temperature
        if reasoning_effort not in {"auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("reasoning_effort is invalid")
        self.reasoning_effort = reasoning_effort
        if wire_format not in {"chat", "responses", "anthropic_messages"}:
            raise ValueError("wire_format is invalid")
        self.wire_format = wire_format
        if auth_type not in {"bearer", "x-api-key", "custom", "none"}:
            raise ValueError("auth_type is invalid")
        self.auth_type = auth_type
        cleaned_headers: dict[str, str] = {}
        for header_name, header_value in (extra_headers or {}).items():
            if not isinstance(header_name, str) or not _HEADER_NAME_PATTERN.fullmatch(
                header_name.strip()
            ):
                raise ValueError("extra_headers names must be valid HTTP header names")
            if not isinstance(header_value, str) or any(
                ord(character) < 32 or ord(character) == 127
                for character in header_value
            ):
                raise ValueError("extra_headers values must be control-character-free text")
            if len(header_value) > 2000 or len(cleaned_headers) >= 32:
                raise ValueError("extra_headers must be bounded")
            cleaned_headers[header_name.strip()] = header_value
        # UA：author 显式配的优先（在 cleaned_headers 里），没有就用 default_user_agent（默认 codex-cli）。
        if not any(k.lower() == "user-agent" for k in cleaned_headers):
            cleaned_headers["User-Agent"] = default_user_agent
        self.extra_headers = cleaned_headers
        self.default_user_agent = default_user_agent
        if max_retries is not None and (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 10
        ):
            raise ValueError("max_retries must be an integer between 0 and 10")
        self.max_retries = max_retries
        # 上下文窗口档位（256k / 1m）：预算全由它查表决定（model_context.py）。
        self.context_window = context_window
        if stream_idle_timeout_seconds is not None and not (
            0.01 <= float(stream_idle_timeout_seconds) <= 3600.0
        ):
            raise ValueError("stream_idle_timeout_seconds must be between 0.01 and 3600")
        self.stream_idle_timeout_seconds = (
            None
            if stream_idle_timeout_seconds is None
            else float(stream_idle_timeout_seconds)
        )
        if (
            isinstance(max_concurrent_requests, bool)
            or not isinstance(max_concurrent_requests, int)
            or not 1 <= max_concurrent_requests <= 32
        ):
            raise ValueError("max_concurrent_requests must be an integer between 1 and 32")
        self.max_concurrent_requests = max_concurrent_requests
        self.is_probe = is_probe
        if (
            isinstance(circuit_breaker_threshold, bool)
            or not isinstance(circuit_breaker_threshold, int)
            or circuit_breaker_threshold < 1
        ):
            raise ValueError("circuit_breaker_threshold must be a positive integer")
        self.circuit_breaker_threshold = circuit_breaker_threshold
        if not 0.01 <= float(circuit_breaker_cooldown_seconds) <= 3600.0:
            raise ValueError(
                "circuit_breaker_cooldown_seconds must be between 0.01 and 3600"
            )
        self.circuit_breaker_cooldown_seconds = float(circuit_breaker_cooldown_seconds)
        self._client = client
        self.max_tool_argument_bytes = max_tool_argument_bytes
        normalized_aliases: list[str] = []
        for alias in accepted_reported_models:
            if not isinstance(alias, str):
                raise ValueError("accepted_reported_models must contain strings")
            normalized = alias.strip()
            if not normalized or len(normalized) > 512:
                raise ValueError(
                    "accepted_reported_models must contain bounded non-blank model names"
                )
            if normalized == self.model:
                continue
            if normalized not in normalized_aliases:
                normalized_aliases.append(normalized)
        self.accepted_reported_models = tuple(sorted(normalized_aliases))
        # Chat 线的指纹文档逐位保持原样——作者机器上已有的 provider-io-boundary
        # 收据是按这份配方算出来的，动一个键就会让旧收据全部对不上。
        # Responses 是另一条线，用自己的 schema 与端点，天然不与旧收据混淆。
        if self.wire_format == "responses":
            dispatch_config = {
                "schema": "zhimeng.openai-compatible-dispatch-config.v1",
                "endpoint": resolve_endpoint(self.base_url, "responses"),
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_output_tokens": self.max_tokens,
                "temperature": self.temperature,
                "reasoning_effort": self.reasoning_effort,
                "stream": True,
                "tool_choice": "auto",
                "max_tool_argument_bytes": self.max_tool_argument_bytes,
                "accepted_reported_models": list(self.accepted_reported_models),
                "credential_reference": self.credential_reference,
                "wire_format": "responses",
            }
        elif self.wire_format == "anthropic_messages":
            dispatch_config = {
                "schema": "zhimeng.openai-compatible-dispatch-config.v1",
                "endpoint": resolve_endpoint(self.base_url, "anthropic_messages"),
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "reasoning_effort": self.reasoning_effort,
                "stream": True,
                "tool_choice": "auto",
                "max_tool_argument_bytes": self.max_tool_argument_bytes,
                "accepted_reported_models": list(self.accepted_reported_models),
                "credential_reference": self.credential_reference,
                "wire_format": "anthropic_messages",
            }
        else:
            dispatch_config = {
                "schema": "zhimeng.openai-compatible-dispatch-config.v1",
                "endpoint": resolve_endpoint(self.base_url, "chat"),
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "reasoning_effort": self.reasoning_effort,
                "stream": True,
                "include_usage": True,
                "tool_choice": "auto",
                "max_tool_argument_bytes": self.max_tool_argument_bytes,
                "accepted_reported_models": list(self.accepted_reported_models),
                "credential_reference": self.credential_reference,
            }
        # 红线同上：只有非默认的认证形态才往指纹里并新键——
        # bearer + 无自定义头时 payload 必须与改造前逐字节一致。
        # 头的值视同密钥，只并头名（排序后确定性）；改值不换指纹，
        # 密钥本身的变更由 credential_reference 表达。
        if self.auth_type != "bearer":
            dispatch_config["auth_type"] = self.auth_type
        if self.extra_headers:
            dispatch_config["extra_header_names"] = sorted(self.extra_headers)
        self._dispatch_config_fingerprint = canonical_json_hash(dispatch_config)

    @property
    def dispatch_config_fingerprint(self) -> str:
        return self._dispatch_config_fingerprint

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.openai-compatible",
            kind=ExecutorKind.REMOTE,
            implementation=(
                "openai-compatible-responses"
                if self.wire_format == "responses"
                else (
                    "openai-compatible-anthropic-messages"
                    if self.wire_format == "anthropic_messages"
                    else "openai-compatible-chat-completions"
                )
            ),
            version=__version__,
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    @property
    def _resilience_key(self) -> str:
        # 熔断与并发限流按 provider 维度共享：有渠道 ID 用 ID，否则按地址
        return self.provider_id or self.base_url

    @staticmethod
    def _counts_toward_circuit(exc: AgentProviderError) -> bool:
        """只有「上游故障类」计入熔断：5xx、超时、连接错误。
        401/403/400 是配置错误，429 是限流，都不计。"""
        if exc.code in {
            "agent_provider_timeout",
            "agent_provider_request_failed",
            "agent_provider_stream_idle_timeout",
        }:
            return True
        if exc.code == "agent_provider_http_error":
            status = (exc.details or {}).get("upstream_status")
            return isinstance(status, int) and status >= 500
        return False

    def _scrub_secret_material(self, text: str) -> str:
        """把完整密钥与自定义头的值从文本里抹掉（诊断要引用这段文本）。"""
        scrubbed = text
        for secret in (self.api_key, *self.extra_headers.values()):
            if secret and len(secret) >= 4:
                scrubbed = scrubbed.replace(secret, "***")
        return scrubbed

    def with_reasoning_effort(self, effort: str) -> "OpenAICompatibleAgentProvider":
        """同一条线路、同一个模型，只换思考强度的一份（共用连接和熔断记账）。

        后台的小活（给对话起名、挑要记住的事）用 low：9-29 真机，跟着对话的 max 走，grok-4.6 挑一次记忆要想一分半钟。
        渠道不认这一档时，发请求那里照旧摘掉这个参数按服务默认重试。
        """
        if effort not in {"auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("reasoning_effort is invalid")
        variant = copy.copy(self)
        variant.reasoning_effort = effort
        return variant

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        # 测活运行在临时事件循环里，实际传输照旧，但不读写正式对话的熔断和并发池。
        if self.is_probe:
            async for event in self._stream_chat_events(messages, cancel_event=cancel_event, tools=tools):
                yield event
            return
        # 熔断：开路期在吐出任何流事件之前快速失败，人话消息带冷却秒数。
        breaker = _circuit_breaker_for(self._resilience_key)
        breaker.before_request(
            threshold=self.circuit_breaker_threshold,
            cooldown_seconds=self.circuit_breaker_cooldown_seconds,
        )
        # 并发限流：同一 provider 的打开+读取阶段共享一个额度池。
        limiter = _concurrency_limiter_for(
            self._resilience_key, self.max_concurrent_requests
        )
        acquired = False
        try:
            await _acquire_limiter(limiter, cancel_event)
            acquired = True
            async for event in self._stream_chat_events(
                messages,
                cancel_event=cancel_event,
                tools=tools,
            ):
                yield event
        except AgentProviderCancelled:
            breaker.on_abort()
            raise
        except AgentProviderError as exc:
            breaker.on_failure(
                countable=self._counts_toward_circuit(exc),
                threshold=self.circuit_breaker_threshold,
            )
            raise
        except (GeneratorExit, asyncio.CancelledError):
            breaker.on_abort()
            raise
        else:
            breaker.on_success()
        finally:
            if acquired:
                limiter.release()

    async def _stream_chat_events(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        responses_wire = self.wire_format == "responses"
        anthropic_wire = self.wire_format == "anthropic_messages"
        endpoint = resolve_endpoint(self.base_url, self.wire_format)
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if anthropic_wire:
            # Anthropic 协议头，extra_headers 可以覆盖（比如网关要别的版本号）
            headers["anthropic-version"] = "2023-06-01"
        # anthropic 线的认证默认建议 x-api-key；作者显式配的 custom/none 优先
        # （bearer 是全局默认值，无法与「显式选 bearer」区分，按协议默认处理）。
        effective_auth = (
            "x-api-key"
            if anthropic_wire and self.auth_type == "bearer"
            else self.auth_type
        )
        if self.api_key:
            if effective_auth == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif effective_auth == "x-api-key":
                headers["x-api-key"] = self.api_key
            # custom / none：认证头由作者自己在 extra_headers 里写，或干脆不带。
        for header_name, header_value in self.extra_headers.items():
            # Content-Type / Accept 是本协议请求的一部分，不许被覆盖；
            # Authorization 可以被覆盖——那正是 custom 的用途。
            if header_name.lower() in ("content-type", "accept"):
                continue
            # HTTP 头名不区分大小写，覆盖时清掉旧拼法，避免同时发出两份认证值。
            for existing_name in tuple(headers):
                if existing_name.lower() == header_name.lower():
                    del headers[existing_name]
            headers[header_name] = header_value
        tool_names = [tool.name for tool in tools]
        if len(tool_names) != len(set(tool_names)):
            raise AgentProviderError(
                "Provider tool definitions must have unique function names",
                code="agent_provider_tool_definition_invalid",
                retryable=False,
            )
        if anthropic_wire:
            system_prompt, anthropic_messages = self._build_anthropic_input(messages)
            payload = {
                "model": self.model,
                "messages": anthropic_messages,
                "stream": True,
                # max_tokens 在 Anthropic Messages 里是必填
                "max_tokens": self.max_tokens,
            }
            if system_prompt is not None:
                # 缓存断点：system 以 blocks 形态发出并在最后一块标记
                # cache_control，上游对整段稳定前缀开提示缓存。
                payload["system"] = [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            thinking_budget = self._anthropic_thinking_budget()
            if thinking_budget is not None:
                payload["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": thinking_budget,
                }
            else:
                # 官方协议里 thinking 与自定义 temperature 互斥；
                # 开了思考就不发 temperature，交给服务端默认。
                payload["temperature"] = self.temperature
            if tools:
                anthropic_tools = [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.parameters,
                    }
                    for tool in tools
                ]
                # 工具清单也是稳定前缀的一部分：最后一项加缓存断点。
                anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
                payload["tools"] = anthropic_tools
        elif responses_wire:
            instructions, input_items = self._build_responses_input(messages)
            payload = {
                "model": self.model,
                "input": input_items,
                "stream": True,
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
            }
            if instructions is not None:
                payload["instructions"] = instructions
            cache_key = PROMPT_CACHE_KEY.get()
            if cache_key:
                payload["prompt_cache_key"] = cache_key
            # 请求上游可公开的推理摘要；这和思考强度是两项独立能力。
            payload["reasoning"] = {"summary": "auto"}
            if self.reasoning_effort != "auto":
                payload["reasoning"]["effort"] = self.reasoning_effort
            if tools:
                payload.update(
                    {
                        "tools": [
                            self._encode_responses_tool_definition(tool) for tool in tools
                        ],
                        "tool_choice": "auto",
                    }
                )
        else:
            payload = {
                "model": self.model,
                "messages": [dict(message) for message in messages],
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.reasoning_effort != "auto":
                payload["reasoning_effort"] = self.reasoning_effort
            if tools:
                payload.update(
                    {
                        "tools": [self._encode_tool_definition(tool) for tool in tools],
                        "tool_choice": "auto",
                    }
                )
        if cancel_event.is_set():
            raise AgentProviderCancelled()
        # 合成 tool_call id 的回合盐：同一 Run 里不同回合、字节级相同的
        # 两次调用（模型改完想重读是正常行为）若拿到相同 id，会被幂等
        # 层判定为重放而返回陈旧结果。盐取自本次 stream_chat。
        stream_salt = uuid4().hex
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(15.0, self.timeout_seconds),
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        started_at = time.monotonic()
        diagnostics: dict[str, Any] = {
            "wire_format": self.wire_format,
            "request_phase": "preparing",
            "request_attempt": 0,
            "response_headers_received": False,
            "response_status": None,
            "stream_lines_received": 0,
        }
        try:
            async with AsyncExitStack() as stack:
                response, effective_effort = await self._open_verified_response(
                    stack,
                    client,
                    endpoint=endpoint,
                    headers=headers,
                    payload=payload,
                    cancel_event=cancel_event,
                    diagnostics=diagnostics,
                )
                content_type = response.headers.get("content-type", "").lower()
                if "text/event-stream" not in content_type:
                    raise AgentProviderError(
                        f"Provider returned non-SSE content type: {content_type or 'missing'}",
                        code="agent_provider_protocol_error",
                        retryable=False,
                    )

                data_lines: list[str] = []
                saw_done = False
                saw_text = False
                finish_reason: str | None = None
                usage: MessageUsage | None = None
                reported_model: str | None = None
                tool_calls: dict[int, _ProviderToolCallAccumulator] = {}
                if anthropic_wire:
                    anthropic_decoder = _AnthropicEventDecoder()
                    decode = anthropic_decoder.decode
                else:
                    decode = (
                        self._decode_responses_event
                        if responses_wire
                        else self._decode_event
                    )
                repetition = _RepetitionGuard()
                diagnostics["request_phase"] = "reading_stream"
                async for line in self._iter_response_lines(response, cancel_event):
                    diagnostics["stream_lines_received"] += 1
                    if not line:
                        if not data_lines:
                            continue
                        chunk = decode("\n".join(data_lines))
                        data_lines.clear()
                        for fragment in chunk.tool_fragments:
                            self._accumulate_tool_call(tool_calls, fragment)
                        for event in chunk.reasoning_events:
                            yield event
                        for event in chunk.text_events:
                            saw_text = True
                            if repetition.observe(event.text):
                                raise AgentProviderError(
                                    "Provider output is looping on the same content: "
                                    f'"{repetition.repeated_segment}"',
                                    code="agent_provider_output_repeating",
                                    retryable=True,
                                    details={
                                        "repeated_segment": repetition.repeated_segment[:200],
                                        "keep_chars": repetition.keep_chars,
                                    },
                                )
                            yield event
                        saw_done = saw_done or chunk.stream_done
                        finish_reason = self._merge_finish_reason(
                            finish_reason,
                            chunk.finish_reason,
                        )
                        usage = chunk.usage or usage
                        reported_model = self._merge_reported_model(
                            reported_model,
                            chunk.reported_model,
                        )
                        if saw_done:
                            break
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

                if data_lines and not saw_done:
                    chunk = decode("\n".join(data_lines))
                    for fragment in chunk.tool_fragments:
                        self._accumulate_tool_call(tool_calls, fragment)
                    for event in chunk.reasoning_events:
                        yield event
                    for event in chunk.text_events:
                        saw_text = True
                        if repetition.observe(event.text):
                            raise AgentProviderError(
                                "Provider output is looping on the same content: "
                                f'"{repetition.repeated_segment}"',
                                code="agent_provider_output_repeating",
                                retryable=True,
                                details={
                                    "repeated_segment": repetition.repeated_segment[:200],
                                    "keep_chars": repetition.keep_chars,
                                },
                            )
                        yield event
                    saw_done = saw_done or chunk.stream_done
                    finish_reason = self._merge_finish_reason(
                        finish_reason,
                        chunk.finish_reason,
                    )
                    usage = chunk.usage or usage
                    reported_model = self._merge_reported_model(
                        reported_model,
                        chunk.reported_model,
                    )

                if cancel_event.is_set():
                    raise AgentProviderCancelled()
                if not saw_done and finish_reason is None:
                    raise AgentProviderError(
                        "Provider stream closed without a terminal event",
                        code="agent_provider_incomplete_stream",
                        retryable=True,
                    )
                normalized_finish = finish_reason or "stop"
                if normalized_finish == "length" and not tool_calls:
                    raise AgentProviderError(
                        "Provider stopped because the output token limit was reached",
                        code="agent_provider_output_truncated",
                        retryable=True,
                    )
                if normalized_finish == "length":
                    # 写工具调用时撞上单次输出上限：参数多半没写完（流式参数是边收边拼的，截断的也可能恰好能解析）。
                    # 以前整轮当「正文写到半句」处理——没吐字就判失败，吐过字就让模型「接着写」，它会在对话里接着写正文。
                    # 照 Pi（agent-loop.ts:468-500）：这一条里的调用都不执行，原因作为每个调用的结果回给模型。
                    for accumulator in tool_calls.values():
                        if accumulator.arguments_error is None:
                            accumulator.arguments_error = (
                                f"这一条回答到了单次输出的长度上限（{self.max_tokens} tokens），"
                                "工具参数可能没写完。内容一次写不下可以分几次写"
                            )
                if normalized_finish == "function_call":
                    raise AgentProviderError(
                        "Provider requested the unsupported legacy function_call protocol",
                        code="agent_provider_tool_call_unsupported",
                        retryable=False,
                    )
                completed_tool_calls = self._complete_tool_calls(tool_calls, salt=stream_salt)
                # 中转站现实：一些网关带着完整 tool_calls 却把 finish_reason 报成
                # "stop"。工具调用本身已逐字段校验过，据此照常转交，不再按协议
                # 错误终止整个 Run。未通告的工具名同样放行，由 AgentService 把
                # 失败结果回给模型自我纠正（有失败上限兜底）。
                if completed_tool_calls and not tools:
                    raise AgentProviderError(
                        "Provider requested a tool call when no tools were supplied",
                        code="agent_provider_tool_call_unsupported",
                        retryable=False,
                    )
                if (
                    normalized_finish == "tool_calls"
                    and not completed_tool_calls
                    and not saw_text
                ):
                    raise AgentProviderError(
                        "Provider ended with tool_calls but emitted no complete tool call",
                        code="agent_provider_tool_call_invalid",
                        retryable=False,
                    )
                if not saw_text and not completed_tool_calls:
                    raise AgentProviderError(
                        "Provider completed without assistant text or tool calls",
                        code="agent_provider_empty_response",
                        retryable=True,
                    )
                for tool_call in completed_tool_calls:
                    yield tool_call
                yield ProviderCompleted(
                    finish_reason=normalized_finish,
                    usage=usage,
                    reported_model=reported_model,
                    effective_reasoning_effort=effective_effort,
                    effective_thinking_budget_tokens=(
                        payload["thinking"]["budget_tokens"]
                        if isinstance(payload.get("thinking"), dict)
                        and payload["thinking"].get("type") == "enabled"
                        else None
                    ),
                )
        except httpx.TimeoutException as exc:
            timeout_kind = next((name for error, name in (
                (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
                (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
            ) if isinstance(exc, error)), "unknown")
            raise AgentProviderError(
                f"Provider {timeout_kind} timed out",
                code="agent_provider_timeout",
                retryable=True,
                details={**diagnostics, "timeout_kind": timeout_kind, "elapsed_seconds": round(time.monotonic() - started_at, 3)},
            ) from exc
        except httpx.RequestError as exc:
            raise AgentProviderError(
                f"Provider request failed: {str(exc).strip() or type(exc).__name__}",
                code="agent_provider_request_failed",
                retryable=True,
                details={**diagnostics, "network_error_type": type(exc).__name__, "elapsed_seconds": round(time.monotonic() - started_at, 3)},
            ) from exc
        except AgentProviderError as exc:
            if exc.code == "agent_provider_stream_idle_timeout":
                exc.details = {**diagnostics, **(exc.details or {}), "timeout_kind": "stream_idle", "elapsed_seconds": round(time.monotonic() - started_at, 3)}
            raise
        finally:
            if owns_client:
                await client.aclose()

    async def _open_verified_response(
        self,
        stack: AsyncExitStack,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        payload: dict[str, Any],
        cancel_event: asyncio.Event,
        diagnostics: dict[str, Any] | None = None,
    ) -> tuple[httpx.Response, str | None]:
        # Some models reachable through OpenAI-compatible relays reject
        # `temperature` outright with HTTP 400 (for example Claude models served
        # by a relay). Dropping that one parameter and retrying once keeps those
        # models usable. HTTP 429 from a brief model cooldown is retried the
        # same way. Both retries happen before any event is yielded.
        allow_temperature_retry = "temperature" in payload
        # prompt_cache_key 只是给上游的路由提示；哪家点名不认，摘掉重试一次，不为它整轮失败。
        allow_cache_key_retry = "prompt_cache_key" in payload
        # Chat 线用扁平的 reasoning_effort，Responses 线用 reasoning:{effort}。
        # 渠道点名拒收哪个，就摘哪个重试一次。
        reasoning_key = "reasoning" if "reasoning" in payload else "reasoning_effort"
        allow_reasoning_retry = reasoning_key in payload
        allow_summary_retry = isinstance(payload.get("reasoning"), dict) and "summary" in payload["reasoning"]
        # 从请求本身读取，而不是复述设置：auto 不发 effort，Anthropic 发的是
        # thinking.budget_tokens，都不能声称发出了一个 reasoning_effort。
        requested_reasoning = payload.get(reasoning_key)
        effective_effort = (
            requested_reasoning.get("effort")
            if isinstance(requested_reasoning, dict)
            else requested_reasoning
        )
        # 默认 429/快速 502 重试 4 次、520/522/524 重试 2 次（保持现状）；
        # 渠道显式配了 max_retries 时两档一起覆盖。
        initial_busy_retries = self.max_retries if self.max_retries is not None else 4
        initial_gateway_retries = self.max_retries if self.max_retries is not None else 2
        remaining_busy_retries = initial_busy_retries
        # 520/522/524 单独记账：注释承诺「这一档最多重试 2 次」，但它和
        # 429/502 共用同一个计数器的话，先来两次 429 就会把 52x 的重试
        # 预算静默吃光。
        remaining_gateway_retries = initial_gateway_retries
        while True:
            request_started = asyncio.get_running_loop().time()
            response = await stack.enter_async_context(
                self._open_response(
                    client,
                    endpoint=endpoint,
                    headers=headers,
                    payload=payload,
                    cancel_event=cancel_event,
                    diagnostics=diagnostics,
                )
            )
            if 200 <= response.status_code < 300:
                return response, effective_effort
            if diagnostics is not None:
                diagnostics["request_phase"] = "reading_error_response"
            body = (await response.aread())[:2048].decode("utf-8", errors="replace")
            lowered = body.lower()
            # 这条响应走不到外层 AsyncExitStack 的正常退出（下面不是
            # continue 就是 raise，continue 时上下文永远挂在栈上不退出），
            # 不显式关闭会把最多 4 条死连接攥到整次流式请求结束。
            await response.aclose()
            if (
                response.status_code == 400
                and allow_temperature_retry
                and "temperature" in lowered
            ):
                allow_temperature_retry = False
                payload.pop("temperature", None)
                continue
            if (
                response.status_code == 400
                and allow_cache_key_retry
                and "prompt_cache_key" in lowered
            ):
                allow_cache_key_retry = False
                payload.pop("prompt_cache_key", None)
                continue
            if (
                response.status_code == 400
                and allow_summary_retry
                and ("reasoning.summary" in lowered or "reasoning[summary]" in lowered)
                and any(marker in lowered for marker in ("unsupported", "not support", "unknown", "unrecognized", "invalid", "not allowed"))
            ):
                # 中转只拒收摘要时保留原思考档位，正文/工具照常处理。
                allow_summary_retry = False
                reasoning = payload.get("reasoning")
                if isinstance(reasoning, dict):
                    reasoning.pop("summary", None)
                    if not reasoning:
                        payload.pop("reasoning", None)
                continue
            if (
                response.status_code == 400
                and allow_reasoning_retry
                and reasoning_key in lowered
                and any(marker in lowered for marker in ("deprecat", "unsupported", "not support", "unknown", "unrecognized", "invalid"))
            ):
                # 扩展档位被明确拒收时先尝试 high，再按服务默认值请求。
                # high 只是兼容性重试，不代表该服务支持的最高档。
                current_effort = None
                if reasoning_key == "reasoning_effort":
                    current_effort = payload.get("reasoning_effort")
                elif reasoning_key == "reasoning" and isinstance(payload.get("reasoning"), dict):
                    current_effort = payload["reasoning"].get("effort")

                if current_effort in ("max", "xhigh", "ultra"):
                    effective_effort = "high"
                    if reasoning_key == "reasoning_effort":
                        payload["reasoning_effort"] = "high"
                    elif reasoning_key == "reasoning" and isinstance(payload.get("reasoning"), dict):
                        payload["reasoning"]["effort"] = "high"
                    continue

                allow_reasoning_retry = False
                effective_effort = None
                if reasoning_key == "reasoning" and "effort" in lowered and isinstance(payload.get("reasoning"), dict):
                    payload["reasoning"].pop("effort", None)
                    if not payload["reasoning"]:
                        payload.pop("reasoning", None)
                else:
                    payload.pop(reasoning_key, None)
                continue
            request_elapsed = asyncio.get_running_loop().time() - request_started
            # 「已耗尽整段请求超时的 502 不再重试」按配置的超时判定，
            # 不能写死 15 秒：超时配到 5~15 秒时整段耗尽的 502 也会落进
            # 这个阈值，恰好违反下面的承诺。
            immediate_bounce_limit = min(15.0, self.timeout_seconds)
            gateway_hit = response.status_code in {520, 522, 524}
            busy_hit = response.status_code == 429 or (
                response.status_code in {502, 503, 504}
                and request_elapsed < immediate_bounce_limit
            )
            retry_busy = busy_hit and remaining_busy_retries > 0
            retry_gateway = (
                not retry_busy
                and gateway_hit
                and remaining_gateway_retries > 0
            )
            if retry_busy or retry_gateway:
                # Relays often park a model for a few seconds, or bounce an
                # immediate 502. Retry those before any stream event is yielded.
                # Do not retry a 502 that already burned the full request
                # timeout — the author has waited long enough.
                if retry_busy:
                    remaining_busy_retries -= 1
                else:
                    remaining_gateway_retries -= 1
                delay = _retry_after_seconds(
                    response,
                    default=5.0 if response.status_code == 429 else 2.0,
                )
                if delay > 0:
                    try:
                        await asyncio.wait_for(cancel_event.wait(), timeout=delay)
                    except TimeoutError:
                        pass
                    else:
                        raise AgentProviderCancelled()
                continue
            # 诊断：完整密钥与自定义头的值先抹掉再引用上游响应，
            # details 只放定位用的元数据（头名列表，绝不含值）。
            excerpt = self._scrub_secret_material(body)
            retry_count = (initial_busy_retries - remaining_busy_retries) + (
                initial_gateway_retries - remaining_gateway_retries
            )
            details: dict[str, Any] = {
                "final_url": endpoint,
                "method": "POST",
                "upstream_status": response.status_code,
                "upstream_message": upstream_message(excerpt),
                "retry_count": retry_count,
            }
            if response.status_code in (401, 403):
                details.update(
                    {
                        "protocol": self.wire_format,
                        "model": self.model,
                        "auth_type": self.auth_type,
                        "api_key_present": bool(self.api_key),
                        "api_key_suffix": (
                            self.api_key[-4:] if self.api_key else None
                        ),
                        "sent_header_names": sorted(headers),
                        "upstream_excerpt": excerpt[:200],
                    }
                )
            raise AgentProviderError(
                f"Provider returned HTTP {response.status_code}: {excerpt}",
                code="agent_provider_http_error",
                retryable=response.status_code >= 500 or response.status_code == 429,
                details=details,
            )

    @asynccontextmanager
    async def _open_response(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        cancel_event: asyncio.Event,
        diagnostics: dict[str, Any] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        request = client.build_request(
            "POST",
            endpoint,
            headers=headers,
            json=payload,
        )
        if diagnostics is not None:
            diagnostics.update(request_phase="awaiting_response_headers", response_headers_received=False, response_status=None)
            diagnostics["request_attempt"] += 1
        send_task = asyncio.create_task(client.send(request, stream=True))
        cancel_task = asyncio.create_task(cancel_event.wait())
        response: httpx.Response | None = None
        try:
            done, _ = await asyncio.wait(
                {send_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and send_task not in done:
                send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
                raise AgentProviderCancelled()
            response = send_task.result()
            if diagnostics is not None:
                diagnostics.update(response_headers_received=True, response_status=response.status_code)
            yield response
        finally:
            if not send_task.done():
                send_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(send_task, cancel_task, return_exceptions=True)
            if response is not None:
                await response.aclose()

    async def _iter_response_lines(
        self,
        response: httpx.Response,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        iterator = response.aiter_lines().__aiter__()
        cancel_task = asyncio.create_task(cancel_event.wait())
        line_task: asyncio.Task[str] | None = None
        idle_seconds = self.stream_idle_timeout_seconds
        try:
            while True:
                line_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    {line_task, cancel_task},
                    timeout=idle_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # 相邻两行之间超过配置的空闲超时：流挂死了，别干等
                    line_task.cancel()
                    await asyncio.gather(line_task, return_exceptions=True)
                    line_task = None
                    raise AgentProviderError(
                        f"模型超过 {int(idle_seconds or 0)} 秒没有继续输出，已中止。正文没有改动。",
                        code="agent_provider_stream_idle_timeout",
                        retryable=True,
                        details={
                            "idle_timeout_seconds": idle_seconds,
                            "final_url": str(response.request.url),
                        },
                    )
                if cancel_task in done and cancel_task.result():
                    line_task.cancel()
                    await asyncio.gather(line_task, return_exceptions=True)
                    line_task = None
                    raise AgentProviderCancelled()
                try:
                    line = line_task.result()
                except StopAsyncIteration:
                    line_task = None
                    return
                line_task = None
                yield line
        finally:
            if line_task is not None and not line_task.done():
                line_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(
                *(task for task in (line_task, cancel_task) if task is not None),
                return_exceptions=True,
            )

    @staticmethod
    def _encode_tool_definition(tool: ProviderToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    @staticmethod
    def _encode_responses_tool_definition(
        tool: ProviderToolDefinition,
    ) -> dict[str, Any]:
        """Responses 的工具是扁平的——没有 Chat 那层 `function` 嵌套。"""
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }

    @staticmethod
    def _build_responses_input(
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """把 Chat 形态的 messages 翻成 Responses 的 instructions + input items。

        三处形态差异：
        - 开头连着的那几条 system 并起来放顶层 `instructions`；对话中间的 system（这一轮末尾补的附言：开着哪个文件、
          环境变了……）照原位置变成一条 user 消息——以前也一起上提，附言一变 instructions 就变，整段提示缓存从头作废；
          9-28 起下一轮照原样带上上一轮的往来，旧附言也在历史里，全上提的话 instructions 每轮都变；
        - assistant 的 `tool_calls` 拆成顶层 `function_call` item（不再嵌在消息里）；
        - `role="tool"` 的结果变成顶层 `function_call_output` item，用 call_id 对上。

        对话消息写全 `"type": "message"`：规范里可省，grok 不带也认，但有的线路（2026-09-26 真机：
        中转站上的 deepseek-v4-pro）缺了就回 400「missing input.type」，换模型当场就用不了。
        """
        instructions: list[str] = []
        items: list[dict[str, Any]] = []
        leading = True
        for message in messages:
            role = message.get("role")
            raw_content = message.get("content")
            content = raw_content if isinstance(raw_content, str) else None
            if role == "system":
                if leading:
                    if content and content.strip():
                        instructions.append(content)
                    continue
                if not (content and content.strip()):
                    continue
                role = "user"
            leading = False
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise AgentProviderError(
                        "Tool result message is missing tool_call_id",
                        code="agent_provider_protocol_error",
                        retryable=False,
                    )
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": content or "",
                    }
                )
                continue
            if role == "assistant":
                if content and content.strip():
                    items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    )
                raw_calls = message.get("tool_calls")
                for raw_call in raw_calls if isinstance(raw_calls, list) else ():
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    function = function if isinstance(function, dict) else {}
                    call_id = raw_call.get("id")
                    name = function.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise AgentProviderError(
                            "Assistant tool call is missing its id or name",
                            code="agent_provider_protocol_error",
                            retryable=False,
                        )
                    arguments = function.get("arguments")
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": arguments if isinstance(arguments, str) else "{}",
                        }
                    )
                continue
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": _image_message_parts(raw_content, "responses") if isinstance(raw_content, list) else [{"type": "input_text", "text": content or ""}],
                }
            )
        return ("\n\n".join(instructions) or None), items

    def _anthropic_thinking_budget(self) -> int | None:
        """reasoning_effort → Anthropic thinking budget。auto/none 不发 thinking。"""
        if self.reasoning_effort in ("auto", "none"):
            return None
        budget = _ANTHROPIC_THINKING_BUDGETS[self.reasoning_effort]
        # 官方要求 budget_tokens ≥ 1024 且 < max_tokens；超出就收敛，
        # 小得连下限都不够时干脆不发 thinking。
        budget = min(budget, self.max_tokens - 1)
        return budget if budget >= 1024 else None

    def _build_anthropic_input(
        self,
        messages: Sequence[Mapping[str, Any]],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """把 Chat 形态的 messages 翻成 Anthropic Messages 的 system + messages。

        - 开头连着的那几条 system 合并上提到顶层 `system` 字段；对话中间的 system 照原位置变成 user 消息
          （同 ``_build_responses_input``：上提的话附言一变 system 就变，缓存从头作废）；
        - assistant 的 `tool_calls` 转 `tool_use` content blocks；
        - `role="tool"` 转 `tool_result` block，连续的若干条并进同一条 user 消息
          （Anthropic 要求工具结果跟着工具调用成组回去）；
        - 工具调用的 id 只留字母、数字、`_`、`-`（Anthropic 只认这些）：9-28 别处一场对话中途换过 Kimi，
          它的 id 带冒号（`Bash:256`），换回 Claude 整场 400；调用和结果用同一种换法，对得上。
        """
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []
        leading = True

        def flush_tool_results() -> None:
            if pending_tool_results:
                converted.append(
                    {"role": "user", "content": list(pending_tool_results)}
                )
                pending_tool_results.clear()

        for message in messages:
            role = message.get("role")
            raw_content = message.get("content")
            content = raw_content if isinstance(raw_content, str) else None
            if role == "system":
                if leading:
                    if content and content.strip():
                        system_parts.append(content)
                    continue
                if not (content and content.strip()):
                    continue
                role = "user"
            leading = False
            if role == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or not call_id.strip():
                    raise AgentProviderError(
                        "Tool result message is missing tool_call_id",
                        code="agent_provider_protocol_error",
                        retryable=False,
                    )
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": _anthropic_tool_id(call_id),
                        "content": content or "",
                    }
                )
                continue
            flush_tool_results()
            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content and content.strip():
                    blocks.append({"type": "text", "text": content})
                raw_calls = message.get("tool_calls")
                for raw_call in raw_calls if isinstance(raw_calls, list) else ():
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    function = function if isinstance(function, dict) else {}
                    call_id = raw_call.get("id")
                    name = function.get("name")
                    if not isinstance(call_id, str) or not isinstance(name, str):
                        raise AgentProviderError(
                            "Assistant tool call is missing its id or name",
                            code="agent_provider_protocol_error",
                            retryable=False,
                        )
                    arguments = function.get("arguments")
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": _anthropic_tool_id(call_id),
                            "name": name,
                            "input": (
                                self._parse_tool_arguments(arguments)
                                if isinstance(arguments, str)
                                else arguments if isinstance(arguments, dict) else {}
                            ),
                        }
                    )
                if blocks:
                    converted.append({"role": "assistant", "content": blocks})
                continue
            converted.append({"role": "user", "content": _image_message_parts(raw_content, "anthropic") if isinstance(raw_content, list) else content or ""})
        flush_tool_results()
        return ("\n\n".join(system_parts) or None), converted

    @staticmethod
    def _responses_output_index(payload: Mapping[str, Any]) -> int:
        raw_index = payload.get("output_index")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0:
            raise AgentProviderError(
                "Responses stream event is missing a valid output_index",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        return raw_index

    @staticmethod
    def _responses_usage(response: Mapping[str, Any]) -> MessageUsage | None:
        raw_usage = response.get("usage")
        if not isinstance(raw_usage, dict):
            return None
        input_tokens = raw_usage.get("input_tokens")
        output_tokens = raw_usage.get("output_tokens")
        total_tokens = raw_usage.get("total_tokens")
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (input_tokens, output_tokens, total_tokens)
        ):
            return None
        cached: int | None = None
        details = raw_usage.get("input_tokens_details")
        if isinstance(details, dict):
            raw_cached = details.get("cached_tokens")
            if isinstance(raw_cached, int) and not isinstance(raw_cached, bool):
                cached = raw_cached
        try:
            return MessageUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_input_tokens=cached,
                cached_input_tokens_complete=cached is not None,
            )
        except ValueError:
            # 中转把三个数字算不平时不该拖垮整次运行——用量是账，不是正文。
            return None

    @classmethod
    def _decode_responses_event(cls, data: str) -> _DecodedProviderChunk:
        """解 Responses 的类型化事件流。

        与 Chat 的 `choices[0].delta` 不同，Responses 每个事件自带 `type`：
        正文走 response.output_text.delta，思考走 response.reasoning_summary_text.delta，
        工具调用先由 response.output_item.added（item.type=function_call）给出
        call_id 与 name，入参再通过 response.function_call_arguments.delta 逐段到达。
        事件形状取自本机真机抓包（.runtime/responses-wire-F-responses-tools.txt）。
        """
        if data.strip() == "[DONE]":
            return _DecodedProviderChunk((), (), (), True, None, None)
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                "Provider emitted malformed SSE JSON",
                code="agent_provider_protocol_error",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProviderError(
                "Provider emitted a non-object SSE payload",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        event_type = payload.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise AgentProviderError(
                "Responses stream event is missing its type",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if event_type == "error":
            raise AgentProviderError(
                str(payload.get("message") or "Provider returned an error event"),
                code="agent_provider_error_event",
                retryable=True,
            )
        response = payload.get("response")
        response = response if isinstance(response, dict) else {}

        reported_model: str | None = None
        raw_model = response.get("model")
        if raw_model is not None:
            if not isinstance(raw_model, str):
                raise AgentProviderError(
                    "Provider emitted a non-string reported model identity",
                    code="agent_provider_reported_model_invalid",
                    retryable=False,
                )
            reported_model = raw_model.strip()
            if not reported_model or len(reported_model) > 512:
                raise AgentProviderError(
                    "Provider emitted an invalid reported model identity",
                    code="agent_provider_reported_model_invalid",
                    retryable=False,
                )

        text_events: list[ProviderTextDelta] = []
        reasoning_events: list[ProviderReasoningDelta] = []
        tool_fragments: list[_ProviderToolCallFragment] = []
        finish_reason: str | None = None
        stream_done = False
        usage: MessageUsage | None = None
        delta = payload.get("delta")

        if event_type == "response.output_text.delta":
            if isinstance(delta, str) and delta:
                text_events.append(ProviderTextDelta(delta))
        elif event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            if isinstance(delta, str) and delta:
                reasoning_events.append(ProviderReasoningDelta(delta, visibility=(
                    "summary" if event_type == "response.reasoning_summary_text.delta" else "full"
                )))
        elif event_type == "response.output_item.added":
            item = payload.get("item")
            item = item if isinstance(item, dict) else {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                arguments = item.get("arguments")
                tool_fragments.append(
                    _ProviderToolCallFragment(
                        index=cls._responses_output_index(payload),
                        id_fragment=call_id if isinstance(call_id, str) else None,
                        name_fragment=name if isinstance(name, str) else None,
                        arguments_fragment=(
                            arguments if isinstance(arguments, str) and arguments else None
                        ),
                    )
                )
        elif event_type == "response.function_call_arguments.delta":
            if isinstance(delta, str) and delta:
                tool_fragments.append(
                    _ProviderToolCallFragment(
                        index=cls._responses_output_index(payload),
                        id_fragment=None,
                        name_fragment=None,
                        arguments_fragment=delta,
                    )
                )
        elif event_type in {
            "response.function_call_arguments.done",
            "response.output_item.done",
        }:
            # 真机抓包里中转两种都发：delta 逐段推、done 再给一遍完整入参
            # （.runtime/responses-wire-F-responses-tools.txt）。把 done 当兜底
            # 交给累积器：没收到过 delta 就用它，收到过就只核对不重复追加。
            # 只认 function_call；message/reasoning 的 done 与工具无关。
            if event_type == "response.output_item.done":
                item = payload.get("item")
                item = item if isinstance(item, dict) else {}
                arguments = (
                    item.get("arguments") if item.get("type") == "function_call" else None
                )
            else:
                arguments = payload.get("arguments")
            if isinstance(arguments, str) and arguments:
                tool_fragments.append(
                    _ProviderToolCallFragment(
                        index=cls._responses_output_index(payload),
                        id_fragment=None,
                        name_fragment=None,
                        arguments_fragment=arguments,
                        arguments_final=True,
                    )
                )
        elif event_type == "response.completed":
            stream_done = True
            usage = cls._responses_usage(response)
            outputs = response.get("output")
            requested_tool_call = isinstance(outputs, list) and any(
                isinstance(entry, dict) and entry.get("type") == "function_call"
                for entry in outputs
            )
            finish_reason = "tool_calls" if requested_tool_call else "stop"
        elif event_type == "response.incomplete":
            stream_done = True
            usage = cls._responses_usage(response)
            details = response.get("incomplete_details")
            reason = details.get("reason") if isinstance(details, dict) else None
            # 只有真的撞上输出预算才报 length——上层据此抛 output_truncated。
            finish_reason = "length" if reason == "max_output_tokens" else "stop"
        elif event_type == "response.failed":
            error = response.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise AgentProviderError(
                str(message or "Responses run failed"),
                code="agent_provider_error_event",
                retryable=True,
            )

        return _DecodedProviderChunk(
            tuple(text_events),
            tuple(reasoning_events),
            tuple(tool_fragments),
            stream_done,
            finish_reason,
            usage,
            reported_model,
        )

    @staticmethod
    def _merge_finish_reason(current: str | None, incoming: str | None) -> str | None:
        if incoming is None:
            return current
        if current is not None and current != incoming:
            raise AgentProviderError(
                "Provider emitted conflicting finish reasons",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        return incoming

    @staticmethod
    def _merge_reported_model(current: str | None, incoming: str | None) -> str | None:
        if incoming is None:
            return current
        if current is not None and current != incoming:
            raise AgentProviderError(
                "Provider emitted conflicting reported model identities",
                code="agent_provider_reported_model_conflict",
                retryable=False,
            )
        return incoming

    def _accumulate_tool_call(
        self,
        accumulators: dict[int, _ProviderToolCallAccumulator],
        fragment: _ProviderToolCallFragment,
    ) -> None:
        accumulator = accumulators.get(fragment.index)
        if accumulator is None:
            # 单次回复的工具调用数不设判死上限：一轮里「读 20 个文件再动手」是
            # 正常批。防失控由 Run 级总次数兜底（打满切收尾轮，不判失败）。
            accumulator = _ProviderToolCallAccumulator(
                index=fragment.index,
                tool_call_id="",
                tool_name="",
                arguments_parts=[],
            )
            accumulators[fragment.index] = accumulator
        if fragment.id_fragment is not None:
            accumulator.tool_call_id = self._merge_streamed_field(
                accumulator.tool_call_id,
                fragment.id_fragment,
                field_name="tool call id",
                max_bytes=MAX_TOOL_CALL_ID_LENGTH,
            )
        if fragment.name_fragment is not None:
            accumulator.tool_name = self._merge_streamed_field(
                accumulator.tool_name,
                fragment.name_fragment,
                field_name="tool name",
                max_bytes=MAX_TOOL_NAME_LENGTH,
            )
        if fragment.arguments_fragment is not None:
            if accumulator.arguments_error is not None:
                # 这次调用的参数已判定不可用：剩余片段照常吸收（保持流对齐）
                # 但不再累积，收尾时以「参数无效」回喂模型重发，不判整轮失败。
                return
            try:
                argument_bytes = len(fragment.arguments_fragment.encode("utf-8"))
            except UnicodeEncodeError:
                accumulator.arguments_error = "参数包含无法编码的字符（invalid Unicode）"
                accumulator.arguments_parts = []
                accumulator.arguments_bytes = 0
                return
            if fragment.arguments_final:
                # Responses 的 *.done 事件带的是完整入参。逐段 delta 已经到齐时
                # 它只是复述，直接跳过；一段都没收到（有的中转不推 delta），
                # 或拼出来的和它不一致时，以 done 为准整体替换。
                if "".join(accumulator.arguments_parts) == fragment.arguments_fragment:
                    return
                accumulator.arguments_parts = [fragment.arguments_fragment]
                accumulator.arguments_bytes = argument_bytes
                return
            # 大小不在流内判死：照常累积，收尾解析时超兜底上限的调用以
            # arguments_error 进场回喂（不判整轮失败）。
            accumulator.arguments_parts.append(fragment.arguments_fragment)
            accumulator.arguments_bytes += argument_bytes

    def _complete_tool_calls(
        self,
        accumulators: Mapping[int, _ProviderToolCallAccumulator],
        *,
        salt: str,
    ) -> list[ProviderToolCall]:
        if not accumulators:
            return []
        # OpenAI-compatible relays do not always emit zero-based contiguous
        # tool_call indexes: some start at 1, some skip numbers when a response
        # carries several calls. The reported index only establishes ordering
        # here, so sort by it and renumber densely rather than rejecting an
        # otherwise valid response. Duplicate ids are still refused below.
        indexes = sorted(accumulators)
        completed: list[ProviderToolCall] = []
        known_ids: set[str] = set()
        for position, index in enumerate(indexes):
            accumulator = accumulators[index]
            tool_call_id = accumulator.tool_call_id.strip()
            # 中转站可能把函数名包成 "functions.project_read_file" 或带空白，
            # 先归一化再校验；名字彻底缺失才是不可恢复的协议错误。
            tool_name = accumulator.tool_name.strip()
            if "." in tool_name:
                tool_name = tool_name.rsplit(".", maxsplit=1)[-1]
            if not tool_name:
                raise AgentProviderError(
                    "Provider tool call is missing a function name",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name) is None:
                raise AgentProviderError(
                    "Provider tool name is outside the compatible function-name subset",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            raw_arguments = "".join(accumulator.arguments_parts)
            # 中转站常发太短（"call_1"）、非法或重复的 tool_call id。id 只用于
            # 本回合内把结果对回调用，据此合成一个稳定替身，而不是拒绝整条响应。
            if (
                len(tool_call_id) < MIN_TOOL_CALL_ID_LENGTH
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", tool_call_id)
                is None
                or tool_call_id in known_ids
            ):
                digest = hashlib.sha256(
                    f"{salt}:{position}:{tool_name}:{raw_arguments}".encode("utf-8"),
                ).hexdigest()[:16]
                tool_call_id = f"auto{position}x{digest}"
            known_ids.add(tool_call_id)
            if accumulator.arguments_error is not None:
                completed.append(
                    ProviderToolCall(
                        index=position,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments={},
                        arguments_error=accumulator.arguments_error,
                    )
                )
                continue
            try:
                arguments = self._parse_tool_arguments(raw_arguments)
            except AgentProviderError as exc:
                if exc.code == "agent_provider_tool_arguments_too_large":
                    detail = "参数超出单次调用的大小上限，请缩短内容或分成多次调用"
                elif exc.code == "agent_provider_tool_arguments_invalid":
                    detail = f"参数不是合法 JSON（{exc}）"
                else:
                    raise
                # 参数问题不判整轮失败：由服务层把原因作为这次调用的
                # 结果回喂模型重发（计入失败计数兜底）。
                completed.append(
                    ProviderToolCall(
                        index=position,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        arguments={},
                        arguments_error=detail,
                    )
                )
                continue
            completed.append(
                ProviderToolCall(
                    index=position,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )
        return completed

    @staticmethod
    def _merge_streamed_field(
        current: str,
        incoming: str,
        *,
        field_name: str,
        max_bytes: int,
    ) -> str:
        if not incoming:
            return current
        if current and incoming == current:
            # A few gateways repeat the last fragment verbatim. Repeating an
            # exact fragment is harmless; any other prefix relationship is
            # ambiguous between a delta and a cumulative snapshot, so fail
            # closed instead of silently dropping or duplicating data.
            merged = current
        else:
            if current and (
                current.startswith(incoming) or incoming.startswith(current)
            ):
                raise AgentProviderError(
                    f"Provider {field_name} used an ambiguous cumulative fragment",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            merged = current + incoming
        try:
            encoded = merged.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AgentProviderError(
                f"Provider {field_name} must use ASCII",
                code="agent_provider_tool_call_invalid",
                retryable=False,
            ) from exc
        if len(encoded) > max_bytes:
            raise AgentProviderError(
                f"Provider {field_name} exceeded its byte limit",
                code="agent_provider_tool_call_invalid",
                retryable=False,
            )
        return merged

    def _parse_tool_arguments(self, raw: str) -> dict[str, Any]:
        try:
            raw_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise AgentProviderError(
                "Provider tool arguments contain invalid Unicode",
                code="agent_provider_tool_arguments_invalid",
                retryable=False,
            ) from exc
        if raw_size > self.max_tool_argument_bytes:
            raise AgentProviderError(
                "Provider tool arguments exceeded the byte limit",
                code="agent_provider_tool_arguments_too_large",
                retryable=False,
            )
        stripped = raw.strip()
        # 无参数工具经常被中转站发成空字符串，等价于空对象。
        if not stripped:
            return {}

        def guarded_loads(text: str, *, strict: bool) -> Any:
            # 重复键按 JSON 通行语义取最后一个值（真实模型确实会在长参数里
            # 把 kind 这类键写两遍，codex/opencode/OpenAI SDK 都是 last-wins）。
            # NaN/Infinity/超界数字照旧拒绝。
            return json.loads(
                text,
                strict=strict,
                parse_constant=self._reject_json_constant,
                parse_float=self._parse_json_float,
            )

        def tolerant_loads(text: str) -> Any:
            try:
                return guarded_loads(text, strict=True)
            except (ValueError, RecursionError):
                # 模型把整章正文塞进参数时经常带裸换行/制表符（严格 JSON 要求
                # 转义）。strict=False 只放宽「字符串内的控制字符」这一点。
                return guarded_loads(text, strict=False)

        try:
            parsed = tolerant_loads(stripped)
        except (ValueError, RecursionError) as exc:
            # 一些模型把参数包进 Markdown 代码栅栏；剥掉后再解析一次。
            unfenced = self._strip_code_fence(stripped)
            if unfenced is None:
                raise self._arguments_error(exc, stripped) from exc
            try:
                parsed = tolerant_loads(unfenced)
            except (ValueError, RecursionError) as fence_exc:
                raise self._arguments_error(fence_exc, unfenced) from fence_exc
        if not isinstance(parsed, dict):
            raise AgentProviderError(
                "Provider tool arguments must be a JSON object",
                code="agent_provider_tool_arguments_invalid",
                retryable=False,
            )
        return parsed

    @staticmethod
    def _arguments_error(exc: BaseException, text: str) -> AgentProviderError:
        """报错带上出错位置附近的原文摘录，中转站的坏参数一次就能定位。

        「Unterminated string」和末尾处的「Expecting …」几乎都是网关把这次
        回答截断了却照常收尾（真实事故：整章正文写进工具参数时被剪断）。
        这不是模型乱说话，标成可重试，作者面的话术按「中断」讲。
        """
        if isinstance(exc, json.JSONDecodeError):
            truncated = exc.msg.startswith("Unterminated string")
        else:
            truncated = False
        detail = str(exc)[:160]
        if isinstance(exc, json.JSONDecodeError):
            start = max(0, exc.pos - 60)
            fragment = exc.doc[start : exc.pos + 60].replace("\n", "\\n")
            detail = f"{exc.msg} @ {exc.pos}：…{fragment}…"
        if truncated:
            return AgentProviderError(
                f"Provider cut the tool arguments short（{detail}）",
                code="agent_provider_tool_arguments_invalid",
                retryable=True,
            )
        return AgentProviderError(
            f"Provider tool arguments are not strict JSON（{detail}）",
            code="agent_provider_tool_arguments_invalid",
            retryable=False,
        )

    @staticmethod
    def _strip_code_fence(text: str) -> str | None:
        match = re.fullmatch(
            r"```[A-Za-z0-9_-]*\s*\n?(.*?)\n?\s*```",
            text,
            flags=re.DOTALL,
        )
        if match is None:
            return None
        inner = match.group(1).strip()
        return inner or None

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise _InvalidJsonValueError(f"non-finite JSON number: {value}")

    @staticmethod
    def _parse_json_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise _InvalidJsonValueError(f"non-finite JSON number: {value}")
        return parsed

    @staticmethod
    def _decode_event(
        data: str,
    ) -> _DecodedProviderChunk:
        if data.strip() == "[DONE]":
            return _DecodedProviderChunk((), (), (), True, None, None)
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                "Provider emitted malformed SSE JSON",
                code="agent_provider_protocol_error",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict):
            raise AgentProviderError(
                "Provider emitted a non-object SSE payload",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            message = error.get("message") if isinstance(error, dict) else None
            raise AgentProviderError(
                str(message or "Provider returned an error event"),
                code="agent_provider_error_event",
                retryable=True,
            )

        reported_model: str | None = None
        if "model" in payload and payload["model"] is not None:
            raw_model = payload["model"]
            if not isinstance(raw_model, str):
                raise AgentProviderError(
                    "Provider emitted a non-string reported model identity",
                    code="agent_provider_reported_model_invalid",
                    retryable=False,
                )
            reported_model = raw_model.strip()
            if not reported_model or len(reported_model) > 512:
                raise AgentProviderError(
                    "Provider emitted an invalid reported model identity",
                    code="agent_provider_reported_model_invalid",
                    retryable=False,
                )

        events: list[ProviderTextDelta] = []
        reasoning_events: list[ProviderReasoningDelta] = []
        tool_fragments: list[_ProviderToolCallFragment] = []
        finish_reason: str | None = None
        choices = payload.get("choices")
        if choices is not None and not isinstance(choices, list):
            raise AgentProviderError(
                "Provider emitted non-list choices",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if isinstance(choices, list) and len(choices) > 1:
            raise AgentProviderError(
                "Provider emitted multiple choices for a single-choice request",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if not isinstance(choice, dict):
                raise AgentProviderError(
                    "Provider emitted a non-object choice",
                    code="agent_provider_protocol_error",
                    retryable=False,
                )
            choice_index = choice.get("index")
            if "index" in choice and (
                not isinstance(choice_index, int)
                or isinstance(choice_index, bool)
                or choice_index != 0
            ):
                raise AgentProviderError(
                    "Provider choice index must be zero",
                    code="agent_provider_protocol_error",
                    retryable=False,
                )
            delta = choice.get("delta")
            if isinstance(delta, dict):
                reasoning = delta.get("reasoning_content")
                if not isinstance(reasoning, str):
                    reasoning = delta.get("reasoning")
                if isinstance(reasoning, str) and reasoning:
                    reasoning_events.append(ProviderReasoningDelta(reasoning))
                content = delta.get("content")
                if isinstance(content, str) and content:
                    events.append(ProviderTextDelta(content))
                if "tool_calls" in delta:
                    raw_tool_calls = delta["tool_calls"]
                    if not isinstance(raw_tool_calls, list):
                        raise AgentProviderError(
                            "Provider emitted non-list tool_calls",
                            code="agent_provider_protocol_error",
                            retryable=False,
                        )
                    for raw_tool_call in raw_tool_calls:
                        tool_fragments.append(
                            OpenAICompatibleAgentProvider._decode_tool_fragment(
                                raw_tool_call
                            )
                        )
            raw_finish = choice.get("finish_reason")
            if isinstance(raw_finish, str) and raw_finish:
                finish_reason = raw_finish

        usage: MessageUsage | None = None
        raw_usage = payload.get("usage")
        if isinstance(raw_usage, dict):
            prompt_tokens = raw_usage.get("prompt_tokens", 0)
            completion_tokens = raw_usage.get("completion_tokens", 0)
            total_tokens = raw_usage.get("total_tokens", 0)
            # DeepSeek 用顶层 prompt_cache_hit_tokens；OpenAI 系藏在
            # prompt_tokens_details.cached_tokens。命中部分是 prompt 的子集，
            # 记账后作者能看见前缀缓存省了多少钱。
            cached_input = raw_usage.get("prompt_cache_hit_tokens")
            if not isinstance(cached_input, int) or isinstance(cached_input, bool) or cached_input < 0:
                details = raw_usage.get("prompt_tokens_details")
                cached_input = (
                    details.get("cached_tokens")
                    if isinstance(details, dict)
                    else None
                )
            if not isinstance(cached_input, int) or isinstance(cached_input, bool) or cached_input < 0:
                cached_input = None
            has_reported_usage = cached_input is not None or any(
                key in raw_usage for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            if has_reported_usage and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (prompt_tokens, completion_tokens, total_tokens)
            ):
                usage = MessageUsage(
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    total_tokens=max(total_tokens, prompt_tokens + completion_tokens),
                    cached_input_tokens=cached_input,
                    input_tokens_reported="prompt_tokens" in raw_usage,
                    output_tokens_reported="completion_tokens" in raw_usage,
                    cached_input_tokens_complete=cached_input is not None,
                )
        return _DecodedProviderChunk(
            tuple(events),
            tuple(reasoning_events),
            tuple(tool_fragments),
            False,
            finish_reason,
            usage,
            reported_model,
        )

    @staticmethod
    def _decode_tool_fragment(raw: object) -> _ProviderToolCallFragment:
        if not isinstance(raw, dict):
            raise AgentProviderError(
                "Provider emitted a non-object tool call fragment",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        index = raw.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise AgentProviderError(
                "Provider tool call fragment has an invalid index",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        raw_type = raw.get("type")
        # 网关常把流式片段的 type 发成 null 或干脆漏发（deepseek 经 new-api
        # 网关实测如此）；只有明确写了别的类型才拒。
        if isinstance(raw_type, str) and raw_type and raw_type != "function":
            raise AgentProviderError(
                "Provider emitted an unsupported tool call type",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        raw_id = raw.get("id")
        if "id" in raw and not isinstance(raw_id, str):
            # DeepSeek 等中转偶尔把 tool_call id 发成数字或 null，
            # 强转成字符串而不是整轮判死。
            raw_id = str(raw_id) if raw_id is not None else None
            raw["id"] = raw_id
        function = raw.get("function")
        if "function" in raw and not isinstance(function, dict):
            raise AgentProviderError(
                "Provider tool call function fragment must be an object",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        raw_name = function.get("name") if isinstance(function, dict) else None
        raw_arguments = function.get("arguments") if isinstance(function, dict) else None
        # 网关常把流式增量的 name 发成 `null` 或干脆漏发（实测 deepseek 经 new-api、
        # glm-5.3-flash 经 kittyrouter 都这样；上一版把它当协议错，整轮被误判判死）。
        # 没发名字就当「这段没发名字」跳过——积累器只在 name 是字符串时才合并，
        # 后续按 id 或下一段补齐。只有 name 是**非字符串的非空值**（数字、对象、数组）
        # 才确实是协议错。
        if (
            isinstance(function, dict)
            and "name" in function
            and not isinstance(raw_name, str)
            and raw_name is not None
        ):
            raise AgentProviderError(
                "Provider tool name fragment must be a string",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        if (
            isinstance(function, dict)
            and "arguments" in function
            and not isinstance(raw_arguments, str)
        ):
            raise AgentProviderError(
                "Provider tool arguments fragment must be a string",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        return _ProviderToolCallFragment(
            index=index,
            id_fragment=raw_id,
            name_fragment=raw_name,
            arguments_fragment=raw_arguments,
        )


_T = TypeVar("_T")


def run_provider_blocking(coro: Coroutine[Any, Any, _T]) -> _T:
    """在同步调用方里驱动一段 Provider 异步流。

    写章服务（run_in_threadpool 驱动）和设置页的测活（线程池）都是同步代码，
    线程里没有事件循环，asyncio.run 直接可用；万一被异步调用方直接调到，
    换个线程跑，避免「事件循环里再开事件循环」。两条通道共用这一个，不各造一份。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def build_agent_provider_from_connection(
    settings: RuntimeSettings,
    *,
    base_url: str | None,
    model: str | None,
    provider_id: str | None = None,
    api_key: str | None,
    credential_reference: str | None,
    reasoning_effort: Literal["auto", "none", "low", "medium", "high", "xhigh", "max", "ultra"] = "auto",
    wire_format: Literal["chat", "responses", "anthropic_messages"] = "chat",
    accepted_reported_models: Sequence[str] = (),
    auth_type: Literal["bearer", "x-api-key", "custom", "none"] = "bearer",
    extra_headers: Mapping[str, str] | None = None,
    request_timeout_seconds: float | None = None,
    max_output_tokens: int | None = None,
    max_retries: int | None = None,
    stream_idle_timeout_seconds: float | None = None,
    max_concurrent_requests: int | None = None,
    circuit_breaker_threshold: int | None = None,
    circuit_breaker_cooldown_seconds: float | None = None,
    context_window: str = "256k",
    is_probe: bool = False,
) -> AgentProvider | None:
    if not base_url or not model:
        return None
    # 环境变量的接受名单是全局兜底，界面里按渠道填的是补充——两边都认。
    # 身份校验本身不放宽（AGENTS.md 3.49），只是让作者不必去改启动脚本。
    merged_aliases = list(settings.agent_provider_accepted_reported_models)
    for alias in accepted_reported_models:
        if alias and alias not in merged_aliases:
            merged_aliases.append(alias)
    return OpenAICompatibleAgentProvider(
        base_url=base_url,
        model=model,
        provider_id=provider_id,
        api_key=api_key,
        credential_reference=credential_reference,
        timeout_seconds=(
            request_timeout_seconds
            if request_timeout_seconds is not None
            else settings.agent_provider_timeout_seconds
        ),
        max_tokens=(
            max_output_tokens
            if max_output_tokens is not None
            else settings.agent_provider_max_tokens
        ),
        temperature=settings.agent_provider_temperature,
        reasoning_effort=reasoning_effort,
        wire_format=wire_format,
        auth_type=auth_type,
        extra_headers=extra_headers,
        accepted_reported_models=tuple(merged_aliases),
        max_retries=max_retries,
        stream_idle_timeout_seconds=stream_idle_timeout_seconds,
        max_concurrent_requests=(
            max_concurrent_requests if max_concurrent_requests is not None else 4
        ),
        circuit_breaker_threshold=(
            circuit_breaker_threshold if circuit_breaker_threshold is not None else 5
        ),
        circuit_breaker_cooldown_seconds=(
            circuit_breaker_cooldown_seconds
            if circuit_breaker_cooldown_seconds is not None
            else 60.0
        ),
        context_window=context_window,
        is_probe=is_probe,
    )


def build_agent_provider(settings: RuntimeSettings) -> AgentProvider | None:
    return build_agent_provider_from_connection(
        settings,
        base_url=settings.agent_provider_base_url,
        model=settings.agent_provider_model,
        api_key=settings.agent_provider_api_key,
        credential_reference=settings.agent_provider_credential_reference,
    )
