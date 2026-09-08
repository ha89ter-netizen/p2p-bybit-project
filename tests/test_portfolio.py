"""Бумажный портфель: веса, взносы, честность оценки, валюты."""

import unittest
from decimal import Decimal

from capital.allocation import AllocationPolicy, allocate_trade
from capital.portfolio import (ASSET_CLASSES, BONDS, CASH, EQUITIES,
                               MODELS_BY_NAME, PORTFOLIO_MODELS,
                               PaperPortfolio, PortfolioModel,
                               build_portfolios)
from capital.prices import MarketPriceProvider, NullPriceProvider, kzt_to_usd
from tests.test_allocation import FakeTrade

P50 = AllocationPolicy(rate_pct=Decimal("50"))


class StubPrices(MarketPriceProvider):
    """Цены есть. Нужен только чтобы проверить, что оценка включается."""
    name = "stub"

    def __init__(self, px=Decimal("100")):
        self.px = px

    def price(self, symbol, at=None):
        return self.px

    def available(self):
        return True


class TestWeights(unittest.TestCase):
    def test_every_model_sums_to_100(self):
        for m in PORTFOLIO_MODELS:
            with self.subTest(model=m.name):
                self.assertEqual(sum(m.weights.values()), Decimal(100))

    def test_bad_weights_are_rejected(self):
        with self.assertRaises(ValueError):
            PortfolioModel("broken", {EQUITIES: Decimal(50), BONDS: Decimal(30)})

    def test_unknown_asset_class_is_rejected(self):
        with self.assertRaises(ValueError):
            PortfolioModel("weird", {EQUITIES: Decimal(50), "gold": Decimal(50)})

    def test_all_four_presets_exist(self):
        for n in ("aggressive", "growth", "balanced", "conservative"):
            self.assertIn(n, MODELS_BY_NAME)


class TestContributions(unittest.TestCase):
    def _ev(self, pnl="3000", buy="B"):
        return allocate_trade(FakeTrade(pnl, buy=buy), P50)

    def test_contribution_splits_by_weights(self):
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])   # 60/30/10
        p.contribute(self._ev("2000"))                          # выделено 1000
        self.assertEqual(p.sleeves[EQUITIES].contributed_kzt, Decimal("600.00"))
        self.assertEqual(p.sleeves[BONDS].contributed_kzt, Decimal("300.00"))
        self.assertEqual(p.sleeves[CASH].contributed_kzt, Decimal("100.00"))

    def test_same_event_applied_twice_is_ignored(self):
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])
        ev = self._ev()
        self.assertTrue(p.contribute(ev))
        self.assertFalse(p.contribute(ev))
        self.assertEqual(p.contributions, 1)

    def test_zero_allocation_makes_no_contribution(self):
        ev = allocate_trade(FakeTrade("3000"), AllocationPolicy(rate_pct=Decimal("0")))
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])
        self.assertFalse(p.contribute(ev))
        self.assertEqual(p.contributions, 0)

    def test_all_models_receive_identical_contributions(self):
        """Иначе модели несравнимы: разница пошла бы от притока, а не весов."""
        evs = [self._ev(buy=f"b{i}") for i in range(4)]
        ports = build_portfolios(evs)
        totals = {n: p.contributed_kzt for n, p in ports.items()}
        self.assertEqual(len(set(totals.values())), 1, totals)

    def test_contributed_equals_sum_of_sleeves(self):
        p = PaperPortfolio(model=MODELS_BY_NAME["growth"])
        for i in range(5):
            p.contribute(self._ev(buy=f"b{i}"))
        parts = sum((p.sleeves[a].contributed_kzt for a in ASSET_CLASSES), Decimal(0))
        self.assertLessEqual(abs(p.contributed_kzt - parts), Decimal("0.05"))


class TestHonestValuation(unittest.TestCase):
    def test_without_prices_market_value_is_none_not_zero(self):
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])
        p.contribute(allocate_trade(FakeTrade("2000"), P50))
        v = p.valuation(NullPriceProvider())
        self.assertIsNone(v["market_value_kzt"])
        self.assertIsNone(v["total_return_pct"])
        self.assertFalse(v["priced"])

    def test_contributions_are_known_even_without_prices(self):
        """Взнос известен точно — его прятать не за чем."""
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])
        p.contribute(allocate_trade(FakeTrade("2000"), P50))
        v = p.valuation(NullPriceProvider())
        self.assertEqual(v["contributed_kzt"], Decimal("1000.00"))

    def test_cash_needs_no_quote(self):
        p = PaperPortfolio(model=PortfolioModel("all_cash", {CASH: Decimal(100)}))
        p.contribute(allocate_trade(FakeTrade("2000"), P50))
        v = p.valuation(NullPriceProvider())
        self.assertEqual(v["market_value_kzt"], Decimal("1000.00"))
        self.assertTrue(v["by_asset"][CASH]["market_value_kzt"] is not None)

    def test_valuation_switches_on_when_prices_appear(self):
        p = PaperPortfolio(model=MODELS_BY_NAME["balanced"])
        px = StubPrices()
        p.contribute(allocate_trade(FakeTrade("2000"), P50), prices=px)
        v = p.valuation(px)
        self.assertIsNotNone(v["market_value_kzt"])
        self.assertTrue(v["priced"])


class TestCurrency(unittest.TestCase):
    def test_no_rate_means_no_conversion(self):
        self.assertIsNone(kzt_to_usd(Decimal("300000"), None))

    def test_conversion_uses_the_supplied_rate(self):
        from capital.prices import FxQuote
        fx = FxQuote(rate_kzt_per_usd=Decimal("500"), source="test",
                     at=0.0, note="", sample=1)
        self.assertEqual(kzt_to_usd(Decimal("300000"), fx), Decimal("600"))

    def test_kzt_and_usd_are_not_added(self):
        """Смешение валют арифметически — ошибка, а не округление."""
        from capital.prices import FxQuote
        fx = FxQuote(rate_kzt_per_usd=Decimal("500"), source="test",
                     at=0.0, note="", sample=1)
        usd = kzt_to_usd(Decimal("300000"), fx)
        self.assertNotEqual(usd, Decimal("300000"))


if __name__ == "__main__":
    unittest.main()
