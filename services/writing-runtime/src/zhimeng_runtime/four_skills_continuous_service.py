from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json

from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.four_skills_continuous_models import (
    FourSkillsContinuousEvaluateRequest,
    FourSkillsContinuousReceipt,
    FourSkillsContinuousResponse,
    FourSkillsContinuousStage,
    FourSkillsContinuousStageCode,
    FourSkillsContinuousStatus,
    four_skills_continuous_receipt_hash,
)


PROJECT_ID_PATTERN = __import__("re").compile(r"^project-[0-9a-f]{24}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class FourSkillsContinuousError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: list[dict[str, object]] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = details or []
        self.retryable = retryable


class FourSkillsContinuousNotFound(FourSkillsContinuousError):
    pass


class FourSkillsContinuousInvalid(FourSkillsContinuousError):
    pass


class FourSkillsContinuousConflict(FourSkillsContinuousError):
    pass


class FourSkillsContinuousService:
    """Evaluate linked continuous evidence across the four skill stages.

    This is an authorization/evidence aggregator for Local v1. It never claims
    four_skills_generic_exactly_once and never unlocks formal draft.
    """

    def __init__(
        self,
        database: SqliteStore,
        *,
        runtime_state_root: Path | None = None,
    ) -> None:
        self.database = database
        self.runtime_state_root = (
            Path(runtime_state_root)
            if runtime_state_root is not None
            else Path(database.path).parent
        )
        self.adjacent_chain_root = self.runtime_state_root / "adjacent-chains"
        self.managed_skill_chain_root = self.runtime_state_root / "managed-skill-chains"
        self.receipt_root = self.runtime_state_root / "four-skills-continuous"

    def evaluate(
        self,
        project_id: str,
        request: FourSkillsContinuousEvaluateRequest | None = None,
    ) -> FourSkillsContinuousResponse:
        request = request or FourSkillsContinuousEvaluateRequest()
        project_id = project_id.strip()
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise FourSkillsContinuousInvalid("project_id is invalid")
        binding = self.database.get_project_binding(project_id)
        if binding is None:
            raise FourSkillsContinuousNotFound("project binding was not found")

        if (
            request.expected_project_revision is not None
            and request.expected_project_revision != binding.revision
        ):
            raise FourSkillsContinuousConflict(
                "project revision does not match the continuous evaluation request",
                details=[{
                    "code": "project_revision_mismatch",
                    "expected": request.expected_project_revision,
                    "actual": binding.revision,
                }],
                retryable=True,
            )
        if (
            request.expected_project_fingerprint is not None
            and request.expected_project_fingerprint != (binding.fingerprint or "")
        ):
            raise FourSkillsContinuousConflict(
                "project fingerprint does not match the continuous evaluation request",
                details=[{
                    "code": "project_fingerprint_mismatch",
                    "expected": request.expected_project_fingerprint,
                    "actual": binding.fingerprint,
                }],
                retryable=True,
            )

        source = self._load_source_stage(
            preferred_source_import_id=request.source_import_id,
        )
        formalization = self._load_formalization_stage(
            source_import_id=None if source is None else str(source["source_import_id"]),
        )
        adjacent = self._load_adjacent_stage(
            project_id,
            preferred_chain_id=request.adjacent_chain_id,
            source_import_id=None if source is None else str(source["source_import_id"]),
        )
        creation = self._load_creation_stage(
            project_id,
            preferred_chain_id=request.managed_skill_chain_id,
        )

        source_present = source is not None
        formalization_present = formalization is not None
        adjacent_present = adjacent is not None
        creation_present = creation is not None

        formalization_linked = bool(
            formalization_present
            and source_present
            and formalization["source_import_id"] == source["source_import_id"]
        )
        adjacent_linked = bool(
            adjacent_present
            and source_present
            and (
                adjacent.get("source_import_id") in {None, "", source["source_import_id"]}
                or adjacent.get("source_import_id") == source["source_import_id"]
            )
            and str(adjacent.get("project_id") or "") in {"", project_id}
        )
        # Require explicit source_import_id match when adjacent carries one.
        if adjacent_present and source_present:
            adj_source = adjacent.get("source_import_id")
            if adj_source not in {None, ""}:
                adjacent_linked = adj_source == source["source_import_id"]
            else:
                adjacent_linked = False
        creation_linked = bool(
            creation_present
            and str(creation.get("project_id") or "") in {"", project_id}
            and creation.get("creation_suite") is True
        )

        stages = [
            FourSkillsContinuousStage(
                code=FourSkillsContinuousStageCode.SOURCE_INGEST,
                ordinal=1,
                required=True,
                present=source_present,
                linked=source_present,
                status="ready" if source_present else "missing",
                evidence_id=None if source is None else str(source["source_import_id"]),
                detail=(
                    f"source import ready status={source['status']}"
                    if source is not None
                    else "no source import evidence available for continuous chain"
                ),
            ),
            FourSkillsContinuousStage(
                code=FourSkillsContinuousStageCode.DISTILLATION_FORMALIZATION,
                ordinal=2,
                required=True,
                present=formalization_present,
                linked=formalization_linked,
                status="formalized" if formalization_linked else (
                    "present_unlinked" if formalization_present else "missing"
                ),
                evidence_id=(
                    None
                    if formalization is None
                    else str(formalization["formalization_receipt_id"])
                ),
                detail=(
                    "distillation formalization linked to source import"
                    if formalization_linked
                    else (
                        "formalization present but not linked to source import"
                        if formalization_present
                        else "no distillation formalization receipt for continuous chain"
                    )
                ),
            ),
            FourSkillsContinuousStage(
                code=FourSkillsContinuousStageCode.ADJACENT_O2,
                ordinal=3,
                required=True,
                present=adjacent_present,
                linked=adjacent_linked,
                status="succeeded" if adjacent_linked else (
                    "present_unlinked" if adjacent_present else "missing"
                ),
                evidence_id=None if adjacent is None else str(adjacent["chain_id"]),
                detail=(
                    "adjacent O2-D->O2-E chain succeeded and linked to source import"
                    if adjacent_linked
                    else (
                        "adjacent chain present but not linked to source import"
                        if adjacent_present
                        else "no succeeded adjacent O2 continuous chain"
                    )
                ),
            ),
            FourSkillsContinuousStage(
                code=FourSkillsContinuousStageCode.CREATION_SUITE,
                ordinal=4,
                required=True,
                present=creation_present,
                linked=creation_linked,
                status="succeeded" if creation_linked else (
                    "present_unlinked" if creation_present else "missing"
                ),
                evidence_id=None if creation is None else str(creation["chain_id"]),
                detail=(
                    "creation-suite continuous managed chain succeeded for project"
                    if creation_linked
                    else (
                        "creation chain present but not a linked creation-suite success"
                        if creation_present
                        else "no succeeded creation-suite continuous chain"
                    )
                ),
            ),
        ]

        missing_required = [
            item.code
            for item in stages
            if item.required and not (item.present and item.linked)
        ]
        status = (
            FourSkillsContinuousStatus.COMPLETE
            if not missing_required
            else FourSkillsContinuousStatus.INCOMPLETE
        )
        evaluated_at = _utcnow()
        receipt_id = _digest_id(
            "four-skills-continuous-",
            project_id,
            binding.revision,
            binding.fingerprint or "",
            source["source_import_id"] if source else "",
            formalization["formalization_receipt_id"] if formalization else "",
            adjacent["chain_id"] if adjacent else "",
            creation["chain_id"] if creation else "",
            status.value,
            ",".join(item.value for item in missing_required),
            evaluated_at.isoformat(),
        )
        kwargs = {
            "receipt_id": receipt_id,
            "project_id": project_id,
            "project_revision": binding.revision,
            "project_fingerprint": binding.fingerprint or None,
            "status": status,
            "authorization_input_ready": status is FourSkillsContinuousStatus.COMPLETE,
            "authorization_unlock": False,
            "formal_draft_allowed": False,
            "writes_project_truth": False,
            "four_skills_generic_exactly_once": False,
            "stages": stages,
            "missing_required": missing_required,
            "source_import_id": None if source is None else str(source["source_import_id"]),
            "formalization_receipt_id": (
                None
                if formalization is None
                else str(formalization["formalization_receipt_id"])
            ),
            "adjacent_chain_id": None if adjacent is None else str(adjacent["chain_id"]),
            "creation_suite_chain_id": (
                None if creation is None else str(creation["chain_id"])
            ),
            "policy_version": "local-v1-four-skills-continuous-1",
            "evaluated_at": evaluated_at,
        }
        provisional = FourSkillsContinuousReceipt.model_construct(
            schema_id="zhimeng.four-skills-continuous-receipt.v1",
            receipt_hash="0" * 64,
            **kwargs,
        )
        receipt = FourSkillsContinuousReceipt(
            receipt_hash=four_skills_continuous_receipt_hash(provisional),
            **kwargs,
        )

        project_root = self.receipt_root / project_id
        project_root.mkdir(parents=True, exist_ok=True)
        payload = receipt.model_dump(mode="json", by_alias=True)
        (project_root / "receipt.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return FourSkillsContinuousResponse(receipt=receipt)

    def _load_source_stage(
        self,
        *,
        preferred_source_import_id: str | None,
    ) -> dict[str, object] | None:
        if preferred_source_import_id:
            job = self.database.get_source_import_job(preferred_source_import_id)
            if job is None:
                return None
            status = str(job.get("status") or "")
            if status not in {
                "awaiting_review",
                "completed",
                "formalized",
                "succeeded",
            }:
                # Seeded knowledge fixtures may leave jobs in awaiting_review.
                # Treat non-failed terminal-ish statuses with draft evidence as ready.
                if status in {"failed", "cancelled"}:
                    return None
            return {
                "source_import_id": str(
                    job.get("import_id") or job.get("id") or preferred_source_import_id
                ),
                "status": status or "ready",
            }

        jobs = self.database.list_source_import_jobs()
        for job in jobs:
            status = str(job.get("status") or "")
            if status in {"failed", "cancelled", "interrupted"}:
                continue
            import_id = str(job.get("import_id") or job.get("id") or "")
            if not import_id:
                continue
            return {"source_import_id": import_id, "status": status or "ready"}
        return None

    def _load_formalization_stage(
        self,
        *,
        source_import_id: str | None,
    ) -> dict[str, object] | None:
        if not source_import_id:
            return None
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT id, source_import_id, receipt_hash, created_at
                FROM knowledge_formalization_receipt
                WHERE source_import_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (source_import_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "formalization_receipt_id": str(row["id"]),
            "source_import_id": str(row["source_import_id"]),
            "receipt_hash": str(row["receipt_hash"]),
        }

    def _load_adjacent_stage(
        self,
        project_id: str,
        *,
        preferred_chain_id: str | None,
        source_import_id: str | None,
    ) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        if preferred_chain_id:
            payload = self._read_json(self.adjacent_chain_root / f"{preferred_chain_id}.json")
            if payload is not None:
                candidates.append(payload)
        else:
            digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:24]
            index = self._read_json(self.adjacent_chain_root / f"project-{digest}.json")
            latest_id = None if index is None else index.get("latest_chain_id")
            if isinstance(latest_id, str) and latest_id:
                payload = self._read_json(self.adjacent_chain_root / f"{latest_id}.json")
                if payload is not None:
                    candidates.append(payload)
            if self.adjacent_chain_root.is_dir():
                for path in sorted(self.adjacent_chain_root.glob("adjacent-chain-*.json")):
                    payload = self._read_json(path)
                    if payload is not None:
                        candidates.append(payload)

        for payload in candidates:
            if str(payload.get("status") or "") != "succeeded":
                continue
            project = payload.get("project")
            project_match = True
            if isinstance(project, dict):
                project_match = str(project.get("project_id") or "") in {"", project_id}
            if not project_match:
                continue
            steps = payload.get("steps")
            if not isinstance(steps, list) or len(steps) < 2:
                continue
            if any(str(item.get("status") or "") != "succeeded" for item in steps if isinstance(item, dict)):
                continue
            return {
                "chain_id": str(payload.get("chain_id") or ""),
                "source_import_id": payload.get("source_import_id"),
                "project_id": (
                    str(project.get("project_id"))
                    if isinstance(project, dict) and project.get("project_id")
                    else project_id
                ),
                "context_pack_id": payload.get("context_pack_id"),
            }
        return None

    def _load_creation_stage(
        self,
        project_id: str,
        *,
        preferred_chain_id: str | None,
    ) -> dict[str, object] | None:
        candidates: list[dict[str, object]] = []
        if preferred_chain_id:
            payload = self._read_json(
                self.managed_skill_chain_root / f"{preferred_chain_id}.json"
            )
            if payload is not None:
                candidates.append(payload)
        elif self.managed_skill_chain_root.is_dir():
            for path in sorted(
                self.managed_skill_chain_root.glob("managed-skill-chain-*.json")
            ):
                payload = self._read_json(path)
                if payload is not None:
                    candidates.append(payload)

        for payload in candidates:
            if str(payload.get("status") or "") != "succeeded":
                continue
            project = payload.get("project")
            if isinstance(project, dict):
                if str(project.get("project_id") or "") not in {"", project_id}:
                    continue
            steps = payload.get("steps")
            if not isinstance(steps, list) or len(steps) < 2:
                continue
            if any(
                not isinstance(item, dict) or str(item.get("status") or "") != "succeeded"
                for item in steps
            ):
                continue
            skill_ids = [
                str(item.get("skill_id") or "")
                for item in steps
                if isinstance(item, dict)
            ]
            chain_key = str(payload.get("chain_key") or "")
            creation_suite = chain_key.startswith("creation-suite") or any(
                item.startswith("novel-creation-suite.") for item in skill_ids
            )
            if not creation_suite:
                # Multi-step managed chain still counts as managed continuous
                # evidence only when creation-suite skills are used.
                continue
            return {
                "chain_id": str(payload.get("chain_id") or ""),
                "project_id": (
                    str(project.get("project_id"))
                    if isinstance(project, dict) and project.get("project_id")
                    else project_id
                ),
                "creation_suite": True,
                "skill_ids": skill_ids,
            }
        return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


__all__ = [
    "FourSkillsContinuousConflict",
    "FourSkillsContinuousError",
    "FourSkillsContinuousInvalid",
    "FourSkillsContinuousNotFound",
    "FourSkillsContinuousService",
]
