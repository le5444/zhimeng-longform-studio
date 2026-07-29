from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
PROJECT = Path.home() / "Documents" / "写小说工作区" / "phase11-玄幻voice验收-灵器铜秤_创作项目"
KB = Path.home() / ".codex" / "skills" / "novel-kb-manager" / "library" / "projects" / "phase11-玄幻voice验收-灵器铜秤.kb"
MS = PROJECT / "05_正文内容" / "第一卷" / "CH002" / "CH002_定稿.md"


def get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=15) as resp:
        data = resp.read()
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            return resp.status, json.loads(data.decode("utf-8"))
        return resp.status, data


def post(action: str, payload: dict):
    body = json.dumps({"action": action, "payload": payload}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        BASE + "/tool",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    checks = []

    def ok(name: str, cond: bool, detail=None):
        checks.append({"name": name, "ok": bool(cond), "detail": detail})
        print(("PASS" if cond else "FAIL"), name, detail if detail is not None else "")

    st, health = get("/health")
    ok("health_status", st == 200 and health.get("ok") is True, health.get("version"))
    actions = set(health.get("actions") or [])
    required = {
        "writing_project_bind",
        "writing_adapter_preview",
        "writing_risk_draft_stage",
        "writing_risk_draft_expand",
        "writing_hard_review",
        "writing_author_diff_preview",
        "writing_writeback_apply",
        "writing_post_writeback_sync",
        "writing_agent_revise",
    }
    ok("actions_core_present", required.issubset(actions), sorted(required - actions))
    ok("workbench_field", health.get("workbench") == "/workbench/")

    st, html = get("/workbench/")
    ok("workbench_html", st == 200 and b"workbench" in html.lower() or st == 200 and len(html) > 500, len(html) if isinstance(html, (bytes, bytearray)) else None)

    st, staging = get("/api/staging")
    ok("staging_api", st == 200 and staging.get("ok") is True, staging.get("runs") and len(staging.get("runs") or []))

    if PROJECT.is_dir():
        st, chapters = get("/api/chapters?root=" + urllib.parse.quote(str(PROJECT)))
        ok("chapters_api", st == 200 and chapters.get("ok") is True, {
            "count": chapters.get("chapter_count"),
            "last": chapters.get("last_completed_chapter_id"),
            "next": chapters.get("suggested_next_chapter_id"),
        })
    else:
        ok("chapters_api", False, f"missing project {PROJECT}")

    if MS.is_file():
        st, body = get("/api/source?path=" + urllib.parse.quote(str(MS)))
        ok("source_read_ms", st == 200 and len(body) > 100, len(body) if isinstance(body, (bytes, bytearray)) else None)
    else:
        ok("source_read_ms", False, f"missing manuscript {MS}")

    # escape should fail
    try:
        get("/api/source?path=" + urllib.parse.quote(r"C:\\Windows\\System32\\drivers\\etc\\hosts"))
        ok("source_escape_blocked", False, "unexpected success")
    except urllib.error.HTTPError as e:
        ok("source_escape_blocked", e.code in (403, 400, 404), e.code)

    # bind smoke (non-destructive)
    if PROJECT.is_dir() and KB.is_dir():
        st, bind = post(
            "writing_project_bind",
            {
                "root_path": str(PROJECT),
                "kb_root": str(KB),
                "chapter_id": "CH002",
                "adapters": ["novel-kb-manager", "novel-creation-suite"],
            },
        )
        res = bind.get("result") or {}
        ok("bind", st == 200 and bind.get("ok") is True and res.get("project_id"), res.get("project_id"))
        status_payload = {
            "project_id": res.get("project_id"),
            "chapter_id": "CH002",
        }
        st2, status = post("writing_project_status", status_payload)
        ok("status", st2 == 200 and status.get("ok") is True, (status.get("result") or {}).get("project", {}).get("status"))
    else:
        ok("bind", False, "project/kb missing")
        ok("status", False, "skipped")

    # EXE presence
    exe = ROOT / "dist" / "zhimeng-workbench" / "zhimeng-workbench.exe"
    ok("exe_present", exe.is_file(), str(exe) if exe.is_file() else None)

    failed = [c for c in checks if not c["ok"]]
    report = {
        "schema": "zhimeng.smoke-e2e.v1",
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "base": BASE,
        "checks": checks,
        "failed_count": len(failed),
        "pass": len(failed) == 0,
    }
    out = ROOT / "evidence" / f"smoke-e2e-{datetime.now().strftime('%Y%m%d')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("report", out, "pass" if report["pass"] else "FAIL", "failed", len(failed))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
