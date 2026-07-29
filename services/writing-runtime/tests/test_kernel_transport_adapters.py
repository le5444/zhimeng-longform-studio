from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_kernel_transport_probe as transport_probe_runner

from kernel_harness.adapters.base import (
    BoundedEventBuffer,
    EvidenceByteBudget,
    KernelEnvelope,
    KernelSkill,
    ProbeCheck,
    TransportCapabilitySnapshot,
    TransportProbeResult,
)
from kernel_harness.adapters.codex import (
    CodexAppServerAdapter,
    _evaluate_harness_skill,
    _measured_capabilities_for_skill,
    _method_names,
    canonical_json_hash,
)
from kernel_harness.adapters.opencode import (
    OpenCodeServerAdapter,
    _is_event_stream_content_type,
    _is_user_only_no_reply_message,
    _usage_total,
)
from kernel_harness.adapters.process_env import (
    isolated_process_environment,
    stage_pinned_binary,
)
from zhimeng_runtime.execution_protocol import ExecutorIdentity


def _binary(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"fixture binary")
    return path


def test_canonical_schema_hash_ignores_object_order_but_not_array_order() -> None:
    left = {"definitions": {"b": {"type": "string"}, "a": {"type": "null"}}}
    right = {"definitions": {"a": {"type": "null"}, "b": {"type": "string"}}}

    assert canonical_json_hash(left) == canonical_json_hash(right)
    assert canonical_json_hash(["a", "b"]) != canonical_json_hash(["b", "a"])


def test_codex_method_allowlist_detects_required_and_forbidden_surfaces() -> None:
    schema = {
        "methods": [
            "initialize",
            "thread/start",
            "thread/resume",
            "thread/read",
            "thread/list",
            "turn/start",
            "turn/interrupt",
            "skills/list",
            "skills/extraRoots/set",
            "thread/shellCommand",
            "process/spawn",
        ]
    }

    methods = _method_names(schema)

    assert "turn/interrupt" in methods
    assert "thread/shellCommand" in methods
    assert "process/spawn" in methods


def test_isolated_environment_does_not_forward_provider_credentials(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("BIGMODEL_API_KEY", "must-not-leak")

    environment = isolated_process_environment(tmp_path / "isolated")

    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "BIGMODEL_API_KEY" not in environment
    assert Path(environment["USERPROFILE"]).is_relative_to(tmp_path)
    assert Path(environment["APPDATA"]).is_relative_to(tmp_path)
    assert Path(environment["XDG_DATA_HOME"]).is_relative_to(tmp_path)


def test_kernel_binary_is_copied_and_hash_checked_before_execution(tmp_path: Path) -> None:
    source = _binary(tmp_path, "candidate.exe")
    isolation = tmp_path / "isolation"

    with pytest.raises(RuntimeError, match="SHA-256"):
        stage_pinned_binary(source, isolation, expected_sha256="0" * 64)
    assert not (isolation / "pinned-bin" / source.name).exists()

    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    staged, observed = stage_pinned_binary(
        source,
        isolation,
        expected_sha256=expected,
    )
    assert staged.read_bytes() == source.read_bytes()
    assert observed == expected


def test_adapter_environment_roots_are_isolated_and_server_secret_is_redacted(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    skill = tmp_path / "skill"
    project.mkdir()
    skill.mkdir()
    codex = CodexAppServerAdapter(
        binary=_binary(tmp_path, "codex.exe"),
        project_root=project,
        skill_roots=[skill],
        isolation_root=tmp_path / "codex-isolation",
        evidence_root=tmp_path / "codex-evidence",
    )
    opencode = OpenCodeServerAdapter(
        binary=_binary(tmp_path, "opencode.exe"),
        project_root=project,
        isolation_root=tmp_path / "opencode-isolation",
        evidence_root=tmp_path / "opencode-evidence",
    )

    assert Path(codex.environment["CODEX_HOME"]).is_relative_to(tmp_path)
    assert Path(codex.environment["USERPROFILE"]).is_relative_to(tmp_path)
    assert opencode.environment["OPENCODE_SERVER_PASSWORD"] == "<redacted>"
    assert Path(opencode.environment["XDG_DATA_HOME"]).is_relative_to(tmp_path)
    assert Path(opencode.environment["OPENCODE_CONFIG_DIR"]).is_dir()
    assert opencode.password not in opencode.environment.values()
    expected_basic = base64.b64encode(
        f"{opencode.username}:{opencode.password}".encode("utf-8")
    ).decode("ascii")
    assert opencode.evidence_secret_values == (
        opencode.password,
        f"{opencode.username}:{opencode.password}",
        f"Basic {expected_basic}",
        expected_basic,
    )


def test_codex_transcript_redacts_discovered_skill_instructions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    skill = tmp_path / "skill"
    project.mkdir()
    skill.mkdir()
    adapter = CodexAppServerAdapter(
        binary=_binary(tmp_path, "codex.exe"),
        project_root=project,
        skill_roots=[skill],
        isolation_root=tmp_path / "codex-isolation",
        evidence_root=tmp_path / "codex-evidence",
    )
    adapter._request_methods[7] = "skills/list"
    source = {
        "id": 7,
        "result": {
            "data": [
                {
                    "cwd": str(project),
                    "skills": [
                        {
                            "name": "fixture-skill",
                            "path": str(skill / "SKILL.md"),
                            "scope": "user",
                            "enabled": True,
                            "description": "private instructions must not enter evidence",
                            "interface": {"defaultPrompt": "also private"},
                        }
                    ],
                }
            ]
        },
    }

    sanitized = adapter._sanitized_transcript_message(source)

    rendered = json.dumps(sanitized, ensure_ascii=False)
    assert "private instructions" not in rendered
    assert "also private" not in rendered
    assert sanitized["result"]["data"][0]["skills"] == [
        {
            "name": "fixture-skill",
            "path": str(skill / "SKILL.md"),
            "scope": "user",
            "enabled": True,
        }
    ]


def _kernel_skill(*, path: Path, enabled: bool = True) -> KernelSkill:
    return KernelSkill(
        name="executor-test-skill",
        path=str(path),
        enabled=enabled,
        metadata={},
    )


def test_codex_skill_measurement_rejects_same_name_at_wrong_path(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected" / "SKILL.md"
    wrong = tmp_path / "wrong" / "SKILL.md"
    expected.parent.mkdir()
    wrong.parent.mkdir()
    expected.write_text("expected", encoding="utf-8")
    wrong.write_text("wrong", encoding="utf-8")
    digest = hashlib.sha256(expected.read_bytes()).hexdigest()

    passed, evidence = _evaluate_harness_skill(
        [_kernel_skill(path=wrong)],
        expected_path=expected,
        before_hash=digest,
        after_hash=digest,
    )

    assert passed is False
    assert evidence["same_name_count"] == 1
    assert evidence["exact_path_count"] == 0


def test_codex_skill_measurement_rejects_disabled_exact_match(tmp_path: Path) -> None:
    expected = tmp_path / "executor-test-skill" / "SKILL.md"
    expected.parent.mkdir()
    expected.write_text("fixture", encoding="utf-8")
    digest = hashlib.sha256(expected.read_bytes()).hexdigest()

    passed, evidence = _evaluate_harness_skill(
        [_kernel_skill(path=expected, enabled=False)],
        expected_path=expected,
        before_hash=digest,
        after_hash=digest,
    )

    assert passed is False
    assert evidence["exact_path_count"] == 1
    assert evidence["enabled_exact_path_count"] == 0


def test_codex_skill_measurement_rejects_content_change(tmp_path: Path) -> None:
    expected = tmp_path / "executor-test-skill" / "SKILL.md"
    expected.parent.mkdir()
    expected.write_text("before", encoding="utf-8")
    before_hash = hashlib.sha256(expected.read_bytes()).hexdigest()
    expected.write_text("after", encoding="utf-8")
    after_hash = hashlib.sha256(expected.read_bytes()).hexdigest()

    passed, evidence = _evaluate_harness_skill(
        [_kernel_skill(path=expected)],
        expected_path=expected,
        before_hash=before_hash,
        after_hash=after_hash,
    )

    assert passed is False
    assert evidence["content_stable"] is False


def test_codex_skill_capability_is_measured_only_after_integrity_check() -> None:
    assert _measured_capabilities_for_skill(False) == []
    assert [
        capability.value for capability in _measured_capabilities_for_skill(True)
    ] == ["skills"]


def test_usage_counter_only_counts_cost_and_token_subtrees() -> None:
    assert _usage_total({"parts": [{"text": "123"}], "sequence": 9}) == 0
    assert _usage_total({"cost": 0, "tokens": {"input": 0, "output": 0}}) == 0
    assert _usage_total({"tokens": {"input": 3, "output": 2}}) == 5


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text/event-stream", True),
        ("Text/Event-Stream; charset=utf-8", True),
        ("application/json", False),
        (None, False),
    ],
)
def test_opencode_sse_requires_event_stream_media_type(
    value: str | None,
    expected: bool,
) -> None:
    assert _is_event_stream_content_type(value) is expected


def test_no_reply_check_requires_exactly_one_zero_usage_user_message() -> None:
    message = {
        "info": {
            "role": "user",
            "agent": "build",
            "model": {"providerID": "opencode", "modelID": "big-pickle"},
        },
        "parts": [{"type": "text", "text": "transport-only"}],
    }

    assert _is_user_only_no_reply_message(
        [message],
        expected_text="transport-only",
    )
    assert not _is_user_only_no_reply_message(
        [message, {"info": {"role": "assistant"}, "parts": []}],
        expected_text="transport-only",
    )
    assert not _is_user_only_no_reply_message(
        [{**message, "tokens": {"input": 1}}],
        expected_text="transport-only",
    )


def test_transport_probe_passed_is_derived_from_every_check() -> None:
    base = {
        "schema": "zhimeng.kernel-transport-probe.v2",
        "adapter": ExecutorIdentity(
            executor_id="executor.test",
            kind="process",
            implementation="fixture",
            version="1",
            protocol_version="1",
            instance_id="instance.test",
        ),
        "binary_path": "C:/fixture/executor.exe",
        "binary_hash": "a" * 64,
        "protocol_schema_hash": "b" * 64,
        "config_hash": "c" * 64,
        "capabilities": {"declared": ["streaming"], "measured": []},
        "session": None,
        "metadata": {},
    }
    passed = TransportProbeResult(
        **base,
        checks=[
            ProbeCheck(
                check_id="check.transport",
                passed=True,
                summary="Transport passed.",
                evidence_ref="evidence://transport",
            )
        ],
        required_check_ids=["check.transport"],
    )
    failed = passed.model_copy(
        update={
            "checks": [
                passed.checks[0].model_copy(update={"passed": False})
            ]
        }
    )

    assert passed.passed is True
    assert failed.passed is False


def test_transport_probe_requires_configured_check_set() -> None:
    with pytest.raises(ValueError, match="missing required checks"):
        TransportProbeResult(
            schema="zhimeng.kernel-transport-probe.v2",
            adapter=ExecutorIdentity(
                executor_id="executor.test",
                kind="process",
                implementation="fixture",
                version="1",
                protocol_version="1",
                instance_id="instance.test",
            ),
            binary_path="C:/fixture/executor.exe",
            binary_hash="a" * 64,
            protocol_schema_hash="b" * 64,
            config_hash="c" * 64,
            capabilities={"declared": ["streaming"], "measured": []},
            session=None,
            checks=[
                ProbeCheck(
                    check_id="check.optional",
                    passed=True,
                    summary="Optional only.",
                    evidence_ref="evidence://optional",
                )
            ],
            required_check_ids=["check.required"],
            metadata={},
        )


def test_evidence_budget_is_shared_across_streams_and_never_exceeds_bytes() -> None:
    first = io.StringIO()
    second = io.StringIO()
    budget = EvidenceByteBudget(limit_bytes=7)

    budget.write_text(first, "1234")
    budget.write_text(second, "中x")
    budget.write_text(first, "overflow")

    assert len(first.getvalue().encode("utf-8")) == 4
    assert len(second.getvalue().encode("utf-8")) == 3
    assert budget.bytes_written == budget.limit_bytes == 7
    assert budget.truncated is True


def test_event_buffer_enforces_item_and_total_byte_limits() -> None:
    event = KernelEnvelope(
        sequence=1,
        kind="notification",
        method="fixture.event",
        request_id=None,
        payload={"value": "bounded"},
    )
    event_size = len(event.model_dump_json(by_alias=True).encode("utf-8"))
    buffer = BoundedEventBuffer(
        item_limit=2,
        byte_limit=event_size,
        per_event_byte_limit=event_size,
    )
    buffer.put(event)

    with pytest.raises(RuntimeError, match="total byte limit"):
        buffer.put(event.model_copy(update={"sequence": 2}))
    assert buffer.overflowed is True
    assert asyncio.run(buffer.get()) == event
    assert buffer.current_bytes == 0
    buffer.close()
    assert asyncio.run(buffer.get()) is None


def test_event_buffer_close_marks_overflow_if_terminal_marker_needs_eviction() -> None:
    buffer = BoundedEventBuffer(
        item_limit=1,
        byte_limit=4096,
        per_event_byte_limit=4096,
    )
    buffer.put(
        KernelEnvelope(
            sequence=1,
            kind="notification",
            method="fixture.event",
            request_id=None,
            payload={},
        )
    )

    buffer.close()

    assert buffer.overflowed is True
    assert asyncio.run(buffer.get()) is None


def test_measured_transport_capabilities_must_be_declared() -> None:
    snapshot = TransportCapabilitySnapshot(
        declared=["streaming", "cancel"],
        measured=["streaming"],
    )
    assert [item.value for item in snapshot.declared] == ["streaming", "cancel"]
    assert [item.value for item in snapshot.measured] == ["streaming"]

    with pytest.raises(ValueError, match="must be declared"):
        TransportCapabilitySnapshot(
            declared=["streaming"],
            measured=["cancel"],
        )


def test_opencode_start_retries_port_selection_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = OpenCodeServerAdapter(
        binary=_binary(tmp_path, "opencode.exe"),
        project_root=project,
        isolation_root=tmp_path / "isolation",
        evidence_root=tmp_path / "evidence",
    )
    starts = 0
    stops = 0

    async def fail_start() -> None:
        nonlocal starts
        starts += 1
        raise RuntimeError("simulated bind race")

    async def record_stop() -> None:
        nonlocal stops
        stops += 1

    monkeypatch.setattr(adapter, "_start_process_once", fail_start)
    monkeypatch.setattr(adapter, "_stop_process", record_stop)
    with pytest.raises(RuntimeError, match="3 bounded port-selection attempts"):
        asyncio.run(adapter._start_process())
    assert starts == 3
    assert stops == 3


def test_opencode_process_identity_refreshes_when_pid_changes(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adapter = OpenCodeServerAdapter(
        binary=_binary(tmp_path, "opencode.exe"),
        project_root=project,
        isolation_root=tmp_path / "isolation",
        evidence_root=tmp_path / "evidence",
    )
    adapter._process = SimpleNamespace(pid=101, returncode=None)
    initial = adapter._bind_current_process_identity()
    adapter._process = SimpleNamespace(pid=202, returncode=None)
    final = adapter._bind_current_process_identity()

    assert initial.instance_id == "opencode-101"
    assert final.instance_id == "opencode-202"
    assert adapter._identity == final
    assert adapter._process_instance_ids == ["opencode-101", "opencode-202"]


# Output path safety


def _safe_named_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    runtime_root = tmp_path / "writing-runtime"
    safe_output_base = runtime_root / ".runtime" / "kernel-harness" / "runs"
    safe_output_base.mkdir(parents=True)
    monkeypatch.setattr(transport_probe_runner, "WRITING_RUNTIME", runtime_root)
    monkeypatch.setattr(transport_probe_runner, "SAFE_OUTPUT_BASE", safe_output_base)
    return safe_output_base / "test-run"


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")


def test_adapter_output_rejects_cross_platform_directory_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    named_run = _safe_named_run(monkeypatch, tmp_path)
    named_run.mkdir()
    outside = tmp_path / "outside-adapter"
    outside.mkdir()
    adapter_link = named_run / "codex"
    _directory_symlink_or_skip(adapter_link, outside)

    try:
        with pytest.raises(ValueError, match="link, junction, or reparse point"):
            transport_probe_runner._prepare_adapter_output_roots(named_run, "codex")
        assert list(outside.iterdir()) == []
    finally:
        adapter_link.unlink(missing_ok=True)


def test_adapter_output_rejects_symlinked_named_run_ancestor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    named_run = _safe_named_run(monkeypatch, tmp_path)
    link_target = named_run.parent / "other-run"
    link_target.mkdir()
    _directory_symlink_or_skip(named_run, link_target)

    try:
        with pytest.raises(ValueError, match="link, junction, or reparse point"):
            transport_probe_runner._prepare_adapter_output_roots(named_run, "codex")
        assert list(link_target.iterdir()) == []
    finally:
        named_run.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_adapter_output_rejects_windows_junction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    named_run = _safe_named_run(monkeypatch, tmp_path)
    named_run.mkdir()
    outside = tmp_path / "outside-junction"
    outside.mkdir()
    junction = named_run / "opencode"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junctions are unavailable: {result.stderr.strip()}")

    try:
        with pytest.raises(ValueError, match="link, junction, or reparse point"):
            transport_probe_runner._prepare_adapter_output_roots(named_run, "opencode")
        assert list(outside.iterdir()) == []
    finally:
        junction.rmdir()


def test_adapter_output_creates_direct_non_reparse_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    named_run = _safe_named_run(monkeypatch, tmp_path)

    adapter_root, workspace, evidence, isolation = (
        transport_probe_runner._prepare_adapter_output_roots(named_run, "codex")
    )

    assert adapter_root.parent == named_run.resolve(strict=True)
    assert [path.name for path in (workspace, evidence, isolation)] == [
        "workspace",
        "evidence",
        "isolation",
    ]
    assert all(path.parent == adapter_root for path in (workspace, evidence, isolation))
    assert all(path.is_dir() for path in (workspace, evidence, isolation))
    assert not any(
        transport_probe_runner._is_link_or_reparse(path)
        for path in (adapter_root, workspace, evidence, isolation)
    )
