"""Load Local v1 evaluation scenario packs without claiming quality assessment."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


POLICY_VERSION = "local-v1-evaluation-scenarios-1"
PACK_SCHEMA = "zhimeng.evaluation-scenario-pack.v1"
RESPONSE_SCHEMA = "zhimeng.evaluation-scenario-pack-response.v1"


class EvaluationScenarioError(RuntimeError):
    code = "evaluation_scenario_error"
    retryable = False

    def __init__(self, message: str, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class EvaluationScenarioNotFound(EvaluationScenarioError):
    code = "evaluation_scenario_not_found"


class EvaluationScenarioInvalid(EvaluationScenarioError):
    code = "evaluation_scenario_invalid"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _canonical_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class EvaluationScenarioService:
    """Evaluation scenario pack registry plus longform evidence execution matrix."""

    def __init__(self, *, packs_root: Path, state_root: Path | None = None) -> None:
        self.packs_root = Path(packs_root)
        self.packs_root.mkdir(parents=True, exist_ok=True)
        self.state_root = Path(state_root) if state_root is not None else self.packs_root.parent / "evaluation-scenario-executions"
        self.state_root.mkdir(parents=True, exist_ok=True)

    def list_packs(self) -> dict[str, Any]:
        packs: list[dict[str, Any]] = []
        for path in sorted(self.packs_root.glob("**/*.json")):
            try:
                pack = self._load_pack_file(path)
            except EvaluationScenarioInvalid:
                continue
            packs.append(
                {
                    "pack_id": pack["pack_id"],
                    "title": pack["title"],
                    "scenario_count": len(pack["scenarios"]),
                    "quality_assessed": False,
                    "training_ready": False,
                    "formal_draft_allowed": False,
                    "relative_path": str(path.relative_to(self.packs_root)).replace("\\", "/"),
                    "pack_hash": pack["pack_hash"],
                }
            )
        return {
            "schema": "zhimeng.evaluation-scenario-pack-list.v1",
            "packs": packs,
            "not_claiming": [
                "quality_assessed",
                "training_dataset_ready",
                "literary_quality_score",
                "local_v1_exe",
            ],
        }

    def get_pack(self, pack_id: str) -> dict[str, Any]:
        for path in sorted(self.packs_root.glob("**/*.json")):
            try:
                pack = self._load_pack_file(path)
            except EvaluationScenarioInvalid:
                continue
            if pack["pack_id"] == pack_id:
                return {
                    "schema": RESPONSE_SCHEMA,
                    "pack": pack,
                    "not_claiming": list(pack.get("not_claiming") or [])
                    + [
                        "quality_assessed",
                        "training_dataset_ready",
                        "literary_quality_score",
                        "formal_draft_authorization_unlock",
                        "local_v1_exe",
                    ],
                }
        raise EvaluationScenarioNotFound(f"evaluation scenario pack not found: {pack_id}")

    def bind_pack_to_suite(
        self,
        *,
        pack_id: str,
        suite_id: str,
        suite_receipt_hash: str,
        member_count: int,
        has_provider_fingerprint: bool,
        blind_review_finalized: bool,
    ) -> dict[str, Any]:
        pack_response = self.get_pack(pack_id)
        pack = pack_response["pack"]
        checks: list[dict[str, Any]] = []
        for scenario in pack["scenarios"]:
            member_ok = member_count >= int(scenario.get("min_member_count") or 1)
            fingerprint_ok = (
                has_provider_fingerprint
                if scenario.get("requires_provider_fingerprint")
                else True
            )
            blind_ok = (
                blind_review_finalized if scenario.get("requires_blind_review") else True
            )
            ready = member_ok and fingerprint_ok and blind_ok
            checks.append(
                {
                    "scenario_id": scenario["scenario_id"],
                    "ready": ready,
                    "member_count_ok": member_ok,
                    "provider_fingerprint_ok": fingerprint_ok,
                    "blind_review_ok": blind_ok,
                    "detail": (
                        "scenario readiness inputs satisfied"
                        if ready
                        else "scenario readiness inputs incomplete"
                    ),
                }
            )
        ready_count = sum(1 for item in checks if item["ready"])
        all_ready = ready_count == len(checks) and len(checks) > 0
        generated_at = _utcnow()
        material = {
            "schema": "zhimeng.evaluation-scenario-suite-binding.v1",
            "pack_id": pack_id,
            "pack_hash": pack["pack_hash"],
            "suite_id": suite_id,
            "suite_receipt_hash": suite_receipt_hash,
            "member_count": member_count,
            "has_provider_fingerprint": has_provider_fingerprint,
            "blind_review_finalized": blind_review_finalized,
            "checks": checks,
            "ready_count": ready_count,
            "all_ready": all_ready,
            "quality_assessed": False,
            "training_ready": False,
            "formal_draft_allowed": False,
            "policy_version": POLICY_VERSION,
            "generated_at": generated_at.isoformat(),
        }
        binding_id = "eval-scenario-binding-" + _canonical_hash(material)[:32]
        material["binding_id"] = binding_id
        material["binding_hash"] = _canonical_hash(material)
        return {
            "schema": "zhimeng.evaluation-scenario-suite-binding-response.v1",
            "binding": material,
            "not_claiming": [
                "quality_assessed",
                "training_dataset_ready",
                "literary_quality_score",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }


    def execute_pack_on_project(
        self,
        *,
        pack_id: str,
        project_id: str,
        project_revision: int,
        project_fingerprint: str,
        available_chapters: list[str],
        available_artifacts: list[str],
        suite_id: str | None = None,
        suite_receipt_hash: str | None = None,
        member_count: int = 1,
        has_provider_fingerprint: bool = False,
        blind_review_finalized: bool = False,
        external_blind_review_present: bool = False,
    ) -> dict[str, Any]:
        """Execute scenario readiness matrix against real longform project evidence.

        This is not literary quality scoring. It records which required chapters and
        artifacts are present for each scenario, and keeps quality_assessed=false.
        """
        pack_response = self.get_pack(pack_id)
        pack = pack_response["pack"]
        chapters = sorted({str(item).strip() for item in available_chapters if str(item).strip()})
        artifacts = sorted({str(item).strip() for item in available_artifacts if str(item).strip()})
        results: list[dict[str, Any]] = []
        for scenario in pack["scenarios"]:
            required_chapters = [str(item) for item in scenario.get("required_chapters") or []]
            required_artifacts = [str(item) for item in scenario.get("required_artifacts") or []]
            missing_chapters = [item for item in required_chapters if item not in chapters]
            missing_artifacts = [item for item in required_artifacts if item not in artifacts]
            member_ok = member_count >= int(scenario.get("min_member_count") or 1)
            fingerprint_ok = (
                has_provider_fingerprint
                if scenario.get("requires_provider_fingerprint")
                else True
            )
            blind_ok = (
                blind_review_finalized if scenario.get("requires_blind_review") else True
            )
            evidence_complete = not missing_chapters and not missing_artifacts
            suite_ready = member_ok and fingerprint_ok and blind_ok
            if evidence_complete and suite_ready:
                status = "executed"
            elif evidence_complete:
                status = "evidence_ready_suite_incomplete"
            elif suite_ready:
                status = "suite_ready_evidence_incomplete"
            else:
                status = "incomplete"
            results.append(
                {
                    "scenario_id": scenario["scenario_id"],
                    "title": scenario["title"],
                    "kind": scenario["kind"],
                    "status": status,
                    "required_chapters": required_chapters,
                    "present_chapters": [item for item in required_chapters if item in chapters],
                    "missing_chapters": missing_chapters,
                    "required_artifacts": required_artifacts,
                    "present_artifacts": [
                        item for item in required_artifacts if item in artifacts
                    ],
                    "missing_artifacts": missing_artifacts,
                    "member_count_ok": member_ok,
                    "provider_fingerprint_ok": fingerprint_ok,
                    "blind_review_ok": blind_ok,
                    "evidence_complete": evidence_complete,
                    "suite_ready": suite_ready,
                    "rubric_dimensions": list(scenario.get("rubric_dimensions") or []),
                    "scored": False,
                    "quality_assessed": False,
                    "detail": (
                        "longform evidence matrix satisfied for this scenario; literary score not claimed"
                        if status == "executed"
                        else "longform evidence matrix incomplete for this scenario"
                    ),
                }
            )
        executed_count = sum(1 for item in results if item["status"] == "executed")
        evidence_ready_count = sum(1 for item in results if item["evidence_complete"])
        all_executed = executed_count == len(results) and len(results) > 0
        generated_at = _utcnow()
        material = {
            "schema": "zhimeng.evaluation-scenario-execution.v1",
            "pack_id": pack_id,
            "pack_hash": pack["pack_hash"],
            "project_id": project_id,
            "project_revision": project_revision,
            "project_fingerprint": project_fingerprint,
            "available_chapters": chapters,
            "available_artifacts": artifacts,
            "suite_id": suite_id,
            "suite_receipt_hash": suite_receipt_hash,
            "member_count": member_count,
            "has_provider_fingerprint": has_provider_fingerprint,
            "blind_review_finalized": blind_review_finalized,
            "external_blind_review_present": external_blind_review_present,
            "results": results,
            "scenario_count": len(results),
            "executed_count": executed_count,
            "evidence_ready_count": evidence_ready_count,
            "all_executed": all_executed,
            "matrix_complete": all_executed and external_blind_review_present,
            "quality_assessed": False,
            "training_ready": False,
            "formal_draft_allowed": False,
            "literary_quality_score": None,
            "policy_version": POLICY_VERSION,
            "generated_at": generated_at.isoformat(),
        }
        execution_id = "eval-scenario-execution-" + _canonical_hash(material)[:32]
        material["execution_id"] = execution_id
        material["execution_hash"] = _canonical_hash(material)
        out = self.state_root / project_id / f"{pack_id}-{execution_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(material, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        latest = self.state_root / project_id / f"{pack_id}-latest.json"
        latest.write_text(json.dumps(material, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "schema": "zhimeng.evaluation-scenario-execution-response.v1",
            "execution": material,
            "not_claiming": [
                "quality_assessed",
                "training_dataset_ready",
                "literary_quality_score",
                "external_blind_review_matrix",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }

    def get_latest_execution(self, *, pack_id: str, project_id: str) -> dict[str, Any]:
        path = self.state_root / project_id / f"{pack_id}-latest.json"
        if not path.is_file():
            raise EvaluationScenarioNotFound(
                f"evaluation scenario execution not found for pack={pack_id} project={project_id}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "schema": "zhimeng.evaluation-scenario-execution-response.v1",
            "execution": payload,
            "not_claiming": [
                "quality_assessed",
                "training_dataset_ready",
                "literary_quality_score",
                "external_blind_review_matrix",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }

    def _load_pack_file(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvaluationScenarioInvalid(f"invalid pack file: {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise EvaluationScenarioInvalid(f"pack root must be object: {path}")
        if payload.get("schema") != PACK_SCHEMA:
            raise EvaluationScenarioInvalid(f"unsupported pack schema: {path}")
        pack_id = str(payload.get("pack_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        scenarios = payload.get("scenarios")
        if not pack_id or not title or not isinstance(scenarios, list) or not scenarios:
            raise EvaluationScenarioInvalid(f"pack missing required fields: {path}")
        if payload.get("quality_assessed") is not False:
            raise EvaluationScenarioInvalid("pack cannot claim quality_assessed")
        if payload.get("training_ready") is not False:
            raise EvaluationScenarioInvalid("pack cannot claim training_ready")
        if payload.get("formal_draft_allowed") is not False:
            raise EvaluationScenarioInvalid("pack cannot allow formal draft")
        normalized_scenarios: list[dict[str, Any]] = []
        for item in scenarios:
            if not isinstance(item, dict):
                raise EvaluationScenarioInvalid("scenario must be object")
            scenario_id = str(item.get("scenario_id") or "").strip()
            if not scenario_id:
                raise EvaluationScenarioInvalid("scenario_id required")
            normalized_scenarios.append(
                {
                    "scenario_id": scenario_id,
                    "title": str(item.get("title") or scenario_id),
                    "kind": str(item.get("kind") or "unspecified"),
                    "required_chapters": list(item.get("required_chapters") or []),
                    "required_artifacts": list(item.get("required_artifacts") or []),
                    "rubric_dimensions": list(item.get("rubric_dimensions") or []),
                    "min_member_count": int(item.get("min_member_count") or 1),
                    "requires_provider_fingerprint": bool(
                        item.get("requires_provider_fingerprint")
                    ),
                    "requires_blind_review": bool(item.get("requires_blind_review")),
                }
            )
        pack_material = {
            "schema": PACK_SCHEMA,
            "pack_id": pack_id,
            "title": title,
            "policy_version": str(payload.get("policy_version") or POLICY_VERSION),
            "scenarios": normalized_scenarios,
            "quality_assessed": False,
            "training_ready": False,
            "formal_draft_allowed": False,
        }
        return {
            **pack_material,
            "not_claiming": list(payload.get("not_claiming") or []),
            "source_path": str(path),
            "pack_hash": _canonical_hash(pack_material),
            "loaded_at": _utcnow().isoformat(),
        }


__all__ = [
    "EvaluationScenarioError",
    "EvaluationScenarioInvalid",
    "EvaluationScenarioNotFound",
    "EvaluationScenarioService",
]
