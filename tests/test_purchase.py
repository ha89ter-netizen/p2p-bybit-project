"""Буфер и покупка пачками.

Взнос от одного цикла — 4.23 USD. При комиссии 3 USD брокеру уходит 71%.
Считать, что каждый взнос сразу становится активом, значит моделировать
операцию, которую невозможно совершить.
"""

import unittest
from decimal import Decimal

from capital.purchase import CashBuffer, PurchasePolicy

RATE = Decimal("461")


class TestThreshold(unittest.TestCase):
    def test_threshold_follows_from_the_commission(self):
        """Порог не назначается, а выводится: комиссия / допустимая доля."""
        p = PurchasePolicy(commission_usd=Decimal("1"), max_fee_share=Decimal("0.01"))
        self.assertEqual(p.min_purchase_usd(), Decimal("100"))

    def test_higher_commission_raises_the_threshold(self):
        cheap = PurchasePolicy(commission_usd=Decimal("0.35"))
        dear = PurchasePolicy(commission_usd=Decimal("3"))
        self.assertLess(cheap.min_purchase_usd(), dear.min_purchase_usd())

    def test_threshold_in_tenge_uses_the_rate(self):
        p = PurchasePolicy(commission_usd=Decimal("1"), max_fee_share=Decimal("0.01"))
        self.assertEqual(p.min_purchase_kzt(RATE), Decimal("46100"))


class TestBuffer(unittest.TestCase):
    def setUp(self):
        self.b = CashBuffer(PurchasePolicy(commission_usd=Decimal("1"),
                                           max_fee_share=Decimal("0.01"),
                                           fx_spread_pct=Decimal("0.5")))

    def test_small_contribution_does_not_buy(self):
        """4.23 USD — покупать нельзя, комиссия съест четверть."""
        self.b.add(Decimal("1952"))
        self.assertIsNone(self.b.buy(0, "equities", RATE))
        self.assertEqual(self.b.balance_kzt, Decimal("1952"))

    def test_accumulated_contributions_do_buy(self):
        for _ in range(30):
            self.b.add(Decimal("1952"))
        p = self.b.buy(0, "equities", RATE)
        self.assertIsNotNone(p)
        self.assertEqual(self.b.balance_kzt, Decimal(0))

    def test_costs_are_deducted_from_what_reaches_the_asset(self):
        for _ in range(30):
            self.b.add(Decimal("1952"))
        p = self.b.buy(0, "equities", RATE)
        self.assertLess(p.invested_kzt, p.gross_kzt)
        self.assertEqual(p.commission_kzt, Decimal("461"))
        self.assertGreater(p.fx_cost_kzt, 0)

    def test_nothing_is_created_out_of_nothing(self):
        for _ in range(30):
            self.b.add(Decimal("1952"))
        p = self.b.buy(0, "equities", RATE)
        total = p.invested_kzt + p.commission_kzt + p.fx_cost_kzt
        self.assertLessEqual(total, p.gross_kzt)

    def test_without_a_rate_nothing_is_bought(self):
        """Нет курса — нет покупки; выдумывать курс нельзя."""
        for _ in range(50):
            self.b.add(Decimal("1952"))
        self.assertIsNone(self.b.buy(0, "equities", None))
        self.assertGreater(self.b.balance_kzt, 0)

    def test_cost_share_is_reported(self):
        for _ in range(30):
            self.b.add(Decimal("1952"))
        self.b.buy(0, "equities", RATE)
        share = self.b.cost_share_pct()
        self.assertGreater(share, 0)
        self.assertLess(share, 5, "издержки не должны съедать больше нескольких процентов")

    def test_negative_contribution_is_ignored(self):
        self.b.add(Decimal("-100"))
        self.assertEqual(self.b.balance_kzt, Decimal(0))
        self.assertEqual(self.b.contributions, 0)


class TestBatchingActuallyHelps(unittest.TestCase):
    def test_buying_every_contribution_is_ruinous(self):
        """Ровно то, что делала прежняя модель, если бы платила комиссию."""
        pol = PurchasePolicy(commission_usd=Decimal("1"), max_fee_share=Decimal("1"))
        b = CashBuffer(pol)
        for _ in range(30):
            b.add(Decimal("1952"))
            b.buy(0, "equities", RATE)
        every = b.cost_share_pct()

        b2 = CashBuffer(PurchasePolicy(commission_usd=Decimal("1"),
                                       max_fee_share=Decimal("0.01")))
        for _ in range(30):
            b2.add(Decimal("1952"))
        b2.buy(0, "equities", RATE)
        batched = b2.cost_share_pct()

        self.assertGreater(every, batched * 5,
                           f"покупка каждый раз {every:.1f}% против пачками {batched:.1f}%")
