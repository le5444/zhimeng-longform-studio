"""Refresh only the public reasoning payload in the canonical execution contract."""
from __future__ import annotations

import json
from pathlib import Path

from zhimeng_runtime.execution_protocol import ReasoningPayload


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "packages/contracts/execution-protocol.schema.json"
    original = path.read_bytes()
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8").replace("\r\n", "\n")
    start = text.index('    "reasoningPayload": {')
    end = text.index('    "toolCallPayload": {', start)
    schema = ReasoningPayload.model_json_schema()
    block = json.dumps({"reasoningPayload": schema}, ensure_ascii=False, indent=2)
    lines = block.splitlines()[1:-1]
    replacement = "\n".join("  " + line for line in lines) + ",\n"
    updated = (text[:start] + replacement + text[end:]).replace("\n", newline).encode("utf-8")
    json.loads(updated)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_bytes(updated)
    temporary.replace(path)


if __name__ == "__main__":
    main()
