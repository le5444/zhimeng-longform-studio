from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.memory_files import (
    memory_enabled, read_user_memory, user_memory_path, user_memory_transaction, write_user_memory,
)


class MemoryChange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["add", "replace", "remove"] = "add"
    text: str = Field(default="", max_length=500)
    old_text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action != "remove" and not self.text.strip():
            raise ValueError("New memory text is required")
        if self.action == "remove" and self.text:
            raise ValueError("remove uses old_text; text must be empty")
        if self.action == "add" and self.old_text is not None:
            raise ValueError("add does not use old_text")
        if self.action != "add" and not (self.old_text or "").strip():
            raise ValueError("Copy the existing entry into old_text")
        if any("\n" in value or "\r" in value for value in (self.text, self.old_text or "")):
            raise ValueError("Memory operations address one entry at a time")
        return self


class MemoryProposal(MemoryChange):
    source_message_id: str = Field(min_length=1)
    evidence: str = Field(min_length=1, max_length=2000)


class MemoryExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    changes: list[MemoryProposal] = Field(max_length=12)


def parse_memory_proposals(text: str, sources: dict[str, str]) -> list[MemoryProposal]:
    value = text.strip()
    if value.startswith("```") and value.endswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    proposals = MemoryExtraction.model_validate_json(value).changes
    for proposal in proposals:
        source = sources.get(proposal.source_message_id)
        if source is None or proposal.evidence not in source:
            raise ValueError("Memory evidence does not match an original user message")
    return proposals


def _entry(line: str) -> tuple[str, str, str]:
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    match = re.fullmatch(r"(\s*(?:[-*]\s+)?)(.*)", body)
    assert match is not None
    return match[1], match[2].strip(), ending


def apply_memory_changes(existing: str, changes: list[MemoryChange], *, project: bool = False) -> tuple[str, list[dict]]:
    lines = existing.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in existing else "\n"
    applied = []
    for change in changes:
        entries = [_entry(line)[1] for line in lines]
        text = change.text.strip()
        old = (change.old_text or "").strip()
        if change.action == "add":
            if text in entries:
                continue
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            if not lines and project:
                lines.append("## 记住的事" + newline + newline)
            lines.append(("- " if project else "") + text + newline)
        else:
            matches = [index for index, entry in enumerate(entries) if entry == old]
            if len(matches) != 1:
                raise ValueError("Existing memory entry is missing or ambiguous; read it again before updating")
            index = matches[0]
            if change.action == "replace":
                if old == text:
                    continue
                prefix, _, ending = _entry(lines[index])
                if text in entries:
                    del lines[index]
                else:
                    lines[index] = prefix + text + ending
            else:
                del lines[index]
        applied.append(change.model_dump(mode="json"))
    content = "".join(lines)
    return content, applied if content != existing else []


def update_user_memory(state_root: Path, changes: list[MemoryChange], *, expected: str | None = None):
    with user_memory_transaction(state_root):
        if not memory_enabled(state_root):
            raise ValueError("User memory is disabled")
        current = read_user_memory(state_root, strict=True)
        if expected is not None and current != expected:
            return user_memory_path(state_root), [], "memory_changed"
        content, applied = apply_memory_changes(current, changes)
        path = write_user_memory(state_root, content) if content != current else user_memory_path(state_root)
        return path, applied, None
