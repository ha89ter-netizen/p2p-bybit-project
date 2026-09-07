"""Взаимоисключающие полосы качества.

Пороги страт вложены, поэтому сравнение страт по максимуму спреда
тавтологично: max по надмножеству не меньше max по подмножеству.
Полосы существуют, чтобы сравнение стало проверяемым утверждением.
"""

import unittest
from decimal import Decimal

from analysis.matching import band_names, best_pair, quality_bands
from config.settings import QUALITY_STRATA
from tests.fixtures import make_ad

AMOUNT = Decimal("300000")


def ad_of_quality(ad_id, side, finish, orders, rate, price="480.00"):
    return make_ad(ad_id=ad_id, side=side, nick=f"n{ad_id}", price=price,
                   finishNum=finish, recentOrderNum=orders, recentExecuteRate=rate)


class TestBandsPartition(unittest.TestCase):
    def test_names_follow_the_strata(self):
        self.assertEqual(band_names(),
                         ["any\\basic", "basic\\good", "good\\premium", "premium"])

    def test_every_ad_lands_in_exactly_one_band(self):
        ads = [
            ad_of_quality("A", "1", 0, 0, 0),        # any
            ad_of_quality("B", "1", 10, 50, 90),     # basic
            ad_of_quality("C", "1", 50, 200, 97),    # good
            ad_of_quality("D", "1", 200, 1000, 98),  # premium
        ]
        bands = quality_bands(ads)
        placed = [a.ad_id for _, band in bands for a in band]
        self.assertEqual(sorted(placed), ["A", "B", "C", "D"])
        self.assertEqual(len(placed), len(set(placed)), "объявление попало в две полосы")

    def test_each_ad_lands_in_the_expected_band(self):
        cases = {
            "A": (0, 0, 0, "any\\basic"),
            "B": (10, 50, 90, "basic\\good"),
            "C": (50, 200, 97, "good\\premium"),
            "D": (200, 1000, 98, "premium"),
        }
        for ad_id, (f, o, r, want) in cases.items():
            with self.subTest(ad=ad_id):
                bands = dict(quality_bands([ad_of_quality(ad_id, "1", f, o, r)]))
                got = [n for n, b in bands.items() if b]
                self.assertEqual(got, [want])

    def test_premium_ad_is_absent_from_lower_bands(self):
        """Главное свойство: верхняя страта НЕ входит в нижние полосы."""
        bands = dict(quality_bands([ad_of_quality("D", "1", 200, 1000, 98)]))
        self.assertEqual(bands["good\\premium"], [])
        self.assertEqual(len(bands["premium"]), 1)

    def test_empty_input_still_lists_all_bands(self):
        self.assertEqual(len(quality_bands([])), len(QUALITY_STRATA))


class TestBandsBreakTheTautology(unittest.TestCase):
    def test_nested_strata_force_monotonic_max(self):
        """Контрольный опыт: на ВЛОЖЕННЫХ множествах порядок неизбежен."""
        from analysis.matching import eligible
        ads = [
            ad_of_quality("b1", "1", 0, 0, 0, "470.00"),      # дешёвый, слабый
            ad_of_quality("s1", "0", 0, 0, 0, "500.00"),
            ad_of_quality("b2", "1", 200, 1000, 98, "490.00"),
            ad_of_quality("s2", "0", 200, 1000, 98, "492.00"),
        ]
        prev = None
        for st in QUALITY_STRATA:
            sub = [a for a in ads if a.meets(st)]
            bp = best_pair(sub, AMOUNT)
            cur = bp.gross_spread_pct if bp else None
            if prev is not None and cur is not None:
                self.assertLessEqual(cur, prev,
                                     "на вложенных стратах max обязан не расти")
            if cur is not None:
                prev = cur

    def test_bands_allow_the_order_to_break(self):
        """На полосах слабая полоса МОЖЕТ оказаться хуже сильной."""
        ads = [
            ad_of_quality("b1", "1", 0, 0, 0, "489.00"),      # слабые: спред мал
            ad_of_quality("s1", "0", 0, 0, 0, "490.00"),
            ad_of_quality("b2", "1", 200, 1000, 98, "470.00"),  # сильные: спред велик
            ad_of_quality("s2", "0", 200, 1000, 98, "500.00"),
        ]
        bands = dict(quality_bands(ads))
        weak = best_pair(bands["any\\basic"], AMOUNT)
        strong = best_pair(bands["premium"], AMOUNT)
        self.assertIsNotNone(weak)
        self.assertIsNotNone(strong)
        self.assertLess(weak.gross_spread_pct, strong.gross_spread_pct,
                        "полосы обязаны допускать нарушение порядка")
