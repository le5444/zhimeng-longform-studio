from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from test_agent_api import ApiProvider
from zhimeng_runtime.app import create_app
from zhimeng_runtime.execution_protocol import MessageUsage, RunStatus
from zhimeng_runtime.run_usage import read_run_usage, summarize_usage


def counter(input_tokens=100, output_tokens=10, cached=50, **flags):
    return MessageUsage(input_tokens=input_tokens, output_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens, cached_input_tokens=cached, **flags).model_dump()


def events(prefix, turn, value, part=None):
    identity = {'turn': turn, **({'part': part} if part is not None else {})}
    return [(prefix + '_started', identity), (prefix + '_completed', {**identity, 'usage': value})]


def test_totals_include_each_compaction_part_and_main_turn_once():
    source = events('model_turn', 1, counter(1000, 100, 500))
    source += events('run_context_summary', 2, counter(200, 20, 100), 1)
    source += events('run_context_summary', 2, counter(300, 30, 0), 2)
    source += events('model_turn', 2, counter(500, 50, 100))
    source.append(source[-1])  # Repeated receipt is not another billed turn.
    result = summarize_usage(source)
    assert result.total == MessageUsage(**counter(2000, 200, 700))
    assert result.main.requests == result.main.completed == 2
    assert result.compaction.requests == result.compaction.completed == 2
    assert result.main.usage.input_tokens == 1500
    assert result.compaction.usage.input_tokens == 500


@pytest.mark.parametrize('missing_first', [True, False])
@pytest.mark.parametrize('missing', ['unfinished', 'no_usage', 'partial'])
def test_failed_or_partial_summary_keeps_known_counts_without_false_completeness(missing_first, missing):
    unknown = [('run_context_summary_started', {'turn': 2, 'part': 1})]
    if missing != 'unfinished':
        unknown.append(('run_context_summary_completed', {'turn': 2, 'part': 1, 'usage':
            counter(20, 0, None, output_tokens_reported=False) if missing == 'partial' else None}))
    known = events('model_turn', 1, counter())
    result = summarize_usage(unknown + known if missing_first else known + unknown)
    assert result.total.input_tokens == (120 if missing == 'partial' else 100)
    assert result.total.output_tokens == 10
    assert result.total.cached_input_tokens == 50
    assert result.total.input_tokens_reported is (missing == 'partial')
    assert not result.total.output_tokens_reported
    assert not result.total.cached_input_tokens_complete
    assert result.compaction.completed == (0 if missing == 'unfinished' else 1)


def test_no_usage_is_unknown_and_reported_zero_is_preserved():
    assert summarize_usage(events('model_turn', 1, None)).total is None
    zero = summarize_usage(events('model_turn', 1, counter(0, 0, 0)))
    assert zero.total.input_tokens == zero.total.output_tokens == zero.total.cached_input_tokens == 0
    assert zero.total.input_tokens_reported and zero.total.cached_input_tokens_complete
    assert zero.compaction.requests == 0 and zero.compaction.usage is None


@pytest.mark.parametrize('terminal', ['succeeded', 'interrupted'])
def test_history_api_reload_paging_session_scope_and_original_usage(settings, terminal):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid = client.post('/api/v1/agent/sessions', json={'title': '用量来源核对'}).json()['session_id']
        other = client.post('/api/v1/agent/sessions', json={'title': '其他会话'}).json()['session_id']
        store = client.app.state.agent_service.store
        run, _, _ = store.create_run(session_id=sid, message='核对采购表', expected_project_revision=None,
            expected_project_fingerprint=None, active_chapter_id=None, idempotency_key='usage-roundtrip')
        store.transition_run(run.run_id, RunStatus.STARTING, detail='fixture')
        store.transition_run(run.run_id, RunStatus.RUNNING, detail='fixture')
        for kind, payload in events('model_turn', 1, counter()) + events('run_context_summary', 2, counter(20, 2, 0), 1):
            store.append_run_event(run.run_id, kind, payload)
        if terminal == 'succeeded':
            store.complete_run(run.run_id, message_id='message-222222222222222222222222', content='完成',
                               finish_reason='stop', usage=MessageUsage(**counter()))
        else:
            store.append_run_event(run.run_id, 'run_context_summary_started', {'turn': 2, 'part': 2})
            store.mark_interrupted(run.run_id, detail='fixture interruption')
        schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/agent-message-list.schema.json').read_text(encoding='utf-8'))
        for query in ('', '?latest=true&limit=1', '?before_ordinal=2&limit=1'):
            response = client.get(f'/api/v1/agent/sessions/{sid}/messages{query}')
            assert response.status_code == 200, response.text
            body = response.json()
            Draft202012Validator(schema).validate(body)
            for message in body['messages']:
                total = message['run_usage']['total']
                assert (total['input_tokens'], total['output_tokens'], total['cached_input_tokens']) == (120, 12, 50)
                assert total['input_tokens_reported'] is (terminal == 'succeeded')
                if message['usage']:
                    assert message['usage']['input_tokens'] == 100  # No cumulative double count or historical rewrite.
        assert read_run_usage(store.database, other, [run.run_id]) == {}
        assert client.get(f'/api/v1/agent/sessions/{other}/messages').json()['messages'] == []
        assert client.get('/api/v1/agent/sessions/session-missing/messages').status_code == 404


def test_exported_message_contract_includes_usage_models():
    from zhimeng_runtime.agent_models import AgentMessageListResponse
    schema = json.loads((Path(__file__).resolve().parents[3] / 'packages/contracts/agent-message-list.schema.json').read_text(encoding='utf-8'))
    schema.pop('$id')
    schema.pop('$schema')
    assert schema == AgentMessageListResponse.model_json_schema(by_alias=True)
