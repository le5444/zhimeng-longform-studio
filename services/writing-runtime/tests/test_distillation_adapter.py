from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from zhimeng_runtime.distillation_adapter import (
    COVERAGE_TOP,
    DistillationBusyError,
    DistillationLibraryChangedError,
    DistillationLibraryInvalidError,
    DistillationOutputError,
    DistillationToolExecutionError,
    DistillationToolTimeoutError,
    SubprocessDistillationAdapter,
)
from zhimeng_runtime.distillation_models import DistillationCoverageRequest


def _health_payload() -> dict:
    return {
        "health": "degraded",
        "structural_health": "healthy",
        "validation_maturity": "risk_ready",
        "cards": 7,
        "counts": {"work": 4, "scene": 3},
        "validation_ladder_debt": {"risk_ready_cards": 3, "warn_steps": 12},
        "hard_rule_eligible_counts": {"work": 4},
        "bad_jsonl_quarantine": "C:/must/not/leak",
    }


def _coverage_payload(*, usage_mode: str = "risk_ready") -> dict:
    return {
        "query": "番茄 男频 玄幻 开篇",
        "profile": "draft_assist",
        "target_health": "degraded",
        "writing_gate": "validate_existing_cards_or_risk_draft",
        "recommended_next_step": "validate_existing_cards_or_run_risk_context",
        "source_coverage_ratio": 1.0,
        "formal_coverage_ratio": 0.4,
        "matched_cards": 4,
        "available_matched_cards": 7,
        "selected_type_counts": {"scene": 1},
        "selected_hard_rule_type_counts": {},
        "selected_risk_ready_type_counts": {"scene": 1},
        "source_quota_gaps": {"scene": 0},
        "formal_quota_gaps": {"scene": 1},
        "missing_types": [],
        "formal_missing_types": ["scene"],
        "feed_request": "先验证现有卡。",
        "library_health": {
            "health": "degraded",
            "structural_health": "healthy",
            "validation_maturity": "risk_ready",
        },
        "selected_cards": [
            {
                "card_id": "scene-fixture",
                "card_type": "scene",
                "title": "开篇压力场",
                "summary": "只返回机制摘要。",
                "usage_mode": usage_mode,
                "hard_rule_eligible": False,
                "safety_risk": 0.3,
                "tags": ["番茄", "开篇"],
                "source_forbidden_items": ["must not leak"],
                "type_payload": {"scene_shell_signature": "must not leak"},
                "audit_only": {"scene_shell_signature": "must not leak"},
                "reader_promise": "must not leak",
            }
        ],
    }


def _adapter(tmp_path: Path) -> tuple[SubprocessDistillationAdapter, Path, Path]:
    skill_root = tmp_path / "skill"
    script = skill_root / "scripts" / "distill_kb.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fixture\n", encoding="utf-8")
    library = tmp_path / "library"
    library.mkdir()
    (library / "manifest.json").write_text("{}\n", encoding="utf-8")
    (library / "cards.jsonl").write_text("{}\n", encoding="utf-8")
    (library / "feedback.jsonl").write_text("", encoding="utf-8")
    return (
        SubprocessDistillationAdapter(
            skill_root=skill_root,
            library=library,
            python_executable=Path(sys.executable),
            timeout_seconds=30,
        ),
        library,
        script,
    )


def _completed(payload: dict) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stderr=b"",
    )


def test_health_runs_only_the_fixed_read_only_command_and_preserves_risk_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, script = _adapter(tmp_path)
    captured: dict = {}
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in (library / "manifest.json", library / "cards.jsonl", library / "feedback.jsonl")
    }

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return _completed(_health_payload())

    monkeypatch.setattr(subprocess, "run", fake_run)
    response = adapter.health()

    assert response.health == "degraded"
    assert response.validation_maturity == "risk_ready"
    assert len(response.library_fingerprint) == 64
    assert len(response.tool_fingerprint) == 64
    assert captured["arguments"] == [
        str(Path(sys.executable).resolve()),
        str(script.resolve()),
        "health",
        str(library.resolve()),
        "--json",
    ]
    assert captured["kwargs"]["shell"] is False
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
        for path in (library / "manifest.json", library / "cards.jsonl", library / "feedback.jsonl")
    } == before


def test_coverage_uses_fixed_top_and_emits_only_compact_card_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, script = _adapter(tmp_path)
    captured: list[str] = []

    def fake_run(arguments, **kwargs):
        del kwargs
        captured.extend(arguments)
        return _completed(_coverage_payload())

    monkeypatch.setattr(subprocess, "run", fake_run)
    request = DistillationCoverageRequest(
        query="番茄 男频 玄幻 开篇",
        profile="draft_assist",
    )
    response = adapter.coverage(request)
    payload = response.model_dump(mode="json", by_alias=True)

    assert captured == [
        str(Path(sys.executable).resolve()),
        str(script.resolve()),
        "coverage",
        str(library.resolve()),
        request.query,
        "--profile",
        request.profile,
        "--top",
        str(COVERAGE_TOP),
        "--json",
    ]
    assert response.risk_preview_allowed is True
    assert response.formal_injection_allowed is False
    assert set(payload["selected_cards"][0]) == {
        "card_id",
        "card_type",
        "title",
        "summary",
        "usage_mode",
        "hard_rule_eligible",
        "safety_risk",
        "tags",
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "must not leak" not in serialized
    assert str(library) not in serialized


def test_unknown_usage_mode_is_invalid_tool_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(_coverage_payload(usage_mode="soft")),
    )

    with pytest.raises(DistillationOutputError):
        adapter.coverage(
            DistillationCoverageRequest(
                query="番茄 男频 玄幻 开篇",
                profile="draft_assist",
            )
        )


def test_library_mutation_during_command_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, _ = _adapter(tmp_path)

    def mutate_library(*args, **kwargs):
        del args, kwargs
        (library / "feedback.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
        return _completed(_health_payload())

    monkeypatch.setattr(subprocess, "run", mutate_library)

    with pytest.raises(DistillationLibraryChangedError) as caught:
        adapter.health()
    assert caught.value.code == "distillation_library_changed"


def test_timeout_has_stable_error_after_read_only_evidence_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path)

    def time_out(*args, **kwargs):
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="fixture", timeout=30)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(DistillationToolTimeoutError) as caught:
        adapter.health()
    assert caught.value.code == "distillation_tool_timeout"


def test_missing_library_state_file_has_stable_error(
    tmp_path: Path,
) -> None:
    adapter, library, _ = _adapter(tmp_path)
    (library / "feedback.jsonl").unlink()

    with pytest.raises(DistillationLibraryInvalidError) as caught:
        adapter.health()
    assert caught.value.code == "distillation_library_invalid"


def test_nonzero_exit_and_invalid_json_have_stable_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=7,
            stdout=b"",
            stderr=b"private path must not escape",
        ),
    )
    with pytest.raises(DistillationToolExecutionError) as failed:
        adapter.health()
    assert failed.value.code == "distillation_tool_failed"
    assert str(failed.value) == "Distillation preview command failed"

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=b"not-json",
            stderr=b"",
        ),
    )
    with pytest.raises(DistillationOutputError) as invalid:
        adapter.health()
    assert invalid.value.code == "distillation_output_invalid"


def test_adapter_rejects_concurrent_preview_with_stable_busy_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path)
    started = threading.Event()
    release = threading.Event()
    thread_errors: list[BaseException] = []

    def blocking_run(*args, **kwargs):
        del args, kwargs
        started.set()
        assert release.wait(3)
        return _completed(_health_payload())

    def invoke_health() -> None:
        try:
            adapter.health()
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            thread_errors.append(exc)

    monkeypatch.setattr(subprocess, "run", blocking_run)
    thread = threading.Thread(target=invoke_health)
    thread.start()
    assert started.wait(2)
    try:
        with pytest.raises(DistillationBusyError) as caught:
            adapter.health()
        assert caught.value.code == "distillation_preview_busy"
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert thread_errors == []


def test_health_reuses_current_cache_while_coverage_holds_the_singleflight_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, _, _ = _adapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(_health_payload()),
    )
    cached = adapter.health()
    started = threading.Event()
    release = threading.Event()
    thread_errors: list[BaseException] = []

    def blocking_coverage(*args, **kwargs):
        del args, kwargs
        started.set()
        assert release.wait(3)
        return _completed(_coverage_payload())

    def invoke_coverage() -> None:
        try:
            adapter.coverage(
                DistillationCoverageRequest(
                    query="番茄 男频 玄幻 开篇",
                    profile="draft_assist",
                )
            )
        except BaseException as exc:  # pragma: no cover - diagnostic capture
            thread_errors.append(exc)

    monkeypatch.setattr(subprocess, "run", blocking_coverage)
    thread = threading.Thread(target=invoke_coverage)
    thread.start()
    assert started.wait(2)
    try:
        while_busy = adapter.health()
        assert while_busy.library_fingerprint == cached.library_fingerprint
        assert while_busy.tool_fingerprint == cached.tool_fingerprint
        assert while_busy.observed_at == cached.observed_at
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert thread_errors == []


def test_busy_health_never_reuses_cache_after_library_identity_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, _ = _adapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: _completed(_health_payload()),
    )
    adapter.health()
    (library / "feedback.jsonl").write_text('{"changed":true}\n', encoding="utf-8")

    assert adapter._invocation_lock.acquire(blocking=False)
    try:
        with pytest.raises(DistillationBusyError):
            adapter.health()
    finally:
        adapter._invocation_lock.release()


def test_knowledge_fingerprint_observation_is_stable_and_does_not_run_tool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, _ = _adapter(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("fingerprint observation must not run a tool"),
    )

    first = adapter.observe_knowledge_fingerprint()
    second = adapter.observe_knowledge_fingerprint()
    (library / "feedback.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
    changed = adapter.observe_knowledge_fingerprint()

    assert len(first) == 64
    assert second == first
    assert changed != first


def test_knowledge_fingerprint_rejects_library_change_during_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, library, _ = _adapter(tmp_path)
    original_after = adapter._snapshot_library_after_invocation

    def changed_after_snapshot():
        (library / "cards.jsonl").write_text('{"changed":true}\n', encoding="utf-8")
        return original_after()

    monkeypatch.setattr(
        adapter,
        "_snapshot_library_after_invocation",
        changed_after_snapshot,
    )

    with pytest.raises(DistillationLibraryChangedError):
        adapter.observe_knowledge_fingerprint()


@pytest.mark.parametrize("query", ["--json", "-x", "\x00unsafe"])
def test_coverage_request_rejects_cli_option_and_control_queries(query: str) -> None:
    with pytest.raises(ValidationError):
        DistillationCoverageRequest(query=query, profile="chapter")
