"""Сравнительный слой по сегментам: сравнимость и отсутствие выдумок."""

import unittest
from decimal import Decimal

from analysis.segments import SegmentMetrics, quality_vs_spread


def m(seg, med, dyads=100, comparable=True, orders=200.0):
    return SegmentMetrics(segment=seg, comparable=comparable,
                          median_spread_pct=med, median_orders_30d=orders,
                          dyads=dyads)


class TestDirectionIsComputed(unittest.TestCase):
    """Вывод не зашит: направление берётся из чисел."""

    def test_decreasing_is_detected(self):
        r = quality_vs_spread([m("a", 3.0), m("b", 2.0), m("c", 1.0)])
        self.assertEqual(r["direction"], "decreasing")

    def test_non_monotonic_is_not_reported_as_decreasing(self):
        r = quality_vs_spread([m("a", 1.0), m("b", 3.0), m("c", 2.0)])
        self.assertEqual(r["direction"], "non_monotonic")

    def test_increasing_is_detected(self):
        r = quality_vs_spread([m("a", 1.0), m("b", 2.0)])
        self.assertEqual(r["direction"], "increasing")

    def test_single_point_is_insufficient(self):
        self.assertEqual(quality_vs_spread([m("a", 1.0)])["direction"],
                         "insufficient_data")

    def test_empty_input_is_insufficient(self):
        self.assertEqual(quality_vs_spread([])["direction"], "insufficient_data")


class TestComparability(unittest.TestCase):
    def test_nested_segments_are_marked_not_comparable(self):
        r = quality_vs_spread([m("any", 3.0, comparable=False),
                               m("good", 1.0, comparable=False)])
        self.assertFalse(r["comparable"])
        self.assertIn("тавтолог", r["note"])

    def test_disjoint_segments_are_comparable(self):
        r = quality_vs_spread([m("a\\b", 3.0), m("b\\c", 1.0)])
        self.assertTrue(r["comparable"])


class TestNoFabrication(unittest.TestCase):
    def test_missing_metrics_stay_none(self):
        x = SegmentMetrics(segment="x", comparable=True)
        for f in ("median_spread_pct", "available_liquidity_kzt",
                  "simulated_pnl_kzt", "median_completion_rate"):
            with self.subTest(field=f):
                self.assertIsNone(getattr(x, f))

    def test_segment_without_data_is_dropped_from_the_chart(self):
        r = quality_vs_spread([m("a", 3.0), SegmentMetrics("b", True)])
        self.assertEqual(len(r["points"]), 1)

    def test_thin_sample_is_flagged(self):
        self.assertTrue(m("x", 1.0, dyads=17).thin)
        self.assertFalse(m("x", 1.0, dyads=30).thin)


class TestLiquidityIsALevel(unittest.TestCase):
    """Ликвидность — уровень на момент, а не сумма по окну.

    Суммирование по 120 наблюдениям давало «227 005 млн ₸» на рынке,
    где в книге стоит несколько сотен миллионов.
    """

    def test_liquidity_is_divided_by_moments(self):
        import inspect
        from analysis import segments
        src = inspect.getsource(segments.collect)
        self.assertIn('a["liq"]) / a["moments"]', src)
        self.assertIn('a["exec"]) / a["moments"]', src)
