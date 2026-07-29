from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import knowledge_source_fingerprint
from zhimeng_runtime.distillation_models import (
    DISTILLATION_PROFILES,
    DistillationCardType,
    DistillationCoverageRequest,
    DistillationCoverageResponse,
    DistillationHealthResponse,
    DistillationProfile,
    DistillationSelectedCard,
)


LIBRARY_FILES = ("manifest.json", "cards.jsonl", "feedback.jsonl")
COVERAGE_TOP = 50
MAX_TOOL_OUTPUT_BYTES = 4 * 1024 * 1024
CARD_TYPES = frozenset(
    {
        "work",
        "author",
        "genre",
        "scene",
        "voice",
        "trope",
        "platform",
        "packaging",
        "benchmark",
        "validator",
    }
)
HEALTH_VALUES = frozenset({"healthy", "degraded", "blocked"})
STRUCTURAL_HEALTH_VALUES = HEALTH_VALUES
VALIDATION_MATURITY_VALUES = frozenset({"validated", "risk_ready", "degraded"})
WRITING_GATE_VALUES = frozenset(
    {
        "proceed_with_distillation_context",
        "pause_and_request_benchmark_sources",
        "validate_existing_cards_or_risk_draft",
        "pause_and_request_sources",
        "proceed_with_risk_only",
        "ask_user_or_proceed_with_risk",
    }
)
NEXT_STEP_VALUES = frozenset(
    {
        "run_context_and_write",
        "validate_existing_cards_or_run_risk_context",
        "run_risk_context_only",
        "ask_user_for_same_type_sources",
        "ask_user_or_run_context_with_risk",
    }
)


class DistillationAdapterError(RuntimeError):
    code = "distillation_adapter_failed"
    status_code = 502
    retryable = True

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details


class DistillationConfigurationError(DistillationAdapterError):
    code = "distillation_unavailable"
    status_code = 503
    retryable = False


class DistillationLibraryInvalidError(DistillationAdapterError):
    code = "distillation_library_invalid"
    status_code = 503
    retryable = False


class DistillationLibraryChangedError(DistillationAdapterError):
    code = "distillation_library_changed"
    status_code = 409
    retryable = True


class DistillationToolChangedError(DistillationAdapterError):
    code = "distillation_tool_changed"
    status_code = 409
    retryable = True


class DistillationToolTimeoutError(DistillationAdapterError):
    code = "distillation_tool_timeout"
    status_code = 504
    retryable = True


class DistillationBusyError(DistillationAdapterError):
    code = "distillation_preview_busy"
    status_code = 409
    retryable = True


class DistillationToolExecutionError(DistillationAdapterError):
    code = "distillation_tool_failed"
    status_code = 502
    retryable = True


class DistillationOutputError(DistillationAdapterError):
    code = "distillation_output_invalid"
    status_code = 502
    retryable = False


class DistillationPreviewAdapter(Protocol):
    def health(self) -> DistillationHealthResponse: ...

    def observe_knowledge_fingerprint(self) -> str: ...

    def coverage(
        self,
        request: DistillationCoverageRequest,
    ) -> DistillationCoverageResponse: ...


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    sha256: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class InvocationResult:
    payload: dict[str, Any]
    library_fingerprint: str
    tool_fingerprint: str
    observed_at: datetime


class SubprocessDistillationAdapter:
    """Fixed-command, read-only adapter for the external distillation skill."""

    def __init__(
        self,
        *,
        skill_root: Path,
        library: Path,
        python_executable: Path,
        timeout_seconds: float,
    ) -> None:
        self._skill_root = _resolve_directory(
            skill_root,
            error_message="Distillation Skill root is unavailable",
        )
        self._library = _resolve_directory(
            library,
            error_message="Distillation library is unavailable",
        )
        self._python_executable = _resolve_regular_file(
            python_executable,
            error_message="Distillation Python executable is unavailable",
        )
        script = self._skill_root / "scripts" / "distill_kb.py"
        self._script = _resolve_regular_file(
            script,
            error_message="Distillation tool script is unavailable",
            trusted_root=self._skill_root,
            reject_symlink=True,
        )
        self._timeout_seconds = timeout_seconds
        self._invocation_lock = threading.Lock()
        self._health_cache: DistillationHealthResponse | None = None

    def health(self) -> DistillationHealthResponse:
        try:
            invocation = self._invoke("health")
        except DistillationBusyError:
            cached = self._current_health_cache()
            if cached is None:
                raise
            return cached.model_copy(deep=True)
        payload = invocation.payload
        try:
            response = DistillationHealthResponse(
                health=_enum_text(payload, "health", HEALTH_VALUES),
                structural_health=_enum_text(
                    payload,
                    "structural_health",
                    STRUCTURAL_HEALTH_VALUES,
                ),
                validation_maturity=_enum_text(
                    payload,
                    "validation_maturity",
                    VALIDATION_MATURITY_VALUES,
                ),
                card_count=_integer(payload, "cards"),
                counts=_card_count_map(payload, "counts"),
                validation_ladder_debt=_validation_ladder_debt(payload),
                hard_rule_eligible_counts=_card_count_map(
                    payload,
                    "hard_rule_eligible_counts",
                ),
                library_fingerprint=invocation.library_fingerprint,
                tool_fingerprint=invocation.tool_fingerprint,
                observed_at=invocation.observed_at,
            )
            self._health_cache = response.model_copy(deep=True)
            return response
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DistillationOutputError(
                "Distillation health output did not match the supported contract"
            ) from exc

    def observe_knowledge_fingerprint(self) -> str:
        """Read one stable library/tool version without invoking the selector."""

        if not self._invocation_lock.acquire(blocking=False):
            raise DistillationBusyError(
                "Another distillation preview is already running"
            )
        try:
            library_before = self._snapshot_library()
            tool_before = self._snapshot_tool()
            library_after = self._snapshot_library_after_invocation()
            tool_after = self._snapshot_tool_after_invocation()
            if library_before != library_after:
                raise DistillationLibraryChangedError(
                    "Distillation library changed while its fingerprint was observed"
                )
            if tool_before != tool_after:
                raise DistillationToolChangedError(
                    "Distillation tool changed while its fingerprint was observed"
                )
            return knowledge_source_fingerprint(
                library_fingerprint=_aggregate_fingerprint(library_after),
                tool_fingerprint=_aggregate_fingerprint(tool_after),
            )
        finally:
            self._invocation_lock.release()

    def _current_health_cache(self) -> DistillationHealthResponse | None:
        cached = self._health_cache
        if cached is None:
            return None
        library_fingerprint = _aggregate_fingerprint(self._snapshot_library())
        tool_fingerprint = _aggregate_fingerprint(self._snapshot_tool())
        if (
            cached.library_fingerprint != library_fingerprint
            or cached.tool_fingerprint != tool_fingerprint
        ):
            self._health_cache = None
            return None
        return cached

    def coverage(
        self,
        request: DistillationCoverageRequest,
    ) -> DistillationCoverageResponse:
        invocation = self._invoke(
            "coverage",
            query=request.query,
            profile=request.profile,
        )
        payload = invocation.payload
        try:
            output_query = _text(payload, "query", maximum=1000)
            output_profile = _enum_text(
                payload,
                "profile",
                DISTILLATION_PROFILES,
            )
            if output_query != request.query or output_profile != request.profile:
                raise ValueError("tool echoed a different request")
            selected_cards = _selected_cards(payload)
            target_health = _enum_text(payload, "target_health", HEALTH_VALUES)
            writing_gate = _enum_text(
                payload,
                "writing_gate",
                WRITING_GATE_VALUES,
            )
            recommended_next_step = _enum_text(
                payload,
                "recommended_next_step",
                NEXT_STEP_VALUES,
            )
            library_health = _mapping(payload, "library_health")
            structural_health = _enum_text(
                library_health,
                "structural_health",
                STRUCTURAL_HEALTH_VALUES,
            )
            if structural_health == "blocked" and target_health != "blocked":
                raise ValueError("blocked library was reported as usable")
            if any(not card.hard_rule_eligible for card in selected_cards):
                if target_health == "healthy":
                    raise ValueError("risk-ready cards were reported as healthy")
            return DistillationCoverageResponse(
                query=request.query,
                profile=request.profile,
                target_health=target_health,
                writing_gate=writing_gate,
                recommended_next_step=recommended_next_step,
                risk_preview_allowed=target_health != "blocked",
                formal_injection_allowed=(
                    target_health == "healthy"
                    and writing_gate == "proceed_with_distillation_context"
                ),
                source_coverage_ratio=_ratio(payload, "source_coverage_ratio"),
                formal_coverage_ratio=_ratio(payload, "formal_coverage_ratio"),
                matched_cards=_integer(payload, "matched_cards"),
                available_matched_cards=_integer(
                    payload,
                    "available_matched_cards",
                ),
                selected_type_counts=_card_count_map(
                    payload,
                    "selected_type_counts",
                ),
                selected_hard_rule_type_counts=_card_count_map(
                    payload,
                    "selected_hard_rule_type_counts",
                ),
                selected_risk_ready_type_counts=_card_count_map(
                    payload,
                    "selected_risk_ready_type_counts",
                ),
                source_quota_gaps=_card_count_map(payload, "source_quota_gaps"),
                formal_quota_gaps=_card_count_map(payload, "formal_quota_gaps"),
                missing_types=_card_type_list(payload, "missing_types"),
                formal_missing_types=_card_type_list(
                    payload,
                    "formal_missing_types",
                ),
                selected_cards=selected_cards,
                feed_request=_text(
                    payload,
                    "feed_request",
                    allow_empty=True,
                    maximum=2400,
                    truncate=True,
                ),
                library_fingerprint=invocation.library_fingerprint,
                tool_fingerprint=invocation.tool_fingerprint,
                observed_at=invocation.observed_at,
            )
        except DistillationAdapterError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise DistillationOutputError(
                "Distillation coverage output did not match the supported contract"
            ) from exc

    def _invoke(
        self,
        command: str,
        *,
        query: str | None = None,
        profile: DistillationProfile | None = None,
    ) -> InvocationResult:
        if not self._invocation_lock.acquire(blocking=False):
            raise DistillationBusyError(
                "Another distillation preview is already running"
            )
        try:
            return self._invoke_serialized(
                command,
                query=query,
                profile=profile,
            )
        finally:
            self._invocation_lock.release()

    def _invoke_serialized(
        self,
        command: str,
        *,
        query: str | None = None,
        profile: DistillationProfile | None = None,
    ) -> InvocationResult:
        if command == "health" and query is None and profile is None:
            arguments = [
                str(self._python_executable),
                str(self._script),
                "health",
                str(self._library),
                "--json",
            ]
        elif command == "coverage" and query is not None and profile is not None:
            arguments = [
                str(self._python_executable),
                str(self._script),
                "coverage",
                str(self._library),
                query,
                "--profile",
                profile,
                "--top",
                str(COVERAGE_TOP),
                "--json",
            ]
        else:
            raise DistillationConfigurationError(
                "Unsupported distillation command configuration"
            )

        library_before = self._snapshot_library()
        tool_before = self._snapshot_tool()
        pending_error: DistillationAdapterError | None = None
        completed: subprocess.CompletedProcess[bytes] | None = None
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        try:
            completed = subprocess.run(
                arguments,
                cwd=self._skill_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            pending_error = DistillationToolTimeoutError(
                "Distillation preview timed out",
                details={"timeout_seconds": self._timeout_seconds},
            )
        except OSError:
            pending_error = DistillationToolExecutionError(
                "Distillation preview process could not start"
            )

        library_after = self._snapshot_library_after_invocation()
        tool_after = self._snapshot_tool_after_invocation()
        if library_before != library_after:
            raise DistillationLibraryChangedError(
                "Distillation library changed while the read-only preview was running"
            )
        if tool_before != tool_after:
            raise DistillationToolChangedError(
                "Distillation tool changed while the preview was running"
            )
        if pending_error is not None:
            raise pending_error
        if completed is None:
            raise DistillationToolExecutionError("Distillation preview did not run")
        if completed.returncode != 0:
            raise DistillationToolExecutionError(
                "Distillation preview command failed",
                details={"exit_code": completed.returncode},
            )
        output = completed.stdout
        if not isinstance(output, bytes):
            output = str(output).encode("utf-8")
        if not output or len(output) > MAX_TOOL_OUTPUT_BYTES:
            raise DistillationOutputError(
                "Distillation preview returned an invalid output size"
            )
        try:
            decoded = output.decode("utf-8-sig", errors="strict")
            payload = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DistillationOutputError(
                "Distillation preview returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DistillationOutputError(
                "Distillation preview returned a non-object JSON payload"
            )
        return InvocationResult(
            payload=payload,
            library_fingerprint=_aggregate_fingerprint(library_after),
            tool_fingerprint=_aggregate_fingerprint(tool_after),
            observed_at=datetime.now(UTC),
        )

    def _snapshot_library(self) -> dict[str, FileSnapshot]:
        snapshots: dict[str, FileSnapshot] = {}
        for name in LIBRARY_FILES:
            target = self._library / name
            try:
                resolved = target.resolve(strict=True)
                resolved.relative_to(self._library)
            except (OSError, ValueError) as exc:
                raise DistillationLibraryInvalidError(
                    "Distillation library is missing a required state file"
                ) from exc
            if target.is_symlink() or not resolved.is_file():
                raise DistillationLibraryInvalidError(
                    "Distillation library state files must be regular local files"
                )
            snapshots[name] = _snapshot_file(
                resolved,
                changed_error=DistillationLibraryChangedError,
                changed_message="Distillation library changed while it was inspected",
            )
        return snapshots

    def _snapshot_library_after_invocation(self) -> dict[str, FileSnapshot]:
        try:
            return self._snapshot_library()
        except DistillationLibraryChangedError:
            raise
        except DistillationLibraryInvalidError as exc:
            raise DistillationLibraryChangedError(
                "Distillation library changed while the read-only preview was running"
            ) from exc

    def _snapshot_tool(self) -> dict[str, FileSnapshot]:
        if not self._script.is_file():
            raise DistillationConfigurationError(
                "Distillation tool script is unavailable"
            )
        return {
            "distill_kb.py": _snapshot_file(
                self._script,
                changed_error=DistillationToolChangedError,
                changed_message="Distillation tool changed while it was inspected",
            )
        }

    def _snapshot_tool_after_invocation(self) -> dict[str, FileSnapshot]:
        try:
            return self._snapshot_tool()
        except DistillationToolChangedError:
            raise
        except DistillationConfigurationError as exc:
            raise DistillationToolChangedError(
                "Distillation tool changed while the preview was running"
            ) from exc


def build_distillation_adapter(
    settings: RuntimeSettings,
) -> SubprocessDistillationAdapter | None:
    if (
        settings.distillation_skill_root is None
        or settings.distillation_library is None
    ):
        return None
    return SubprocessDistillationAdapter(
        skill_root=settings.distillation_skill_root,
        library=settings.distillation_library,
        python_executable=settings.distillation_python_executable,
        timeout_seconds=settings.distillation_timeout_seconds,
    )


def _resolve_directory(path: Path, *, error_message: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise DistillationConfigurationError(error_message) from exc
    if not resolved.is_dir():
        raise DistillationConfigurationError(error_message)
    return resolved


def _resolve_regular_file(
    path: Path,
    *,
    error_message: str,
    trusted_root: Path | None = None,
    reject_symlink: bool = False,
) -> Path:
    candidate = path.expanduser()
    try:
        resolved = candidate.resolve(strict=True)
        if trusted_root is not None:
            resolved.relative_to(trusted_root)
    except (OSError, ValueError) as exc:
        raise DistillationConfigurationError(error_message) from exc
    if not resolved.is_file() or (reject_symlink and candidate.is_symlink()):
        raise DistillationConfigurationError(error_message)
    return resolved


def _snapshot_file(
    path: Path,
    *,
    changed_error: type[DistillationAdapterError],
    changed_message: str,
) -> FileSnapshot:
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
    except OSError as exc:
        raise changed_error(changed_message) from exc
    before_identity = (before.st_size, before.st_mtime_ns)
    after_identity = (after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise changed_error(changed_message)
    return FileSnapshot(
        sha256=digest.hexdigest(),
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def _aggregate_fingerprint(snapshots: Mapping[str, FileSnapshot]) -> str:
    payload = [
        {
            "name": name,
            "sha256": snapshot.sha256,
            "size": snapshot.size,
            "mtime_ns": snapshot.mtime_ns,
        }
        for name, snapshot in sorted(snapshots.items())
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(payload: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = payload[field]
    if not isinstance(value, dict):
        raise TypeError(f"{field} must be an object")
    return value


def _text(
    payload: Mapping[str, Any],
    field: str,
    *,
    allow_empty: bool = False,
    maximum: int,
    truncate: bool = False,
) -> str:
    value = payload[field]
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > maximum:
        if not truncate:
            raise ValueError(f"{field} exceeds the supported size")
        normalized = normalized[:maximum]
    return normalized


def _enum_text(
    payload: Mapping[str, Any],
    field: str,
    allowed: set[str] | frozenset[str],
) -> str:
    value = _text(payload, field, maximum=100)
    if value not in allowed:
        raise ValueError(f"{field} is unsupported")
    return value


def _integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError(f"{field} must be a non-negative integer")
    return value


def _ratio(payload: Mapping[str, Any], field: str) -> float:
    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be numeric")
    ratio = float(value)
    if ratio < 0.0 or ratio > 1.0:
        raise ValueError(f"{field} must be between zero and one")
    return ratio


def _count_map(payload: Mapping[str, Any], field: str) -> dict[str, int]:
    raw = _mapping(payload, field)
    result: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise TypeError(f"{field} has an invalid key")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError(f"{field} has an invalid count")
        result[key] = value
    return result


def _card_count_map(
    payload: Mapping[str, Any],
    field: str,
) -> dict[DistillationCardType, int]:
    raw = _count_map(payload, field)
    if any(key not in CARD_TYPES for key in raw):
        raise ValueError(f"{field} contains an unsupported card type")
    return raw  # type: ignore[return-value]


def _validation_ladder_debt(payload: Mapping[str, Any]) -> dict[str, int]:
    raw = _count_map(payload, "validation_ladder_debt")
    return {
        "risk_ready_cards": raw.get("risk_ready_cards", 0),
        "warn_steps": raw.get("warn_steps", 0),
    }


def _card_type_list(
    payload: Mapping[str, Any],
    field: str,
) -> list[DistillationCardType]:
    raw = payload[field]
    if not isinstance(raw, list):
        raise TypeError(f"{field} must be an array")
    if len(raw) > len(CARD_TYPES):
        raise ValueError(f"{field} contains too many values")
    if any(not isinstance(item, str) or item not in CARD_TYPES for item in raw):
        raise ValueError(f"{field} contains an unsupported card type")
    if len(set(raw)) != len(raw):
        raise ValueError(f"{field} contains duplicate values")
    return raw  # type: ignore[return-value]


def _selected_cards(payload: Mapping[str, Any]) -> list[DistillationSelectedCard]:
    raw_cards = payload["selected_cards"]
    if not isinstance(raw_cards, list):
        raise TypeError("selected_cards must be an array")
    if len(raw_cards) > 24:
        raise ValueError("selected_cards exceeds the compact response limit")
    cards: list[DistillationSelectedCard] = []
    seen_ids: set[str] = set()
    for raw in raw_cards:
        if not isinstance(raw, dict):
            raise TypeError("selected_cards entries must be objects")
        card_id = _text(raw, "card_id", maximum=300)
        if card_id in seen_ids:
            raise ValueError("selected_cards contains duplicate card ids")
        seen_ids.add(card_id)
        hard_rule_eligible = raw.get("hard_rule_eligible")
        if not isinstance(hard_rule_eligible, bool):
            raise TypeError("hard_rule_eligible must be a boolean")
        usage_mode = _enum_text(
            raw,
            "usage_mode",
            {"hard_rule", "risk_ready"},
        )
        tags = raw.get("tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise TypeError("tags must be an array of strings")
        normalized_tags: list[str] = []
        for tag in tags[:24]:
            normalized = tag.strip()[:120]
            if normalized and normalized not in normalized_tags:
                normalized_tags.append(normalized)
        card_type = _enum_text(raw, "card_type", CARD_TYPES)
        safety_risk = raw.get("safety_risk")
        if isinstance(safety_risk, bool) or not isinstance(safety_risk, (int, float)):
            raise TypeError("safety_risk must be numeric")
        cards.append(
            DistillationSelectedCard(
                card_id=card_id,
                card_type=card_type,
                title=_text(raw, "title", maximum=500, truncate=True),
                summary=_text(
                    raw,
                    "summary",
                    allow_empty=True,
                    maximum=1200,
                    truncate=True,
                ),
                usage_mode=usage_mode,
                hard_rule_eligible=hard_rule_eligible,
                safety_risk=float(safety_risk),
                tags=normalized_tags,
            )
        )
    return cards
