from __future__ import annotations

import hashlib
import os
import json
import re
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import yaml

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.errors import ApiError
from zhimeng_runtime.managed_skill_service import (
    ManagedSkillError,
    ManagedSkillService,
    MANIFEST_NAME,
)
from zhimeng_runtime.skill_adaptation_models import (
    SkillAdaptationMaterialization,
    SkillAdaptationMaterializeRequest,
    SkillAdaptationMaterializationResponse,
    SkillAdaptationMissing,
    SkillAdaptationPlan,
    SkillAdaptationPlanRequest,
    SkillAdaptationPlanResponse,
    SkillAdaptationSource,
    SkillAdaptationTarget,
)


ADAPTER_ID = "zhimeng.raw-skill-instruction"
ADAPTER_VERSION = "1"
ADAPTER_FINGERPRINT = hashlib.sha256(
    b"zhimeng-raw-skill-instruction-adapter-v1\0"
    b"instruction_source_only\0no_entrypoint\0no_network\0copy_skill_md"
).hexdigest()
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MANAGED_SKILL_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")
MAX_SKILL_MD_BYTES = 512 * 1024
REPARSE_POINT_ATTRIBUTE = 0x400


class SkillAdaptationError(RuntimeError):
    code = "skill_adaptation_error"
    status_code = 409
    retryable = False

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.details = details


class SkillAdaptationNotFound(SkillAdaptationError):
    code = "skill_adaptation_not_found"
    status_code = 404


class SkillAdaptationUnavailable(SkillAdaptationError):
    code = "skill_adaptation_unavailable"
    status_code = 503
    retryable = True


class SkillAdaptationInvalid(SkillAdaptationError):
    code = "skill_adaptation_invalid"
    status_code = 422


class SkillAdaptationConflict(SkillAdaptationError):
    code = "skill_adaptation_conflict"
    status_code = 409


class SkillAdaptationNotAllowed(SkillAdaptationError):
    code = "skill_adaptation_not_allowed"
    status_code = 403


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _digest_id(prefix: str, *parts: object) -> str:
    material = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


class SkillAdaptationService:
    """Plan and materialize raw SKILL.md into inert instruction_source packages.

    Never executes raw SKILL.md and never auto-generates deterministic tools.
    """

    def __init__(
        self,
        managed_skill_service: ManagedSkillService | None,
        *,
        trusted_skill_roots: tuple[Path, ...],
        runtime_state_root: Path,
    ) -> None:
        self.managed_skill_service = managed_skill_service
        self.trusted_skill_roots = tuple(Path(item) for item in trusted_skill_roots)
        self.root = Path(runtime_state_root) / "managed-skill-adaptations"
        self.plans_root = self.root / "plans"
        self.materialized_root = self.root / "materialized"
        self.plans_root.mkdir(parents=True, exist_ok=True)
        self.materialized_root.mkdir(parents=True, exist_ok=True)

    def plan(self, request: SkillAdaptationPlanRequest) -> SkillAdaptationPlanResponse:
        source_root = self._resolve_trusted_source(Path(request.source_path))
        if (source_root / MANIFEST_NAME).exists():
            raise SkillAdaptationConflict(
                "来源已有 skill.manifest.json，请直接走 managed import，不需要 raw adaptation。"
            )
        skill_md = source_root / "SKILL.md"
        if not skill_md.is_file():
            raise SkillAdaptationInvalid("raw Skill 来源必须包含 SKILL.md。")
        content, skill_md_hash = self._read_skill_md(skill_md)
        name, description = self._parse_frontmatter(content)
        target_skill_id = self._derive_skill_id(name)
        version = f"2026.07.26-adapted.{skill_md_hash[:12]}"
        missing = (
            SkillAdaptationMissing(
                code="managed_manifest_missing",
                message="缺少 skill.manifest.json；materialize 将生成 instruction_source 包。",
            ),
            SkillAdaptationMissing(
                code="deterministic_contracts_absent",
                message="未生成 deterministic input/output contracts；不能当作可执行 tool。",
            ),
        )
        now = _utcnow()
        source = SkillAdaptationSource(
            source_path=str(source_root),
            skill_md_path=str(skill_md),
            skill_md_hash=skill_md_hash,
            skill_md_bytes=len(content),
            frontmatter_name=name,
            frontmatter_description=description,
            locator_hash=hashlib.sha256(str(source_root).encode("utf-8")).hexdigest(),
        )
        target = SkillAdaptationTarget(
            skill_id=target_skill_id,
            display_name=name.replace("-", " ").replace("_", " ").title(),
            description=description,
            kind="instruction_source",
            version=version,
        )
        plan_id = _digest_id(
            "skill-adaptation-plan-",
            source.locator_hash,
            skill_md_hash,
            target_skill_id,
            version,
            ADAPTER_FINGERPRINT,
        )
        provisional = SkillAdaptationPlan(
            plan_id=plan_id,
            adapter_fingerprint=ADAPTER_FINGERPRINT,
            status="ready_to_materialize",
            source=source,
            target=target,
            missing=missing,
            created_at=now,
            plan_hash="0" * 64,
        )
        material = provisional.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        plan = provisional.model_copy(update={"plan_hash": canonical_json_hash(material)})
        self._save_plan(plan)
        return SkillAdaptationPlanResponse(plan=plan)

    def materialize(
        self,
        request: SkillAdaptationMaterializeRequest,
    ) -> SkillAdaptationMaterializationResponse:
        plan = self._require_plan(request.expected_plan_id)
        if plan.plan_hash != request.expected_plan_hash:
            raise SkillAdaptationConflict("adaptation plan hash 不匹配，请重新 plan。")
        material = plan.model_dump(mode="json", by_alias=True, exclude={"plan_hash"})
        if plan.plan_hash != canonical_json_hash(material):
            raise SkillAdaptationConflict("adaptation plan 已被篡改。")
        if plan.status != "ready_to_materialize":
            raise SkillAdaptationConflict("当前 adaptation plan 不能 materialize。")

        package_root = self.materialized_root / plan.plan_id / "package"
        if package_root.exists():
            shutil.rmtree(package_root)
        package_root.mkdir(parents=True)

        skill_md_source = Path(plan.source.skill_md_path)
        content, skill_md_hash = self._read_skill_md(skill_md_source)
        if skill_md_hash != plan.source.skill_md_hash:
            raise SkillAdaptationConflict("源 SKILL.md 已变化，请重新 plan。")

        resources = package_root / "resources"
        resources.mkdir(parents=True)
        (resources / "SKILL.md").write_bytes(content)
        line_count = content.decode("utf-8-sig").count("\n") + (
            0 if content.endswith(b"\n") else 1
        )
        instruction_map = {
            "schema": "zhimeng.raw-skill-instruction-map.v1",
            "skill_id": plan.target.skill_id,
            "source_path": "resources/SKILL.md",
            "source_sha256": skill_md_hash,
            "source_bytes": len(content),
            "source_line_count": line_count,
            "include_full_source": False,
            "note": (
                "Adapted raw SKILL.md is review-only instruction material; "
                "Runtime never executes it as a managed tool."
            ),
        }
        (package_root / "instruction-map.json").write_text(
            json.dumps(instruction_map, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        provenance = (
            "# Adapted raw SKILL.md\n\n"
            f"- adapter: {ADAPTER_ID}@{ADAPTER_VERSION}\n"
            f"- source: `{plan.source.source_path}`\n"
            f"- skill_md_hash: `{plan.source.skill_md_hash}`\n"
            "- kind: instruction_source only\n"
            "- formal_draft_allowed: false\n"
            "- not executable as managed tool\n"
        )
        (package_root / "PROVENANCE.md").write_text(provenance, encoding="utf-8")
        manifest = {
            "schema": "zhimeng.skill-manifest.v1",
            "id": plan.target.skill_id,
            "display_name": plan.target.display_name,
            "description": plan.target.description,
            "kind": "instruction_source",
            "version": plan.target.version,
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
            "artifacts": [],
            "capabilities": [plan.target.capability],
            "resources": sorted(set(plan.target.resources)),
        }
        (package_root / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        package_fingerprint = self._package_fingerprint(package_root)
        now = _utcnow()
        materialization_id = _digest_id(
            "skill-adaptation-materialization-",
            plan.plan_id,
            package_fingerprint,
            plan.plan_hash,
        )
        imported = False
        skill_version_id = None
        import_receipt_id = None
        import_result = None
        if request.import_after_materialize:
            if self.managed_skill_service is None:
                raise SkillAdaptationUnavailable("Managed Skill service is unavailable")
            try:
                imported_response = self.managed_skill_service.import_runtime_owned_package(
                    package_root,
                )
            except ManagedSkillError as exc:
                raise SkillAdaptationConflict(str(exc)) from exc
            imported = True
            skill_version_id = imported_response.version.skill_version_id
            import_receipt_id = imported_response.receipt.receipt_id
            import_result = imported_response.model_dump(mode="json", by_alias=True)
            package_fingerprint = imported_response.version.package_fingerprint

        provisional = SkillAdaptationMaterialization(
            materialization_id=materialization_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            package_root=str(package_root),
            package_fingerprint=package_fingerprint,
            skill_id=plan.target.skill_id,
            skill_version=plan.target.version,
            kind="instruction_source",
            formal_draft_allowed=False,
            imported=imported,
            skill_version_id=skill_version_id,
            import_receipt_id=import_receipt_id,
            created_at=now,
            materialization_hash="0" * 64,
        )
        hash_material = provisional.model_dump(
            mode="json",
            by_alias=True,
            exclude={"materialization_hash"},
        )
        materialization = provisional.model_copy(
            update={"materialization_hash": canonical_json_hash(hash_material)}
        )
        self._save_materialization(materialization)
        return SkillAdaptationMaterializationResponse(
            materialization=materialization,
            import_result=import_result,
        )

    def get_plan(self, plan_id: str) -> SkillAdaptationPlanResponse:
        return SkillAdaptationPlanResponse(plan=self._require_plan(plan_id))

    def _resolve_trusted_source(self, raw: Path) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise SkillAdaptationInvalid("source_path 必须是绝对路径。")
        try:
            value = candidate.lstat()
        except OSError as exc:
            raise SkillAdaptationInvalid("source_path 不可访问。") from exc
        if not stat.S_ISDIR(value.st_mode):
            raise SkillAdaptationInvalid("source_path 必须是目录。")
        if self._is_link_or_reparse(value) or getattr(candidate, "is_junction", lambda: False)():
            raise SkillAdaptationInvalid("source_path 不能是链接或 reparse point。")
        resolved = candidate.resolve(strict=True)
        if not self.trusted_skill_roots:
            raise SkillAdaptationNotAllowed(
                "未配置 trusted skill roots，不能对 raw SKILL.md 做 adaptation。"
            )
        if not any(self._is_within(resolved, root.resolve()) for root in self.trusted_skill_roots):
            raise SkillAdaptationNotAllowed(
                "raw Skill 来源不在 trusted skill roots 内。"
            )
        return resolved

    def _read_skill_md(self, path: Path) -> tuple[bytes, str]:
        try:
            value = path.lstat()
        except OSError as exc:
            raise SkillAdaptationInvalid("SKILL.md 不可访问。") from exc
        if not stat.S_ISREG(value.st_mode) or self._is_link_or_reparse(value):
            raise SkillAdaptationInvalid("SKILL.md 必须是普通文件。")
        if int(getattr(value, "st_nlink", 1)) > 1:
            raise SkillAdaptationInvalid("SKILL.md 不能是 hardlink。")
        if value.st_size > MAX_SKILL_MD_BYTES:
            raise SkillAdaptationInvalid(
                f"SKILL.md 超过 {MAX_SKILL_MD_BYTES} 字节上限。"
            )
        content = path.read_bytes()
        if len(content) != value.st_size:
            raise SkillAdaptationConflict("SKILL.md 在读取过程中发生变化。")
        return content, hashlib.sha256(content).hexdigest()

    def _parse_frontmatter(self, content: bytes) -> tuple[str, str]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SkillAdaptationInvalid("SKILL.md 必须是 UTF-8。") from exc
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise SkillAdaptationInvalid("SKILL.md 必须以 YAML frontmatter 开头。")
        closing = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        if closing is None:
            raise SkillAdaptationInvalid("SKILL.md frontmatter 未闭合。")
        try:
            parsed = yaml.safe_load("\n".join(lines[1:closing]))
        except yaml.YAMLError as exc:
            raise SkillAdaptationInvalid(f"SKILL.md frontmatter YAML 无效: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SkillAdaptationInvalid("SKILL.md frontmatter 必须是 mapping。")
        name = parsed.get("name")
        description = parsed.get("description")
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
            raise SkillAdaptationInvalid("frontmatter name 必须是合法 skill 标识。")
        if not isinstance(description, str) or not description.strip():
            raise SkillAdaptationInvalid("frontmatter description 不能为空。")
        return name, " ".join(description.split())

    def _derive_skill_id(self, name: str) -> str:
        candidate = f"adapted.{name}"
        if not MANAGED_SKILL_ID_PATTERN.fullmatch(candidate):
            raise SkillAdaptationInvalid(
                f"无法从 name={name} 派生 managed skill id。"
            )
        if len(candidate) > 128:
            raise SkillAdaptationInvalid("派生 skill id 超过 128 字符。")
        return candidate

    def _package_fingerprint(self, package_root: Path) -> str:
        files: list[tuple[str, bytes]] = []
        for path in sorted(package_root.rglob("*")):
            if path.is_dir():
                continue
            relative = path.relative_to(package_root).as_posix()
            files.append((relative, path.read_bytes()))
        material = [
            {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}
            for relative, content in files
        ]
        return hashlib.sha256(
            json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest()

    def _is_within(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _is_link_or_reparse(self, value: os.stat_result) -> bool:
        return bool(
            stat.S_ISLNK(value.st_mode)
            or (getattr(value, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE)
        )

    def _save_plan(self, plan: SkillAdaptationPlan) -> None:
        path = self.plans_root / f"{plan.plan_id}.json"
        path.write_text(
            json.dumps(plan.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )

    def _require_plan(self, plan_id: str) -> SkillAdaptationPlan:
        path = self.plans_root / f"{plan_id}.json"
        if not path.is_file():
            raise SkillAdaptationNotFound("adaptation plan 不存在。")
        return SkillAdaptationPlan.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _save_materialization(self, item: SkillAdaptationMaterialization) -> None:
        path = self.materialized_root / f"{item.materialization_id}.json"
        path.write_text(
            json.dumps(item.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )


def raise_skill_adaptation_error(exc: SkillAdaptationError) -> None:
    raise ApiError(
        str(exc),
        status_code=exc.status_code,
        code=exc.code,
        details=exc.details,
        retryable=exc.retryable,
    ) from exc


__all__ = [
    "SkillAdaptationError",
    "SkillAdaptationService",
    "raise_skill_adaptation_error",
]
