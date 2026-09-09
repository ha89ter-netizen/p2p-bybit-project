"""Результат как функция от неизмеримого.

Вероятность успешного круга и доля возврата при срыве в API не
наблюдаемы. Раньше они молча подставлялись единицей и нулём — модель
считала, что риска нет, и выдавала 4.7% в сутки. Сетка не измеряет эти
величины (их измеряют только реальные сделки), но перестаёт подменять
их удобным значением.
"""

import unittest
from decimal import Decimal

from analysis.economics import (COMPLETION_GRID, RECOVERY_GRID,
                                breakeven_completion, expected_pnl_per_trade,
                                risk_grid)

NET = Decimal("3600")
RING = Decimal("300000")
CAP = Decimal("1000000")
TPD = Decimal("12.9")


class TestExpectation(unittest.TestCase):
    def test_certain_success_equals_the_raw_profit(self):
        e = expected_pnl_per_trade(NET, RING, Decimal("1"), Decimal("0"))
        self.assertEqual(e, NET)

    def test_certain_failure_loses_the_body(self):
        e = expected_pnl_per_trade(NET, RING, Decimal("0"), Decimal("0"))
        self.assertEqual(e, -RING)

    def test_full_recovery_removes_the_downside(self):
        e = expected_pnl_per_trade(NET, RING, Decimal("0.5"), Decimal("1"))
        self.assertEqual(e, NET / 2)

    def test_small_failure_rate_dominates_the_profit(self):
        """1.2% срывов уже съедает всю прибыль — вот почему число хрупкое."""
        e = expected_pnl_per_trade(NET, RING, Decimal("0.988"), Decimal("0"))
        self.assertLess(abs(e), NET / 10)


class TestGrid(unittest.TestCase):
    def setUp(self):
        self.g = risk_grid(NET, RING, TPD, CAP)

    def test_shape_matches_the_published_grids(self):
        self.assertEqual(len(self.g["rows"]), len(RECOVERY_GRID))
        self.assertEqual(len(self.g["rows"][0]["cells"]), len(COMPLETION_GRID))

    def test_certainty_column_is_the_headline_number(self):
        """Столбец p=1 — это ровно то, что система показывала как результат."""
        last = self.g["rows"][0]["cells"][-1]
        self.assertEqual(last["p"], 1.0)
        expected = float(NET * TPD / CAP * 100)
        self.assertAlmostEqual(last["daily_pct"], expected, places=1)

    def test_lower_probability_never_improves_the_result(self):
        for row in self.g["rows"]:
            vals = [c["daily_pct"] for c in row["cells"]]
            self.assertEqual(vals, sorted(vals), "доходность обязана расти с p")

    def test_pessimistic_corner_is_deeply_negative(self):
        worst = self.g["rows"][0]["cells"][0]
        self.assertLess(worst["daily_pct"], -10)

    def test_zero_capital_does_not_divide(self):
        self.assertEqual(risk_grid(NET, RING, TPD, Decimal(0))["rows"], [])


class TestBreakeven(unittest.TestCase):
    def test_full_loss_needs_near_certainty(self):
        p = breakeven_completion(NET, RING, Decimal("0"))
        self.assertGreater(float(p), 0.98)

    def test_recovery_lowers_the_bar(self):
        a = breakeven_completion(NET, RING, Decimal("0"))
        b = breakeven_completion(NET, RING, Decimal("0.85"))
        self.assertLess(b, a)

    def test_no_profit_has_no_breakeven(self):
        self.assertIsNone(breakeven_completion(Decimal("-1"), RING, Decimal("0")))
