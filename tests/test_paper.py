"""Бумажная торговля.

Главная опасность этого модуля — приятное число. Все тесты здесь про то,
чтобы симулятор НЕ завышал результат.
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analysis.paper import simulate
from config.settings import PaperConfig
from storage.db import Store
from tests.fixtures import make_ad

HOST = "api2.bybit.com"

# Скрининг в этих тестах выключен намеренно: они про капитал, ликвидность
# и исполнение. Фильтры проверяются отдельно в test_screening.py, а здесь
# только мешали бы отличить «отфильтровано» от «логика сломана».
CFG = PaperConfig(deploy_pct=Decimal("30"), capital_kzt=Decimal("1000000"),
                  min_spread_pct=Decimal("0.5"), execution_delay_sec=0,
                  roundtrip_sec=600, min_recent_orders=0, min_execute_rate=0,
                  min_ad_finish_num=0, require_ad_shows_volume=False,
                  screen_requirements=False, screen_conflicts=False,
                  recent_volume_sec=0, max_release_sec=0,
                  max_price_deviation_pct=Decimal("1000"),
                  blacklist_after=10**9, compound=False)


class PaperCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "t.sqlite"))
        self.t0 = 100000.0

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def observe(self, t, ads):
        for side in ("1", "0"):
            self.store.record_poll(started_at=t, finished_at=t + 1, host=HOST,
                                   side=side, complete=True, n_items=1, n_pages=1,
                                   total_count=1, latency_ms=1, error=None)
        for ad in ads:
            object.__setattr__(ad, "observed_at", t)
        if ads:
            self.store.upsert_ads(ads)

    def pair_ads(self, buy="480.00", sell="490.00", qty="5000", **over):
        return [make_ad(ad_id="b", side="1", nick="b", price=buy,
                        quantity=qty, lastQuantity=qty, **over),
                make_ad(ad_id="s", side="0", nick="s", price=sell,
                        quantity=qty, lastQuantity=qty, **over)]


class TestCapitalConstraint(PaperCase):
    def test_capital_is_30_percent_of_total(self):
        self.assertEqual(CFG.working_capital_kzt, Decimal("300000"))

    def test_cannot_open_second_trade_while_capital_is_locked(self):
        """Деньги заперты на время круга. Без этого симулятор входит в
        каждую возможность подряд и рисует прибыль на деньги, которых нет."""
        ads = self.pair_ads()
        for i in range(20):                       # 20 моментов по 61 с
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, CFG)
        span = 19 * 61
        self.assertLessEqual(r.n, span // CFG.roundtrip_sec + 1)
        self.assertGreater(r.entries_skipped_no_capital, 0)


class TestLiquidityConsumption(PaperCase):
    def test_own_trades_exhaust_the_ad(self):
        """Объявление на 350k не может выдать 300k дважды."""
        cfg = PaperConfig(**{**CFG.__dict__, "roundtrip_sec": 60})
        ads = self.pair_ads(qty="729.17")          # 729.17 * 480 ≈ 350 000 KZT
        for i in range(10):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, cfg)
        self.assertEqual(r.n, 1)
        self.assertGreater(r.entries_skipped_exhausted, 0)

    def test_deep_ad_allows_several_trades(self):
        cfg = PaperConfig(**{**CFG.__dict__, "roundtrip_sec": 60})
        ads = self.pair_ads(qty="5000")            # ≈ 2.4 млн KZT
        for i in range(10):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, cfg)
        self.assertGreater(r.n, 1)


class TestVolumeProof(PaperCase):
    def test_untouched_ads_are_skipped_when_required(self):
        """Объявление с выгодной ценой, которое сутки никто не берёт, —
        это не возможность, а свидетельство, что взять его нельзя."""
        cfg = PaperConfig(**{**CFG.__dict__, "require_ad_shows_volume": True})
        ads = self.pair_ads()
        for i in range(10):
            self.observe(self.t0 + i * 61, ads)   # executedQuantity не меняется
        self.assertEqual(simulate(self.store, HOST, cfg).n, 0)
        self.assertGreater(simulate(self.store, HOST, CFG).n, 0)

    def test_ads_with_moving_counter_are_traded(self):
        cfg = PaperConfig(**{**CFG.__dict__, "require_ad_shows_volume": True})
        for i in range(10):
            self.observe(self.t0 + i * 61, self.pair_ads(
                qty="5000", executedQuantity=str(100 + i * 10)))
        self.assertGreater(simulate(self.store, HOST, cfg).n, 0)


class TestExecutionRealism(PaperCase):
    def test_vanished_buy_leg_yields_no_profit(self):
        cfg = PaperConfig(**{**CFG.__dict__, "execution_delay_sec": 120})
        self.observe(self.t0, self.pair_ads())
        self.observe(self.t0 + 61, [])            # книга опустела
        self.store.close_absent(HOST, "1", set(), self.t0 + 61)
        self.store.close_absent(HOST, "0", set(), self.t0 + 61)
        r = simulate(self.store, HOST, cfg)
        self.assertTrue(all(t.pnl_kzt == 0 for t in r.trades))

    def test_no_entry_below_threshold(self):
        for i in range(5):
            self.observe(self.t0 + i * 61, self.pair_ads(sell="480.50"))  # 0.1%
        self.assertEqual(simulate(self.store, HOST, CFG).n, 0)

    def test_pnl_matches_hand_calculation(self):
        cfg = PaperConfig(**{**CFG.__dict__, "roundtrip_sec": 60})
        for i in range(3):
            self.observe(self.t0 + i * 61, self.pair_ads("480.00", "490.00"))
        r = simulate(self.store, HOST, cfg)
        self.assertTrue(r.trades)
        t = r.trades[0]
        expected = Decimal("300000") / Decimal("480") * Decimal("490") - 300000
        self.assertAlmostEqual(float(t.pnl_kzt), float(expected), places=2)

    def test_deterministic(self):
        for i in range(5):
            self.observe(self.t0 + i * 61, self.pair_ads())
        a, b = simulate(self.store, HOST, CFG), simulate(self.store, HOST, CFG)
        self.assertEqual(a.total_pnl_kzt, b.total_pnl_kzt)
        self.assertEqual(a.n, b.n)

    def test_no_data_yields_empty_result(self):
        r = simulate(self.store, HOST, CFG)
        self.assertEqual(r.n, 0)
        self.assertEqual(r.total_pnl_kzt, Decimal(0))


class TestCompounding(PaperCase):
    """Торгуем фиксированной ДОЛЕЙ капитала, а не фиксированной суммой."""

    def cfg(self, **over):
        return PaperConfig(**{**CFG.__dict__, "compound": True,
                              "roundtrip_sec": 60, **over})

    def test_position_grows_after_profit(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(12):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, self.cfg())
        self.assertGreater(len(r.trades), 2)
        sizes = [t.amount_kzt for t in r.trades]
        self.assertEqual(sizes, sorted(sizes))            # монотонный рост
        self.assertGreater(sizes[-1], sizes[0])

    def test_position_is_always_30_percent_of_current_capital(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(8):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, self.cfg())
        capital = Decimal("1000000")
        for t in r.trades:
            self.assertEqual(t.amount_kzt, capital * Decimal("30") / 100)
            capital += t.pnl_kzt

    def test_final_capital_equals_start_plus_pnl(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(8):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, self.cfg())
        self.assertEqual(r.final_capital_kzt,
                         r.start_capital_kzt + r.total_pnl_kzt)

    def test_fixed_mode_keeps_position_constant(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(8):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, self.cfg(compound=False))
        self.assertEqual({t.amount_kzt for t in r.trades}, {Decimal("300000")})

    def test_equity_curve_is_recorded(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(8):
            self.observe(self.t0 + i * 61, ads)
        r = simulate(self.store, HOST, self.cfg())
        self.assertEqual(len(r.equity), 8)
        self.assertEqual(r.equity[0].capital_kzt, Decimal("1000000"))

    def test_drawdown_is_never_positive(self):
        ads = self.pair_ads("480.00", "490.00", qty="50000")
        for i in range(8):
            self.observe(self.t0 + i * 61, ads)
        self.assertLessEqual(simulate(self.store, HOST, self.cfg()).max_drawdown_pct,
                             Decimal(0))


if __name__ == "__main__":
    unittest.main()
