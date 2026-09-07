"""Оптимизация матчинга не должна менять ответ.

find_pairs переписан с наивного двойного цикла на ленивое слияние кучей
(иначе отчёт по 72 часам данных не досчитывается). Эти тесты доказывают,
что результат совпадает с наивным перебором.
"""
import itertools
import random
import unittest
from decimal import Decimal

from analysis.matching import (best_pair, evaluate, find_pairs,
                               iter_pairs_by_spread)
from config.settings import MATCHING
from tests.fixtures import make_ad

A300 = Decimal("300000")


def brute_force(ads, amount=A300):
    """Заведомо правильная, заведомо медленная реализация."""
    buys = [a for a in ads if a.side == "1"]
    sells = [a for a in ads if a.side == "0"]
    out = []
    for b, s in itertools.product(buys, sells):
        pair = evaluate(b, s, amount)
        if pair.executable and pair.gross_spread_pct >= MATCHING.min_gross_spread_pct:
            out.append(pair)
    out.sort(key=lambda p: p.gross_spread_pct, reverse=True)
    return out


def random_book(rng, n_buy=12, n_sell=15):
    ads = []
    for i in range(n_buy):
        ads.append(make_ad(ad_id=f"b{i}", side="1", nick=f"b{i}",
                           price=f"{rng.uniform(460, 500):.2f}",
                           quantity=str(rng.choice([50, 700, 5000])),
                           minAmount=str(rng.choice([50000, 100000, 300000])),
                           maxAmount=str(rng.choice([200000, 500000, 2000000])),
                           payments=rng.sample(["150", "203", "549"],
                                               rng.randint(1, 3))))
    for i in range(n_sell):
        ads.append(make_ad(ad_id=f"s{i}", side="0", nick=f"s{i}",
                           price=f"{rng.uniform(460, 510):.2f}",
                           quantity=str(rng.choice([50, 700, 5000])),
                           minAmount=str(rng.choice([50000, 100000, 300000])),
                           maxAmount=str(rng.choice([200000, 500000, 2000000])),
                           payments=rng.sample(["150", "203", "549"],
                                               rng.randint(1, 3))))
    return ads


class TestPairOrdering(unittest.TestCase):
    def test_matches_brute_force_on_random_books(self):
        for seed in range(40):
            rng = random.Random(seed)
            ads = random_book(rng)
            fast = find_pairs(ads, A300)
            slow = brute_force(ads)
            self.assertEqual(len(fast), len(slow), f"seed={seed}")
            self.assertEqual([str(p.gross_spread_pct) for p in fast],
                             [str(p.gross_spread_pct) for p in slow],
                             f"seed={seed}")

    def test_best_pair_matches_brute_force(self):
        for seed in range(40):
            rng = random.Random(seed)
            ads = random_book(rng)
            fast, slow = best_pair(ads, A300), brute_force(ads)
            if not slow:
                self.assertIsNone(fast, f"seed={seed}")
                continue
            self.assertEqual(fast.gross_spread_pct, slow[0].gross_spread_pct,
                             f"seed={seed}")

    def test_yields_in_strictly_descending_order(self):
        rng = random.Random(7)
        spreads = [p.gross_spread_pct
                   for p in iter_pairs_by_spread(random_book(rng), A300)]
        self.assertEqual(spreads, sorted(spreads, reverse=True))

    def test_limit_returns_prefix_of_full_result(self):
        rng = random.Random(11)
        ads = random_book(rng)
        full = find_pairs(ads, A300)
        self.assertEqual([str(p.gross_spread_pct) for p in find_pairs(ads, A300, limit=5)],
                         [str(p.gross_spread_pct) for p in full[:5]])

    def test_lazy_iteration_stops_early(self):
        """best_pair не должен перебирать всю книгу — на этом держится
        обсчёт 72 часов данных за секунды, а не за часы."""
        rng = random.Random(3)
        ads = random_book(rng, n_buy=40, n_sell=60)
        evaluated = 0
        it = iter_pairs_by_spread(ads, A300)
        for _ in it:
            evaluated += 1
            break
        self.assertLessEqual(evaluated, 1)
        self.assertGreater(len(find_pairs(ads, A300)), evaluated)

    def test_empty_side_yields_nothing(self):
        only_buys = [make_ad(ad_id="b", side="1", nick="b")]
        self.assertEqual(find_pairs(only_buys, A300), [])
        self.assertIsNone(best_pair(only_buys, A300))

    def test_zero_price_ad_is_skipped_not_crash(self):
        bad = make_ad(ad_id="b", side="1", nick="b", price="0")
        good = make_ad(ad_id="s", side="0", nick="s", price="490.00")
        self.assertIsNone(best_pair([bad, good], A300))


class TestSchemaGuard(unittest.TestCase):
    def test_stale_database_is_rejected_loudly(self):
        import sqlite3, tempfile
        from pathlib import Path
        from storage.db import SchemaMismatch, Store
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "old.sqlite")
            sqlite3.connect(path).executescript(
                "CREATE TABLE ad_state (ad_id TEXT, observed_at REAL);"
                "CREATE TABLE advertiser_state (key TEXT, observed_at REAL);")
            with self.assertRaises(SchemaMismatch):
                Store(path)


if __name__ == "__main__":
    unittest.main()
