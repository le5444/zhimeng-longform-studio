"""网页太长时全文存成文件（9-29，总指令 8.3 第 8 件的网页那一半）。

以前一张网页剥成文字后超过 2.4 万字只留头尾各一半，中间直接丢了。现在和命令输出一样：头尾照给，
全文存进运行时文件夹下的 command-output/，中间那一行写明文件在哪、怎么搜、能用 read_file 整段读。
网页内容不是本机的东西，照原样存（不遮密钥）。这里用本机的假网页，不连外网。
"""

from __future__ import annotations

import asyncio
import http.server
import os
import threading
from pathlib import Path

import pytest

from test_agent_tools import _build_runtime, _call_tool
from zhimeng_runtime.agent_tools import WEB_FETCH_CONTENT_CHARACTERS


class _PageHandler(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self) -> None:  # noqa: N802
        data = type(self).body
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def page_url():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/article"
    finally:
        server.shutdown()
        thread.join()


def _page(paragraphs: int, middle: str) -> bytes:
    body = []
    for index in range(1, paragraphs + 1):
        body.append(f"<p>第{index}段：山门前的石阶一共九百级，{'风' * 30}。</p>")
        if index == paragraphs // 2:
            body.append(f"<p>{middle}</p>")
    return f"<html><head><title>长文章</title></head><body>{''.join(body)}</body></html>".encode("utf-8")


def test_long_page_keeps_head_and_tail_and_saves_the_whole_text(tmp_path: Path, page_url: str) -> None:
    _PageHandler.body = _page(1200, "中间那一句：青云直上九万里")
    executor, *_rest = _build_runtime(tmp_path)

    output = asyncio.run(_call_tool(executor, "web_fetch", {"url": page_url})).output

    assert output["truncated"] is True
    assert output["title"] == "长文章"
    saved = Path(output["content_file"])
    assert saved.parent.name == "command-output"
    assert saved.name.startswith("web-")
    whole = saved.read_text(encoding="utf-8")
    assert "青云直上九万里" in whole
    assert "第1200段" in whole
    shown = output["content"]
    assert "青云直上九万里" not in shown
    assert "第1段" in shown[:40]  # 剥出来的正文开头带着标题（原来的剥法）
    assert "第1200段" in shown
    assert str(saved) in shown
    assert ("Select-String -Path" if os.name == "nt" else "grep -n") in shown
    assert len(shown) < WEB_FETCH_CONTENT_CHARACTERS + 800


def test_short_page_is_not_saved(tmp_path: Path, page_url: str) -> None:
    _PageHandler.body = _page(20, "中间那一句")
    executor, *_rest = _build_runtime(tmp_path)

    output = asyncio.run(_call_tool(executor, "web_fetch", {"url": page_url})).output

    assert output["truncated"] is False
    assert "content_file" not in output
    assert "中间那一句" in output["content"]
