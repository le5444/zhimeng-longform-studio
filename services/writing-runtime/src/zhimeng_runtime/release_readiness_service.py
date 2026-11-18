"""Runtime-facing Local v1 release readiness evaluation.

Surfaces the packaging release-readiness aggregator through the writing-runtime
API so Workbench ops can inspect fail-closed status. This endpoint never flips
local_v1_ready / local_v1_exe from incomplete evidence.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from zhimeng_runtime.state_roots import ensure_state_root


POLICY_VERSION = "local-v1-release-readiness-runtime-1"
LATEST_FILENAME = "local-v1-release-readiness.json"


class ReleaseReadinessError(RuntimeError):
    code = "release_readiness_error"
    retryable = False


class ReleaseReadinessStateError(ReleaseReadinessError):
    code = "release_readiness_state"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _repo_root() -> Path:
    # zhimeng_runtime -> src -> writing-runtime -> services -> repo root
    return Path(__file__).resolve().parents[4]


def _ensure_repo_on_path() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_evaluator() -> Callable[..., dict[str, Any]]:
    _ensure_repo_on_path()
    try:
        from desktop_packaging.portable.release_readiness import (
            evaluate_local_v1_release_readiness,
        )
    except ImportError as exc:  # pragma: no cover - exercised only when packaging tree is absent
        raise ReleaseReadinessStateError(
            "desktop_packaging.release_readiness is unavailable"
        ) from exc
    return evaluate_local_v1_release_readiness


def _force_invariants(response: dict[str, Any]) -> dict[str, Any]:
    receipt = dict(response.get("receipt") or {})
    receipt["formal_draft_allowed"] = False
    receipt["quality_assessed"] = False
    # Product policy for this Runtime surface: never claim exe from incomplete evidence.
    if not bool(receipt.get("local_v1_ready")):
        receipt["local_v1_exe"] = False
    else:
        # Even ready aggregation still requires real signed package evidence before exe claim.
        receipt["local_v1_exe"] = False
    receipt["runtime_surface"] = "writing-runtime-ops"
    receipt["runtime_policy_version"] = POLICY_VERSION
    not_claiming = list(response.get("not_claiming") or [])
    for code in (
        "local_v1_exe",
        "local_v1_complete",
        "formal_draft_authorization_unlock",
        "four_skills_generic_exactly_once",
        "nfr_release_gate_pass",
        "signed_windows_installer",
        "clean_vm_live_run",
    ):
        if code not in not_claiming:
            not_claiming.append(code)
    return {
        "schema": response.get("schema") or "zhimeng.local-v1-release-readiness-response.v1",
        "receipt": receipt,
        "not_claiming": not_claiming,
    }


class ReleaseReadinessService:
    """Evaluate and persist Local v1 release readiness for Runtime Ops."""

    def __init__(
        self,
        *,
        state_root: Path,
        package_path: Path | str | None = None,
        clean_vm_matrix_evidence: Path | str | dict[str, Any] | None = None,
    ) -> None:
        self.state_root = ensure_state_root(Path(state_root))
        self.package_path = Path(package_path) if package_path is not None else None
        self.clean_vm_matrix_evidence = clean_vm_matrix_evidence

    @property
    def latest_path(self) -> Path:
        return self.state_root / LATEST_FILENAME

    def evaluate(self) -> dict[str, Any]:
        evaluator = _load_evaluator()
        try:
            raw = evaluator(
                package_path=self.package_path,
                clean_vm_matrix_evidence=self.clean_vm_matrix_evidence,
                # Runtime never trusts client-provided completion flags.
                all_local_v1_fr_verified=False,
                all_local_v1_nfr_measured=False,
                ace2e_all_verified=False,
            )
        except Exception as exc:  # pragma: no cover - packaging internals
            raise ReleaseReadinessStateError(f"release readiness evaluation failed: {exc}") from exc

        response = _force_invariants(raw)
        receipt = response["receipt"]
        # Stamp evaluation wall time for the Runtime surface without rewriting hash identity
        # from packaging; packaging already set generated_at / receipt_hash.
        receipt.setdefault("evaluated_via", "runtime-api")
        receipt.setdefault("evaluated_at", _utcnow().isoformat())
        response["receipt"] = receipt

        self.latest_path.write_text(
            json.dumps(response, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return response

    def latest(self) -> dict[str, Any] | None:
        path = self.latest_path
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseReadinessStateError("latest release readiness receipt is unreadable") from exc
        if not isinstance(payload, dict):
            raise ReleaseReadinessStateError("latest release readiness receipt is invalid")
        return _force_invariants(payload)


__all__ = [
    "ReleaseReadinessError",
    "ReleaseReadinessService",
    "ReleaseReadinessStateError",
]
