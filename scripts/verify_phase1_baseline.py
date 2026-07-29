from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    "README.md",
    "docs/CURRENT_STATE_AUDIT.md",
    "docs/ARCHITECTURE.md",
    "docs/FOUR_SKILLS_CAPABILITY_MAP.md",
    "docs/DEVELOPMENT_BRIEF.md",
    "legacy/LEGACY_MANIFEST.md",
    "packages/contracts/health.schema.json",
    "packages/contracts/runtime-status.schema.json",
    "packages/contracts/run-event.schema.json",
    "packages/contracts/error-envelope.schema.json",
    "packages/contracts/skill-manifest.schema.json",
]

SCHEMA_FILES = [path for path in REQUIRED_FILES if path.endswith(".schema.json")]

NEW_CODE_ROOTS = [
    ROOT / "apps",
    ROOT / "services",
    ROOT / "packages",
    ROOT / "adapters",
]

FORBIDDEN_WRITABLE_REFERENCES = [
    "Desktop\\相关\\织梦写作台",
    "Desktop/相关/织梦写作台",
]


def main() -> int:
    failures: list[str] = []

    for relative in REQUIRED_FILES:
        path = ROOT / relative
        if not path.is_file():
            failures.append(f"missing required file: {relative}")

    for relative in SCHEMA_FILES:
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"invalid JSON schema {relative}: {error}")
            continue
        if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            failures.append(f"unexpected schema dialect: {relative}")
        if not value.get("$id") or not value.get("title"):
            failures.append(f"schema identity missing: {relative}")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "当前磁盘事实" not in readme or "Phase 1" not in readme:
        failures.append("README must state the current Phase 1 baseline")
    if "旧 `bridge/writing_runtime.py` 仍然残缺" not in readme:
        failures.append("README must keep the legacy runtime boundary explicit")

    legacy = (ROOT / "legacy/LEGACY_MANIFEST.md").read_text(encoding="utf-8")
    if "不代表当前产品 Runtime" not in legacy:
        failures.append("legacy manifest does not protect the old runtime boundary")

    suffixes = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".toml", ".yaml", ".yml"}
    for code_root in NEW_CODE_ROOTS:
        if not code_root.exists():
            continue
        for path in code_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in suffixes:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for marker in FORBIDDEN_WRITABLE_REFERENCES:
                if marker in content:
                    failures.append(f"new code contains old-project path: {path.relative_to(ROOT)}")

    if failures:
        print("PHASE1_BASELINE_FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("PHASE1_BASELINE_OK")
    print(f"root={ROOT}")
    print(f"schemas={len(SCHEMA_FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
