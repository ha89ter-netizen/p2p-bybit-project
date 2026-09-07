"""Фильтр по банкам, счета в которых у нас реально есть.

До 07.09.2026 матчер пересекал методы оплаты только между двумя
объявлениями. Пара на банке, которым мы не пользуемся, считалась
исполнимой — то есть страта качества включала недостижимые пары.
"""

import unittest
from dataclasses import replace
from decimal import Decimal

from analysis.matching import evaluate
from config.settings import MATCHING
from tests.fixtures import make_ad

KASPI, HALYK, FREEDOM, JUSAN = "150", "203", "549", "377"
MINE = frozenset({KASPI, HALYK, FREEDOM})
AMOUNT = Decimal("300000")


class TestMyPayments(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(MATCHING, my_payments=MINE)

    def _pair(self, buy_pays, sell_pays, cfg=None):
        buy = make_ad(ad_id="B", side="1", nick="b", price="480.00",
                      payments=list(buy_pays))
        sell = make_ad(ad_id="S", side="0", nick="s", price="490.00",
                       payments=list(sell_pays))
        return evaluate(buy, sell, AMOUNT, cfg=cfg or self.cfg)

    def test_pair_on_our_bank_is_executable(self):
        p = self._pair([KASPI, JUSAN], [KASPI])
        self.assertNotIn("no_payment_we_hold", p.rejection_reasons)
        self.assertTrue(p.executable)

    def test_pair_on_bank_we_lack_is_rejected(self):
        """Обе ноги на Jusan: пересечение есть, счёта у нас нет."""
        p = self._pair([JUSAN], [JUSAN])
        self.assertIn("no_payment_we_hold", p.rejection_reasons)
        self.assertFalse(p.executable)

    def test_no_overlap_at_all_reports_the_other_reason(self):
        """Если ноги вообще не пересекаются, причина прежняя, не наша."""
        p = self._pair([KASPI], [HALYK])
        self.assertIn("no_common_payment", p.rejection_reasons)
        self.assertNotIn("no_payment_we_hold", p.rejection_reasons)

    def test_any_one_of_our_banks_is_enough(self):
        for bank in (KASPI, HALYK, FREEDOM):
            with self.subTest(bank=bank):
                p = self._pair([bank, JUSAN], [bank, JUSAN])
                self.assertNotIn("no_payment_we_hold", p.rejection_reasons)

    def test_empty_config_disables_the_check(self):
        """Пустой MY_PAYMENTS возвращает прежнее поведение исследования."""
        cfg = replace(MATCHING, my_payments=frozenset())
        p = self._pair([JUSAN], [JUSAN], cfg=cfg)
        self.assertNotIn("no_payment_we_hold", p.rejection_reasons)
        self.assertTrue(p.executable)


if __name__ == "__main__":
    unittest.main()
