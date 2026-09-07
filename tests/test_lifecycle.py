"""Жизненный цикл объявления и устойчивость к пропускам сбора.

Это самые важные тесты в проекте: если presence-логика врёт, то врёт и
главная метрика исследования — persistence.
"""
import tempfile
import unittest
from pathlib import Path

from storage.db import Store
from tests.fixtures import make_ad


class TestAdLifecycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "t.sqlite"))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _presence(self, ad_id):
        return self.store.conn.execute(
            "SELECT appeared_at, disappeared_at FROM ad_presence WHERE ad_id=?"
            " ORDER BY appeared_at", (ad_id,)).fetchall()

    def test_new_ad_opens_presence_interval(self):
        ad = make_ad(ad_id="A")
        self.store.upsert_ads([ad])
        rows = self._presence("A")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["disappeared_at"])

    def test_unchanged_ad_writes_no_duplicate_state(self):
        """Главная защита от раздувания БД: 100 одинаковых наблюдений -> 1 строка."""
        for t in range(100):
            ad = make_ad(ad_id="A")
            object.__setattr__(ad, "observed_at", 1000.0 + t)
            self.store.upsert_ads([ad])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM ad_state WHERE ad_id='A'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_fingerprint_matches_stored_columns(self):
        """Защита от рассинхрона Ad.state_fingerprint() и SELECT в upsert_ads:
        именно он молча отключил дедупликацию и дал 100 строк вместо 1."""
        ad = make_ad(ad_id="A")
        self.store.upsert_ads([ad])
        row = self.store.conn.execute(
            "SELECT price, quantity, frozen_qty, min_amount, max_amount,"
            " version, status, finish_num, order_num, last_qty, executed_qty"
            " FROM ad_state WHERE ad_id='A'").fetchone()
        self.assertEqual(len(tuple(row)), len(ad.state_fingerprint()))
        self.assertEqual(tuple(row), ad.state_fingerprint())

    def test_partial_fill_writes_new_state(self):
        """Выкуп части объявления — событие рынка, а не шум: именно оно
        отличает активно торгуемое объявление от статичной витрины."""
        self.store.upsert_ads([make_ad(ad_id="A", quantity="1000",
                                       lastQuantity="1000")])
        a2 = make_ad(ad_id="A", quantity="1000", lastQuantity="400",
                     executedQuantity="600")
        object.__setattr__(a2, "observed_at", 2000.0)
        self.store.upsert_ads([a2])
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM ad_state WHERE ad_id='A'").fetchone()[0], 2)

    def test_price_change_writes_new_state(self):
        a1 = make_ad(ad_id="A", price="480.00")
        self.store.upsert_ads([a1])
        a2 = make_ad(ad_id="A", price="481.00")
        object.__setattr__(a2, "observed_at", 1060.0)
        self.store.upsert_ads([a2])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM ad_state WHERE ad_id='A'").fetchone()[0]
        self.assertEqual(n, 2)

    def test_disappearance_closes_interval(self):
        self.store.upsert_ads([make_ad(ad_id="A")])
        gone = self.store.close_absent("api2.bybit.com", "1", set(), 2000.0)
        self.assertEqual(gone, 1)
        self.assertEqual(self._presence("A")[0]["disappeared_at"], 2000.0)

    def test_reappearance_opens_second_interval(self):
        """Объявление сняли и переставили — это два интервала, не один."""
        self.store.upsert_ads([make_ad(ad_id="A")])
        self.store.close_absent("api2.bybit.com", "1", set(), 2000.0)
        self.store.reopen_after_gap("api2.bybit.com", "1", {"A"}, 3000.0)
        rows = self._presence("A")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["disappeared_at"], 2000.0)
        self.assertIsNone(rows[1]["disappeared_at"])

    def test_incomplete_poll_must_not_kill_ads(self):
        """Ядро защиты данных на ноутбуке: при неполном обходе close_absent
        не вызывается вовсе, и объявление остаётся живым."""
        self.store.upsert_ads([make_ad(ad_id="A")])
        # имитируем цикл, в котором fetch_book вернул complete=False:
        # poller в этом случае просто не трогает presence
        rows = self._presence("A")
        self.assertIsNone(rows[0]["disappeared_at"])

    def test_advertiser_state_deduplicated(self):
        for t in range(50):
            ad = make_ad(ad_id="A")
            object.__setattr__(ad, "observed_at", 1000.0 + t)
            self.store.upsert_ads([ad])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM advertiser_state").fetchone()[0]
        self.assertEqual(n, 1)

    def test_finish_num_is_stored_per_ad_not_per_advertiser(self):
        """finishNum — счётчик ОБЪЯВЛЕНИЯ. Один контрагент держит два
        объявления на одной стороне со 100 и 24 сделками при общем
        recentOrderNum=1107 (проверено на живых данных api2.bybit.com).
        Если хранить его как атрибут человека, ряды затирают друг друга
        каждый опрос, а страта качества мерит возраст объявления."""
        a1 = make_ad(ad_id="A1", side="1", nick="same",
                     finishNum=100, orderNum=100, recentOrderNum=1107)
        a2 = make_ad(ad_id="A2", side="1", nick="same",
                     finishNum=24, orderNum=24, recentOrderNum=1107)
        for t in range(10):
            for ad in (a1, a2):
                object.__setattr__(ad, "observed_at", 1000.0 + t)
                self.store.upsert_ads([ad])
        per_ad = dict(self.store.conn.execute(
            "SELECT ad_id, finish_num FROM ad_state").fetchall())
        self.assertEqual(per_ad, {"A1": 100, "A2": 24})
        # профиль контрагента при этом стабилен: одна строка, без дребезга
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM advertiser_state").fetchone()[0], 1)

    def test_advertiser_state_tracks_reputation_change(self):
        self.store.upsert_ads([make_ad(ad_id="A", recentOrderNum=500)])
        a2 = make_ad(ad_id="A", recentOrderNum=501)
        object.__setattr__(a2, "observed_at", 2000.0)
        self.store.upsert_ads([a2])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM advertiser_state").fetchone()[0]
        self.assertEqual(n, 2)

    def test_ad_counter_growth_writes_ad_state_not_advertiser_state(self):
        """Сделка на объявлении двигает ad_state, а не профиль контрагента."""
        self.store.upsert_ads([make_ad(ad_id="A", finishNum=500, orderNum=500)])
        a2 = make_ad(ad_id="A", finishNum=501, orderNum=501)
        object.__setattr__(a2, "observed_at", 2000.0)
        self.store.upsert_ads([a2])
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM ad_state").fetchone()[0], 2)
        self.assertEqual(self.store.conn.execute(
            "SELECT COUNT(*) FROM advertiser_state").fetchone()[0], 1)

    def test_no_state_churn_when_advertiser_trades_both_sides(self):
        """Регрессия: 204 контрагента давали 826 строк за 5 циклов."""
        buy = make_ad(ad_id="B", side="1", nick="same", finishNum=246)
        sell = make_ad(ad_id="S", side="0", nick="same", finishNum=23)
        for t in range(20):
            for ad in (buy, sell):
                object.__setattr__(ad, "observed_at", 1000.0 + t)
                self.store.upsert_ads([ad])
        n = self.store.conn.execute(
            "SELECT COUNT(*) FROM advertiser_state").fetchone()[0]
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
