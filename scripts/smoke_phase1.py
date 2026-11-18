from __future__ import annotations

import json
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = "http://127.0.0.1:8787"
WORKBENCH = "http://127.0.0.1:5174"


def get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    checks: list[dict] = []

    def check(name: str, condition: bool, detail: object) -> None:
        checks.append({"name": name, "ok": bool(condition), "detail": detail})
        print(f"{'PASS' if condition else 'FAIL'} {name}: {detail}")

    live = get_json(f"{RUNTIME}/health/live")
    ready = get_json(f"{RUNTIME}/health/ready")
    status = get_json(f"{RUNTIME}/api/v1/runtime/status")
    proxy_status = get_json(f"{WORKBENCH}/api/v1/runtime/status")

    with urllib.request.urlopen(f"{WORKBENCH}/", timeout=5) as response:
        page = response.read().decode("utf-8")
        check("workbench_http", response.status == 200, response.status)
        check("workbench_title", "<title>织梦</title>" in page, len(page))

    request = urllib.request.Request(
        f"{RUNTIME}/api/v1/events/stream",
        headers={"Accept": "text/event-stream"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        lines = [response.readline().decode("utf-8").strip() for _ in range(3)]

    check("live", live.get("schema") == "zhimeng.health.v1" and live.get("status") == "live", live)
    check("ready", ready.get("status") == "ready" and ready.get("database_schema_version") == 1, ready)
    check(
        "runtime_status",
        status.get("state") == "ready"
        and status.get("database", {}).get("journal_mode") == "wal",
        status,
    )
    check("vite_proxy", proxy_status.get("state") == "ready", proxy_status)
    check("sse_ready", any(line == "event: ready" for line in lines), lines)

    report = {
        "schema": "zhimeng.phase1-live-smoke.v1",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runtime": RUNTIME,
        "workbench": WORKBENCH,
        "checks": checks,
        "pass": all(item["ok"] for item in checks),
    }
    output = ROOT / "evidence" / "phase1-live-smoke-20260718.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report={output}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
