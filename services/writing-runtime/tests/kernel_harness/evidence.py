from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from kernel_harness.models import EvidenceKind


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _location(
    evidence_root: Path,
    relative_path: str,
    *,
    json_pointer: str | None = None,
    expected_fields: Mapping[str, object] | None = None,
) -> dict[str, object]:
    path = evidence_root / relative_path
    if not path.is_file():
        raise ValueError(f"logical evidence target does not exist: {relative_path}")
    result: dict[str, object] = {
        "path": relative_path,
        "sha256": _file_hash(path),
        "size_bytes": path.stat().st_size,
    }
    if json_pointer is not None:
        pointed = _resolve_json_pointer_file(path, json_pointer)
        if expected_fields is not None:
            if not isinstance(pointed, Mapping) or any(
                pointed.get(key) != value for key, value in expected_fields.items()
            ):
                raise ValueError(
                    f"logical evidence pointer has unexpected content: {json_pointer}"
                )
        result["json_pointer"] = json_pointer
    return result


def _decode_pointer_token(token: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(token):
        if token[index] != "~":
            decoded.append(token[index])
            index += 1
            continue
        if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
            raise ValueError("logical evidence contains an invalid JSON pointer escape")
        decoded.append("~" if token[index + 1] == "0" else "/")
        index += 2
    return "".join(decoded)


def _resolve_json_pointer(value: object, pointer: str) -> object:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("logical evidence JSON pointer must start with a slash")
    current = value
    for raw_token in pointer[1:].split("/"):
        token = _decode_pointer_token(raw_token)
        if isinstance(current, Mapping):
            if token not in current:
                raise ValueError(f"logical evidence JSON pointer is missing: {pointer}")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (token.startswith("0") and token != "0"):
                raise ValueError(f"logical evidence JSON pointer is invalid: {pointer}")
            index = int(token)
            if index >= len(current):
                raise ValueError(f"logical evidence JSON pointer is missing: {pointer}")
            current = current[index]
        else:
            raise ValueError(f"logical evidence JSON pointer is missing: {pointer}")
    return current


def _resolve_json_pointer_file(path: Path, pointer: str) -> object:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"logical evidence target is not valid JSON: {path.name}") from exc
    return _resolve_json_pointer(payload, pointer)


def validate_evidence_index(evidence_root: Path, index: Mapping[str, object]) -> None:
    raw_entries = index.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("evidence index entries must be a list")
    seen_uris: set[str] = set()
    root = evidence_root.resolve(strict=True)
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, Mapping):
            raise ValueError("evidence index entry must be an object")
        uri = raw_entry.get("uri")
        kind = raw_entry.get("kind")
        locations = raw_entry.get("locations")
        if not isinstance(uri, str) or uri in seen_uris or not isinstance(locations, list):
            raise ValueError("evidence index entry identity is invalid")
        seen_uris.add(uri)
        for location in locations:
            if not isinstance(location, Mapping) or not isinstance(location.get("path"), str):
                raise ValueError("evidence index location is invalid")
            relative = Path(location["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("evidence index location escapes its root")
            path = (root / relative).resolve(strict=True)
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError("evidence index location escapes its root") from exc
            if not path.is_file():
                raise ValueError("evidence index location is not a file")
            if _file_hash(path) != location.get("sha256"):
                raise ValueError(f"evidence index hash mismatch: {uri}")
            if path.stat().st_size != location.get("size_bytes"):
                raise ValueError(f"evidence index size mismatch: {uri}")
            pointer = location.get("json_pointer")
            if pointer is not None:
                if not isinstance(pointer, str):
                    raise ValueError("evidence index JSON pointer is invalid")
                pointed = _resolve_json_pointer_file(path, pointer)
                if kind == "transport_check" and (
                    not isinstance(pointed, Mapping)
                    or pointed.get("evidence_ref") != uri
                ):
                    raise ValueError(f"transport check evidence ref mismatch: {uri}")


def build_evidence_index(
    evidence_root: Path,
    *,
    adapter_name: str,
    adapter_id: str,
    catalog: Mapping[str, EvidenceKind],
    probe_evidence_refs: Sequence[str],
    receipt_evidence_refs: Sequence[str],
) -> dict[str, object]:
    """Bind every logical evidence URI to content-addressed files on disk."""

    transcript_name = (
        "native-events.ndjson" if adapter_name == "codex" else "http-transcript.ndjson"
    )
    process_logs = (
        ["stderr.log"]
        if adapter_name == "codex"
        else ["stdout.log", "stderr.log"]
    )
    entries: dict[str, dict[str, object]] = {}

    for uri, kind in catalog.items():
        suffix = uri.rsplit("/", 1)[-1]
        if kind is EvidenceKind.PROTOCOL_TRANSCRIPT:
            locations = [_location(evidence_root, transcript_name)]
        elif kind is EvidenceKind.PROCESS_LIFECYCLE:
            locations = [
                _location(evidence_root, path)
                for path in process_logs
            ]
            locations.append(
                _location(
                    evidence_root,
                    "transport-probe.json",
                    json_pointer="/adapter",
                )
            )
            locations.append(
                _location(
                    evidence_root,
                    "cleanup-report.json",
                    json_pointer="/root_process_close_status",
                )
            )
        elif kind is EvidenceKind.CAPABILITY_SNAPSHOT:
            locations = [
                _location(
                    evidence_root,
                    "transport-probe.json",
                    json_pointer="/capabilities",
                )
            ]
        elif kind is EvidenceKind.WORKSPACE_SNAPSHOT:
            pointer = "/project_before" if suffix == "workspace-before" else "/project_after"
            locations = [
                _location(
                    evidence_root,
                    "workspace-snapshots.json",
                    json_pointer=pointer,
                )
            ]
        elif kind is EvidenceKind.CLEANUP_REPORT:
            locations = [_location(evidence_root, "cleanup-report.json")]
        elif kind is EvidenceKind.ISOLATION_REPORT:
            locations = [_location(evidence_root, "isolation-report.json")]
        else:
            raise ValueError(f"unsupported transport evidence kind: {kind.value}")
        entries[uri] = {"kind": kind.value, "locations": locations}

    for index, uri in enumerate(probe_evidence_refs):
        if uri in entries:
            continue
        locations = [
            _location(
                evidence_root,
                "transport-probe.json",
                json_pointer=f"/checks/{index}",
                expected_fields={"evidence_ref": uri},
            ),
            _location(evidence_root, transcript_name),
        ]
        entries[uri] = {"kind": "transport_check", "locations": locations}

    required_refs = sorted(set(probe_evidence_refs) | set(receipt_evidence_refs))
    missing = [uri for uri in required_refs if uri not in entries]
    if missing:
        raise ValueError(f"logical evidence URI is not bound: {', '.join(missing)}")

    result = {
        "schema": "zhimeng.kernel-harness-evidence-index.v1",
        "adapter": adapter_id,
        "entries": [
            {"uri": uri, **entries[uri]}
            for uri in sorted(entries)
        ],
        "receipt_refs": sorted(set(receipt_evidence_refs)),
        "probe_refs": sorted(set(probe_evidence_refs)),
    }
    validate_evidence_index(evidence_root, result)
    return result
