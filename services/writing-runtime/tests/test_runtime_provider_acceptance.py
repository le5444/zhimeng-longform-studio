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


def test_real_provider_harness_reaches_candidate_writer_and_writes_hash_only_report(
    tmp_path: Path,
) -> None:
    provider = CandidateProvider()

    report = run_runtime_provider_acceptance(
        _config(tmp_path),
        provider=provider,
    )

    assert provider.requests == 1
    assert report.acceptance_status == "passed"
    assert report.runtime_schema_version is not None
    assert report.runtime_schema_version >= 20
    assert report.run.status == "succeeded"
    assert report.project_kb is not None
    assert report.project_kb.status == "current"
    assert report.project_kb.context_pack_binding_match is True
    assert report.project_kb.scope_state_revision >= 0
    assert len(report.provider_turns) == 1
    assert report.provider_turns[0].model_identity_status == "exact_match"
    assert report.provider_turns[0].requested_model == MODEL
    assert report.provider_turns[0].reported_model == MODEL
    assert report.provider_turns[0].reported_model_hash is not None
    assert report.provider_receipt_chain is not None
    assert report.provider_receipt_chain.provider_identity_status == "verified"
    assert report.provider_receipt_chain.requested_model == MODEL
    assert report.workflow is not None
    assert len(report.workflow.checkpoints) == 3
    assert report.workflow.provider_boundary_id is not None
    assert report.prewrite_gate is not None
    assert report.prewrite_gate.status == "succeeded"
    assert report.candidate is not None
    assert report.candidate.provider_identity_status == "verified"
    assert report.candidate.provider_turn_receipt_ids == (
        report.provider_turns[0].receipt_id,
    )
    assert report.candidate.provider_turn_receipt_hashes == (
        report.provider_turns[0].receipt_hash,
    )
    assert report.writer is not None
    assert report.writer_trace is not None
    assert report.writer_trace.schema_id == "zhimeng.writer-trace.v1"
    assert report.writer_trace.run_id == report.run.run_id
    assert report.writer_trace.writer_task_id == report.writer.writer_task_id
    assert report.writer_trace.writer_task_hash == report.writer.writer_task_hash
    assert report.writer_trace.candidate_output_id == report.candidate.candidate_output_id
    assert report.writer_trace.candidate_output_hash == report.candidate.envelope_hash
    assert report.writer_trace.automatic_provider_replay_count == 0
    assert report.writer_trace.provider_recovery_present is False
    expected_content = AgentCandidateContent(
        title=RAW_TITLE,
        summary=RAW_SUMMARY,
        body=RAW_BODY,
        format="markdown",
    )
    assert report.candidate.content_hash == agent_candidate_content_hash(
        expected_content
    )
    assert report.writer.candidate_output_id == report.candidate.candidate_output_id
    assert report.writer.candidate_output_hash == report.candidate.envelope_hash
    assert report.storage.api_key_persisted is False
    assert report.storage.authorization_header_persisted is False
    assert report.storage.project_unchanged is True
    assert report.storage.isolated_runtime_removed is True
    assert not list(
        (tmp_path / ".runtime" / "provider-acceptance").glob("work-*")
    )

    output = tmp_path / ".runtime" / "provider-acceptance" / "report.json"
    write_runtime_provider_acceptance_report(
        output,
        report,
        api_key=API_KEY,
    )
    written = output.read_text(encoding="utf-8")
    assert API_KEY not in written
    assert RAW_TITLE not in written
    assert RAW_SUMMARY not in written
    assert RAW_BODY not in written
    assert "Authorization" not in written
    payload = json.loads(written)
    assert payload["project_kb"]["status"] == "current"
    assert payload["project_kb"]["context_pack_binding_match"] is True
    assert payload["candidate"]["content_hash"] == report.candidate.content_hash
    assert payload["provider_exchange"]["tool_call_count"] == 1


def test_provider_secret_reflection_fails_managed_run_without_persisting_secret(
    tmp_path: Path,
) -> None:
    report = run_runtime_provider_acceptance(
        _config(tmp_path),
        provider=CandidateProvider(body=API_KEY),
    )

    assert report.acceptance_status == "failed"
    assert report.run.status == "failed"
    assert report.run.error_code == "provider_acceptance_secret_reflection"
    assert report.failure_code == "provider_acceptance_secret_reflection"
    assert report.candidate is None
    assert report.writer is None
    assert report.writer_trace is None
    assert report.storage.api_key_persisted is False
    assert report.storage.authorization_header_persisted is False
    assert report.storage.isolated_runtime_removed is True

    output = tmp_path / ".runtime" / "provider-acceptance" / "failed.json"
    write_runtime_provider_acceptance_report(
        output,
        report,
        api_key=API_KEY,
    )
    written = output.read_text(encoding="utf-8")
    assert API_KEY not in written
    assert "Authorization" not in written


def test_unreported_provider_model_cannot_turn_managed_success_into_acceptance(
    tmp_path: Path,
) -> None:
    report = run_runtime_provider_acceptance(
        _config(tmp_path),
        provider=CandidateProvider(reported_model=None),
    )

    assert report.run.status == "succeeded"
    assert report.candidate is not None
    assert report.candidate.provider_identity_status == "unreported"
    assert report.writer is not None
    assert report.writer_trace is not None
    assert len(report.provider_turns) == 1
    assert report.provider_turns[0].model_identity_status == "unreported"
    assert report.provider_turns[0].requested_model == MODEL
    assert report.provider_turns[0].reported_model is None
    assert report.provider_receipt_chain is not None
    assert report.provider_receipt_chain.provider_identity_status == "unreported"
    assert report.acceptance_status == "failed"
    assert report.failure_code == "managed_terminal_contract_incomplete"


def test_unaccepted_model_alias_fails_before_candidate_submission(
    tmp_path: Path,
) -> None:
    report = run_runtime_provider_acceptance(
        _config(tmp_path),
        provider=CandidateProvider(reported_model="fixture-unaccepted-alias"),
    )

    assert report.run.status == "failed"
    assert report.run.error_code == "agent_provider_model_mismatch"
    assert report.acceptance_status == "failed"
    assert report.failure_code == "agent_provider_model_mismatch"
    assert report.candidate is None
    assert report.writer is None
    assert report.writer_trace is None
    assert len(report.provider_turns) == 1
    assert report.provider_turns[0].model_identity_status == "rejected_mismatch"
    assert report.provider_turns[0].reported_model == "fixture-unaccepted-alias"


def test_explicit_model_alias_is_recorded_and_can_pass_only_with_allowlist(
    tmp_path: Path,
) -> None:
    alias = "fixture-explicit-grok-alias"
    report = run_runtime_provider_acceptance(
        _config(tmp_path, accepted_reported_models=(alias,)),
        provider=CandidateProvider(
            reported_model=alias,
            accepted_reported_models=(alias,),
        ),
    )

    assert report.acceptance_status == "passed"
    assert report.run.status == "succeeded"
    assert len(report.provider_turns) == 1
    assert report.provider_turns[0].model_identity_status == "accepted_alias"
    assert report.provider_turns[0].requested_model == MODEL
    assert report.provider_turns[0].reported_model == alias
    assert report.target.accepted_reported_model_count == 1
    assert len(report.target.accepted_reported_model_hashes) == 1

    output = tmp_path / ".runtime" / "provider-acceptance" / "alias.json"
    write_runtime_provider_acceptance_report(
        output,
        report,
        api_key=API_KEY,
    )
    written = output.read_text(encoding="utf-8")
    assert alias in written
    assert RAW_TITLE not in written
    assert RAW_SUMMARY not in written
    assert RAW_BODY not in written


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
