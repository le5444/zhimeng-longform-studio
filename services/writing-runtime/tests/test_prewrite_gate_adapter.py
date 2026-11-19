from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextAuthority,
    ContextCandidateFragment,
    ContextCreateRequest,
    canonical_json_hash,
)
from zhimeng_runtime.knowledge_models import knowledge_formalization_receipt_id
from zhimeng_runtime.nlcl_models import (
    NlclRouteSource,
    NlclSelectedCard,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.prewrite_models import build_prewrite_operation_package
from zhimeng_runtime.provenance_models import KnowledgeProvenanceRef
from zhimeng_runtime.skill_models import CreationPrewriteGateResult
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_SCRIPT = (
    REPOSITORY_ROOT
    / "adapters"
    / "builtin"
    / "creation-suite-prewrite-gate"
    / "run_prewrite_gate.py"
)


def _candidate(
    source_id: str,
    *,
    source_type: str,
    title: str,
    authority: ContextAuthority,
    content: str,
    version: str = "revision-1",
    knowledge_provenance: KnowledgeProvenanceRef | None = None,
) -> ContextCandidateFragment:
    return ContextCandidateFragment(
        source_id=source_id,
        source_type=source_type,
        title=title,
        authority=authority,
        version=version,
        source_hash=canonical_json_hash({"source": source_id}),
        content=content,
        priority=900,
        mandatory=authority is ContextAuthority.PROJECT_TRUTH,
        eligible=True,
        knowledge_provenance=knowledge_provenance,
    )


def _operation_package(*, extra_missing_requirements: tuple[str, ...] = ()):
    knowledge_hash = canonical_json_hash({"source": "knowledge-card:pressure-card"})
    knowledge_version = "knowledge-version-" + "b" * 32
    source_import_id = "source-import-" + "1" * 32
    provenance = KnowledgeProvenanceRef(
        source_import_id=source_import_id,
        source_receipt_id=source_import_receipt_id(source_import_id),
        source_receipt_hash="2" * 64,
        source_packet_fingerprint="3" * 64,
        cards_draft_hash="4" * 64,
        candidate_id="knowledge-candidate-" + "5" * 32,
        candidate_origin_hash="6" * 64,
        card_id="pressure-card",
        version_id=knowledge_version,
        version_content_hash=knowledge_hash,
        version_lineage_receipt_id="knowledge-lineage-" + "7" * 32,
        version_lineage_receipt_hash="8" * 64,
        formalization_receipt_id=knowledge_formalization_receipt_id(source_import_id),
        formalization_receipt_hash="9" * 64,
    )
    pack = compile_context_pack(
        context_pack_id="context-pack-prewrite-adapter",
        session_id="session-prewrite-adapter",
        project_id="project-prewrite-adapter",
        history_through_ordinal=0,
        request=ContextCreateRequest(
            objective="Write a bounded risk draft for CH001.",
            profile="chapter",
            active_chapter_id="CH001",
            expected_project_revision=1,
            expected_project_fingerprint="a" * 64,
            project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            project_kb_manifest_hash="b" * 64,
        ),
        history=(),
        candidates=(
            _candidate(
                "project:character-state",
                source_type="project_section",
                title="03_人物设定/人物状态.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                content="The protagonist refuses to sacrifice the witness.",
            ),
            _candidate(
                "project:active-chapter",
                source_type="active_chapter",
                title="05_正文内容/第一卷/CH001_正文.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                content="The witness left a blood-stained key.",
            ),
            _candidate(
                "project:chapter-control",
                source_type="chapter_control",
                title="04_剧情大纲/CH001_控制卡.md",
                authority=ContextAuthority.PROJECT_TRUTH,
                content="Goal: recover the witness. Cost: expose the investigator.",
            ),
            _candidate(
                "knowledge-card:pressure-card",
                source_type="native_knowledge_card:work",
                title="Observable pressure escalation",
                authority=ContextAuthority.FORMAL_KNOWLEDGE,
                content='{"summary":"Escalate observable pressure."}',
                version=knowledge_version,
                knowledge_provenance=provenance,
            ),
        ),
        max_characters=20_000,
        max_tokens=20_000,
        knowledge_fingerprint="b" * 64,
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    route = build_nlcl_route_receipt(
        pack,
        source=NlclRouteSource.NATIVE_KNOWLEDGE,
        route_cards=(
            NlclSelectedCard(
                card_id="pressure-card",
                card_type="work",
                title="Observable pressure escalation",
                version_id=knowledge_version,
                content_hash=knowledge_hash,
                route_terms=("pressure",),
                recommended_profiles=("chapter",),
                negative_routes=(),
                safety_summary="Use only the transferable mechanism.",
                feedback_layer="L3_DISTILLATION_RETRIEVAL",
                knowledge_provenance=provenance,
            ),
        ),
    )
    package = build_prewrite_operation_package(
        pack,
        route,
        extra_missing_requirements=extra_missing_requirements,
    )
    return package


def _run_gate(
    tmp_path: Path,
    audit_status: str,
    *,
    extra_missing_requirements: tuple[str, ...] = (),
):
    project = tmp_path / "project"
    project.mkdir()
    staging = tmp_path / f"staging-{audit_status}"
    staging.mkdir()
    request = {
        "schema": "zhimeng.creation-prewrite-gate-request.v1",
        "operation_package": _operation_package(
            extra_missing_requirements=extra_missing_requirements,
        ).model_dump(
            mode="json",
            by_alias=True,
        ),
        "project_audit_execution_receipt_hash": "c" * 64,
        "project_audit_skill_receipt_hash": "d" * 64,
        "project_audit_tool_result_hash": "e" * 64,
        "project_audit_domain_status": audit_status,
        "project_audit_finding_count": 0 if audit_status == "healthy" else 1,
    }
    completed = subprocess.run(
        [sys.executable, str(ADAPTER_SCRIPT), str(project), "--json"],
        cwd=staging,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        # 与 test_draft_gate_adapter.py 同一处理：生产路径
        # （skill_executor.py 走 bytes + 显式 encode("utf-8")）不受终端影响，
        # 但 text=True 默认按系统 ANSI 代码页编码 stdin（本机是 gbk）。
        # 外部设了 PYTHONIOENCODING=utf-8 跑 pytest 时两边就打架，中文请求会
        # UnicodeDecodeError，表面报成「JSONDecodeError: Expecting value」，
        # 看着像代码回归其实是环境陷阱（2026-09-12 实测：prewrite 5 红，去掉前缀全绿）。
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        capture_output=True,
        check=False,
        timeout=30,
    )
    envelope = json.loads(completed.stdout)
    result = CreationPrewriteGateResult.model_validate(envelope["result"])
    artifact = json.loads(
        (staging / "prewrite-gate.json").read_text(encoding="utf-8")
    )
    assert artifact == envelope["result"]
    return completed, envelope, result


def test_prewrite_adapter_allows_only_risk_draft_when_inputs_are_bound(
    tmp_path: Path,
) -> None:
    completed, envelope, result = _run_gate(tmp_path, "healthy")

    assert completed.returncode == 0
    assert envelope["health"] == "degraded"
    assert result.gate_status == "risk_draft_only"
    assert result.formal_draft_allowed is False
    assert result.allowed_output == "repair_plan_or_risk_draft_only"
    assert "formal_writer_contract_unavailable" in result.failure_signatures
    declared = {
        route.failure_signature for route in _operation_package().failure_routing
    }
    assert set(result.failure_signatures) <= declared


def test_prewrite_adapter_blocks_when_project_audit_is_blocked(
    tmp_path: Path,
) -> None:
    completed, envelope, result = _run_gate(tmp_path, "blocked")

    assert completed.returncode == 1
    assert envelope["health"] == "blocked"
    assert result.gate_status == "blocked"
    assert result.formal_draft_allowed is False
    assert "project_audit_blocked" in result.failure_signatures
    assert next(check for check in result.checks if check.code == "project_audit").status == (
        "blocked"
    )


@pytest.mark.parametrize(
    ("raw_code", "expected_signature"),
    [
        ("current_character_state_unbound", "truth_or_state_unbound"),
        (
            "chapter_control_contract_unbound",
            "operation_package_unreadable_or_missing",
        ),
        (
            "formal_knowledge_unavailable",
            "retrieval_selection_or_budget_unproven",
        ),
    ],
)
def test_prewrite_adapter_emits_only_declared_failure_signatures(
    tmp_path: Path,
    raw_code: str,
    expected_signature: str,
) -> None:
    _, _, result = _run_gate(
        tmp_path,
        "healthy",
        extra_missing_requirements=(raw_code,),
    )
    package = _operation_package(extra_missing_requirements=(raw_code,))
    declared = {route.failure_signature for route in package.failure_routing}

    assert expected_signature in result.failure_signatures
    assert set(result.failure_signatures) <= declared
    assert any(
        raw_code in action and expected_signature in action
        for action in result.next_actions
    )
