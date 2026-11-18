from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateContent,
    agent_candidate_content_hash,
)
from zhimeng_runtime.agent_provider import (
    ProviderCompleted,
    ProviderEvent,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)
from zhimeng_runtime.provider_probe import SecretLeakError


ROOT = Path(__file__).resolve().parents[3]
HARNESS = (
    ROOT
    / "services"
    / "writing-runtime"
    / "scripts"
    / "runtime_provider_acceptance.py"
)
CLI = (
    ROOT
    / "services"
    / "writing-runtime"
    / "scripts"
    / "accept_real_provider_run.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "runtime_provider_acceptance_test_harness",
    HARNESS,
)
assert _SPEC is not None and _SPEC.loader is not None
_HARNESS_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _HARNESS_MODULE
_SPEC.loader.exec_module(_HARNESS_MODULE)

RuntimeProviderAcceptanceConfig = (
    _HARNESS_MODULE.RuntimeProviderAcceptanceConfig
)
run_runtime_provider_acceptance = (
    _HARNESS_MODULE.run_runtime_provider_acceptance
)
write_runtime_provider_acceptance_report = (
    _HARNESS_MODULE.write_runtime_provider_acceptance_report
)


API_KEY = "acceptance-test-key-that-must-never-be-persisted"
MODEL = "fixture-grok-model"
RAW_TITLE = "REAL_PROVIDER_TITLE_9f659a25b67f4b04"
RAW_SUMMARY = "REAL_PROVIDER_SUMMARY_5bd1e5b754c14818"
RAW_BODY = "REAL_PROVIDER_BODY_120c4580c7e942a6"


class CandidateProvider:
    model = MODEL

    def __init__(
        self,
        *,
        body: str = RAW_BODY,
        reported_model: str | None = MODEL,
        accepted_reported_models: tuple[str, ...] = (),
    ) -> None:
        self.body = body
        self.reported_model = reported_model
        self.accepted_reported_models = accepted_reported_models
        self.requests = 0

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "f" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.acceptance-fixture",
            kind=ExecutorKind.REMOTE,
            implementation="acceptance-fixture-provider",
            version="1",
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
        self.requests += 1
        if cancel_event.is_set():
            return
        assert any(tool.name == "candidate_output_submit" for tool in tools)
        dispatch = _writer_dispatch(messages)
        instruction_dispatch = _creation_instruction_dispatch(messages)
        assert dispatch["runtime_adds_writer_task_reference"] is True
        assert instruction_dispatch["runtime_adds_materialization_reference"] is True
        yield ProviderToolCall(
            index=0,
            tool_call_id="call_real_provider_acceptance_0001",
            tool_name="candidate_output_submit",
            arguments={
                "kind": dispatch["required_candidate_kind"],
                "content": {
                    "title": RAW_TITLE,
                    "summary": RAW_SUMMARY,
                    "body": self.body,
                    "format": "markdown",
                },
            },
        )
        yield ProviderCompleted(
            "tool_calls",
            None,
            reported_model=self.reported_model,
        )


def _writer_dispatch(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema") == "zhimeng.writer-task-dispatch.v1":
            return payload
    raise AssertionError("Runtime Writer dispatch was not sent to Provider")


def _creation_instruction_dispatch(
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    for message in reversed(messages):
        if message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema") == "zhimeng.creation-instruction-dispatch.v1":
            return payload
    raise AssertionError("Runtime creation instruction was not sent to Provider")


def _config(
    workspace_root: Path,
    *,
    accepted_reported_models: tuple[str, ...] = (),
) -> RuntimeProviderAcceptanceConfig:
    return RuntimeProviderAcceptanceConfig(
        workspace_root=workspace_root,
        base_url="https://provider.example.test/v1",
        model=MODEL,
        api_key_env="TEST_REAL_PROVIDER_API_KEY",
        api_key=API_KEY,
        accepted_reported_models=accepted_reported_models,
        timeout_seconds=30,
        provider_timeout_seconds=30,
    )


def test_report_writer_rejects_secret_in_any_metadata_field(tmp_path: Path) -> None:
    report = run_runtime_provider_acceptance(
        _config(tmp_path),
        provider=CandidateProvider(),
    )
    leaked_target = report.target.model_copy(update={"model": API_KEY})
    leaked_report = report.model_copy(update={"target": leaked_target})
    output = tmp_path / "must-not-exist.json"

    with pytest.raises(SecretLeakError):
        write_runtime_provider_acceptance_report(
            output,
            leaked_report,
            api_key=API_KEY,
        )

    assert not output.exists()


def test_cli_rejects_direct_key_argument_without_echoing_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = importlib.util.spec_from_file_location(
        "accept_real_provider_run_test_cli",
        CLI,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit) as stopped:
        module.parse_args(
            [
                "--base-url",
                "https://provider.example.test/v1",
                "--model",
                MODEL,
                "--api-key-env",
                "TEST_REAL_PROVIDER_API_KEY",
                "--api-key",
                API_KEY,
            ]
        )

    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert API_KEY not in captured.out
    assert API_KEY not in captured.err
