"""Распределение прибыли: границы, идемпотентность, убытки."""

import unittest
from decimal import Decimal

from capital.allocation import (ALLOCATION_RATES, AllocationPolicy,
                                allocate, allocate_trade, summarize, trade_ref)


class FakeTrade:
    """Минимальная сделка. Поля те же, что у PaperTrade."""

    def __init__(self, pnl, amount="300000", decided=1000.0,
                 buy="B", sell="S", spread=Decimal("1.0")):
        self.pnl_kzt = Decimal(str(pnl))
        self.amount_kzt = Decimal(amount)
        self.decided_at = decided
        self.settled_at = decided + 600
        self.buy_ad_id = buy
        self.sell_ad_id = sell
        self._spread = spread

    @property
    def spread_expected_pct(self):
        return self._spread


P50 = AllocationPolicy(rate_pct=Decimal("50"))


class TestSign(unittest.TestCase):
    def test_positive_profit_is_allocated(self):
        ev = allocate_trade(FakeTrade("3000"), P50)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.allocated_kzt, Decimal("1500.00"))
        self.assertEqual(ev.retained_kzt, Decimal("1500.00"))

    def test_zero_profit_allocates_nothing(self):
        self.assertIsNone(allocate_trade(FakeTrade("0"), P50))

    def test_loss_allocates_nothing(self):
        self.assertIsNone(allocate_trade(FakeTrade("-5000"), P50))

    def test_loss_never_withdraws_from_portfolio(self):
        """Портфель — независимый карман; убыток P2P его не трогает."""
        trades = [FakeTrade("3000", buy="A"), FakeTrade("-9000", buy="B")]
        events = allocate(trades, P50)
        self.assertEqual(len(events), 1)
        self.assertTrue(all(e.allocated_kzt > 0 for e in events))
        total = sum((e.allocated_kzt for e in events), Decimal(0))
        self.assertGreater(total, 0)


class TestRates(unittest.TestCase):
    def test_zero_rate_contributes_nothing_but_retains_all(self):
        ev = allocate_trade(FakeTrade("4000"), AllocationPolicy(rate_pct=Decimal("0")))
        self.assertEqual(ev.allocated_kzt, Decimal("0.00"))
        self.assertEqual(ev.retained_kzt, Decimal("4000.00"))
        self.assertFalse(ev.contributes)

    def test_full_rate_retains_nothing(self):
        ev = allocate_trade(FakeTrade("4000"), AllocationPolicy(rate_pct=Decimal("100")))
        self.assertEqual(ev.allocated_kzt, Decimal("4000.00"))
        self.assertEqual(ev.retained_kzt, Decimal("0.00"))

    def test_every_published_rate_conserves_the_base(self):
        trades = [FakeTrade("3000", buy=f"b{i}") for i in range(7)]
        for rate in ALLOCATION_RATES:
            with self.subTest(rate=rate):
                pol = AllocationPolicy(rate_pct=rate)
                s = summarize(allocate(trades, pol), pol)
                self.assertTrue(s.check_ok, f"{rate}: {s.allocated_kzt}+{s.retained_kzt}")

    def test_rate_outside_range_is_rejected(self):
        for bad in ("-1", "101"):
            with self.subTest(v=bad):
                with self.assertRaises(ValueError):
                    AllocationPolicy(rate_pct=Decimal(bad))


class TestIdempotency(unittest.TestCase):
    def test_same_trade_twice_yields_one_event(self):
        t = FakeTrade("3000")
        self.assertEqual(len(allocate([t, t], P50)), 1)

    def test_reprocessing_history_does_not_double_count(self):
        """Пересчёт с нуля — обычная операция, удваивать он не имеет права."""
        trades = [FakeTrade("3000", buy=f"b{i}") for i in range(5)]
        first = allocate(trades, P50)
        seen = {e.event_id for e in first}
        second = allocate(trades, P50, seen=seen)
        self.assertEqual(len(second), 0)

    def test_event_id_survives_window_change(self):
        """Идентификатор выводится из сделки, а не из позиции в списке."""
        t = FakeTrade("3000", decided=1234.5, buy="X", sell="Y")
        a = allocate([FakeTrade("1", buy="z"), t], P50)[-1]
        b = allocate([t], P50)[0]
        self.assertEqual(a.event_id, b.event_id)

    def test_different_rates_are_different_events(self):
        t = FakeTrade("3000")
        a = allocate_trade(t, AllocationPolicy(rate_pct=Decimal("25")))
        b = allocate_trade(t, AllocationPolicy(rate_pct=Decimal("75")))
        self.assertNotEqual(a.event_id, b.event_id)

    def test_ref_distinguishes_legs(self):
        self.assertNotEqual(trade_ref(FakeTrade("1", buy="A", sell="B")),
                            trade_ref(FakeTrade("1", buy="A", sell="C")))


class TestSummary(unittest.TestCase):
    def test_losses_are_reported_separately(self):
        trades = [FakeTrade("3000", buy="a"), FakeTrade("-1000", buy="b")]
        s = summarize(allocate(trades, P50), P50,
                      total_net_pnl_kzt=Decimal("2000"))
        self.assertEqual(s.allocatable_kzt, Decimal("3000"))
        self.assertEqual(s.losses_kzt, Decimal("-1000"))
        self.assertTrue(s.check_ok)

    def test_rounding_never_creates_money(self):
        """Копейки округляются вниз: учёт не имеет права выдумать тенге."""
        t = FakeTrade("1000.01")
        ev = allocate_trade(t, AllocationPolicy(rate_pct=Decimal("33")))
        self.assertLessEqual(ev.allocated_kzt + ev.retained_kzt, t.pnl_kzt)


if __name__ == "__main__":
    unittest.main()
