from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
ADAPTER = ROOT / "adapters" / "builtin" / "creation-suite-draft-gate" / "run_draft_gate.py"
CONTRACTS = ROOT / "packages" / "contracts"


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _request(
    files: list[tuple[str, bytes]],
    *,
    candidate_id: str = "candidate-draft-gate-test",
    manifest: dict | None = None,
    declared_hashes: dict[str, str] | None = None,
    knowledge_validator_bundle: dict | None = None,
) -> dict:
    declared_hashes = declared_hashes or {}
    return {
        "schema": "zhimeng.creation-draft-gate-request.v1",
        "candidate_id": candidate_id,
        **({"candidate_manifest": manifest} if manifest is not None else {}),
        **(
            {"knowledge_validator_bundle": knowledge_validator_bundle}
            if knowledge_validator_bundle is not None
            else {}
        ),
        "candidate_files": [
            {
                "path": path,
                "after_bytes_base64": base64.b64encode(content).decode("ascii"),
                **(
                    {"after_sha256": declared_hashes[path]}
                    if path in declared_hashes
                    else {}
                ),
            }
            for path, content in files
        ],
    }


def _execute(tmp_path: Path, request: dict) -> subprocess.CompletedProcess[str]:
    staging = tmp_path / "staging"
    staging.mkdir(parents=True)
    return subprocess.run(
        [sys.executable, str(ADAPTER), str(tmp_path / "bound-snapshot"), "--json"],
        cwd=staging,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def _run(tmp_path: Path, request: dict) -> tuple[subprocess.CompletedProcess[str], dict]:
    completed = _execute(tmp_path, request)
    assert completed.stdout, completed.stderr
    envelope = json.loads(completed.stdout)
    return completed, envelope


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validator_bundle(
    *,
    rules: list[dict],
    configuration_errors: list[str] | None = None,
) -> dict:
    errors = configuration_errors or []
    content_hash = "a" * 64
    validator = {
        "card_id": "validator-draft-gate-test",
        "version_id": "knowledge-version-" + "b" * 32,
        "content_hash": content_hash,
        "validator_name": "draft_gate_test_validator",
        "provenance": {
            "schema": "zhimeng.knowledge-provenance-ref.v1",
            "source_import_id": "source-import-" + "c" * 32,
            "source_receipt_id": "source-import-receipt-" + "d" * 32,
            "source_receipt_hash": "e" * 64,
            "source_packet_fingerprint": "f" * 64,
            "cards_draft_hash": "1" * 64,
            "candidate_id": "knowledge-candidate-" + "2" * 32,
            "candidate_origin_hash": "3" * 64,
            "card_id": "validator-draft-gate-test",
            "version_id": "knowledge-version-" + "b" * 32,
            "version_content_hash": content_hash,
            "version_lineage_receipt_id": "knowledge-lineage-" + "4" * 32,
            "version_lineage_receipt_hash": "5" * 64,
            "formalization_receipt_id": "knowledge-formalization-" + "6" * 32,
            "formalization_receipt_hash": "7" * 64,
        },
        "rules": rules,
        "configuration_errors": errors,
        "rules_hash": _canonical_hash(
            {"rules": rules, "configuration_errors": errors}
        ),
    }
    bundle = {
        "schema": "zhimeng.knowledge-validator-bundle.v1",
        "context_pack_id": "context-pack-draft-gate-test",
        "context_pack_hash": "8" * 64,
        "route_receipt_id": "nlcl-route-" + "9" * 32,
        "route_receipt_hash": "a" * 64,
        "knowledge_fingerprint": "b" * 64,
        "validators": [validator],
    }
    bundle["bundle_hash"] = _canonical_hash(bundle)
    return bundle


def _validator_rules() -> list[dict]:
    return [
        {
            "rule_id": "forbid_source_terms",
            "kind": "forbid_terms",
            "terms": ["Archive Night", "source shell"],
            "case_sensitive": False,
            "failure_signature": "validator_source_term_leak",
            "message": "正文候选包含正式 validator 明确禁止的来源词项。",
        },
        {
            "rule_id": "require_visible_pressure",
            "kind": "require_any_term",
            "terms": ["before", "代价", "压力", "选择"],
            "case_sensitive": False,
            "failure_signature": "validator_visible_pressure_missing",
            "message": "正文候选缺少正式 validator 要求的可见压力或代价词项。",
        },
    ]


def _assert_result_contract(envelope: dict) -> None:
    envelope_schema = _schema("deterministic-skill-output.schema.json")
    result_schema = _schema("creation-draft-gate-result.schema.json")
    assert list(Draft202012Validator(envelope_schema).iter_errors(envelope)) == []
    assert list(Draft202012Validator(result_schema).iter_errors(envelope["result"])) == []


def test_clean_candidate_passes_with_hashes_and_only_staging_artifact(
    tmp_path: Path,
) -> None:
    body = "夜雨敲在旧站的铁皮檐上。\n他把钥匙收进掌心。\n".encode("utf-8")
    request = _request(
        [("05_正文内容/第一卷/CH001_正文.md", body)],
        manifest={
            "schema": "zhimeng.candidate-draft-manifest.v1",
            "manifest_id": "draft-manifest-test",
            "files": [
                {
                    "path": "05_正文内容/第一卷/CH001_正文.md",
                    "after_sha256": hashlib.sha256(body).hexdigest(),
                    "byte_length": len(body),
                }
            ],
        },
    )

    completed, envelope = _run(tmp_path, request)
    _assert_result_contract(envelope)
    result = envelope["result"]

    assert completed.returncode == 0
    assert envelope["health"] == "healthy"
    assert result["status"] == "passed"
    assert result["quality_assessed"] is False
    assert result["formal_draft_allowed"] is False
    assert result["failure_signatures"] == []
    assert result["files"][0]["content_hash"] == hashlib.sha256(body).hexdigest()
    assert result["files"][0]["findings"] == []
    assert sorted(item.name for item in (tmp_path / "staging").iterdir()) == [
        "draft-gate.json"
    ]
    assert json.loads(
        (tmp_path / "staging" / "draft-gate.json").read_text(encoding="utf-8")
    ) == result


def test_body_contamination_is_blocked_with_locations_and_signatures(
    tmp_path: Path,
) -> None:
    body = (
        "系统：以下是完整正文。\n"
        "{\"chapter_id\": \"CH001\"}\n"
        "模型服务超时，请稍后重试。\n"
    ).encode("utf-8")

    completed, envelope = _run(
        tmp_path,
        _request([("05_正文内容/CH001_正文.md", body)]),
    )
    _assert_result_contract(envelope)
    result = envelope["result"]
    findings = result["files"][0]["findings"]

    assert completed.returncode == 1
    assert envelope["health"] == "blocked"
    assert result["status"] == "blocked"
    assert {
        "draft_role_marker_leak",
        "draft_body_contract_violation",
        "draft_metadata_leak",
        "draft_model_error_leak",
    }.issubset(result["failure_signatures"])
    assert all(
        finding["location"]["start"]["line"] >= 1
        and finding["location"]["start"]["column"] >= 1
        for finding in findings
    )
    assert all(finding["severity"] == "blocked" for finding in findings)


def test_manifest_and_declared_hash_drift_is_blocked(tmp_path: Path) -> None:
    body = "正文内容。\n".encode("utf-8")
    request = _request(
        [("05_正文内容/CH001_正文.md", body)],
        manifest={
            "schema": "zhimeng.candidate-draft-manifest.v1",
            "manifest_id": "draft-manifest-drift",
            "files": [
                {
                    "path": "05_正文内容/CH001_正文.md",
                    "after_sha256": "0" * 64,
                    "byte_length": len(body) + 1,
                },
                {
                    "path": "05_正文内容/CH002_正文.md",
                    "after_sha256": "1" * 64,
                    "byte_length": 10,
                },
            ],
        },
        declared_hashes={"05_正文内容/CH001_正文.md": "f" * 64},
    )

    completed, envelope = _run(tmp_path, request)
    _assert_result_contract(envelope)
    result = envelope["result"]

    assert completed.returncode == 1
    assert result["status"] == "blocked"
    assert "candidate_hash_mismatch" in result["failure_signatures"]
    assert "candidate_manifest_mismatch" in result["failure_signatures"]
    assert any(
        "CH002" in finding["evidence"]
        for finding in result["global_findings"]
    )


def test_invalid_path_and_empty_body_are_blocked(tmp_path: Path) -> None:
    completed, envelope = _run(
        tmp_path,
        _request(
            [
                ("../escape.md", b"safe"),
                ("05_正文内容/CH001_正文.md", b""),
            ]
        ),
    )
    _assert_result_contract(envelope)
    result = envelope["result"]

    assert completed.returncode == 1
    assert "candidate_path_invalid" in result["failure_signatures"]
    assert "draft_empty_body" in result["failure_signatures"]
    assert result["files"][1]["findings"][0]["code"] == "empty_body"


def test_formal_knowledge_validator_rules_pass_and_bind_their_hashes(
    tmp_path: Path,
) -> None:
    bundle = _validator_bundle(rules=_validator_rules())
    request = _request(
        [("05_正文内容/CH001_正文.md", "他必须在天亮前选择，并承担代价。\n".encode())],
        knowledge_validator_bundle=bundle,
    )
    assert list(
        Draft202012Validator(
            _schema("creation-draft-gate-request.schema.json")
        ).iter_errors(request)
    ) == []

    completed, envelope = _run(tmp_path, request)
    _assert_result_contract(envelope)
    result = envelope["result"]

    assert completed.returncode == 0
    assert result["status"] == "passed"
    assert result["quality_assessed"] is False
    assert result["formal_draft_allowed"] is False
    assert result["knowledge_validator_bundle_hash"] == bundle["bundle_hash"]
    execution = result["knowledge_validator_results"][0]
    assert execution["card_id"] == "validator-draft-gate-test"
    assert execution["rules_hash"] == bundle["validators"][0]["rules_hash"]
    assert execution["status"] == "passed"
    assert [item["status"] for item in execution["rule_results"]] == [
        "passed",
        "passed",
    ]
    assert result["failure_signatures"] == []


def test_forbidden_term_is_blocked_with_exact_location(tmp_path: Path) -> None:
    bundle = _validator_bundle(rules=_validator_rules())
    body = "他在雨里停住。\nArchive Night 的旧门再次打开。\n".encode()

    completed, envelope = _run(
        tmp_path,
        _request(
            [("05_正文内容/CH001_正文.md", body)],
            knowledge_validator_bundle=bundle,
        ),
    )
    _assert_result_contract(envelope)
    result = envelope["result"]
    finding = next(
        item
        for item in result["files"][0]["findings"]
        if item["failure_signature"] == "validator_source_term_leak"
    )

    assert completed.returncode == 1
    assert result["status"] == "blocked"
    assert finding["location"]["start"] == {"line": 2, "column": 1, "offset": 8}
    assert finding["evidence"] == "Archive Night"
    execution = result["knowledge_validator_results"][0]
    assert execution["status"] == "blocked"
    assert execution["rule_results"][0]["match_count"] == 1
    assert execution["rule_results"][0]["evidence_refs"] == [
        "05_正文内容/CH001_正文.md:2:1"
    ]


def test_required_term_and_missing_rule_configuration_fail_closed(
    tmp_path: Path,
) -> None:
    missing_term_bundle = _validator_bundle(rules=_validator_rules())
    completed, envelope = _run(
        tmp_path / "missing-term",
        _request(
            [("05_正文内容/CH001_正文.md", "他推开旧门，雨声压住脚步。\n".encode())],
            knowledge_validator_bundle=missing_term_bundle,
        ),
    )
    _assert_result_contract(envelope)
    result = envelope["result"]
    missing = next(
        item
        for item in result["files"][0]["findings"]
        if item["failure_signature"] == "validator_visible_pressure_missing"
    )
    assert completed.returncode == 1
    assert missing["location"]["start"] == {"line": 1, "column": 1, "offset": 0}
    assert missing["evidence"] == "<required-term-missing>"

    configuration_bundle = _validator_bundle(
        rules=[],
        configuration_errors=["validator_execution_rules_missing"],
    )
    completed, envelope = _run(
        tmp_path / "missing-rules",
        _request(
            [("05_正文内容/CH001_正文.md", "他必须在天亮前选择。\n".encode())],
            knowledge_validator_bundle=configuration_bundle,
        ),
    )
    _assert_result_contract(envelope)
    result = envelope["result"]
    assert completed.returncode == 1
    assert result["status"] == "blocked"
    assert "validator_execution_rules_missing" in result["failure_signatures"]
    assert result["knowledge_validator_results"][0]["configuration_errors"] == [
        "validator_execution_rules_missing"
    ]


@pytest.mark.parametrize(
    "tamper_kind",
    ["bundle_hash", "rules_hash", "empty_validators"],
)
def test_knowledge_validator_hash_tamper_fails_closed(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    bundle = _validator_bundle(rules=_validator_rules())
    if tamper_kind == "bundle_hash":
        bundle["bundle_hash"] = "0" * 64
        expected_error = "bundle hash does not match"
    elif tamper_kind == "rules_hash":
        bundle["validators"][0]["rules_hash"] = "0" * 64
        bundle["bundle_hash"] = _canonical_hash(
            {key: value for key, value in bundle.items() if key != "bundle_hash"}
        )
        expected_error = "rules hash does not match"
    else:
        bundle["validators"] = []
        bundle["bundle_hash"] = _canonical_hash(
            {key: value for key, value in bundle.items() if key != "bundle_hash"}
        )
        expected_error = "bundle validators are invalid"

    completed = _execute(
        tmp_path,
        _request(
            [("05_正文内容/CH001_正文.md", "他在天亮前选择。\n".encode())],
            knowledge_validator_bundle=bundle,
        ),
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert expected_error in completed.stderr
    assert list((tmp_path / "staging").iterdir()) == []
