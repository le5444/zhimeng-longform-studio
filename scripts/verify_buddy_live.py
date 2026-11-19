# -*- coding: utf-8 -*-
"""沙箱实测：搭档对话的搜索 + 蒸馏 + 入库三条路。

- 沙箱目录：.runtime/buddy-live-<时间戳>
- 蒸馏卡库：临时库（绝不碰作者真总库）
- 后端：独立 DB / 端口，复制主 runtime 的 provider-settings.json
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
SANDBOX = ROOT / ".runtime" / f"buddy-live-{STAMP}"
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}/api/v1"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
DISTILL_KB = Path.home() / ".codex" / "skills" / "novel-distillation" / "scripts" / "distill_kb.py"
REAL_CARDS = Path.home() / ".codex" / "skills" / "novel-distillation" / "library" / "cards.jsonl"

RESULTS: list[dict] = []

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows GBK 控制台兑 emoji
except Exception:
    pass


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": ok, "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else ""), flush=True)


def http_post(path: str, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_for_server(deadline_s: float = 60.0) -> bool:
    start = time.time()
    while time.time() - start < deadline_s:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health/live", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1.0)
    return False


def main() -> int:
    SANDBOX.mkdir(parents=True)
    lib_dir = SANDBOX / "tmp-library"
    lib_dir.mkdir()
    # 用真卡库前 40 行建临时库，保证搜索有真实命中
    lines = REAL_CARDS.read_text(encoding="utf-8").splitlines()[:40]
    (lib_dir / "cards.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    stats = subprocess.run(
        [PY, str(DISTILL_KB), "stats", str(lib_dir)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    check("临时卡库就绪", stats.returncode == 0, stats.stdout.strip()[:120])

    # 合成一本小书（含可被项目文件检索命中的章节与设定）
    book = SANDBOX / "author-projects" / "测试书_青云剑"
    for idx, label in {1: "基础信息", 2: "人物设定", 3: "世界观", 5: "正文内容"}.items():
        (book / f"{idx:02d}_{label}").mkdir(parents=True)
    vol = book / "05_正文内容" / "第一卷"
    vol.mkdir(parents=True)
    (vol / "CH001.md").write_text(
        "# 第一章 少年下山\n\n" + "林凡握紧青云剑，一步一步走出山门。" * 40,
        encoding="utf-8",
    )
    (book / "02_人物设定" / "林凡.md").write_text("林凡，青云宗末代弟子。", encoding="utf-8")

    # 复制 provider 配置到沙箱（后端按 database_path.parent 找它）。
    # 用 data/ 下 deepseek-v3.2 那份：实测比 grok-4.6 稳。
    shutil.copy(ROOT / "data" / "provider-settings.json", SANDBOX / "provider-settings.json")

    env_extra = {
        "ZHIMENG_ENVIRONMENT": "sandbox",
        "ZHIMENG_PORT": str(PORT),
        "ZHIMENG_DATABASE_PATH": str(SANDBOX / "workbench.sqlite3"),
        "ZHIMENG_TRUSTED_PROJECT_ROOTS": str(SANDBOX / "author-projects"),
        "ZHIMENG_FOUR_SKILLS_DISTILLATION_LIBRARY": str(lib_dir),
        "PYTHONPATH": str(ROOT / "services" / "writing-runtime" / "src"),
        "PYTHONUTF8": "1",
    }
    import os
    env = {**os.environ, **env_extra}
    log_path = SANDBOX / "backend.log"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(
        [PY, "-m", "zhimeng_runtime"],
        cwd=str(ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        check("后端启动", wait_for_server(), f"端口 {PORT}，日志 {log_path.name}")
        if not RESULTS[-1]["ok"]:
            return finish(proc, log_file)

        inspected = http_post("/projects/inspect", {"root_path": str(book)})
        project_id = inspected["project"]["project_id"]
        check("小书扫描上架", inspected["project"]["display_name"] == "测试书_青云剑", project_id)

        # —— 路线 2：蒸馏（不调模型，先验确定性链）——
        story = "《测试书_青云剑》第一章：林凡下山。" * 60 + "\n主角在第一章立住「少年下山」的钩子。"
        distill_msg = f"请把下面这本书蒸馏入库：\n{story}"
        r2 = http_post(f"/projects/{project_id}/chat", {"message": distill_msg}, timeout=180)
        ok2 = r2.get("ok") and ("草稿" in r2.get("content", "") or "蒸馏" in r2.get("content", ""))
        check("蒸馏触发", bool(ok2), f"buddy_tools={r2.get('buddy_tools')}")
        print("---- 蒸馏回复前 400 字 ----")
        print(r2.get("content", "")[:400], flush=True)

        # —— 路线 3：入库 ——
        r3 = http_post(f"/projects/{project_id}/chat", {"message": "入库"}, timeout=120)
        c3 = r3.get("content", "")
        ok3 = (
            r3.get("ok")
            and "distillation_commit" in (r3.get("buddy_tools") or [])
            and ("入库" in c3 or "写入" in c3)
        )
        check("入库触发", bool(ok3), f"buddy_tools={r3.get('buddy_tools')}")
        print("---- 入库回复前 400 字 ----")
        print(r3.get("content", "")[:400], flush=True)
        stats2 = subprocess.run(
            [PY, str(DISTILL_KB), "stats", str(lib_dir)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        check("临时库卡片增加", stats2.returncode == 0, stats2.stdout.strip()[:160])

        # —— 路线 1：报书名搜索（普通消息，会调真实模型）——
        t0 = time.time()
        r1 = http_post(f"/projects/{project_id}/chat", {"message": "你知道《测试书_青云剑》吗？"}, timeout=120)
        cost = time.time() - t0
        check("报书名搜索", bool(r1.get("ok")), f"{cost:.1f}s, buddy_tools={r1.get('buddy_tools')}")
        print("---- 搜索回复前 500 字 ----")
        print(r1.get("content", "")[:500], flush=True)
    finally:
        finish(proc, log_file)
    return 0 if all(item["ok"] for item in RESULTS) else 1


def finish(proc, log_file) -> int:
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    log_file.close()
    print(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    return 0 if all(item["ok"] for item in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
