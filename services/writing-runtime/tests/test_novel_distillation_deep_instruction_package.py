from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.managed_skill_service import ManagedSkillService
from zhimeng_runtime.skill_models import SkillManifest


REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = (
    REPO_ROOT / "adapters" / "builtin" / "novel-distillation-deep-instruction"
)
SOURCE_RELATIVE_PATH = "references/bundled-skills/novel-distillation/SKILL.md"
SOURCE_SHA256 = "f4bffb45f6881ebb31ef2a4187ba92d5de4f6c241f7870308458e72bb9bfd003"
SOURCE_BYTES = 25_674
PACKAGE_ID = "novel-distillation.deep-instruction"


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    item_stat = path.lstat()
    return (
        path.is_symlink()
        or bool(int(getattr(item_stat, "st_file_attributes", 0)) & 0x400)
        or bool(getattr(os.path, "isjunction", lambda _: False)(path))
        or stat.S_ISLNK(item_stat.st_mode)
    )


def test_deep_distillation_instruction_package_is_bounded_and_managed(
    tmp_path: Path,
) -> None:
    manifest_payload = _json(PACKAGE_ROOT / "skill.manifest.json")
    manifest = SkillManifest.model_validate(manifest_payload, strict=True)

    assert manifest.id == PACKAGE_ID
    assert manifest.kind == "instruction_source"
    assert manifest.entrypoint is None
    assert manifest.timeout_seconds is None
    assert manifest.exit_codes is None
    assert manifest.input_schema is None
    assert manifest.output_schema is None
    assert manifest.artifacts == []
    assert manifest.permissions.read == []
    assert manifest.permissions.write == []
    assert manifest.permissions.network is False
    assert manifest.permissions.secrets == []
    assert {
        "entrypoint",
        "timeout_seconds",
        "exit_codes",
        "input_schema",
        "output_schema",
    }.isdisjoint(manifest_payload)

    expected_resources = [
        "instruction-contract.json",
        "instruction-map.json",
        SOURCE_RELATIVE_PATH,
    ]
    assert manifest.resources == expected_resources
    for resource in manifest.resources:
        relative = PurePosixPath(resource)
        assert not relative.is_absolute()
        assert all(part not in {"", ".", ".."} for part in relative.parts)
        assert (PACKAGE_ROOT / Path(*relative.parts)).is_file()

    for path in [PACKAGE_ROOT, *PACKAGE_ROOT.rglob("*")]:
        assert not _is_link_or_reparse(path), path
    for path in PACKAGE_ROOT.rglob("*"):
        if path.is_file():
            assert stat.S_ISREG(path.stat(follow_symlinks=False).st_mode), path

    source_path = PACKAGE_ROOT / SOURCE_RELATIVE_PATH
    source_hash = _sha256(source_path)
    assert source_path.stat().st_size == SOURCE_BYTES
    assert source_hash == SOURCE_SHA256

    provenance = (PACKAGE_ROOT / "PROVENANCE.md").read_text(encoding="utf-8")
    provenance_hashes = re.findall(r"`([0-9a-f]{64})`", provenance)
    assert provenance_hashes == [SOURCE_SHA256]
    assert f"| {SOURCE_BYTES} | `{SOURCE_SHA256}` |" in provenance
    assert "ordinary file, byte-for-byte, with no links" in provenance

    instruction_map = _json(PACKAGE_ROOT / "instruction-map.json")
    assert instruction_map["package_id"] == PACKAGE_ID
    assert instruction_map["source_path"] == SOURCE_RELATIVE_PATH
    assert instruction_map["source_sha256"] == SOURCE_SHA256
    assert instruction_map["source_bytes"] == SOURCE_BYTES
    assert instruction_map["source_line_count"] == 262
    assert instruction_map["contract_path"] == "instruction-contract.json"
    assert [
        (item["line_start"], item["line_end"])
        for item in instruction_map["included_ranges"]
    ] == [(100, 110), (112, 119), (142, 161), (189, 212), (214, 237)]
    assert "call_provider" in instruction_map["excluded_authority"]
    assert "write_project_truth" in instruction_map["excluded_authority"]

    contract = _json(PACKAGE_ROOT / "instruction-contract.json")
    assert contract["package_id"] == PACKAGE_ID
    assert contract["source"] == {
        "path": SOURCE_RELATIVE_PATH,
        "sha256": SOURCE_SHA256,
        "bytes": SOURCE_BYTES,
        "line_count": 262,
        "copy_mode": "ordinary_file_byte_for_byte",
    }
    assert contract["runtime_boundary"] == {
        "runtime_owned": True,
        "package_execution_allowed": False,
        "provider_call_allowed": False,
        "project_read_allowed": False,
        "project_write_allowed": False,
        "library_mutation_allowed": False,
        "nlcl_sync_allowed": False,
        "formalization_allowed": False,
    }

    output = contract["output"]
    assert output["candidate_state"] == "pending"
    assert output["candidate_count_exact"] == 3
    assert output["allowed_candidate_types"] == ["work", "voice", "validator"]
    assert output["candidate_slots"] == [
        {"candidate_type": "work", "count_exact": 1},
        {"candidate_type": "voice", "count_exact": 1},
        {"candidate_type": "validator", "count_exact": 1},
    ]
    assert output["required_flags"] == {
        "draft_candidate": True,
        "requires_author_review": True,
        "formalization_allowed": False,
    }
    assert output["anchor_evidence_refs_per_candidate_minimum"] == 1
    assert output["anchor_refs_must_resolve_to_runtime_input"] is True
    assert output["additional_candidate_types_allowed"] is False
    assert output["direct_project_write_allowed"] is False

    contract_input = contract["input"]
    assert contract_input["runtime_supplied_anchor_evidence_required"] is True
    assert contract_input["minimum_anchor_count"] == 1
    assert contract_input["anchor_required_fields"] == [
        "anchor_id",
        "artifact_id",
        "content_hash",
        "locator",
    ]
    assert contract_input["live_codex_skill_read_allowed"] is False

    contamination = contract["anti_contamination"]
    assert set(contamination["forbidden_transfer"]) == {
        "source_proper_names",
        "distinctive_setting_shells",
        "recognizable_scene_order_or_plot_shells",
        "signature_phrases",
        "long_source_passages",
        "recognizable_author_voice_imitation",
    }
    assert contamination["source_excerpt_policy"]["long_passage_storage_allowed"] is False
    assert contamination["project_truth_override_allowed"] is False
    assert set(contract["forbidden_claims"]) >= {
        "formal_library_ingestion_complete",
        "formal_card_created",
        "project_truth_updated",
    }

    app_source = (
        REPO_ROOT
        / "services"
        / "writing-runtime"
        / "src"
        / "zhimeng_runtime"
        / "app.py"
    ).read_text(encoding="utf-8")
    assert PACKAGE_ID not in app_source
    assert "novel-distillation-deep-instruction" not in app_source

    store = SqliteStore(tmp_path / "runtime.sqlite3")
    store.initialize()
    managed_root = tmp_path / "managed-skills"
    service = ManagedSkillService(store, managed_root)
    first = service.register_builtin(PACKAGE_ROOT)
    second = service.register_builtin(PACKAGE_ROOT)

    assert second.skill_version_id == first.skill_version_id
    assert second.manifest_fingerprint == first.manifest_fingerprint
    assert second.package_fingerprint == first.package_fingerprint
    assert first.package_root != PACKAGE_ROOT.resolve()
    assert first.package_root.is_relative_to(managed_root.resolve())
    assert ".codex" not in {part.casefold() for part in first.package_root.parts}
    assert service.verify_active(first) == first.package_fingerprint
    assert first.instruction_map_path is not None
    assert first.instruction_map_path.read_bytes() == (
        PACKAGE_ROOT / "instruction-map.json"
    ).read_bytes()
    for resource in manifest.resources:
        assert (first.package_root / resource).read_bytes() == (
            PACKAGE_ROOT / resource
        ).read_bytes()
