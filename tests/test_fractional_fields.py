"""Дробные значения в полях, объявленных целыми.

Живой случай: completeRateDay30="96.5". Прежний разбор ронял анализ
(голый int() в replay) и молча возвращал 0 в сборщике — объявление
с требованием 96.5% выглядело как объявление без требований.
"""

import unittest

from domain.models import Ad, _int, _int_ceil
from tests.fixtures import BASE_ITEM


class TestParsers(unittest.TestCase):
    def test_fractional_string_no_longer_falls_back_to_zero(self):
        self.assertEqual(_int("96.5"), 96)
        self.assertNotEqual(_int("96.5"), 0)

    def test_requirements_round_up(self):
        """Ошибаться следует в сторону строгости: 96.5 -> 97, не 96."""
        self.assertEqual(_int_ceil("96.5"), 97)
        self.assertEqual(_int_ceil("96.0"), 96)
        self.assertEqual(_int_ceil(96.1), 97)

    def test_garbage_still_falls_back(self):
        for bad in ("", None, "abc", "--"):
            with self.subTest(v=bad):
                self.assertEqual(_int(bad), 0)
                self.assertEqual(_int_ceil(bad), 0)

    def test_plain_integers_unchanged(self):
        for v in ("85", 85, 0, "0"):
            with self.subTest(v=v):
                self.assertEqual(_int(v), int(v))
                self.assertEqual(_int_ceil(v), int(v))


class TestAdParsesFractionalRequirement(unittest.TestCase):
    def _ad(self, rate):
        item = dict(BASE_ITEM)
        prefs = dict(BASE_ITEM["tradingPreferenceSet"])
        prefs["completeRateDay30"] = rate
        item["tradingPreferenceSet"] = prefs
        return Ad.from_api(item, host="h", observed_at=1000.0)

    def test_fractional_requirement_is_not_dropped(self):
        ad = self._ad("96.5")
        self.assertEqual(ad.prefs.min_complete_rate_30d, 97)

    def test_such_an_ad_blocks_a_weaker_account(self):
        """Именно этот случай и терялся: требование превращалось в 0."""
        from analysis.screening import MyProfile, blocking_reasons
        me = MyProfile(completed_orders_30d=500, completion_rate_30d=96)
        reasons = blocking_reasons(self._ad("96.5"), me)
        self.assertTrue(any("rate" in r for r in reasons),
                        f"требование 96.5% должно закрывать счёт с 96%: {reasons}")

    def test_account_meeting_the_rounded_bar_passes(self):
        from analysis.screening import MyProfile, blocking_reasons
        me = MyProfile(completed_orders_30d=500, completion_rate_30d=97)
        self.assertFalse([r for r in blocking_reasons(self._ad("96.5"), me)
                          if "rate" in r])


class TestReplayUsesTheSameParser(unittest.TestCase):
    def test_replay_does_not_call_bare_int_on_prefs(self):
        """Расхождение разбора между сборщиком и воспроизведением книги
        и было причиной падения всей отчётности."""
        import pathlib
        src = pathlib.Path("analysis/replay.py").read_text(encoding="utf-8")
        self.assertNotIn('int(prefs_raw.get("completeRateDay30")', src)
        self.assertIn("_int_ceil(prefs_raw.get(\"completeRateDay30\")", src)
