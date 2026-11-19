"""Compressed, content-addressed SQLite snapshots for local development.

Product backup/restore remains owned by BackupService. This tool keeps development
checkpoints in .runtime/dev-snapshots; it never removes the source or restores over
an existing database. Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_STORE = Path(__file__).resolve().parents[1] / ".runtime" / "dev-snapshots"


def fingerprint(stream) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def reference(store: Path, label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", label):
        raise ValueError("Label must be 1-160 letters, digits, dots, underscores or hyphens")
    return store / "refs" / f"{label}.json"


def verify_object(path: Path, digest: str, size: int) -> None:
    with gzip.open(path, "rb") as stream:
        actual = fingerprint(stream)
    if actual != (digest, size):
        raise ValueError(f"Snapshot checksum mismatch: {path}")


def archive(source: Path, store: Path, label: str, *, origin: Path | None = None) -> dict:
    """Archive a closed SQLite snapshot byte for byte; keep the source file."""
    ref = reference(store, label)
    source = source.resolve(strict=True)
    companions = [Path(str(source) + suffix) for suffix in ("-wal", "-shm", "-journal")]
    if any(path.exists() for path in companions):
        raise ValueError("Database has journal companions; use capture for a live database")
    before = source.stat()
    with source.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError(f"Not a SQLite database: {source}")
        stream.seek(0)
        digest, size = fingerprint(stream)
    existing = json.loads(ref.read_text(encoding="utf-8")) if ref.exists() else None
    if existing is not None and existing["sha256"] != digest:
        raise ValueError(f"Label already names a different snapshot: {label}")
    target = store / "objects" / f"{digest}.sqlite3.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Publish only a complete, verified object. A failed archive keeps its source.
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix="archive-", dir=store) as temporary:
            staged = Path(temporary) / "snapshot.gz"
            with source.open("rb") as src, gzip.open(staged, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            verify_object(staged, digest, size)
            staged.replace(target)
    else:
        verify_object(target, digest, size)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or any(
        path.exists() for path in companions
    ):
        raise ValueError("Source changed during archiving; use capture for a live database")
    record = {
        "label": label,
        "source": str(origin or source),
        "sha256": digest,
        "size_bytes": size,
        "stored_bytes": target.stat().st_size,
        "created_at": datetime.now(UTC).isoformat(),
    }
    ref.parent.mkdir(parents=True, exist_ok=True)
    if existing is not None:
        return existing
    with ref.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return record


def capture(source: Path, store: Path, label: str) -> dict:
    """Take a consistent SQLite online backup, compress it, discard the temp copy."""
    reference(store, label)
    source = source.resolve(strict=True)
    store.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=store) as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
        return archive(snapshot, store, label, origin=source)


def restore(store: Path, label: str, output: Path) -> Path:
    record = json.loads(reference(store, label).read_text(encoding="utf-8"))
    digest = record["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid snapshot digest")
    source = store / "objects" / f"{digest}.sqlite3.gz"
    output.parent.mkdir(parents=True, exist_ok=True)
    # Verify before publishing; exclusive creation preserves existing data.
    with tempfile.TemporaryDirectory(prefix="restore-", dir=output.parent) as temporary:
        staged = Path(temporary) / "snapshot.sqlite3"
        with gzip.open(source, "rb") as src, staged.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        with staged.open("rb") as stream:
            if fingerprint(stream) != (digest, record["size_bytes"]):
                raise ValueError("Snapshot checksum mismatch")
        with output.open("xb") as dst:
            try:
                with staged.open("rb") as src:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except BaseException:
                dst.close()
                output.unlink()
                raise
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "archive"):
        command = commands.add_parser(name)
        command.add_argument("database", type=Path)
        command.add_argument("label")
    command = commands.add_parser("restore")
    command.add_argument("label")
    command.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        print(restore(args.store, args.label, args.output))
    else:
        action = capture if args.command == "capture" else archive
        print(json.dumps(action(args.database, args.store, args.label), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
