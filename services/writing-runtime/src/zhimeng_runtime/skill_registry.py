from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from zhimeng_runtime.skill_models import (
    DiscoveredSkill,
    SkillDiagnostic,
    SkillRegistryResponse,
    SkillRootInventory,
    SkillRootSnapshot,
    SkillScanLimits,
    SkillSource,
)


MAX_SCAN_ENTRIES: Final = 50_000
MAX_SCAN_DIRECTORIES: Final = 5_000
MAX_SCAN_DEPTH: Final = 32
MAX_SKILL_MANIFESTS: Final = 2_000
MAX_MANIFEST_BYTES: Final = 512 * 1024
MAX_TOTAL_MANIFEST_BYTES: Final = 16 * 1024 * 1024
MAX_YAML_NODES: Final = 512
MAX_YAML_DEPTH: Final = 64
REPARSE_POINT_ATTRIBUTE: Final = 0x400
SKILL_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

# These are content/cache trees, not places where the four writing systems keep
# their nested instruction skills. Pruning them is what makes discovery safe for
# the 2.2 GB distillation package: only SKILL.md manifests are ever read.
DEFAULT_EXCLUDED_DIRECTORIES: Final = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".svn",
        ".venv",
        ".vscode",
        "__pycache__",
        "archive",
        "archives",
        "artifacts",
        "backup",
        "backups",
        "build",
        "cache",
        "data",
        "dist",
        "downloads",
        "library",
        "libraries",
        "node_modules",
        "output",
        "outputs",
        "temp",
        "tmp",
        "venv",
        "下载",
        "备份",
        "归档",
        "语料库",
    }
)


class SkillRegistryError(RuntimeError):
    """Base error for read-only skill discovery."""


class UntrustedSkillPathError(SkillRegistryError):
    """A configured or discovered path violates the trusted-root boundary."""


class SkillScanLimitError(SkillRegistryError):
    """A trusted root exceeded an explicit discovery budget."""


class SkillManifestChangedError(SkillRegistryError):
    """A manifest changed while its immutable revision was being read."""


@dataclass(slots=True)
class _Inventory:
    directories_visited: int = 0
    entries_seen: int = 0
    excluded_directories: int = 0
    manifests_found: int = 0
    manifests_loaded: int = 0
    manifest_bytes_read: int = 0


class _ManifestSafeLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key, alias and node-count defenses."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._node_count = 0
        self._compose_depth = 0

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise ComposerError(
                None,
                None,
                "YAML aliases are not allowed in a skill manifest",
                event.start_mark,
            )
        self._node_count += 1
        if self._node_count > MAX_YAML_NODES:
            event = self.peek_event()
            raise ComposerError(
                None,
                None,
                f"skill manifest exceeds the {MAX_YAML_NODES} YAML node limit",
                event.start_mark,
            )
        self._compose_depth += 1
        try:
            if self._compose_depth > MAX_YAML_DEPTH:
                event = self.peek_event()
                raise ComposerError(
                    None,
                    None,
                    f"skill manifest exceeds the {MAX_YAML_DEPTH} YAML depth limit",
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1


def _construct_unique_mapping(
    loader: _ManifestSafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(
            None,
            None,
            "expected a YAML mapping",
            node.start_mark,
        )
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_ManifestSafeLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if is_junction is not None and is_junction(path):
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except (OSError, ValueError) as exc:
        raise SkillRegistryError(f"cannot inspect skill path metadata: {path}") from exc
    return bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _canonical_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=True))).replace("\\", "/")


def _resolve_trusted_root(raw: str | os.PathLike[str]) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise UntrustedSkillPathError(
            f"trusted skill root must be an absolute path: {raw}"
        )
    try:
        linked = _is_link_or_reparse(candidate)
    except SkillRegistryError as exc:
        raise UntrustedSkillPathError(
            f"trusted skill root does not resolve to an existing directory: {raw}"
        ) from exc
    if linked:
        raise UntrustedSkillPathError(
            f"trusted skill root cannot be a link, junction or reparse point: {candidate}"
        )
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UntrustedSkillPathError(
            f"trusted skill root does not resolve to an existing directory: {raw}"
        ) from exc
    if not resolved.is_dir():
        raise UntrustedSkillPathError(
            f"trusted skill root is not a directory: {resolved}"
        )
    return resolved


def _root_id(root_identity: str) -> str:
    digest = hashlib.sha256(root_identity.encode("utf-8")).hexdigest()
    return f"skill-root-{digest[:24]}"


def _root_display_name(root: Path) -> str:
    return root.name or str(root)


def _skill_id(root_identity: str, relative_path: str) -> str:
    relative_identity = os.path.normcase(relative_path).replace("\\", "/")
    digest = hashlib.sha256(
        f"{root_identity}\0{relative_identity}".encode("utf-8")
    ).hexdigest()
    return f"skill-{digest[:32]}"


def _diagnostic(
    severity: str,
    code: str,
    message: str,
    *,
    relative_path: str | None = None,
) -> SkillDiagnostic:
    return SkillDiagnostic(
        severity=severity,
        code=code,
        message=message,
        relative_path=relative_path,
    )


def _manifest_stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _stat_is_link_or_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & REPARSE_POINT_ATTRIBUTE)


def _validate_manifest_stat(path: Path, value: os.stat_result) -> None:
    if _stat_is_link_or_reparse(value) or not stat.S_ISREG(value.st_mode):
        raise UntrustedSkillPathError(
            f"skill manifests must be regular non-reparse files: {path}"
        )
    if int(getattr(value, "st_nlink", 1)) > 1:
        raise UntrustedSkillPathError(
            f"hardlinked skill manifests are not allowed: {path}"
        )


def _read_stable_manifest(path: Path, *, max_bytes: int) -> bytes:
    try:
        before = path.lstat()
        _validate_manifest_stat(path, before)
        if before.st_size > max_bytes:
            raise SkillScanLimitError(
                f"skill manifest exceeds the {max_bytes} byte limit: {path}"
            )
        with path.open("rb") as handle:
            opened_before = os.fstat(handle.fileno())
            _validate_manifest_stat(path, opened_before)
            if _manifest_stat_key(before) != _manifest_stat_key(opened_before):
                raise SkillManifestChangedError(
                    f"skill manifest changed before its read started: {path}"
                )
            content = handle.read(max_bytes)
            opened_after = os.fstat(handle.fileno())
        after = path.lstat()
    except SkillRegistryError:
        raise
    except (OSError, ValueError) as exc:
        raise SkillRegistryError(f"cannot read skill manifest: {path}") from exc

    _validate_manifest_stat(path, opened_after)
    _validate_manifest_stat(path, after)
    if opened_after.st_size > max_bytes or after.st_size > max_bytes:
        raise SkillScanLimitError(
            f"skill manifest exceeds the {max_bytes} byte limit: {path}"
        )
    if (
        _manifest_stat_key(opened_before) != _manifest_stat_key(opened_after)
        or _manifest_stat_key(opened_after) != _manifest_stat_key(after)
        or len(content) != opened_after.st_size
    ):
        raise SkillManifestChangedError(
            f"skill manifest changed while being read: {path}"
        )
    return content


def _frontmatter(content: bytes) -> tuple[Mapping[object, object] | None, list[SkillDiagnostic]]:
    diagnostics: list[SkillDiagnostic] = []
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, [
            _diagnostic(
                "error",
                "manifest_not_utf8",
                "SKILL.md must be valid UTF-8 text",
            )
        ]

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, [
            _diagnostic(
                "error",
                "frontmatter_missing",
                "SKILL.md must begin with YAML frontmatter",
            )
        ]
    closing_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
        None,
    )
    if closing_index is None:
        return None, [
            _diagnostic(
                "error",
                "frontmatter_unclosed",
                "SKILL.md YAML frontmatter has no closing delimiter",
            )
        ]

    source = "\n".join(lines[1:closing_index])
    loader: _ManifestSafeLoader | None = None
    try:
        loader = _ManifestSafeLoader(source)
        parsed = loader.get_single_data()
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError) as exc:
        diagnostics.append(
            _diagnostic(
                "error",
                "frontmatter_invalid_yaml",
                f"SKILL.md frontmatter is invalid YAML: {exc}",
            )
        )
        return None, diagnostics
    finally:
        if loader is not None:
            loader.dispose()
    if not isinstance(parsed, Mapping):
        return None, [
            _diagnostic(
                "error",
                "frontmatter_not_mapping",
                "SKILL.md frontmatter must be a YAML mapping",
            )
        ]
    return parsed, diagnostics


def _validate_frontmatter(
    metadata: Mapping[object, object] | None,
    diagnostics: list[SkillDiagnostic],
) -> tuple[str | None, str | None, str | None, list[SkillDiagnostic]]:
    if metadata is None:
        return None, None, None, diagnostics

    name_value = metadata.get("name")
    if not isinstance(name_value, str) or not SKILL_NAME_PATTERN.fullmatch(name_value):
        diagnostics.append(
            _diagnostic(
                "error",
                "skill_name_invalid",
                "frontmatter name must be a lowercase skill identifier",
            )
        )
        name = None
    else:
        name = name_value

    description_value = metadata.get("description")
    if (
        not isinstance(description_value, str)
        or not description_value.strip()
        or len(description_value) > 16_384
    ):
        diagnostics.append(
            _diagnostic(
                "error",
                "skill_description_invalid",
                "frontmatter description must be a non-empty string of at most 16384 characters",
            )
        )
        description = None
    else:
        description = description_value.strip()

    version_value = metadata.get("version")
    if version_value is None:
        version = None
    elif not isinstance(version_value, str) or not version_value.strip() or len(version_value) > 128:
        diagnostics.append(
            _diagnostic(
                "error",
                "skill_version_invalid",
                "frontmatter version must be a non-empty string of at most 128 characters",
            )
        )
        version = None
    else:
        version = version_value.strip()
    return name, description, version, diagnostics


def _registry_fingerprint(material: object) -> str:
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class SkillRegistry:
    """Discover SKILL.md files under explicit roots without executing or writing."""

    def __init__(
        self,
        trusted_roots: Iterable[str | os.PathLike[str]],
        *,
        max_entries: int = MAX_SCAN_ENTRIES,
        max_directories: int = MAX_SCAN_DIRECTORIES,
        max_depth: int = MAX_SCAN_DEPTH,
        max_manifests: int = MAX_SKILL_MANIFESTS,
        max_manifest_bytes: int = MAX_MANIFEST_BYTES,
        max_total_manifest_bytes: int = MAX_TOTAL_MANIFEST_BYTES,
        excluded_directories: Iterable[str] = DEFAULT_EXCLUDED_DIRECTORIES,
    ) -> None:
        excluded = sorted(
            {item.strip().casefold() for item in excluded_directories if item.strip()}
        )
        self.limits = SkillScanLimits(
            max_entries=max_entries,
            max_directories=max_directories,
            max_depth=max_depth,
            max_manifests=max_manifests,
            max_manifest_bytes=max_manifest_bytes,
            max_total_manifest_bytes=max_total_manifest_bytes,
            excluded_directory_names=excluded,
        )
        roots: list[Path] = []
        for raw_root in trusted_roots:
            root = _resolve_trusted_root(raw_root)
            if root not in roots:
                roots.append(root)
        if not roots:
            raise UntrustedSkillPathError(
                "at least one explicit trusted skill root is required"
            )
        for index, root in enumerate(roots):
            if any(
                _is_within(root, other) or _is_within(other, root)
                for other in roots[:index]
            ):
                raise UntrustedSkillPathError(
                    "trusted skill roots cannot overlap because every source instance must be unambiguous"
                )
        self.trusted_roots = tuple(roots)
        self._root_identities = tuple(_canonical_path(root) for root in roots)
        self._root_ids = tuple(_root_id(identity) for identity in self._root_identities)
        self._excluded_directories = frozenset(excluded)

    @classmethod
    def unconfigured_response(cls) -> SkillRegistryResponse:
        limits = SkillScanLimits(
            max_entries=MAX_SCAN_ENTRIES,
            max_directories=MAX_SCAN_DIRECTORIES,
            max_depth=MAX_SCAN_DEPTH,
            max_manifests=MAX_SKILL_MANIFESTS,
            max_manifest_bytes=MAX_MANIFEST_BYTES,
            max_total_manifest_bytes=MAX_TOTAL_MANIFEST_BYTES,
            excluded_directory_names=sorted(DEFAULT_EXCLUDED_DIRECTORIES),
        )
        diagnostics = [
            _diagnostic(
                "info",
                "registry_not_configured",
                "No trusted skill roots are configured; no user directories were scanned",
            )
        ]
        material = {
            "configured": False,
            "limits": limits.model_dump(mode="json"),
            "roots": [],
            "skills": [],
            "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        }
        return SkillRegistryResponse(
            configured=False,
            registry_fingerprint=_registry_fingerprint(material),
            limits=limits,
            roots=[],
            skills=[],
            diagnostics=diagnostics,
        )

    def scan(self) -> SkillRegistryResponse:
        root_snapshots: list[SkillRootSnapshot] = []
        skills: list[DiscoveredSkill] = []
        diagnostics: list[SkillDiagnostic] = []
        aggregate = _Inventory()
        for priority, root in enumerate(self.trusted_roots):
            root_snapshot, root_skills = self._scan_root(
                root,
                priority,
                aggregate,
            )
            root_snapshots.append(root_snapshot)
            skills.extend(root_skills)

        skills, duplicate_diagnostics = self._mark_duplicate_names(skills)
        diagnostics.extend(duplicate_diagnostics)
        material = {
            "configured": True,
            "limits": self.limits.model_dump(mode="json"),
            "roots": [item.model_dump(mode="json") for item in root_snapshots],
            "skills": [item.model_dump(mode="json") for item in skills],
            "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
        }
        return SkillRegistryResponse(
            configured=True,
            registry_fingerprint=_registry_fingerprint(material),
            limits=self.limits,
            roots=root_snapshots,
            skills=skills,
            diagnostics=diagnostics,
        )

    def _scan_root(
        self,
        root: Path,
        priority: int,
        aggregate: _Inventory,
    ) -> tuple[SkillRootSnapshot, list[DiscoveredSkill]]:
        root_id = self._root_ids[priority]
        root_identity = self._root_identities[priority]
        inventory = _Inventory()
        diagnostics: list[SkillDiagnostic] = []
        skills: list[DiscoveredSkill] = []
        unsafe_path_seen = False
        limit_hit = False

        try:
            pending: list[tuple[Path, int]] = [(root, 0)]
            while pending:
                current, depth = pending.pop()
                if depth > self.limits.max_depth:
                    raise SkillScanLimitError(
                        f"skill discovery exceeds the {self.limits.max_depth} depth limit"
                    )
                if aggregate.directories_visited >= self.limits.max_directories:
                    raise SkillScanLimitError(
                        f"skill discovery exceeds the {self.limits.max_directories} directory limit"
                    )
                inventory.directories_visited += 1
                aggregate.directories_visited += 1

                names = self._bounded_directory_names(current, inventory, aggregate)
                child_directories: list[Path] = []
                for name in names:
                    candidate = current / name
                    relative_path = candidate.relative_to(root).as_posix()
                    if _is_link_or_reparse(candidate):
                        unsafe_path_seen = True
                        diagnostics.append(
                            _diagnostic(
                                "error",
                                "unsafe_link_rejected",
                                "A link, junction or reparse-point directory was not traversed",
                                relative_path=relative_path,
                            )
                        )
                        if name.casefold() == "skill.md":
                            if aggregate.manifests_found >= self.limits.max_manifests:
                                raise SkillScanLimitError(
                                    "skill discovery exceeds the "
                                    f"{self.limits.max_manifests} manifest limit"
                                )
                            inventory.manifests_found += 1
                            aggregate.manifests_found += 1
                            skills.append(
                                self._unreadable_skill(
                                    root,
                                    root_id,
                                    priority,
                                    candidate,
                                    relative_path,
                                    "unsafe_link_rejected",
                                    "Linked or reparse-point SKILL.md files are not read",
                                )
                            )
                        continue
                    try:
                        resolved = candidate.resolve(strict=True)
                    except (OSError, RuntimeError) as exc:
                        raise SkillRegistryError(
                            f"cannot resolve skill directory: {candidate}"
                        ) from exc
                    if not _is_within(resolved, root):
                        unsafe_path_seen = True
                        diagnostics.append(
                            _diagnostic(
                                "error",
                                "path_escape_rejected",
                                "A path resolving outside its trusted root was not accessed",
                                relative_path=relative_path,
                            )
                        )
                        continue
                    if resolved.is_dir():
                        if name.casefold() in self._excluded_directories:
                            inventory.excluded_directories += 1
                            continue
                        child_directories.append(resolved)
                        continue
                    if not resolved.is_file():
                        unsafe_path_seen = True
                        diagnostics.append(
                            _diagnostic(
                                "error",
                                "unsupported_path_rejected",
                                "A non-file, non-directory path was not accessed",
                                relative_path=relative_path,
                            )
                        )
                        continue
                    if name.casefold() != "skill.md":
                        continue
                    if aggregate.manifests_found >= self.limits.max_manifests:
                        raise SkillScanLimitError(
                            f"skill discovery exceeds the {self.limits.max_manifests} manifest limit"
                        )
                    inventory.manifests_found += 1
                    aggregate.manifests_found += 1
                    skill, unsafe_manifest = self._read_skill_record(
                        root,
                        root_id,
                        priority,
                        resolved,
                        relative_path,
                        inventory,
                        aggregate,
                    )
                    unsafe_path_seen = unsafe_path_seen or unsafe_manifest
                    skills.append(skill)

                pending.extend(
                    (directory, depth + 1)
                    for directory in reversed(child_directories)
                )
        except SkillScanLimitError as exc:
            limit_hit = True
            diagnostics.append(
                _diagnostic(
                    "error",
                    "skill_scan_limit",
                    str(exc),
                )
            )
        except SkillRegistryError as exc:
            unsafe_path_seen = True
            diagnostics.append(
                _diagnostic(
                    "error",
                    "skill_scan_failed",
                    str(exc),
                )
            )

        if not unsafe_path_seen and not limit_hit:
            try:
                if _is_link_or_reparse(root) or _canonical_path(root) != root_identity:
                    raise SkillManifestChangedError(
                        "trusted skill root changed during discovery"
                    )
            except (OSError, RuntimeError, SkillRegistryError):
                unsafe_path_seen = True
                diagnostics.append(
                    _diagnostic(
                        "error",
                        "skill_root_changed_during_scan",
                        "The trusted skill root changed before discovery completed",
                    )
                )

        if limit_hit or unsafe_path_seen:
            scan_status = "partial" if skills else "blocked"
            manifest_set_fingerprint = None
        else:
            scan_status = "complete"
            manifest_set_fingerprint = _registry_fingerprint(
                [
                    {
                        "relative_path": skill.source.relative_path,
                        "manifest_fingerprint": skill.manifest_fingerprint,
                    }
                    for skill in skills
                ]
            )
        snapshot = SkillRootSnapshot(
            root_id=root_id,
            priority=priority,
            display_name=_root_display_name(root),
            root_path=str(root),
            scan_status=scan_status,
            manifest_set_fingerprint=manifest_set_fingerprint,
            inventory=SkillRootInventory(
                directories_visited=inventory.directories_visited,
                entries_seen=inventory.entries_seen,
                excluded_directories=inventory.excluded_directories,
                manifests_found=inventory.manifests_found,
                manifests_loaded=inventory.manifests_loaded,
                manifest_bytes_read=inventory.manifest_bytes_read,
            ),
            diagnostics=diagnostics,
        )
        return snapshot, skills

    def _bounded_directory_names(
        self,
        directory: Path,
        inventory: _Inventory,
        aggregate: _Inventory,
    ) -> list[str]:
        names: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if aggregate.entries_seen >= self.limits.max_entries:
                        raise SkillScanLimitError(
                            "skill discovery exceeds the "
                            f"{self.limits.max_entries} entry limit"
                        )
                    inventory.entries_seen += 1
                    aggregate.entries_seen += 1
                    names.append(entry.name)
        except SkillRegistryError:
            raise
        except OSError as exc:
            raise SkillRegistryError(
                f"cannot enumerate trusted skill directory {directory}: {exc}"
            ) from exc
        return sorted(names, key=str.casefold)

    def _read_skill_record(
        self,
        root: Path,
        root_id: str,
        priority: int,
        path: Path,
        relative_path: str,
        inventory: _Inventory,
        aggregate: _Inventory,
    ) -> tuple[DiscoveredSkill, bool]:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SkillRegistryError(
                f"cannot inspect skill manifest size: {path}"
            ) from exc
        if size > self.limits.max_manifest_bytes:
            return (
                self._unreadable_skill(
                    root,
                    root_id,
                    priority,
                    path,
                    relative_path,
                    "manifest_size_limit",
                    f"SKILL.md exceeds the {self.limits.max_manifest_bytes} byte limit",
                ),
                True,
            )
        remaining_bytes = (
            self.limits.max_total_manifest_bytes - aggregate.manifest_bytes_read
        )
        if size > remaining_bytes:
            raise SkillScanLimitError(
                "skill discovery exceeds the total manifest byte limit"
            )
        read_limit = min(self.limits.max_manifest_bytes, remaining_bytes)
        try:
            content = _read_stable_manifest(
                path,
                max_bytes=read_limit,
            )
        except SkillScanLimitError as exc:
            if read_limit < self.limits.max_manifest_bytes:
                raise SkillScanLimitError(
                    "skill discovery exceeds the total manifest byte limit"
                ) from exc
            return (
                self._unreadable_skill(
                    root,
                    root_id,
                    priority,
                    path,
                    relative_path,
                    "manifest_size_limit",
                    str(exc),
                ),
                True,
            )
        except UntrustedSkillPathError as exc:
            return (
                self._unreadable_skill(
                    root,
                    root_id,
                    priority,
                    path,
                    relative_path,
                    "unsafe_manifest_rejected",
                    str(exc),
                ),
                True,
            )
        except SkillManifestChangedError as exc:
            return (
                self._unreadable_skill(
                    root,
                    root_id,
                    priority,
                    path,
                    relative_path,
                    "manifest_changed_during_scan",
                    str(exc),
                ),
                True,
            )
        except SkillRegistryError as exc:
            return (
                self._unreadable_skill(
                    root,
                    root_id,
                    priority,
                    path,
                    relative_path,
                    "manifest_read_failed",
                    str(exc),
                ),
                True,
            )
        if aggregate.manifest_bytes_read + len(content) > self.limits.max_total_manifest_bytes:
            raise SkillScanLimitError(
                "skill discovery exceeds the total manifest byte limit"
            )
        inventory.manifests_loaded += 1
        inventory.manifest_bytes_read += len(content)
        aggregate.manifests_loaded += 1
        aggregate.manifest_bytes_read += len(content)
        return (
            self._loaded_skill(
                root,
                root_id,
                priority,
                path,
                relative_path,
                content,
            ),
            False,
        )

    def _loaded_skill(
        self,
        root: Path,
        root_id: str,
        priority: int,
        path: Path,
        relative_path: str,
        content: bytes,
    ) -> DiscoveredSkill:
        fingerprint = hashlib.sha256(content).hexdigest()
        metadata, diagnostics = _frontmatter(content)
        name, description, version, diagnostics = _validate_frontmatter(
            metadata,
            diagnostics,
        )
        diagnostics = [
            item.model_copy(update={"relative_path": relative_path})
            for item in diagnostics
        ]
        valid = not any(item.severity == "error" for item in diagnostics)
        return DiscoveredSkill(
            skill_id=_skill_id(self._root_identities[priority], relative_path),
            name=name,
            description=description,
            source=SkillSource(
                root_id=root_id,
                root_priority=priority,
                root_path=str(root),
                relative_path=relative_path,
                manifest_path=str(path),
            ),
            manifest_fingerprint=fingerprint,
            manifest_revision=f"sha256:{fingerprint}",
            revision_status="manifest_only",
            declared_version=version,
            effective_version=version or f"sha256:{fingerprint[:12]}",
            version_status=(
                "declared" if version else "derived_from_manifest_fingerprint"
            ),
            validation_status="valid" if valid else "invalid",
            readiness="loadable" if valid else "discovered",
            diagnostics=diagnostics,
        )

    def _unreadable_skill(
        self,
        root: Path,
        root_id: str,
        priority: int,
        path: Path,
        relative_path: str,
        code: str,
        message: str,
    ) -> DiscoveredSkill:
        return DiscoveredSkill(
            skill_id=_skill_id(self._root_identities[priority], relative_path),
            name=None,
            description=None,
            source=SkillSource(
                root_id=root_id,
                root_priority=priority,
                root_path=str(root),
                relative_path=relative_path,
                manifest_path=str(path),
            ),
            manifest_fingerprint=None,
            manifest_revision=None,
            revision_status="unavailable",
            declared_version=None,
            effective_version=None,
            version_status="unavailable",
            validation_status="invalid",
            readiness="discovered",
            diagnostics=[
                _diagnostic(
                    "error",
                    code,
                    message,
                    relative_path=relative_path,
                )
            ],
        )

    def _mark_duplicate_names(
        self,
        skills: list[DiscoveredSkill],
    ) -> tuple[list[DiscoveredSkill], list[SkillDiagnostic]]:
        by_name: dict[str, list[int]] = {}
        for index, skill in enumerate(skills):
            if skill.name:
                by_name.setdefault(skill.name.casefold(), []).append(index)

        registry_diagnostics: list[SkillDiagnostic] = []
        for indexes in by_name.values():
            if len(indexes) < 2:
                continue
            name = skills[indexes[0]].name
            paths = [skills[index].source.manifest_path for index in indexes]
            message = (
                f"Skill name {name!r} exists at {len(indexes)} source paths; "
                "all source instances are preserved in explicit root priority order"
            )
            registry_diagnostics.append(
                _diagnostic(
                    "warning",
                    "duplicate_skill_name",
                    message,
                )
            )
            for index in indexes:
                skill = skills[index]
                path_sample = " | ".join(paths[:5])
                if len(paths) > 5:
                    path_sample += f" | ... ({len(paths) - 5} more)"
                path_sample = path_sample[:3000]
                duplicate = _diagnostic(
                    "warning",
                    "duplicate_skill_name",
                    message + ": " + path_sample,
                    relative_path=skill.source.relative_path,
                )
                skills[index] = skill.model_copy(
                    update={"diagnostics": [*skill.diagnostics, duplicate]}
                )
        return skills, registry_diagnostics
