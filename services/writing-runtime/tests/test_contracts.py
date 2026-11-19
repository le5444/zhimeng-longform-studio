from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateOutput,
    AgentCandidateOutputV1,
    AgentCandidateOutputV2,
)
from zhimeng_runtime.agent_models import AgentRunCreateRequest
from zhimeng_runtime.change_set_models import (
    ChangeSetApplyRequest,
    ChangeSetCreateRequest,
    ChangeSetDecisionRequest,
    ChangeSetListResponse,
    ChangeSetResponse,
    ChangeSetRollbackRequest,
    CreateFileChangeInput,
    MoveFileChangeInput,
    CandidateFileChange,
    WritebackFileResult,
    RelativeProjectPath,
    WritebackManifest,
)
from zhimeng_runtime.context_compiler import compile_context_pack
from zhimeng_runtime.context_models import (
    ContextCandidateFragment,
    ContextCreateRequest,
    ContextPack,
)
from zhimeng_runtime.creation_instruction_models import (
    CreationInstructionActivationBinding,
    CreationInstructionDispatchReceipt,
    CreationInstructionMaterialization,
)
from zhimeng_runtime.distillation_models import (
    DistillationCoverageRequest,
    DistillationCoverageResponse,
    DistillationHealthResponse,
)
from zhimeng_runtime.project_models import (
    ProjectBindingListResponse,
    ProjectFileContentResponse,
    ProjectInspectionResponse,
)
from zhimeng_runtime.project_kb_models import (
    ProjectKbRebuildRequest,
    ProjectKbRebuildResult,
    ProjectKbStateRecord,
)
from zhimeng_runtime.skill_models import (
    CreationHandoffRequest,
    CreationHandoffResult,
    CreationProjectAuditRequest,
    CreationProjectAuditResult,
    CreationPrewriteGateRequest,
    CreationPrewriteGateResult,
    ManagedSkillActivationRequest,
    ManagedSkillActivationResponse,
    ManagedSkillCatalogResponse,
    ManagedSkillImportRequest,
    ManagedSkillImportResponse,
    ManagedSkillLifecycleCatalogResponse,
    SkillExecutionReceipt,
    SkillManifest,
    SkillRegistryResponse,
)
from zhimeng_runtime.source_import_models import (
    LocalFileSourceCandidate,
    LocalSourceImportCreateRequest,
    LocalSourceImportJob,
    LocalSourceInspectRequest,
    SourceImportCreateRequest,
    SourceImportJob,
    SourceImportListResponse,
    TomatoSearchRequest,
    TomatoSearchResponse,
)
from zhimeng_runtime.source_import_receipt_models import (
    LocalSourceImportExecutionReceipt,
    SourceImportExecutionReceipt,
    SourceImportExecutionReceiptRecord,
)
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidate,
    KnowledgeCandidateList,
    KnowledgeCandidateUpdateRequest,
    KnowledgeCard,
    KnowledgeCardList,
    KnowledgeScopedCardList,
    KnowledgeCardStateRequest,
    KnowledgePromotionRequest,
    KnowledgeRollbackRequest,
    KnowledgeFormalizationReceiptV2,
    KnowledgeVersionLineageReceiptV2,
)
from zhimeng_runtime.knowledge_scope_models import KnowledgeScopeRecord
from zhimeng_runtime.nlcl_models import NlclRouteReceipt
from zhimeng_runtime.nlcl_route_execution_models import (
    NlclRouteExecutionReceipt,
    NlclRouteExecutionState,
)
from zhimeng_runtime.prewrite_models import PrewriteOperationPackage
from zhimeng_runtime.post_draft_gate_models import (
    PostDraftGateReceipt,
    PostDraftGateRequest,
    PostDraftGateResponse,
    PostDraftGateResult,
)
from zhimeng_runtime.provider_recovery_models import (
    ProviderRecoveryDecision,
    ProviderRecoveryDecisionRequest,
    ProviderRecoveryDossier,
    ProviderRecoveryResponse,
)
from zhimeng_runtime.writer_models import WriterCandidateResult, WriterTask


ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts"


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _assert_top_level_contract(payload: dict, schema: dict) -> None:
    required = set(schema.get("required") or [])
    properties = set((schema.get("properties") or {}).keys())
    assert required.issubset(payload)
    if schema.get("additionalProperties") is False:
        assert set(payload).issubset(properties)


def test_health_responses_match_contract_shape(client: TestClient) -> None:
    schema = _schema("health.schema.json")
    _assert_top_level_contract(client.get("/health/live").json(), schema)
    _assert_top_level_contract(client.get("/health/ready").json(), schema)


def test_runtime_status_matches_contract_shape(client: TestClient) -> None:
    schema = _schema("runtime-status.schema.json")
    _assert_top_level_contract(client.get("/api/v1/runtime/status").json(), schema)


def test_error_envelope_matches_contract_shape(client: TestClient) -> None:
    schema = _schema("error-envelope.schema.json")
    _assert_top_level_contract(client.get("/missing").json(), schema)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("project-bindings.schema.json", ProjectBindingListResponse),
        ("project-inspection.schema.json", ProjectInspectionResponse),
        ("project-file-content.schema.json", ProjectFileContentResponse),
    ],
)
def test_generated_project_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


def test_generated_skill_registry_contract_matches_pydantic_model() -> None:
    file_name = "skill-registry.schema.json"
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == SkillRegistryResponse.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("skill-manifest.schema.json", SkillManifest),
        ("managed-skill-catalog.schema.json", ManagedSkillCatalogResponse),
        ("skill-execution-receipt.schema.json", SkillExecutionReceipt),
    ],
)
def test_generated_managed_skill_contracts_match_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


def test_skill_manifest_contract_exposes_non_executable_instruction_source() -> None:
    manifest = _schema("skill-manifest.schema.json")

    assert set(manifest["properties"]["kind"]["enum"]) == {
        "deterministic_tool",
        "instruction_source",
    }
    assert "entrypoint" not in manifest["required"]
    assert "input_schema" not in manifest["required"]
    assert "output_schema" not in manifest["required"]
    assert "resources" not in manifest["required"]
    assert manifest["properties"]["resources"]["maxItems"] == 32
    deterministic, instruction_source = manifest["allOf"]
    assert deterministic["then"]["properties"]["artifacts"]["minItems"] == 1
    instruction_properties = instruction_source["then"]["properties"]
    assert instruction_source["then"]["required"] == ["resources"]
    assert instruction_properties["entrypoint"]["type"] == "null"
    assert instruction_properties["input_schema"]["type"] == "null"
    assert instruction_properties["output_schema"]["type"] == "null"
    assert instruction_properties["resources"]["contains"] == {
        "const": "instruction-map.json"
    }

    instruction_payload = {
        "schema": "zhimeng.skill-manifest.v1",
        "id": "novel-creation-suite.writer-instruction",
        "display_name": "Creation writer instruction",
        "description": "Immutable managed Writer instructions.",
        "kind": "instruction_source",
        "version": "2026.07.23-o2.2",
        "source": {
            "type": "builtin_managed",
            "reference": "PROVENANCE.md",
            "license": None,
            "source_url": None,
            "source_version": None,
            "modified": True,
        },
        "approval": "none",
        "permissions": {
            "read": [],
            "write": [],
            "network": False,
            "secrets": [],
        },
        "resources": ["instruction-map.json"],
    }
    validator = Draft202012Validator(manifest)
    assert validator.is_valid(instruction_payload)
    assert not validator.is_valid({**instruction_payload, "entrypoint": "run.py"})
    assert not validator.is_valid({**instruction_payload, "resources": []})


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        (
            "managed-skill-lifecycle-catalog.schema.json",
            ManagedSkillLifecycleCatalogResponse,
        ),
        ("managed-skill-import-request.schema.json", ManagedSkillImportRequest),
        ("managed-skill-import.schema.json", ManagedSkillImportResponse),
        (
            "managed-skill-activation-request.schema.json",
            ManagedSkillActivationRequest,
        ),
        ("managed-skill-activation.schema.json", ManagedSkillActivationResponse),
    ],
)
def test_generated_managed_skill_lifecycle_contracts_match_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("agent-candidate-output.schema.json", AgentCandidateOutput),
        ("agent-candidate-output-v2.schema.json", AgentCandidateOutputV2),
        ("agent-candidate-output-v1.schema.json", AgentCandidateOutputV1),
    ],
)
def test_generated_agent_candidate_output_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        (
            "creation-instruction-activation.schema.json",
            CreationInstructionActivationBinding,
        ),
        (
            "creation-instruction-materialization.schema.json",
            CreationInstructionMaterialization,
        ),
        (
            "creation-instruction-dispatch-receipt.schema.json",
            CreationInstructionDispatchReceipt,
        ),
    ],
)
def test_generated_creation_instruction_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    "file_name",
    [
        "creation-writer-instruction-request.schema.json",
        "creation-writer-instruction-result.schema.json",
    ],
)
def test_deprecated_creation_writer_instruction_contracts_are_not_exported(
    file_name: str,
) -> None:
    assert not (CONTRACTS / file_name).exists()


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("writer-task.schema.json", WriterTask),
        ("writer-candidate-result.schema.json", WriterCandidateResult),
    ],
)
def test_generated_writer_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


def test_writer_task_contract_excludes_deprecated_instruction_execution_contract() -> None:
    stored = _schema("writer-task.schema.json")

    assert "creation_instruction" not in stored["properties"]
    assert "CreationWriterInstructionResult" not in stored.get("$defs", {})
    assert "WriterInstructionBinding" not in stored.get("$defs", {})


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("provider-recovery-dossier.schema.json", ProviderRecoveryDossier),
        (
            "provider-recovery-decision-request.schema.json",
            ProviderRecoveryDecisionRequest,
        ),
        ("provider-recovery-decision.schema.json", ProviderRecoveryDecision),
        ("provider-recovery.schema.json", ProviderRecoveryResponse),
    ],
)
def test_generated_provider_recovery_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    Draft202012Validator.check_schema(stored)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


def test_provider_recovery_contracts_preserve_manual_zero_replay_boundary() -> None:
    dossier = _schema("provider-recovery-dossier.schema.json")
    request = _schema("provider-recovery-decision-request.schema.json")
    decision = _schema("provider-recovery-decision.schema.json")
    response = _schema("provider-recovery.schema.json")

    assert dossier["additionalProperties"] is False
    assert dossier["properties"]["remote_result"]["const"] == "unknown"
    assert dossier["properties"]["provider_replay_count"]["const"] == 0
    assert set(request["properties"]["action"]["enum"]) == {
        "adopt_persisted_candidate",
        "abandon_and_recompile",
    }
    assert "neither recovery action authorizes Provider replay" in request[
        "x-zhimeng-invariants"
    ]
    assert "one decision per dossier is stored append-only" in decision[
        "x-zhimeng-invariants"
    ]
    assert (
        "decision created_at is not earlier than the enclosed dossier created_at"
        in response["x-zhimeng-invariants"]
    )
    assert response["properties"]["decision"]["default"] is None
    assert response["$defs"]["ProviderRecoveryDossier"][
        "additionalProperties"
    ] is False


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("creation-project-audit-request.schema.json", CreationProjectAuditRequest),
        ("creation-project-audit-result.schema.json", CreationProjectAuditResult),
        ("creation-prewrite-gate-request.schema.json", CreationPrewriteGateRequest),
        ("creation-prewrite-gate-result.schema.json", CreationPrewriteGateResult),
        ("creation-handoff-request.schema.json", CreationHandoffRequest),
        ("creation-handoff-result.schema.json", CreationHandoffResult),
        ("prewrite-operation-package.schema.json", PrewriteOperationPackage),
    ],
)
def test_generated_creation_project_audit_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id") == f"https://zhimeng.local/contracts/{file_name}"
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("change-set-create.schema.json", ChangeSetCreateRequest),
        ("change-set.schema.json", ChangeSetResponse),
        ("change-set-list.schema.json", ChangeSetListResponse),
        ("change-set-decisions.schema.json", ChangeSetDecisionRequest),
        ("change-set-apply.schema.json", ChangeSetApplyRequest),
        ("change-set-rollback.schema.json", ChangeSetRollbackRequest),
        ("writeback-manifest.schema.json", WritebackManifest),
    ],
)
def test_generated_change_set_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("post-draft-gate-request.schema.json", PostDraftGateRequest),
        ("post-draft-gate-result.schema.json", PostDraftGateResult),
        ("post-draft-gate-receipt.schema.json", PostDraftGateReceipt),
        ("post-draft-gate-response.schema.json", PostDraftGateResponse),
    ],
)
def test_generated_post_draft_gate_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("tomato-search-request.schema.json", TomatoSearchRequest),
        ("tomato-search.schema.json", TomatoSearchResponse),
        ("local-source-inspect-request.schema.json", LocalSourceInspectRequest),
        ("local-source-inspect.schema.json", LocalFileSourceCandidate),
        ("local-source-import-create.schema.json", LocalSourceImportCreateRequest),
        ("local-source-import.schema.json", LocalSourceImportJob),
        (
            "local-source-import-execution-receipt.schema.json",
            LocalSourceImportExecutionReceipt,
        ),
        ("source-import-create.schema.json", SourceImportCreateRequest),
        ("source-import.schema.json", SourceImportJob),
        ("source-import-list.schema.json", SourceImportListResponse),
        ("source-import-execution-receipt.schema.json", SourceImportExecutionReceipt),
        (
            "source-import-execution-receipt-record.schema.json",
            SourceImportExecutionReceiptRecord,
        ),
    ],
)
def test_generated_source_import_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("knowledge-candidate.schema.json", KnowledgeCandidate),
        ("knowledge-candidate-list.schema.json", KnowledgeCandidateList),
        ("knowledge-candidate-update.schema.json", KnowledgeCandidateUpdateRequest),
        ("knowledge-promotion.schema.json", KnowledgePromotionRequest),
        ("knowledge-card.schema.json", KnowledgeCard),
        ("knowledge-card-list.schema.json", KnowledgeCardList),
        ("knowledge-scoped-card-list.schema.json", KnowledgeScopedCardList),
        ("knowledge-card-state.schema.json", KnowledgeCardStateRequest),
        ("knowledge-card-rollback.schema.json", KnowledgeRollbackRequest),
        ("knowledge-scope-record.schema.json", KnowledgeScopeRecord),
        ("project-kb-rebuild-request.schema.json", ProjectKbRebuildRequest),
        ("project-kb-rebuild-result.schema.json", ProjectKbRebuildResult),
        ("project-kb-state-record.schema.json", ProjectKbStateRecord),
        (
            "knowledge-version-lineage-receipt-v2.schema.json",
            KnowledgeVersionLineageReceiptV2,
        ),
        (
            "knowledge-formalization-receipt-v2.schema.json",
            KnowledgeFormalizationReceiptV2,
        ),
    ],
)
def test_generated_knowledge_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("distillation-health.schema.json", DistillationHealthResponse),
        ("distillation-coverage-request.schema.json", DistillationCoverageRequest),
        ("distillation-coverage.schema.json", DistillationCoverageResponse),
    ],
)
def test_generated_distillation_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


@pytest.mark.parametrize(
    ("file_name", "model"),
    [
        ("context-pack-create.schema.json", ContextCreateRequest),
        ("context-pack.schema.json", ContextPack),
        ("nlcl-route-receipt.schema.json", NlclRouteReceipt),
        (
            "nlcl-route-execution-receipt.schema.json",
            NlclRouteExecutionReceipt,
        ),
        ("nlcl-route-execution-state.schema.json", NlclRouteExecutionState),
    ],
)
def test_generated_context_contracts_match_pydantic_models(
    file_name: str,
    model: type,
) -> None:
    stored = _schema(file_name)
    assert stored.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert stored.pop("$id").endswith(file_name)
    assert stored == model.model_json_schema(by_alias=True)


def test_context_pack_contract_is_strict_and_preserves_public_boundaries() -> None:
    create_schema = _schema("context-pack-create.schema.json")
    pack_schema = _schema("context-pack.schema.json")
    Draft202012Validator.check_schema(create_schema)
    Draft202012Validator.check_schema(pack_schema)

    assert create_schema["additionalProperties"] is False
    # 知识库快照绑定可选（2026-09-25）：普通对话的资料包不带它。
    assert set(create_schema["required"]) == {
        "objective",
        "profile",
        "expected_project_revision",
        "expected_project_fingerprint",
    }
    selector = create_schema["$defs"]["FormalKnowledgeSelector"]
    assert selector["additionalProperties"] is False
    assert set(selector["required"]) == {
        "source_import_id",
        "formalization_receipt_id",
        "formalization_receipt_hash",
        "expected_knowledge_fingerprint",
        "cards",
    }
    assert selector["properties"]["cards"]["minItems"] == 1
    assert selector["properties"]["cards"]["maxItems"] == 64
    assert (
        selector["properties"]["cards"]["items"]["$ref"]
        == "#/$defs/FormalKnowledgeCardSelector"
    )
    assert (
        pack_schema["properties"]["formal_knowledge_selector"]
        == create_schema["properties"]["formal_knowledge_selector"]
    )
    assert "schema" in pack_schema["properties"]
    assert "schema_id" not in pack_schema["properties"]
    assert {
        "pack_hash",
        "injection_hash",
        "provider_messages_hash",
    }.issubset(pack_schema["required"])
    for field_name in ("project_kb_state_id", "project_kb_manifest_hash"):
        assert field_name not in pack_schema["required"]
        assert {option.get("type") for option in pack_schema["properties"][field_name]["anyOf"]} == {
            "string",
            "null",
        }

    for file_name in (
        "nlcl-route-receipt.schema.json",
        "nlcl-route-execution-receipt.schema.json",
    ):
        persisted_schema = _schema(file_name)
        for field_name in ("project_kb_state_id", "project_kb_manifest_hash"):
            assert field_name not in persisted_schema["required"]
            assert {
                option.get("type")
                for option in persisted_schema["properties"][field_name]["anyOf"]
            } == {"string", "null"}

    budget = pack_schema["$defs"]["ContextBudget"]
    assert {
        "character_estimator",
        "max_characters",
        "used_characters",
        "remaining_characters",
        "token_estimator",
        "max_tokens",
        "used_tokens",
        "remaining_tokens",
    }.issubset(budget["properties"])
    excluded = pack_schema["$defs"]["ContextExcludedFragment"]
    assert excluded["additionalProperties"] is False
    assert "content" not in excluded["properties"]

    request = ContextCreateRequest(
        objective="Continue the chapter without changing established facts.",
        profile="chapter",
        active_chapter_id="CH001",
        expected_project_revision=1,
        expected_project_fingerprint="a" * 64,
        project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project_kb_manifest_hash="b" * 64,
    )
    risk = ContextCandidateFragment(
        source_id="risk-reference-1",
        source_type="knowledge_card",
        title="Unvalidated reference",
        authority="risk",
        version="candidate-v1",
        source_hash="b" * 64,
        content="This content must remain outside the excluded trace.",
        priority=100,
        mandatory=False,
        eligible=True,
    )
    pack = compile_context_pack(
        context_pack_id="context-pack-contract",
        session_id="session-contract",
        project_id="project-contract",
        history_through_ordinal=0,
        request=request,
        history=(),
        candidates=(risk,),
        max_characters=20_000,
        max_tokens=20_000,
        knowledge_fingerprint="c" * 64,
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    payload = pack.model_dump(mode="json", by_alias=True)
    validator = Draft202012Validator(pack_schema)
    assert list(validator.iter_errors(payload)) == []
    assert "content" not in payload["excluded"][0]

    create_validator = Draft202012Validator(create_schema)
    request_payload = request.model_dump(mode="json", by_alias=True)
    # 知识库快照绑定整对可省（2026-09-25）；只给一半由模型拒绝
    # （test_chat_pack_without_kb_20260925.py 走 API 验过）。
    without_binding = dict(request_payload)
    without_binding.pop("project_kb_state_id")
    without_binding.pop("project_kb_manifest_hash")
    assert list(create_validator.iter_errors(without_binding)) == []
    with pytest.raises(ValueError):
        ContextCreateRequest.model_validate({**without_binding, "project_kb_state_id": request.project_kb_state_id})

    legacy_request = ContextCreateRequest.model_construct(
        objective=request.objective,
        profile=request.profile,
        active_chapter_id=request.active_chapter_id,
        expected_project_revision=request.expected_project_revision,
        expected_project_fingerprint=request.expected_project_fingerprint,
        project_kb_state_id=None,
        project_kb_manifest_hash=None,
        formal_knowledge_selector=None,
    )
    legacy_pack = compile_context_pack(
        context_pack_id="context-pack-contract-legacy",
        session_id="session-contract",
        project_id="project-contract",
        history_through_ordinal=0,
        request=legacy_request,
        history=(),
        candidates=(risk,),
        max_characters=20_000,
        max_tokens=20_000,
        knowledge_fingerprint="c" * 64,
        created_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    legacy_payload = legacy_pack.model_dump(mode="json", by_alias=True)
    legacy_payload.pop("project_kb_state_id")
    legacy_payload.pop("project_kb_manifest_hash")
    parsed_legacy = ContextPack.model_validate(legacy_payload)
    assert parsed_legacy.project_kb_state_id is None
    assert parsed_legacy.project_kb_manifest_hash is None

    assert list(validator.iter_errors({**payload, "schema_id": payload["schema"]}))
    with_excluded_content = json.loads(json.dumps(payload))
    with_excluded_content["excluded"][0]["content"] = "must be rejected"
    assert list(validator.iter_errors(with_excluded_content))
    missing_token_budget = json.loads(json.dumps(payload))
    missing_token_budget["budget"].pop("max_tokens")
    assert list(validator.iter_errors(missing_token_budget))


def test_agent_run_accepts_the_context_contract_chapter_id_limit() -> None:
    chapter_id = "C" * 256
    context_request = ContextCreateRequest(
        objective="Continue the reviewed chapter.",
        profile="chapter",
        active_chapter_id=chapter_id,
        expected_project_revision=1,
        expected_project_fingerprint="a" * 64,
        project_kb_state_id="project-kb-state-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        project_kb_manifest_hash="b" * 64,
    )
    run_request = AgentRunCreateRequest(
        message=context_request.objective,
        active_chapter_id=context_request.active_chapter_id,
        expected_project_revision=context_request.expected_project_revision,
        expected_project_fingerprint=context_request.expected_project_fingerprint,
    )

    assert run_request.active_chapter_id == chapter_id


def test_public_distillation_coverage_schema_rejects_runtime_paths_and_top() -> None:
    schema = _schema("distillation-coverage-request.schema.json")
    validator = Draft202012Validator(schema)
    valid = {"query": "番茄 男频 玄幻 开篇", "profile": "draft_assist"}

    assert list(validator.iter_errors(valid)) == []
    assert list(validator.iter_errors({**valid, "top": 500}))
    assert list(validator.iter_errors({**valid, "library": "C:/outside"}))
    assert list(validator.iter_errors({**valid, "path": "C:/outside"}))


def test_public_change_set_create_schema_rejects_unsafe_or_mismatched_operations() -> None:
    schema = _schema("change-set-create.schema.json")
    validator = Draft202012Validator(schema)
    base = {
        "base_revision": 1,
        "base_fingerprint": "a" * 64,
        "title": "Contract test",
        "summary": "Validate public operation shapes.",
    }
    traversal = {
        **base,
        "changes": [
            {
                "operation": "create",
                "relative_path": "../outside.md",
                "after_content": "bad",
                "rationale": "Traversal must fail.",
            }
        ],
    }
    mismatched_update = {
        **base,
        "changes": [
            {
                "operation": "update",
                "relative_path": "08_素材库/file.md",
                "after_content": "bad",
                "rationale": "Update must use file_id.",
            }
        ],
    }
    valid = {
        **base,
        "changes": [
            {
                "operation": "create",
                "relative_path": "08_素材库/file.md",
                "after_content": "ok",
                "rationale": "Valid create.",
            }
        ],
    }

    assert list(validator.iter_errors(traversal))
    assert list(validator.iter_errors(mismatched_update))
    assert list(validator.iter_errors(valid)) == []

    for unsafe_path in (
        "08_素材库/bad?.md",
        "08_素材库/control\x01.md",
    ):
        unsafe = {
            **base,
            "changes": [
                {
                    "operation": "create",
                    "relative_path": unsafe_path,
                    "after_content": "bad",
                    "rationale": "Portable paths only.",
                }
            ],
        }
        assert list(validator.iter_errors(unsafe)), unsafe_path

    reserved_device = {
        **base,
        "changes": [
            {
                "operation": "create",
                "relative_path": "08_素材库/CON.md",
                "after_content": "bad",
                "rationale": "Windows device aliases are not files.",
            }
        ],
    }
    with pytest.raises(ValueError, match="Windows device names"):
        ChangeSetCreateRequest.model_validate(reserved_device)


@pytest.mark.parametrize(
    "relative_path",
    [
        "08_素材库/灵感.md",
        "设定/新功法.md",
        "99_番外/彩蛋.md",
        "README.md",
        "人物/主角/柳青烟.json",
        "src/app.py",
        "web/main.js",
        "settings.ini",
        "data/sales.csv",
        "Dockerfile",
        ".env",
        ".config/settings.custom-extension",
        ".git/config",
        "images/not-a-binary.exe",
    ],
)
def test_change_set_paths_accept_the_authors_own_folder_layout(
    relative_path: str,
) -> None:
    """01_~09_ 是新建书的默认夹子，不是准入条件：作者怎么摆，Agent 就能写到哪。"""

    change = CreateFileChangeInput(
        operation="create",
        relative_path=relative_path,
        after_content="正文",
        rationale="作者自己的文件夹摆法。",
    )
    assert change.relative_path == relative_path


@pytest.mark.parametrize(
    ("relative_path", "reason"),
    [
        ("../outside.md", "不能越出书根"),
        ("设定/../README.md", "不能带 dot 段"),
        ("/etc/config", "必须相对路径"),
        ("C:/config.py", "必须相对路径"),
        ("folder//file.py", "不能带空段"),
        ("folder/./file.py", "不能带 dot 段"),
        ("folder/file.py.", "不能尾点"),
        ("folder/file.py ", "不能尾空格"),
        ("NUL", "Windows 保留名"),
        ("config/con .py", "Windows 保留名别名"),
        ("AUX/config.py", "Windows 保留目录名"),
        ("08_素材库/CON.md", "Windows 保留名"),
        ("设定/坏?.md", "不可移植字符"),
    ],
)
def test_change_set_paths_still_reject_unsafe_targets(
    relative_path: str,
    reason: str,
) -> None:
    payload = {
        "operation": "create",
        "relative_path": relative_path,
        "after_content": "正文",
        "rationale": reason,
    }
    with pytest.raises(ValidationError):
        CreateFileChangeInput.model_validate(payload)


def test_workspace_path_changes_reach_candidate_move_and_writeback_schemas() -> None:
    from pydantic import TypeAdapter
    from zhimeng_runtime.change_set_models import RelativeWorkspacePath

    for path in (".env", "src/app.py", "config/settings.ini", "Dockerfile", "data/table.csv"):
        assert TypeAdapter(RelativeWorkspacePath).validate_python(path) == path
        assert MoveFileChangeInput(operation="move", file_id="file-" + "a" * 32, target_relative_path=path, rationale="重命名文件").target_relative_path == path
        for model in (CandidateFileChange, WritebackFileResult):
            fields = model.model_json_schema()["properties"]
            Draft202012Validator(fields["relative_path"]).validate(path)
            Draft202012Validator(fields["target_relative_path"]).validate(path)
        public = _schema("change-set-create.schema.json")
        payload = {"base_revision": 1, "base_fingerprint": "a" * 64, "title": "普通文件", "summary": "保存普通文本", "changes": [{"operation": "create", "relative_path": path, "after_content": "text", "rationale": "保存文件"}]}
        Draft202012Validator(public).validate(payload)

    # Only the live workspace path changes; legacy chapter-specific models keep
    # their existing type until that retired chain is separately removed.
    for path in ("app.py", ".env", "Dockerfile"):
        with pytest.raises(ValidationError):
            TypeAdapter(RelativeProjectPath).validate_python(path)
