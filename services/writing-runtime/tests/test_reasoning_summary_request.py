from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from test_agent_provider_responses import ChunkStream, completed, run_stream, sse
from zhimeng_runtime.agent_provider import AgentProviderError, OpenAICompatibleAgentProvider, ProviderCompleted


@pytest.mark.parametrize(('effort', 'rejection', 'expected'), [
    ('auto', None, [{'summary': 'auto'}]),
    ('high', "Unknown parameter: 'reasoning.summary'", [{'summary': 'auto', 'effort': 'high'}, {'effort': 'high'}]),
    ('auto', "Unsupported parameter: 'reasoning.summary'", [{'summary': 'auto'}, None]),
    ('high', "Unsupported parameter: 'reasoning.effort'", [{'summary': 'auto', 'effort': 'high'}, {'summary': 'auto'}]),
    ('high', "Unsupported parameter: 'reasoning'", [{'summary': 'auto', 'effort': 'high'}, None]),
])
def test_summary_is_requested_independently_and_narrow_fallback_preserves_other_fields(effort, rejection, expected):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if rejection and len(requests) == 1:
            return httpx.Response(400, json={'error': {'message': rejection}})
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=ChunkStream(sse(
            {'type': 'response.output_text.delta', 'delta': 'Document checked.'},
            completed([{'type': 'message', 'id': 'message-1'}]),
        )))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(base_url='https://provider.test/v1', model='test-model',
        api_key='test-key', credential_reference='fixture-key', timeout_seconds=10, max_tokens=1024,
        temperature=.2, reasoning_effort=effort, wire_format='responses', client=client)
    try:
        events = run_stream(provider, [{'role': 'user', 'content': 'Check this document.'}])
    finally:
        asyncio.run(client.aclose())
    assert [body.get('reasoning') for body in requests] == expected
    assert all(body['input'] == requests[0]['input'] for body in requests)
    last = next(event for event in events if isinstance(event, ProviderCompleted))
    assert last.effective_reasoning_effort == (expected[-1] or {}).get('effort')


@pytest.mark.parametrize(('status', 'message'), [
    (403, 'reasoning.summary requires authorization'),
    (400, 'Invalid schema: tools[0].parameters.summary'),
])
def test_other_failures_are_not_silently_retried_without_the_summary(status, message):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': message}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(base_url='https://provider.test/v1', model='test-model',
        api_key='test-key', credential_reference='fixture-key', timeout_seconds=10, max_tokens=1024,
        temperature=.2, reasoning_effort='high', wire_format='responses', client=client)
    try:
        with pytest.raises(AgentProviderError):
            run_stream(provider, [{'role': 'user', 'content': 'Check the file.'}])
    finally:
        asyncio.run(client.aclose())
    assert len(calls) == 1
