from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from zhimeng_runtime.agent_provider import AgentProviderError, OpenAICompatibleAgentProvider


IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jJ1sAAAAASUVORK5CYII='


@pytest.mark.parametrize('wire', ['chat', 'responses', 'anthropic_messages'])
def test_images_reach_actual_wire_payload_with_text_and_tool_history(wire):
    captured = []
    messages = [
        {'role': 'system', 'content': 'Workspace facts.'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'What is in this image?'}, {'type': 'image_url', 'image_url': {'url': IMAGE, 'detail': 'auto'}}]},
        {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"notes.txt"}'}}]},
        {'role': 'tool', 'tool_call_id': 'call1', 'content': 'reference text'},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': IMAGE}}]},
    ]
    original = copy.deepcopy(messages)

    async def handler(request):
        captured.append(json.loads(request.content))
        if wire == 'responses':
            events = [{'type': 'response.output_text.delta', 'delta': 'Received image.'}, {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}]
        elif wire == 'anthropic_messages':
            events = [{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Received image.'}}, {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}}, {'type': 'message_stop'}]
        else:
            events = [{'choices': [{'index': 0, 'delta': {'content': 'Received image.'}, 'finish_reason': None}]}, {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
        data = ''.join(f'data: {json.dumps(event)}\n\n' for event in events)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=data.encode())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleAgentProvider(base_url='https://images.test/v1', model='image-model', api_key='synthetic', credential_reference='synthetic',
                timeout_seconds=5, max_tokens=1024, temperature=0.2, reasoning_effort='auto', wire_format=wire, client=client)
            return [event async for event in provider.stream_chat(messages, cancel_event=asyncio.Event())]

    assert asyncio.run(run())
    assert messages == original
    assert len(captured) == 1
    payload = captured[0]
    if wire == 'chat':
        assert payload['messages'] == original
    elif wire == 'responses':
        assert payload['instructions'] == 'Workspace facts.'
        assert payload['input'][0]['content'][1] == {'type': 'input_image', 'image_url': IMAGE, 'detail': 'auto'}
        assert payload['input'][1]['type'] == 'function_call'
        assert payload['input'][2] == {'type': 'function_call_output', 'call_id': 'call1', 'output': 'reference text'}
        assert payload['input'][3]['content'][0]['image_url'] == IMAGE
    else:
        content = payload['messages'][0]['content']
        assert content[0] == {'type': 'text', 'text': 'What is in this image?'}
        assert content[1] == {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': IMAGE.split(',', 1)[1]}}
        assert payload['messages'][1]['content'][0]['type'] == 'tool_use'
        assert payload['messages'][2]['content'][0]['tool_use_id'] == 'call1'
        assert payload['messages'][3]['content'][0]['type'] == 'image'


def test_unhydrated_image_reference_does_not_silently_become_empty_text():
    with pytest.raises(AgentProviderError, match='图片消息尚未准备好'):
        OpenAICompatibleAgentProvider._build_responses_input([{'role': 'user', 'content': [{'type': 'image_ref', 'image_id': 'image-test'}]}])
