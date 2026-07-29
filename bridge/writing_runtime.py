"""Thin Gateway facade for trusted-root novel runtime actions."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

try:
    from novel_runtime.adapter_preview import (
        ADAPTER_ID,
        MAX_TOTAL_SOURCE_BYTES,
        MAX_TOTAL_SOURCE_FILES,
        REQUEST_SCHEMA,
        AdapterPreviewError,
        execute_adapter_preview,
        failure_payload,
        source_tree_manifest,
        valid_chapter_id,
    )
    from novel_runtime.project_reader import NovelProjectReader, resolve_trusted_project_root
    from novel_runtime.store import PreviewLeaseBusyError, RuntimeStore
except ModuleNotFoundError:
    from bridge.novel_runtime.adapter_preview import (
        ADAPTER_ID,
        MAX_TOTAL_SOURCE_BYTES,
        MAX_TOTAL_SOURCE_FILES,
        REQUEST_SCHEMA,
        AdapterPreviewError,
        execute_adapter_preview,
        failure_payload,
        source_tree_manifest,
        valid_chapter_id,
    )
    from bridge.novel_runtime.project_reader import NovelProjectReader, resolve_trusted_project_root
    from bridge.novel_runtime.store import PreviewLeaseBusyError, RuntimeStore

try:
    from novel_runtime.risk_draft import (
        REQUEST_SCHEMA as RISK_DRAFT_REQUEST_SCHEMA,
        stage_risk_draft,
    )
except ModuleNotFoundError:
    from bridge.novel_runtime.risk_draft import (  # type: ignore[no-redef]
        REQUEST_SCHEMA as RISK_DRAFT_REQUEST_SCHEMA,
        stage_risk_draft,
    )
try:
    from novel_runtime.expand_draft import (
        EXPAND_REQUEST_SCHEMA,
        expand_risk_draft,
    )
    from novel_runtime.hard_review import (
        REVIEW_REQUEST_SCHEMA,
        run_hard_review,
    )
except ModuleNotFoundError:
    from bridge.novel_runtime.expand_draft import (  # type: ignore[no-redef]
        EXPAND_REQUEST_SCHEMA,
        expand_risk_draft,
    )
    from bridge.novel_runtime.hard_review import (  # type: ignore[no-redef]
        REVIEW_REQUEST_SCHEMA,
        run_hard_review,
    )




try:
    from novel_runtime.writeback import (
        DIFF_REQUEST_SCHEMA,
        WRITEBACK_REQUEST_SCHEMA,
        apply_writeback,
        create_author_diff_preview,
    )
except ModuleNotFoundError:
    from bridge.novel_runtime.writeback import (  # type: ignore[no-redef]
        DIFF_REQUEST_SCHEMA,
        WRITEBACK_REQUEST_SCHEMA,
        apply_writeback,
        create_author_diff_preview,
    )


try:
    from novel_runtime.post_writeback import (
        SYNC_REQUEST_SCHEMA,
        post_writeback_sync,
        scan_chapter_tree,
    )
except ModuleNotFoundError:
    from bridge.novel_runtime.post_writeback import (  # type: ignore[no-redef]
        SYNC_REQUEST_SCHEMA,
        post_writeback_sync,
        scan_chapter_tree,
    )


try:
    from novel_runtime.agent_revise import (
        REVISE_REQUEST_SCHEMA,
        agent_revise,
    )
except ModuleNotFoundError:
    from bridge.novel_runtime.agent_revise import (  # type: ignore[no-redef]
        REVISE_REQUEST_SCHEMA,
        agent_revise,
    )

WRITING_ACTIONS = {
    "writing_adapter_preview",
    "writing_agent_revise",
    "writing_author_diff_preview",
    "writing_hard_review",
    "writing_post_writeback_sync",
    "writing_project_bind",
    "writing_project_status",
    "writing_risk_draft_expand",
    "writing_risk_draft_stage",
    "writing_runtime_status",
    "writing_writeback_apply",
}

_PREVIEW_REQUEST_KEYS = {
    "schema",
    "adapter_id",
    "project_id",
    "binding_revision",
    "expected_project_fingerprint",
    "chapter_id",
    "stage",
}

_CONFIG_LOCK = threading.RLock()
_TRUSTED_ROOTS: tuple[Path, ...] = ()


def _path_values(values: Iterable[str | os.PathLike[str]] | None) -> List[str]:
    result = [str(value).strip() for value in (values or []) if str(value).strip()]
    env_value = os.environ.get("ZHIMENG_WRITING_ROOTS", "")
    if env_value:
        result.extend(item.strip() for item in env_value.split(os.pathsep) if item.strip())
    return result


def configure_writing_roots(values: Iterable[str | os.PathLike[str]] | None = None) -> Dict[str, Any]:
    configured: List[Path] = []
    rejected: List[str] = []
    seen = set()
    for raw in _path_values(values):
        path = Path(raw).expanduser().resolve()
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        if not path.exists() or not path.is_dir():
            rejected.append(str(path))
            continue
        configured.append(path)
    with _CONFIG_LOCK:
        global _TRUSTED_ROOTS
        _TRUSTED_ROOTS = tuple(configured)
    return {
        "mode": "trusted-writing-roots",
        "roots": [str(path) for path in configured],
        "rejected": rejected,
        "full_filesystem_access": False,
    }


def trusted_writing_roots() -> tuple[Path, ...]:
    with _CONFIG_LOCK:
        return tuple(_TRUSTED_ROOTS)


def _runtime_store(bridge_root: Path) -> RuntimeStore:
    return RuntimeStore(bridge_root / ".codex-runtime" / "writing" / "runtime.sqlite3")


def _inspection_fingerprint(
    inspection: Dict[str, Any],
    project_root: Path,
    kb_root: Path | None,
) -> str:
    resolved_project = project_root.resolve()
    roots = [("project", resolved_project)]
    inspected_project = inspection.get("project") if isinstance(inspection.get("project"), dict) else {}
    inspected_kb_root = str(inspected_project.get("kb_root") or "").strip()
    resolved_kb = kb_root.resolve() if kb_root is not None else None
    if resolved_kb is None and inspected_kb_root:
        resolved_kb = Path(inspected_kb_root).resolve(strict=True)
    if resolved_kb is not None:
        if resolved_kb != resolved_project:
            roots.append(("knowledge_base", resolved_kb))

    try:
        manifests = [source_tree_manifest(source, root) for source, root in roots]
    except AdapterPreviewError as error:
        raise ValueError(f"cannot fingerprint bound novel source: {error}") from error
    file_count = sum(int(item["file_count"]) for item in manifests)
    byte_count = sum(int(item["bytes"]) for item in manifests)
    if file_count > MAX_TOTAL_SOURCE_FILES or byte_count > MAX_TOTAL_SOURCE_BYTES:
        raise ValueError(
            "bound project and KB exceed the writing runtime fingerprint budget: "
            f"files={file_count}, bytes={byte_count}"
        )

    sections = inspection.get("sections") if isinstance(inspection.get("sections"), list) else []
    fingerprint_payload = {
        "schema": "zhimeng.writing-project-fingerprint.v3",
        "project_root": str(resolved_project),
        "knowledge_base_root": str(resolved_kb) if resolved_kb is not None else "",
        "sections": [
            {
                "index": item.get("index"),
                "directory_name": item.get("directory_name"),
                "present": bool(item.get("present")),
            }
            for item in sections
            if isinstance(item, dict)
        ],
        "source_trees": [
            {
                "role": item["role"],
                "file_count": item["file_count"],
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in manifests
        ],
    }
    content = json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _adapter_list(payload: Dict[str, Any]) -> List[str]:
    values = payload.get("adapters") if isinstance(payload.get("adapters"), list) else []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _resolve_optional_kb_root(payload: Dict[str, Any], roots: tuple[Path, ...]) -> Path | None:
    raw = str(payload.get("kb_root") or payload.get("knowledge_base_root") or "").strip()
    if not raw:
        return None
    return resolve_trusted_project_root(raw, roots)


def writing_runtime_status(bridge_root: Path) -> Dict[str, Any]:
    roots = trusted_writing_roots()
    store = _runtime_store(bridge_root)
    return {
        "schema": "zhimeng.writing-runtime-status.v1",
        "phase": "isolated_adapter_preview",
        "trusted_roots": [str(path) for path in roots],
        "full_filesystem_access": False,
        "database_path": str(store.database_path),
        "bindings": store.list_bindings(),
        "actions": sorted(WRITING_ACTIONS),
    }


def bind_writing_project(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    roots = trusted_writing_roots()
    if not roots:
        raise ValueError("No trusted writing roots configured. Start Gateway with --writing-root PATH.")
    raw_root = str(payload.get("root_path") or payload.get("path") or "").strip()
    if not raw_root:
        raise ValueError("writing_project_bind requires root_path")
    root = resolve_trusted_project_root(raw_root, roots)
    kb_root = _resolve_optional_kb_root(payload, roots)
    chapter_id = str(payload.get("chapter_id") or "").strip() or None
    reader = NovelProjectReader(roots)
    inspection = reader.inspect(root, kb_root=kb_root, chapter_id=chapter_id)
    fingerprint = _inspection_fingerprint(inspection, root, kb_root)
    project_record = inspection.get("project") if isinstance(inspection.get("project"), dict) else {}
    store = _runtime_store(bridge_root)
    binding = store.bind_project(
        root,
        kb_root=kb_root,
        display_name=str(project_record.get("name") or root.name),
        project_fingerprint=fingerprint,
        adapters=_adapter_list(payload),
    )
    event = store.append_event(
        binding["project_id"],
        "project_bound",
        str(inspection.get("status") or "unknown"),
        {
            "root_path": str(root),
            "kb_root": str(kb_root) if kb_root else "",
            "chapter_id": inspection.get("active_chapter_id"),
            "blocker_count": len(inspection.get("blockers") or []),
            "project_fingerprint": fingerprint,
        },
    )
    return {
        "schema": "zhimeng.writing-project-binding.v1",
        "project_id": binding["project_id"],
        "binding": binding,
        "project": inspection,
        "event": event,
    }


def writing_project_status(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    project_id = str(payload.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("writing_project_status requires project_id")
    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        raise KeyError(f"writing project binding not found: {project_id}")
    roots = trusted_writing_roots()
    root = resolve_trusted_project_root(binding["root_path"], roots)
    kb_root = _resolve_optional_kb_root({"kb_root": binding.get("kb_root")}, roots)
    chapter_id = str(payload.get("chapter_id") or "").strip() or None
    inspection = NovelProjectReader(roots).inspect(root, kb_root=kb_root, chapter_id=chapter_id)
    fingerprint = _inspection_fingerprint(inspection, root, kb_root)
    binding = store.refresh_project_fingerprint(project_id, fingerprint)
    events = store.recent_events(
        project_id,
        after_sequence=max(0, int(payload.get("after_sequence") or 0)),
        limit=max(1, min(int(payload.get("limit") or 100), 500)),
    )
    return {
        "schema": "zhimeng.writing-project-status.v1",
        "project_id": project_id,
        "binding": binding,
        "project": inspection,
        "events": events,
        "cursor": events[-1]["sequence"] if events else max(0, int(payload.get("after_sequence") or 0)),
    }


def _preview_failure(
    code: str,
    message: str,
    *,
    adapter_id: str = ADAPTER_ID,
    project_id: str = "",
    binding: Dict[str, Any] | None = None,
    details: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return failure_payload(
        code,
        message,
        adapter_id=adapter_id,
        project_id=project_id,
        binding_revision=int(binding["revision"]) if binding else None,
        project_fingerprint=str(binding["project_fingerprint"]) if binding else "",
        details=details,
    )


def writing_adapter_preview(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    unexpected = sorted(set(payload) - _PREVIEW_REQUEST_KEYS)
    missing = sorted(_PREVIEW_REQUEST_KEYS - set(payload))
    adapter_id = str(payload.get("adapter_id") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    if unexpected or missing:
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            "Adapter preview request must match the registered schema exactly",
            adapter_id=adapter_id or ADAPTER_ID,
            project_id=project_id,
            details={"unsupported_fields": unexpected, "missing_fields": missing},
        )
    if str(payload.get("schema") or "") != REQUEST_SCHEMA:
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            f"Adapter preview requires schema {REQUEST_SCHEMA}",
            adapter_id=adapter_id or ADAPTER_ID,
            project_id=project_id,
        )
    if adapter_id != ADAPTER_ID:
        return _preview_failure(
            "WRITING_ADAPTER_NOT_REGISTERED",
            f"Writing adapter is not registered: {adapter_id or '<empty>'}",
            adapter_id=adapter_id or ADAPTER_ID,
            project_id=project_id,
        )
    if not project_id:
        return _preview_failure("WRITING_ADAPTER_REQUEST_INVALID", "project_id is required")

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return _preview_failure(
            "WRITING_ADAPTER_BINDING_NOT_FOUND",
            f"Writing project binding not found: {project_id}",
            project_id=project_id,
        )
    required_adapters = {"novel-kb-manager", "novel-creation-suite"}
    if not required_adapters.issubset(set(binding.get("adapters") or [])):
        return _preview_failure(
            "WRITING_ADAPTER_NOT_REGISTERED",
            "Binding does not enable the NLCL and Creation Suite adapter pair",
            project_id=project_id,
            binding=binding,
        )

    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    stage = str(payload.get("stage") or "").strip()
    supplied_revision = payload.get("binding_revision")
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()
    if not valid_chapter_id(chapter_id) or stage != "draft":
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            "Preview requires chapter_id=CHnnn and stage=draft",
            project_id=project_id,
            binding=binding,
        )
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            "binding_revision must be an integer",
            project_id=project_id,
            binding=binding,
        )
    if len(expected_fingerprint) != 64 or any(char not in "0123456789abcdef" for char in expected_fingerprint):
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            "expected_project_fingerprint must be a 64-character SHA-256",
            project_id=project_id,
            binding=binding,
        )

    roots = trusted_writing_roots()
    try:
        project_root = resolve_trusted_project_root(binding["root_path"], roots)
        kb_root = _resolve_optional_kb_root({"kb_root": binding.get("kb_root")}, roots)
    except (OSError, ValueError) as error:
        return _preview_failure(
            "WRITING_ADAPTER_UNTRUSTED_PATH",
            str(error),
            project_id=project_id,
            binding=binding,
        )
    if kb_root is None:
        return _preview_failure(
            "WRITING_ADAPTER_REQUEST_INVALID",
            "Adapter preview requires a bound knowledge base root",
            project_id=project_id,
            binding=binding,
        )

    inspection = NovelProjectReader(roots).inspect(project_root, kb_root=kb_root, chapter_id=chapter_id)
    current_fingerprint = _inspection_fingerprint(inspection, project_root, kb_root)
    binding = store.refresh_project_fingerprint(project_id, current_fingerprint)
    if int(binding["revision"]) != supplied_revision:
        return _preview_failure(
            "WRITING_ADAPTER_BINDING_REVISION_MISMATCH",
            "Binding revision changed before preview",
            project_id=project_id,
            binding=binding,
            details={"expected_revision": supplied_revision, "current_revision": binding["revision"]},
        )
    if str(binding["project_fingerprint"]).casefold() != expected_fingerprint:
        return _preview_failure(
            "WRITING_ADAPTER_PREVIEW_STALE",
            "Project fingerprint changed before preview",
            project_id=project_id,
            binding=binding,
            details={"expected_fingerprint": expected_fingerprint, "current_fingerprint": binding["project_fingerprint"]},
        )

    generated = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"writing-{generated}-{uuid.uuid4().hex[:12]}"
    try:
        lease = store.acquire_preview_lease(project_id, run_id, ttl_seconds=1800)
    except PreviewLeaseBusyError as error:
        return _preview_failure(
            error.code,
            str(error),
            project_id=project_id,
            binding=binding,
            details={"holder": error.holder},
        )
    try:
        store.append_event(
            project_id,
            "preview_requested",
            "requested",
            {
                "adapter_id": adapter_id,
                "chapter_id": chapter_id,
                "binding_revision": supplied_revision,
                "lease_id": lease["lease_id"],
            },
            run_id=run_id,
        )
    except Exception:
        store.release_preview_lease(project_id, run_id)
        raise

    def emit_event(event_type: str, status: str, event_payload: Dict[str, Any]) -> None:
        store.append_event(project_id, event_type, status, event_payload, run_id=run_id)

    def prepublish_guard() -> None:
        try:
            latest_inspection = NovelProjectReader(roots).inspect(
                project_root,
                kb_root=kb_root,
                chapter_id=chapter_id,
            )
            latest_fingerprint = _inspection_fingerprint(latest_inspection, project_root, kb_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise AdapterPreviewError(
                "WRITING_ADAPTER_SOURCE_CHANGED",
                f"Cannot revalidate source before preview publication: {error}",
            ) from error
        if latest_fingerprint.casefold() != str(binding["project_fingerprint"]).casefold():
            raise AdapterPreviewError(
                "WRITING_ADAPTER_PREVIEW_STALE",
                "Project or knowledge base changed before preview publication",
                details={
                    "expected_fingerprint": binding["project_fingerprint"],
                    "current_fingerprint": latest_fingerprint,
                },
            )
        try:
            store.assert_preview_publish(
                project_id,
                run_id,
                int(binding["revision"]),
                str(binding["project_fingerprint"]),
            )
        except PreviewLeaseBusyError as error:
            raise AdapterPreviewError(
                error.code,
                str(error),
                details={"holder": error.holder},
            ) from error

    try:
        return execute_adapter_preview(
            project_id=project_id,
            binding_revision=int(binding["revision"]),
            project_fingerprint=str(binding["project_fingerprint"]),
            project_root=project_root,
            kb_root=kb_root,
            chapter_id=chapter_id,
            stage=stage,
            inspection_status=str(inspection.get("status") or "blocked"),
            runtime_root=bridge_root / ".codex-runtime" / "writing",
            run_id=run_id,
            event_sink=emit_event,
            prepublish_guard=prepublish_guard,
        )
    except AdapterPreviewError as error:
        emit_event("preview_failed", "failed", {"code": error.code, "message": str(error)})
        return _preview_failure(
            error.code,
            str(error),
            project_id=project_id,
            binding=binding,
            details=error.details,
        )
    except Exception as error:
        emit_event("preview_failed", "failed", {"code": "WRITING_ADAPTER_INTERNAL_ERROR", "message": str(error)})
        return _preview_failure(
            "WRITING_ADAPTER_INTERNAL_ERROR",
            str(error),
            project_id=project_id,
            binding=binding,
        )
    finally:
        try:
            store.release_preview_lease(project_id, run_id)
        except Exception as error:
            try:
                emit_event("preview_lease_release_failed", "failed", {"message": str(error)})
            except Exception:
                pass




def writing_risk_draft_stage(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    """Stage a risk draft candidate from a completed adapter preview run.

    Writes only under .codex-runtime/writing/staging/<draft_run_id>.
    Never writes novel project or knowledge base paths.
    """
    required = [
        "schema",
        "preview_run_id",
        "project_id",
        "chapter_id",
        "binding_revision",
        "expected_project_fingerprint",
    ]
    missing = [key for key in required if key not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != RISK_DRAFT_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REQUEST_INVALID",
            "message": f"schema must be {RISK_DRAFT_REQUEST_SCHEMA}",
        }

    project_id = str(payload.get("project_id") or "").strip()
    preview_run_id = str(payload.get("preview_run_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }

    supplied_revision = payload.get("binding_revision")
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REQUEST_INVALID",
            "message": "binding_revision must be an integer",
        }
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()
    if len(expected_fingerprint) != 64:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REQUEST_INVALID",
            "message": "expected_project_fingerprint must be 64-char sha256",
        }

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    if int(binding.get("revision") or -1) != supplied_revision:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_REVISION_MISMATCH",
            "message": "binding revision mismatch",
            "details": {
                "expected_revision": supplied_revision,
                "current_revision": binding.get("revision"),
            },
        }
    if str(binding.get("project_fingerprint") or "").casefold() != expected_fingerprint:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_STALE",
            "message": "project fingerprint mismatch",
            "details": {
                "expected_fingerprint": expected_fingerprint,
                "current_fingerprint": binding.get("project_fingerprint"),
            },
        }

    runtime_root = bridge_root / ".codex-runtime" / "writing"
    receipt_path = runtime_root / "runs" / preview_run_id / "execution_receipt.json"
    domain_gate = "risk_draft_only"
    adapter_state = "UNKNOWN"
    formal_from_preview = False
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        domain_gate = str(receipt.get("domain_gate") or domain_gate)
        adapter_state = str(receipt.get("adapter_state") or adapter_state)
        formal_from_preview = bool(receipt.get("formal_draft_allowed"))

    try:
        result = stage_risk_draft(
            runtime_root=runtime_root,
            preview_run_id=preview_run_id,
            project_id=project_id,
            chapter_id=chapter_id,
            binding_revision=supplied_revision,
            project_fingerprint=str(binding.get("project_fingerprint") or ""),
            domain_gate=domain_gate,
            adapter_state=adapter_state,
            formal_draft_allowed_from_preview=formal_from_preview,
        )
    except FileNotFoundError as error:
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_PREVIEW_MISSING",
            "message": str(error),
        }
    except Exception as error:  # noqa: BLE001 - surface to gateway
        return {
            "schema": "zhimeng.writing-risk-draft.v1",
            "status": "error",
            "code": "WRITING_RISK_DRAFT_INTERNAL_ERROR",
            "message": str(error),
        }

    store.append_event(
        project_id,
        "risk_draft_staged",
        str(result.get("status") or "ok"),
        {
            "draft_run_id": result.get("draft_run_id"),
            "preview_run_id": preview_run_id,
            "chapter_id": chapter_id,
            "risk_code": result.get("risk_code"),
            "promotion_allowed": result.get("promotion_allowed"),
            "staging_root": result.get("staging_root"),
        },
        run_id=str(result.get("draft_run_id") or ""),
    )
    return result




def writing_risk_draft_expand(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = [
        "schema",
        "skeleton_draft_run_id",
        "project_id",
        "chapter_id",
        "binding_revision",
        "expected_project_fingerprint",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != EXPAND_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REQUEST_INVALID",
            "message": f"schema must be {EXPAND_REQUEST_SCHEMA}",
        }

    project_id = str(payload.get("project_id") or "").strip()
    skeleton_id = str(payload.get("skeleton_draft_run_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }
    supplied_revision = payload.get("binding_revision")
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REQUEST_INVALID",
            "message": "binding_revision must be an integer",
        }
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()
    if len(expected_fingerprint) != 64:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REQUEST_INVALID",
            "message": "expected_project_fingerprint must be 64-char sha256",
        }

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    if int(binding.get("revision") or -1) != supplied_revision:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_REVISION_MISMATCH",
            "message": "binding revision mismatch",
        }
    if str(binding.get("project_fingerprint") or "").casefold() != expected_fingerprint:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_STALE",
            "message": "project fingerprint mismatch",
        }

    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = expand_risk_draft(
            runtime_root=runtime_root,
            skeleton_draft_run_id=skeleton_id,
            project_id=project_id,
            chapter_id=chapter_id,
            binding_revision=supplied_revision,
            project_fingerprint=str(binding.get("project_fingerprint") or ""),
        )
    except FileNotFoundError as error:
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_MISSING",
            "message": str(error),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-risk-draft-expand.v1",
            "status": "error",
            "code": "WRITING_RISK_EXPAND_INTERNAL_ERROR",
            "message": str(error),
        }

    store.append_event(
        project_id,
        "risk_draft_expanded",
        str(result.get("status") or "ok"),
        {
            "expand_run_id": result.get("expand_run_id"),
            "skeleton_draft_run_id": skeleton_id,
            "chapter_id": chapter_id,
            "word_count": result.get("word_count"),
            "risk_code": result.get("risk_code"),
            "staging_root": result.get("staging_root"),
        },
        run_id=str(result.get("expand_run_id") or ""),
    )
    return result


def writing_hard_review(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = ["schema", "expand_run_id", "project_id", "chapter_id"]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != REVIEW_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_REQUEST_INVALID",
            "message": f"schema must be {REVIEW_REQUEST_SCHEMA}",
        }
    project_id = str(payload.get("project_id") or "").strip()
    expand_run_id = str(payload.get("expand_run_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }
    store = _runtime_store(bridge_root)
    if store.get_binding(project_id) is None:
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = run_hard_review(
            runtime_root=runtime_root,
            expand_run_id=expand_run_id,
            chapter_id=chapter_id,
            project_id=project_id,
        )
    except FileNotFoundError as error:
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_MISSING",
            "message": str(error),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-hard-review.v1",
            "status": "error",
            "code": "WRITING_HARD_REVIEW_INTERNAL_ERROR",
            "message": str(error),
        }
    store.append_event(
        project_id,
        "hard_review_completed",
        str(result.get("status") or "fail"),
        {
            "review_id": result.get("review_id"),
            "expand_run_id": expand_run_id,
            "chapter_id": chapter_id,
            "author_review_allowed": result.get("author_review_allowed"),
            "word_count": result.get("word_count"),
        },
        run_id=str(result.get("review_id") or ""),
    )
    return result




def writing_author_diff_preview(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = [
        "schema",
        "expand_run_id",
        "project_id",
        "chapter_id",
        "binding_revision",
        "expected_project_fingerprint",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != DIFF_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REQUEST_INVALID",
            "message": f"schema must be {DIFF_REQUEST_SCHEMA}",
        }

    project_id = str(payload.get("project_id") or "").strip()
    expand_run_id = str(payload.get("expand_run_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }
    supplied_revision = payload.get("binding_revision")
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REQUEST_INVALID",
            "message": "binding_revision must be an integer",
        }
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()
    if len(expected_fingerprint) != 64:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REQUEST_INVALID",
            "message": "expected_project_fingerprint must be 64-char sha256",
        }

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    if int(binding.get("revision") or -1) != supplied_revision:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_REVISION_MISMATCH",
            "message": "binding revision mismatch",
        }
    if str(binding.get("project_fingerprint") or "").casefold() != expected_fingerprint:
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_STALE",
            "message": "project fingerprint mismatch",
        }

    roots = trusted_writing_roots()
    try:
        project_root = resolve_trusted_project_root(binding["root_path"], roots)
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_UNTRUSTED_PATH",
            "message": str(error),
        }

    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = create_author_diff_preview(
            runtime_root=runtime_root,
            expand_run_id=expand_run_id,
            project_id=project_id,
            project_root=project_root,
            chapter_id=chapter_id,
            binding_revision=supplied_revision,
            project_fingerprint=str(binding.get("project_fingerprint") or ""),
        )
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-author-diff.v1",
            "status": "error",
            "code": "WRITING_DIFF_INTERNAL_ERROR",
            "message": str(error),
        }

    store.append_event(
        project_id,
        "author_diff_previewed",
        "ok",
        {
            "preview_id": result.get("preview_id"),
            "expand_run_id": expand_run_id,
            "chapter_id": chapter_id,
            "target_path": result.get("target_path"),
            "word_count": result.get("word_count"),
        },
        run_id=str(result.get("preview_id") or ""),
    )
    return result


def writing_writeback_apply(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = [
        "schema",
        "expand_run_id",
        "project_id",
        "chapter_id",
        "binding_revision",
        "expected_project_fingerprint",
        "approval_token",
        "expected_after_sha256",
        "confirm_write_source",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != WRITEBACK_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_REQUEST_INVALID",
            "message": f"schema must be {WRITEBACK_REQUEST_SCHEMA}",
        }
    if payload.get("confirm_write_source") is not True:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_NOT_CONFIRMED",
            "message": "confirm_write_source must be true",
        }

    project_id = str(payload.get("project_id") or "").strip()
    expand_run_id = str(payload.get("expand_run_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    approval_token = str(payload.get("approval_token") or "").strip()
    expected_after_sha256 = str(payload.get("expected_after_sha256") or "").strip()
    supplied_revision = payload.get("binding_revision")
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_REQUEST_INVALID",
            "message": "binding_revision must be an integer",
        }
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    if int(binding.get("revision") or -1) != supplied_revision:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_REVISION_MISMATCH",
            "message": "binding revision mismatch",
        }
    if str(binding.get("project_fingerprint") or "").casefold() != expected_fingerprint:
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_STALE",
            "message": "project fingerprint mismatch",
        }

    roots = trusted_writing_roots()
    try:
        project_root = resolve_trusted_project_root(binding["root_path"], roots)
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_UNTRUSTED_PATH",
            "message": str(error),
        }

    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = apply_writeback(
            runtime_root=runtime_root,
            expand_run_id=expand_run_id,
            project_id=project_id,
            project_root=project_root,
            chapter_id=chapter_id,
            binding_revision=supplied_revision,
            project_fingerprint=str(binding.get("project_fingerprint") or ""),
            approval_token=approval_token,
            expected_after_sha256=expected_after_sha256,
        )
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-writeback.v1",
            "status": "error",
            "code": "WRITING_WRITEBACK_FAILED",
            "message": str(error),
        }

    store.append_event(
        project_id,
        "writeback_applied",
        "ok",
        {
            "expand_run_id": expand_run_id,
            "chapter_id": chapter_id,
            "written_path": result.get("written_path"),
            "written_sha256": result.get("written_sha256"),
            "backup_path": result.get("backup_path"),
        },
        run_id=str(expand_run_id or ""),
    )
    return result




def writing_post_writeback_sync(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = ["schema", "project_id", "chapter_id"]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != SYNC_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_REQUEST_INVALID",
            "message": f"schema must be {SYNC_REQUEST_SCHEMA}",
        }
    project_id = str(payload.get("project_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    expand_run_id = str(payload.get("expand_run_id") or "").strip()
    written_path = str(payload.get("written_path") or "").strip()
    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }
    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    roots = trusted_writing_roots()
    try:
        project_root = resolve_trusted_project_root(binding["root_path"], roots)
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_UNTRUSTED_PATH",
            "message": str(error),
        }
    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = post_writeback_sync(
            runtime_root=runtime_root,
            project_root=project_root,
            project_id=project_id,
            chapter_id=chapter_id,
            expand_run_id=expand_run_id,
            written_path=written_path,
        )
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-post-writeback-sync.v1",
            "status": "error",
            "code": "WRITING_SYNC_FAILED",
            "message": str(error),
        }
    store.append_event(
        project_id,
        "post_writeback_synced",
        "ok",
        {
            "sync_id": result.get("sync_id"),
            "completed_chapter_id": chapter_id,
            "next_chapter_id": result.get("next_chapter_id"),
            "runtime_sync_dir": result.get("runtime_sync_dir"),
        },
        run_id=str(result.get("sync_id") or ""),
    )
    return result




def writing_agent_revise(payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    required = [
        "schema",
        "project_id",
        "chapter_id",
        "instruction",
        "binding_revision",
        "expected_project_fingerprint",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REQUEST_INVALID",
            "message": "missing fields: " + ",".join(missing),
        }
    if str(payload.get("schema") or "") != REVISE_REQUEST_SCHEMA:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REQUEST_INVALID",
            "message": f"schema must be {REVISE_REQUEST_SCHEMA}",
        }

    project_id = str(payload.get("project_id") or "").strip()
    chapter_id = str(payload.get("chapter_id") or "").strip().upper()
    instruction = str(payload.get("instruction") or "").strip()
    source_path_raw = str(payload.get("source_path") or "").strip()
    supplied_revision = payload.get("binding_revision")
    expected_fingerprint = str(payload.get("expected_project_fingerprint") or "").strip().casefold()

    if not valid_chapter_id(chapter_id):
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REQUEST_INVALID",
            "message": "chapter_id must look like CHnnn",
        }
    if not isinstance(supplied_revision, int) or isinstance(supplied_revision, bool):
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REQUEST_INVALID",
            "message": "binding_revision must be an integer",
        }
    if len(expected_fingerprint) != 64:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REQUEST_INVALID",
            "message": "expected_project_fingerprint must be 64-char sha256",
        }

    store = _runtime_store(bridge_root)
    binding = store.get_binding(project_id)
    if binding is None:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_BINDING_NOT_FOUND",
            "message": f"binding not found: {project_id}",
        }
    if int(binding.get("revision") or -1) != supplied_revision:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_REVISION_MISMATCH",
            "message": "binding revision mismatch",
        }
    if str(binding.get("project_fingerprint") or "").casefold() != expected_fingerprint:
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_STALE",
            "message": "project fingerprint mismatch",
        }

    roots = trusted_writing_roots()
    try:
        project_root = resolve_trusted_project_root(binding["root_path"], roots)
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_UNTRUSTED_PATH",
            "message": str(error),
        }

    if source_path_raw:
        source_path = Path(source_path_raw).expanduser().resolve()
        try:
            source_path.relative_to(project_root.resolve())
        except Exception:
            return {
                "schema": "zhimeng.writing-agent-revise.v1",
                "status": "error",
                "code": "WRITING_REVISE_SOURCE_ESCAPE",
                "message": "source_path must stay inside project root",
            }
    else:
        # default manuscript path convention
        source_path = project_root / "05_正文内容" / "第一卷" / chapter_id / f"{chapter_id}_定稿.md"
        if not source_path.exists():
            # fallback search
            hits = list(project_root.rglob(f"{chapter_id}_定稿.md"))
            if not hits:
                return {
                    "schema": "zhimeng.writing-agent-revise.v1",
                    "status": "error",
                    "code": "WRITING_REVISE_SOURCE_MISSING",
                    "message": f"manuscript not found for {chapter_id}",
                }
            source_path = hits[0]

    runtime_root = bridge_root / ".codex-runtime" / "writing"
    try:
        result = agent_revise(
            runtime_root=runtime_root,
            project_id=project_id,
            chapter_id=chapter_id,
            source_path=source_path,
            instruction=instruction,
            binding_revision=supplied_revision,
            project_fingerprint=str(binding.get("project_fingerprint") or ""),
        )
    except Exception as error:  # noqa: BLE001
        return {
            "schema": "zhimeng.writing-agent-revise.v1",
            "status": "error",
            "code": "WRITING_REVISE_FAILED",
            "message": str(error),
        }

    store.append_event(
        project_id,
        "agent_revised",
        str(result.get("status") or "ok"),
        {
            "revise_run_id": result.get("revise_run_id"),
            "chapter_id": chapter_id,
            "instruction": instruction[:200],
            "ops": result.get("ops"),
            "changed": result.get("changed"),
        },
        run_id=str(result.get("revise_run_id") or ""),
    )
    return result


def dispatch_writing_action(action: str, payload: Dict[str, Any], bridge_root: Path) -> Dict[str, Any]:
    if action == "writing_runtime_status":
        return writing_runtime_status(bridge_root)
    if action == "writing_project_bind":
        return bind_writing_project(payload, bridge_root)
    if action == "writing_project_status":
        return writing_project_status(payload, bridge_root)
    if action == "writing_adapter_preview":
        return writing_adapter_preview(payload, bridge_root)
    if action == "writing_risk_draft_stage":
        return writing_risk_draft_stage(payload, bridge_root)
    if action == "writing_risk_draft_expand":
        return writing_risk_draft_expand(payload, bridge_root)
    if action == "writing_hard_review":
        return writing_hard_review(payload, bridge_root)
    if action == "writing_author_diff_preview":
        return writing_author_diff_preview(payload, bridge_root)
    if action == "writing_writeback_apply":
        return writing_writeback_apply(payload, bridge_root)
    if action == "writing_post_writeback_sync":
        return writing_post_writeback_sync(payload, bridge_root)
    if action == "writing_agent_revise":
        return writing_agent_revise(payload, bridge_root)
    raise ValueError(f"Unsupported writing runtime action: {action}")
