from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from zhimeng_runtime import agent_tools
from zhimeng_runtime.agent_tools import AgentToolExecutor
from zhimeng_runtime.web_reader import RenderedWebPage, decode_web_text, needs_browser


@pytest.fixture
def fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    real_client = httpx.Client
    executor = AgentToolExecutor.__new__(AgentToolExecutor)
    executor._command_output_root = tmp_path / "command-output"

    def call(body: bytes, *, status: int = 200, content_type: str = "text/html"):
        def respond(request: httpx.Request):
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            return httpx.Response(status, content=body, headers={"content-type": content_type})

        monkeypatch.setattr(agent_tools.httpx, "Client", lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(respond),
        ))
        return executor._fetch_web_page("https://example.org/article")

    return call


SHELL = b'<html><head><title>Page</title></head><body><div id="root"></div><script src="/app.js"></script></body></html>'


def test_dynamic_page_returns_rendered_text_and_real_destination(fetch, monkeypatch):
    monkeypatch.setattr(agent_tools, "render_web_page", lambda url, **kwargs: RenderedWebPage(
        "https://example.org/final", 200, "text/html", "Loaded page", "First quote by Alice.\nSecond quote by Bob.",
    ))
    result = fetch(SHELL)
    assert result.error_code is None
    assert result.output["extraction_method"] == "browser"
    assert result.output["final_url"] == "https://example.org/final"
    assert "Second quote by Bob" in result.output["content"]


def test_static_page_keeps_fast_path(fetch, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Static content should not launch a browser")
    monkeypatch.setattr(agent_tools, "render_web_page", unexpected)
    result = fetch(b"<title>Static page</title><p>Some useful text.</p><script>alert(1)</script>")
    assert result.error_code is None
    assert result.output["extraction_method"] == "http"
    assert "Some useful text." in result.output["content"]


@pytest.mark.parametrize("status", [401, 404, 500])
def test_http_error_is_failed_read_with_original_receipt(fetch, monkeypatch, status):
    monkeypatch.setattr(agent_tools, "render_web_page", lambda *a, **k: pytest.fail("Do not retry HTTP error"))
    result = fetch(b"<h1>Not the article</h1>", status=status)
    assert result.error_code == "agent_tool_web_http_error"
    assert result.output["status_code"] == status
    assert result.output["read_status"] == "failed"
    assert "Not the article" in result.output["content"]


def test_browser_can_recover_public_page_rejected_by_http(fetch, monkeypatch):
    monkeypatch.setattr(agent_tools, "render_web_page", lambda *a, **k: RenderedWebPage(
        "https://example.org/article", 200, "text/html", "Article", "Actual article content.",
    ))
    result = fetch(b"Denied", status=403)
    assert result.error_code is None
    assert result.output["status_code"] == 200
    assert result.output["content"] == "Actual article content."


def test_browser_verification_with_http_200_is_not_read_success(fetch, monkeypatch):
    monkeypatch.setattr(agent_tools, "render_web_page", lambda *a, **k: RenderedWebPage(
        "https://example.org/article", 200, "text/html", "Just a moment...", "Verify you are human",
    ))
    result = fetch(b"Denied", status=403)
    assert result.error_code == "agent_tool_web_verification_required"
    assert result.output["read_status"] == "failed"


def test_missing_browser_preserves_partial_html_and_reports_failure(fetch, monkeypatch):
    def unavailable(*args, **kwargs):
        raise RuntimeError("Browser unavailable")
    monkeypatch.setattr(agent_tools, "render_web_page", unavailable)
    result = fetch(SHELL)
    assert result.error_code == "agent_tool_web_render_failed"
    assert result.output["content"] == "Page"
    assert result.output["browser_error"] == "Browser unavailable"


def test_empty_page_is_failed_read(fetch):
    result = fetch(b"<html><body></body></html>")
    assert result.error_code == "agent_tool_web_content_missing"
    assert result.output["content"] == ""


def test_rendered_long_page_retains_middle_in_saved_text(fetch, monkeypatch):
    text = "a" * 15000 + "MIDDLE EVIDENCE" + "z" * 15000
    monkeypatch.setattr(agent_tools, "render_web_page", lambda *a, **k: RenderedWebPage(
        "https://example.org/article", 200, "text/html", "Long page", text,
    ))
    result = fetch(SHELL)
    assert result.output["truncated"] is True
    assert "MIDDLE EVIDENCE" not in result.output["content"]
    assert Path(result.output["content_file"]).read_text(encoding="utf-8") == text


@pytest.mark.parametrize("declaration", ['<meta charset="gb2312">', '<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'])
def test_chinese_meta_charset_is_decoded(declaration):
    source = declaration + "<p>中国网页正文</p>"
    assert decode_web_text(source.encode("gb2312"), "text/html") == source


def test_header_charset_and_utf8_bom():
    assert decode_web_text("正文".encode("gb18030"), "text/plain; charset='gb18030'") == "正文"
    assert decode_web_text(b"\xef\xbb\xbfhello", "text/plain") == "hello"
    assert decode_web_text(b"hello", "text/plain; charset=unknown") == "hello"


def test_large_static_html_and_plain_pages_do_not_trigger_browser():
    assert not needs_browser('<script src="analytics.js"></script><p>Article</p>', "article " * 500)
    assert not needs_browser("<p>Small static page</p>", "Small static page")
