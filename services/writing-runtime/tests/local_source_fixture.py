from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_MANIFEST_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "local-source"
    / "archive-night-sample.manifest.json"
)


def _load_manifest() -> dict[str, Any]:
    payload = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("local source fixture manifest must be a JSON object")
    return payload


LOCAL_SOURCE_FIXTURE_MANIFEST = _load_manifest()
SOURCE_SPEC = LOCAL_SOURCE_FIXTURE_MANIFEST["source"]
SNAPSHOT_SPEC = LOCAL_SOURCE_FIXTURE_MANIFEST["managed_snapshot"]
ADJACENT_CONTRACTS = LOCAL_SOURCE_FIXTURE_MANIFEST["adjacent_contracts"]
CAPABILITY_SCOPE = LOCAL_SOURCE_FIXTURE_MANIFEST["capability_scope"]

SOURCE_FIXTURE = (FIXTURE_MANIFEST_PATH.parent / SOURCE_SPEC["relative_path"]).resolve()
SOURCE_SHA256 = str(SOURCE_SPEC["sha256"])
SOURCE_SIZE_BYTES = int(SOURCE_SPEC["size_bytes"])
SOURCE_CHAPTER_MARKER_COUNT = int(SOURCE_SPEC["chapter_marker_count"])
SNAPSHOT_ROOT = (
    REPOSITORY_ROOT / str(SNAPSHOT_SPEC["repository_relative_root"])
).resolve()

INSPECT_OUTPUT_CONTRACT = ADJACENT_CONTRACTS["inspect_output"]
PREPARE_OUTPUT_CONTRACT = ADJACENT_CONTRACTS["prepare_output"]
DISTILLATION_OUTPUT_CONTRACT = ADJACENT_CONTRACTS["distillation_output"]
WRITER_OUTPUT_CONTRACT = ADJACENT_CONTRACTS["writer_output"]

SOURCE_ARTIFACT_KINDS = frozenset(PREPARE_OUTPUT_CONTRACT["required_artifact_kinds"])
DRAFT_ARTIFACT_KINDS = frozenset(
    DISTILLATION_OUTPUT_CONTRACT["required_artifact_kinds"]
)
EXPECTED_CARD_TYPES = tuple(DISTILLATION_OUTPUT_CONTRACT["card_types"])
EXPECTED_CARD_TYPE_SET = frozenset(EXPECTED_CARD_TYPES)
EXPECTED_WORKFLOW_SKILL_IDS = tuple(WRITER_OUTPUT_CONTRACT["workflow_skill_ids"])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_fixture_manifest_static_contract() -> None:
    assert LOCAL_SOURCE_FIXTURE_MANIFEST["schema"] == (
        "zhimeng.local-source-acceptance-fixture.v1"
    )
    assert LOCAL_SOURCE_FIXTURE_MANIFEST["fixture_id"] == "archive-night-sample"
    assert LOCAL_SOURCE_FIXTURE_MANIFEST["fixture_version"] == "2026.07.23.1"
    assert SOURCE_SPEC["format"] == "txt"
    assert SOURCE_SPEC["encoding"] == "utf-8"
    assert SOURCE_FIXTURE.parent == FIXTURE_MANIFEST_PATH.parent.resolve()
    assert SOURCE_FIXTURE.is_file()
    assert SOURCE_FIXTURE.stat().st_size == SOURCE_SIZE_BYTES
    assert sha256_file(SOURCE_FIXTURE) == SOURCE_SHA256
    SOURCE_FIXTURE.read_text(encoding="utf-8")

    managed_files = SNAPSHOT_SPEC["files"]
    assert isinstance(managed_files, dict)
    assert len(managed_files) == 3
    for relative_path, expected_hash in managed_files.items():
        snapshot_path = (SNAPSHOT_ROOT / relative_path).resolve(strict=True)
        assert snapshot_path.is_relative_to(SNAPSHOT_ROOT)
        assert snapshot_path.is_file()
        assert sha256_file(snapshot_path) == expected_hash

    assert INSPECT_OUTPUT_CONTRACT == {
        "source_format": "txt",
        "source_size_bytes": SOURCE_SIZE_BYTES,
        "source_sha256": SOURCE_SHA256,
    }
    assert set(PREPARE_OUTPUT_CONTRACT["sha256_binding_fields"]) == {
        "source_file_sha256",
        "packet_source_sha256",
        "text_sha256",
    }
    assert CAPABILITY_SCOPE == {
        "managed_local_txt_vertical_slice": "verified",
        "external_tomato_downloader_book_id": "not_proven",
        "complete_runtime_owned_project_kb_state": "not_proven",
        "generic_creation_skill_workflow": "not_proven",
    }


__all__ = [
    "CAPABILITY_SCOPE",
    "DISTILLATION_OUTPUT_CONTRACT",
    "DRAFT_ARTIFACT_KINDS",
    "EXPECTED_CARD_TYPES",
    "EXPECTED_CARD_TYPE_SET",
    "EXPECTED_WORKFLOW_SKILL_IDS",
    "FIXTURE_MANIFEST_PATH",
    "INSPECT_OUTPUT_CONTRACT",
    "LOCAL_SOURCE_FIXTURE_MANIFEST",
    "PREPARE_OUTPUT_CONTRACT",
    "SNAPSHOT_ROOT",
    "SNAPSHOT_SPEC",
    "SOURCE_ARTIFACT_KINDS",
    "SOURCE_CHAPTER_MARKER_COUNT",
    "SOURCE_FIXTURE",
    "SOURCE_SHA256",
    "SOURCE_SIZE_BYTES",
    "WRITER_OUTPUT_CONTRACT",
    "assert_fixture_manifest_static_contract",
    "sha256_file",
]
