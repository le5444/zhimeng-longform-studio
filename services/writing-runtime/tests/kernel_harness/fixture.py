from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kernel_harness.models import HarnessCase, HarnessSeedFile
from zhimeng_runtime.project_reader import ProjectReader


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError as exc:
        raise ValueError(f"cannot inspect harness fixture path: {path}") from exc
    return bool(attributes & 0x400)


def _resolve_fixture_member(
    fixture_root: Path,
    relative_path: str,
    *,
    kind: str,
) -> Path:
    candidate = fixture_root / Path(relative_path)
    if _is_link_or_reparse(candidate):
        raise ValueError(f"harness fixture {kind} cannot be a link: {relative_path}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(fixture_root)
    except ValueError as exc:
        raise ValueError(
            f"harness fixture {kind} escapes the case root: {relative_path}"
        ) from exc
    return resolved


def fixture_fingerprint(files: list[HarnessSeedFile]) -> str:
    material = [
        {
            "path": item.path,
            "role": item.role.value,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in sorted(files, key=lambda item: item.path)
    ]
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def tree_fingerprint(root: Path) -> str:
    root = root.resolve(strict=True)
    material: list[dict[str, object]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if _is_link_or_reparse(path):
            raise ValueError(f"harness fixture cannot contain links: {path}")
        if not path.is_file():
            continue
        stat_result = path.stat()
        if int(getattr(stat_result, "st_nlink", 1)) > 1:
            raise ValueError(f"harness fixture cannot contain hardlinked files: {path}")
        material.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": stat_result.st_size,
            }
        )
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def verify_case_fixture(case_file: Path, case: HarnessCase) -> None:
    fixture_root = case_file.resolve(strict=True).parent
    if fixture_fingerprint(case.fixture.files) != case.fixture_fingerprint:
        raise ValueError("declared harness fixture fingerprint is stale")
    declared_paths = {item.path for item in case.fixture.files}
    checked_roots = {
        case.fixture.project_root,
        case.fixture.references_root,
        case.fixture.skills_root,
        case.fixture.expected_root,
    }
    actual_paths: set[str] = set()
    for relative_root in checked_roots:
        root = _resolve_fixture_member(
            fixture_root,
            relative_root,
            kind="root",
        )
        if not root.is_dir():
            raise ValueError(f"harness fixture root is not a regular directory: {relative_root}")
        for path in root.rglob("*"):
            relative = path.relative_to(fixture_root)
            if any(part.casefold() == ".git" for part in relative.parts):
                raise ValueError("harness fixture project cannot contain Git metadata")
            if _is_link_or_reparse(path):
                raise ValueError(f"harness fixture cannot contain links: {path}")
            if path.is_file():
                actual_paths.add(path.relative_to(fixture_root).as_posix())
    if actual_paths != declared_paths:
        raise ValueError("fixture files do not match the declared inventory")
    for expected in case.fixture.files:
        path = _resolve_fixture_member(
            fixture_root,
            expected.path,
            kind="file",
        )
        if not path.is_file():
            raise ValueError(f"fixture seed must be a regular file: {expected.path}")
        stat_result = path.stat()
        if int(getattr(stat_result, "st_nlink", 1)) > 1:
            raise ValueError(f"harness fixture cannot contain hardlinked files: {expected.path}")
        if stat_result.st_size != expected.size_bytes:
            raise ValueError(f"fixture size mismatch: {expected.path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected.sha256:
            raise ValueError(f"fixture hash mismatch: {expected.path}")


@dataclass(frozen=True, slots=True)
class HarnessWorkspace:
    root: Path
    project: Path
    references: Path
    skills: Path
    staging: Path
    expected: Path
    source_fixture_fingerprint: str
    project_snapshot: dict[str, object]
    git_head: str


def _initialize_disposable_repository(project: Path) -> str:
    if (project / ".git").exists():
        raise ValueError("disposable project must not contain pre-existing Git metadata")
    git_environment = dict(os.environ)
    for key in list(git_environment):
        if key.upper().startswith("GIT_"):
            del git_environment[key]
    git_environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    commands = [
        ["git", "-c", "init.templateDir=", "init", "-q"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "core.hooksPath=.git/no-hooks",
            "-c",
            "user.name=Zhimeng Kernel Harness",
            "-c",
            "user.email=kernel-harness@zhimeng.local",
            "commit",
            "-q",
            "-m",
            "harness baseline",
        ],
    ]
    for command in commands:
        result = subprocess.run(
            command,
            cwd=project,
            env=git_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"cannot initialize disposable harness Git repository: {result.stderr.strip()}"
            )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        env=git_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()
    return head


def prepare_disposable_fixture(
    case_file: Path,
    case: HarnessCase,
    destination: Path,
) -> HarnessWorkspace:
    verify_case_fixture(case_file, case)
    fixture_root = case_file.resolve(strict=True).parent
    destination = destination.resolve(strict=False)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("harness destination must be absent or empty")
    destination.mkdir(parents=True, exist_ok=True)

    roots = {
        "project": case.fixture.project_root,
        "references": case.fixture.references_root,
        "skills": case.fixture.skills_root,
        "expected": case.fixture.expected_root,
    }
    copied: dict[str, Path] = {}
    for name, relative in roots.items():
        source = _resolve_fixture_member(
            fixture_root,
            relative,
            kind="root",
        )
        if not source.is_dir():
            raise ValueError(f"harness fixture root is not a regular directory: {relative}")
        target = destination / relative
        shutil.copytree(source, target, dirs_exist_ok=False)
        copied[name] = target

    staging = destination / case.fixture.staging_root
    staging.mkdir(parents=True, exist_ok=False)
    git_head = _initialize_disposable_repository(copied["project"])
    project_reader = ProjectReader([copied["project"]])
    project_snapshot = project_reader.inspect(copied["project"])
    return HarnessWorkspace(
        root=destination,
        project=copied["project"],
        references=copied["references"],
        skills=copied["skills"],
        staging=staging,
        expected=copied["expected"],
        source_fixture_fingerprint=case.fixture_fingerprint,
        project_snapshot=project_snapshot,
        git_head=git_head,
    )
