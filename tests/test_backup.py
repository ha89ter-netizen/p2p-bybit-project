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
        self.off = self.root / "offsite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_snapshot_is_a_readable_database(self):
        p = snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        restored = self.root / "restored.sqlite"
        with gzip.open(p, "rb") as src:
            restored.write_bytes(src.read())
        c = sqlite3.connect(restored)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ad").fetchone()[0], 2)
        c.close()

    def test_existing_snapshot_is_never_overwritten(self):
        first = snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        before = first.read_bytes()
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO ad VALUES ('C','482')"); c.commit(); c.close()
        self.assertIsNone(snapshot(self.db, self.out, stamp="20260907", offsite=self.off))
        self.assertEqual(first.read_bytes(), before)

    def test_snapshot_file_is_read_only(self):
        p = snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        self.assertEqual(p.stat().st_mode & 0o222, 0)

    def test_checksum_is_recorded(self):
        snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        sums = (self.out / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn("p2p-20260907.sqlite.gz", sums)
        self.assertRegex(sums, r"[0-9a-f]{64}")

    def test_distinct_days_coexist(self):
        a = snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        b = snapshot(self.db, self.out, stamp="20260908", offsite=self.off)
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
        snap = snapshot(self.db, self.out, stamp="20260907", offsite=self.off)
        dst = copy_offsite(snap, digest_of_gz(snap), offsite=self.off)
        self.assertTrue(dst.exists())
        self.assertEqual(digest_of_gz(dst), digest_of_gz(snap))

    def test_corrupt_copy_is_rejected_and_removed(self):
        """Битая копия не должна остаться под видом резервной."""
        from scripts.backup import snapshot, copy_offsite
        snap = snapshot(self.db, self.out, stamp="20260907",
                        offsite=self.root / "elsewhere")
        with self.assertRaises(OSError):
            copy_offsite(snap, "0" * 64, offsite=self.off)
        self.assertEqual(list(self.off.glob("*.gz")), [])

    def test_offsite_failure_does_not_lose_local_snapshot(self):
        from scripts.backup import snapshot
        import scripts.backup as B
        orig, B.OFFSITE_DIR = B.OFFSITE_DIR, Path("/proc/nonexistent/nope")
        try:
            snap = snapshot(self.db, self.out, stamp="20260908", offsite=self.off)
        finally:
            B.OFFSITE_DIR = orig
        self.assertTrue(snap.exists())


class TestTestsNeverTouchRealArchive(unittest.TestCase):
    """Тест, который пишет в настоящий архив, портит исследовательские данные.

    Это уже случилось: прогон тестов положил 297-байтную тестовую базу
    в реальный каталог копий и дописал её хеш в SHA256SUMS.
    """

    def test_empty_env_disables_offsite(self):
        """Пустая переменная должна отключать копию, а не писать в "." """
        import os
        from unittest import mock
        import scripts.backup as B
        with mock.patch.dict(os.environ, {"P2P_OFFSITE_DIR": "  "}):
            self.assertIsNone(B._offsite_from_env())

    def test_snapshot_honours_explicit_offsite(self):
        from scripts.backup import snapshot, OFFSITE_DIR
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db = root / "s.sqlite"
            c = sqlite3.connect(db)
            c.execute("CREATE TABLE t (x INTEGER)"); c.commit(); c.close()
            off = root / "off"
            snapshot(db, root / "loc", stamp="29991231", offsite=off)
            self.assertTrue((off / "p2p-29991231.sqlite.gz").exists())
            self.assertFalse((OFFSITE_DIR / "p2p-29991231.sqlite.gz").exists())


class TestMissingDays(unittest.TestCase):
    """Пропущенный день должен обнаруживаться по файлам, а не по вере
    в одну ветку кода: «снимок за сегодня уже есть» однажды прозвучало
    при отсутствующем файле."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "s.sqlite"
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE poll_run (started_at REAL)")
        c.executemany("INSERT INTO poll_run VALUES (?)",
                      [(1788700000.0,), (1788790000.0,)])
        c.commit(); c.close()
        self.out = self.root / "b"
        self.out.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _days(self):
        import time
        return sorted({time.strftime("%Y%m%d", time.localtime(t))
                       for t in (1788700000.0, 1788790000.0)})

    def test_all_days_missing_when_dir_is_empty(self):
        from scripts.backup import missing_days
        self.assertEqual(missing_days(self.db, self.out), self._days())

    def test_present_snapshot_is_not_reported(self):
        from scripts.backup import missing_days
        days = self._days()
        (self.out / f"p2p-{days[0]}.sqlite.gz").write_bytes(b"x")
        self.assertEqual(missing_days(self.db, self.out), days[1:])

    def test_nothing_missing_when_all_present(self):
        from scripts.backup import missing_days
        for d in self._days():
            (self.out / f"p2p-{d}.sqlite.gz").write_bytes(b"x")
        self.assertEqual(missing_days(self.db, self.out), [])
