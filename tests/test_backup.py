"""Снимок базы: неизменяемость и целостность."""

import gzip
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.backup import snapshot


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "src.sqlite"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE ad (ad_id TEXT PRIMARY KEY, price TEXT)")
        c.executemany("INSERT INTO ad VALUES (?,?)", [("A", "480"), ("B", "481")])
        c.commit(); c.close()
        self.out = self.root / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_a_readable_database(self):
        p = snapshot(self.db, self.out, stamp="20260907")
        restored = self.root / "restored.sqlite"
        with gzip.open(p, "rb") as src:
            restored.write_bytes(src.read())
        c = sqlite3.connect(restored)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ad").fetchone()[0], 2)
        c.close()

    def test_existing_snapshot_is_never_overwritten(self):
        first = snapshot(self.db, self.out, stamp="20260907")
        before = first.read_bytes()
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO ad VALUES ('C','482')"); c.commit(); c.close()
        self.assertIsNone(snapshot(self.db, self.out, stamp="20260907"))
        self.assertEqual(first.read_bytes(), before)

    def test_snapshot_file_is_read_only(self):
        p = snapshot(self.db, self.out, stamp="20260907")
        self.assertEqual(p.stat().st_mode & 0o222, 0)

    def test_checksum_is_recorded(self):
        snapshot(self.db, self.out, stamp="20260907")
        sums = (self.out / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn("p2p-20260907.sqlite.gz", sums)
        self.assertRegex(sums, r"[0-9a-f]{64}")

    def test_distinct_days_coexist(self):
        a = snapshot(self.db, self.out, stamp="20260907")
        b = snapshot(self.db, self.out, stamp="20260908")
        self.assertTrue(a.exists() and b.exists())


class TestOffsite(unittest.TestCase):
    """Копия вне машины: проверяется по РАСПАКОВАННОМУ содержимому."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "src.sqlite"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE ad (ad_id TEXT PRIMARY KEY)")
        c.execute("INSERT INTO ad VALUES ('A')")
        c.commit(); c.close()
        self.out = self.root / "local"
        self.off = self.root / "offsite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_offsite_copy_matches_source_data(self):
        from scripts.backup import snapshot, copy_offsite, digest_of_gz, _sha256
        snap = snapshot(self.db, self.out, stamp="20260907")
        dst = copy_offsite(snap, digest_of_gz(snap), offsite=self.off)
        self.assertTrue(dst.exists())
        self.assertEqual(digest_of_gz(dst), digest_of_gz(snap))

    def test_corrupt_copy_is_rejected_and_removed(self):
        """Битая копия не должна остаться под видом резервной."""
        from scripts.backup import snapshot, copy_offsite
        snap = snapshot(self.db, self.out, stamp="20260907")
        with self.assertRaises(OSError):
            copy_offsite(snap, "0" * 64, offsite=self.off)
        self.assertEqual(list(self.off.glob("*.gz")), [])

    def test_offsite_failure_does_not_lose_local_snapshot(self):
        from scripts.backup import snapshot
        import scripts.backup as B
        orig, B.OFFSITE_DIR = B.OFFSITE_DIR, Path("/proc/nonexistent/nope")
        try:
            snap = snapshot(self.db, self.out, stamp="20260908")
        finally:
            B.OFFSITE_DIR = orig
        self.assertTrue(snap.exists())
