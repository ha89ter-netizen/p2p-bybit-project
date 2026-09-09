"""Эталон: тенговый вклад.

Без него сравнение портфелей не отвечает на вопрос, стоило ли вообще
выводить деньги из тенге.
"""

import unittest
from decimal import Decimal

from capital.benchmark import SECONDS_PER_YEAR, DepositBenchmark


class TestUnknownRate(unittest.TestCase):
    def test_without_a_rate_nothing_is_invented(self):
        b = DepositBenchmark()
        b.add(0, Decimal("10000"))
        self.assertFalse(b.known)
        self.assertIsNone(b.value_at(float(SECONDS_PER_YEAR)))
        self.assertIsNone(b.interest_at(float(SECONDS_PER_YEAR)))

    def test_summary_says_why_it_is_empty(self):
        s = DepositBenchmark().summary(0)
        self.assertFalse(s["known"])
        self.assertIn("не задана", s["note"])


class TestAccrual(unittest.TestCase):
    def setUp(self):
        self.b = DepositBenchmark(annual_rate_pct=Decimal("15"))

    def test_a_year_at_fifteen_percent(self):
        self.b.add(0, Decimal("100000"))
        v = self.b.value_at(float(SECONDS_PER_YEAR))
        self.assertAlmostEqual(float(v), 115000, places=0)

    def test_later_contributions_earn_less(self):
        """Взнос, сделанный позже, обязан принести меньше — иначе учёт врёт."""
        early = DepositBenchmark(annual_rate_pct=Decimal("15"))
        early.add(0, Decimal("100000"))
        late = DepositBenchmark(annual_rate_pct=Decimal("15"))
        late.add(float(SECONDS_PER_YEAR) / 2, Decimal("100000"))
        t = float(SECONDS_PER_YEAR)
        self.assertGreater(early.value_at(t), late.value_at(t))

    def test_deposited_is_tracked_separately_from_value(self):
        self.b.add(0, Decimal("50000"))
        self.b.add(0, Decimal("50000"))
        self.assertEqual(self.b.deposited_kzt, Decimal("100000"))
        self.assertGreater(self.b.value_at(float(SECONDS_PER_YEAR)), Decimal("100000"))

    def test_zero_rate_keeps_the_principal(self):
        z = DepositBenchmark(annual_rate_pct=Decimal("0"))
        z.add(0, Decimal("100000"))
        self.assertEqual(z.value_at(float(SECONDS_PER_YEAR)), Decimal("100000"))

    def test_value_before_any_time_equals_principal(self):
        self.b.add(0, Decimal("100000"))
        self.assertEqual(self.b.value_at(0), Decimal("100000"))


class TestComparison(unittest.TestCase):
    def test_deposit_can_beat_a_dollar_portfolio(self):
        """Главный смысл эталона: он вполне может выиграть."""
        dep = DepositBenchmark(annual_rate_pct=Decimal("15"))
        dep.add(0, Decimal("100000"))
        year = float(SECONDS_PER_YEAR)
        portfolio_at_8pct = Decimal("108000")
        self.assertGreater(dep.value_at(year), portfolio_at_8pct)
