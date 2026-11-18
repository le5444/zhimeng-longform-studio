"""Owned localhost HTTP/SSE peer; operates only in its explicit synthetic root."""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, field
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
import uvicorn


@dataclass
class HttpFixture:
    root: Path
    era: str = "legacy"
    response_style: str = "sse"
    expected_secret: str | None = None
    unauthorized: bool = False
    expired: bool = False
    redirect_target: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    cancelled: set[int | str] = field(default_factory=set)
    released: set[str] = field(default_factory=set)
    session_number: int = 0
    active_session: str | None = None
    url: str = ""

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sample.csv").write_text("department,amount\n设计,10\n设计,20\n工程,50\n", encoding="utf-8")
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}/mcp"
        app = Starlette(routes=[Route("/{path:path}", self.handle, methods=["GET", "POST", "DELETE"])])
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False,
            timeout_graceful_shutdown=1, lifespan="off"))
        self._thread = threading.Thread(target=self._server.run, kwargs={"sockets": [self._socket]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Synthetic HTTP fixture did not start")
            time.sleep(.01)
        return self

    def __exit__(self, *_):
        self._server.should_exit = True
        self._thread.join(5)
        self._socket.close()
        assert not self._thread.is_alive(), "owned HTTP server leaked"

    def _response(self, identifier, result, headers=None):
        payload = {"jsonrpc": "2.0", "id": identifier, "result": result}
        if self.response_style == "json":
            return JSONResponse(payload, headers=headers)
        async def stream():
            yield "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    async def handle(self, request: Request):
        path = request.path_params["path"]
        self.records.append({"http_method": request.method, "path": path,
            "secret_received": request.headers.get("x-workspace-secret") == self.expected_secret if self.expected_secret else False,
            "has_custom_header": "x-workspace-secret" in request.headers,
            "protocol": request.headers.get("mcp-protocol-version"), "session": request.headers.get("mcp-session-id")})
        if path == "redirect-other":
            return Response(status_code=307, headers={"location": self.redirect_target or "http://127.0.0.1:1/mcp"})
        if path == "redirect-same":
            return Response(status_code=307, headers={"location": "/mcp"})
        if self.unauthorized or (self.expected_secret and request.headers.get("x-workspace-secret") != self.expected_secret):
            return Response("unauthorized", status_code=401)
        if request.method == "DELETE":
            self.records.append({"delete_session": request.headers.get("mcp-session-id")})
            return Response(status_code=204)
        if request.method == "GET":
            return Response(status_code=405)
        data = await request.json()
        method = data["method"]
        identifier = data.get("id")
        self.records.append({"method": method, "id": identifier, "meta": data.get("params", {}).get("_meta", {})})
        if method == "server/discover":
            if self.era == "modern":
                return self._response(identifier, {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "ttlMs": 0})
            return JSONResponse({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "legacy"}})
        if method == "initialize":
            self.expired = False
            self.session_number += 1
            self.active_session = f"fixture-{self.session_number}"
            return self._response(identifier, {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                "serverInfo": {"name": "owned-http-fixture", "version": "1"}}, {"Mcp-Session-Id": self.active_session})
        if self.expired:
            return Response("session missing", status_code=404)
        if method == "notifications/cancelled":
            self.cancelled.add(data["params"]["requestId"])
            return Response(status_code=202)
        if identifier is None:
            return Response(status_code=202)
        if method == "tools/list":
            cursor = data.get("params", {}).get("cursor")
            names = ["read.csv"] if not cursor else ["write.summary", "wait", "error", "disconnect.write", "echo.headers"]
            result = {"tools": [{"name": name, "description": "Synthetic HTTP tool " + name,
                "inputSchema": {"type": "object", "properties": {"text": {"type": ["string", "null"]}}, "additionalProperties": True},
                "annotations": {"readOnlyHint": name in {"read.csv", "wait", "echo.headers"}}} for name in names]}
            if not cursor:
                result["nextCursor"] = "page2"
            if self.era == "modern":
                result.update(resultType="complete", cacheScope="private", ttlMs=0)
            return self._response(identifier, result)
        if method == "tools/call":
            name = data["params"]["name"]
            arguments = data["params"].get("arguments", {})
            self.records.append({"call": name, "id": identifier, "arguments": arguments})
            if name == "read.csv":
                with (self.root / "sample.csv").open(encoding="utf-8", newline="") as file:
                    content = json.dumps(list(csv.DictReader(file)), ensure_ascii=False)
            elif name == "write.summary":
                (self.root / "summary.md").write_text(arguments.get("text", ""), encoding="utf-8")
                content = "written"
            elif name == "echo.headers":
                content = request.headers.get("x-workspace-secret", "")
            elif name in {"wait", "disconnect.write"}:
                async def stream():
                    yield ": accepted\n\n"
                    if name == "disconnect.write":
                        with (self.root / "side-effects.txt").open("a") as file:
                            file.write("once\n")
                        return
                    try:
                        while arguments.get("gate", "release") not in self.released and identifier not in self.cancelled:
                            await asyncio.sleep(.02)
                        if identifier not in self.cancelled:
                            payload = {"jsonrpc": "2.0", "id": identifier, "result": {"content": [{"type": "text", "text": "completed"}], "isError": False, "resultType": "complete"}}
                            yield "event: message\ndata: " + json.dumps(payload) + "\n\n"
                    finally:
                        self.records.append({"stream_closed": identifier})
                return StreamingResponse(stream(), media_type="text/event-stream")
            else:
                content = "explicit error"
            result = {"content": [{"type": "text", "text": content}, {"type": "text", "text": "second block"}],
                "structuredContent": {"value": content}, "isError": name == "error"}
            if self.era == "modern":
                result["resultType"] = "complete"
            return self._response(identifier, result)
        return JSONResponse({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "unknown method"}})
