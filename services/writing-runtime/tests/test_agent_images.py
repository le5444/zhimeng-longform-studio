from __future__ import annotations

import asyncio
import base64
import copy
import json
import threading
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_agent_api import ApiProvider, wait_for_success
from zhimeng_runtime.app import create_app
from zhimeng_runtime.database import SqliteStore, SCHEMA_VERSION


def image_bytes(format='PNG'):
    buffer = BytesIO()
    Image.new('RGB', (7, 5), color=(35, 80, 180)).save(buffer, format=format)
    return buffer.getvalue()


def upload(client, format='PNG'):
    data = image_bytes(format)
    response = client.post('/api/v1/agent/images', json={'name': f'sample.{format.lower()}', 'data_url': 'data:image/anything;base64,' + base64.b64encode(data).decode()})
    assert response.status_code == 201, response.text
    return response.json(), data


def session(client):
    response = client.post('/api/v1/agent/sessions', json={'title': '图片对话'})
    assert response.status_code == 201, response.text
    return response.json()['session_id']


def start(client, sid, images, key='image-request-0001', message=''):
    return client.post(f'/api/v1/agent/sessions/{sid}/runs', headers={'Idempotency-Key': key}, json={'message': message, 'image_ids': images})


class VisionProvider(ApiProvider):
    def __init__(self, blocked=False):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.blocked = blocked

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        from zhimeng_runtime.agent_provider import ProviderTextDelta, ProviderCompleted
        self.calls.append(copy.deepcopy(messages))
        self.entered.set()
        while self.blocked and len(self.calls) == 1 and not self.release.is_set():
            if cancel_event.is_set():
                return
            await asyncio.sleep(0.01)
        yield ProviderTextDelta('已收到图片内容。')
        yield ProviderCompleted('stop', None)


def images_in(messages):
    return [part for message in messages if isinstance(message.get('content'), list)
            for part in message['content'] if part.get('type') == 'image_url']


@pytest.mark.parametrize(('format','mime'), [('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp'),('GIF','image/gif')])
def test_upload_validates_actual_format_and_serves_exact_bytes(settings, format, mime):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        attachment, original = upload(client, format)
        assert attachment['mime_type'] == mime
        assert (attachment['width'], attachment['height']) == (7,5)
        response = client.get(attachment['url'])
        assert response.status_code == 200
        assert response.content == original
        assert response.headers['content-type'] == mime
        assert response.headers['x-content-type-options'] == 'nosniff'


@pytest.mark.parametrize('raw', [b'not an image', b'\x89PNG\r\n\x1a\n', b'GIF89a', b'<svg/>'])
def test_upload_rejects_fake_or_truncated_bytes(settings, raw):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        response = client.post('/api/v1/agent/images', json={'name':'fake.png','data_url':'data:image/png;base64,'+base64.b64encode(raw).decode()})
        assert response.status_code == 422, response.text


def test_image_only_run_history_restart_and_idempotency(settings):
    provider = VisionProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        attachment, raw = upload(client)
        created = start(client, sid, [attachment['image_id']])
        assert created.status_code == 202, created.text
        receipt = created.json()
        rid = receipt['run']['run_id']
        assert receipt['user_message']['content'] == '[图片]'
        assert receipt['user_message']['images'][0]['image_id'] == attachment['image_id']
        assert wait_for_success(client, rid)['status'] == 'succeeded'
        assert len(images_in(provider.calls[0])) == 1
        assert images_in(provider.calls[0])[0]['image_url']['url'].endswith(base64.b64encode(raw).decode())
        replay = start(client, sid, [attachment['image_id']])
        assert replay.status_code == 202 and replay.json()['replayed']
        another, _ = upload(client)
        assert start(client, sid, [another['image_id']]).status_code == 409
        with client.app.state.agent_service.store.database.connection() as connection:
            transcript = connection.execute('SELECT messages_json FROM run_transcript WHERE run_id=?',(rid,)).fetchone()[0]
            assert 'data:image' not in transcript and 'base64' not in transcript
        url = receipt['user_message']['images'][0]['url']
    restarted_provider = VisionProvider()
    with TestClient(create_app(settings, agent_provider=restarted_provider)) as restarted:
        page = restarted.get(f'/api/v1/agent/sessions/{sid}/messages').json()
        assert page['messages'][0]['images'][0]['image_id'] == attachment['image_id']
        assert restarted.get(url).content == raw
        second = start(restarted, sid, [], key='image-next-00002', message='继续看前面的图片')
        assert second.status_code == 202, second.text
        assert wait_for_success(restarted, second.json()['run']['run_id'])['status'] == 'succeeded'
        assert len(images_in(restarted_provider.calls[0])) == 1
        forked = restarted.post(f'/api/v1/agent/sessions/{sid}/fork',json={'up_to_message_id':receipt['user_message']['message_id'],'title':'图片分叉'})
        assert forked.status_code == 201, forked.text
        fork_id = forked.json()['session_id']
        fork_messages = restarted.get(f'/api/v1/agent/sessions/{fork_id}/messages').json()['messages']
        assert len(fork_messages[0]['images']) == 1
        assert restarted.get(fork_messages[0]['images'][0]['url']).content == raw
        fork_turn = start(restarted, fork_id, [], key='fork-image-question', message='描述分叉前的图片')
        assert fork_turn.status_code == 202, fork_turn.text
        assert wait_for_success(restarted, fork_turn.json()['run']['run_id'])['status'] == 'succeeded'
        assert len(images_in(restarted_provider.calls[-1])) == 1


def test_image_claim_is_session_scoped_and_queue_survives_restart(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        first, second = session(client), session(client)
        image, raw = upload(client)
        url = f'/api/v1/agent/sessions/{first}/queue'
        queued = client.post(url, json={'item_id':'image-queue-1','message':'','image_ids':[image['image_id']]})
        assert queued.status_code == 200, queued.text
        assert queued.json()['items'][0]['image_ids'] == [image['image_id']]
        assert start(client, second, [image['image_id']]).status_code == 409
        assert client.get(f"/api/v1/agent/sessions/{second}/images/{image['image_id']}").status_code == 404
        assert client.get(image['url']).content == raw
    provider = VisionProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as restarted:
        assert restarted.get(url).json()['items'][0]['image_ids'] == [image['image_id']]
        dispatched = restarted.post(url+'/image-queue-1/dispatch')
        assert dispatched.status_code == 200, dispatched.text
        assert wait_for_success(restarted, dispatched.json()['accepted']['run']['run_id'])['status'] == 'succeeded'
        assert len(images_in(provider.calls[0])) == 1


def test_steered_image_stays_in_same_run_and_history_once(settings):
    provider = VisionProvider(blocked=True)
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        first = start(client, sid, [], message='先介绍一下自己')
        assert first.status_code == 202, first.text
        rid = first.json()['run']['run_id']
        assert provider.entered.wait(10)
        image, _ = upload(client)
        try:
            queued = client.post(f'/api/v1/agent/sessions/{sid}/queue', json={'item_id':'image-steer-1','image_ids':[image['image_id']]})
            assert queued.status_code == 200, queued.text
            steered = client.post(f'/api/v1/agent/sessions/{sid}/queue/image-steer-1/steer', json={'expected_run_id':rid})
            assert steered.status_code == 200, steered.text
        finally:
            provider.release.set()
        assert wait_for_success(client,rid)['status'] == 'succeeded'
        assert len(provider.calls) == 2
        assert len(images_in(provider.calls[-1])) == 1
        messages = client.get(f'/api/v1/agent/sessions/{sid}/messages').json()['messages']
        assert len([message for message in messages if message.get('images')]) == 1
        next_run = start(client, sid, [], key='image-after-steer', message='前面图里是什么')
        assert next_run.status_code == 202, next_run.text
        assert wait_for_success(client,next_run.json()['run']['run_id'])['status'] == 'succeeded'
        assert len(images_in(provider.calls[-1])) == 1


def test_schema_29_migration_adds_images_without_losing_messages(settings):
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as client:
        sid=session(client)
        result=start(client,sid,[],message='旧对话保留')
        assert wait_for_success(client,result.json()['run']['run_id'])['status']=='succeeded'
    database=SqliteStore(settings.database_path)
    with database.connection() as connection:
        connection.execute('DROP TABLE agent_image')
        connection.execute('ALTER TABLE session_message DROP COLUMN images_json')
        connection.execute('PRAGMA user_version=29')
    assert database.initialize().schema_version == SCHEMA_VERSION
    with TestClient(create_app(settings, agent_provider=ApiProvider())) as restarted:
        page=restarted.get(f'/api/v1/agent/sessions/{sid}/messages').json()
        assert page['messages'][0]['content']=='旧对话保留'
        assert page['messages'][0]['images']==[]


def test_compacted_images_stay_readable_without_resending_old_pixels_each_turn(settings):
    provider = VisionProvider()
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        sid = session(client)
        image, raw = upload(client)
        first = start(client, sid, [image['image_id']], message='记住这张图')
        rid = first.json()['run']['run_id']
        assert wait_for_success(client, rid)['status'] == 'succeeded'
        store = client.app.state.agent_service.store
        store.append_session_checkpoint(sid, run_id=None, through_ordinal=2, source_message_count=2,
            content='【当前目标】用户附了一张图片。', reported_model=None)
        second = start(client, sid, [], key='compacted-image-question', message='现在还能看到前面那张图吗')
        assert second.status_code == 202, second.text
        assert wait_for_success(client, second.json()['run']['run_id'])['status'] == 'succeeded'
        assert images_in(provider.calls[-1]) == []
        assert any('附图仍保存在历史消息' in str(message.get('content')) for message in provider.calls[-1])
        history = client.get(f'/api/v1/agent/sessions/{sid}/messages').json()['messages']
        assert client.get(history[0]['images'][0]['url']).content == raw
        with store.database.connection() as connection:
            assert connection.execute('SELECT count(*) FROM agent_image').fetchone()[0] == 1
            assert all('data:image' not in row[0] for row in connection.execute('SELECT messages_json FROM run_transcript'))


def test_image_contracts_match_current_models():
    from pathlib import Path
    import runpy
    root = Path(__file__).resolve().parents[3]
    exported = runpy.run_path(str(root/'services/writing-runtime/scripts/export_agent_image_contracts.py'))
    for name, model in exported['MODELS'].items():
        schema = json.loads((root/'packages/contracts'/name).read_text(encoding='utf-8'))
        schema.pop('$id')
        schema.pop('$schema')
        assert schema == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize('wire', ['chat','responses','anthropic_messages'])
def test_uploaded_image_reaches_actual_http_provider_payload(settings, wire):
    import httpx
    from zhimeng_runtime.agent_provider import OpenAICompatibleAgentProvider
    from test_multiwire_agent_integration import _events

    captured = []
    async def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, content=_events(wire,text='图片已通过实际接口接收。'))
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleAgentProvider(base_url='https://image-fixture.invalid/v1', model='fixture-model',
        api_key='synthetic', credential_reference='synthetic',timeout_seconds=5,max_tokens=1024,
        temperature=0.2,reasoning_effort='auto',wire_format=wire,client=upstream)
    with TestClient(create_app(settings,agent_provider=provider)) as client:
        sid=session(client)
        image, raw=upload(client)
        created=start(client,sid,[image['image_id']],message='看看这张合成图')
        assert created.status_code == 202, created.text
        terminal=wait_for_success(client,created.json()['run']['run_id'])
        assert terminal['status']=='succeeded', terminal
    assert len(captured)==1
    messages=captured[0].get('messages',captured[0].get('input'))
    parts=[part for message in messages if isinstance(message.get('content'),list) for part in message['content']]
    found=next(part for part in parts if part['type'] in {'image_url','input_image','image'})
    encoded=base64.b64encode(raw).decode()
    if wire=='chat': assert found['image_url']['url']=='data:image/png;base64,'+encoded
    elif wire=='responses': assert found['image_url']=='data:image/png;base64,'+encoded
    else: assert found['source']=={'type':'base64','media_type':'image/png','data':encoded}


def test_old_queue_without_image_field_can_be_retried_after_upgrade(settings):
    with TestClient(create_app(settings,agent_provider=ApiProvider())) as client:
        sid=session(client)
        entry={'item_id':'legacy-image-queue','message':'旧队列内容'}
        url=f'/api/v1/agent/sessions/{sid}/queue'
        assert client.post(url,json=entry).status_code==200
        with client.app.state.agent_service.store.database.connection() as connection:
            state=json.loads(connection.execute('SELECT state_json FROM session_queue WHERE session_id=?',(sid,)).fetchone()[0])
            state['items'][0]['input'].pop('image_ids')
            connection.execute('UPDATE session_queue SET state_json=? WHERE session_id=?',(json.dumps(state),sid))
        result=client.post(url,json=entry)
        assert result.status_code==200,result.text
        assert len(result.json()['items'])==1


def test_queue_image_caption_can_be_cleared_while_text_only_empty_edit_is_rejected(settings):
    with TestClient(create_app(settings,agent_provider=ApiProvider())) as client:
        sid=session(client)
        image,_=upload(client)
        url=f'/api/v1/agent/sessions/{sid}/queue'
        queued=client.post(url,json={'item_id':'caption-image-1','message':'说明','image_ids':[image['image_id']]}).json()
        edited=client.patch(url,json={'revision':queued['revision'],'action':'edit','item_id':'caption-image-1','message':''})
        assert edited.status_code==200,edited.text
        item=edited.json()['items'][0]
        assert item['message']=='[图片]' and item['image_ids']==[image['image_id']]
        text=client.post(url,json={'item_id':'caption-text-1','message':'纯文字'}).json()
        invalid=client.patch(url,json={'revision':text['revision'],'action':'edit','item_id':'caption-text-1','message':''})
        assert invalid.status_code==422,invalid.text
