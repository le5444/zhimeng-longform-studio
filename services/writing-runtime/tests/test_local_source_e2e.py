from __future__ import annotations

import hashlib
import time
from pathlib import Path

from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.local_source_adapter import ManagedLocalSourceAdapter
from zhimeng_runtime.source_import_receipt_models import (
    LocalSourceImportExecutionReceipt,
    SourceImportAttemptReceipt,
)

from local_source_fixture import (
    DISTILLATION_OUTPUT_CONTRACT,
    DRAFT_ARTIFACT_KINDS,
    EXPECTED_CARD_TYPE_SET,
    PREPARE_OUTPUT_CONTRACT,
    SNAPSHOT_ROOT,
    SOURCE_ARTIFACT_KINDS,
    SOURCE_CHAPTER_MARKER_COUNT,
    SOURCE_FIXTURE,
    SOURCE_SHA256,
    SOURCE_SIZE_BYTES,
    assert_fixture_manifest_static_contract,
)


def _wait_for_terminal_source_status(
    client: TestClient,
    import_id: str,
    *,
    timeout: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/source-imports/{import_id}")
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] in {"awaiting_review", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"local source import did not stop: {latest}")


def _reviewed_work_card(card_id: str) -> dict:
    promise = (
        "Each costly choice changes the protagonist's options, pays off visibly, "
        "and creates a traceable higher-order obligation."
    )
    return {
        "card_id": card_id,
        "card_type": "work",
        "title": "Cost-bound progression loop",
        "source": {
            "name": "Archive Night sample",
            "author": "runtime fixture",
            "scope": "Three complete fixture chapters reviewed across repeated structures.",
            "evidence_quality": "medium",
            "notes": "Author review kept only transferable mechanics and excluded source prose.",
        },
        "taxonomy": {
            "genre": ["speculative fiction"],
            "scene_function": ["costly progression"],
            "emotion": ["pressure", "release"],
            "craft": ["choice and consequence"],
        },
        "tags": ["progression", "choice", "consequence"],
        "keywords": ["visible cost", "stage payoff"],
        "applicable_tasks": ["chapter_brief", "draft_scene", "review"],
        "summary": (
            "Build a repeatable long-form loop from visible pressure, a constrained "
            "choice, an irreversible cost, a concrete payoff, and a new obligation."
        ),
        "reader_promise": promise,
        "reader_effect": {
            "author_intent": "Make every advancement alter both action space and relationships.",
            "observed_reader_effect": "The payoff resolves one pressure while opening a larger debt.",
            "effect_evidence": [
                "All three reviewed chapters connect a decision to a later consequence."
            ],
            "effect_failure_boundary": "Pure numeric rewards without changed choices break the loop.",
            "misread_risk": "Copying surface terminology loses the causal mechanism.",
            "when_it_turns_bad": "A payoff has no effect on the next decision or relationship.",
        },
        "type_payload": {
            "premise_pattern": "A constrained protagonist accepts a cost to gain new action space.",
            "core_reader_promise": promise,
            "opening_contract": "Show a visible loss before presenting a limited risky opportunity.",
            "progression_map": [
                "externalize pressure",
                "force a consequential choice",
                "pay off and create a larger obligation",
            ],
            "recurring_scene_loops": [
                "resource constraint -> risky test -> payoff -> higher pressure"
            ],
            "transfer_limits": [
                "do not copy entities",
                "do not copy setting shells",
                "do not copy scene order",
            ],
        },
        "mechanics": [
            {
                "name": "cost-bound stage payoff",
                "use_when": "A chapter must advance capability and future pressure together.",
                "rule": "Require an irreversible choice before a payoff changes available options.",
                "failure_mode": "Reward-first ordering removes weight from the decision.",
            }
        ],
        "validation_tests": [
            "Verify that the payoff changes the next available choice rather than only a number.",
            "Verify that the new obligation is caused by the chapter's choice and cost.",
        ],
        "validation_ladder": {
            step: {
                "status": "pass",
                "evidence": [f"Author review recorded reproducible evidence for {step}."],
            }
            for step in (
                "internal_consistency",
                "predictive_validation",
                "blind_sample_validation",
                "extreme_scenario_stress_test",
            )
        },
        "scores": {
            "evidence": 0.72,
            "specificity": 0.76,
            "generativity": 0.78,
            "retrieval": 0.68,
            "safety_risk": 0.18,
        },
        "retrieval": {
            "preferred_queries": ["progression choice cost payoff"],
            "negative_queries": ["slice of life without progression"],
        },
        "nlcl_route_hints": {
            "recommended_profiles": ["chapter", "draft_assist", "review"],
            "nlcl_layers": ["distillation_index"],
            "route_terms": ["progression", "choice", "cost", "payoff"],
            "hard_filters": {"genre": ["speculative fiction"]},
            "negative_routes": ["source imitation"],
            "safety_summary": "Use only the abstract causal mechanism, never source expression.",
            "feedback_default_layer": "L3_DISTILLATION_RETRIEVAL",
        },
        "safety": {
            "copyright_boundary": "Retain transferable mechanics without identifiable expression.",
            "do_not_reuse": ["source entities", "setting shells", "scene order"],
            "source_entity_names": [],
            "source_setting_shells": [],
            "signature_phrases": [],
            "contamination_risks": ["source_shell", "entity_leakage"],
        },
        "draft_candidate": False,
        "requires_human_distillation": False,
    }


def _attempt_receipts(tmp_path: Path, import_id: str) -> list[SourceImportAttemptReceipt]:
    """每次导入尝试留下的回执：前端从来不读，接口 9-26 删了（T15 刀 1b），这里直接读库核对。"""
    from zhimeng_runtime.database import SqliteStore

    store = SqliteStore(tmp_path / "runtime.sqlite3")
    return [
        SourceImportAttemptReceipt.model_validate(payload)
        for payload in store.list_source_import_attempt_receipts(import_id)
    ]


def _settings(tmp_path: Path, *, trusted: bool = True) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        trusted_source_roots=(SOURCE_FIXTURE.parent.resolve(),) if trusted else (),
    )


def test_real_local_txt_runs_through_author_review_and_formalization(
    tmp_path: Path,
) -> None:
    assert_fixture_manifest_static_contract()
    before_sha = hashlib.sha256(SOURCE_FIXTURE.read_bytes()).hexdigest()
    with TestClient(create_app(_settings(tmp_path))) as client:
        inspected = client.post(
            "/api/v1/source-imports/local/inspect",
            json={"source_path": str(SOURCE_FIXTURE.resolve())},
        )
        assert inspected.status_code == 200, inspected.text
        candidate = inspected.json()
        assert candidate["source_sha256"] == SOURCE_SHA256
        assert candidate["source_size_bytes"] == SOURCE_SIZE_BYTES
        assert candidate["source_format"] == PREPARE_OUTPUT_CONTRACT["source_format"]

        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "local_file",
                "query": candidate["source_path"],
                "candidate": candidate,
            },
        )
        assert created.status_code == 202, created.text
        import_id = created.json()["import_id"]
        job = _wait_for_terminal_source_status(client, import_id)
        assert job["status"] == "awaiting_review", job
        packet = job["source_packet"]
        assert packet["source_backend"] == PREPARE_OUTPUT_CONTRACT["source_backend"]
        assert packet["source_format"] == PREPARE_OUTPUT_CONTRACT["source_format"]
        assert packet["download_scope"] == PREPARE_OUTPUT_CONTRACT["download_scope"]
        assert packet["chapter_marker_count"] == SOURCE_CHAPTER_MARKER_COUNT
        assert packet["source_file_size_bytes"] == SOURCE_SIZE_BYTES
        for field_name in PREPARE_OUTPUT_CONTRACT["sha256_binding_fields"]:
            assert packet[field_name] == SOURCE_SHA256
        assert SOURCE_ARTIFACT_KINDS <= {
            item["kind"] for item in packet["artifacts"]
        }

        receipt_response = client.get(
            f"/api/v1/source-imports/{import_id}/execution-receipt"
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = LocalSourceImportExecutionReceipt.model_validate(
            receipt_response.json()["receipt"]
        )
        assert receipt.source_file_size_bytes == SOURCE_SIZE_BYTES
        assert receipt.source_file_sha256 == SOURCE_SHA256
        assert receipt.envelope_schema == DISTILLATION_OUTPUT_CONTRACT[
            "envelope_schema"
        ]
        assert receipt.envelope_status == DISTILLATION_OUTPUT_CONTRACT["status"]
        assert receipt.card_count == len(EXPECTED_CARD_TYPE_SET)
        assert SOURCE_ARTIFACT_KINDS <= {
            item.kind for item in receipt.source_artifacts
        }
        assert DRAFT_ARTIFACT_KINDS <= {
            item.kind for item in receipt.draft_artifacts
        }
        attempts = _attempt_receipts(tmp_path, import_id)
        assert len(attempts) == 1
        assert attempts[0].status == "success"
        assert attempts[0].execution_receipt_hash == receipt.receipt_hash

        imported = client.post(
            f"/api/v1/knowledge/source-imports/{import_id}/candidates"
        )
        assert imported.status_code == 201, imported.text
        candidates = imported.json()["candidates"]
        assert {item["card_type"] for item in candidates} == EXPECTED_CARD_TYPE_SET
        work = next(item for item in candidates if item["card_type"] == "work")
        reviewed = client.put(
            f"/api/v1/knowledge/candidates/{work['candidate_id']}",
            json={
                "expected_revision": work["revision"],
                "expected_content_hash": work["content_hash"],
                "card": _reviewed_work_card(work["card_id"]),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["issues"] == []
        promoted = client.post(
            f"/api/v1/knowledge/candidates/{work['candidate_id']}/promote",
            json={
                "expected_revision": reviewed.json()["revision"],
                "expected_content_hash": reviewed.json()["content_hash"],
                "replace_active": False,
            },
        )
        assert promoted.status_code == 200, promoted.text

        for item in candidates:
            if item["candidate_id"] == work["candidate_id"]:
                continue
            rejected = client.post(
                f"/api/v1/knowledge/candidates/{item['candidate_id']}/reject",
                json={
                    "expected_revision": item["revision"],
                    "expected_content_hash": item["content_hash"],
                },
            )
            assert rejected.status_code == 200, rejected.text

        terminal = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": import_id},
        )
        assert terminal.status_code == 200, terminal.text
        finalized = client.post(
            f"/api/v1/knowledge/source-imports/{import_id}/finalize",
            json={
                "expected_source_receipt_hash": receipt.receipt_hash,
                "expected_draft_hash": receipt.cards_draft_hash,
                "expected_candidate_set_hash": terminal.json()["candidate_set_hash"],
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert {item["decision"] for item in finalized.json()["decisions"]} == {
            "promoted",
            "rejected",
        }
        assert client.get(f"/api/v1/source-imports/{import_id}").json()[
            "status"
        ] == "completed"
        formal_cards = client.get("/api/v1/knowledge/cards")
        assert formal_cards.status_code == 200, formal_cards.text
        assert [
            item["active_version"]["card"]["card_type"]
            for item in formal_cards.json()["cards"]
        ] == ["work"]

    assert SOURCE_FIXTURE.stat().st_size == SOURCE_SIZE_BYTES
    assert before_sha == hashlib.sha256(SOURCE_FIXTURE.read_bytes()).hexdigest()


def test_uploaded_txt_runs_without_a_machine_specific_trusted_root(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path, trusted=False))) as client:
        created = client.post(
            "/api/v1/source-imports/local/upload-text",
            json={
                "schema": "zhimeng.local-text-source-upload-request.v1",
                "file_name": "作者选择的资料.txt",
                "content": SOURCE_FIXTURE.read_text(encoding="utf-8"),
            },
        )
        assert created.status_code == 202, created.text
        payload = created.json()
        uploaded_path = Path(payload["candidate"]["source_path"])
        assert uploaded_path.name == "作者选择的资料.txt"
        assert uploaded_path.is_file()
        assert uploaded_path.parent.parent == (tmp_path / "source-uploads").resolve()
        assert SOURCE_FIXTURE.parent.resolve() not in uploaded_path.parents

        job = _wait_for_terminal_source_status(client, payload["import_id"])
        assert job["status"] == "awaiting_review", job
        assert job["distillation_draft"]["card_count"] > 0
        assert job["source_packet"]["source_backend"] == "generic_local_file"


def test_uploaded_txt_rejects_a_path_disguised_as_a_file_name(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(_settings(tmp_path, trusted=False))) as client:
        response = client.post(
            "/api/v1/source-imports/local/upload-text",
            json={
                "schema": "zhimeng.local-text-source-upload-request.v1",
                "file_name": "..\\outside.txt",
                "content": "第一章\n正文",
            },
        )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "source_upload_invalid"
    assert not (tmp_path / "outside.txt").exists()


class _FailingDraftAdapter:
    def __init__(self, delegate: ManagedLocalSourceAdapter) -> None:
        self.delegate = delegate

    def execution_identity(self):
        return self.delegate.execution_identity()

    def inspect(self, source_path: str):
        return self.delegate.inspect(source_path)

    def prepare(self, *args, **kwargs):
        return self.delegate.prepare(*args, **kwargs)

    def draft(self, *args, **kwargs):
        raise RuntimeError("intentional distillation failure")

    def cleanup_failed_workspace(self, workspace: Path):
        return self.delegate.cleanup_failed_workspace(workspace)


def test_local_failure_is_cleaned_and_has_an_immutable_attempt_receipt(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "source-imports"
    delegate = ManagedLocalSourceAdapter(
        snapshot_root=SNAPSHOT_ROOT,
        state_root=state_root,
        trusted_source_roots=(SOURCE_FIXTURE.parent.resolve(),),
    )
    adapter = _FailingDraftAdapter(delegate)
    candidate = delegate.inspect(str(SOURCE_FIXTURE.resolve()))

    with TestClient(
        create_app(
            _settings(tmp_path, trusted=False),
            local_source_adapter=adapter,
        )
    ) as client:
        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "local_file",
                "query": candidate.source_path,
                "candidate": candidate.model_dump(mode="json"),
            },
        )
        assert created.status_code == 202, created.text
        import_id = created.json()["import_id"]
        job = _wait_for_terminal_source_status(client, import_id)
        assert job["status"] == "failed"
        assert job["error"] == "intentional distillation failure"
        receipt = _attempt_receipts(tmp_path, import_id)[0]
        assert receipt.status == "failed"
        assert receipt.stage == "drafting"
        assert receipt.failure_signature is not None
        assert receipt.cleanup_status == "clean"
        assert not (state_root / import_id).exists()

    assert SOURCE_FIXTURE.stat().st_size == SOURCE_SIZE_BYTES
    assert hashlib.sha256(SOURCE_FIXTURE.read_bytes()).hexdigest() == SOURCE_SHA256
