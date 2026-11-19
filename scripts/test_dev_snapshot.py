import gzip
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dev_snapshot import archive, capture, restore


class DevelopmentSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = self.root / "store"
        self.source = self.root / "source.sqlite3"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE notes (body TEXT)")
            db.execute("INSERT INTO notes VALUES (?)", ("原始内容",))
            db.commit()

    def test_archive_preserves_bytes_and_deduplicates_labels(self):
        before = self.source.read_bytes()
        first = archive(self.source, self.store, "before-a")
        second = archive(self.source, self.store, "before-b")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(len(list((self.store / "objects").iterdir())), 1)
        self.assertEqual(self.source.read_bytes(), before)
        restored = restore(self.store, "before-b", self.root / "restored.sqlite3")
        self.assertEqual(restored.read_bytes(), before)

    def test_capture_includes_committed_wal_without_modifying_source(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO notes VALUES ('committed WAL')")
            db.commit()
            with self.assertRaisesRegex(ValueError, "journal companions"):
                archive(self.source, self.store, "unsafe-copy")
            record = capture(self.source, self.store, "online")
            self.assertEqual(record["source"], str(self.source.resolve()))
            self.assertEqual(db.execute("SELECT count(*) FROM notes").fetchone()[0], 2)
        output = restore(self.store, "online", self.root / "online.sqlite3")
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(db.execute("SELECT body FROM notes ORDER BY rowid").fetchall(),
                             [("原始内容",), ("committed WAL",)])
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertFalse(list(self.store.glob("capture-*")))

    def test_restore_does_not_overwrite_existing_data(self):
        archive(self.source, self.store, "original")
        output = self.root / "already-exists.sqlite3"
        output.write_bytes(b"keep me")
        with self.assertRaises(FileExistsError):
            restore(self.store, "original", output)
        self.assertEqual(output.read_bytes(), b"keep me")

    def test_corruption_and_reused_labels_preserve_source_and_reference(self):
        before = self.source.read_bytes()
        record = archive(self.source, self.store, "original")
        ref = self.store / "refs" / "original.json"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("INSERT INTO notes VALUES ('new version')")
            db.commit()
        with self.assertRaisesRegex(ValueError, "different snapshot"):
            archive(self.source, self.store, "original")
        self.assertEqual(json.loads(ref.read_text(encoding="utf-8")), record)
        self.source.write_bytes(before)
        obj = self.store / "objects" / f"{record['sha256']}.sqlite3.gz"
        with gzip.open(obj, "wb") as stream:
            stream.write(b"damaged")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            archive(self.source, self.store, "copy")
        output = self.root / "damaged.sqlite3"
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            restore(self.store, "original", output)
        self.assertFalse(output.exists())
        self.assertEqual(self.source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
