from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
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
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
    MessageUsage,
)


MAX_TOOL_CALLS_PER_RESPONSE = 8
MAX_TOOL_ARGUMENT_BYTES = 256 * 1024
MIN_TOOL_CALL_ID_LENGTH = 8
MAX_TOOL_CALL_ID_LENGTH = 128
MAX_TOOL_NAME_LENGTH = 64


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
class ProviderToolCall:
    index: int
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ProviderCompleted:
    finish_reason: str
    usage: MessageUsage | None
    reported_model: str | None = None


ProviderEvent = ProviderTextDelta | ProviderToolCall | ProviderCompleted


@dataclass(frozen=True, slots=True)
class _ProviderToolCallFragment:
    index: int
    id_fragment: str | None
    name_fragment: str | None
    arguments_fragment: str | None


@dataclass(slots=True)
class _ProviderToolCallAccumulator:
    index: int
    tool_call_id: str
    tool_name: str
    arguments_parts: list[str]
    arguments_bytes: int = 0


@dataclass(frozen=True, slots=True)
class _DecodedProviderChunk:
    text_events: tuple[ProviderTextDelta, ...]
    tool_fragments: tuple[_ProviderToolCallFragment, ...]
    stream_done: bool
    finish_reason: str | None
    usage: MessageUsage | None
    reported_model: str | None = None


class _DuplicateJsonKeyError(ValueError):
    pass


class _InvalidJsonValueError(ValueError):
    pass


class AgentProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class AgentProviderCancelled(AgentProviderError):
    def __init__(self) -> None:
        super().__init__(
            "Agent run was cancelled",
            code="agent_run_cancelled",
            retryable=False,
        )


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


class OpenAICompatibleAgentProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        credential_reference: str | None,
        timeout_seconds: float,
        max_tokens: int,
        temperature: float,
        client: httpx.AsyncClient | None = None,
        max_tool_calls_per_response: int = MAX_TOOL_CALLS_PER_RESPONSE,
        max_tool_argument_bytes: int = MAX_TOOL_ARGUMENT_BYTES,
        accepted_reported_models: Sequence[str] = (),
    ) -> None:
        if (
            not isinstance(max_tool_calls_per_response, int)
            or isinstance(max_tool_calls_per_response, bool)
            or max_tool_calls_per_response < 1
        ):
            raise ValueError("max_tool_calls_per_response must be positive")
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
        self.api_key = api_key
        self.credential_reference = normalized_reference
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = client
        self.max_tool_calls_per_response = max_tool_calls_per_response
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
        self._dispatch_config_fingerprint = canonical_json_hash(
            {
                "schema": "zhimeng.openai-compatible-dispatch-config.v1",
                "endpoint": f"{self.base_url}/chat/completions",
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "stream": True,
                "include_usage": True,
                "tool_choice": "auto",
                "parallel_tool_calls": False,
                "max_tool_calls_per_response": self.max_tool_calls_per_response,
                "max_tool_argument_bytes": self.max_tool_argument_bytes,
                "accepted_reported_models": list(self.accepted_reported_models),
                "credential_reference": self.credential_reference,
            }
        )

    @property
    def dispatch_config_fingerprint(self) -> str:
        return self._dispatch_config_fingerprint

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.openai-compatible",
            kind=ExecutorKind.REMOTE,
            implementation="openai-compatible-chat-completions",
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

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        endpoint = f"{self.base_url}/chat/completions"
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": [dict(message) for message in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        tool_names = [tool.name for tool in tools]
        if len(tool_names) != len(set(tool_names)):
            raise AgentProviderError(
                "Provider tool definitions must have unique function names",
                code="agent_provider_tool_definition_invalid",
                retryable=False,
            )
        if tools:
            payload.update(
                {
                    "tools": [self._encode_tool_definition(tool) for tool in tools],
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                }
            )
        if cancel_event.is_set():
            raise AgentProviderCancelled()
        timeout = httpx.Timeout(
            self.timeout_seconds,
            connect=min(15.0, self.timeout_seconds),
        )
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=timeout)
        try:
            async with self._open_response(
                client,
                endpoint=endpoint,
                headers=headers,
                payload=payload,
                cancel_event=cancel_event,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    body = (await response.aread())[:2048].decode("utf-8", errors="replace")
                    raise AgentProviderError(
                        f"Provider returned HTTP {response.status_code}: {body}",
                        code="agent_provider_http_error",
                        retryable=response.status_code >= 500 or response.status_code == 429,
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
                async for line in self._iter_response_lines(response, cancel_event):
                    if not line:
                        if not data_lines:
                            continue
                        chunk = self._decode_event("\n".join(data_lines))
                        data_lines.clear()
                        for fragment in chunk.tool_fragments:
                            self._accumulate_tool_call(tool_calls, fragment)
                        for event in chunk.text_events:
                            saw_text = True
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
                    chunk = self._decode_event("\n".join(data_lines))
                    for fragment in chunk.tool_fragments:
                        self._accumulate_tool_call(tool_calls, fragment)
                    for event in chunk.text_events:
                        saw_text = True
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
                if normalized_finish == "length":
                    raise AgentProviderError(
                        "Provider stopped because the output token limit was reached",
                        code="agent_provider_output_truncated",
                        retryable=True,
                    )
                if normalized_finish == "function_call":
                    raise AgentProviderError(
                        "Provider requested the unsupported legacy function_call protocol",
                        code="agent_provider_tool_call_unsupported",
                        retryable=False,
                    )
                completed_tool_calls = self._complete_tool_calls(tool_calls)
                if normalized_finish == "tool_calls":
                    if not completed_tool_calls:
                        raise AgentProviderError(
                            "Provider ended with tool_calls but emitted no complete tool call",
                            code="agent_provider_tool_call_invalid",
                            retryable=False,
                        )
                    if not tools:
                        raise AgentProviderError(
                            "Provider requested a tool call when no tools were supplied",
                            code="agent_provider_tool_call_unsupported",
                            retryable=False,
                        )
                    unknown_tools = sorted(
                        {
                            tool_call.tool_name
                            for tool_call in completed_tool_calls
                            if tool_call.tool_name not in tool_names
                        }
                    )
                    if unknown_tools:
                        raise AgentProviderError(
                            "Provider requested an unadvertised tool: "
                            + ", ".join(unknown_tools),
                            code="agent_provider_tool_call_unknown",
                            retryable=False,
                        )
                elif completed_tool_calls:
                    raise AgentProviderError(
                        "Provider emitted tool calls without a tool_calls finish reason",
                        code="agent_provider_protocol_error",
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
                )
        except httpx.TimeoutException as exc:
            raise AgentProviderError(
                "Provider stream timed out",
                code="agent_provider_timeout",
                retryable=True,
            ) from exc
        except httpx.RequestError as exc:
            raise AgentProviderError(
                f"Provider request failed: {exc}",
                code="agent_provider_request_failed",
                retryable=True,
            ) from exc
        finally:
            if owns_client:
                await client.aclose()

    @asynccontextmanager
    async def _open_response(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[httpx.Response]:
        request = client.build_request(
            "POST",
            endpoint,
            headers=headers,
            json=payload,
        )
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
            yield response
        finally:
            if not send_task.done():
                send_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(send_task, cancel_task, return_exceptions=True)
            if response is not None:
                await response.aclose()

    @staticmethod
    async def _iter_response_lines(
        response: httpx.Response,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[str]:
        iterator = response.aiter_lines().__aiter__()
        cancel_task = asyncio.create_task(cancel_event.wait())
        line_task: asyncio.Task[str] | None = None
        try:
            while True:
                line_task = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait(
                    {line_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
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
            if len(accumulators) >= self.max_tool_calls_per_response:
                raise AgentProviderError(
                    "Provider exceeded the tool call limit for one response",
                    code="agent_provider_tool_call_limit_exceeded",
                    retryable=False,
                )
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
            try:
                argument_bytes = len(fragment.arguments_fragment.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise AgentProviderError(
                    "Provider tool arguments contain invalid Unicode",
                    code="agent_provider_tool_arguments_invalid",
                    retryable=False,
                ) from exc
            if accumulator.arguments_bytes + argument_bytes > self.max_tool_argument_bytes:
                raise AgentProviderError(
                    "Provider tool arguments exceeded the byte limit",
                    code="agent_provider_tool_arguments_too_large",
                    retryable=False,
                )
            accumulator.arguments_parts.append(fragment.arguments_fragment)
            accumulator.arguments_bytes += argument_bytes

    def _complete_tool_calls(
        self,
        accumulators: Mapping[int, _ProviderToolCallAccumulator],
    ) -> list[ProviderToolCall]:
        if not accumulators:
            return []
        indexes = sorted(accumulators)
        if indexes != list(range(len(indexes))):
            raise AgentProviderError(
                "Provider tool call indexes must be contiguous and zero based",
                code="agent_provider_tool_call_invalid",
                retryable=False,
            )
        completed: list[ProviderToolCall] = []
        known_ids: set[str] = set()
        for index in indexes:
            accumulator = accumulators[index]
            tool_call_id = accumulator.tool_call_id
            tool_name = accumulator.tool_name
            if not tool_call_id or not tool_name:
                raise AgentProviderError(
                    "Provider tool call is missing an id or function name",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name) is None:
                raise AgentProviderError(
                    "Provider tool name is outside the compatible function-name subset",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            if (
                len(tool_call_id) < MIN_TOOL_CALL_ID_LENGTH
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", tool_call_id)
                is None
            ):
                raise AgentProviderError(
                    "Provider tool call id is outside the Runtime stable-id subset",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            if tool_call_id in known_ids:
                raise AgentProviderError(
                    "Provider emitted duplicate tool call ids",
                    code="agent_provider_tool_call_invalid",
                    retryable=False,
                )
            known_ids.add(tool_call_id)
            arguments = self._parse_tool_arguments(
                "".join(accumulator.arguments_parts),
            )
            completed.append(
                ProviderToolCall(
                    index=index,
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
        try:
            parsed = json.loads(
                raw,
                object_pairs_hook=self._unique_json_object,
                parse_constant=self._reject_json_constant,
                parse_float=self._parse_json_float,
            )
        except (ValueError, RecursionError) as exc:
            raise AgentProviderError(
                "Provider tool arguments are not strict JSON",
                code="agent_provider_tool_arguments_invalid",
                retryable=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentProviderError(
                "Provider tool arguments must be a JSON object",
                code="agent_provider_tool_arguments_invalid",
                retryable=False,
            )
        return parsed

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

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
            return _DecodedProviderChunk((), (), True, None, None)
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
            if all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in (prompt_tokens, completion_tokens, total_tokens)
            ):
                usage = MessageUsage(
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    total_tokens=max(total_tokens, prompt_tokens + completion_tokens),
                )
        return _DecodedProviderChunk(
            tuple(events),
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
        if "type" in raw and raw_type != "function":
            raise AgentProviderError(
                "Provider emitted an unsupported tool call type",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        raw_id = raw.get("id")
        if "id" in raw and not isinstance(raw_id, str):
            raise AgentProviderError(
                "Provider tool call id fragment must be a string",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        function = raw.get("function")
        if "function" in raw and not isinstance(function, dict):
            raise AgentProviderError(
                "Provider tool call function fragment must be an object",
                code="agent_provider_protocol_error",
                retryable=False,
            )
        raw_name = function.get("name") if isinstance(function, dict) else None
        raw_arguments = function.get("arguments") if isinstance(function, dict) else None
        if (
            isinstance(function, dict)
            and "name" in function
            and not isinstance(raw_name, str)
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


def build_agent_provider_from_connection(
    settings: RuntimeSettings,
    *,
    base_url: str | None,
    model: str | None,
    api_key: str | None,
    credential_reference: str | None,
) -> AgentProvider | None:
    if not base_url or not model:
        return None
    return OpenAICompatibleAgentProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        credential_reference=credential_reference,
        timeout_seconds=settings.agent_provider_timeout_seconds,
        max_tokens=settings.agent_provider_max_tokens,
        temperature=settings.agent_provider_temperature,
        accepted_reported_models=settings.agent_provider_accepted_reported_models,
    )


def build_agent_provider(settings: RuntimeSettings) -> AgentProvider | None:
    return build_agent_provider_from_connection(
        settings,
        base_url=settings.agent_provider_base_url,
        model=settings.agent_provider_model,
        api_key=settings.agent_provider_api_key,
        credential_reference=settings.agent_provider_credential_reference,
    )
