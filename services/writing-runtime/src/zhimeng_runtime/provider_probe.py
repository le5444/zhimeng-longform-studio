"""Bounded, vendor-neutral probes for OpenAI-compatible Provider capabilities.

This module deliberately keeps Provider capability evidence separate from Agent
Kernel transport evidence. It never persists request headers, response bodies,
or model-generated text. The CLI receives credentials only by named environment
variable and passes the value here only long enough to construct HTTP requests.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http.client import HTTPException
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


_MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_STREAM_LINE_BYTES = 128 * 1024
_MAX_STREAM_EVENTS = 2048
_MAX_STREAM_LINES = 8192
_MAX_STREAM_BYTES = 4 * 1024 * 1024
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
_ENVIRONMENT_NAME = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    ),
]
_TARGET_STRING = Annotated[str, StringConstraints(min_length=1, max_length=512)]


def _capability_schema_rules(
    pass_properties: Mapping[str, object],
) -> dict[str, object]:
    pass_required = ["http_status", *pass_properties]
    return {
        "allOf": [
            {
                "if": {
                    "properties": {"status": {"const": "pass"}},
                    "required": ["status"],
                },
                "then": {
                    "required": pass_required,
                    "properties": {
                        "http_status": {"minimum": 200, "maximum": 299},
                        "error_code": {"type": "null"},
                        **pass_properties,
                    },
                },
            },
            {
                "if": {
                    "properties": {"status": {"const": "fail"}},
                    "required": ["status"],
                },
                "then": {
                    "required": ["error_code"],
                    "properties": {"error_code": {"not": {"type": "null"}}},
                },
            },
        ]
    }


class ProviderProbeError(RuntimeError):
    """A safe-to-display probe configuration or output error."""


class SecretLeakError(ProviderProbeError):
    """Raised before a report would persist secret material."""


class CapabilityStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_MEASURED = "not_measured"


class ProbeErrorCode(StrEnum):
    HTTP_STATUS = "http_status"
    TRANSPORT_ERROR = "transport_error"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    MODEL_MISMATCH = "model_mismatch"
    INVALID_STREAM = "invalid_stream"
    STREAM_LIMIT = "stream_limit"
    DEADLINE_EXCEEDED = "deadline_exceeded"


class ProbeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=False)


class ProviderTarget(ProbeModel):
    base_url: _TARGET_STRING
    model: _TARGET_STRING
    api_key_env: _ENVIRONMENT_NAME
    user_agent: _TARGET_STRING
    insecure_loopback_http: bool

    @model_validator(mode="after")
    def validate_transport_security(self) -> "ProviderTarget":
        try:
            normalized = normalize_base_url(
                self.base_url,
                allow_insecure_loopback_http=self.insecure_loopback_http,
            )
        except ProviderProbeError as error:
            raise ValueError("provider target transport security is invalid") from error
        if normalized != self.base_url:
            raise ValueError("provider target base URL must be normalized")
        is_cleartext = urlsplit(self.base_url).scheme == "http"
        if self.insecure_loopback_http is not is_cleartext:
            raise ValueError(
                "insecure_loopback_http must describe the target transport"
            )
        return self


class ProbeLimits(ProbeModel):
    timeout_seconds: float = Field(gt=0, le=300)
    max_json_response_bytes: Literal[2 * 1024 * 1024]
    max_stream_line_bytes: Literal[128 * 1024]
    max_stream_events: Literal[2048]
    max_stream_lines: Literal[8192]
    max_stream_bytes: Literal[4 * 1024 * 1024]


class CredentialHandling(ProbeModel):
    source: Literal["environment_variable"]
    key_written_to_report: Literal[False]
    key_printed_by_cli: Literal[False]


class UsageSummary(ProbeModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class CapabilityResult(ProbeModel):
    status: CapabilityStatus
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: ProbeErrorCode | None = None
    duration_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> "CapabilityResult":
        if self.status is CapabilityStatus.PASS:
            if self.http_status is None or not 200 <= self.http_status < 300:
                raise ValueError("passing probe results require a 2xx HTTP status")
            if self.error_code is not None:
                raise ValueError("passing probe results cannot carry an error_code")
        elif self.status is CapabilityStatus.FAIL:
            if self.error_code is None:
                raise ValueError("failing probe results require an error_code")
        elif (
            self.http_status is not None
            or self.error_code is not None
            or self.duration_ms is not None
        ):
            raise ValueError("not_measured probe results cannot claim measurements")
        return self


class ModelsCapability(CapabilityResult):
    model_config = ConfigDict(
        json_schema_extra=_capability_schema_rules(
            {
                "model_count": {"minimum": 1},
                "selected_model_listed": {"const": True},
            }
        )
    )
    status: Literal[CapabilityStatus.PASS, CapabilityStatus.FAIL]
    model_count: int | None = Field(default=None, ge=0)
    selected_model_listed: bool | None = None

    @model_validator(mode="after")
    def validate_pass_evidence(self) -> "ModelsCapability":
        if self.status is CapabilityStatus.PASS and not (
            self.model_count is not None
            and self.model_count > 0
            and self.selected_model_listed is True
        ):
            raise ValueError("passing models probes require listed target evidence")
        return self


class JsonObjectCapability(CapabilityResult):
    model_config = ConfigDict(
        json_schema_extra=_capability_schema_rules(
            {
                "plain_text_control_observed": {"const": True},
                "control_response_model_matches_requested": {"const": True},
                "control_finish_reason_present": {"const": True},
                "valid_json_object": {"const": True},
                "format_enforced_against_plain_text_control": {"const": True},
                "response_model_matches_requested": {"const": True},
                "finish_reason_present": {"const": True},
            }
        )
    )
    status: Literal[CapabilityStatus.PASS, CapabilityStatus.FAIL]
    plain_text_control_observed: bool | None = None
    control_response_model_matches_requested: bool | None = None
    control_finish_reason_present: bool | None = None
    valid_json_object: bool | None = None
    format_enforced_against_plain_text_control: bool | None = None
    response_model_matches_requested: bool | None = None
    finish_reason_present: bool | None = None
    usage: UsageSummary | None = None

    @model_validator(mode="after")
    def validate_pass_evidence(self) -> "JsonObjectCapability":
        if self.status is CapabilityStatus.PASS and not (
            self.plain_text_control_observed is True
            and self.control_response_model_matches_requested is True
            and self.control_finish_reason_present is True
            and self.valid_json_object is True
            and self.format_enforced_against_plain_text_control is True
            and self.response_model_matches_requested is True
            and self.finish_reason_present is True
        ):
            raise ValueError("passing JSON probes require differential format evidence")
        return self


class NamedFunctionToolChoiceCapability(CapabilityResult):
    model_config = ConfigDict(
        json_schema_extra=_capability_schema_rules(
            {
                "tool_call_count": {"const": 1},
                "function_type_valid": {"const": True},
                "tool_call_id_present": {"const": True},
                "selected_named_function": {"const": True},
                "arguments_valid_json_object": {"const": True},
                "arguments_match_expected_marker": {"const": True},
                "response_model_matches_requested": {"const": True},
                "finish_reason_present": {"const": True},
                "finish_reason_is_tool_calls": {"const": True},
            }
        )
    )
    status: Literal[CapabilityStatus.PASS, CapabilityStatus.FAIL]
    tool_call_count: int | None = Field(default=None, ge=0)
    function_type_valid: bool | None = None
    tool_call_id_present: bool | None = None
    selected_named_function: bool | None = None
    arguments_valid_json_object: bool | None = None
    arguments_match_expected_marker: bool | None = None
    response_model_matches_requested: bool | None = None
    finish_reason_present: bool | None = None
    finish_reason_is_tool_calls: bool | None = None
    usage: UsageSummary | None = None

    @model_validator(mode="after")
    def validate_pass_evidence(self) -> "NamedFunctionToolChoiceCapability":
        if self.status is CapabilityStatus.PASS and not (
            self.tool_call_count == 1
            and self.function_type_valid is True
            and self.tool_call_id_present is True
            and self.selected_named_function is True
            and self.arguments_valid_json_object is True
            and self.arguments_match_expected_marker is True
            and self.response_model_matches_requested is True
            and self.finish_reason_present is True
            and self.finish_reason_is_tool_calls is True
        ):
            raise ValueError("passing tool-choice probes require complete call evidence")
        return self


class StreamingCapability(CapabilityResult):
    model_config = ConfigDict(
        json_schema_extra=_capability_schema_rules(
            {
                "sse_event_count": {"minimum": 1},
                "done_sentinel_received": {"const": True},
                "content_delta_received": {"const": True},
                "response_model_matches_requested": {"const": True},
                "content_type_is_event_stream": {"const": True},
                "skipped_by_caller": {"const": False},
            }
        )
    )
    sse_event_count: int | None = Field(default=None, ge=0)
    done_sentinel_received: bool | None = None
    content_delta_received: bool | None = None
    response_model_matches_requested: bool | None = None
    content_type_is_event_stream: bool | None = None
    skipped_by_caller: bool = False

    @model_validator(mode="after")
    def validate_skip(self) -> "StreamingCapability":
        if self.skipped_by_caller and self.status is not CapabilityStatus.NOT_MEASURED:
            raise ValueError("only not_measured streaming probes can be skipped")
        if not self.skipped_by_caller and self.status is CapabilityStatus.NOT_MEASURED:
            raise ValueError("not_measured streaming probes must be caller-skipped")
        if self.status is CapabilityStatus.PASS and not (
            self.sse_event_count is not None
            and self.sse_event_count > 0
            and self.done_sentinel_received is True
            and self.content_delta_received is True
            and self.response_model_matches_requested is True
            and self.content_type_is_event_stream is True
            and self.skipped_by_caller is False
        ):
            raise ValueError("passing streaming probes require complete SSE evidence")
        return self


class CancelCapability(ProbeModel):
    status: Literal[CapabilityStatus.NOT_MEASURED]
    reason: Literal["no_portable_server_cancel_contract"]


class ProviderCapabilities(ProbeModel):
    models: ModelsCapability
    json_object: JsonObjectCapability
    named_function_tool_choice: NamedFunctionToolChoiceCapability
    streaming: StreamingCapability
    cancel: CancelCapability


class ProviderCapabilityProbeReport(ProbeModel):
    schema_id: Literal["zhimeng.provider-capability-probe.v3"] = Field(alias="schema")
    generated_at: datetime
    target: ProviderTarget
    probe_limits: ProbeLimits
    credential_handling: CredentialHandling
    capabilities: ProviderCapabilities


class ResponseLike(Protocol):
    status: int
    headers: Any

    def read(self, amount: int | None = None) -> bytes: ...

    def readline(self, limit: int = -1) -> bytes: ...

    def close(self) -> None: ...


RequestOpener = Callable[..., ResponseLike]


@dataclass(frozen=True)
class _JsonResponse:
    status: int | None
    payload: Mapping[str, Any] | None
    error_code: ProbeErrorCode | None
    duration_ms: int


@dataclass(frozen=True)
class _StreamResponse:
    status: int | None
    error_code: ProbeErrorCode | None
    duration_ms: int
    event_count: int | None
    done_sentinel_received: bool | None
    content_delta_received: bool | None
    response_model_matches_requested: bool | None


@dataclass(frozen=True)
class _DeadlineCallResult:
    timed_out: bool
    value: object | None = None
    error: Exception | None = None


class _ProbeDeadlineExceeded(TimeoutError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        status_code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> Request | None:
        return None


def _default_opener(request: Request, *, timeout: float) -> ResponseLike:
    # A Provider redirect must not receive the bearer header on a new origin.
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def normalize_base_url(
    value: str,
    *,
    allow_insecure_loopback_http: bool = False,
) -> str:
    """Validate a credential-free OpenAI-compatible API base URL."""

    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ProviderProbeError("provider base URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProviderProbeError(
            "provider base URL cannot contain credentials, query, or fragment"
        )
    if parsed.scheme == "http" and not (
        allow_insecure_loopback_http and _is_loopback_host(parsed.hostname)
    ):
        raise ProviderProbeError(
            "provider base URL must use HTTPS; insecure HTTP is opt-in for loopback only"
        )
    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def read_api_key_from_environment(
    variable_name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return one named environment secret without putting it in a report."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", variable_name):
        raise ProviderProbeError("api-key environment variable name is invalid")
    source = os.environ if environ is None else environ
    value = source.get(variable_name)
    if value is None or not value.strip() or "\r" in value or "\n" in value:
        raise ProviderProbeError("api-key environment variable is missing or invalid")
    return value


def _safe_close(response: ResponseLike | HTTPError | None) -> None:
    if response is None:
        return
    try:
        response.close()
    except Exception:
        # Cleanup failure cannot safely improve a probe report.
        pass


def _safe_duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _call_before_deadline(
    operation: Callable[[], object],
    *,
    started: float,
    timeout_seconds: float,
    close_late_result: Callable[[object], None] | None = None,
) -> _DeadlineCallResult:
    remaining = timeout_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return _DeadlineCallResult(timed_out=True)
    results: queue.Queue[tuple[object | None, Exception | None]] = queue.Queue(maxsize=1)
    cancelled = threading.Event()
    state_lock = threading.Lock()

    def run() -> None:
        try:
            value = operation()
            error = None
        except Exception as exc:
            value = None
            error = exc
        with state_lock:
            if cancelled.is_set():
                if value is not None and close_late_result is not None:
                    close_late_result(value)
                return
            results.put((value, error))

    threading.Thread(
        target=run,
        name="provider-probe-deadline-call",
        daemon=True,
    ).start()
    try:
        value, error = results.get(timeout=remaining)
    except queue.Empty:
        with state_lock:
            cancelled.set()
            try:
                late_value, _ = results.get_nowait()
            except queue.Empty:
                late_value = None
            if late_value is not None and close_late_result is not None:
                close_late_result(late_value)
        return _DeadlineCallResult(timed_out=True)
    return _DeadlineCallResult(timed_out=False, value=value, error=error)


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _usage_summary(payload: Mapping[str, Any]) -> UsageSummary | None:
    raw_usage = payload.get("usage")
    if not _is_mapping(raw_usage):
        return None
    return UsageSummary(
        prompt_tokens=_non_negative_int(raw_usage.get("prompt_tokens")),
        completion_tokens=_non_negative_int(raw_usage.get("completion_tokens")),
        total_tokens=_non_negative_int(raw_usage.get("total_tokens")),
    )


def _response_model_matches(payload: Mapping[str, Any], model: str) -> bool | None:
    returned = payload.get("model")
    if not isinstance(returned, str):
        return None
    return returned == model


def _first_choice(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not _is_mapping(choices[0]):
        return None
    return choices[0]


def _message_from_response(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choice = _first_choice(payload)
    if choice is None:
        return None
    message = choice.get("message")
    if not _is_mapping(message):
        return None
    return message


def _finish_reason_present(payload: Mapping[str, Any]) -> bool | None:
    choice = _first_choice(payload)
    if choice is None:
        return None
    return isinstance(choice.get("finish_reason"), str)


def _response_content_type(response: ResponseLike) -> str | None:
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        raw = getheader("Content-Type")
        if isinstance(raw, str):
            return raw.split(";", 1)[0].strip().casefold()
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Content-Type")
    if not isinstance(raw, str):
        return None
    return raw.split(";", 1)[0].strip().casefold()


class CompatibleProviderProbe:
    """Run a bounded capability probe against an OpenAI-compatible endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str,
        api_key: str,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_insecure_loopback_http: bool = False,
        timeout_seconds: float = 30.0,
        opener: RequestOpener | None = None,
        probe_nonce: str | None = None,
    ) -> None:
        if not model.strip() or len(model) > 512:
            raise ProviderProbeError("model name is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ProviderProbeError("timeout_seconds must be between 0 and 300")
        if not api_key or "\r" in api_key or "\n" in api_key:
            raise ProviderProbeError("api key is invalid")
        if not user_agent.strip() or "\r" in user_agent or "\n" in user_agent:
            raise ProviderProbeError("user agent is invalid")
        self.target = ProviderTarget(
            base_url=normalize_base_url(
                base_url,
                allow_insecure_loopback_http=allow_insecure_loopback_http,
            ),
            model=model.strip(),
            api_key_env=api_key_env,
            user_agent=user_agent.strip(),
            insecure_loopback_http=base_url.strip().casefold().startswith("http://"),
        )
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._opener = opener or _default_opener
        generated_nonce = uuid.uuid4().hex if probe_nonce is None else probe_nonce
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", generated_nonce):
            raise ProviderProbeError("probe nonce is invalid")
        self._probe_nonce = generated_nonce

    def run(self, *, include_streaming: bool = True) -> ProviderCapabilityProbeReport:
        return ProviderCapabilityProbeReport(
            schema="zhimeng.provider-capability-probe.v3",
            generated_at=datetime.now(UTC),
            target=self.target,
            probe_limits=ProbeLimits(
                timeout_seconds=self._timeout_seconds,
                max_json_response_bytes=_MAX_JSON_RESPONSE_BYTES,
                max_stream_line_bytes=_MAX_STREAM_LINE_BYTES,
                max_stream_events=_MAX_STREAM_EVENTS,
                max_stream_lines=_MAX_STREAM_LINES,
                max_stream_bytes=_MAX_STREAM_BYTES,
            ),
            credential_handling=CredentialHandling(
                source="environment_variable",
                key_written_to_report=False,
                key_printed_by_cli=False,
            ),
            capabilities=ProviderCapabilities(
                models=self._probe_models(),
                json_object=self._probe_json_object(),
                named_function_tool_choice=self._probe_named_function_tool_choice(),
                streaming=(
                    self._probe_streaming()
                    if include_streaming
                    else StreamingCapability(
                        status=CapabilityStatus.NOT_MEASURED,
                        skipped_by_caller=True,
                    )
                ),
                # Closing an HTTP response is not a portable proof that the Provider
                # cancelled an active model run, so this remains deliberately unmeasured.
                cancel=CancelCapability(
                    status=CapabilityStatus.NOT_MEASURED,
                    reason="no_portable_server_cancel_contract",
                ),
            ),
        )

    def _endpoint(self, suffix: str) -> str:
        return f"{self.target.base_url}{suffix}"

    def _request(
        self,
        *,
        method: str,
        endpoint: str,
        body: Mapping[str, Any] | None,
        accept: str = "application/json",
    ) -> Request:
        encoded_body = None
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": self.target.user_agent,
        }
        if body is not None:
            encoded_body = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return Request(endpoint, data=encoded_body, headers=headers, method=method)

    def _open(self, request: Request) -> ResponseLike:
        return self._opener(request, timeout=self._timeout_seconds)

    def _open_before_deadline(self, request: Request, *, started: float) -> ResponseLike:
        result = _call_before_deadline(
            lambda: self._open(request),
            started=started,
            timeout_seconds=self._timeout_seconds,
            close_late_result=lambda value: _safe_close(value),
        )
        if result.timed_out:
            raise _ProbeDeadlineExceeded
        if result.error is not None:
            raise result.error
        if result.value is None:
            raise OSError("provider opener returned no response")
        return result.value  # type: ignore[return-value]

    def _json_request(
        self,
        *,
        method: str,
        endpoint: str,
        body: Mapping[str, Any] | None,
    ) -> _JsonResponse:
        started = time.monotonic()
        response: ResponseLike | None = None
        try:
            response = self._open_before_deadline(
                self._request(method=method, endpoint=endpoint, body=body),
                started=started,
            )
            status = int(response.status)
            if not 200 <= status < 300:
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.HTTP_STATUS,
                    duration_ms=_safe_duration_ms(started),
                )
            read_result = _call_before_deadline(
                lambda: response.read(_MAX_JSON_RESPONSE_BYTES + 1),
                started=started,
                timeout_seconds=self._timeout_seconds,
            )
            if read_result.timed_out:
                _safe_close(response)
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.DEADLINE_EXCEEDED,
                    duration_ms=_safe_duration_ms(started),
                )
            if read_result.error is not None:
                raise read_result.error
            if not isinstance(read_result.value, bytes):
                raise ValueError("provider response body is not bytes")
            raw = read_result.value
            if len(raw) > _MAX_JSON_RESPONSE_BYTES:
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.RESPONSE_TOO_LARGE,
                    duration_ms=_safe_duration_ms(started),
                )
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.INVALID_JSON,
                    duration_ms=_safe_duration_ms(started),
                )
            except json.JSONDecodeError:
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.INVALID_JSON,
                    duration_ms=_safe_duration_ms(started),
                )
            if not _is_mapping(decoded):
                return _JsonResponse(
                    status=status,
                    payload=None,
                    error_code=ProbeErrorCode.INVALID_RESPONSE,
                    duration_ms=_safe_duration_ms(started),
                )
            return _JsonResponse(
                status=status,
                payload=decoded,
                error_code=None,
                duration_ms=_safe_duration_ms(started),
            )
        except _ProbeDeadlineExceeded:
            return _JsonResponse(
                status=None,
                payload=None,
                error_code=ProbeErrorCode.DEADLINE_EXCEEDED,
                duration_ms=_safe_duration_ms(started),
            )
        except HTTPError as error:
            _safe_close(error)
            return _JsonResponse(
                status=int(error.code),
                payload=None,
                error_code=ProbeErrorCode.HTTP_STATUS,
                duration_ms=_safe_duration_ms(started),
            )
        except (HTTPException, URLError, TimeoutError, OSError, ValueError):
            return _JsonResponse(
                status=None,
                payload=None,
                error_code=ProbeErrorCode.TRANSPORT_ERROR,
                duration_ms=_safe_duration_ms(started),
            )
        finally:
            _safe_close(response)

    def _probe_models(self) -> ModelsCapability:
        response = self._json_request(
            method="GET",
            endpoint=self._endpoint("/models"),
            body=None,
        )
        if response.error_code is not None:
            return ModelsCapability(
                status=CapabilityStatus.FAIL,
                http_status=response.status,
                error_code=response.error_code,
                duration_ms=response.duration_ms,
            )
        assert response.payload is not None and response.status is not None
        models = response.payload.get("data")
        if not isinstance(models, list):
            return ModelsCapability(
                status=CapabilityStatus.FAIL,
                http_status=response.status,
                error_code=ProbeErrorCode.INVALID_RESPONSE,
                duration_ms=response.duration_ms,
            )
        selected_model_listed = any(
            _is_mapping(item) and item.get("id") == self.target.model for item in models
        )
        return ModelsCapability(
            status=(
                CapabilityStatus.PASS
                if selected_model_listed
                else CapabilityStatus.FAIL
            ),
            http_status=response.status,
            error_code=(
                None if selected_model_listed else ProbeErrorCode.MODEL_MISMATCH
            ),
            duration_ms=response.duration_ms,
            model_count=len(models),
            selected_model_listed=selected_model_listed,
        )

    def _probe_json_object(self) -> JsonObjectCapability:
        plain_marker = f"PLAIN_{self._probe_nonce[:24]}"
        messages = [
            {
                "role": "user",
                "content": (
                    f"Return exactly {plain_marker}; do not return an object. "
                    "The word JSON is present only for protocol compatibility."
                ),
            }
        ]
        control = self._json_request(
            method="POST",
            endpoint=self._endpoint("/chat/completions"),
            body={
                "model": self.target.model,
                "messages": messages,
                "temperature": 0,
            },
        )
        if control.error_code is not None:
            return JsonObjectCapability(
                status=CapabilityStatus.FAIL,
                http_status=control.status,
                error_code=control.error_code,
                duration_ms=control.duration_ms,
            )
        assert control.payload is not None and control.status is not None
        control_message = _message_from_response(control.payload)
        control_content = (
            None if control_message is None else control_message.get("content")
        )
        plain_text_control_observed = (
            isinstance(control_content, str)
            and control_content.strip() == plain_marker
        )
        control_model_matches = _response_model_matches(
            control.payload,
            self.target.model,
        )
        control_finish_reason_present = _finish_reason_present(control.payload)

        response = self._json_request(
            method="POST",
            endpoint=self._endpoint("/chat/completions"),
            body={
                "model": self.target.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
        )
        if response.error_code is not None:
            return JsonObjectCapability(
                status=CapabilityStatus.FAIL,
                http_status=response.status,
                error_code=response.error_code,
                duration_ms=response.duration_ms,
            )
        assert response.payload is not None and response.status is not None
        message = _message_from_response(response.payload)
        content = None if message is None else message.get("content")
        valid_json_object = False
        if isinstance(content, str):
            try:
                valid_json_object = _is_mapping(json.loads(content))
            except json.JSONDecodeError:
                valid_json_object = False
        model_matches = _response_model_matches(response.payload, self.target.model)
        finish_reason_present = _finish_reason_present(response.payload)
        format_enforced = plain_text_control_observed and valid_json_object
        passed = (
            plain_text_control_observed
            and control_model_matches is True
            and control_finish_reason_present is True
            and valid_json_object
            and format_enforced
            and model_matches is True
            and finish_reason_present is True
        )
        return JsonObjectCapability(
            status=CapabilityStatus.PASS if passed else CapabilityStatus.FAIL,
            http_status=response.status,
            error_code=(
                None
                if passed
                else (
                    ProbeErrorCode.MODEL_MISMATCH
                    if model_matches is False or control_model_matches is False
                    else ProbeErrorCode.INVALID_RESPONSE
                )
            ),
            duration_ms=response.duration_ms,
            plain_text_control_observed=plain_text_control_observed,
            control_response_model_matches_requested=control_model_matches,
            control_finish_reason_present=control_finish_reason_present,
            valid_json_object=valid_json_object,
            format_enforced_against_plain_text_control=format_enforced,
            response_model_matches_requested=model_matches,
            finish_reason_present=finish_reason_present,
            usage=_usage_summary(response.payload),
        )

    def _probe_named_function_tool_choice(self) -> NamedFunctionToolChoiceCapability:
        nonce = self._probe_nonce[:32]
        tool_name = f"provider_probe_selected_{nonce}"
        decoy_name = f"provider_probe_decoy_{nonce}"
        expected_marker = f"provider-probe-{nonce}"
        response = self._json_request(
            method="POST",
            endpoint=self._endpoint("/chat/completions"),
            body={
                "model": self.target.model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Call {decoy_name} with marker set to decoy. "
                            "Do not choose any other function from the message alone."
                        ),
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": "Return the selected probe marker.",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "marker": {
                                        "type": "string",
                                        "enum": [expected_marker],
                                    }
                                },
                                "required": ["marker"],
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": decoy_name,
                            "description": "Return the decoy marker requested in the message.",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "marker": {
                                        "type": "string",
                                        "enum": ["decoy"],
                                    }
                                },
                                "required": ["marker"],
                            },
                        },
                    },
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": tool_name},
                },
                "temperature": 0,
            },
        )
        if response.error_code is not None:
            return NamedFunctionToolChoiceCapability(
                status=CapabilityStatus.FAIL,
                http_status=response.status,
                error_code=response.error_code,
                duration_ms=response.duration_ms,
            )
        assert response.payload is not None and response.status is not None
        message = _message_from_response(response.payload)
        raw_tool_calls = None if message is None else message.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else []
        function_type_valid = False
        tool_call_id_present = False
        selected_named_function = False
        arguments_valid_json_object = False
        arguments_match_expected_marker = False
        if len(tool_calls) == 1 and _is_mapping(tool_calls[0]):
            function_type_valid = tool_calls[0].get("type") == "function"
            tool_call_id_present = (
                isinstance(tool_calls[0].get("id"), str)
                and bool(tool_calls[0].get("id"))
            )
            function = tool_calls[0].get("function")
            if _is_mapping(function):
                selected_named_function = function.get("name") == tool_name
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        parsed_arguments = json.loads(arguments)
                        arguments_valid_json_object = _is_mapping(parsed_arguments)
                        arguments_match_expected_marker = (
                            arguments_valid_json_object
                            and parsed_arguments.get("marker") == expected_marker
                        )
                    except json.JSONDecodeError:
                        arguments_valid_json_object = False
                        arguments_match_expected_marker = False
        model_matches = _response_model_matches(response.payload, self.target.model)
        choice = _first_choice(response.payload)
        finish_reason = None if choice is None else choice.get("finish_reason")
        passed = (
            len(tool_calls) == 1
            and function_type_valid
            and tool_call_id_present
            and selected_named_function
            and arguments_valid_json_object
            and arguments_match_expected_marker
            and model_matches is True
            and finish_reason == "tool_calls"
        )
        return NamedFunctionToolChoiceCapability(
            status=CapabilityStatus.PASS if passed else CapabilityStatus.FAIL,
            http_status=response.status,
            error_code=(
                None
                if passed
                else (
                    ProbeErrorCode.MODEL_MISMATCH
                    if model_matches is False
                    else ProbeErrorCode.INVALID_RESPONSE
                )
            ),
            duration_ms=response.duration_ms,
            tool_call_count=len(tool_calls),
            function_type_valid=function_type_valid,
            tool_call_id_present=tool_call_id_present,
            selected_named_function=selected_named_function,
            arguments_valid_json_object=arguments_valid_json_object,
            arguments_match_expected_marker=arguments_match_expected_marker,
            response_model_matches_requested=model_matches,
            finish_reason_present=_finish_reason_present(response.payload),
            finish_reason_is_tool_calls=finish_reason == "tool_calls",
            usage=_usage_summary(response.payload),
        )

    def _probe_streaming(self) -> StreamingCapability:
        started = time.monotonic()
        response: ResponseLike | None = None
        try:
            response = self._open_before_deadline(
                self._request(
                    method="POST",
                    endpoint=self._endpoint("/chat/completions"),
                    body={
                        "model": self.target.model,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Reply with the single word READY.",
                            }
                        ],
                        "stream": True,
                        "temperature": 0,
                    },
                    accept="text/event-stream",
                ),
                started=started,
            )
            status = int(response.status)
            content_type_is_event_stream = (
                _response_content_type(response) == "text/event-stream"
            )
            if not 200 <= status < 300:
                return StreamingCapability(
                    status=CapabilityStatus.FAIL,
                    http_status=status,
                    error_code=ProbeErrorCode.HTTP_STATUS,
                    duration_ms=_safe_duration_ms(started),
                )
            stream = self._read_stream(response, started)
            passed = (
                stream.error_code is None
                and stream.event_count is not None
                and stream.event_count > 0
                and stream.done_sentinel_received is True
                and stream.content_delta_received is True
                and stream.response_model_matches_requested is True
                and content_type_is_event_stream
            )
            return StreamingCapability(
                status=CapabilityStatus.PASS if passed else CapabilityStatus.FAIL,
                http_status=status,
                error_code=(
                    None
                    if passed
                    else (
                        stream.error_code
                        or (
                            ProbeErrorCode.MODEL_MISMATCH
                            if stream.response_model_matches_requested is False
                            else ProbeErrorCode.INVALID_STREAM
                        )
                    )
                ),
                duration_ms=stream.duration_ms,
                sse_event_count=stream.event_count,
                done_sentinel_received=stream.done_sentinel_received,
                content_delta_received=stream.content_delta_received,
                response_model_matches_requested=stream.response_model_matches_requested,
                content_type_is_event_stream=content_type_is_event_stream,
            )
        except _ProbeDeadlineExceeded:
            return StreamingCapability(
                status=CapabilityStatus.FAIL,
                error_code=ProbeErrorCode.DEADLINE_EXCEEDED,
                duration_ms=_safe_duration_ms(started),
            )
        except HTTPError as error:
            _safe_close(error)
            return StreamingCapability(
                status=CapabilityStatus.FAIL,
                http_status=int(error.code),
                error_code=ProbeErrorCode.HTTP_STATUS,
                duration_ms=_safe_duration_ms(started),
            )
        except (HTTPException, URLError, TimeoutError, OSError, ValueError):
            return StreamingCapability(
                status=CapabilityStatus.FAIL,
                error_code=ProbeErrorCode.TRANSPORT_ERROR,
                duration_ms=_safe_duration_ms(started),
            )
        finally:
            _safe_close(response)

    def _read_stream(self, response: ResponseLike, started: float) -> _StreamResponse:
        event_count = 0
        line_count = 0
        total_bytes = 0
        done_sentinel_received = False
        content_delta_received = False
        response_model_matches_requested: bool | None = None
        lines: queue.Queue[bytes | None] = queue.Queue(maxsize=16)
        stop_reader = threading.Event()

        def put_line(value: bytes | None) -> bool:
            while not stop_reader.is_set():
                try:
                    lines.put(value, timeout=0.05)
                    return True
                except queue.Full:
                    continue
            return False

        def read_lines() -> None:
            try:
                while not stop_reader.is_set():
                    raw = response.readline(_MAX_STREAM_LINE_BYTES + 1)
                    if not put_line(raw) or not raw:
                        return
            except Exception:
                put_line(None)

        reader = threading.Thread(
            target=read_lines,
            name="provider-probe-stream-reader",
            daemon=True,
        )
        reader.start()
        try:
            while True:
                remaining = self._timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    _safe_close(response)
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.DEADLINE_EXCEEDED,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                try:
                    raw_line = lines.get(timeout=remaining)
                except queue.Empty:
                    _safe_close(response)
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.DEADLINE_EXCEEDED,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                if raw_line is None:
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.TRANSPORT_ERROR,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                if not raw_line:
                    break
                line_count += 1
                total_bytes += len(raw_line)
                if (
                    len(raw_line) > _MAX_STREAM_LINE_BYTES
                    or line_count > _MAX_STREAM_LINES
                    or total_bytes > _MAX_STREAM_BYTES
                ):
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.STREAM_LIMIT,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.INVALID_STREAM,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                if not line or line.startswith((":", "event:", "id:", "retry:")):
                    continue
                if not line.startswith("data:"):
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.INVALID_STREAM,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    done_sentinel_received = True
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.INVALID_STREAM,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                if not _is_mapping(event):
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.INVALID_STREAM,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                event_count += 1
                if event_count > _MAX_STREAM_EVENTS:
                    return _StreamResponse(
                        status=int(response.status),
                        error_code=ProbeErrorCode.STREAM_LIMIT,
                        duration_ms=_safe_duration_ms(started),
                        event_count=event_count,
                        done_sentinel_received=done_sentinel_received,
                        content_delta_received=content_delta_received,
                        response_model_matches_requested=response_model_matches_requested,
                    )
                model_matches = _response_model_matches(event, self.target.model)
                if model_matches is False:
                    response_model_matches_requested = False
                elif (
                    model_matches is True
                    and response_model_matches_requested is None
                ):
                    response_model_matches_requested = True
                choices = event.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        if not _is_mapping(choice):
                            continue
                        delta = choice.get("delta")
                        if (
                            _is_mapping(delta)
                            and isinstance(delta.get("content"), str)
                            and delta.get("content")
                        ):
                            content_delta_received = True
            return _StreamResponse(
                status=int(response.status),
                error_code=None,
                duration_ms=_safe_duration_ms(started),
                event_count=event_count,
                done_sentinel_received=done_sentinel_received,
                content_delta_received=content_delta_received,
                response_model_matches_requested=response_model_matches_requested,
            )
        except (HTTPException, OSError, ValueError):
            return _StreamResponse(
                status=int(response.status),
                error_code=ProbeErrorCode.TRANSPORT_ERROR,
                duration_ms=_safe_duration_ms(started),
                event_count=event_count,
                done_sentinel_received=done_sentinel_received,
                content_delta_received=content_delta_received,
                response_model_matches_requested=response_model_matches_requested,
            )
        finally:
            stop_reader.set()


def _walk_forbidden_values(value: object, *, api_key: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in {
                "api_key",
                "apikey",
                "authorization",
                "authorization_header",
                "token",
                "secret",
                "password",
            }:
                raise SecretLeakError("report has a forbidden secret-bearing field")
            _walk_forbidden_values(child, api_key=api_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _walk_forbidden_values(child, api_key=api_key)
    elif isinstance(value, str) and api_key in value:
        raise SecretLeakError("report would contain the API key")


def report_payload_without_secrets(
    report: ProviderCapabilityProbeReport,
    *,
    api_key: str,
) -> dict[str, Any]:
    """Produce a serializable report or reject it before disk I/O."""

    payload = report.model_dump(mode="json", by_alias=True)
    _walk_forbidden_values(payload, api_key=api_key)
    return payload


def write_provider_probe_report(
    path: Path,
    report: ProviderCapabilityProbeReport,
    *,
    api_key: str,
    overwrite: bool = False,
) -> None:
    """Atomically write a caller-chosen report only after a secret scan."""

    payload = report_payload_without_secrets(report, api_key=api_key)
    target = path.expanduser()
    if target.exists() and not overwrite:
        raise ProviderProbeError("report output already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def report_has_measured_failures(report: ProviderCapabilityProbeReport) -> bool:
    capabilities = report.capabilities
    return any(
        result.status is CapabilityStatus.FAIL
        for result in (
            capabilities.models,
            capabilities.json_object,
            capabilities.named_function_tool_choice,
            capabilities.streaming,
        )
    )
