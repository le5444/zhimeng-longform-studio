# -*- coding: utf-8 -*-
"""联网搜索 search_web（2026-09-26）。

以前只有 web_fetch：给了网址才能抓，模型要查「今天的新闻、最新版本」只能自己编网址或者嘴上说查过。
现在照 OpenCode（packages/core/src/tool/websearch.ts）接公开的免费搜索服务：先 Exa，不通再 Parallel，都走 MCP 的 tools/call。
这里用本机的假服务顶替两家，不连外网。
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
from pathlib import Path

import pytest

from test_agent_tools import _build_runtime, _call_tool, _expect_tool_error
from zhimeng_runtime import agent_tools

EXA_TEXT = (
    "Title: Releases · openai/codex\nURL: https://github.com/openai/codex/releases\nPublished: N/A\nAuthor: openai\n"
    "Highlights:\nRelease 0.157.0\n\n---\n\n"
    "Title: N/A\nURL: https://example.com/codex-0-157\nPublished: 2026-09-25T08:00:00.000Z\nAuthor: someone\n"
    "Highlights:\nCodex 0.157 发布了。"
)
PARALLEL_JSON = json.dumps({
    "search_id": "search_x",
    "results": [
        {"url": "https://fanqienovel.com/", "title": "番茄小说", "publish_date": None, "excerpts": ["作家专区", "征文"]},
        {"url": "https://example.org/zhengwen", "title": "盛夏寻章征文", "publish_date": "2026-07-01", "excerpts": ["7 月 1 日开赛"]},
    ],
}, ensure_ascii=False)


def _rpc_result(text: str, *, is_error: bool = False) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}], **({"isError": True} if is_error else {})}}


class _SearchHandler(http.server.BaseHTTPRequestHandler):
    behaviour: dict[str, tuple[int, str, str]] = {}
    captured: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).captured.append({"path": self.path, "body": body})
        status, content_type, payload = type(self).behaviour[self.path]
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        return


def _sse(message: dict) -> tuple[int, str, str]:
    return 200, "text/event-stream", "event: message\ndata: " + json.dumps(message, ensure_ascii=False) + "\n\n"


def _json(message: dict) -> tuple[int, str, str]:
    return 200, "application/json", json.dumps(message, ensure_ascii=False)


@pytest.fixture
def search_services(monkeypatch: pytest.MonkeyPatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SearchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(agent_tools, "WEB_SEARCH_EXA_URL", f"{base}/exa")
    monkeypatch.setattr(agent_tools, "WEB_SEARCH_PARALLEL_URL", f"{base}/parallel")
    _SearchHandler.captured = []
    _SearchHandler.behaviour = {"/exa": _sse(_rpc_result(EXA_TEXT)), "/parallel": _json(_rpc_result(PARALLEL_JSON))}
    try:
        yield _SearchHandler
    finally:
        server.shutdown()
        thread.join()


def _search(tmp_path: Path, arguments: dict) -> dict:
    executor, *_rest = _build_runtime(tmp_path)
    return asyncio.run(_call_tool(executor, "search_web", arguments)).output


def test_exa_answers_with_titles_urls_dates_and_the_search_time(tmp_path: Path, search_services) -> None:
    output = _search(tmp_path, {"query": "Codex CLI 最新版本", "limit": 5})

    assert output["schema"] == "zhimeng.tool-result.web-search.v1"
    assert output["provider"] == "exa"
    assert output["results"] == [
        {"title": "Releases · openai/codex", "url": "https://github.com/openai/codex/releases", "published": None},
        {"title": None, "url": "https://example.com/codex-0-157", "published": "2026-09-25T08:00:00.000Z"},
    ]
    assert "Codex 0.157 发布了。" in output["content"]
    assert output["searched_at"][:4].isdigit() and "T" in output["searched_at"]
    assert "外部网页内容" in output["note"]
    assert "tried" not in output
    # 请求只带搜索词和条数，不捎带任何本地内容。
    (call,) = search_services.captured
    assert call["path"] == "/exa"
    assert call["body"]["method"] == "tools/call"
    assert call["body"]["params"] == {"name": "web_search_exa", "arguments": {"query": "Codex CLI 最新版本", "numResults": 5}}


def test_parallel_takes_over_when_exa_is_down_and_says_why(tmp_path: Path, search_services) -> None:
    search_services.behaviour["/exa"] = (500, "text/plain", "boom")
    output = _search(tmp_path, {"query": "番茄小说征文"})

    assert output["provider"] == "parallel"
    assert output["tried"] == [{"provider": "exa", "error": "HTTP 500"}]
    assert [item["url"] for item in output["results"]] == ["https://fanqienovel.com/", "https://example.org/zhengwen"]
    assert "7 月 1 日开赛" in output["content"]
    parallel_call = search_services.captured[-1]
    assert parallel_call["body"]["params"]["name"] == "web_search"
    assert parallel_call["body"]["params"]["arguments"]["search_queries"] == ["番茄小说征文"]


def test_an_exa_error_message_also_falls_back_to_parallel(tmp_path: Path, search_services) -> None:
    search_services.behaviour["/exa"] = _sse({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "rate limited"}})
    output = _search(tmp_path, {"query": "番茄小说征文"})
    assert output["provider"] == "parallel"
    assert output["tried"] == [{"provider": "exa", "error": "rate limited"}]


def test_both_services_down_is_a_retryable_error_naming_each_reason(tmp_path: Path, search_services) -> None:
    search_services.behaviour["/exa"] = (503, "text/plain", "busy")
    search_services.behaviour["/parallel"] = _json(_rpc_result("quota exceeded", is_error=True))
    executor, *_rest = _build_runtime(tmp_path)
    error = _expect_tool_error(executor, "search_web", {"query": "任何东西"})
    assert error.code == "agent_tool_web_search_failed"
    assert error.retryable is True
    assert "exa：HTTP 503" in str(error)
    assert "parallel：quota exceeded" in str(error)
    assert "web_fetch" in str(error)


def test_nothing_found_is_a_normal_answer_not_a_failure(tmp_path: Path, search_services) -> None:
    search_services.behaviour["/exa"] = _sse(_rpc_result("No search results found."))
    output = _search(tmp_path, {"query": "一个不存在的词组 zq9x"})
    assert output["provider"] == "exa"
    assert output["results"] == []
    assert output["result_count"] == 0


def test_model_spellings_are_accepted_limit_is_clamped_blank_query_is_refused(tmp_path: Path, search_services) -> None:
    executor, *_rest = _build_runtime(tmp_path)
    asyncio.run(_call_tool(executor, "search_web", {"query": "codex", "limit": 50}))
    assert search_services.captured[-1]["body"]["params"]["arguments"]["numResults"] == 20
    with pytest.raises(Exception) as info:
        asyncio.run(_call_tool(executor, "search_web", {"query": ""}))
    assert "query" in str(info.value)


def test_search_engine_pages_and_bare_queries_given_to_web_fetch_become_a_web_search(tmp_path: Path, search_services) -> None:
    """grok-4.6 真机：想搜东西时拿 web_fetch 连开十几次 google.com/search?q=…，还有一次把搜索词当网址传。"""
    executor, *_rest = _build_runtime(tmp_path)
    google = asyncio.run(_call_tool(executor, "web_fetch", {"url": "https://www.google.com/search?q=OpenAI+Codex+CLI+latest+version"})).output
    assert google["schema"] == "zhimeng.tool-result.web-search.v1"
    assert google["routed_from_url"] == "https://www.google.com/search?q=OpenAI+Codex+CLI+latest+version"
    assert "改用 search_web" in google["note"] and "外部网页内容" in google["note"]
    assert search_services.captured[-1]["body"]["params"]["arguments"]["query"] == "OpenAI Codex CLI latest version"

    asyncio.run(_call_tool(executor, "web_fetch", {"url": "https://cn.bing.com/search?q=番茄小说+征文&form=QBLH"}))
    assert search_services.captured[-1]["body"]["params"]["arguments"]["query"] == "番茄小说 征文"
    asyncio.run(_call_tool(executor, "web_fetch", {"url": "https://www.baidu.com/s?wd=黄金三章"}))
    assert search_services.captured[-1]["body"]["params"]["arguments"]["query"] == "黄金三章"

    bare = asyncio.run(_call_tool(executor, "web_fetch", {"url": '"Codex CLI" OpenAI latest version'})).output
    assert bare["provider"] == "exa"
    assert search_services.captured[-1]["body"]["params"]["arguments"]["query"] == "Codex CLI\" OpenAI latest version"
    assert "不是网址" in bare["note"]


def test_ordinary_urls_are_still_fetched_and_a_missing_scheme_is_filled_in(tmp_path: Path) -> None:
    from zhimeng_runtime.agent_tools import _search_query_from_url

    for url in (
        "https://github.com/openai/codex/releases",
        "https://www.google.com/maps/place/x",
        "https://www.bing.com/",
        "http://127.0.0.1:8788/api",
        "example.com/hello",
        "localhost:5175",
    ):
        assert _search_query_from_url(url) is None, url
    assert _search_query_from_url("https://duckduckgo.com/?q=codex+cli") == "codex cli"
    assert _search_query_from_url("番茄小说 2026 征文") == "番茄小说 2026 征文"


def test_web_search_is_offered_and_runs_as_a_read_only_tool(tmp_path: Path) -> None:
    executor, *_rest = _build_runtime(tmp_path)
    names = [item.name for item in executor.definitions]
    assert names.index("search_web") == names.index("web_fetch") + 1
    assert "search_web" in agent_tools.READONLY_TOOL_NAMES
    assert "search_web" in agent_tools.WEB_TOOL_NAMES
    assert "web_search" not in agent_tools.WEB_TOOL_NAMES  # 见 WEB_TOOL_NAMES 上面的注释
    description = executor._definitions_by_name["search_web"].description
    assert "current year" in description
