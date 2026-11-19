from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from test_agent_provider_resilience import StalledStream, collect, make_provider
from zhimeng_runtime.agent_provider import AgentProviderError


@pytest.mark.parametrize(('exception', 'kind'), [
    (httpx.ConnectTimeout, 'connect'), (httpx.ReadTimeout, 'read'),
    (httpx.WriteTimeout, 'write'), (httpx.PoolTimeout, 'pool'),
])
def test_timeout_details_classify_network_stage_without_request_secrets(exception, kind):
    async def handler(request):
        raise exception('private credentials in exception message', request=request)
    provider = make_provider(handler)
    try:
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.code == 'agent_provider_timeout'
        info = caught.value.details
        assert info['timeout_kind'] == kind
        assert info['request_phase'] == 'awaiting_response_headers'
        assert info['response_headers_received'] is False
        assert info['response_status'] is None
        assert info['stream_lines_received'] == 0
        assert info['request_attempt'] == 1
        assert info['elapsed_seconds'] >= 0
        assert 'private credentials' not in str(caught.value)
        assert 'sk-live' not in json.dumps(info)
        assert 'provider.test' not in json.dumps(info)
    finally:
        asyncio.run(provider._client.aclose())


class TimedOutBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b': heartbeat\n\n'
        raise httpx.ReadTimeout('body timed out')


@pytest.mark.parametrize(('status', 'phase', 'lines'), [(200, 'reading_stream', 2), (503, 'reading_error_response', 0)])
def test_received_headers_and_body_timeout_are_recorded_separately(status, phase, lines):
    async def handler(request):
        return httpx.Response(status, headers={'content-type': 'text/event-stream'}, stream=TimedOutBody())
    provider = make_provider(handler, max_retries=0)
    try:
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.details['timeout_kind'] == 'read'
        assert caught.value.details['request_phase'] == phase
        assert caught.value.details['response_headers_received'] is True
        assert caught.value.details['response_status'] == status
        assert caught.value.details['stream_lines_received'] == lines
    finally:
        asyncio.run(provider._client.aclose())


def test_compatibility_retry_does_not_reuse_previous_attempt_headers():
    attempts = []
    async def handler(request):
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(400, json={'error': {'message': 'unsupported temperature'}})
        raise httpx.ReadTimeout('no headers', request=request)
    provider = make_provider(handler)
    try:
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.details['request_attempt'] == 2
        assert caught.value.details['response_headers_received'] is False
        assert caught.value.details['response_status'] is None
    finally:
        asyncio.run(provider._client.aclose())


def test_stream_idle_diagnostic_retains_idle_setting_and_actual_phase():
    async def handler(request):
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=StalledStream([]))
    provider = make_provider(handler, stream_idle_timeout_seconds=.02)
    try:
        with pytest.raises(AgentProviderError) as caught:
            asyncio.run(collect(provider))
        assert caught.value.code == 'agent_provider_stream_idle_timeout'
        assert caught.value.details['timeout_kind'] == 'stream_idle'
        assert caught.value.details['request_phase'] == 'reading_stream'
        assert caught.value.details['response_headers_received'] is True
    finally:
        asyncio.run(provider._client.aclose())
