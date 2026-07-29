from __future__ import annotations

from pathlib import Path

import pytest

from zhimeng_runtime.chapter_draft_service import ChapterDraftError, ChapterDraftService
from zhimeng_runtime.project_reader import ProjectReader, SECTION_LABELS


class _ConfiguredProvider:
    configured = True
    model = "test-model"


class _ProviderSettings:
    def effective(self):
        return _ConfiguredProvider()


class _ChangeSets:
    def create(self, *_args, **_kwargs):
        raise AssertionError("unsafe input must fail before creating a ChangeSet")


def _project(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    prose = root / "05_正文内容" / "CH001.md"
    prose.write_text("唯一需要重写的原文。", encoding="utf-8")
    return root, prose


def _service(reader: ProjectReader) -> ChapterDraftService:
    return ChapterDraftService(
        _ChangeSets(),
        provider_settings=_ProviderSettings(),
        project_reader=reader,
    )


def test_rewrite_passage_rejects_relative_path_that_does_not_match_file_id(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    reader = ProjectReader((library,))
    snapshot = reader.inspect(project)
    project_record = snapshot["project"]
    prose_record = next(item for item in snapshot["files"] if item["kind"] == "prose")

    with pytest.raises(ChapterDraftError, match="路径与文件身份对不上"):
        _service(reader).rewrite_passage(
            project_id=project_record["project_id"],
            project_root=project,
            relative_path="../outside.md",
            file_id=prose_record["file_id"],
            passage="唯一需要重写的原文。",
            instruction="更紧张",
            base_revision=project_record["revision"],
            base_fingerprint=project_record["fingerprint"],
        )


def test_rewrite_passage_rejects_file_id_that_is_not_in_current_snapshot(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    reader = ProjectReader((library,))
    snapshot = reader.inspect(project)
    project_record = snapshot["project"]

    with pytest.raises(ChapterDraftError, match="不能安全读取"):
        _service(reader).rewrite_passage(
            project_id=project_record["project_id"],
            project_root=project,
            relative_path="../outside.md",
            file_id="file-00000000000000000000000000000000",
            passage="唯一需要重写的原文。",
            instruction="更紧张",
            base_revision=project_record["revision"],
            base_fingerprint=project_record["fingerprint"],
        )


def test_rewrite_passage_rejects_stale_project_identity_before_provider_call(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    project, _ = _project(library / "book")
    reader = ProjectReader((library,))
    snapshot = reader.inspect(project)
    project_record = snapshot["project"]
    prose_record = next(item for item in snapshot["files"] if item["kind"] == "prose")

    with pytest.raises(ChapterDraftError, match="作品内容已变化"):
        _service(reader).rewrite_passage(
            project_id=project_record["project_id"],
            project_root=project,
            relative_path=prose_record["relative_path"],
            file_id=prose_record["file_id"],
            passage="唯一需要重写的原文。",
            instruction="更紧张",
            base_revision=project_record["revision"] + 1,
            base_fingerprint=project_record["fingerprint"],
        )
