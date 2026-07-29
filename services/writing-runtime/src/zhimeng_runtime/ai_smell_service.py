"""调用 novel-creation-suite 的 detect_ai_smell.py，给稿子打「AI 味」分。

为什么是 subprocess 而不是重写：

* `detect_ai_smell.py` 是 5311 行纯标准库的确定性规则引擎，作者已经用了很久。
  重写等于造第二套评分标准，违反 DRY，而且分数不可比。
* 它输出带 `content_hash` 的 JSON，天然适合做证据绑定。

路径策略（按优先级）：
  1. 显式传入 / 设置里配置
  2. 环境变量 `ZHIMENG_AI_SMELL_SCRIPT`
  3. 自动探测 `~/.codex/skills/novel-creation-suite/scripts/detect_ai_smell.py`

⚠️ 打包成 EXE 前必须把脚本纳入受管快照，不能继续依赖用户机器上的 .codex 路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RELATIVE = Path(".codex/skills/novel-creation-suite/scripts/detect_ai_smell.py")
DEFAULT_TIMEOUT_SECONDS = 120


class AiSmellError(RuntimeError):
    code = "ai_smell_failed"


class AiSmellUnavailable(AiSmellError):
    code = "ai_smell_unavailable"


@dataclass(frozen=True, slots=True)
class AiSmellReport:
    score: float
    level: str
    content_hash: str
    detector_version: str
    #  只保留分数最高（最像 AI）的几个维度，作者不需要看全部 30 个 0.0
    top_components: tuple[tuple[str, float], ...]

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "level": self.level,
            "content_hash": self.content_hash,
            "detector_version": self.detector_version,
            "top_components": [
                {"name": name, "value": value} for name, value in self.top_components
            ],
        }


def resolve_script(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    for candidate in (
        explicit,
        os.environ.get("ZHIMENG_AI_SMELL_SCRIPT"),
        Path.home() / DEFAULT_RELATIVE,
    ):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file():
            return path
    return None


class AiSmellService:
    def __init__(
        self,
        *,
        script_path: str | os.PathLike[str] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._explicit = script_path
        self._timeout = timeout_seconds

    @property
    def script(self) -> Path | None:
        return resolve_script(self._explicit)

    @property
    def available(self) -> bool:
        return self.script is not None

    def evaluate_text(self, text: str, *, genre: str | None = None) -> AiSmellReport:
        script = self.script
        if script is None:
            raise AiSmellUnavailable(
                "没找到 AI 味检测脚本 detect_ai_smell.py。"
                "可设置环境变量 ZHIMENG_AI_SMELL_SCRIPT 指向它。"
            )

        with tempfile.TemporaryDirectory(prefix="zhimeng-ai-smell-") as workspace:
            draft = Path(workspace) / "draft.md"
            draft.write_text(text, encoding="utf-8")
            output = Path(workspace) / "report.json"

            command = [
                sys.executable,
                str(script),
                str(draft),
                "--top",
                "3",
                "--output",
                str(output),
            ]
            if genre:
                command += ["--genre", genre]

            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    timeout=self._timeout,
                    # 检测器只读稿子、算规则分，不需要网络也不该继承调用者的密钥
                    env={
                        k: v
                        for k, v in os.environ.items()
                        if not k.startswith(("ZHIMENG_AGENT_PROVIDER", "ANTHROPIC_", "OPENAI_"))
                    },
                )
            except subprocess.TimeoutExpired as exc:
                raise AiSmellError("AI 味检测超时") from exc
            except OSError as exc:
                raise AiSmellError(f"无法运行 AI 味检测脚本: {exc}") from exc

            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace")[:300]
                raise AiSmellError(f"AI 味检测失败（退出码 {completed.returncode}）: {detail}")

            try:
                payload = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AiSmellError("AI 味检测没有产出可解析的结果") from exc

        overall = payload.get("overall_score")
        if not isinstance(overall, dict):
            raise AiSmellError("AI 味检测结果缺少 overall_score")

        components = overall.get("components")
        ranked: list[tuple[str, float]] = []
        if isinstance(components, dict):
            numeric = [
                (str(name), float(value))
                for name, value in components.items()
                if isinstance(value, (int, float))
            ]
            ranked = sorted(numeric, key=lambda item: item[1], reverse=True)[:3]

        return AiSmellReport(
            score=float(overall.get("score", 0.0)),
            level=str(overall.get("level", "unknown")),
            content_hash=str(payload.get("content_hash", "")),
            detector_version=str((payload.get("detector") or {}).get("version", "")),
            top_components=tuple(ranked),
        )
