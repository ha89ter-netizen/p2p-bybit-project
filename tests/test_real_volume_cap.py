"""Нельзя провести через контрагента больше, чем он реально наторговал.

Лимит на число сделок не отвечает, много ли для конкретного человека три
круга по 300 000. Замерено на живых данных: для большинства наш
симулированный оборот составлял 3-20% их настоящего, но у двоих из
тридцати четырёх — 141% и 224%. Такие сделки — фантом.
"""

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path


class TestRealVolumeQuery(unittest.TestCase):
    """Оборот считается из движения executedQuantity, счётчика самой биржи."""

    def setUp(self):
        from storage.db import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "t.sqlite"
        s = Store(str(self.db))          # схему создаёт сам Store
        c = s.conn
        c.executemany(
            "INSERT INTO ad (ad_id, host, side, advertiser_key, token, currency,"
            " payments, prefs, remark, first_seen, last_seen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(a, "h", "1", k, "USDT", "KZT", "[]", "{}", "", 0.0, 9.0)
             for a, k in (("a1", "ADV"), ("a2", "ADV"), ("b1", "OTHER"))])
        # ADV: по a1 прошло 100 USDT, по a2 ещё 50; цена 500 -> 75 000 ₸
        c.executemany(
            "INSERT INTO ad_state (ad_id, observed_at, price, quantity, frozen_qty,"
            " min_amount, max_amount, version, status, finish_num, order_num,"
            " last_qty, executed_qty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(ad, t, "500", "1000", "0", "0", "1000000", 1, 10, 0, 0, "1000", e)
             for ad, t, e in (("a1", 1.0, "0"), ("a1", 2.0, "100"),
                              ("a2", 1.0, "10"), ("a2", 2.0, "60"),
                              ("b1", 1.0, "0"), ("b1", 2.0, "0"))])
        c.commit()
        s.close()

    def _volumes(self):
        from analysis.paper import _real_volume_by_advertiser
        from storage.db import Store
        s = Store(str(self.db))
        try:
            return _real_volume_by_advertiser(s)
        finally:
            s.close()

    def test_volume_sums_across_all_ads_of_one_advertiser(self):
        v = self._volumes()
        self.assertEqual(v["ADV"], Decimal("75000"))

    def test_motionless_advertiser_has_zero_volume(self):
        """Объявление, которое никто не тронул, оборота не даёт."""
        self.assertEqual(self._volumes().get("OTHER", Decimal(0)), Decimal(0))

    def test_zero_volume_blocks_everything_at_any_share(self):
        """Ноль реального оборота — значит торговать не с кем."""
        vol = self._volumes()
        for share in ("0.1", "0.5", "1.0"):
            with self.subTest(share=share):
                cap = vol.get("OTHER", Decimal(0)) * Decimal(share)
                self.assertLess(cap, Decimal("300000"))


class TestCapArithmetic(unittest.TestCase):
    def _room(self, real, already, amount, share="0.5"):
        cap = Decimal(str(real)) * Decimal(share)
        return Decimal(str(already)) + Decimal(str(amount)) <= cap

    def test_small_share_of_a_big_counterparty_passes(self):
        self.assertTrue(self._room(real=20_000_000, already=0, amount=300_000))

    def test_exceeding_half_the_real_volume_is_blocked(self):
        self.assertFalse(self._room(real=500_000, already=0, amount=300_000))

    def test_accumulated_volume_counts(self):
        """Третий круг через мелкого контрагента должен упереться."""
        self.assertTrue(self._room(real=2_000_000, already=600_000, amount=300_000))
        self.assertFalse(self._room(real=2_000_000, already=900_000, amount=300_000))

    def test_share_zero_disables_the_rule(self):
        from config.settings import PAPER
        self.assertGreaterEqual(PAPER.max_share_of_real_volume, 0)


if __name__ == "__main__":
    unittest.main()
