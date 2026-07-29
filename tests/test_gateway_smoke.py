"""End-to-end smoke test for the minimal novel writing Gateway.

Starts the Gateway in-process on an ephemeral port, then exercises:
  - GET /health
  - POST /tool {"action": "writing_runtime_status"}
  - POST /tool with an unsupported action (expect a clean 400)

Standard library only. Run:
    python tests/test_gateway_smoke.py
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "bridge")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bridge.gateway import build_server  # noqa: E402
from bridge.writing_runtime import configure_writing_roots  # noqa: E402


def _post(url: str, obj: dict) -> tuple[int, dict]:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    # No trusted roots needed for status/health; keep it empty for a pure smoke run.
    configure_writing_roots([])
    server = build_server("127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    failures = []
    try:
        # 1. health
        status, body = _get(base + "/health")
        if status != 200 or not body.get("ok"):
            failures.append(f"health failed: {status} {body}")
        elif "writing_runtime_status" not in body.get("actions", []):
            failures.append(f"health missing actions: {body}")
        else:
            print("PASS  GET /health ->", body["actions"])

        # 2. writing_runtime_status
        status, body = _post(base + "/tool", {"action": "writing_runtime_status"})
        if status != 200 or not body.get("ok"):
            failures.append(f"runtime_status failed: {status} {body}")
        else:
            result = body.get("result", {})
            if result.get("schema") != "zhimeng.writing-runtime-status.v1":
                failures.append(f"runtime_status bad schema: {result}")
            else:
                print("PASS  POST /tool writing_runtime_status -> phase:", result.get("phase"))

        # 3. unsupported action -> clean 400
        status, body = _post(base + "/tool", {"action": "definitely_not_real"})
        if status != 400 or body.get("error") != "unsupported_action":
            failures.append(f"unsupported action not rejected cleanly: {status} {body}")
        else:
            print("PASS  POST /tool bad action -> 400 unsupported_action")
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nALL GATEWAY SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
