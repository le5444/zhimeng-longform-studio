from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from zhimeng_runtime.agent_models import AgentMessageResponse
from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider, _AnthropicEventDecoder
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.execution_protocol import MessageUsage


def usage(input_tokens: int = 100, output_tokens: int = 10, cached: int | None = 0, **flags: bool) -> MessageUsage:
    return MessageUsage(
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens, cached_input_tokens=cached, **flags,
    )


@pytest.mark.parametrize("raw,expected_input,expected_output", [
    ({"prompt_tokens": 100, "prompt_cache_hit_tokens": 63}, True, False),
    ({"completion_tokens": 7}, False, True),
    ({"total_tokens": 120}, False, False),
    ({"prompt_cache_hit_tokens": 63}, False, False),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "prompt_cache_hit_tokens": 0}, True, True),
])
def test_chat_usage_distinguishes_missing_counts_from_reported_zero(raw, expected_input, expected_output) -> None:
    decoded = OpenAICompatibleAgentProvider._decode_event(json.dumps({"choices": [], "usage": raw}))
    reported = decoded.usage
    assert reported is not None
    assert reported.input_tokens_reported is expected_input
    assert reported.output_tokens_reported is expected_output
    assert reported.input_tokens == raw.get("prompt_tokens", 0)
    assert reported.output_tokens == raw.get("completion_tokens", 0)
    assert reported.cached_input_tokens == raw.get("prompt_cache_hit_tokens")
    assert reported.cached_input_tokens_complete is ("prompt_cache_hit_tokens" in raw)


@pytest.mark.parametrize("raw", [None, {}, {"unrecognized_counter": 8}, {"prompt_tokens_details": {}}, {"prompt_cache_hit_tokens": None}])
def test_chat_without_any_usage_keeps_none(raw) -> None:
    assert OpenAICompatibleAgentProvider._decode_event(json.dumps({"choices": [], "usage": raw})).usage is None


@pytest.mark.parametrize("cached", [None, 0, 80])
def test_responses_and_anthropic_cache_completeness_tracks_reported_field(cached) -> None:
    raw_responses = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    raw_anthropic = {"input_tokens": 100, "output_tokens": 10}
    if cached is not None:
        raw_responses["input_tokens_details"] = {"cached_tokens": cached}
        raw_anthropic["cache_read_input_tokens"] = cached
    for reported in (
        OpenAICompatibleAgentProvider._responses_usage({"usage": raw_responses}),
        _AnthropicEventDecoder()._final_usage({"usage": raw_anthropic}),
    ):
        assert reported is not None
        assert reported.input_tokens_reported is True
        assert reported.output_tokens_reported is True
        assert reported.cached_input_tokens == cached
        assert reported.cached_input_tokens_complete is (cached is not None)


@pytest.mark.parametrize("missing_first", [True, False])
def test_partial_cache_is_preserved_without_claiming_full_rate(missing_first: bool) -> None:
    known = usage(900, 20, 630)
    missing = usage(200, 10, None)
    first, second = (missing, known) if missing_first else (known, missing)
    total = AgentService._merge_usage(first, second, previous_turns=1)
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.cached_input_tokens) == (1100, 30, 630)
    assert total.input_tokens_reported and total.output_tokens_reported
    assert total.cached_input_tokens_complete is False
    total = AgentService._merge_usage(total, usage(100, 10, 50), previous_turns=2)
    assert total is not None
    assert total.cached_input_tokens == 680
    assert total.cached_input_tokens_complete is False


def test_complete_multiturn_usage_and_explicit_zero_stay_complete() -> None:
    total = AgentService._merge_usage(None, usage(100, 0, 0))
    total = AgentService._merge_usage(total, usage(900, 20, 630), previous_turns=1)
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.total_tokens, total.cached_input_tokens) == (1000, 20, 1020, 630)
    assert total.input_tokens_reported and total.output_tokens_reported and total.cached_input_tokens_complete


@pytest.mark.parametrize("missing_first", [True, False])
def test_whole_missing_turn_keeps_known_numbers_and_marks_incomplete(missing_first: bool) -> None:
    known = usage(900, 20, 630)
    if missing_first:
        total = AgentService._merge_usage(None, None)
        assert total is None
        total = AgentService._merge_usage(total, known, previous_turns=1)
    else:
        total = AgentService._merge_usage(known, None, previous_turns=1)
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.cached_input_tokens) == (900, 20, 630)
    assert not total.input_tokens_reported and not total.output_tokens_reported and not total.cached_input_tokens_complete
    later = AgentService._merge_usage(total, usage(), previous_turns=2)
    assert later is not None
    assert not later.input_tokens_reported and not later.output_tokens_reported and not later.cached_input_tokens_complete
    assert known.input_tokens_reported and known.output_tokens_reported and known.cached_input_tokens_complete


def test_all_turns_without_usage_remain_none() -> None:
    total = None
    for previous_turns in range(3):
        total = AgentService._merge_usage(total, None, previous_turns=previous_turns)
        assert total is None


def test_partial_input_and_output_remain_partial_after_later_complete_turns() -> None:
    first = usage(0, 10, None, input_tokens_reported=False)
    second = usage(100, 0, 60, output_tokens_reported=False)
    total = AgentService._merge_usage(first, second, previous_turns=1)
    assert total is not None
    assert (total.input_tokens, total.output_tokens) == (100, 10)
    assert not total.input_tokens_reported and not total.output_tokens_reported
    total = AgentService._merge_usage(total, usage(10, 1), previous_turns=2)
    assert total is not None
    assert (total.input_tokens, total.output_tokens) == (110, 11)
    assert not total.input_tokens_reported and not total.output_tokens_reported


def test_old_usage_defaults_remain_compatible_and_new_flags_roundtrip() -> None:
    original = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    old = MessageUsage.model_validate(original)
    assert old.input_tokens_reported and old.output_tokens_reported and old.cached_input_tokens_complete
    marked = old.model_copy(update={"output_tokens_reported": False, "cached_input_tokens_complete": False})
    assert MessageUsage.model_validate_json(marked.model_dump_json()) == marked
    schema_path = Path(__file__).resolve().parents[3] / "packages/contracts/execution-protocol.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"]["messageUsage"]
    Draft202012Validator(schema).validate(original)
    Draft202012Validator(schema).validate(marked.model_dump(mode="json"))


def test_history_message_preserves_boolean_usage_metadata_in_json() -> None:
    message = AgentMessageResponse(
        message_id="message-111111111111111111111111", session_id="session-111111111111111111111111",
        run_id="run-111111111111111111111111", ordinal=1, role="assistant", format="plain",
        content="完成", created_at="2026-10-11T00:00:00Z",
        usage=usage(output_tokens=0, cached=63, output_tokens_reported=False, cached_input_tokens_complete=False).model_dump(),
    )
    payload = json.loads(message.model_dump_json(by_alias=True))["usage"]
    assert payload["input_tokens_reported"] is True
    assert payload["output_tokens_reported"] is False
    assert payload["cached_input_tokens_complete"] is False
    assert payload["cached_input_tokens"] == 63


def test_saved_usage_flags_survive_message_api_reload(settings) -> None:
    from fastapi.testclient import TestClient
    from test_agent_api import ApiProvider
    from zhimeng_runtime.app import create_app
    from zhimeng_runtime.execution_protocol import RunStatus

    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = client.post('/api/v1/agent/sessions', json={'title': '缓存计数'}).json()['session_id']
        store = client.app.state.agent_service.store
        run, _, _ = store.create_run(
            session_id=sid, message='检查计数', expected_project_revision=None,
            expected_project_fingerprint=None, active_chapter_id=None, idempotency_key='usage-metadata-reload',
        )
        store.transition_run(run.run_id, RunStatus.STARTING, detail='fixture preparing')
        store.transition_run(run.run_id, RunStatus.RUNNING, detail='fixture running')
        store.complete_run(
            run.run_id, message_id='message-222222222222222222222222', content='完成', finish_reason='stop',
            usage=usage(output_tokens=0, cached=63, output_tokens_reported=False, cached_input_tokens_complete=False),
        )
        for _ in range(2):
            response = client.get(f'/api/v1/agent/sessions/{sid}/messages')
            assert response.status_code == 200, response.text
            reported = next(item for item in response.json()['messages'] if item['role'] == 'assistant')['usage']
            assert reported['input_tokens_reported'] is True
            assert reported['output_tokens_reported'] is False
            assert reported['cached_input_tokens_complete'] is False
            assert reported['cached_input_tokens'] == 63
