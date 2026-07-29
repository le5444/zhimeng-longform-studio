from __future__ import annotations

import importlib.util
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from zhimeng_runtime.provider_probe import (
    CapabilityStatus,
    CompatibleProviderProbe,
    JsonObjectCapability,
    ModelsCapability,
    NamedFunctionToolChoiceCapability,
    ProbeErrorCode,
    ProviderCapabilityProbeReport,
    ProviderProbeError,
    ProviderTarget,
    StreamingCapability,
    SecretLeakError,
    read_api_key_from_environment,
    report_has_measured_failures,
    write_provider_probe_report,
)


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"
SCRIPT = ROOT / "services" / "writing-runtime" / "scripts" / "probe_compatible_provider.py"
TEST_KEY = "test-provider-key-which-must-not-be-written"
MODEL = "fixture-model"
FIXTURE_NONCE = "0123456789abcdef"


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        json_body: dict[str, Any] | None = None,
        stream_lines: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = (
            b"" if json_body is None else json.dumps(json_body).encode("utf-8")
        )
        self._stream_lines = deque(stream_lines or [])
        self.headers = headers or {
            "Content-Type": (
                "text/event-stream; charset=utf-8"
                if stream_lines is not None
                else "application/json"
            )
        }
        self.closed = False

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self._body
        return self._body[:amount]

    def readline(self, limit: int = -1) -> bytes:
        if not self._stream_lines:
            return b""
        line = self._stream_lines.popleft()
        return line if limit < 0 else line[:limit]

    def close(self) -> None:
        self.closed = True


class RecordingOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any, *, timeout: float) -> FakeResponse:
        self.requests.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "authorization": request.get_header("Authorization"),
                "accept": request.get_header("Accept"),
                "user_agent": request.get_header("User-agent"),
                "body": request.data,
                "timeout": timeout,
            }
        )
        assert self.responses, "unexpected network request"
        return self.responses.popleft()


class BlockingStreamResponse(FakeResponse):
    def __init__(self) -> None:
        super().__init__()
        self._release = threading.Event()

    def readline(self, limit: int = -1) -> bytes:
        self._release.wait(timeout=5)
        return b""

    def close(self) -> None:
        super().close()
        self._release.set()


class BlockingJsonResponse(BlockingStreamResponse):
    def read(self, amount: int | None = None) -> bytes:
        self._release.wait(timeout=5)
        return b"{}"


def _success_responses() -> list[FakeResponse]:
    return [
        FakeResponse(json_body={"data": [{"id": MODEL}]}),
        FakeResponse(
            json_body={
                "model": MODEL,
                "choices": [
                    {
                        "message": {"content": f"PLAIN_{FIXTURE_NONCE}"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ),
        FakeResponse(
            json_body={
                "model": MODEL,
                "choices": [
                    {
                        "message": {"content": '{"ok": true}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 3,
                    "total_tokens": 14,
                },
            }
        ),
        FakeResponse(
            json_body={
                "model": MODEL,
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_fixture",
                                    "type": "function",
                                    "function": {
                                        "name": f"provider_probe_selected_{FIXTURE_NONCE}",
                                        "arguments": (
                                            '{"marker": "provider-probe-'
                                            f'{FIXTURE_NONCE}"}}'
                                        ),
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ),
        FakeResponse(
            stream_lines=[
                (
                    b'data: {"model":"fixture-model","choices":['
                    b'{"delta":{"content":"READY"},"finish_reason":null}]}\n\n'
                ),
                b"data: [DONE]\n\n",
            ]
        ),
    ]


def _probe(opener: RecordingOpener) -> CompatibleProviderProbe:
    return CompatibleProviderProbe(
        base_url="https://provider.example.test/v1/",
        model=MODEL,
        api_key_env="TEST_PROVIDER_KEY",
        api_key=TEST_KEY,
        opener=opener,
        probe_nonce=FIXTURE_NONCE,
    )


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("probe_compatible_provider_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_probe_records_models_json_named_tool_streaming_and_cancel_without_network() -> None:
    opener = RecordingOpener(_success_responses())

    report = _probe(opener).run()

    assert not opener.responses
    assert [item["url"] for item in opener.requests] == [
        "https://provider.example.test/v1/models",
        "https://provider.example.test/v1/chat/completions",
        "https://provider.example.test/v1/chat/completions",
        "https://provider.example.test/v1/chat/completions",
        "https://provider.example.test/v1/chat/completions",
    ]
    assert all(item["authorization"] == f"Bearer {TEST_KEY}" for item in opener.requests)
    assert [item["accept"] for item in opener.requests] == [
        "application/json",
        "application/json",
        "application/json",
        "application/json",
        "text/event-stream",
    ]
    assert all(item["user_agent"] for item in opener.requests)
    assert report.capabilities.models.status is CapabilityStatus.PASS
    assert report.capabilities.models.selected_model_listed is True
    assert report.capabilities.json_object.status is CapabilityStatus.PASS
    assert report.capabilities.json_object.plain_text_control_observed is True
    assert report.capabilities.json_object.valid_json_object is True
    assert (
        report.capabilities.json_object.format_enforced_against_plain_text_control
        is True
    )
    assert report.capabilities.named_function_tool_choice.status is CapabilityStatus.PASS
    assert report.capabilities.named_function_tool_choice.function_type_valid is True
    assert report.capabilities.named_function_tool_choice.tool_call_id_present is True
    assert report.capabilities.named_function_tool_choice.selected_named_function is True
    assert report.capabilities.named_function_tool_choice.arguments_match_expected_marker is True
    assert report.capabilities.streaming.status is CapabilityStatus.PASS
    assert report.capabilities.streaming.done_sentinel_received is True
    assert report.capabilities.streaming.content_delta_received is True
    assert report.capabilities.streaming.content_type_is_event_stream is True
    assert report.capabilities.cancel.status is CapabilityStatus.NOT_MEASURED
    assert report.capabilities.cancel.reason == "no_portable_server_cancel_contract"
    assert report.probe_limits.timeout_seconds == 30
    tool_request = json.loads(opener.requests[3]["body"])
    assert len(tool_request["tools"]) == 2
    assert tool_request["tool_choice"]["function"]["name"].startswith(
        "provider_probe_selected_"
    )
    assert tool_request["tools"][0]["function"]["parameters"]["properties"][
        "marker"
    ]["enum"] == [f"provider-probe-{FIXTURE_NONCE}"]
    assert tool_request["tools"][1]["function"]["parameters"]["properties"][
        "marker"
    ]["enum"] == ["decoy"]
    assert "provider_probe_decoy_" in tool_request["messages"][0]["content"]
    assert not report_has_measured_failures(report)


def test_probe_records_a_real_failure_without_turning_it_into_pass() -> None:
    responses = _success_responses()
    responses[0] = FakeResponse(json_body={"data": [{"id": "other-model"}]})
    report = _probe(RecordingOpener(responses)).run()

    assert report.capabilities.models.status is CapabilityStatus.FAIL
    assert report.capabilities.models.selected_model_listed is False
    assert report_has_measured_failures(report)


def test_skip_streaming_stays_explicitly_not_measured() -> None:
    opener = RecordingOpener(_success_responses()[:4])

    report = _probe(opener).run(include_streaming=False)

    assert report.capabilities.streaming.status is CapabilityStatus.NOT_MEASURED
    assert report.capabilities.streaming.skipped_by_caller is True
    assert report.capabilities.streaming.duration_ms is None
    assert report.capabilities.cancel.status is CapabilityStatus.NOT_MEASURED
    assert not opener.responses


def test_json_mode_requires_a_plain_text_control_difference() -> None:
    responses = _success_responses()
    responses[1] = FakeResponse(
        json_body={
            "model": MODEL,
            "choices": [
                {
                    "message": {"content": '{"followed_prompt": false}'},
                    "finish_reason": "stop",
                }
            ],
        }
    )

    report = _probe(RecordingOpener(responses)).run()

    assert report.capabilities.json_object.status is CapabilityStatus.FAIL
    assert report.capabilities.json_object.plain_text_control_observed is False
    assert (
        report.capabilities.json_object.format_enforced_against_plain_text_control
        is False
    )


def test_streaming_requires_event_stream_content_type() -> None:
    response = _success_responses()[-1]
    response.headers = {"Content-Type": "application/json"}
    result = _probe(RecordingOpener([response]))._probe_streaming()

    assert result.status is CapabilityStatus.FAIL
    assert result.error_code is ProbeErrorCode.INVALID_STREAM
    assert result.content_type_is_event_stream is False


def test_report_write_is_secret_scanned_and_atomic(tmp_path: Path) -> None:
    report = _probe(RecordingOpener(_success_responses())).run()
    output = tmp_path / "provider-report.json"

    write_provider_probe_report(output, report, api_key=TEST_KEY)

    written = output.read_text(encoding="utf-8")
    assert TEST_KEY not in written
    assert "Authorization" not in written
    assert "\"content\"" not in written
    assert json.loads(written)["schema"] == "zhimeng.provider-capability-probe.v3"

    rejected = tmp_path / "rejected.json"
    with pytest.raises(SecretLeakError):
        write_provider_probe_report(rejected, report, api_key=MODEL)
    assert not rejected.exists()


def test_api_key_only_comes_from_a_named_environment_variable() -> None:
    assert read_api_key_from_environment(
        "TEST_PROVIDER_KEY",
        environ={"TEST_PROVIDER_KEY": TEST_KEY},
    ) == TEST_KEY
    with pytest.raises(ProviderProbeError):
        read_api_key_from_environment("TEST-PROVIDER-KEY", environ={})
    with pytest.raises(ProviderProbeError):
        read_api_key_from_environment("TEST_PROVIDER_KEY", environ={})


def test_remote_cleartext_http_is_rejected_but_explicit_loopback_is_allowed() -> None:
    with pytest.raises(ProviderProbeError, match="HTTPS"):
        CompatibleProviderProbe(
            base_url="http://provider.example.test/v1",
            model=MODEL,
            api_key_env="TEST_PROVIDER_KEY",
            api_key=TEST_KEY,
        )

    probe = CompatibleProviderProbe(
        base_url="http://127.0.0.1:8080/v1",
        model=MODEL,
        api_key_env="TEST_PROVIDER_KEY",
        api_key=TEST_KEY,
        allow_insecure_loopback_http=True,
        opener=RecordingOpener([]),
    )
    assert probe.target.insecure_loopback_http is True


def test_streaming_probe_has_a_total_deadline_for_keepalive_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(stream_lines=[b": keepalive\n"] * 20)
    opener = RecordingOpener([response])
    probe = CompatibleProviderProbe(
        base_url="https://provider.example.test/v1",
        model=MODEL,
        api_key_env="TEST_PROVIDER_KEY",
        api_key=TEST_KEY,
        timeout_seconds=2,
        opener=opener,
    )
    clock_values = iter([0.0, 0.0, 1.0, 2.1])
    last_clock = [2.1]

    def fake_monotonic() -> float:
        try:
            last_clock[0] = next(clock_values)
        except StopIteration:
            pass
        return last_clock[0]

    monkeypatch.setattr(
        "zhimeng_runtime.provider_probe.time.monotonic", fake_monotonic
    )

    result = probe._probe_streaming()

    assert result.status is CapabilityStatus.FAIL
    assert result.error_code is ProbeErrorCode.DEADLINE_EXCEEDED


def test_streaming_deadline_interrupts_a_readline_that_never_returns() -> None:
    response = BlockingStreamResponse()
    probe = CompatibleProviderProbe(
        base_url="https://provider.example.test/v1",
        model=MODEL,
        api_key_env="TEST_PROVIDER_KEY",
        api_key=TEST_KEY,
        timeout_seconds=0.05,
        opener=RecordingOpener([response]),
    )

    started = time.monotonic()
    result = probe._probe_streaming()

    assert time.monotonic() - started < 0.5
    assert result.status is CapabilityStatus.FAIL
    assert result.error_code is ProbeErrorCode.DEADLINE_EXCEEDED
    assert response.closed is True


def test_json_deadline_interrupts_a_body_read_that_never_returns() -> None:
    response = BlockingJsonResponse()
    probe = CompatibleProviderProbe(
        base_url="https://provider.example.test/v1",
        model=MODEL,
        api_key_env="TEST_PROVIDER_KEY",
        api_key=TEST_KEY,
        timeout_seconds=0.05,
        opener=RecordingOpener([response]),
    )

    started = time.monotonic()
    result = probe._probe_models()

    assert time.monotonic() - started < 0.5
    assert result.status is CapabilityStatus.FAIL
    assert result.error_code is ProbeErrorCode.DEADLINE_EXCEEDED
    assert response.closed is True


def test_default_http_transport_obeys_wall_clock_deadline_on_slow_body() -> None:
    release = threading.Event()

    class SlowBodyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.flush()
            release.wait(timeout=5)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowBodyHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        probe = CompatibleProviderProbe(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model=MODEL,
            api_key_env="TEST_PROVIDER_KEY",
            api_key=TEST_KEY,
            allow_insecure_loopback_http=True,
            timeout_seconds=0.05,
        )
        started = time.monotonic()
        result = probe._probe_models()

        assert time.monotonic() - started < 0.5
        assert result.status is CapabilityStatus.FAIL
        assert result.error_code is ProbeErrorCode.DEADLINE_EXCEEDED
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_v3_target_requires_explicit_transport_security_evidence() -> None:
    with pytest.raises(ValidationError):
        ProviderTarget(
            base_url="https://provider.example.test/v1",
            model=MODEL,
            api_key_env="TEST_PROVIDER_KEY",
            user_agent="fixture-agent",
        )


@pytest.mark.parametrize(
    ("base_url", "insecure_loopback_http"),
    [
        ("http://127.0.0.1:8080/v1", False),
        ("https://provider.example.test/v1", True),
        ("http://provider.example.test/v1", True),
        ("https://provider.example.test/v1/", False),
    ],
)
def test_v3_target_rejects_inconsistent_or_unnormalized_transport_evidence(
    base_url: str,
    insecure_loopback_http: bool,
) -> None:
    with pytest.raises(ValidationError):
        ProviderTarget(
            base_url=base_url,
            model=MODEL,
            api_key_env="TEST_PROVIDER_KEY",
            user_agent="fixture-agent",
            insecure_loopback_http=insecure_loopback_http,
        )


@pytest.mark.parametrize(
    "capability",
    [
        lambda: ModelsCapability(status="pass", http_status=200),
        lambda: JsonObjectCapability(status="pass", http_status=200),
        lambda: NamedFunctionToolChoiceCapability(status="pass", http_status=200),
        lambda: StreamingCapability(status="pass", http_status=200),
    ],
)
def test_capability_contract_rejects_pass_without_required_evidence(capability) -> None:
    with pytest.raises(ValidationError):
        capability()


def test_cli_does_not_print_key_when_a_safe_report_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script_module()
    report = _probe(RecordingOpener(_success_responses())).run(include_streaming=False)

    class FakeProbe:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["api_key"] == TEST_KEY

        def run(self, *, include_streaming: bool) -> ProviderCapabilityProbeReport:
            assert include_streaming is False
            return report

    monkeypatch.setenv("TEST_PROVIDER_KEY", TEST_KEY)
    monkeypatch.setattr(module, "CompatibleProviderProbe", FakeProbe)
    exit_code = module.main(
        [
            "--base-url",
            "https://provider.example.test/v1",
            "--model",
            MODEL,
            "--api-key-env",
            "TEST_PROVIDER_KEY",
            "--output",
            str(tmp_path / "result.json"),
            "--skip-streaming",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert TEST_KEY not in captured.out
    assert TEST_KEY not in captured.err
    assert "Provider capability report written." in captured.out


def test_provider_probe_contract_matches_the_pydantic_model() -> None:
    stored = json.loads(
        (CONTRACTS / "provider-capability-probe.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith("provider-capability-probe.schema.json")
    assert stored == ProviderCapabilityProbeReport.model_json_schema(by_alias=True)


def test_v2_contract_is_archived_and_v3_schema_encodes_pass_evidence() -> None:
    legacy = json.loads(
        (CONTRACTS / "provider-capability-probe-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    current = json.loads(
        (CONTRACTS / "provider-capability-probe.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert legacy["$id"].endswith("provider-capability-probe-v2.schema.json")
    assert legacy["properties"]["schema"]["const"].endswith(".v2")
    assert "insecure_loopback_http" not in legacy["$defs"]["ProviderTarget"][
        "required"
    ]
    assert current["properties"]["schema"]["const"].endswith(".v3")
    assert current["$defs"]["ModelsCapability"]["allOf"]
    assert current["$defs"]["NamedFunctionToolChoiceCapability"]["allOf"]
    assert current["$defs"]["StreamingCapability"]["allOf"]
    named_pass_rules = current["$defs"]["NamedFunctionToolChoiceCapability"][
        "allOf"
    ][0]["then"]
    assert "finish_reason_present" in named_pass_rules["required"]
    assert named_pass_rules["properties"]["finish_reason_present"] == {
        "const": True
    }


def test_cli_requires_a_caller_chosen_output_path() -> None:
    module = _load_script_module()
    with pytest.raises(SystemExit):
        module.parse_args(
            [
                "--base-url",
                "https://provider.example.test/v1",
                "--model",
                MODEL,
                "--api-key-env",
                "TEST_PROVIDER_KEY",
            ]
        )


def test_cli_argument_errors_do_not_echo_a_mistakenly_supplied_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_script_module()
    with pytest.raises(SystemExit):
        module.parse_args(["--api-key", TEST_KEY])
    captured = capsys.readouterr()

    assert TEST_KEY not in captured.out
    assert TEST_KEY not in captured.err
    assert "invalid arguments" in captured.err
