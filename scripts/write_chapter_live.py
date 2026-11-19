"""真实模型写一章正文，并经作者确认后真正落盘。

这条脚本把此前互相分离的两段打通成一次运行：
  1) 真实 Provider 产出正文候选   （a1fc724 已证明可行，但止步于 candidate）
  2) ChangeSet 审阅 -> apply 落盘 （author-prose-save-live 已证明可行，但写的是标记文本）

两段以前从未在同一次运行里连起来，导致「系统从没写出过一句小说」。

边界（与既有架构一致，未削弱）：
  * 复用 Runtime 既有 HTTP 合同，不新建第二套写回通道（DRY）。
  * 走 origin=manual 的**作者审阅**路径：模型只产出候选，
    必须由作者在本脚本里显式确认（y）才 apply。
    这正是 formal_draft_allowed=false 的本意——禁止 Agent 绕过作者，
    而不是禁止正文存在。
  * apply 由 Runtime 完成，自带备份 / writeback journal / revision 递增，可回滚。

用法：
    python scripts/write_chapter_live.py --api-key-env ZM_KEY \
        --base-url https://api.example.com/v1 --model your-model-name
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "services" / "writing-runtime" / "tests" / "fixtures" / "minimal-novel"
STAMP = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

# 章节正文所在的规范目录（四套 Skills 的 01-09 约定）
PROSE_DIR = "05_正文内容"


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def log(message: str) -> None:
    print(message, flush=True)


def http_json(method: str, url: str, body=None, timeout: float = 60.0):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_ready(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            status, payload = http_json("GET", url, timeout=3)
            if status == 200:
                return
            last = (status, payload)
        except Exception as exc:  # noqa: BLE001 - 启动期任何异常都只是「还没起来」
            last = str(exc)
        time.sleep(0.4)
    raise RuntimeError(f"Runtime 未能就绪: {url} last={last}")


# --------------------------------------------------------------------------
# 上下文组装：读项目真值
# --------------------------------------------------------------------------
def collect_truth(project: Path, limit_chars: int = 6000) -> str:
    src = str(ROOT / "services" / "writing-runtime" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from zhimeng_runtime.chapter_draft_service import _read_truth
    return _read_truth(project, limit_chars=limit_chars)


def find_chapters(project: Path) -> list[Path]:
    """返回按章号排序的正文文件（排除续写卡等非正文）。"""
    prose_root = project / PROSE_DIR
    if not prose_root.is_dir():
        return []
    found = [
        path
        for path in prose_root.rglob("CH*.md")
        if "正文" in path.stem or path.stem.upper().startswith("CH")
    ]
    # 续写卡 / 大纲卡不是正文
    found = [path for path in found if "卡" not in path.stem]
    return sorted(found)


def previous_prose(project: Path, tail_chars: int = 1800) -> tuple[str, str]:
    chapters = find_chapters(project)
    if not chapters:
        return "", ""
    last = chapters[-1]
    try:
        text = last.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return last.name, ""
    return last.name, text[-tail_chars:]


# --------------------------------------------------------------------------
# 真实模型调用
# --------------------------------------------------------------------------
def build_prompt(project: Path, target_words: int) -> tuple[str, str]:
    truth = collect_truth(project)
    prev_name, prev_text = previous_prose(project)

    system = (
        "你是一位资深中文长篇小说写手。你只输出小说正文，"
        "不输出任何解释、标题编号说明、markdown 代码块或写作建议。"
        "叙述克制具体，贴着本书已测到的写法偏斜，不要回到通用书面平均腔。"
        "不要每段都以人名开头，不要写总结性议论收尾。"
    )

    parts = [
        "以下是这本书的项目真值资料，请严格遵守其中的世界观、人物设定与文风规则。",
        "",
        truth if truth else "（暂无补充资料）",
        "",
    ]
    if prev_text:
        parts += [
            f"以下是上一章（{prev_name}）的结尾，请自然承接，不要重复已写过的内容：",
            "",
            prev_text,
            "",
        ]
    parts += [
        f"请续写下一章正文，约 {target_words} 字。",
        "要求：",
        "1. 第一行是章节标题，格式为「# 第X章 标题」。",
        "2. 正文要有具体的场景、动作和对话，推进剧情，不要原地打转。",
        "3. 结尾留一个让读者想继续看下去的钩子。",
        "4. 只输出正文本身。",
    ]
    return system, "\n".join(parts)


def call_model(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    timeout: float,
) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.load(response)
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("模型返回空正文")
    return content.strip()


def clean_prose(text: str) -> str:
    """去掉模型偶尔加的 markdown 围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def next_chapter_path(project: Path) -> tuple[str, int]:
    """决定新章节的相对路径与章号。"""
    chapters = find_chapters(project)
    numbers: list[int] = []
    for path in chapters:
        digits = "".join(ch for ch in path.stem if ch.isdigit())
        if digits:
            numbers.append(int(digits))
    number = (max(numbers) + 1) if numbers else 1

    if chapters:
        parent = chapters[-1].parent.relative_to(project).as_posix()
    else:
        parent = f"{PROSE_DIR}/第一卷"
    return f"{parent}/CH{number:03d}_正文.md", number


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型写一章并落盘")
    parser.add_argument("--project", default=None, help="小说项目根目录；缺省用测试样例的副本")
    parser.add_argument("--base-url", default=os.environ.get("ZM_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("ZM_MODEL", ""))
    parser.add_argument("--api-key-env", default="ZM_KEY", help="存放 API key 的环境变量名")
    parser.add_argument("--words", type=int, default=2200)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=420.0)
    parser.add_argument("--yes", action="store_true", help="跳过确认，直接落盘")
    args = parser.parse_args()

    # 命令行 > 环境变量 > 本地配置文件（本地配置文件不进版本管理）
    config_path = ROOT / "本地模型配置.json"
    config: dict[str, str] = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log(f"[!] 本地模型配置.json 读取失败，已忽略: {exc}")

    base_url = args.base_url or config.get("base_url", "")
    model = args.model or config.get("model", "")
    api_key = os.environ.get(args.api_key_env, "").strip() or config.get("api_key", "").strip()
    project_override = args.project or config.get("project", "")

    if not api_key:
        log(f"[X] 没有找到 API key。请设置环境变量 {args.api_key_env}，")
        log(f"    或在 {config_path} 里填写 api_key。")
        return 2
    if not base_url or not model:
        log("[X] 缺少 base_url 或 model。请用 --base-url/--model，")
        log(f"    或在 {config_path} 里填写。")
        return 2

    args.base_url = base_url
    args.model = model
    args.project = project_override or None

    work = ROOT / ".runtime" / f"chapter-live-{STAMP}"
    evidence_path = ROOT / "evidence" / f"chapter-live-{STAMP}.json"

    # 1. 准备项目
    if args.project:
        project = Path(args.project).resolve()
        library = project.parent
        if not project.is_dir():
            log(f"[X] 项目目录不存在: {project}")
            return 2
    else:
        library = work / "library"
        project = library / "minimal-novel"
        library.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURE, project)
        log(f"[i] 未指定项目，已复制样例到: {project}")

    work.mkdir(parents=True, exist_ok=True)
    database = work / "runtime.sqlite3"
    port = free_port()
    runtime_url = f"http://127.0.0.1:{port}"

    result: dict[str, object] = {
        "schema": "zhimeng.chapter-live.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_url": runtime_url,
        "project_root": str(project),
        "base_url": args.base_url,
        "model": args.model,
        "ok": False,
    }

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(ROOT / "services" / "writing-runtime" / "src"),
            "ZHIMENG_ENVIRONMENT": "development",
            "ZHIMENG_HOST": "127.0.0.1",
            "ZHIMENG_PORT": str(port),
            "ZHIMENG_DATABASE_PATH": str(database),
            "ZHIMENG_TRUSTED_PROJECT_ROOTS": str(library),
        }
    )
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)

    out_log = (work / "runtime.stdout.log").open("w", encoding="utf-8")
    err_log = (work / "runtime.stderr.log").open("w", encoding="utf-8")
    runtime = subprocess.Popen(
        [str(python), "-m", "zhimeng_runtime"],
        cwd=str(ROOT),
        env=env,
        stdout=out_log,
        stderr=err_log,
    )

    try:
        log("[1/6] 启动 Runtime ...")
        wait_ready(f"{runtime_url}/health/ready")

        log("[2/6] 读取项目真值 ...")
        status, snapshot = http_json(
            "POST", f"{runtime_url}/api/v1/projects/inspect", {"root_path": str(project)}
        )
        if status != 200:
            raise RuntimeError(f"inspect 失败: {status} {snapshot}")
        project_id = snapshot["project"]["project_id"]

        relative_path, chapter_number = next_chapter_path(project)
        result["target_relative_path"] = relative_path
        result["chapter_number"] = chapter_number

        log(f"[3/6] 调用真实模型写第 {chapter_number} 章（约 {args.words} 字）...")
        system_prompt, user_prompt = build_prompt(project, args.words)
        started = time.time()
        prose = clean_prose(
            call_model(
                args.base_url,
                api_key,
                args.model,
                system_prompt,
                user_prompt,
                args.max_tokens,
                args.timeout,
            )
        )
        elapsed = round(time.time() - started, 1)
        result["provider_seconds"] = elapsed
        result["prose_characters"] = len(prose)
        log(f"    模型返回 {len(prose)} 字，用时 {elapsed}s")

        # 落盘前把候选写到工作区，便于作者查看/留证
        draft_path = work / "draft.md"
        draft_path.write_text(prose + "\n", encoding="utf-8")
        result["draft_path"] = str(draft_path)

        log("")
        log("=" * 60)
        log(prose[:700])
        if len(prose) > 700:
            log(f"...（共 {len(prose)} 字，全文见 {draft_path}）")
        log("=" * 60)
        log("")

        if not args.yes:
            answer = input("确认把这一章写入小说项目？(y/N) ").strip().lower()
            if answer not in {"y", "yes"}:
                result["author_decision"] = "rejected"
                log("[i] 作者未确认，项目文件一个字节都没有改动。")
                evidence_path.parent.mkdir(parents=True, exist_ok=True)
                evidence_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                return 0
        result["author_decision"] = "approved"

        log("[4/6] 生成 ChangeSet ...")
        status, created = http_json(
            "POST",
            f"{runtime_url}/api/v1/projects/{project_id}/change-sets",
            {
                "base_revision": snapshot["project"]["revision"],
                "base_fingerprint": snapshot["project"]["fingerprint"],
                "title": f"第 {chapter_number} 章正文",
                "summary": f"由 {args.model} 生成，作者已确认",
                "origin": "manual",
                "gate_required": False,
                "changes": [
                    {
                        "operation": "create",
                        "relative_path": relative_path,
                        "after_content": prose + "\n",
                        "rationale": f"real provider prose via {args.model}",
                    }
                ],
            },
        )
        if status != 201:
            raise RuntimeError(f"创建 ChangeSet 失败: {status} {created}")
        change_set_id = created["change_set_id"]
        result["change_set_id"] = change_set_id

        log("[5/6] 作者确认全部改动 ...")
        decisions = [
            {"hunk_id": hunk["hunk_id"], "decision": "accepted"}
            for change in created["changes"]
            for hunk in change["hunks"]
        ]
        status, ready = http_json(
            "PATCH",
            f"{runtime_url}/api/v1/change-sets/{change_set_id}/decisions",
            {"expected_review_revision": created["review_revision"], "decisions": decisions},
        )
        if status != 200:
            raise RuntimeError(f"审阅决定失败: {status} {ready}")

        log("[6/6] 写回磁盘 ...")
        status, applied = http_json(
            "POST",
            f"{runtime_url}/api/v1/change-sets/{change_set_id}/apply",
            {"expected_review_revision": ready["review_revision"]},
            timeout=180,
        )
        if status != 200:
            raise RuntimeError(f"apply 失败: {status} {applied}")

        landed = project / relative_path
        on_disk = landed.read_text(encoding="utf-8") if landed.is_file() else ""
        result.update(
            {
                "apply_status": applied.get("status"),
                "landed_path": str(landed),
                "landed_characters": len(on_disk),
                "disk_matches_model_output": on_disk.strip() == prose.strip(),
                "ok": bool(
                    applied.get("status") == "applied"
                    and landed.is_file()
                    and on_disk.strip() == prose.strip()
                ),
            }
        )

        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        log("")
        if result["ok"]:
            log("[OK] 这一章已经真实写进小说项目。")
            log(f"     正文文件: {landed}")
            log(f"     字数    : {len(on_disk)}")
            log(f"     证据    : {evidence_path}")
            log("     如需撤销：Runtime 已保存备份与 writeback journal，可回滚。")
        else:
            log("[X] 落盘校验未通过，详见证据文件。")
        return 0 if result["ok"] else 1

    except Exception as exc:  # noqa: BLE001 - 统一收敛为可读报告
        result["error"] = str(exc)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log(f"[X] 失败: {exc}")
        log(f"    证据: {evidence_path}")
        return 1
    finally:
        if runtime.poll() is None:
            runtime.terminate()
            try:
                runtime.wait(timeout=10)
            except subprocess.TimeoutExpired:
                runtime.kill()
        out_log.close()
        err_log.close()


if __name__ == "__main__":
    raise SystemExit(main())
