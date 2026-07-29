from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from zhimeng_runtime.local_source_adapter import (
    EXPECTED_SNAPSHOT_HASHES,
    LocalSourceAdapterError,
    ManagedLocalSourceAdapter,
)

from local_source_fixture import (
    CAPABILITY_SCOPE,
    DISTILLATION_OUTPUT_CONTRACT,
    DRAFT_ARTIFACT_KINDS,
    EXPECTED_CARD_TYPES,
    PREPARE_OUTPUT_CONTRACT,
    SNAPSHOT_ROOT,
    SNAPSHOT_SPEC,
    SOURCE_ARTIFACT_KINDS,
    SOURCE_CHAPTER_MARKER_COUNT,
    SOURCE_FIXTURE,
    SOURCE_SHA256,
    SOURCE_SIZE_BYTES,
    assert_fixture_manifest_static_contract,
    sha256_file,
)


def build_adapter(tmp_path: Path, *, trusted_root: Path) -> ManagedLocalSourceAdapter:
    return ManagedLocalSourceAdapter(
        snapshot_root=SNAPSHOT_ROOT,
        state_root=tmp_path / "runtime-state",
        trusted_source_roots=(trusted_root,),
    )


def test_local_source_fixture_manifest_pins_static_inputs_and_scope() -> None:
    assert_fixture_manifest_static_contract()
    managed_hashes = {
        Path(relative_path).name: expected_hash
        for relative_path, expected_hash in SNAPSHOT_SPEC["files"].items()
    }
    assert managed_hashes == EXPECTED_SNAPSHOT_HASHES
    assert CAPABILITY_SCOPE["managed_local_txt_vertical_slice"] == "verified"
    assert {
        name for name, status in CAPABILITY_SCOPE.items() if status == "not_proven"
    } == {
        "external_tomato_downloader_book_id",
        "complete_runtime_owned_project_kb_state",
        "generic_creation_skill_workflow",
    }


def test_managed_local_source_adapter_runs_real_txt_to_draft_chain(
    tmp_path: Path,
) -> None:
    adapter = build_adapter(tmp_path, trusted_root=SOURCE_FIXTURE.parent)
    candidate = adapter.inspect(str(SOURCE_FIXTURE))
    before_hash = sha256_file(SOURCE_FIXTURE)
    progress: list[dict[str, object]] = []
    workspace = tmp_path / "runtime-state" / "source-import-real"

    source = adapter.prepare(
        candidate,
        workspace,
        threading.Event(),
        progress.append,
    )
    draft = adapter.draft(source, workspace, threading.Event())

    assert before_hash == sha256_file(SOURCE_FIXTURE) == SOURCE_SHA256
    assert candidate.source_sha256 == SOURCE_SHA256
    assert candidate.source_size_bytes == SOURCE_SIZE_BYTES
    assert candidate.source_format == PREPARE_OUTPUT_CONTRACT["source_format"]
    assert source.source_backend == PREPARE_OUTPUT_CONTRACT["source_backend"]
    assert source.source_format == PREPARE_OUTPUT_CONTRACT["source_format"]
    assert source.download_scope == PREPARE_OUTPUT_CONTRACT["download_scope"]
    assert source.chapter_marker_count == SOURCE_CHAPTER_MARKER_COUNT
    for field_name in PREPARE_OUTPUT_CONTRACT["sha256_binding_fields"]:
        assert getattr(source, field_name) == SOURCE_SHA256
    assert source.source_file_size_bytes == SOURCE_SIZE_BYTES
    assert SOURCE_ARTIFACT_KINDS <= {item.kind for item in source.artifacts}
    assert progress[-1]["stage"] == "source_ready"

    cards = json.loads(Path(draft.cards_path).read_text(encoding="utf-8"))
    envelope = json.loads(Path(draft.envelope_path).read_text(encoding="utf-8"))
    assert [card["card_type"] for card in cards] == list(EXPECTED_CARD_TYPES)
    assert all(
        card["draft_candidate"] is DISTILLATION_OUTPUT_CONTRACT["draft_candidate"]
        for card in cards
    )
    assert all(
        card["requires_human_distillation"]
        is DISTILLATION_OUTPUT_CONTRACT["requires_human_distillation"]
        for card in cards
    )
    assert envelope["tool_result_envelope"] == DISTILLATION_OUTPUT_CONTRACT[
        "envelope_schema"
    ]
    assert envelope["status"] == DISTILLATION_OUTPUT_CONTRACT["status"]
    assert envelope["checkpoints"]["chapter_count"] == SOURCE_CHAPTER_MARKER_COUNT
    assert envelope["success_criteria"]["ready_for_formal_ingest"] is (
        DISTILLATION_OUTPUT_CONTRACT["ready_for_formal_ingest"]
    )
    assert DRAFT_ARTIFACT_KINDS <= {item.kind for item in draft.artifacts}
    assert draft.card_count == len(EXPECTED_CARD_TYPES)
    assert adapter.execution_identity() == adapter.execution_identity()


def test_local_source_adapter_rejects_changed_bytes_before_prepare(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted"
    trusted_root.mkdir()
    source_path = trusted_root / "source.txt"
    source_path.write_bytes(SOURCE_FIXTURE.read_bytes())
    adapter = build_adapter(tmp_path, trusted_root=trusted_root)
    candidate = adapter.inspect(str(source_path))
    source_path.write_text("changed after inspection", encoding="utf-8")

    with pytest.raises(LocalSourceAdapterError, match="changed after selection"):
        adapter.prepare(
            candidate,
            tmp_path / "runtime-state" / "tampered",
            threading.Event(),
            lambda _progress: None,
        )


def test_local_source_adapter_cleanup_is_scoped_and_hashed(tmp_path: Path) -> None:
    adapter = build_adapter(tmp_path, trusted_root=SOURCE_FIXTURE.parent)
    workspace = tmp_path / "runtime-state" / "failed-import"
    workspace.mkdir(parents=True)
    (workspace / "partial.txt").write_text("partial", encoding="utf-8")

    status, cleanup_hash = adapter.cleanup_failed_workspace(workspace)

    assert status == "clean"
    assert len(cleanup_hash) == 64
    assert not workspace.exists()
    assert SOURCE_FIXTURE.exists()
