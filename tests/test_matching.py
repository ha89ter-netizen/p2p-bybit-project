import unittest
from decimal import Decimal

from analysis.matching import eligible, evaluate, find_pairs, best_pair, is_hard_rejected
from config.settings import QualityStratum
from tests.fixtures import make_ad

A300 = Decimal("300000")


class TestLimitMatching(unittest.TestCase):
    def test_ad_below_max_limit_is_useless(self):
        """Самая дешёвая реклама с лимитом 80k бесполезна для 300k."""
        cheap = make_ad(price="470.00", maxAmount="80000.00")
        self.assertFalse(cheap.supports(A300))
        self.assertEqual(eligible([cheap], A300, "1"), [])

    def test_ad_above_min_limit_is_useless(self):
        big = make_ad(minAmount="990000.00", maxAmount="2000000.00")
        self.assertFalse(big.supports(A300))

    def test_boundaries_are_inclusive(self):
        exact = make_ad(minAmount="300000.00", maxAmount="300000.00")
        self.assertTrue(exact.supports(A300))


class TestLiquidityMatching(unittest.TestCase):
    def test_insufficient_quantity_blocks_fill(self):
        """Лимиты подходят, но USDT на объявлении не хватает на 300k."""
        thin = make_ad(quantity="100")          # 100 * 480 = 48 000 KZT
        self.assertEqual(thin.liquidity_kzt, Decimal("48000.00"))
        self.assertFalse(thin.supports(A300))

    def test_liquidity_is_not_read_from_original_quantity(self):
        """Регрессия: ликвидность считалась по quantity, и проверка была
        почти бутафорской — в живой книге 10 из 12 объявлений имеют
        quantity != lastQuantity."""
        ad = make_ad(quantity="100000", lastQuantity="10", price="480.00")
        self.assertFalse(ad.supports(A300))

    def test_partially_executed_ad_uses_remaining_quantity(self):
        """quantity — ИСХОДНЫЙ объём, доступен только lastQuantity.
        Живой пример: quantity=131203.74, executed=128723.66, last=2480.08 —
        счёт по quantity завысил бы ликвидность в 53 раза."""
        ad = make_ad(quantity="131203.7366", executedQuantity="128723.6566",
                     lastQuantity="2480.08", price="480.00",
                     maxAmount="1190438.40")
        self.assertEqual(ad.available_quantity, Decimal("2480.08"))
        self.assertEqual(ad.liquidity_kzt, Decimal("2480.08") * Decimal("480.00"))
        self.assertTrue(ad.supports(A300))

    def test_nearly_exhausted_ad_cannot_fill(self):
        """Лимиты объявления подходят, но остатка уже не хватает."""
        ad = make_ad(quantity="5000", executedQuantity="4900",
                     lastQuantity="100", price="480.00")
        self.assertEqual(ad.liquidity_kzt, Decimal("48000.00"))
        self.assertFalse(ad.supports(A300))


class TestPaymentMatching(unittest.TestCase):
    def test_no_common_payment_is_hard_rejection(self):
        b = make_ad(ad_id="b", side="1", nick="b", payments=["150"])
        s = make_ad(ad_id="s", side="0", nick="s", payments=["203"], price="490.00")
        pair = evaluate(b, s, A300)
        self.assertIn("no_common_payment", pair.rejection_reasons)
        self.assertTrue(is_hard_rejected(pair))

    def test_common_payment_is_reported(self):
        b = make_ad(ad_id="b", side="1", nick="b", payments=["150", "203"])
        s = make_ad(ad_id="s", side="0", nick="s", payments=["203", "549"],
                    price="490.00")
        pair = evaluate(b, s, A300)
        self.assertEqual(pair.common_payments, ("203",))
        self.assertFalse(is_hard_rejected(pair))


class TestSameCounterparty(unittest.TestCase):
    def test_single_maker_market_yields_nothing(self):
        """Ровно случай bybit.kz: один мейкер по обе стороны -> пар нет."""
        b = make_ad(ad_id="b", side="1", nick="SkyBridge", price="479.99")
        s = make_ad(ad_id="s", side="0", nick="SkyBridge", price="450.59")
        self.assertEqual(find_pairs([b, s], A300), [])
        self.assertIsNone(best_pair([b, s], A300))


class TestPairSearch(unittest.TestCase):
    def test_best_executable_is_not_best_price(self):
        """Лучшая цена с негодным лимитом не должна побеждать."""
        cheap_useless = make_ad(ad_id="1", side="1", nick="a",
                                price="460.00", maxAmount="80000.00")
        usable = make_ad(ad_id="2", side="1", nick="b", price="475.00")
        sell = make_ad(ad_id="3", side="0", nick="c", price="490.00")
        best = best_pair([cheap_useless, usable, sell], A300)
        self.assertIsNotNone(best)
        self.assertEqual(best.buy_ad.ad_id, "2")

    def test_quality_stratum_filters_both_legs(self):
        b = make_ad(ad_id="1", side="1", nick="a", price="475.00",
                    finishNum=10, recentOrderNum=10)
        s = make_ad(ad_id="2", side="0", nick="b", price="490.00",
                    finishNum=5000, recentOrderNum=5000)
        strong = QualityStratum("good", min_ad_finish_num=50,
                                min_recent_orders=200, min_execute_rate=97)
        self.assertIsNone(best_pair([b, s], A300, stratum=strong))
        self.assertIsNotNone(best_pair([b, s], A300))

    def test_fresh_ad_of_veteran_advertiser_is_not_confused_with_newbie(self):
        """finishNum — зрелость ОБЪЯВЛЕНИЯ, recentOrderNum — репутация
        ЧЕЛОВЕКА. Опытный мерчант, переставивший объявление, не должен
        отсеиваться как новичок, если страта требует только репутацию."""
        veteran_fresh_ad = make_ad(ad_id="1", side="1", nick="a", price="475.00",
                                   finishNum=3, orderNum=3, recentOrderNum=1107)
        partner = make_ad(ad_id="2", side="0", nick="b", price="490.00",
                          finishNum=900, orderNum=910, recentOrderNum=900)
        reputation_only = QualityStratum("rep", min_ad_finish_num=0,
                                         min_recent_orders=500, min_execute_rate=97)
        self.assertIsNotNone(best_pair([veteran_fresh_ad, partner], A300,
                                       stratum=reputation_only))
        mature_ads_only = QualityStratum("mature", min_ad_finish_num=100,
                                         min_recent_orders=0, min_execute_rate=0)
        self.assertIsNone(best_pair([veteran_fresh_ad, partner], A300,
                                    stratum=mature_ads_only))

    def test_stale_data_is_excluded(self):
        b = make_ad(ad_id="1", side="1", nick="a", price="475.00")
        s = make_ad(ad_id="2", side="0", nick="b", price="490.00")
        self.assertIsNone(best_pair([b, s], A300, now=1000.0 + 600))
        self.assertIsNotNone(best_pair([b, s], A300, now=1000.0 + 10))

    def test_restrictive_prefs_warn_but_do_not_hard_reject(self):
        b = make_ad(ad_id="1", side="1", nick="a", price="475.00",
                    prefs={"orderFinishNumberDay30": 60})
        s = make_ad(ad_id="2", side="0", nick="b", price="490.00")
        pair = evaluate(b, s, A300)
        self.assertIn("restrictive_prefs__unverifiable", pair.rejection_reasons)
        self.assertFalse(is_hard_rejected(pair))


if __name__ == "__main__":
    unittest.main()


class TestThresholdsAgreeWithTheInterval(unittest.TestCase):
    """Пороги времени обязаны согласовываться с частотой опроса.

    При подъёме интервала с 20 до 120 с порог свежести в 90 с стал меньше
    интервала: между двумя обходами вся книга считалась бы протухшей.
    Конфликт был тихим, потому что фильтр сейчас не вызывается — первый
    же вызов с `now` отфильтровал бы всё до нуля.
    """

    def test_freshness_exceeds_the_poll_interval(self):
        from config.settings import COLLECTOR, MATCHING
        self.assertGreater(MATCHING.max_data_age_sec, COLLECTOR.poll_interval_sec,
                           "порог свежести не может быть меньше интервала опроса")

    def test_freshness_leaves_room_for_a_missed_cycle(self):
        """Один пропущенный обход не должен обнулять книгу."""
        from config.settings import COLLECTOR, MATCHING
        self.assertGreaterEqual(MATCHING.max_data_age_sec,
                                COLLECTOR.poll_interval_sec * 2)

    def test_filter_drops_stale_ads_when_now_is_given(self):
        from analysis.matching import eligible
        from config.settings import MATCHING
        from tests.fixtures import make_ad
        ad = make_ad(ad_id="A", side="1")          # observed_at = 1000.0
        fresh = eligible([ad], Decimal("300000"), "1", now=1000.0)
        stale = eligible([ad], Decimal("300000"), "1",
                         now=1000.0 + MATCHING.max_data_age_sec + 1)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(len(stale), 0)
