from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from zhimeng_runtime import skill_registry as registry_module
from zhimeng_runtime.skill_registry import (
    SkillRegistry,
    UntrustedSkillPathError,
)


FIXTURES = Path(__file__).parent / "fixtures" / "skills"


def _manifest(
    directory: Path,
    *,
    name: str = "fixture-skill",
    description: str = "Fixture skill description.",
    extra: str = "",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra}"
        "---\n\n"
        "# Fixture\n",
        encoding="utf-8",
    )
    return path


def test_registry_discovers_nested_skills_and_preserves_duplicate_sources(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    shutil.copytree(FIXTURES / "primary", primary)
    shutil.copytree(FIXTURES / "secondary", secondary)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    snapshot = SkillRegistry([primary, secondary]).scan()

    assert snapshot.read_only is True
    assert snapshot.configured is True
    assert len(snapshot.skills) == 3
    assert [root.priority for root in snapshot.roots] == [0, 1]
    assert all(root.scan_status == "complete" for root in snapshot.roots)
    assert {
        skill.source.relative_path for skill in snapshot.skills
    } >= {
        "SKILL.md",
        "references/bundled-skills/chapter-brief/SKILL.md",
        "sync-backup-20260531/SKILL.md",
    }
    duplicates = [
        skill
        for skill in snapshot.skills
        if skill.name == "shared-writing-skill"
    ]
    assert len(duplicates) == 2
    assert len({skill.skill_id for skill in duplicates}) == 2
    assert len({skill.manifest_fingerprint for skill in duplicates}) == 2
    assert len({skill.source.manifest_path for skill in duplicates}) == 2
    assert [skill.source.root_priority for skill in duplicates] == [0, 1]
    assert [skill.declared_version for skill in duplicates] == ["1.0.0", "0.8.0"]
    assert all(skill.version_status == "declared" for skill in duplicates)
    assert all(
        "duplicate_skill_name" in {item.code for item in skill.diagnostics}
        for skill in duplicates
    )
    assert [item.code for item in snapshot.diagnostics] == [
        "duplicate_skill_name"
    ]
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_manifest_identity_changes_without_changing_stable_skill_id(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    manifest = _manifest(root)
    registry = SkillRegistry([root])

    first_snapshot = registry.scan()
    unchanged_snapshot = registry.scan()
    first = first_snapshot.skills[0]
    assert unchanged_snapshot.registry_fingerprint == first_snapshot.registry_fingerprint
    assert unchanged_snapshot.roots[0].manifest_set_fingerprint == (
        first_snapshot.roots[0].manifest_set_fingerprint
    )
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\nChanged body.\n",
        encoding="utf-8",
    )
    second = registry.scan().skills[0]

    assert second.skill_id == first.skill_id
    assert second.manifest_fingerprint != first.manifest_fingerprint
    assert second.manifest_revision == f"sha256:{second.manifest_fingerprint}"
    assert second.revision_status == "manifest_only"
    assert second.declared_version is None
    assert second.effective_version == f"sha256:{second.manifest_fingerprint[:12]}"
    assert second.version_status == "derived_from_manifest_fingerprint"
    assert second.package_fingerprint is None
    assert second.package_fingerprint_status == "not_scanned"


@pytest.mark.parametrize(
    ("frontmatter", "expected_code"),
    [
        (
            "---\nname: duplicate\nname: duplicate-again\n"
            "description: Duplicate key.\n---\n",
            "frontmatter_invalid_yaml",
        ),
        (
            "---\nname: alias-skill\nbase: &base Alias\n"
            "description: *base\n---\n",
            "frontmatter_invalid_yaml",
        ),
        (
            "# No frontmatter\n",
            "frontmatter_missing",
        ),
    ],
)
def test_structured_yaml_parser_rejects_ambiguous_or_missing_frontmatter(
    tmp_path: Path,
    frontmatter: str,
    expected_code: str,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "SKILL.md").write_text(frontmatter, encoding="utf-8")

    skill = SkillRegistry([root]).scan().skills[0]

    assert skill.validation_status == "invalid"
    assert skill.readiness == "discovered"
    assert expected_code in {item.code for item in skill.diagnostics}
    assert skill.execution_status == "not_assessed"


def test_deep_yaml_is_rejected_without_escaping_as_recursion_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    depth = 400
    (root / "SKILL.md").write_text(
        "---\n"
        "name: deep-yaml\n"
        "description: Deep YAML must remain a typed manifest error.\n"
        f"nested: {'[' * depth}0{']' * depth}\n"
        "---\n",
        encoding="utf-8",
    )

    skill = SkillRegistry([root]).scan().skills[0]

    assert skill.validation_status == "invalid"
    assert skill.diagnostics[0].code == "frontmatter_invalid_yaml"


def test_yaml_scalar_constructor_failure_is_a_typed_manifest_error(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\n"
        "name: huge-integer\n"
        "description: Constructor failures must not escape the parser.\n"
        f"extra: {'9' * 5_000}\n"
        "---\n",
        encoding="utf-8",
    )

    skill = SkillRegistry([root]).scan().skills[0]

    assert skill.validation_status == "invalid"
    assert skill.diagnostics[0].code == "frontmatter_invalid_yaml"


def test_yaml_reader_failure_is_a_typed_manifest_error(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\n"
        "name: control-character\n"
        f"description: Invalid{chr(0)}YAML\n"
        "---\n",
        encoding="utf-8",
    )

    skill = SkillRegistry([root]).scan().skills[0]

    assert skill.validation_status == "invalid"
    assert skill.diagnostics[0].code == "frontmatter_invalid_yaml"


def test_filesystem_root_has_a_nonempty_display_name() -> None:
    filesystem_root = Path(Path.cwd().anchor)

    assert registry_module._root_display_name(filesystem_root)


def test_excluded_library_is_pruned_without_content_reads_or_package_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "distillation-skill"
    manifest = _manifest(root, name="distillation-skill")
    library = root / "library" / "raw-sources"
    library.mkdir(parents=True)
    (library / "SKILL.md").write_text("not a real skill", encoding="utf-8")
    corpus = library / "corpus.epub"
    corpus.write_bytes(b"excluded content must not be read")
    reads: list[Path] = []
    original = registry_module._read_stable_manifest
    original_path_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path == corpus:
            pytest.fail("excluded library content was opened")
        return original_path_open(path, *args, **kwargs)

    def recording_read(path: Path, **kwargs) -> bytes:
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(registry_module, "_read_stable_manifest", recording_read)
    snapshot = SkillRegistry([root]).scan()

    assert reads == [manifest]
    assert [skill.name for skill in snapshot.skills] == ["distillation-skill"]
    inventory = snapshot.roots[0].inventory
    assert inventory.directories_visited == 1
    assert inventory.entries_seen == 2
    assert inventory.excluded_directories == 1
    assert inventory.manifests_found == 1
    assert inventory.manifest_bytes_read == manifest.stat().st_size
    assert inventory.content_files_read == 0
    assert inventory.content_bytes_read == 0
    assert inventory.package_fingerprint is None
    assert inventory.package_fingerprint_status == "not_scanned"


def test_entry_and_manifest_byte_budgets_stop_unbounded_discovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _manifest(root / "one", name="one")
    _manifest(root / "two", name="two")

    entry_limited = SkillRegistry([root], max_entries=1).scan()
    byte_limited = SkillRegistry([root], max_total_manifest_bytes=1).scan()

    assert entry_limited.roots[0].scan_status == "blocked"
    assert entry_limited.skills == []
    assert "skill_scan_limit" in {
        item.code for item in entry_limited.roots[0].diagnostics
    }
    assert byte_limited.roots[0].scan_status == "blocked"
    assert byte_limited.skills == []
    assert byte_limited.roots[0].inventory.manifest_bytes_read == 0


def test_scan_budgets_are_aggregate_across_trusted_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _manifest(first, name="first")
    _manifest(second, name="second")

    snapshot = SkillRegistry([first, second], max_manifests=1).scan()

    assert [skill.name for skill in snapshot.skills] == ["first"]
    assert snapshot.roots[0].scan_status == "complete"
    assert snapshot.roots[1].scan_status == "blocked"
    assert "skill_scan_limit" in {
        item.code for item in snapshot.roots[1].diagnostics
    }


def test_total_byte_budget_is_passed_to_each_manifest_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    first = _manifest(root / "one", name="one")
    second = _manifest(root / "two", name="two")
    total_bytes = first.stat().st_size + second.stat().st_size
    read_limits: list[int] = []
    original = registry_module._read_stable_manifest

    def recording_read(path: Path, *, max_bytes: int) -> bytes:
        read_limits.append(max_bytes)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(registry_module, "_read_stable_manifest", recording_read)

    snapshot = SkillRegistry(
        [root],
        max_total_manifest_bytes=total_bytes,
    ).scan()

    assert snapshot.roots[0].scan_status == "complete"
    assert read_limits == [total_bytes, second.stat().st_size]
    assert snapshot.roots[0].inventory.manifest_bytes_read == total_bytes


def test_oversized_manifest_is_reported_without_being_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    manifest = _manifest(root)
    reads: list[Path] = []
    monkeypatch.setattr(
        registry_module,
        "_read_stable_manifest",
        lambda path, **kwargs: reads.append(path),
    )

    snapshot = SkillRegistry([root], max_manifest_bytes=1).scan()

    assert reads == []
    assert snapshot.skills[0].manifest_fingerprint is None
    assert snapshot.skills[0].revision_status == "unavailable"
    assert snapshot.skills[0].diagnostics[0].code == "manifest_size_limit"
    assert snapshot.roots[0].scan_status == "partial"
    assert snapshot.roots[0].manifest_set_fingerprint is None
    assert manifest.exists()


def test_manifest_growing_after_initial_stat_marks_root_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    manifest = _manifest(root)
    original = registry_module._read_stable_manifest

    def grow_then_read(path: Path, *, max_bytes: int) -> bytes:
        path.write_bytes(path.read_bytes() + b"x" * (max_bytes + 1))
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(registry_module, "_read_stable_manifest", grow_then_read)

    snapshot = SkillRegistry([root], max_manifest_bytes=256).scan()

    assert snapshot.skills[0].diagnostics[0].code == "manifest_size_limit"
    assert snapshot.roots[0].scan_status == "partial"
    assert snapshot.roots[0].manifest_set_fingerprint is None


def test_root_moved_after_manifest_read_uses_cached_identity_and_marks_partial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _manifest(root)
    moved = tmp_path / "moved"
    registry = SkillRegistry([root])
    original = registry_module._read_stable_manifest

    def read_then_move(path: Path, *, max_bytes: int) -> bytes:
        content = original(path, max_bytes=max_bytes)
        root.rename(moved)
        return content

    monkeypatch.setattr(registry_module, "_read_stable_manifest", read_then_move)

    snapshot = registry.scan()

    assert len(snapshot.skills) == 1
    assert snapshot.skills[0].skill_id.startswith("skill-")
    assert snapshot.roots[0].scan_status == "partial"
    assert snapshot.roots[0].manifest_set_fingerprint is None
    assert snapshot.roots[0].diagnostics[0].code == "skill_root_changed_during_scan"


def test_hardlinked_manifest_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    manifest = _manifest(root)
    try:
        os.link(manifest, root / "manifest-alias.md")
    except OSError:
        pytest.skip("hardlinks are not available for this test account")

    skill = SkillRegistry([root]).scan().skills[0]

    assert skill.validation_status == "invalid"
    assert skill.manifest_fingerprint is None
    assert skill.diagnostics[0].code == "unsafe_manifest_rejected"


def test_manifest_file_handle_must_match_the_scanned_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    manifest = _manifest(root)
    outside = tmp_path / "outside.md"
    outside.write_bytes(manifest.read_bytes())
    original_open = Path.open

    def redirected_open(path: Path, *args, **kwargs):
        target = outside if path == manifest else path
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", redirected_open)

    snapshot = SkillRegistry([root]).scan()

    assert snapshot.roots[0].scan_status == "partial"
    assert snapshot.skills[0].diagnostics[0].code == "manifest_changed_during_scan"


def test_symlink_directory_is_not_followed_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _manifest(root)
    outside = tmp_path / "outside"
    _manifest(outside, name="outside-skill")
    link = root / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available for this test account")

    snapshot = SkillRegistry([root]).scan()

    assert [skill.name for skill in snapshot.skills] == ["fixture-skill"]
    assert snapshot.roots[0].scan_status == "partial"
    assert "unsafe_link_rejected" in {
        item.code for item in snapshot.roots[0].diagnostics
    }


def test_junction_or_reparse_directory_is_not_followed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    _manifest(root)
    junction = root / "junction"
    _manifest(junction, name="hidden-skill")
    real_isjunction = getattr(os.path, "isjunction", lambda path: False)
    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda path: Path(path) == junction or real_isjunction(path),
        raising=False,
    )

    snapshot = SkillRegistry([root]).scan()

    assert [skill.name for skill in snapshot.skills] == ["fixture-skill"]
    assert snapshot.roots[0].scan_status == "partial"


def test_trusted_roots_must_be_absolute_real_and_non_overlapping(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    child = root / "child"
    child.mkdir(parents=True)

    with pytest.raises(UntrustedSkillPathError, match="absolute"):
        SkillRegistry([Path("relative-skills")])
    with pytest.raises(UntrustedSkillPathError, match="at least one"):
        SkillRegistry([])
    with pytest.raises(UntrustedSkillPathError, match="does not resolve"):
        SkillRegistry([tmp_path / "missing"])
    with pytest.raises(UntrustedSkillPathError, match="overlap"):
        SkillRegistry([root, child])
