"""Выбор моментов наблюдения и восстановление книги на момент времени.

Это фундамент честной симуляции: решение принимается по book_at(t),
результат оценивается по book_at(t+Δ). Ошибка здесь означает, что
всё исследование считает не то, и молча.
"""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from analysis.replay import book_at, observation_times
from storage.db import Store
from tests.fixtures import make_ad

HOST = "api2.bybit.com"


class ReplayCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.dir.name) / "t.sqlite"))

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def poll(self, side, t, complete=True):
        self.store.record_poll(started_at=t, finished_at=t + 1, host=HOST,
                               side=side, complete=complete, n_items=1,
                               n_pages=1, total_count=1, latency_ms=1, error=None)

    def ad_at(self, ad_id, side, t, price="480.00"):
        ad = make_ad(ad_id=ad_id, side=side, nick=ad_id, price=price)
        object.__setattr__(ad, "observed_at", t)
        self.store.upsert_ads([ad])


class TestObservationTimes(ReplayCase):
    def test_skips_moments_before_other_side_was_ever_polled(self):
        """Первый опрос side=1 идёт раньше первого опроса side=0. Брать для
        него наблюдение side=0 из будущего — look-ahead; на практике это
        давало момент с пустой половиной книги и нули в отчёте."""
        self.poll("1", 1000.0)
        self.poll("0", 1008.0)          # сторона "0" появилась ПОЗЖЕ
        self.poll("1", 1100.0)
        self.poll("0", 1108.0)
        times = observation_times(self.store, HOST, min_spacing_sec=60,
                                  max_skew_sec=300)
        self.assertNotIn(1000.0, times)
        self.assertEqual(times, [1100.0])

    def test_uses_previous_not_nearest_observation(self):
        self.poll("0", 1000.0)
        self.poll("1", 1050.0)
        self.poll("0", 1051.0)          # ближе, но в будущем относительно 1050
        times = observation_times(self.store, HOST, min_spacing_sec=60, max_skew_sec=60)
        self.assertEqual(times, [1050.0])

    def test_rejects_moment_when_other_side_is_too_stale(self):
        self.poll("0", 1000.0)
        self.poll("1", 9000.0)          # разрыв больше step_sec
        self.assertEqual(observation_times(self.store, HOST, min_spacing_sec=60, max_skew_sec=60), [])

    def test_incomplete_polls_are_excluded(self):
        self.poll("0", 1000.0)
        self.poll("1", 1010.0, complete=False)
        self.assertEqual(observation_times(self.store, HOST, min_spacing_sec=60, max_skew_sec=60), [])

    def test_respects_minimum_spacing(self):
        self.poll("0", 1000.0)
        for t in (1010.0, 1020.0, 1030.0, 1200.0):
            self.poll("1", t)
            self.poll("0", t + 1)
        times = observation_times(self.store, HOST, min_spacing_sec=60,
                                  max_skew_sec=300)
        self.assertEqual(times, [1010.0, 1200.0])

    def test_sparse_sampling_does_not_loosen_skew_tolerance(self):
        """Регрессия: step_sec был одновременно шагом выборки и допуском на
        рассинхрон сторон. Отчёт просил редкую выборку и молча получал
        право сшивать книгу из наблюдений 15-минутной давности."""
        self.poll("0", 1000.0)
        self.poll("1", 1010.0)
        self.poll("0", 1500.0)
        self.poll("1", 2000.0)      # сторона "0" отстала на 500 с
        times = observation_times(self.store, HOST, min_spacing_sec=900,
                                  max_skew_sec=60)
        self.assertEqual(times, [1010.0])

    def test_single_sided_history_yields_nothing(self):
        for t in (1000.0, 1100.0):
            self.poll("1", t)
        self.assertEqual(observation_times(self.store, HOST, min_spacing_sec=60, max_skew_sec=60), [])


class TestBookAt(ReplayCase):
    def test_no_look_ahead(self):
        """book_at физически не видит состояний из будущего."""
        self.ad_at("A", "1", 1000.0, price="480.00")
        self.ad_at("A", "1", 2000.0, price="490.00")
        self.assertEqual(book_at(self.store, HOST, 1500.0)[0].price,
                         Decimal("480.00"))
        self.assertEqual(book_at(self.store, HOST, 2500.0)[0].price,
                         Decimal("490.00"))

    def test_ad_absent_before_it_appeared(self):
        self.ad_at("A", "1", 2000.0)
        self.assertEqual(book_at(self.store, HOST, 1000.0), [])

    def test_ad_absent_after_it_disappeared(self):
        self.ad_at("A", "1", 1000.0)
        self.store.close_absent(HOST, "1", set(), 1500.0)
        self.assertEqual(len(book_at(self.store, HOST, 1200.0)), 1)
        self.assertEqual(book_at(self.store, HOST, 1600.0), [])

    def test_available_quantity_survives_round_trip(self):
        ad = make_ad(ad_id="A", side="1", nick="A", quantity="5000",
                     lastQuantity="120", executedQuantity="4880")
        self.store.upsert_ads([ad])
        restored = book_at(self.store, HOST, 1000.0)[0]
        self.assertEqual(restored.available_quantity, Decimal("120"))
        self.assertEqual(restored.quantity, Decimal("5000"))
        self.assertEqual(restored.liquidity_kzt, ad.liquidity_kzt)

    def test_advertiser_stats_survive_round_trip(self):
        ad = make_ad(ad_id="A", side="1", nick="A", finishNum=42,
                     orderNum=44, recentOrderNum=1107, recentExecuteRate=98)
        self.store.upsert_ads([ad])
        restored = book_at(self.store, HOST, 1000.0)[0]
        self.assertEqual(restored.finish_num, 42)
        self.assertEqual(restored.advertiser.recent_order_num, 1107)
        self.assertEqual(restored.advertiser.recent_execute_rate, 98)


if __name__ == "__main__":
    unittest.main()
