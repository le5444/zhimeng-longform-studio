"""Synthetic JSON-RPC peer for real-pipe MCP tests; never reads author files."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

root = Path.cwd()
mode = sys.argv[1] if len(sys.argv) > 1 else "legacy"
lock = threading.Lock()
cancelled = set()
changed = False


def record(data):
    with lock:
        with (root / "events.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(data, ensure_ascii=False) + "\n")


def send(data):
    with lock:
        print(json.dumps(data, ensure_ascii=False), flush=True)


child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
record({"pid": os.getpid(), "child_pid": child.pid, "host_secret_present": "AI_HOST_TEST_SECRET" in os.environ, "args": sys.argv[2:]})
secret = os.environ.get("ODD_CREDENTIAL", "")
if secret:
    middle = len(secret) // 2
    sys.stderr.write("credential: " + secret[:middle])
    sys.stderr.flush()
    time.sleep(0.02)
    sys.stderr.write(secret[middle:] + "\n")
    sys.stderr.flush()


def tool(name, readonly=False):
    return {"name": name, "title": "合成工具 " + name, "description": "Synthetic file task " + name,
            "inputSchema": {"type": "object", "properties": {"text": {"type": ["string", "null"]}}, "additionalProperties": True},
            "annotations": {"readOnlyHint": readonly}}


def call(request):
    global changed
    identifier = request["id"]
    name = request["params"]["name"]
    args = request["params"].get("arguments", {})
    record({"call": name, "id": identifier, "arguments": args})
    if name == "wait":
        gate = root / args.get("gate", "release")
        while not gate.exists() and identifier not in cancelled:
            time.sleep(0.01)
        if identifier in cancelled:
            return
        content = "wait completed"
    elif name == "read.csv":
        with (root / "sample.csv").open(encoding="utf-8", newline="") as file:
            rows = list(csv.DictReader(file))
        content = json.dumps(rows, ensure_ascii=False)
    elif name == "write.summary":
        (root / "summary.md").write_text(args.get("text", ""), encoding="utf-8")
        if args.get("wait_after_write"):
            while not (root / "release-after-write").exists() and identifier not in cancelled:
                time.sleep(0.01)
            if identifier in cancelled:
                return
        content = "written"
    elif name == "disconnect.write":
        with (root / "side-effects.txt").open("a") as file:
            file.write("once\n")
        os._exit(3)
    elif name == "change.catalog":
        changed = True
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        content = "changed"
    else:
        content = secret if name == "echo.secret" else "explicit failure" if name == "error" else "ok"
    result = {"content": [{"type": "text", "text": content}, {"type": "text", "text": "second block"}], "isError": name == "error" or (name == "write.summary" and args.get("fail") is True), "structuredContent": {"value": content}}
    if mode == "modern":
        result.update(resultType="complete")
    send({"jsonrpc": "2.0", "id": identifier, "result": result})


for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    identifier = request.get("id")
    if method in {"server/discover", "initialize"}:
        record({"handshake": method, "pid": os.getpid()})
    if method == "server/discover":
        if mode == "modern":
            send({"jsonrpc": "2.0", "id": identifier, "result": {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {"listChanged": True}}, "ttlMs": 0}})
        elif mode in {"handshake-fail", "legacy-discovery-disconnect"}:
            os._exit(4)
        elif mode == "legacy-discovery-silent":
            continue
        else:
            send({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "legacy peer"}})
    elif method == "initialize":
        if mode == "handshake-fail":
            os._exit(4)
        send({"jsonrpc": "2.0", "id": identifier, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "fixture", "version": "1"}}})
    elif method == "tools/list":
        cursor = request.get("params", {}).get("cursor")
        record({"list": cursor})
        if mode == "list-fail":
            send({"jsonrpc": "2.0", "id": identifier, "error": {"code": -32000, "message": "list failed " + secret}})
            continue
        result = {"tools": [tool("read.csv", True), tool("write.summary")] if not cursor else [tool("wait", True), tool("error"), tool("echo.secret", True), tool("disconnect.write"), tool("change.catalog")]}
        if changed:
            result["tools"].append(tool("new.tool" if not cursor else "another.new.tool", True))
        if not cursor or mode == "cursor-repeat":
            result["nextCursor"] = "page2"
        if mode == "modern":
            result.update(cacheScope="private", resultType="complete", ttlMs=0)
        send({"jsonrpc": "2.0", "id": identifier, "result": result})
    elif method == "tools/call":
        threading.Thread(target=call, args=(request,), daemon=True).start()
    elif method == "notifications/cancelled":
        cancelled.add(request["params"]["requestId"])
        record({"cancelled": request["params"]["requestId"]})
