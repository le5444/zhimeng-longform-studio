"""Read project instructions with per-file evidence, separate from user memory."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from zhimeng_runtime.text_encoding import decode_project_text

PROJECT_MEMORY_FILE_NAME = "AGENTS.md"
LEGACY_PROJECT_NOTES_FILE_NAME = "写作笔记.md"
PROJECT_INSTRUCTION_FILE_NAMES = (PROJECT_MEMORY_FILE_NAME, "CLAUDE.md", LEGACY_PROJECT_NOTES_FILE_NAME)
MAX_PROJECT_INSTRUCTION_BYTES = 32 * 1024


@dataclass(frozen=True)
class ProjectInstructionSnapshot:
    files: tuple[tuple[str, str], ...] = ()
    states: tuple[tuple[str, str], ...] = ()
    available: bool = True
    project_id: str | None = None

    def metadata(self) -> dict[str, Any]:
        # Content remains in the existing project_instructions artifact field.
        return {
            "version": 1,
            "project_id": self.project_id,
            "available": self.available,
            "states": dict(self.states),
        }

    @classmethod
    def from_metadata(
        cls, files: Sequence[Sequence[str]], metadata: Mapping[str, Any]
    ) -> ProjectInstructionSnapshot:
        states = metadata.get("states")
        if metadata.get("version") != 1 or not isinstance(states, dict):
            return cls(available=False)
        return cls(
            files=tuple((str(name), str(content)) for name, content in files),
            states=tuple((str(name), str(state)) for name, state in states.items()),
            available=metadata.get("available") is True,
            project_id=metadata.get("project_id"),
        )

    @property
    def unreliable(self) -> bool:
        return not self.available or any(state not in {"read", "empty", "missing"} for _, state in self.states)

    def message(
        self, *, current: bool = False, missing_names: Sequence[str] = ()
    ) -> dict[str, object] | None:
        prefix = "这个文件夹里的说明文件"
        if not self.available:
            return {"role": "user", "content": prefix + "暂时无法核对：项目目录或绑定无法读取，不能确认文件是否存在；历史内容不代表当前状态。"}
        contents = dict(self.files)
        blocks: list[str] = []
        for name, state in self.states:
            if state == "read" and name in contents:
                blocks.append(f"── {name} ──\n{contents[name]}")
            elif state == "empty":
                blocks.append(f"{name}：文件存在，内容为空。")
            elif state == "missing":
                if current and name in missing_names:
                    blocks.append(f"{name}：本轮检查时不存在。")
            else:
                reason = "内容不是有效的受支持文本" if state == "invalid_text" else "发生读取错误"
                blocks.append(f"{name}：暂时无法读取（{reason}），不能确认最新内容，也不能认定已删除；历史内容是先前快照。")
        if not blocks:
            if not current:
                return None
            blocks.append("本轮检查未找到说明文件。" if self.states else "当前对话未关联文件夹，历史项目说明属于先前的工作区。")
        return {"role": "user", "content": prefix + ("当前读取结果：\n\n" if current else "：\n\n") + "\n\n".join(blocks)}


def read_project_instruction_snapshot(root: Path, *, project_id: str | None = None) -> ProjectInstructionSnapshot:
    root = Path(root)
    try:
        if not stat.S_ISDIR(root.stat().st_mode):
            return ProjectInstructionSnapshot(available=False, project_id=project_id)
    except OSError:
        return ProjectInstructionSnapshot(available=False, project_id=project_id)
    files: list[tuple[str, str]] = []
    states: list[tuple[str, str]] = []
    for name in PROJECT_INSTRUCTION_FILE_NAMES:
        try:
            with (root / name).open("rb") as handle:
                raw = handle.read(MAX_PROJECT_INSTRUCTION_BYTES + 1)
        except FileNotFoundError:
            states.append((name, "missing"))
            continue
        except OSError:
            states.append((name, "unreadable"))
            continue
        truncated = len(raw) > MAX_PROJECT_INSTRUCTION_BYTES
        try:
            text, _encoding, _bom = decode_project_text(raw[:MAX_PROJECT_INSTRUCTION_BYTES], final=not truncated)
        except ValueError:
            states.append((name, "invalid_text"))
            continue
        text = text.replace("\r\n", "\n").strip()
        if truncated:
            text += "\n（这份文件后面还有，没有带上；要看全文就读这个文件。）"
        states.append((name, "read" if text else "empty"))
        if text:
            files.append((name, text))
    # A directory disappearing during the reads must not turn all files into
    # apparently confirmed deletions.
    try:
        available = stat.S_ISDIR(root.stat().st_mode)
    except OSError:
        available = False
    return ProjectInstructionSnapshot(tuple(files), tuple(states), available, project_id)


def read_project_instructions(root: Path) -> list[tuple[str, str]]:
    """Compatibility content view; runtime context uses the evidence snapshot."""
    return list(read_project_instruction_snapshot(root).files)
