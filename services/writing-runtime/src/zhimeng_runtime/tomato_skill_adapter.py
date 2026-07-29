from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Any, Protocol

from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    PreparedSourcePacket,
    SourceArtifact,
    TomatoSearchCandidate,
)
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportAdapterExecutionIdentity,
    SourceImportToolEvidence,
    canonical_json_hash,
)


class TomatoAdapterError(RuntimeError):
    pass


class TomatoAdapterCancelled(TomatoAdapterError):
    pass


ProgressCallback = Callable[[dict[str, object]], None]


class TomatoSourceAdapter(Protocol):
    def execution_identity(self) -> SourceImportAdapterExecutionIdentity: ...

    def search(self, query: str) -> list[TomatoSearchCandidate]: ...

    def resolve(
        self,
        *,
        book_id: str | None = None,
        url: str | None = None,
        title: str,
        author: str,
    ) -> TomatoSearchCandidate: ...

    def prepare(
        self,
        candidate: TomatoSearchCandidate,
        workspace: Path,
        cancel_event: threading.Event,
        on_progress: ProgressCallback,
    ) -> PreparedSourcePacket: ...

    def draft(
        self,
        source: PreparedSourcePacket,
        workspace: Path,
        cancel_event: threading.Event,
    ) -> DistillationDraft: ...


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
    evidence: list[dict[str, str]] = []
    for role, path in sorted(files, key=lambda item: item[0]):
        if _is_link_or_junction(path):
            raise TomatoAdapterError(f"Source import tool evidence is unsafe: {role}")
        resolved = path.resolve(strict=True)
        if _is_link_or_junction(resolved) or not resolved.is_file():
            raise TomatoAdapterError(f"Source import tool evidence is unsafe: {role}")
        evidence.append({"role": role, "sha256": _sha256_file(resolved)})
    return canonical_json_hash(
        {
            "schema": "zhimeng.source-import-tool-fingerprint.v1",
            "tool_id": tool_id,
            "tool_version": tool_version,
            "files": evidence,
        }
    )


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
        raise TomatoAdapterError("Skill adapter returned no JSON result envelope")
    return candidates[-1]


def extract_tomato_book_id(
    *,
    book_id: str | None = None,
    url: str | None = None,
) -> tuple[str, Literal["book_id", "url"], str]:
    if (book_id is None) == (url is None):
        raise TomatoAdapterError("exactly one of book_id or url is required")
    if book_id is not None:
        cleaned = book_id.strip()
        if re.fullmatch(r"[0-9]{1,32}", cleaned) is None:
            raise TomatoAdapterError("book_id must be 1-32 digits")
        return cleaned, "book_id", cleaned
    assert url is not None
    locator = url.strip()
    patterns = (
        r"(?i)(?:[?&#](?:book_id|bookId)=)([0-9]{1,32})",
        r"(?i)fanqie[^\s/]*/(?:page|reader)/([0-9]{1,32})",
        r"(?i)/(?:page|reader)/([0-9]{1,32})(?:[/?#]|$)",
        r"(?i)^https?://[^\s]+/([0-9]{1,32})(?:[/?#]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, locator)
        if match is not None:
            return match.group(1), "url", locator
    raise TomatoAdapterError("URL does not contain a recognizable Tomato book_id")


def parse_search_candidates(envelope: dict[str, Any]) -> list[TomatoSearchCandidate]:
    if envelope.get("ok") is not True or envelope.get("mode") != "list_only":
        message = envelope.get("error_message") or "Tomato search did not succeed"
        raise TomatoAdapterError(str(message))
    parsed: list[TomatoSearchCandidate] = []
    seen: set[str] = set()
    for group in envelope.get("items") or []:
        if not isinstance(group, dict):
            continue
        for raw_line in group.get("preview_lines") or []:
            line = str(raw_line).strip()
            match = re.match(r"^(\d+)\.\s+(.*?)\s+/\s+([0-9]{1,32})$", line)
            if match is None:
                continue
            index = int(match.group(1))
            label = match.group(2)
            book_id = match.group(3)
            if " / " not in label or book_id in seen:
                continue
            title, author = label.rsplit(" / ", 1)
            title = title.strip()
            author = author.strip()
            if not title or not author:
                continue
            parsed.append(
                TomatoSearchCandidate(
                    index=index,
                    book_id=book_id,
                    title=title,
                    author=author,
                )
            )
            seen.add(book_id)
    if not parsed and int(envelope.get("count") or 0) > 0:
        raise TomatoAdapterError("Tomato search results could not be parsed safely")
    return parsed


def normalized_book_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s《》〈〉<>「」『』\[\]【】()（）]+", "", normalized)


class PowerShellTomatoSkillAdapter:
    def __init__(
        self,
        *,
        skill_root: Path,
        downloader_script: Path,
        download_dir: Path,
        state_root: Path,
        powershell_executable: str = "powershell.exe",
        server_url: str = "http://127.0.0.1:18423",
        max_wait_minutes: int = 180,
    ) -> None:
        self.skill_root = skill_root.resolve(strict=True)
        self.downloader_script = downloader_script.resolve(strict=True)
        self.download_dir = download_dir.resolve(strict=False)
        self.state_root = state_root.resolve(strict=False)
        self.powershell_executable = powershell_executable
        self.server_url = server_url.rstrip("/")
        self.max_wait_minutes = max_wait_minutes
        self.prepare_script = (
            self.skill_root / "scripts" / "prepare_tomato_sources.ps1"
        ).resolve(strict=True)
        self.extractor_script = (
            self.skill_root / "scripts" / "extract_epub_text.py"
        ).resolve(strict=True)
        self.distill_script = (
            self.skill_root.parent
            / "novel-distillation"
            / "scripts"
            / "distill_source_packet.py"
        ).resolve(strict=True)
        self.state_root.mkdir(parents=True, exist_ok=True)

    def execution_identity(self) -> SourceImportAdapterExecutionIdentity:
        prepare_tool_id = "zhimeng.tomato-source-prepare"
        prepare_tool_version = "1"
        distillation_tool_id = "zhimeng.novel-distillation-source-packet-draft"
        distillation_tool_version = "1"
        adapter_source = Path(__file__).resolve(strict=True)
        return SourceImportAdapterExecutionIdentity(
            prepare_tool=SourceImportToolEvidence(
                tool_id=prepare_tool_id,
                tool_version=prepare_tool_version,
                tool_fingerprint=_tool_fingerprint(
                    prepare_tool_id,
                    prepare_tool_version,
                    (
                        ("runtime_adapter", adapter_source),
                        ("prepare_script", self.prepare_script),
                        ("epub_extractor", self.extractor_script),
                        ("downloader_wrapper", self.downloader_script),
                    ),
                ),
            ),
            distillation_tool=SourceImportToolEvidence(
                tool_id=distillation_tool_id,
                tool_version=distillation_tool_version,
                tool_fingerprint=_tool_fingerprint(
                    distillation_tool_id,
                    distillation_tool_version,
                    (
                        ("runtime_adapter", adapter_source),
                        ("distillation_script", self.distill_script),
                    ),
                ),
            ),
        )

    def search(self, query: str) -> list[TomatoSearchCandidate]:
        candidate_query = query.strip()
        if re.fullmatch(r"[0-9]+", candidate_query) or re.search(
            r"(?i)https?://|(?:book_id|bookId)=",
            candidate_query,
        ):
            raise TomatoAdapterError(
                "The name-search endpoint only accepts a novel title, not a book ID or URL"
            )
        search_root = self.state_root / "search-cache"
        search_root.mkdir(parents=True, exist_ok=True)
        try:
            completed = subprocess.run(
                self._powershell_command(
                    "-Queries",
                    candidate_query,
                    "-ListOnly",
                    "-DownloadDir",
                    str(self.download_dir),
                    "-LibraryDir",
                    str(search_root),
                    "-DownloaderScript",
                    str(self.downloader_script),
                ),
                cwd=self.state_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                env=self._subprocess_environment(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise TomatoAdapterError("Tomato name search timed out") from exc
        except OSError as exc:
            raise TomatoAdapterError(f"Tomato search process could not start: {exc}") from exc
        envelope = _decode_envelope(completed.stdout)
        if completed.returncode != 0:
            message = envelope.get("error_message") or completed.stderr.strip()
            raise TomatoAdapterError(str(message or "Tomato search process failed"))
        try:
            return self._structured_search(candidate_query)
        except (OSError, ValueError, urllib.error.URLError):
            return parse_search_candidates(envelope)

    def resolve(
        self,
        *,
        book_id: str | None = None,
        url: str | None = None,
        title: str,
        author: str,
    ) -> TomatoSearchCandidate:
        resolved_id, _locator_kind, _source_locator = extract_tomato_book_id(
            book_id=book_id,
            url=url,
        )
        cleaned_title = title.strip()
        cleaned_author = author.strip()
        if not cleaned_title or not cleaned_author:
            raise TomatoAdapterError("title and author are required for Tomato resolve")
        return TomatoSearchCandidate(
            index=1,
            book_id=resolved_id,
            title=cleaned_title,
            author=cleaned_author,
        )

    def prepare(
        self,
        candidate: TomatoSearchCandidate,
        workspace: Path,
        cancel_event: threading.Event,
        on_progress: ProgressCallback,
    ) -> PreparedSourcePacket:
        workspace = workspace.resolve(strict=False)
        if not _is_within(workspace, self.state_root):
            raise TomatoAdapterError("Source import workspace escaped Runtime state")
        library_dir = workspace / "source-library"
        library_dir.mkdir(parents=True, exist_ok=True)
        known_jobs = self._downloader_job_ids()
        process = subprocess.Popen(
            self._powershell_command(
                "-Queries",
                candidate.book_id,
                "-MaxWaitMinutes",
                str(self.max_wait_minutes),
                "-DownloadDir",
                str(self.download_dir),
                "-LibraryDir",
                str(library_dir),
                "-DownloaderScript",
                str(self.downloader_script),
            ),
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        downloader_job_id: str | None = None
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1.5)
                break
            except subprocess.TimeoutExpired:
                job = self._find_downloader_job(candidate.book_id, known_jobs)
                if job is not None:
                    downloader_job_id = str(job.get("id") or downloader_job_id or "") or None
                    self._report_progress(job, on_progress)
                if cancel_event.is_set():
                    if downloader_job_id is None:
                        job = self._find_downloader_job(candidate.book_id, known_jobs)
                        downloader_job_id = (
                            None if job is None else str(job.get("id") or "") or None
                        )
                    if downloader_job_id:
                        self._cancel_downloader_job(downloader_job_id)
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise TomatoAdapterCancelled("Tomato import was cancelled")

        envelope = _decode_envelope(stdout)
        if process.returncode != 0 or envelope.get("ok") is not True:
            message = envelope.get("error_message") or stderr.strip()
            raise TomatoAdapterError(str(message or "Tomato source preparation failed"))
        if envelope.get("readiness") is not True or envelope.get("status") != "success":
            raise TomatoAdapterError("Tomato source packet is not full-download ready")
        items = envelope.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise TomatoAdapterError("Tomato source preparation returned an invalid item set")
        return self._validated_source_packet(items[0], workspace)

    def draft(
        self,
        source: PreparedSourcePacket,
        workspace: Path,
        cancel_event: threading.Event,
    ) -> DistillationDraft:
        workspace = workspace.resolve(strict=False)
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
            env=self._subprocess_environment(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise TomatoAdapterCancelled("Distillation draft was cancelled")
        envelope = _decode_envelope(stdout)
        if process.returncode != 0 or envelope.get("status") != "draft_ready":
            message = envelope.get("error") or stderr.strip()
            raise TomatoAdapterError(str(message or "Distillation draft generation failed"))
        outputs = envelope.get("output_artifacts")
        if not isinstance(outputs, dict):
            raise TomatoAdapterError("Distillation draft returned no output artifacts")
        paths: dict[str, Path] = {}
        for key, raw in outputs.items():
            path = Path(str(raw)).resolve(strict=True)
            if not _is_within(path, output_dir) or _is_link_or_junction(path) or not path.is_file():
                raise TomatoAdapterError(f"Distillation artifact is unsafe: {key}")
            paths[str(key)] = path
        required = {
            "cards_draft_json",
            "distillation_report_md",
            "validation_report_md",
            "tool_result_envelope_json",
        }
        if not required.issubset(paths):
            raise TomatoAdapterError("Distillation draft is missing required artifacts")
        checkpoints = envelope.get("checkpoints")
        card_count = int(checkpoints.get("card_count") or 0) if isinstance(checkpoints, dict) else 0
        if card_count < 1:
            raise TomatoAdapterError("Distillation draft contains no candidate cards")
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

    def _powershell_command(self, *arguments: str) -> list[str]:
        return [
            self.powershell_executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.prepare_script),
            *arguments,
        ]

    @staticmethod
    def _subprocess_environment() -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        return environment

    def _structured_search(self, query: str) -> list[TomatoSearchCandidate]:
        payload = self._api_json(
            f"/api/search?q={urllib.parse.quote(query, safe='')}",
            timeout=60.0,
        )
        candidates: list[TomatoSearchCandidate] = []
        seen: set[str] = set()
        for index, item in enumerate(payload.get("items") or [], start=1):
            if not isinstance(item, dict):
                continue
            book_id = str(item.get("book_id") or "").strip()
            title = str(item.get("title") or "").strip()
            author = str(item.get("author") or "").strip()
            if not book_id or not title or not author or book_id in seen:
                continue
            candidates.append(
                TomatoSearchCandidate(
                    index=index,
                    book_id=book_id,
                    title=title,
                    author=author,
                )
            )
            seen.add(book_id)
        return candidates

    def _validated_source_packet(
        self,
        item: dict[str, Any],
        workspace: Path,
    ) -> PreparedSourcePacket:
        packet_dir = Path(str(item.get("packet_dir") or "")).resolve(strict=True)
        if not _is_within(packet_dir, workspace) or _is_link_or_junction(packet_dir):
            raise TomatoAdapterError("Prepared source packet escaped its import workspace")
        file_fields = {
            "packet_manifest_path": "packet_manifest",
            "epub_path": "packet_epub",
            "text_path": "packet_text",
            "metadata_path": "packet_metadata",
        }
        files: dict[str, Path] = {}
        for field, _kind in file_fields.items():
            path = Path(str(item.get(field) or "")).resolve(strict=True)
            if not _is_within(path, packet_dir) or _is_link_or_junction(path) or not path.is_file():
                raise TomatoAdapterError(f"Prepared source artifact is unsafe: {field}")
            files[field] = path
        downloaded = Path(str(item.get("downloaded_epub_path") or "")).resolve(strict=True)
        download_root = self.download_dir.resolve(strict=True)
        if not _is_within(downloaded, download_root) or _is_link_or_junction(downloaded) or not downloaded.is_file():
            raise TomatoAdapterError("Downloaded EPUB is outside the configured download directory")
        expected_hashes = {
            "epub_path": item.get("packet_epub_sha256"),
            "text_path": item.get("text_sha256"),
            "downloaded_epub_path": item.get("downloaded_epub_sha256"),
        }
        actual_hashes = {
            "epub_path": _sha256_file(files["epub_path"]),
            "text_path": _sha256_file(files["text_path"]),
            "downloaded_epub_path": _sha256_file(downloaded),
        }
        for key, expected in expected_hashes.items():
            if not isinstance(expected, str) or expected != actual_hashes[key]:
                raise TomatoAdapterError(f"Prepared source hash mismatch: {key}")
        if str(item.get("download_scope")) != "full":
            raise TomatoAdapterError("Only full Tomato downloads can enter automatic distillation")
        artifacts = [
            self._artifact(kind, files[field])
            for field, kind in file_fields.items()
        ]
        artifacts.append(self._artifact("downloaded_epub", downloaded))
        return PreparedSourcePacket(
            title=str(item.get("title") or "").strip(),
            author=str(item.get("author") or "Unknown").strip() or "Unknown",
            download_scope="full",
            packet_dir=str(packet_dir),
            packet_manifest_path=str(files["packet_manifest_path"]),
            epub_path=str(files["epub_path"]),
            downloaded_epub_path=str(downloaded),
            text_path=str(files["text_path"]),
            metadata_path=str(files["metadata_path"]),
            char_count=int(item.get("char_count") or 0),
            chapter_marker_count=int(item.get("chapter_marker_count") or 0),
            artifacts=artifacts,
        )

    @staticmethod
    def _artifact(kind: str, path: Path) -> SourceArtifact:
        return SourceArtifact(
            kind=kind,
            path=str(path),
            size_bytes=path.stat().st_size,
            sha256=_sha256_file(path),
        )

    def _api_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, object] | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}

    def _downloader_job_ids(self) -> set[str]:
        try:
            payload = self._api_json("/api/jobs")
        except (OSError, ValueError, urllib.error.URLError):
            return set()
        return {
            str(item.get("id"))
            for item in payload.get("items") or []
            if isinstance(item, dict) and item.get("id")
        }

    def _find_downloader_job(
        self,
        book_id: str,
        known_jobs: set[str],
    ) -> dict[str, Any] | None:
        try:
            payload = self._api_json("/api/jobs")
        except (OSError, ValueError, urllib.error.URLError):
            return None
        matches = [
            item
            for item in payload.get("items") or []
            if isinstance(item, dict)
            and str(item.get("book_id") or "") == book_id
            and str(item.get("id") or "") not in known_jobs
        ]
        return matches[0] if matches else None

    @staticmethod
    def _report_progress(job: dict[str, Any], callback: ProgressCallback) -> None:
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        saved = int(progress.get("saved_chapters") or 0)
        total = int(progress.get("chapter_total") or 0)
        percent = min(100, round(saved / total * 100)) if total > 0 else None
        try:
            callback(
                {
                    "stage": "downloading",
                    "message": str(job.get("message") or job.get("state") or "Downloading"),
                    "saved_chapters": saved,
                    "total_chapters": total,
                    "percent": percent,
                }
            )
        except Exception:
            pass

    def _cancel_downloader_job(self, job_id: str) -> None:
        try:
            self._api_json(
                f"/api/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
                method="POST",
                body={},
                timeout=5.0,
            )
        except (OSError, ValueError, urllib.error.URLError):
            pass


def build_tomato_skill_adapter(
    settings: RuntimeSettings,
    *,
    state_root: Path,
) -> PowerShellTomatoSkillAdapter | None:
    if settings.tomato_skill_root is None or settings.tomato_downloader_script is None:
        return None
    return PowerShellTomatoSkillAdapter(
        skill_root=settings.tomato_skill_root,
        downloader_script=settings.tomato_downloader_script,
        download_dir=settings.tomato_download_dir,
        state_root=state_root,
        powershell_executable=settings.powershell_executable,
        server_url=settings.tomato_server_url,
        max_wait_minutes=settings.tomato_max_wait_minutes,
    )
