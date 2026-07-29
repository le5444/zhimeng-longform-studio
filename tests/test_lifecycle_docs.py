from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from verify_lifecycle_docs import validate_repository  # noqa: E402


PRODUCT_REQUIREMENTS = "docs/PRODUCT_REQUIREMENTS.md"
NON_FUNCTIONAL_REQUIREMENTS = "docs/NON_FUNCTIONAL_REQUIREMENTS.md"
TRACEABILITY = "docs/REQUIREMENTS_TRACEABILITY.md"
M3H_IMPLEMENTATION = "docs/M3H_POST_DRAFT_GATE_IMPLEMENTATION.md"
M3I_IMPLEMENTATION = "docs/M3I_DURABLE_WORKFLOW_IMPLEMENTATION.md"
M3I_EVIDENCE = "evidence/durable-workflow-validation-20260721.md"
M3I_DESKTOP_SCREENSHOT = "evidence/durable-workflow-resume-desktop-1440x900.png"


def _validate_mutation(
    relative: str,
    mutate: Callable[[str], str],
) -> list[str]:
    original = (ROOT / relative).read_text(encoding="utf-8")
    mutated = mutate(original)
    assert mutated != original, f"test mutation did not change {relative}"
    return validate_repository(ROOT, text_overrides={relative: mutated})


def _replace_once(old: str, new: str) -> Callable[[str], str]:
    def mutate(text: str) -> str:
        assert text.count(old) >= 1, f"missing mutation target: {old}"
        return text.replace(old, new, 1)

    return mutate


def test_lifecycle_documents_are_complete_and_consistent() -> None:
    assert validate_repository(ROOT) == []


def test_deleted_acceptance_definition_is_rejected() -> None:
    errors = _validate_mutation(
        PRODUCT_REQUIREMENTS,
        _replace_once("`AC-FR-PROJ-001-03`", "`REMOVED-AC-FR-PROJ-001-03`"),
    )

    assert any(
        "AC-FR-PROJ-001-03" in error
        and ("never defined" in error or "undefined stable ID" in error)
        for error in errors
    )


def test_traceability_must_cover_every_acceptance() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once("`AC-FR-PROJ-001-03`", "removed acceptance"),
    )

    assert any(
        error.endswith("AC-FR-PROJ-001-03")
        and "missing from functional traceability table" in error
        for error in errors
    )


def test_native_knowledge_acceptance_is_part_of_traceability_coverage() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once("`AC-FR-KB-001-03`", "removed acceptance"),
    )

    assert any(
        error.endswith("AC-FR-KB-001-03")
        and "missing from functional traceability table" in error
        for error in errors
    )


def test_duplicate_stable_id_definition_is_rejected() -> None:
    errors = _validate_mutation(
        PRODUCT_REQUIREMENTS,
        lambda text: text + "\n\n#### `FR-PROJ-001` 重复定义\n\n不得接受重复定义。\n",
    )

    assert any(
        "duplicate stable ID definition: FR-PROJ-001" in error
        for error in errors
    )


def test_invalid_traceability_status_is_rejected() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once("`partial_current`：", "`mostly_current`："),
    )

    assert any("invalid traceability status: mostly_current" in error for error in errors)


def test_nfr_implementation_column_rejects_measurement_status() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once(
            "| `not_current` | `not_current`：",
            "| `not_measured` | `not_current`：",
        ),
    )

    assert any("invalid traceability status: not_measured" in error for error in errors)


def test_nfr_measurement_column_rejects_implementation_status() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once(
            "| `partial_current` | `not_measured`：",
            "| `partial_current` | `verified_current`：",
        ),
    )

    assert any(
        "invalid traceability measurement status: verified_current" in error
        for error in errors
    )


def test_invalid_nfr_measurement_status_is_rejected() -> None:
    errors = _validate_mutation(
        NON_FUNCTIONAL_REQUIREMENTS,
        _replace_once("当前状态：`not_measured`", "当前状态：`mostly_measured`"),
    )

    assert any(
        "invalid NFR measurement status" in error and "mostly_measured" in error
        for error in errors
    )


def test_unresolved_source_locator_is_rejected() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once("`project_reader.py`", "`missing_project_reader.py`"),
    )

    assert any(
        "unresolved source locator: missing_project_reader.py" in error
        for error in errors
    )


def test_ambiguous_bare_source_locator_is_rejected() -> None:
    errors = _validate_mutation(
        TRACEABILITY,
        _replace_once("`project_reader.py`", "`README.md`"),
    )

    assert any(
        "ambiguous bare source locator: README.md" in error
        for error in errors
    )


def test_readme_schema_drift_from_runtime_is_rejected() -> None:
    errors = _validate_mutation(
        "README.md",
        _replace_once("SQLite schema v16 共 28 张表", "SQLite schema v15 共 27 张表"),
    )

    assert any(
        "README.md is missing current-state claim: SQLite schema v16 共 28 张表"
        in error
        for error in errors
    )


def test_data_model_table_count_drift_from_runtime_is_rejected() -> None:
    errors = _validate_mutation(
        "docs/DATA_MODEL.md",
        _replace_once("下图对应当前 28 张表", "下图对应当前 27 张表"),
    )

    assert any(
        "docs/DATA_MODEL.md is missing current-state claim: 下图对应当前 28 张表"
        in error
        for error in errors
    )


def test_m3h_formal_authorization_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3H_IMPLEMENTATION,
        lambda text: text.replace("formal_draft_allowed=false", "formal authorization omitted"),
    )

    assert any(
        f"{M3H_IMPLEMENTATION} is missing M3h boundary: formal_draft_allowed=false"
        in error
        for error in errors
    )


def test_m3i_formal_authorization_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3I_IMPLEMENTATION,
        lambda text: text.replace(
            "formal_draft_allowed=false",
            "formal authorization omitted",
        ),
    )

    assert any(
        f"{M3I_IMPLEMENTATION} is missing M3i boundary: formal_draft_allowed=false"
        in error
        for error in errors
    )


def test_m3i_evidence_id_is_required() -> None:
    errors = _validate_mutation(
        M3I_EVIDENCE,
        _replace_once(
            "Evidence ID: `VAL-M3I-20260721`",
            "Evidence ID: `VAL-M3I-MISSING`",
        ),
    )

    assert any(
        f"{M3I_EVIDENCE} has the wrong or missing Evidence ID" in error
        for error in errors
    )


def test_m3i_screenshot_hash_binding_is_required() -> None:
    screenshot_hash = hashlib.sha256(
        (ROOT / M3I_DESKTOP_SCREENSHOT).read_bytes()
    ).hexdigest().upper()
    errors = _validate_mutation(
        M3I_EVIDENCE,
        _replace_once(screenshot_hash, "0" * 64),
    )

    assert any(
        f"{M3I_EVIDENCE} does not bind current screenshot SHA-256 {screenshot_hash}"
        in error
        for error in errors
    )
