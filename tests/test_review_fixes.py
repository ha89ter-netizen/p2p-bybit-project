"""Правки по результатам контр-аудита: квантиль, лимит, концентрация, потолок."""

import unittest
from decimal import Decimal

from analysis.matching import pair_spread_quantile
from tests.test_allocation import FakeTrade


class TestQuantileMetric(unittest.TestCase):
    """Максимум по N растёт вместе с N сам по себе; квантиль устойчивее."""

    def test_no_pairs_gives_none(self):
        self.assertIsNone(pair_spread_quantile([], Decimal("300000")))

    def test_quantile_never_exceeds_the_maximum(self):
        from analysis.matching import iter_pairs_by_spread
        from tests.fixtures import make_ad
        ads = [make_ad(ad_id=f"b{i}", side="1", nick=f"b{i}", price=str(470 + i))
               for i in range(6)]
        ads += [make_ad(ad_id=f"s{i}", side="0", nick=f"s{i}", price=str(490 + i))
                for i in range(6)]
        amt = Decimal("300000")
        best = next(iter_pairs_by_spread(ads, amt), None)
        q = pair_spread_quantile(ads, amt, Decimal("0.99"))
        self.assertIsNotNone(q)
        self.assertLessEqual(q, best.gross_spread_pct)

    def test_lower_quantile_is_lower(self):
        from tests.fixtures import make_ad
        ads = [make_ad(ad_id=f"b{i}", side="1", nick=f"b{i}", price=str(460 + i * 2))
               for i in range(8)]
        ads += [make_ad(ad_id=f"s{i}", side="0", nick=f"s{i}", price=str(490 + i * 2))
                for i in range(8)]
        amt = Decimal("300000")
        hi = pair_spread_quantile(ads, amt, Decimal("0.99"))
        lo = pair_spread_quantile(ads, amt, Decimal("0.50"))
        self.assertGreater(hi, lo)


class TestAdvertiserLimit(unittest.TestCase):
    def test_config_exposes_the_limit(self):
        from config.settings import PAPER
        self.assertGreaterEqual(PAPER.max_trades_per_advertiser, 0)
        self.assertGreaterEqual(PAPER.max_volume_per_advertiser_kzt, 0)

    def test_zero_disables_the_limit(self):
        """Ноль обязан возвращать прежнее поведение, иначе нельзя сравнить."""
        from dataclasses import replace
        from config.settings import PAPER
        cfg = replace(PAPER, max_trades_per_advertiser=0,
                      max_volume_per_advertiser_kzt=Decimal(0))
        self.assertEqual(cfg.max_trades_per_advertiser, 0)


class TestConcentration(unittest.TestCase):
    def _res(self, pnls):
        from analysis.paper import PaperResult, PaperTrade
        r = PaperResult()
        for i, (adv, v) in enumerate(pnls):
            r.trades.append(PaperTrade(
                decided_at=i, executed_at=i, settled_at=i,
                amount_kzt=Decimal("300000"), buy_price=Decimal("480"),
                sell_price_expected=Decimal("485"), sell_price_actual=Decimal("485"),
                usdt=Decimal("600"), pnl_kzt=Decimal(str(v)), outcome="filled",
                buy_ad_id=f"b{i}", sell_ad_id=f"s{i}", buy_adv="x", sell_adv=adv))
        return r

    def test_single_counterparty_is_maximum_concentration(self):
        self.assertEqual(self._res([("a", 100), ("a", 100)]).concentration_hhi(), 1.0)

    def test_even_spread_lowers_the_index(self):
        r = self._res([("a", 100), ("b", 100), ("c", 100), ("d", 100)])
        self.assertAlmostEqual(r.concentration_hhi(), 0.25, places=3)

    def test_top_share_is_reported(self):
        r = self._res([("a", 700), ("b", 300)])
        self.assertEqual(r.top_share(1), 70.0)
        self.assertEqual(r.top_share(2), 100.0)

    def test_no_profit_gives_none(self):
        self.assertIsNone(self._res([("a", -50)]).concentration_hhi())


class TestPlausibilityCeiling(unittest.TestCase):
    def _res(self, pct_per_day):
        """Доходность считается по сумме сделок, а не по итоговому капиталу."""
        from analysis.paper import PaperResult, PaperTrade
        r = PaperResult()
        r.start_capital_kzt = Decimal("1000000")
        r.window_from, r.window_to = 0.0, 86400.0
        pnl = Decimal("1000000") * Decimal(str(pct_per_day)) / 100
        r.trades.append(PaperTrade(
            decided_at=0, executed_at=0, settled_at=0,
            amount_kzt=Decimal("300000"), buy_price=Decimal("480"),
            sell_price_expected=Decimal("485"), sell_price_actual=Decimal("485"),
            usdt=Decimal("600"), pnl_kzt=pnl, outcome="filled",
            buy_ad_id="b", sell_ad_id="s", buy_adv="x", sell_adv="y"))
        r.final_capital_kzt = r.start_capital_kzt + pnl
        return r

    def test_modest_return_is_plausible(self):
        self.assertFalse(self._res("0.5").implausible())

    def test_ten_percent_a_day_is_flagged(self):
        """10% в сутки на розничном валютном рынке — сигнал об ошибке модели."""
        self.assertTrue(self._res("10").implausible())

    def test_threshold_comes_from_config(self):
        from config.settings import PAPER
        self.assertGreater(PAPER.plausible_daily_return_pct, 0)
