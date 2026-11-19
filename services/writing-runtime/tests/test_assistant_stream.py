from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from test_agent_api import ApiProvider
from test_agent_queue import session
from zhimeng_runtime.agent_provider import AgentProviderError, ProviderCompleted, ProviderReasoningDelta, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app
from zhimeng_runtime.assistant_stream import AssistantStream
from zhimeng_runtime.reasoning_stream import ReasoningStream


class PausedTextProvider(ApiProvider):
    text = 'Report: ' + 'x' * 500

    def __init__(self, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        yield ProviderTextDelta('Report: ')
        for _ in range(500):
            yield ProviderTextDelta('x')
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.fail:
            raise AgentProviderError('broken', code='fixture_stream_failed', retryable=False)
        yield ProviderCompleted('stop', None)


def start(client, sid):
    result = client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key': 'assistant-stream-001'}, json={'message': 'Return the prepared report.'})
    assert result.status_code == 202, result.text
    return result.json()['run']['run_id']


def activities(client, rid):
    response = client.get(f'/api/v1/agent/runs/{rid}/activities')
    assert response.status_code == 200, response.text
    return response.json()['activities']


def deltas(client, rid):
    return [item['payload']['delta'] for item in activities(client, rid)
            if item['kind'] == 'message' and item['payload']['phase'] == 'delta']


def terminal(client, rid):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = client.get(f'/api/v1/agent/runs/{rid}').json()['run']
        if run['status'] in {'succeeded', 'cancelled', 'failed', 'interrupted'}:
            return run
        time.sleep(0.02)
    raise AssertionError('Run did not finish')


@pytest.mark.parametrize('end', ['complete', 'cancel', 'fail', 'shutdown'])
def test_pending_text_is_flushed_at_every_terminal_boundary(settings, monkeypatch, end):
    monkeypatch.setattr(AssistantStream, 'flush_seconds', 60)
    provider = PausedTextProvider(fail=end == 'fail')
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        rid = start(client, session(client))
        assert provider.entered.wait(15)
        assert deltas(client, rid) == ['Report: ']
        if end == 'cancel':
            assert client.post(f'/api/v1/agent/runs/{rid}/cancel').status_code == 200
        elif end in {'complete', 'fail'}:
            provider.release.set()
        if end != 'shutdown':
            assert terminal(client, rid)['status'] == {'complete': 'succeeded', 'cancel': 'cancelled', 'fail': 'failed'}[end]
            assert ''.join(deltas(client, rid)) == provider.text
            assert len(deltas(client, rid)) == 2
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        assert ''.join(deltas(restarted, rid)) == provider.text
        assert len(deltas(restarted, rid)) == 2
        assert terminal(restarted, rid)['status'] == {'complete': 'succeeded', 'cancel': 'cancelled', 'fail': 'failed', 'shutdown': 'interrupted'}[end]


def test_quiet_tail_is_published_while_provider_is_still_waiting(settings):
    provider = PausedTextProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        rid = start(client, session(client))
        assert provider.entered.wait(15)
        deadline = time.monotonic() + 10
        while ''.join(deltas(client, rid)) != provider.text and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ''.join(deltas(client, rid)) == provider.text
        assert len(deltas(client, rid)) < 20
        assert client.get(f'/api/v1/agent/runs/{rid}').json()['run']['status'] == 'running'
        provider.release.set()
        assert terminal(client, rid)['status'] == 'succeeded'


def test_text_is_durable_before_reasoning_and_tool_execution(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(AssistantStream, 'flush_seconds', 60)
    document = tmp_path / 'report.txt'
    document.write_bytes(b'Units: 42\n')

    class Provider(ApiProvider):
        calls = 0

        async def stream_chat(self, messages, *, cancel_event, tools=()):
            self.calls += 1
            if self.calls == 1:
                yield ProviderTextDelta('Read ')
                yield ProviderTextDelta('the report.')
                yield ProviderReasoningDelta('Check units.')
                yield ProviderToolCall(0, 'read-report', 'read_file', {'path': str(document)})
                yield ProviderCompleted('tool_calls', None)
            else:
                yield ProviderTextDelta('Units: ')
                yield ProviderTextDelta('42')
                yield ProviderCompleted('stop', None)

    with TestClient(create_app(settings, agent_provider=Provider())) as client:
        sid = session(client)
        rid = start(client, sid)
        assert terminal(client, rid)['status'] == 'succeeded'
        items = activities(client, rid)
        before_reasoning = next(item for item in items if item['kind'] == 'message' and item['payload'].get('delta') == 'the report.')
        reasoning = next(item for item in items if item['kind'] == 'reasoning')
        tool = next(item for item in items if item['kind'] == 'tool_call')
        assert before_reasoning['sequence'] < reasoning['sequence'] < tool['sequence']
        messages = client.get(f'/api/v1/agent/sessions/{sid}/messages').json()['messages']
        assert [message['content'] for message in messages if message['role'] == 'assistant'] == ['Read the report.', 'Units: 42']


def test_cancel_waits_for_inflight_write_without_replaying_text():
    class Store:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.saved = []

        def append_assistant_delta(self, rid, **payload):
            self.entered.set()
            assert self.release.wait(10)
            self.saved.append(payload['delta'])

    class Broker:
        async def publish(self, rid):
            pass

    async def scenario():
        store = Store()
        stream = AssistantStream(store, Broker(), 'run-text', 'message-text')
        sending = asyncio.create_task(stream.add('Already received.'))
        assert await asyncio.to_thread(store.entered.wait, 10)
        sending.cancel()
        finishing = asyncio.create_task(stream.finish_preserving_cancellation())
        await asyncio.sleep(0.01)
        assert not finishing.done()
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await finishing
        assert store.saved == ['Already received.']
        assert stream.closed

    asyncio.run(scenario())


def test_timer_write_error_is_observed_and_is_not_retried():
    class Store:
        calls = 0

        def append_assistant_delta(self, rid, **payload):
            self.calls += 1
            if self.calls == 2:
                raise OSError('storage unavailable')

    class Broker:
        async def publish(self, rid):
            pass

    async def scenario():
        store = Store()
        stream = AssistantStream(store, Broker(), 'run-text', 'message-text')
        stream.flush_seconds = 0
        await stream.add('First.')
        await stream.add('Tail.')
        with pytest.raises(OSError, match='storage unavailable'):
            await stream.finish_preserving_cancellation()
        assert store.calls == 2

    asyncio.run(scenario())


@pytest.mark.parametrize('stream_type', [AssistantStream, ReasoningStream])
def test_text_arriving_during_timer_write_is_flushed_without_another_event(stream_type):
    class Store:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.saved = []

        def append_assistant_delta(self, rid, **payload):
            if not payload.get('delta'):
                return
            if len(self.saved) == 1:
                self.entered.set()
                assert self.release.wait(10)
            self.saved.append(payload['delta'])

        append_reasoning = append_assistant_delta

    class Broker:
        def __init__(self):
            self.published = 0
            self.tail_published = asyncio.Event()

        async def publish(self, rid):
            self.published += 1
            if self.published == 3:
                self.tail_published.set()

    async def scenario():
        store, broker = Store(), Broker()
        stream = (stream_type(store, broker, 'run-text', 'message-text')
                  if stream_type is AssistantStream else stream_type(store, broker, 'run-text'))
        stream.flush_seconds = 0.01
        await stream.add('First.')
        await stream.add('Second.')
        assert await asyncio.to_thread(store.entered.wait, 10)
        try:
            await stream.add('Quiet tail.')
        finally:
            store.release.set()
        await asyncio.wait_for(broker.tail_published.wait(), 2)
        assert store.saved == ['First.', 'Second.', 'Quiet tail.']
        await stream.finish()

    asyncio.run(scenario())
