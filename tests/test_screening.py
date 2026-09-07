"""Фильтры исполнимости и реинвестирование капитала."""
import unittest
from decimal import Decimal

from analysis.screening import (Blacklist, MyProfile, Requirements,
                                blocking_reasons, pair_conflicts, price_outlier)
from analysis.matching import evaluate
from tests.fixtures import make_ad

A300 = Decimal("300000")


class TestRequirements(unittest.TestCase):
    def test_receipt_demand_detected(self):
        r = Requirements.parse("С Каспи на Каспи после оплаты чек в чат")
        self.assertTrue(r.wants_receipt)
        self.assertFalse(r.refuses_receipt)

    def test_receipt_refusal_is_not_a_demand(self):
        """«Чеки не предоставляю» содержит слово «чек» — наивная регулярка
        засчитала бы это как требование чека, то есть ровно наоборот."""
        r = Requirements.parse("Отправляю по номеру карты. Чеки не предоставляю")
        self.assertTrue(r.refuses_receipt)
        self.assertFalse(r.wants_receipt)

    def test_first_person_variants(self):
        for txt in ("Принимаю только от 1х лиц", "работаю с первыми лицами",
                    "только однофамильцы", "перевод от владельца карты"):
            self.assertTrue(Requirements.parse(txt).first_person_only, txt)

    def test_empty_remark_demands_nothing(self):
        r = Requirements.parse("")
        self.assertFalse(any([r.wants_receipt, r.refuses_receipt,
                              r.first_person_only, r.exact_amount]))


class TestPairConflicts(unittest.TestCase):
    def test_receipt_chain_broken(self):
        """Реальный случай из данных: IZZI23 требует чек, KORAZON не даёт.
        Метод оплаты у обоих Kaspi, обычный матчер пару пропускает."""
        buy = make_ad(ad_id="b", side="1", nick="IZZI23", payments=["150"],
                      remark="Принимаю только от 1х лиц. После оплаты чек в чат")
        sell = make_ad(ad_id="s", side="0", nick="KORAZON", payments=["150"],
                       price="490.00", remark="Отправляю по номеру карты. Чеки не предоставляю")
        pair = evaluate(buy, sell, A300)
        self.assertTrue(pair.executable)          # обычный матчер пропускает
        self.assertIn("receipt_chain_broken", pair_conflicts(pair))

    def test_compatible_pair_has_no_conflicts(self):
        buy = make_ad(ad_id="b", side="1", nick="b", payments=["150"], remark="")
        sell = make_ad(ad_id="s", side="0", nick="s", payments=["150"],
                       price="490.00", remark="Оплачиваю сразу")
        self.assertEqual(pair_conflicts(evaluate(buy, sell, A300)), ())


class TestBlockingReasons(unittest.TestCase):
    def test_new_account_blocked_by_order_requirement(self):
        ad = make_ad(prefs={"orderFinishNumberDay30": 60})
        self.assertTrue(blocking_reasons(ad, MyProfile(completed_orders_30d=0)))
        self.assertFalse(blocking_reasons(ad, MyProfile(completed_orders_30d=60)))

    def test_completion_rate_requirement(self):
        ad = make_ad(prefs={"completeRateDay30": 95})
        self.assertTrue(blocking_reasons(ad, MyProfile(completion_rate_30d=90)))
        self.assertFalse(blocking_reasons(ad, MyProfile(completion_rate_30d=98)))

    def test_receipt_requirement_when_we_cannot_provide(self):
        ad = make_ad(remark="обязательно чек в чат")
        self.assertIn("receipt_required",
                      blocking_reasons(ad, MyProfile(can_provide_receipt=False)))

    def test_clean_ad_blocks_nobody(self):
        self.assertEqual(blocking_reasons(make_ad(remark=""), MyProfile()), ())


class TestPriceOutlier(unittest.TestCase):
    def test_suspiciously_cheap_buy_is_flagged(self):
        """Цена сильно ЛУЧШЕ рынка — приманка, а не удача."""
        ad = make_ad(side="1", price="440.00")
        self.assertTrue(price_outlier(ad, Decimal("480"), Decimal("8")))

    def test_suspiciously_expensive_sell_is_flagged(self):
        ad = make_ad(side="0", price="530.00")
        self.assertTrue(price_outlier(ad, Decimal("480"), Decimal("8")))

    def test_unfavourable_price_is_not_suspicious(self):
        """Цена в невыгодную сторону неинтересна, но о добросовестности
        ничего не говорит — флагом не помечается."""
        self.assertFalse(price_outlier(make_ad(side="1", price="530.00"),
                                       Decimal("480"), Decimal("8")))

    def test_normal_price_passes(self):
        self.assertFalse(price_outlier(make_ad(side="1", price="478.00"),
                                       Decimal("480"), Decimal("8")))


class TestBlacklist(unittest.TestCase):
    def test_persistent_non_trader_is_blocked(self):
        b = Blacklist(min_appearances=5)
        for _ in range(6):
            b.observe("dead", False)
        self.assertTrue(b.is_blocked("dead"))

    def test_one_real_trade_clears_suspicion(self):
        b = Blacklist(min_appearances=5)
        for i in range(10):
            b.observe("live", i == 3)
        self.assertFalse(b.is_blocked("live"))

    def test_rare_appearance_is_not_enough_evidence(self):
        b = Blacklist(min_appearances=20)
        for _ in range(3):
            b.observe("shy", False)
        self.assertFalse(b.is_blocked("shy"))


if __name__ == "__main__":
    unittest.main()
