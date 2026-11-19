from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from zhimeng_runtime.project_reader import (
    SECTION_LABELS,
    ProjectChangedDuringReadError,
    ProjectReader,
    ProjectScanLimitError,
    UnreadableProjectFileError,
    UntrustedProjectPathError,
)
from zhimeng_runtime import project_reader as project_reader_module


def _project(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    volume = root / "05_正文内容" / "第一卷文件夹"
    volume.mkdir()
    prose = volume / "CH001_初稿.md"
    prose.write_text("第一章正文", encoding="utf-8")
    (volume / "CH001_context.json").write_text('{"chapter": 1}', encoding="utf-8")
    (volume / "CH001_handoff.md").write_text("handoff", encoding="utf-8")
    (volume / "CH002_控制卡.md").write_text("控制卡", encoding="utf-8")
    return root, prose


def _file(snapshot: dict, relative_path: str) -> dict:
    return next(item for item in snapshot["files"] if item["relative_path"] == relative_path)


def test_reader_recognizes_sections_volume_chapters_and_only_real_prose(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "长篇项目")
    before = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }

    snapshot = ProjectReader([library]).inspect(project)

    assert snapshot["read_only"] is True
    assert snapshot["project"]["display_name"] == "长篇项目"
    assert snapshot["project"]["revision"] == 1
    assert len(snapshot["sections"]) == 9
    assert all(item["present"] for item in snapshot["sections"])
    assert snapshot["volumes"] == [
        {
            "volume_id": "VOL001",
            "number": 1,
            "chapter_ids": ["VOL001-CH001", "VOL001-CH002"],
        }
    ]
    assert [item["chapter_id"] for item in snapshot["chapters"]] == [
        "VOL001-CH001",
        "VOL001-CH002",
    ]

    prose_record = _file(snapshot, prose.relative_to(project).as_posix())
    assert prose_record["kind"] == "prose"
    assert prose_record["sha256"] == hashlib.sha256(prose.read_bytes()).hexdigest()
    assert len(prose_record["file_id"]) == len("file-") + 32
    assert _file(snapshot, "05_正文内容/第一卷文件夹/CH001_context.json")["kind"] == "context"
    assert _file(snapshot, "05_正文内容/第一卷文件夹/CH001_handoff.md")["kind"] == "handoff"
    assert _file(snapshot, "05_正文内容/第一卷文件夹/CH002_控制卡.md")["kind"] == "control"
    assert [item["relative_path"] for item in snapshot["files"] if item["kind"] == "prose"] == [
        "05_正文内容/第一卷文件夹/CH001_初稿.md"
    ]
    after = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_file_id_survives_rename_while_snapshot_revision_advances(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "book")
    reader = ProjectReader([library])

    first = reader.inspect(project)
    first_file = _file(first, prose.relative_to(project).as_posix())
    renamed = prose.with_name("第一章_定稿.md")
    prose.rename(renamed)
    second = reader.inspect(project)
    second_file = _file(second, renamed.relative_to(project).as_posix())
    unchanged = reader.inspect(project)

    assert second_file["file_id"] == first_file["file_id"]
    assert second["project"]["revision"] == first["project"]["revision"] + 1
    assert second["project"]["fingerprint"] != first["project"]["fingerprint"]
    assert unchanged["project"]["revision"] == second["project"]["revision"]


def _age(project: Path, seconds: int = 3600) -> None:
    """把书里所有文件的修改时间往前拨，模拟「早就写好、这次没动」的书。"""
    past = time.time_ns() - seconds * 1_000_000_000
    for path in project.rglob("*"):
        if path.is_file():
            os.utime(path, ns=(past, past))


def test_same_size_content_change_updates_hash_and_revision(tmp_path: Path) -> None:
    # 文件刚写过（修改时间在 RACY_WINDOW_NS 之内）：哪怕大小和修改时间都被改回原样，下一次扫描也照样重读。
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "book")
    prose.write_text("甲乙", encoding="utf-8")
    reader = ProjectReader([library])
    first = reader.inspect(project)
    first_file = _file(first, prose.relative_to(project).as_posix())
    timestamps = prose.stat()

    prose.write_text("丙丁", encoding="utf-8")
    os.utime(prose, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
    second = reader.inspect(project)
    second_file = _file(second, prose.relative_to(project).as_posix())

    assert second_file["size_bytes"] == first_file["size_bytes"]
    assert second_file["file_id"] == first_file["file_id"]
    assert second_file["sha256"] != first_file["sha256"]
    assert second["project"]["revision"] == first["project"]["revision"] + 1


def test_unchanged_files_are_not_reread_on_the_next_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "book")
    _age(project)
    reader = ProjectReader([library])
    first = reader.inspect(project)
    calls: list[str] = []
    original = project_reader_module._hash_stable_file

    def recording_hash(path: Path, relative_path: str, **kwargs):
        calls.append(relative_path)
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(project_reader_module, "_hash_stable_file", recording_hash)
    second = reader.inspect(project)

    assert calls == []
    assert second["files"] == first["files"]
    assert second["project"] == first["project"]

    prose.write_text("第一章改过了", encoding="utf-8")
    third = reader.inspect(project)

    assert calls == [prose.relative_to(project).as_posix()]
    assert _file(third, calls[0])["sha256"] == hashlib.sha256("第一章改过了".encode("utf-8")).hexdigest()
    assert third["project"]["revision"] == first["project"]["revision"] + 1


def test_stealth_same_size_change_outside_the_racy_window_is_caught_when_read(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    structured = project / "03_人物设定" / "state.json"
    structured.write_text('{"value": 1}', encoding="utf-8")
    _age(project)
    reader = ProjectReader([library])
    reader.inspect(project)
    timestamps = structured.stat()

    structured.write_text('{"value": 2}', encoding="utf-8")
    os.utime(structured, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))
    snapshot = reader.inspect(project)
    record = _file(snapshot, "03_人物设定/state.json")

    # 大小、修改时间、文件身份都被改回原样：整本书的指纹认不出（和 git 一样只看文件状态）……
    assert record["sha256"] == hashlib.sha256(b'{"value": 1}').hexdigest()
    # ……但真去读这个文件时内容和快照对不上，读不出错的内容；
    with pytest.raises(ProjectChangedDuringReadError, match="changed after snapshot"):
        reader.read_text_file_from_snapshot(
            snapshot,
            record["file_id"],
            allowed_kinds={"structured_state"},
        )
    # ……重启以后（新的 Reader）照常重算。
    fresh = ProjectReader([library]).inspect(project)
    assert _file(fresh, "03_人物设定/state.json")["sha256"] == hashlib.sha256(b'{"value": 2}').hexdigest()


def test_snapshot_text_reader_reuses_one_inspection_for_internal_file_kinds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    status = project / "07_运营数据" / "project_status.md"
    status.write_text("连载中", encoding="utf-8")
    structured = project / "03_人物设定" / "characters.json"
    structured.write_text('{"lead": "林见川"}', encoding="utf-8")
    asset = project / "08_素材库" / "reference.dat"
    asset.write_text("可读素材", encoding="utf-8")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)

    monkeypatch.setattr(
        reader,
        "inspect",
        lambda _root: pytest.fail("snapshot reads must not rescan the project"),
    )
    allowed_kinds = {"context", "status", "structured_state", "asset"}
    expected = {
        "05_正文内容/第一卷文件夹/CH001_context.json": ("context", '{"chapter": 1}'),
        "07_运营数据/project_status.md": ("status", "连载中"),
        "03_人物设定/characters.json": ("structured_state", '{"lead": "林见川"}'),
        "08_素材库/reference.dat": ("asset", "可读素材"),
    }

    for relative_path, (kind, content) in expected.items():
        record = _file(snapshot, relative_path)
        result = reader.read_text_file_from_snapshot(
            snapshot,
            record["file_id"],
            allowed_kinds=allowed_kinds,
        )

        assert result["file"]["kind"] == kind
        assert result["content"] == content
        assert result["etag"] == record["sha256"]
        assert result["snapshot_revision"] == snapshot["project"]["revision"]
        assert result["project_fingerprint"] == snapshot["project"]["fingerprint"]


def test_snapshot_text_reader_rejects_content_changed_after_inspection(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    structured = project / "03_人物设定" / "state.json"
    structured.write_text('{"value": 1}', encoding="utf-8")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)
    record = _file(snapshot, "03_人物设定/state.json")
    timestamps = structured.stat()

    structured.write_text('{"value": 2}', encoding="utf-8")
    os.utime(structured, ns=(timestamps.st_atime_ns, timestamps.st_mtime_ns))

    with pytest.raises(ProjectChangedDuringReadError, match="changed after snapshot"):
        reader.read_text_file_from_snapshot(
            snapshot,
            record["file_id"],
            allowed_kinds={"structured_state"},
        )


def test_snapshot_text_reader_revalidates_root_links_size_and_utf8(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    assets = project / "08_素材库"
    link_target = assets / "link-target.dat"
    link_target.write_text("链接边界", encoding="utf-8")
    oversized = assets / "oversized.dat"
    oversized.write_text("大小边界", encoding="utf-8")
    invalid_utf8 = assets / "invalid-utf8.dat"
    invalid_utf8.write_bytes(b"\xff\xfe\xfa")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)

    outside = tmp_path / "outside"
    outside.mkdir()
    forged_snapshot = dict(snapshot)
    forged_snapshot["project"] = {**snapshot["project"], "root_path": str(outside)}
    link_record = _file(snapshot, "08_素材库/link-target.dat")
    with pytest.raises(UntrustedProjectPathError):
        reader.read_text_file_from_snapshot(
            forged_snapshot,
            link_record["file_id"],
            allowed_kinds={"asset"},
        )

    real_is_link_or_reparse = project_reader_module._is_link_or_reparse
    monkeypatch.setattr(
        project_reader_module,
        "_is_link_or_reparse",
        lambda path: Path(path) == link_target or real_is_link_or_reparse(Path(path)),
    )
    with pytest.raises(UntrustedProjectPathError, match="link or junction"):
        reader.read_text_file_from_snapshot(
            snapshot,
            link_record["file_id"],
            allowed_kinds={"asset"},
        )

    invalid_record = _file(snapshot, "08_素材库/invalid-utf8.dat")
    with pytest.raises(UnreadableProjectFileError, match="not readable text"):
        reader.read_text_file_from_snapshot(
            snapshot,
            invalid_record["file_id"],
            allowed_kinds={"asset"},
        )

    oversized.write_bytes(b"x" * (project_reader_module.MAX_TEXT_FILE_BYTES + 1))
    oversized_record = _file(snapshot, "08_素材库/oversized.dat")
    with pytest.raises(ProjectScanLimitError, match="byte read limit"):
        reader.read_text_file_from_snapshot(
            snapshot,
            oversized_record["file_id"],
            allowed_kinds={"asset"},
        )


def test_public_text_reader_opens_text_regardless_of_legacy_kind(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "book")
    reader = ProjectReader([library])
    snapshot = reader.inspect(project)
    prose_record = _file(snapshot, prose.relative_to(project).as_posix())
    context_record = _file(snapshot, "05_正文内容/第一卷文件夹/CH001_context.json")

    assert reader.read_text_file(project, prose_record["file_id"])["content"] == "第一章正文"
    assert reader.read_text_file(project, context_record["file_id"])["content"] == (project / context_record["relative_path"]).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "file_name, expected_kind",
    [
        ("CH001_chapter-brief.md", "document"),
        ("CH001_续写卡.md", "document"),
        ("CH001_记忆保险丝.md", "document"),
        ("CH001_章纲.md", "document"),
        ("CH001_ai_smell_report.md", "review"),
        ("CH001_draft_body_cleanliness.md", "review"),
        # CH001_写回同步.md 不再列：受管链的写回文件已退役，带章号的中文名一律当正文（2026-09-24）。
    ],
)
def test_chapter_support_documents_do_not_become_prose(
    tmp_path: Path,
    file_name: str,
    expected_kind: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    support = project / "05_正文内容" / "第一卷文件夹" / file_name
    support.write_text("写作辅助文件", encoding="utf-8")

    snapshot = ProjectReader([library]).inspect(project)

    assert _file(snapshot, support.relative_to(project).as_posix())["kind"] == expected_kind
    assert support.name not in {
        Path(item["relative_path"]).name
        for item in snapshot["files"]
        if item["kind"] == "prose"
    }


@pytest.mark.parametrize(
    "file_name",
    ["CH003_雨夜来信.md", "第4章 港口重逢.md"],
)
def test_titled_chapter_files_are_recognized_as_prose(
    tmp_path: Path,
    file_name: str,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    titled = project / "05_正文内容" / "第一卷文件夹" / file_name
    titled.write_text("章节正文", encoding="utf-8")

    snapshot = ProjectReader([library]).inspect(project)

    assert _file(snapshot, titled.relative_to(project).as_posix())["kind"] == "prose"


def test_same_chapter_number_in_two_volumes_remains_distinct(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    second_volume = project / "05_正文内容" / "第二卷文件夹"
    second_volume.mkdir()
    (second_volume / "CH001_定稿.md").write_text("第二卷第一章", encoding="utf-8")

    snapshot = ProjectReader([library]).inspect(project)

    assert {item["chapter_id"] for item in snapshot["chapters"]} >= {
        "VOL001-CH001",
        "VOL002-CH001",
    }
    assert {item["volume_id"] for item in snapshot["volumes"]} == {"VOL001", "VOL002"}


def test_chapters_and_volume_members_are_sorted_in_tree_order(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    first_volume = project / "05_正文内容" / "第一卷文件夹"
    (first_volume / "CH010_定稿.md").write_text("第一卷第十章", encoding="utf-8")
    second_volume = project / "05_正文内容" / "第二卷文件夹"
    second_volume.mkdir()
    (second_volume / "CH003_定稿.md").write_text("第二卷第三章", encoding="utf-8")
    (second_volume / "CH001_定稿.md").write_text("第二卷第一章", encoding="utf-8")
    (project / "05_正文内容" / "CH999_定稿.md").write_text(
        "未分卷章节",
        encoding="utf-8",
    )

    snapshot = ProjectReader([library]).inspect(project)

    assert [item["chapter_id"] for item in snapshot["chapters"]] == [
        "VOL001-CH001",
        "VOL001-CH002",
        "VOL001-CH010",
        "VOL002-CH001",
        "VOL002-CH003",
        "CH999",
    ]
    assert snapshot["volumes"] == [
        {
            "volume_id": "VOL001",
            "number": 1,
            "chapter_ids": ["VOL001-CH001", "VOL001-CH002", "VOL001-CH010"],
        },
        {
            "volume_id": "VOL002",
            "number": 2,
            "chapter_ids": ["VOL002-CH001", "VOL002-CH003"],
        },
    ]


def test_hardlink_entries_are_rejected_to_preserve_stable_file_identity(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, prose = _project(library / "book")
    alias = project / "01_项目定位" / "alias.md"
    try:
        os.link(prose, alias)
    except OSError:
        pytest.skip("hardlinks are not available for this test account")
    with pytest.raises(UntrustedProjectPathError, match="hardlinked files"):
        ProjectReader([library]).inspect(project)


def test_scan_budget_is_checked_before_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    calls: list[Path] = []
    original = project_reader_module._hash_stable_file

    def recording_hash(path: Path, relative_path: str, **kwargs):
        calls.append(path)
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(project_reader_module, "_hash_stable_file", recording_hash)
    with pytest.raises(ProjectScanLimitError):
        ProjectReader([library], max_bytes=1).inspect(project)

    assert calls == []


def test_scan_budget_is_rechecked_if_file_grows_after_size_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project = library / "book"
    section = project / "01_项目定位"
    section.mkdir(parents=True)
    target = section / "a.md"
    target.write_bytes(b"a")
    original = project_reader_module._hash_stable_file

    def growing_hash(path: Path, relative_path: str, **kwargs):
        if path == target:
            path.write_bytes(b"ab")
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(project_reader_module, "_hash_stable_file", growing_hash)

    with pytest.raises(ProjectScanLimitError, match="remaining 1 byte budget"):
        ProjectReader([library], max_bytes=1).inspect(project)


def test_directory_entry_change_during_scan_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    trigger = project / "09_完本复盘" / "trigger.md"
    trigger.write_text("trigger", encoding="utf-8")
    late = project / "01_项目定位" / "late.md"
    original = project_reader_module._hash_stable_file

    def mutating_hash(path: Path, relative_path: str, **kwargs):
        if path == trigger and not late.exists():
            late.write_text("late", encoding="utf-8")
        return original(path, relative_path, **kwargs)

    monkeypatch.setattr(project_reader_module, "_hash_stable_file", mutating_hash)

    with pytest.raises(ProjectChangedDuringReadError, match="directory entries changed"):
        ProjectReader([library]).inspect(project)


def test_traversal_and_unconfigured_roots_are_rejected(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    project, _ = _project(trusted / "book")
    outside, _ = _project(tmp_path / "outside")
    reader = ProjectReader([trusted])

    with pytest.raises(UntrustedProjectPathError):
        reader.inspect(project / ".." / ".." / outside.name)
    with pytest.raises(UntrustedProjectPathError):
        ProjectReader([])


def test_symlink_to_outside_is_skipped_not_followed(tmp_path: Path) -> None:
    """链接指向项目外面：不跟进去、外面的文件不进快照（9-27 起不再整个扫描失败）。"""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    project, _ = _project(trusted / "book")
    outside = tmp_path / "outside-data"
    outside.mkdir()
    (outside / "CH999.md").write_text("outside", encoding="utf-8")
    link = project / "05_正文内容" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available for this test account")

    snapshot = ProjectReader([trusted]).inspect(project)

    paths = {item["relative_path"] for item in snapshot["files"]}
    assert "05_正文内容/第一卷文件夹/CH001_初稿.md" in paths
    assert not any("CH999" in path or path.startswith("05_正文内容/escape") for path in paths)


def test_junction_is_skipped_and_scan_still_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """联接点（Windows「文档」里就有系统的）：跳过它，别的照常进快照；以前整个扫描失败、文件夹打不开。"""
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    project, _ = _project(trusted / "book")
    junction = project / "05_正文内容" / "junction"
    junction.mkdir()
    (junction / "CH998.md").write_text("inside the junction", encoding="utf-8")
    real_isjunction = getattr(os.path, "isjunction", lambda path: False)

    monkeypatch.setattr(
        os.path,
        "isjunction",
        lambda path: Path(path) == junction or real_isjunction(path),
        raising=False,
    )
    snapshot = ProjectReader([trusted]).inspect(project)

    paths = {item["relative_path"] for item in snapshot["files"]}
    assert "05_正文内容/第一卷文件夹/CH001_初稿.md" in paths
    assert not any(path.startswith("05_正文内容/junction") for path in paths)


def test_dot_and_generated_folders_stay_out_of_the_snapshot(tmp_path: Path) -> None:
    """打开代码仓库当项目（纠偏 P2）：.venv / .runtime 这类点号文件夹、dist / build 等生成文件夹不进快照。"""
    trusted = tmp_path / "trusted"
    repo = trusted / "repo"
    for relative in (
        "src/app.py",
        "README.md",
        ".venv/Lib/site-packages/big.py",
        ".runtime/cache.json",
        "dist/bundle.js",
        "build/out.txt",
        "node_modules/x/index.js",
    ):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    progress: list[tuple[int, int]] = []

    snapshot = ProjectReader([trusted]).inspect(repo, lambda done, total: progress.append((done, total)))

    assert {item["relative_path"] for item in snapshot["files"]} == {"README.md", "src/app.py"}
    # 进度里的「一共多少个」也只数会进快照的。
    assert progress[0] == (0, 2)
