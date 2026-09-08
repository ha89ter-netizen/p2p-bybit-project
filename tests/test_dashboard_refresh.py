"""Свежесть данных панели.

Пересчёт журналов дорожает вместе с историей: ~32 с на 29 часах, около
13 минут на месяце. При фиксированной паузе в 3 минуты поток на седьмые
сутки закрутился бы без остановки и держал бы процессор занятым всегда.
"""

import unittest


class TestAdaptivePause(unittest.TestCase):
    def _pause(self, took):
        from dashboard.server import (_LEDGER_DUTY, _LEDGER_MAX_PAUSE,
                                      _LEDGER_MIN_PAUSE)
        return min(_LEDGER_MAX_PAUSE, max(_LEDGER_MIN_PAUSE, took * _LEDGER_DUTY))

    def test_short_computation_keeps_the_floor(self):
        self.assertEqual(self._pause(5), 180.0)

    def test_long_computation_stretches_the_pause(self):
        """13-минутный пересчёт не имеет права идти каждые 3 минуты."""
        took = 13 * 60
        self.assertGreater(self._pause(took), took)

    def test_pause_is_capped(self):
        """Потолок есть, но он не должен ломать гарантию занятости:
        при часовом потолке получасовой пересчёт давал бы 33%."""
        from dashboard.server import _LEDGER_MAX_PAUSE
        self.assertEqual(self._pause(10 ** 6), _LEDGER_MAX_PAUSE)
        self.assertGreaterEqual(_LEDGER_MAX_PAUSE, 5 * 72 * 60)

    def test_duty_cycle_stays_bounded(self):
        """Доля занятого процессора ограничена при любой длительности."""
        for took in (1, 30, 120, 600, 1800):
            with self.subTest(took=took):
                duty = took / (took + self._pause(took))
                self.assertLessEqual(duty, 0.20, f"{took} с: занято {duty:.0%}")


class TestStaleness(unittest.TestCase):
    def test_fresh_cache_is_not_stale(self):
        import time
        from dashboard import server
        server._LEDGERS.update(data={"x": 1}, at=time.time(),
                               took=30.0, next_in=180.0, error=None)
        self.assertFalse(server.ledgers()["stale"])

    def test_frozen_worker_is_reported_as_stale(self):
        """Молчаливо показывать старые числа как свежие нельзя."""
        import time
        from dashboard import server
        server._LEDGERS.update(data={"x": 1}, at=time.time() - 3600,
                               took=30.0, next_in=180.0, error=None)
        self.assertTrue(server.ledgers()["stale"])

    def test_pending_state_before_first_run(self):
        from dashboard import server
        saved = server._LEDGERS.get("data")
        server._LEDGERS["data"] = None
        try:
            self.assertTrue(server.ledgers()["pending"])
        finally:
            server._LEDGERS["data"] = saved
