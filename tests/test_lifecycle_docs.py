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
M3J_IMPLEMENTATION = "docs/M3J_CREATION_HANDOFF_IMPLEMENTATION.md"
M3J_EVIDENCE = "evidence/creation-handoff-validation-20260722.md"
M3J_CANONICAL_CHAIN = "project-audit -> prewrite-gate -> creation-handoff"
M3K_IMPLEMENTATION = "docs/M3K_AGENT_CANDIDATE_RECOVERY_IMPLEMENTATION.md"
M3K_EVIDENCE = "evidence/agent-candidate-recovery-validation-20260723.md"
M3K_DESKTOP_SCREENSHOT = (
    "evidence/provider-recovery-decision-desktop-1440x900.png"
)
M3K_CANDIDATE_CONTRACT = "packages/contracts/agent-candidate-output.schema.json"
M3L_IMPLEMENTATION = "docs/M3L_MANAGED_SKILL_LIFECYCLE_IMPLEMENTATION.md"
M3L_EVIDENCE = "evidence/managed-skill-lifecycle-validation-20260722.md"
M3L_CATALOG_CONTRACT = "packages/contracts/managed-skill-catalog.schema.json"
API_COMPATIBILITY = "docs/API_COMPATIBILITY.md"
FOUR_SKILLS_CAPABILITY_MAP = "docs/FOUR_SKILLS_CAPABILITY_MAP.md"
TEST_AND_EVALUATION_STRATEGY = "docs/TEST_AND_EVALUATION_STRATEGY.md"
WORKBENCH_README = "apps/workbench/README.md"
CONTRACTS_README = "packages/contracts/README.md"
PROVIDER_TURN_RECEIPTS_PATH = (
    "/api/v1/agent/runs/{run_id}/provider-turn-receipts"
)


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
        _replace_once("SQLite schema v20 共 33 张表", "SQLite schema v19 共 33 张表"),
    )

    assert any(
        "README.md is missing current-state claim: SQLite schema v20 共 33 张表"
        in error
        for error in errors
    )


def test_data_model_table_count_drift_from_runtime_is_rejected() -> None:
    errors = _validate_mutation(
        "docs/DATA_MODEL.md",
        _replace_once("下图对应当前 33 张表", "下图对应当前 32 张表"),
    )

    assert any(
        "docs/DATA_MODEL.md is missing current-state claim: 下图对应当前 33 张表"
        in error
        for error in errors
    )


def test_readme_project_kb_binding_pair_is_required() -> None:
    errors = _validate_mutation(
        "README.md",
        lambda text: text.replace(
            "project_kb_manifest_hash",
            "removed_kb_manifest_digest",
        ),
    )

    assert any(
        "README.md is missing current-state claim: project_kb_manifest_hash" in error
        for error in errors
    )


def test_legacy_project_kb_null_rows_cannot_be_documented_as_executable() -> None:
    errors = _validate_mutation(
        "README.md",
        _replace_once(
            "v19 历史 NULL 记录可读但不可执行",
            "v19 历史 NULL 记录一律视为 current",
        ),
    )

    assert any(
        "README.md is missing current-state fact: "
        "v19 NULL project-KB bindings are readable but not executable" in error
        for error in errors
    )


def test_contract_readme_must_document_project_kb_binding_pair() -> None:
    errors = _validate_mutation(
        CONTRACTS_README,
        lambda text: text.replace(
            "project_kb_state_id",
            "removed_kb_state_identity",
        ),
    )

    assert any(
        f"{CONTRACTS_README} is missing current-state claim: project_kb_state_id"
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


def test_m3j_canonical_three_step_chain_is_required() -> None:
    errors = _validate_mutation(
        M3J_IMPLEMENTATION,
        lambda text: text.replace(
            M3J_CANONICAL_CHAIN,
            "project-audit -> prewrite-gate",
        ),
    )

    assert any(
        f"{M3J_IMPLEMENTATION} is missing canonical M3j workflow chain: "
        f"{M3J_CANONICAL_CHAIN}" in error
        for error in errors
    )


def test_m3j_formal_authorization_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3J_IMPLEMENTATION,
        lambda text: text.replace(
            "formal_draft_allowed=false",
            "formal authorization omitted",
        ),
    )

    assert any(
        f"{M3J_IMPLEMENTATION} is missing M3j boundary: "
        "formal_draft_allowed=false" in error
        for error in errors
    )


def test_m3j_provider_candidate_gap_is_required() -> None:
    errors = _validate_mutation(
        M3J_IMPLEMENTATION,
        lambda text: text.replace("Provider 后", "Provider boundary 之后"),
    )

    assert any(
        f"{M3J_IMPLEMENTATION} is missing M3j boundary: Provider 后" in error
        for error in errors
    )


def test_m3j_evidence_id_is_required() -> None:
    errors = _validate_mutation(
        M3J_EVIDENCE,
        _replace_once(
            "Evidence ID: `VAL-M3J-20260722`",
            "Evidence ID: `VAL-M3J-MISSING`",
        ),
    )

    assert any(
        f"{M3J_EVIDENCE} has the wrong or missing Evidence ID" in error
        for error in errors
    )


def test_m3k_candidate_terminal_tool_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3K_IMPLEMENTATION,
        lambda text: text.replace(
            "candidate_output_submit",
            "candidate terminal tool omitted",
        ),
    )

    assert any(
        f"{M3K_IMPLEMENTATION} is missing M3k boundary: "
        "candidate_output_submit" in error
        for error in errors
    )


def test_m3k_formal_authorization_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3K_IMPLEMENTATION,
        lambda text: text.replace(
            "formal_draft_allowed=false",
            "formal authorization omitted",
        ),
    )

    assert any(
        f"{M3K_IMPLEMENTATION} is missing M3k boundary: "
        "formal_draft_allowed=false" in error
        for error in errors
    )


def test_m3k_zero_provider_replay_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3K_IMPLEMENTATION,
        lambda text: text.replace(
            "provider_replay_count=0",
            "provider replay policy omitted",
        ),
    )

    assert any(
        f"{M3K_IMPLEMENTATION} is missing M3k boundary: "
        "provider_replay_count=0" in error
        for error in errors
    )


def test_m3k_evidence_id_is_required() -> None:
    errors = _validate_mutation(
        M3K_EVIDENCE,
        _replace_once(
            "Evidence ID: `VAL-M3K-20260723`",
            "Evidence ID: `VAL-M3K-MISSING`",
        ),
    )

    assert any(
        f"{M3K_EVIDENCE} has the wrong or missing Evidence ID" in error
        for error in errors
    )


def test_m3k_screenshot_hash_binding_is_required() -> None:
    screenshot_hash = hashlib.sha256(
        (ROOT / M3K_DESKTOP_SCREENSHOT).read_bytes()
    ).hexdigest().upper()
    errors = _validate_mutation(
        M3K_EVIDENCE,
        _replace_once(screenshot_hash, "0" * 64),
    )

    assert any(
        f"{M3K_EVIDENCE} does not bind current screenshot SHA-256 "
        f"{screenshot_hash}" in error
        for error in errors
    )


def test_m3k_contract_hash_binding_is_required() -> None:
    contract_hash = hashlib.sha256(
        (ROOT / M3K_CANDIDATE_CONTRACT).read_bytes()
    ).hexdigest().upper()
    errors = _validate_mutation(
        M3K_EVIDENCE,
        _replace_once(contract_hash, "0" * 64),
    )

    assert any(
        f"{M3K_EVIDENCE} does not bind current contract "
        f"{M3K_CANDIDATE_CONTRACT} SHA-256 {contract_hash}" in error
        for error in errors
    )


def test_current_candidate_v3_is_required_in_core_documents() -> None:
    errors = _validate_mutation(
        TEST_AND_EVALUATION_STRATEGY,
        _replace_once("agent-candidate-output.v3", "agent-candidate-output.v2"),
    )

    assert any(
        f"{TEST_AND_EVALUATION_STRATEGY} is missing current-state claim: "
        "agent-candidate-output.v3" in error
        for error in errors
    )


def test_m3k_two_phase_recovery_cutpoint_is_required() -> None:
    errors = _validate_mutation(
        M3K_IMPLEMENTATION,
        lambda text: text.replace("Candidate recovery cutpoint", "terminal bundle"),
    )

    assert any(
        f"{M3K_IMPLEMENTATION} is missing M3k v3 boundary: "
        "Candidate recovery cutpoint" in error
        for error in errors
    )


def test_legacy_candidates_cannot_complete_current_run() -> None:
    errors = _validate_mutation(
        API_COMPATIBILITY,
        lambda text: text.replace("v1/v2", "legacy-removed"),
    )

    assert any(
        f"{API_COMPATIBILITY} is missing current-state fact: "
        "legacy Candidate v1/v2 is read-only and cannot complete a current Run"
        in error
        for error in errors
    )


def test_provider_turn_receipt_get_is_required_in_api_docs() -> None:
    errors = _validate_mutation(
        API_COMPATIBILITY,
        _replace_once(
            PROVIDER_TURN_RECEIPTS_PATH,
            "/api/v1/agent/runs/{run_id}/provider-receipts-omitted",
        ),
    )

    assert any(
        f"{API_COMPATIBILITY} is missing current-state claim: "
        f"{PROVIDER_TURN_RECEIPTS_PATH}" in error
        for error in errors
    )


def test_managed_local_txt_three_type_chain_is_required() -> None:
    errors = _validate_mutation(
        FOUR_SKILLS_CAPABILITY_MAP,
        _replace_once(
            "当前 Runtime 的版本化本地 TXT 代表性链已走通",
            "本地来源能力摘要",
        ),
    )

    assert any(
        f"{FOUR_SKILLS_CAPABILITY_MAP} is missing current-state fact: "
        "managed local TXT work/voice/validator review and batch formalization"
        in error
        for error in errors
    )


def test_context_three_stage_author_confirmation_is_required() -> None:
    claim = "编译 Context -> 审查证据并选择 Writer 模式 -> 明确确认运行"
    errors = _validate_mutation(
        "README.md",
        _replace_once(claim, "编译 Context -> 直接运行"),
    )

    assert any(
        f"README.md is missing current-state claim: {claim}" in error
        for error in errors
    )


def test_workbench_readme_cannot_revert_to_two_step_context_review() -> None:
    current = "Context Pack 编译、证据/Writer 模式审查、明确确认三阶段"
    errors = _validate_mutation(
        WORKBENCH_README,
        _replace_once(current, "Context Pack 两步审查"),
    )

    assert any(
        f"{WORKBENCH_README} is missing current-state claim: {current}" in error
        for error in errors
    )


def test_m3l_current_fact_is_required_in_core_documents() -> None:
    errors = _validate_mutation(
        "README.md",
        _replace_once(
            "M3l 应用级/项目级受控 Skill 生命周期",
            "受管 Skill 生命周期状态省略",
        ),
    )

    assert any(
        "README.md is missing current-state claim: "
        "M3l 应用级/项目级受控 Skill 生命周期" in error
        for error in errors
    )


def test_m3l_raw_skill_adaptation_boundary_is_required() -> None:
    errors = _validate_mutation(
        M3L_IMPLEMENTATION,
        lambda text: text.replace(
            "managed_skill_adaptation_required",
            "raw skill adaptation boundary omitted",
        ),
    )

    assert any(
        f"{M3L_IMPLEMENTATION} is missing M3l boundary: "
        "managed_skill_adaptation_required" in error
        for error in errors
    )


def test_m3l_workbench_lifecycle_ui_nonproof_is_required() -> None:
    errors = _validate_mutation(
        M3L_IMPLEMENTATION,
        lambda text: text.replace(
            "管理 UI",
            "Workbench lifecycle boundary omitted",
        ),
    )

    assert any(
        f"{M3L_IMPLEMENTATION} is missing M3l boundary: 管理 UI"
        in error
        for error in errors
    )


def test_m3l_evidence_id_is_required() -> None:
    errors = _validate_mutation(
        M3L_EVIDENCE,
        _replace_once(
            "Evidence ID: `VAL-M3L-20260722`",
            "Evidence ID: `VAL-M3L-MISSING`",
        ),
    )

    assert any(
        f"{M3L_EVIDENCE} has the wrong or missing Evidence ID" in error
        for error in errors
    )


def test_m3l_contract_hash_binding_is_required() -> None:
    contract_hash = hashlib.sha256(
        (ROOT / M3L_CATALOG_CONTRACT).read_bytes()
    ).hexdigest().upper()
    errors = _validate_mutation(
        M3L_EVIDENCE,
        _replace_once(contract_hash, "0" * 64),
    )

    assert any(
        f"{M3L_EVIDENCE} does not bind current contract "
        f"{M3L_CATALOG_CONTRACT} SHA-256 {contract_hash}" in error
        for error in errors
    )
