import unittest
from decimal import Decimal

from analysis.economics import (compute, breakeven_p_completion, required_spread_pct)
from analysis.matching import evaluate
from config.settings import EconomicsConfig, Assumption
from tests.fixtures import make_ad

A300 = Decimal("300000")


def pair(buy="501.20", sell="504.35"):
    b = make_ad(ad_id="b", side="1", nick="b", price=buy)
    s = make_ad(ad_id="s", side="0", nick="s", price=sell)
    return evaluate(b, s, A300)


class TestSpreadCalculation(unittest.TestCase):
    def test_gross_spread_matches_hand_calculation(self):
        p = pair()
        self.assertAlmostEqual(float(p.gross_spread_pct), 0.6285, places=3)

    def test_gross_pnl_matches_hand_calculation(self):
        """300 000 KZT при 501.20 -> 504.35 даёт около +1 884 KZT."""
        p = pair()
        self.assertAlmostEqual(float(p.gross_pnl_kzt), 1885.5, delta=2.0)

    def test_decimal_precision_is_exact(self):
        """float дал бы 473.98999999999999 и уехал бы на сотые в PnL."""
        p = pair(buy="473.99", sell="490.60")
        self.assertEqual(p.buy_ad.price, Decimal("473.99"))
        self.assertIsInstance(p.gross_pnl_kzt, Decimal)

    def test_negative_spread_is_negative_pnl(self):
        """Случай bybit.kz: покупаем дороже, чем продаём."""
        p = pair(buy="479.99", sell="450.59")
        self.assertLess(p.gross_spread_pct, 0)
        self.assertLess(p.gross_pnl_kzt, 0)


class TestEconomics(unittest.TestCase):
    def test_zero_exchange_fee_is_known_not_guessed(self):
        e = compute(pair())
        self.assertEqual(e.exchange_fees_kzt, Decimal("0"))
        self.assertNotIn("exchange_fee_taker", e.unknown_components)

    def test_unknown_components_are_flagged(self):
        e = compute(pair())
        self.assertFalse(e.is_trustworthy)
        self.assertIn("p_leg_completes", e.unknown_components)
        self.assertIn("p_bank_freeze_per_trade", e.unknown_components)

    def test_bank_fee_reduces_net(self):
        cfg = EconomicsConfig(bank_transfer_fee_kzt=Assumption(
            Decimal("500"), "ESTIMATED", "test"))
        e = compute(pair(), cfg=cfg)
        self.assertEqual(e.bank_fees_kzt, Decimal("1000"))   # две ноги
        self.assertEqual(e.known_net_pnl_kzt, e.gross_pnl_kzt - 1000)

    def test_fx_drift_scales_with_roundtrip_time(self):
        cfg = EconomicsConfig(fx_drift_bps_per_minute=Assumption(
            Decimal("1"), "ESTIMATED", "test"))
        fast = compute(pair(), roundtrip_minutes=Decimal("1"), cfg=cfg)
        slow = compute(pair(), roundtrip_minutes=Decimal("10"), cfg=cfg)
        self.assertEqual(slow.fx_drift_kzt, fast.fx_drift_kzt * 10)
        self.assertLess(slow.known_net_pnl_kzt, fast.known_net_pnl_kzt)

    def test_deterministic(self):
        self.assertEqual(compute(pair()), compute(pair()))


class TestBreakeven(unittest.TestCase):
    def test_breakeven_probability(self):
        """При net +1885 и потере 5000 в застрявшем круге нужна p > 0.726."""
        e = compute(pair())
        p = breakeven_p_completion(e, Decimal("5000"))
        self.assertAlmostEqual(float(p), 5000 / (1885.5 + 5000), places=2)

    def test_no_breakeven_when_net_is_negative(self):
        e = compute(pair(buy="479.99", sell="450.59"))
        self.assertIsNone(breakeven_p_completion(e, Decimal("5000")))

    def test_required_spread_grows_as_completion_falls(self):
        loss = Decimal("5000")
        at99 = required_spread_pct(A300, Decimal("0.99"), loss)
        at90 = required_spread_pct(A300, Decimal("0.90"), loss)
        self.assertGreater(at90, at99)


if __name__ == "__main__":
    unittest.main()
