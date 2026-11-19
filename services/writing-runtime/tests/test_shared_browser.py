import asyncio
import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from jsonschema import Draft202012Validator

from zhimeng_runtime.shared_browser import SharedBrowserArguments, SharedBrowserClient, SharedBrowserError


def client(tmp_path, handler):
    (tmp_path / "browser-bridge.json").write_text(json.dumps({"url": "http://127.0.0.1:12345", "token": "a" * 64}))
    return SharedBrowserClient(tmp_path / "artifacts", directory=tmp_path, transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_lost_browser_reply_reads_same_receipt_without_repeating_click(tmp_path):
    calls = []
    request_id = None

    def handler(request):
        nonlocal request_id
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer " + "a" * 64
        if request.method == "POST":
            request_id = json.loads(request.content)["request_id"]
            raise httpx.ReadError("reply lost after real action")
        assert request.url.path.endswith("/requests/" + request_id)
        return httpx.Response(200, json={"status": "completed", "result": {"tab": {"id": "tab-one"}}})

    browser = client(tmp_path, handler)
    result = await browser.execute("session-one", "run-one", "call-one", SharedBrowserArguments(action="click", tab_id="tab-one", observation_id="observed", ref="1"))
    assert result["receipt_recovered"] is True
    assert [method for method, _ in calls] == ["POST", "GET"]


@pytest.mark.anyio
async def test_uncertain_and_user_takeover_do_not_retry_actions(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadError("lost")
        return httpx.Response(200, json={"status": "started"})
    browser = client(tmp_path, handler)
    with pytest.raises(SharedBrowserError, match="未自动重放") as caught:
        await browser.execute("session", "run", "call", SharedBrowserArguments(action="open", url="https://example.com"))
    assert caught.value.code == "browser_action_uncertain"
    assert calls == ["POST", "GET"]
    calls.clear()
    def takeover(request):
        calls.append(request.method)
        return httpx.Response(409, json={"code": "user_control", "error": "用户正在操作此页面"})
    browser = client(tmp_path, takeover)
    with pytest.raises(SharedBrowserError) as caught:
        await browser.execute("session", "run", "call", SharedBrowserArguments(action="reload", tab_id="tab", observation_id="old"))
    assert caught.value.code == "user_control"
    assert calls == ["POST"]


@pytest.mark.anyio
@pytest.mark.parametrize("action,endpoint", [("navigate", "action"), ("screenshot", "screenshot")])
async def test_cancel_releases_page_and_does_not_send_another_action(tmp_path, action, endpoint):
    started = asyncio.Event(); calls = []
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"tabs": []})
        started.set()
        await asyncio.Event().wait()
    browser = client(tmp_path, handler)
    operation = asyncio.create_task(browser.execute("session", "run", "call", SharedBrowserArguments(action=action, tab_id="tab", observation_id="old", url="https://example.com")))
    await started.wait(); operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert calls == [f"/sessions/session/tabs/tab/{endpoint}", "/sessions/session/cancel"]


@pytest.mark.parametrize("body", [
    {"action": "open"}, {"action": "click", "tab_id": "tab", "ref": "1"},
    {"action": "fill", "tab_id": "tab", "observation_id": "obs", "ref": "1"},
    {"action": "press", "tab_id": "tab", "observation_id": "obs"}, {"action": "receipt"},
])
def test_incomplete_browser_action_rejected_before_dispatch(body):
    assert not Draft202012Validator(SharedBrowserArguments.model_json_schema()).is_valid(body)
    with pytest.raises(ValidationError):
        SharedBrowserArguments.model_validate(body)


def test_browser_contract_matches_current_arguments():
    filename = "tool-shared-browser-arguments.schema.json"
    actual = json.loads((Path(__file__).resolve().parents[3] / "packages/contracts" / filename).read_bytes())
    expected = SharedBrowserArguments.model_json_schema()
    expected["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    expected["$id"] = f"https://zhimeng.local/contracts/{filename}"
    assert actual == expected
