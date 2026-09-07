"""Расписание отчётов по стенным часам.

Ошибка здесь тихая: отчёт либо не приходит, либо приходит дважды, и
заметно это только через сутки.
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from notify.schedule import SentLog, last_slot, slot_key

TZ = ZoneInfo("Asia/Almaty")
HOURS = (8, 20)


def at(h, m=0, day=2):
    return datetime(2026, 9, day, h, m, tzinfo=TZ)


class TestLastSlot(unittest.TestCase):
    def test_before_first_slot_falls_back_to_yesterday_evening(self):
        """В 03:00 последний наступивший слот — вчерашние 20:00, а не
        сегодняшние 08:00, которые ещё не настали."""
        self.assertEqual(slot_key(last_slot(at(3), HOURS)), "2026-09-01T20")

    def test_just_before_slot_does_not_count_it(self):
        self.assertEqual(slot_key(last_slot(at(7, 59), HOURS)), "2026-09-01T20")

    def test_just_after_slot_counts_it(self):
        self.assertEqual(slot_key(last_slot(at(8, 1), HOURS)), "2026-09-02T08")

    def test_exactly_on_slot_counts_it(self):
        self.assertEqual(slot_key(last_slot(at(8, 0), HOURS)), "2026-09-02T08")

    def test_midday_keeps_morning_slot(self):
        self.assertEqual(slot_key(last_slot(at(14), HOURS)), "2026-09-02T08")

    def test_evening_switches_to_evening_slot(self):
        self.assertEqual(slot_key(last_slot(at(20, 1), HOURS)), "2026-09-02T20")

    def test_custom_schedule(self):
        self.assertEqual(slot_key(last_slot(at(13), (6, 12, 18))),
                         "2026-09-02T12")


class TestSentLog(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = SentLog(Path(self.dir.name) / "last.txt")

    def tearDown(self):
        self.dir.cleanup()

    def test_first_slot_is_due(self):
        self.assertTrue(self.log.is_due("2026-09-02T08"))

    def test_marked_slot_is_not_due_again(self):
        self.log.mark("2026-09-02T08")
        self.assertFalse(self.log.is_due("2026-09-02T08"))

    def test_next_slot_is_due_after_previous_marked(self):
        self.log.mark("2026-09-02T08")
        self.assertTrue(self.log.is_due("2026-09-02T20"))

    def test_survives_restart(self):
        """Перезапуск коллектора не должен вызывать повторную отправку."""
        self.log.mark("2026-09-02T08")
        reopened = SentLog(self.log.path)
        self.assertFalse(reopened.is_due("2026-09-02T08"))

    def test_missed_slot_fires_once_on_return(self):
        """Ноутбук спал сутки. При возврате уходит ОДИН отчёт за текущий
        слот, а не пачка за все пропущенные."""
        self.log.mark("2026-09-01T08")
        self.assertTrue(self.log.is_due("2026-09-02T20"))
        self.log.mark("2026-09-02T20")
        self.assertFalse(self.log.is_due("2026-09-02T20"))

    def test_unwritable_path_does_not_crash(self):
        bad = SentLog("/proc/nonexistent/last.txt")
        bad.mark("x")                      # не должно бросить
        self.assertTrue(bad.is_due("x"))


if __name__ == "__main__":
    unittest.main()
