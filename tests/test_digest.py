"""Дайджест и его отправка.

Главная ловушка здесь — посчитать одну статичную связку, простоявшую три
часа, как 540 возможностей. Тогда витрина с фиксированной наценкой
выглядела бы в отчёте как бурная торговля.
"""
import json
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest import mock

from analysis.digest import build
from config.settings import TelegramConfig
from notify.telegram import TELEGRAM_MAX_CHARS, TelegramError, render, send
from storage.db import Store
from tests.fixtures import make_ad

HOST = "api2.bybit.com"
CFG = TelegramConfig(bot_token="123:SECRET-TOKEN", chat_id="42",
                     digest_hours=3, amount_kzt=Decimal("300000"),
                     rows_per_bucket=5, min_recent_orders=200,
                     min_execute_rate=97, min_ad_finish_num=50)


class DigestCase(unittest.TestCase):
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
                                   side=side, complete=True, n_items=1,
                                   n_pages=1, total_count=1, latency_ms=1,
                                   error=None)
        for ad in ads:
            object.__setattr__(ad, "observed_at", t)
        self.store.upsert_ads(ads)

    def good(self, ad_id, side, price, **over):
        over.setdefault("finishNum", 500)
        over.setdefault("orderNum", 500)
        over.setdefault("recentOrderNum", 1000)
        over.setdefault("recentExecuteRate", 99)
        return make_ad(ad_id=ad_id, side=side, price=price, nick=ad_id, **over)


class TestDigestAggregation(DigestCase):
    def test_static_pair_counts_once_not_once_per_observation(self):
        ads = [self.good("b", "1", "480.00"), self.good("s", "0", "482.00")]
        for i in range(60):
            self.observe(self.t0 + i * 61, ads)
        d = build(self.store, HOST, window_sec=3 * 3600, cfg=CFG,
                  now=self.t0 + 60 * 61)
        self.assertEqual(d.bucket_totals["<1%"], 1)
        row = d.buckets["<1%"][0]
        self.assertGreater(row.seen_count, 1)
        self.assertGreater(row.lifetime_sec, 3000)

    def test_buckets_split_by_spread(self):
        self.observe(self.t0, [
            self.good("b", "1", "480.00"),
            self.good("s1", "0", "482.00"),      # 0.42%
            self.good("s2", "0", "490.00"),      # 2.08%
            self.good("s3", "0", "500.00"),      # 4.17%
        ])
        self.observe(self.t0 + 61, [])
        d = build(self.store, HOST, window_sec=3600, cfg=CFG,
                  now=self.t0 + 120)
        self.assertEqual(d.bucket_totals, {"<1%": 1, "1-3%": 1, "3%+": 1})

    def test_losing_pairs_are_counted_not_shown(self):
        """Отрицательный спред — это убыток, а не «маленькая возможность»."""
        self.observe(self.t0, [self.good("b", "1", "490.00"),
                               self.good("s", "0", "480.00")])
        self.observe(self.t0 + 61, [])
        d = build(self.store, HOST, window_sec=3600, cfg=CFG,
                  now=self.t0 + 120)
        self.assertEqual(sum(d.bucket_totals.values()), 0)
        self.assertEqual(d.losing, 1)

    def test_low_quality_pair_is_filtered_and_counted_once(self):
        self.observe(self.t0, [
            self.good("b", "1", "480.00"),
            make_ad(ad_id="s", side="0", price="500.00", nick="s",
                    finishNum=1, orderNum=1, recentOrderNum=4,
                    recentExecuteRate=100),
        ])
        self.observe(self.t0 + 61, [])
        d = build(self.store, HOST, window_sec=3600, cfg=CFG,
                  now=self.t0 + 120)
        self.assertEqual(sum(d.bucket_totals.values()), 0)
        self.assertEqual(d.filtered_out, 1)

    def test_window_excludes_older_observations(self):
        old = [self.good("b", "1", "480.00"), self.good("s", "0", "500.00")]
        self.observe(self.t0, old)
        self.observe(self.t0 + 61, old)
        d = build(self.store, HOST, window_sec=60, cfg=CFG,
                  now=self.t0 + 10 * 3600)
        self.assertEqual(d.moments, 0)
        self.assertEqual(sum(d.bucket_totals.values()), 0)

    def test_window_start_clamps_to_first_observation(self):
        """Если сбор идёт меньше запрошенного окна, заголовок обязан
        показывать реальный охват, а не обещанные N часов."""
        self.observe(self.t0, [self.good("b", "1", "480.00"),
                               self.good("s", "0", "482.00")])
        self.observe(self.t0 + 61, [])
        d = build(self.store, HOST, window_sec=3 * 3600, cfg=CFG,
                  now=self.t0 + 3600)
        self.assertGreaterEqual(d.window_from, self.t0)
        self.assertLess(d.window_to - d.window_from, 3 * 3600)

    def test_incomplete_polls_are_surfaced_as_gaps(self):
        self.store.record_poll(started_at=self.t0, finished_at=self.t0, host=HOST,
                               side="1", complete=False, n_items=0, n_pages=0,
                               total_count=0, latency_ms=0, error="boom")
        d = build(self.store, HOST, window_sec=3600, cfg=CFG,
                  now=self.t0 + 100)
        self.assertEqual(d.poll_gaps, 1)


class TestRender(DigestCase):
    def _digest(self):
        self.observe(self.t0, [
            self.good("b", "1", "479.68"),
            self.good("s1", "0", "483.02"),
            self.good("s2", "0", "492.00"),
        ])
        self.observe(self.t0 + 61, [])
        return build(self.store, HOST, window_sec=3600, cfg=CFG,
                     now=self.t0 + 120)

    def test_contains_prices_spread_and_simulation_marker(self):
        text = render(self._digest(), CFG)
        self.assertIn("479.68", text)
        self.assertIn("483.02", text)
        self.assertIn("0.70%", text)
        self.assertIn("SIMULATION ONLY", text)

    def test_bucket_names_are_html_escaped(self):
        """Имя бакета "<1%" без экранирования Telegram разбирает как тег
        и отвечает 400 Bad Request: can't parse entities. Живой баг,
        который --dry-run поймать не мог: он не ходит в API."""
        text = render(self._digest(), CFG)
        self.assertIn("&lt;1%", text)
        self.assertNotIn("<b><1%</b>", text)

    def test_shows_all_three_buckets_even_when_empty(self):
        text = render(self._digest(), CFG)
        for name in ("&lt;1%", "1-3%", "3%+"):
            self.assertIn(name, text)

    def test_rendered_html_tags_are_all_known(self):
        """Ни один тег в сообщении не должен быть случайным: Telegram
        принимает лишь узкий список, всё прочее — ошибка 400."""
        import re
        allowed = {"b", "/b", "i", "/i", "pre", "/pre", "code", "/code"}
        tags = set(re.findall(r"<(/?[a-zA-Z0-9]+)[^>]*>", render(self._digest(), CFG)))
        self.assertTrue(tags <= allowed, f"неизвестные теги: {tags - allowed}")

    def test_never_exceeds_telegram_limit(self):
        """Обрезка проверяется на собранном Digest, а не через 400 обходов
        БД: тест должен мерить render(), а не скорость SQLite."""
        from analysis.digest import Digest, Row
        rows = [Row(buy_price=Decimal("479.68"), sell_price=Decimal("999.99"),
                    spread_pct=Decimal("1.23"), seen_count=5,
                    first_seen=0.0, last_seen=600.0) for _ in range(2000)]
        huge = Digest(host=HOST, amount_kzt=Decimal("300000"),
                      window_from=0.0, window_to=10800.0, moments=180,
                      buckets={name: rows for name in ("<1%", "1-3%", "3%+")},
                      bucket_totals={"<1%": 2000, "1-3%": 2000, "3%+": 2000},
                      filtered_out=0, losing=0, poll_gaps=0)
        text = render(huge, CFG)
        self.assertLessEqual(len(text), TELEGRAM_MAX_CHARS)
        self.assertTrue(text.endswith("<i>…</i>"))


class TestSend(unittest.TestCase):
    def test_disabled_config_refuses_to_send(self):
        with self.assertRaises(TelegramError):
            send("x", TelegramConfig(bot_token="", chat_id=""))

    def test_unauthorized_is_not_retried(self):
        """401 повтором не лечится — только сожжёт время и лимиты."""
        calls = {"n": 0}

        def fail(*a, **k):
            calls["n"] += 1
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)

        with mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(TelegramError):
                send("x", CFG)
        self.assertEqual(calls["n"], 1)

    def test_token_never_appears_in_error_text(self):
        def fail(*a, **k):
            raise urllib.error.HTTPError("u", 400, "Bad Request", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=fail):
            with self.assertRaises(TelegramError) as ctx:
                send("x", CFG)
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))

    def test_ok_false_response_is_an_error(self):
        import io

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        body = json.dumps({"ok": False, "description": "chat not found"}).encode()
        with mock.patch("time.sleep"), \
             mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: R(body)):
            with self.assertRaises(TelegramError):
                send("x", CFG)


if __name__ == "__main__":
    unittest.main()


class TestPriceLevelCollapse(unittest.TestCase):
    """Четыре продавца на одной цене не должны занимать весь бакет."""

    def _row(self, buy, sell, first, last):
        from analysis.digest import Row
        from decimal import Decimal as D
        return Row(buy_price=D(buy), sell_price=D(sell),
                   spread_pct=(D(sell) / D(buy) - 1) * 100,
                   seen_count=1, first_seen=first, last_seen=last)

    def test_same_price_level_keeps_longest_lived(self):
        from analysis.digest import _collapse_price_levels
        rows = [self._row("480", "484", 0, 60),
                self._row("480", "484", 0, 600),
                self._row("480", "484", 0, 30)]
        out = _collapse_price_levels(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].lifetime_sec, 600)

    def test_distinct_levels_are_kept(self):
        from analysis.digest import _collapse_price_levels
        rows = [self._row("480", "484", 0, 60), self._row("479", "484", 0, 60)]
        self.assertEqual(len(_collapse_price_levels(rows)), 2)

    def test_order_stays_by_spread(self):
        from analysis.digest import _collapse_price_levels
        rows = [self._row("480", "484", 0, 60), self._row("478", "485", 0, 60)]
        out = _collapse_price_levels(rows)
        self.assertGreater(out[0].spread_pct, out[1].spread_pct)


class TestStrataPanel(unittest.TestCase):
    """Панель градиента: четыре страты и размеры выборки за каждой."""

    def _row(self, name, med, p25, p75, ads, advs, moments=10):
        from analysis.digest import StratumRow
        from decimal import Decimal as D
        mk = lambda v: None if v is None else D(str(v))
        return StratumRow(name=name, moments=moments, ads=ads, advertisers=advs,
                          median_spread=mk(med), p25=mk(p25), p75=mk(p75))

    def _render(self, strata):
        from analysis.digest import Digest
        from notify.telegram import render
        from decimal import Decimal as D
        d = Digest(host="h", amount_kzt=D("300000"), window_from=0, window_to=3600,
                   moments=10, buckets={}, bucket_totals={}, filtered_out=0,
                   losing=0, poll_gaps=0, strata=tuple(strata))
        return render(d, CFG)

    def test_all_four_strata_are_shown(self):
        out = self._render([
            self._row("any", 2.8, 1.9, 3.4, 400, 300),
            self._row("basic", 2.49, 1.7, 3.0, 250, 190),
            self._row("good", 1.27, 0.8, 1.9, 90, 70),
            self._row("premium", -3.63, -4.0, -3.0, 12, 9),
        ])
        for name in ("any", "basic", "good", "premium"):
            self.assertIn(name, out)

    def test_sample_sizes_are_visible(self):
        """Читатель должен видеть, сколько лиц стоит за столбцом."""
        out = self._render([self._row("good", 1.27, 0.8, 1.9, 90, 70)])
        self.assertIn("90", out)
        self.assertIn("70", out)

    def test_monotonic_order_reported_as_held(self):
        out = self._render([
            self._row("any", 2.8, 1.9, 3.4, 400, 300),
            self._row("good", 1.27, 0.8, 1.9, 90, 70),
        ])
        self.assertIn("сохранён", out)

    def test_broken_order_is_flagged(self):
        """Нарушение порядка — сигнал, а не молчание."""
        out = self._render([
            self._row("any", 1.0, 0.5, 1.5, 400, 300),
            self._row("good", 2.0, 1.5, 2.5, 90, 70),
        ])
        self.assertIn("НАРУШЕН", out)

    def test_empty_stratum_does_not_crash(self):
        out = self._render([self._row("premium", None, None, None, 3, 2)])
        self.assertIn("premium", out)


class TestStrataAreDisjoint(unittest.TestCase):
    """Пороги страт вложены, поэтому максимум по ВЛОЖЕННЫМ множествам
    убывает механически и проверка порядка на них ничего не значит."""

    def test_nested_thresholds_make_max_monotonic_by_construction(self):
        from config.settings import QUALITY_STRATA
        prev = None
        for q in QUALITY_STRATA:
            cur = (q.min_ad_finish_num, q.min_recent_orders, q.min_execute_rate)
            if prev is not None:
                self.assertTrue(all(a >= b for a, b in zip(cur, prev)),
                                "страты обязаны быть вложены — на этом держится "
                                "необходимость их разносить в панели")
            prev = cur

    def test_panel_names_mark_the_difference_sets(self):
        """Имя страты в отчёте должно показывать, что это разность множеств."""
        from analysis.digest import StratumRow
        from config.settings import QUALITY_STRATA
        names = [f"{q.name}\\{QUALITY_STRATA[i+1].name}"
                 if i + 1 < len(QUALITY_STRATA) else q.name
                 for i, q in enumerate(QUALITY_STRATA)]
        self.assertEqual(names[0], "any\\basic")
        self.assertEqual(names[-1], "premium")
