from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    LocalFileSourceCandidate,
    PreparedLocalSourcePacket,
    SourceArtifact,
    local_source_locator_hash,
)
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportAdapterExecutionIdentity,
    SourceImportToolEvidence,
    canonical_json_hash,
)


EXPECTED_SNAPSHOT_HASHES = {
    "prepare_downloaded_sources.ps1": (
        "bdf03a80843834407591ca398121fb882469a91362ca2c90fe76ad8d648c81cf"
    ),
    "extract_epub_text.py": (
        "0c7224fa3087b3e9f25fba00b9341d74ae1dc8f398e0a29851db0b5604894f99"
    ),
    "distill_source_packet.py": (
        "71068828f516d28eac081e1c64988e3541c394adcc0fbdf955f5a99e4ef9c132"
    ),
}


class LocalSourceAdapterError(RuntimeError):
    pass


class LocalSourceAdapterCancelled(LocalSourceAdapterError):
    pass


ProgressCallback = Callable[[dict[str, object]], None]


class LocalSourceAdapter(Protocol):
    def execution_identity(self) -> SourceImportAdapterExecutionIdentity: ...

    def inspect(self, source_path: str) -> LocalFileSourceCandidate: ...

    def prepare(
        self,
        candidate: LocalFileSourceCandidate,
        workspace: Path,
        cancel_event: threading.Event,
        on_progress: ProgressCallback,
    ) -> PreparedLocalSourcePacket: ...

    def draft(
        self,
        source: PreparedLocalSourcePacket,
        workspace: Path,
        cancel_event: threading.Event,
    ) -> DistillationDraft: ...

    def cleanup_failed_workspace(self, workspace: Path) -> tuple[str, str]: ...


class ManagedLocalSourceAdapter:
    def __init__(
        self,
        *,
        snapshot_root: Path,
        state_root: Path,
        trusted_source_roots: tuple[Path, ...],
        powershell_executable: str = "powershell.exe",
    ) -> None:
        self.snapshot_root = snapshot_root.resolve(strict=True)
        self.scripts_root = (self.snapshot_root / "scripts").resolve(strict=True)
        self.state_root = state_root.resolve(strict=False)
        self.trusted_source_roots = tuple(
            root.resolve(strict=True) for root in trusted_source_roots
        )
        if not self.trusted_source_roots:
            raise LocalSourceAdapterError("Local source import requires a trusted root")
        self.powershell_executable = powershell_executable.strip()
        if not self.powershell_executable:
            raise LocalSourceAdapterError("PowerShell executable must not be empty")
        self.prepare_script = self.scripts_root / "prepare_downloaded_sources.ps1"
        self.extractor_script = self.scripts_root / "extract_epub_text.py"
        self.distill_script = self.scripts_root / "distill_source_packet.py"
        self._assert_managed_snapshot()
        self.state_root.mkdir(parents=True, exist_ok=True)

    def execution_identity(self) -> SourceImportAdapterExecutionIdentity:
        adapter_source = Path(__file__).resolve(strict=True)
        prepare_tool_id = "zhimeng.local-source-prepare"
        distillation_tool_id = "zhimeng.local-source-distillation-draft"
        return SourceImportAdapterExecutionIdentity(
            prepare_tool=SourceImportToolEvidence(
                tool_id=prepare_tool_id,
                tool_version="1",
                tool_fingerprint=_tool_fingerprint(
                    prepare_tool_id,
                    "1",
                    (
                        ("runtime_adapter", adapter_source),
                        ("prepare_script", self.prepare_script),
                        ("epub_extractor", self.extractor_script),
                    ),
                ),
            ),
            distillation_tool=SourceImportToolEvidence(
                tool_id=distillation_tool_id,
                tool_version="1",
                tool_fingerprint=_tool_fingerprint(
                    distillation_tool_id,
                    "1",
                    (
                        ("runtime_adapter", adapter_source),
                        ("distillation_script", self.distill_script),
                    ),
                ),
            ),
        )

    def inspect(self, source_path: str) -> LocalFileSourceCandidate:
        path = self._trusted_source_path(source_path)
        suffix = path.suffix.lower()
        if suffix not in {".txt", ".epub"}:
            raise LocalSourceAdapterError("Local source import accepts TXT or EPUB")
        size_bytes = path.stat().st_size
        if size_bytes < 1:
            raise LocalSourceAdapterError("Local source file is empty")
        if suffix == ".epub":
            self._preflight_epub(path)
        source_sha256 = _sha256_file(path)
        if path.stat().st_size != size_bytes:
            raise LocalSourceAdapterError("Local source size changed during inspection")
        return LocalFileSourceCandidate(
            source_path=str(path),
            source_format=suffix.removeprefix("."),
            title=path.stem,
            author="unknown",
            source_size_bytes=size_bytes,
            source_sha256=source_sha256,
            source_locator_hash=local_source_locator_hash(str(path)),
        )

    def prepare(
        self,
        candidate: LocalFileSourceCandidate,
        workspace: Path,
        cancel_event: threading.Event,
        on_progress: ProgressCallback,
    ) -> PreparedLocalSourcePacket:
        workspace = self._workspace(workspace)
        source_path = self._trusted_source_path(candidate.source_path)
        self._assert_candidate_snapshot(candidate, source_path)
        if cancel_event.is_set():
            raise LocalSourceAdapterCancelled("Local source import was cancelled")
        workspace.mkdir(parents=True, exist_ok=True)
        library_dir = workspace / "source-library"
        on_progress(
            {
                "stage": "preparing",
                "message": f"Preparing local {candidate.source_format.upper()} source",
                "percent": 10,
            }
        )
        process = subprocess.Popen(
            [
                self.powershell_executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.prepare_script),
                "-SourcePaths",
                str(source_path),
                "-LibraryDir",
                str(library_dir),
            ],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, stderr = self._wait(process, cancel_event, "source preparation")
        envelope = _decode_envelope(stdout)
        if process.returncode != 0 or envelope.get("ok") is not True:
            message = envelope.get("error_message") or stderr.strip()
            raise LocalSourceAdapterError(str(message or "Local source preparation failed"))
        if (
            envelope.get("status") != "success"
            or envelope.get("readiness") is not True
            or envelope.get("orphan_risk") is not False
        ):
            raise LocalSourceAdapterError("Local source preparation did not reach a clean success")
        items = envelope.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise LocalSourceAdapterError("Local source preparation returned an invalid item set")
        self._assert_candidate_snapshot(candidate, source_path)
        packet = self._validated_packet(items[0], candidate, workspace)
        on_progress(
            {
                "stage": "source_ready",
                "message": (
                    "Local TXT bytes and source packet are verified"
                    if candidate.source_format == "txt"
                    else "Local EPUB snapshot and extracted text are verified"
                ),
                "percent": 100,
            }
        )
        return packet

    def draft(
        self,
        source: PreparedLocalSourcePacket,
        workspace: Path,
        cancel_event: threading.Event,
    ) -> DistillationDraft:
        workspace = self._workspace(workspace)
        output_dir = workspace / "distillation-draft"
        output_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [
                sys.executable,
                str(self.distill_script),
                source.packet_dir,
                "--out-dir",
                str(output_dir),
            ],
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, stderr = self._wait(process, cancel_event, "distillation draft")
        envelope = _decode_envelope(stdout)
        if process.returncode != 0 or envelope.get("status") != "draft_ready":
            message = envelope.get("error") or stderr.strip()
            raise LocalSourceAdapterError(str(message or "Distillation draft generation failed"))
        if envelope.get("tool_result_envelope") != "novel_distillation.source_packet_draft.v1":
            raise LocalSourceAdapterError("Distillation draft returned the wrong contract")
        outputs = envelope.get("output_artifacts")
        checkpoints = envelope.get("checkpoints")
        success_criteria = envelope.get("success_criteria")
        if not all(isinstance(value, dict) for value in (outputs, checkpoints, success_criteria)):
            raise LocalSourceAdapterError("Distillation draft omitted structured evidence")
        assert isinstance(outputs, dict)
        assert isinstance(checkpoints, dict)
        assert isinstance(success_criteria, dict)
        if (
            checkpoints.get("cards_are_draft_candidates") is not True
            or checkpoints.get("requires_human_distillation") is not True
            or success_criteria.get("ready_for_formal_ingest") is not False
        ):
            raise LocalSourceAdapterError("Distillation output made an unsafe formal-ingest claim")

        paths: dict[str, Path] = {}
        for key, raw_path in outputs.items():
            path = _resolve_process_path(raw_path, workspace)
            if not _is_within(path, output_dir) or _is_link_or_junction(path) or not path.is_file():
                raise LocalSourceAdapterError(f"Distillation artifact is unsafe: {key}")
            paths[str(key)] = path
        required = {
            "cards_draft_json",
            "distillation_report_md",
            "validation_report_md",
            "tool_result_envelope_json",
        }
        if not required.issubset(paths):
            raise LocalSourceAdapterError("Distillation draft is missing required artifacts")
        card_count = int(checkpoints.get("card_count") or 0)
        if card_count < 1:
            raise LocalSourceAdapterError("Distillation draft contains no candidate cards")
        artifacts = [self._artifact(key, path) for key, path in sorted(paths.items())]
        return DistillationDraft(
            output_dir=str(output_dir),
            cards_path=str(paths["cards_draft_json"]),
            report_path=str(paths["distillation_report_md"]),
            validation_report_path=str(paths["validation_report_md"]),
            envelope_path=str(paths["tool_result_envelope_json"]),
            card_count=card_count,
            artifacts=artifacts,
        )

    def cleanup_failed_workspace(self, workspace: Path) -> tuple[str, str]:
        workspace = self._workspace(workspace)
        existed = workspace.exists()
        failure: str | None = None
        if existed:
            try:
                shutil.rmtree(workspace)
            except OSError as exc:
                failure = exc.__class__.__name__
        remaining = workspace.exists()
        status = "partial" if remaining else "clean"
        cleanup_hash = canonical_json_hash(
            {
                "schema": "zhimeng.source-import-cleanup.v1",
                "workspace_key": workspace.name,
                "existed": existed,
                "remaining": remaining,
                "failure_type": failure,
            }
        )
        return status, cleanup_hash

    def _validated_packet(
        self,
        item: dict[str, Any],
        candidate: LocalFileSourceCandidate,
        workspace: Path,
    ) -> PreparedLocalSourcePacket:
        if item.get("source_backend") != "generic_local_file":
            raise LocalSourceAdapterError("Prepared packet did not identify a local-file source")
        source_format = str(item.get("source_format") or "")
        if (
            source_format != candidate.source_format
            or item.get("download_scope") != "full"
        ):
            raise LocalSourceAdapterError(
                "Prepared packet does not match the selected local source format"
            )
        packet_dir = _resolve_process_path(item.get("packet_dir"), workspace)
        if not _is_within(packet_dir, workspace) or _is_link_or_junction(packet_dir):
            raise LocalSourceAdapterError("Prepared source packet escaped Runtime state")
        if not packet_dir.is_dir():
            raise LocalSourceAdapterError("Prepared source packet directory is missing")
        raw_fields = {
            "packet_manifest_path": item.get("packet_manifest_path"),
            "packet_source_path": (
                item.get("epub_path")
                if source_format == "epub"
                else item.get("text_path")
            ),
            "text_path": item.get("text_path"),
            "metadata_path": item.get("metadata_path"),
        }
        files: dict[str, Path] = {}
        for field, raw_path in raw_fields.items():
            path = _resolve_process_path(raw_path, workspace)
            if not _is_within(path, packet_dir) or _is_link_or_junction(path) or not path.is_file():
                raise LocalSourceAdapterError(f"Prepared source artifact is unsafe: {field}")
            files[field] = path
        packet_source = files["packet_source_path"]
        selected_source_hashes = {
            "source_file_sha256": candidate.source_sha256,
            "packet_source_sha256": candidate.source_sha256,
        }
        for field, expected in selected_source_hashes.items():
            if item.get(field) != expected:
                raise LocalSourceAdapterError(f"Prepared source hash mismatch: {field}")
        text_sha256 = str(item.get("text_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", text_sha256) is None:
            raise LocalSourceAdapterError("Prepared source text hash is invalid")
        if _sha256_file(packet_source) != candidate.source_sha256:
            raise LocalSourceAdapterError(
                "Prepared source snapshot differs from the selected source"
            )
        if packet_source.stat().st_size != candidate.source_size_bytes:
            raise LocalSourceAdapterError(
                "Prepared source snapshot size differs from the selected source"
            )
        if _sha256_file(files["text_path"]) != text_sha256:
            raise LocalSourceAdapterError("Prepared extracted text hash is invalid")
        if files["text_path"].stat().st_size < 1:
            raise LocalSourceAdapterError("Prepared extracted text is empty")
        try:
            if not files["text_path"].read_text(encoding="utf-8").strip():
                raise LocalSourceAdapterError("Prepared extracted text is empty")
        except UnicodeError as exc:
            raise LocalSourceAdapterError(
                "Prepared extracted text is not valid UTF-8"
            ) from exc
        try:
            metadata = json.loads(files["metadata_path"].read_text(encoding="utf-8"))
            manifest = json.loads(files["packet_manifest_path"].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LocalSourceAdapterError("Prepared packet metadata is not valid UTF-8 JSON") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("source_backend") != "generic_local_file"
            or metadata.get("source_file_sha256") != candidate.source_sha256
            or metadata.get("packet_source_sha256") != candidate.source_sha256
            or metadata.get("text_sha256") != text_sha256
        ):
            raise LocalSourceAdapterError("Prepared packet metadata does not preserve source lineage")
        if source_format == "epub":
            quality = metadata.get("text_quality")
            if not isinstance(quality, dict) or quality.get("status") != "ok":
                raise LocalSourceAdapterError(
                    "Prepared EPUB extracted text is degraded or unverified"
                )
            if files["text_path"] == packet_source or text_sha256 == candidate.source_sha256:
                raise LocalSourceAdapterError(
                    "Prepared EPUB source and extracted text are not independent"
                )
        if (
            not isinstance(manifest, dict)
            or manifest.get("source_backend") != "generic_local_file"
            or manifest.get("download_scope") != "full"
        ):
            raise LocalSourceAdapterError("Prepared packet manifest has the wrong source contract")

        artifacts = [
            self._artifact("packet_manifest", files["packet_manifest_path"]),
            self._artifact("packet_source", packet_source),
            self._artifact("packet_text", files["text_path"]),
            self._artifact("packet_metadata", files["metadata_path"]),
        ]
        return PreparedLocalSourcePacket(
            source_format=source_format,
            title=str(item.get("title") or candidate.title).strip(),
            author=str(item.get("author") or candidate.author).strip(),
            packet_dir=str(packet_dir),
            packet_manifest_path=str(files["packet_manifest_path"]),
            packet_source_path=str(packet_source),
            text_path=str(files["text_path"]),
            metadata_path=str(files["metadata_path"]),
            char_count=int(item.get("char_count") or 0),
            chapter_marker_count=int(item.get("chapter_marker_count") or 0),
            source_file_size_bytes=candidate.source_size_bytes,
            source_file_sha256=candidate.source_sha256,
            packet_source_sha256=candidate.source_sha256,
            text_sha256=text_sha256,
            artifacts=artifacts,
        )

    def _preflight_epub(self, source_path: Path) -> None:
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.extractor_script),
                    "--epub",
                    str(source_path),
                    "--validate-only",
                ],
                cwd=self.state_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_environment(),
                timeout=60,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalSourceAdapterError("EPUB preflight exceeded its time limit") from exc
        if completed.returncode != 0:
            raise LocalSourceAdapterError(
                f"EPUB preflight rejected the source: {_epub_error(completed.stderr)}"
            )
        envelope = _decode_envelope(completed.stdout)
        quality = envelope.get("text_quality")
        if (
            envelope.get("status") != "validated"
            or not isinstance(quality, dict)
            or quality.get("status") != "ok"
            or int(envelope.get("char_count") or 0) < 1
        ):
            raise LocalSourceAdapterError("EPUB preflight did not verify readable text")

    def _trusted_source_path(self, raw_path: str) -> Path:
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise LocalSourceAdapterError("Local source file was not found") from exc
        if not path.is_file() or _is_link_or_junction(path):
            raise LocalSourceAdapterError("Local source path must be a regular file")
        if not any(_is_within(path, root) for root in self.trusted_source_roots):
            raise LocalSourceAdapterError("Local source file is outside trusted roots")
        return path

    def _workspace(self, workspace: Path) -> Path:
        path = workspace.resolve(strict=False)
        if not _is_within(path, self.state_root) or path == self.state_root:
            raise LocalSourceAdapterError("Local source workspace escaped Runtime state")
        if path.exists() and _is_link_or_junction(path):
            raise LocalSourceAdapterError("Local source workspace is a link")
        return path

    @staticmethod
    def _assert_candidate_snapshot(
        candidate: LocalFileSourceCandidate,
        source_path: Path,
    ) -> None:
        if source_path.stat().st_size != candidate.source_size_bytes:
            raise LocalSourceAdapterError("Local source size changed after selection")
        if _sha256_file(source_path) != candidate.source_sha256:
            raise LocalSourceAdapterError("Local source bytes changed after selection")
        if local_source_locator_hash(str(source_path)) != candidate.source_locator_hash:
            raise LocalSourceAdapterError("Local source path changed after selection")

    def _assert_managed_snapshot(self) -> None:
        for filename, expected_hash in EXPECTED_SNAPSHOT_HASHES.items():
            path = self.scripts_root / filename
            if _is_link_or_junction(path) or not path.resolve(strict=True).is_file():
                raise LocalSourceAdapterError(f"Managed source adapter file is unsafe: {filename}")
            if _sha256_file(path) != expected_hash:
                raise LocalSourceAdapterError(f"Managed source adapter hash mismatch: {filename}")

    @staticmethod
    def _artifact(kind: str, path: Path) -> SourceArtifact:
        return SourceArtifact(
            kind=kind,
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=_sha256_file(path),
        )

    @staticmethod
    def _wait(
        process: subprocess.Popen[str],
        cancel_event: threading.Event,
        label: str,
    ) -> tuple[str, str]:
        while True:
            try:
                return process.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                if not cancel_event.is_set():
                    continue
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise LocalSourceAdapterCancelled(f"Local {label} was cancelled")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tool_fingerprint(
    tool_id: str,
    tool_version: str,
    files: tuple[tuple[str, Path], ...],
) -> str:
    evidence = [
        {"role": role, "sha256": _sha256_file(path.resolve(strict=True))}
        for role, path in sorted(files, key=lambda item: item[0])
    ]
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-tool-fingerprint.v1",
            "tool_id": tool_id,
            "tool_version": tool_version,
            "files": evidence,
        }
    )


def _decode_envelope(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for match in re.finditer(r"\{", output):
        try:
            value, end = decoder.raw_decode(output[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not output[match.start() + end :].strip():
            candidates.append(value)
    if not candidates:
        raise LocalSourceAdapterError("Managed source adapter returned no JSON envelope")
    return candidates[-1]


def _epub_error(stderr: str) -> str:
    for line in reversed(stderr.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"]
    return "managed EPUB validation failed"


def _resolve_process_path(raw_path: object, cwd: Path) -> Path:
    path = Path(str(raw_path or ""))
    if not path.is_absolute():
        path = cwd / path
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise LocalSourceAdapterError("Managed source adapter returned a missing path") from exc


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    return environment


__all__ = [
    "LocalSourceAdapter",
    "LocalSourceAdapterCancelled",
    "LocalSourceAdapterError",
    "ManagedLocalSourceAdapter",
]
