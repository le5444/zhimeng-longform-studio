from __future__ import annotations

import hashlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.source_import_receipt_models import (
    LocalSourceImportExecutionReceipt,
)

from epub_fixture import mark_zip_entries_encrypted, write_synthetic_epub


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXTRACTOR_PATH = (
    REPOSITORY_ROOT
    / "adapters"
    / "builtin"
    / "local-source-acceptance"
    / "scripts"
    / "extract_epub_text.py"
)


def _load_extractor():
    name = "zhimeng_test_epub_extractor"
    spec = importlib.util.spec_from_file_location(name, EXTRACTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _wait_for_terminal(client: TestClient, import_id: str) -> dict:
    deadline = time.monotonic() + 20
    latest: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/source-imports/{import_id}")
        assert response.status_code == 200, response.text
        latest = response.json()
        if latest["status"] in {"awaiting_review", "failed", "cancelled"}:
            return latest
        time.sleep(0.02)
    raise AssertionError(f"EPUB source import did not stop: {latest}")


def test_extractor_accepts_a_generated_epub_without_committed_book_bytes(
    tmp_path: Path,
) -> None:
    extractor = _load_extractor()
    epub_path = write_synthetic_epub(tmp_path / "source.epub")

    text, metadata = extractor.extract(epub_path)
    second_path = tmp_path / "other-runtime" / "different-packet-name.epub"
    second_path.parent.mkdir()
    second_path.write_bytes(epub_path.read_bytes())
    second_text, second_metadata = extractor.extract(second_path)

    assert metadata["spine_item_count"] == 2
    assert metadata["text_quality"]["status"] == "ok"
    assert metadata["char_count"] > 0
    assert "第一章" in text and "第二章" in text
    assert second_text == text
    assert second_metadata == metadata
    assert str(epub_path) not in text
    assert str(second_path) not in second_text


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"include_mimetype": False}, "mimetype"),
        ({"include_container": False}, "container.xml"),
        ({"include_opf": False}, "content.opf"),
        ({"include_spine": False}, "no spine items"),
        ({"chapters": ()}, "no spine items"),
    ],
)
def test_extractor_rejects_missing_epub_structure(
    tmp_path: Path,
    options: dict[str, object],
    message: str,
) -> None:
    extractor = _load_extractor()
    epub_path = write_synthetic_epub(tmp_path / "source.epub", **options)

    with pytest.raises(RuntimeError, match=message):
        extractor.extract(epub_path)


@pytest.mark.parametrize("unsafe_name", ["../escape.xhtml", "/escape.xhtml", "C:/escape.xhtml", "bad\\escape.xhtml"])
def test_extractor_rejects_unsafe_zip_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    extractor = _load_extractor()
    if "\\" in unsafe_name:
        # zipfile normalizes Windows separators while constructing ZipInfo.
        with pytest.raises(RuntimeError, match="unsafe"):
            extractor._safe_archive_name(unsafe_name)
        return
    epub_path = write_synthetic_epub(
        tmp_path / "source.epub",
        extra_entries=((unsafe_name, b"unsafe"),),
    )

    with pytest.raises(RuntimeError, match="unsafe|absolute|traversal"):
        extractor.extract(epub_path)


def test_extractor_rejects_encrypted_entries(tmp_path: Path) -> None:
    extractor = _load_extractor()
    epub_path = write_synthetic_epub(tmp_path / "source.epub")
    mark_zip_entries_encrypted(epub_path)

    with pytest.raises(RuntimeError, match="encrypted"):
        extractor.extract(epub_path)


@pytest.mark.parametrize(
    ("constant", "limit", "message"),
    [
        ("MAX_ARCHIVE_ENTRIES", 3, "entry-count"),
        ("MAX_SPINE_ITEMS", 1, "spine-item"),
        ("MAX_SINGLE_ENTRY_BYTES", 32, "single-entry"),
        ("MAX_TOTAL_UNCOMPRESSED_BYTES", 64, "total uncompressed"),
        ("MAX_COMPRESSION_RATIO", 1.01, "compression-ratio"),
    ],
)
def test_extractor_enforces_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    limit: int | float,
    message: str,
) -> None:
    extractor = _load_extractor()
    epub_path = write_synthetic_epub(tmp_path / "source.epub")
    monkeypatch.setattr(extractor, constant, limit)

    with pytest.raises(RuntimeError, match=message):
        extractor.extract(epub_path)


@pytest.mark.parametrize(
    ("chapters", "message"),
    [
        (("<script>only ignored content</script>",), "no readable text"),
        (("第一章\n" + "\ufffd" * 100,), "mojibake_suspected"),
    ],
)
def test_extractor_rejects_empty_or_mojibake_text(
    tmp_path: Path,
    chapters: tuple[str, ...],
    message: str,
) -> None:
    extractor = _load_extractor()
    epub_path = write_synthetic_epub(tmp_path / "source.epub", chapters=chapters)

    with pytest.raises(RuntimeError, match=message):
        extractor.extract(epub_path)


def test_epub_runs_through_source_api_to_review_with_independent_receipt_evidence(
    tmp_path: Path,
) -> None:
    trusted_root = tmp_path / "trusted"
    epub_path = write_synthetic_epub(trusted_root / "source.epub")
    source_bytes = epub_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    settings = RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime" / "runtime.sqlite3",
        allowed_origins=(),
        trusted_source_roots=(trusted_root.resolve(),),
    )

    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/source-imports/local/inspect",
            json={"source_path": str(epub_path.resolve())},
        )
        assert inspected.status_code == 200, inspected.text
        candidate = inspected.json()
        assert candidate["source_format"] == "epub"
        assert candidate["source_sha256"] == source_sha256
        assert candidate["source_size_bytes"] == len(source_bytes)

        created = client.post(
            "/api/v1/source-imports",
            json={
                "provider": "local_file",
                "query": candidate["source_path"],
                "candidate": candidate,
            },
        )
        assert created.status_code == 202, created.text
        import_id = created.json()["import_id"]
        job = _wait_for_terminal(client, import_id)
        assert job["status"] == "awaiting_review", job
        packet = job["source_packet"]
        assert packet["source_format"] == "epub"
        assert packet["source_file_sha256"] == source_sha256
        assert packet["packet_source_sha256"] == source_sha256
        assert packet["text_sha256"] != source_sha256
        assert packet["packet_source_path"] != packet["text_path"]

        receipt_response = client.get(
            f"/api/v1/source-imports/{import_id}/execution-receipt"
        )
        assert receipt_response.status_code == 200, receipt_response.text
        receipt = LocalSourceImportExecutionReceipt.model_validate(
            receipt_response.json()["receipt"]
        )
        assert receipt.source_format == "epub"
        assert receipt.packet_source_hash == source_sha256
        assert receipt.packet_text_hash == packet["text_sha256"]
        assert receipt.packet_source_hash != receipt.packet_text_hash
        assert receipt.packet_source_size_bytes == len(source_bytes)
        assert receipt.packet_text_size_bytes is not None
        assert receipt.packet_text_size_bytes > 0

    assert epub_path.read_bytes() == source_bytes
