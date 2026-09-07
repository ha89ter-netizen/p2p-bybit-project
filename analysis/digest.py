"""
Дайджест за окно времени: что было в книге за последние N часов.

Не снимок «прямо сейчас», а свод по всем моментам наблюдения в окне —
иначе раз в три часа мы видели бы одну случайную секунду рынка.

Пара считается один раз по ключу (buy_ad, sell_ad): если одна и та же
связка простояла три часа, это ОДНА возможность, а не 540 штук. Иначе
статичная витрина выглядела бы как бурная торговля.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from analysis.matching import band_names, iter_pairs_by_spread, quality_bands
from analysis.replay import book_at, observation_times
from config.settings import QUALITY_STRATA, DIGEST_BUCKETS, TELEGRAM, TelegramConfig
from domain.models import Pair
from storage.db import Store


@dataclass(frozen=True)
class Row:
    buy_price: Decimal
    sell_price: Decimal
    spread_pct: Decimal
    seen_count: int          # на скольких моментах наблюдения встретилась
    first_seen: float
    last_seen: float

    @property
    def lifetime_sec(self) -> float:
        return self.last_seen - self.first_seen


def _collapse_price_levels(rows: list[Row]) -> list[Row]:
    """Схлопнуть строки с одинаковой парой цен.

    Дайджест уже схлопывает по (buy_ad_id, sell_ad_id), но на плотном
    рынке четыре разных продавца стоят на одной цене, и в сообщение
    уезжает пять визуально одинаковых строк — весь слот бакета тратится
    на один ценовой уровень. Оставляем самую долгоживущую строку уровня:
    она и информативнее, короткая могла быть случайным дребезгом.
    """
    best: dict[tuple, Row] = {}
    for r in rows:
        key = (r.buy_price, r.sell_price)
        cur = best.get(key)
        if cur is None or r.lifetime_sec > cur.lifetime_sec:
            best[key] = r
    return sorted(best.values(), key=lambda r: r.spread_pct, reverse=True)


@dataclass(frozen=True)
class StratumRow:
    """Срез по одной страте качества за окно.

    Размеры выборки хранятся отдельно и намеренно: одна связка, провисевшая
    три часа, даёт сотни наблюдений состояния, но это НЕ сотни независимых
    экономических наблюдений. Читатель должен видеть, сколько за столбцом
    стоит уникальных контрагентов и объявлений, а не только медиану.
    """
    name: str
    moments: int              # моментов, где страта дала хотя бы одну пару
    ads: int                  # уникальных объявлений, прошедших порог
    advertisers: int          # уникальных КОНТРАГЕНТОВ — единица кластеризации
    median_spread: Decimal | None
    p25: Decimal | None
    p75: Decimal | None


@dataclass(frozen=True)
class PaperSummary:
    trades: int
    pnl_kzt: Decimal
    pnl_per_day_kzt: Decimal
    return_pct: Decimal
    hours: float
    outcomes: dict[str, int]
    working_capital_kzt: Decimal
    breakeven_pct: Decimal | None
    start_capital_kzt: Decimal = Decimal(0)
    final_capital_kzt: Decimal = Decimal(0)
    growth_pct: Decimal = Decimal(0)
    max_drawdown_pct: Decimal = Decimal(0)
    next_position_kzt: Decimal = Decimal(0)


@dataclass(frozen=True)
class Digest:
    host: str
    amount_kzt: Decimal
    window_from: float
    window_to: float
    moments: int
    buckets: dict[str, list[Row]]
    bucket_totals: dict[str, int]
    filtered_out: int          # УНИКАЛЬНЫХ пар, отсеянных по качеству
    losing: int                # уникальных пар с отрицательным спредом
    poll_gaps: int
    strata: tuple[StratumRow, ...] = ()
    paper: PaperSummary | None = None


def _passes_quality(pair: Pair, cfg: TelegramConfig) -> bool:
    for ad in (pair.buy_ad, pair.sell_ad):
        if ad.finish_num < cfg.min_ad_finish_num:
            return False
        if ad.advertiser.recent_order_num < cfg.min_recent_orders:
            return False
        if ad.advertiser.recent_execute_rate < cfg.min_execute_rate:
            return False
    return True


def _bucket_of(spread_pct: Decimal) -> str | None:
    for name, lo, hi in DIGEST_BUCKETS:
        if spread_pct >= lo and (hi is None or spread_pct < hi):
            return name
    return None


def build(store: Store, host: str, window_sec: int,
          cfg: TelegramConfig = TELEGRAM, now: float | None = None) -> Digest:
    import time
    now = now if now is not None else time.time()
    since = now - window_sec

    times = [t for t in observation_times(store, host, min_spacing_sec=60)
             if t >= since]

    # Заголовок не должен обещать окно, которого нет: если сбор идёт
    # полтора часа, показывать «за 3 часа» — значит выдавать неполные
    # данные за полные.
    if times:
        since = max(since, min(times))

    # Панель страт считается в ЭТОМ же проходе: book_at — самая дорогая
    # операция отчёта, читать книгу второй раз ради тех же моментов незачем.
    _bands = band_names()
    st_spreads: dict[str, list[Decimal]] = {n: [] for n in _bands}
    st_ads: dict[str, set[str]] = {n: set() for n in _bands}
    st_advs: dict[str, set[str]] = {n: set() for n in _bands}

    # Схлопываем одну и ту же связку по всему окну.
    seen: dict[tuple[str, str], dict] = {}
    filtered: set[tuple[str, str]] = set()
    for t in times:
        book = book_at(store, host, t)

        # Полосы взаимоисключающие — определение одно на весь проект,
        # см. analysis.matching.quality_bands.
        for name, band in quality_bands(book):
            for a in band:
                st_ads[name].add(a.ad_id)
                st_advs[name].add(a.advertiser.key)
            # лениво: нужна только лучшая пара момента, итератор отсортирован
            best = next(iter_pairs_by_spread(band, cfg.amount_kzt), None)
            if best is not None:
                st_spreads[name].append(best.gross_spread_pct)

        for pair in iter_pairs_by_spread(book, cfg.amount_kzt):
            key = (pair.buy_ad.ad_id, pair.sell_ad.ad_id)
            if not _passes_quality(pair, cfg):
                # считаем по уникальным парам: одна и та же связка,
                # простоявшая три часа, это одна отсеянная пара, а не 540
                filtered.add(key)
                continue
            rec = seen.get(key)
            if rec is None:
                seen[key] = {"pair": pair, "n": 1, "first": t, "last": t}
            else:
                rec["n"] += 1
                rec["last"] = t
                # держим последнюю наблюдённую цену
                rec["pair"] = pair

    buckets: dict[str, list[Row]] = {name: [] for name, _, _ in DIGEST_BUCKETS}
    losing = 0
    for rec in seen.values():
        pair = rec["pair"]
        name = _bucket_of(pair.gross_spread_pct)
        if name is None:
            losing += 1          # спред ниже нижней границы = убыток
            continue
        buckets[name].append(Row(
            buy_price=pair.buy_ad.price, sell_price=pair.sell_ad.price,
            spread_pct=pair.gross_spread_pct, seen_count=rec["n"],
            first_seen=rec["first"], last_seen=rec["last"]))

    totals = {name: len(rows) for name, rows in buckets.items()}
    for name in buckets:
        buckets[name].sort(key=lambda r: r.spread_pct, reverse=True)
        buckets[name] = _collapse_price_levels(buckets[name])[:cfg.rows_per_bucket]

    gaps = store.conn.execute(
        "SELECT COUNT(*) FROM poll_run WHERE host=? AND started_at>=?"
        " AND complete=0", (host, since)).fetchone()[0]

    # Бумажная торговля считается по ВСЕЙ истории, а не по окну дайджеста:
    # нарастающий итог интереснее среза за три часа. Пересчёт с нуля
    # детерминирован, поэтому состояние в БД не копится и не расходится
    # при смене правил.
    paper = None
    try:
        from analysis.paper import simulate
        from config.settings import PAPER
        r = simulate(store, host)
        be = r.breakeven_success_rate()
        paper = PaperSummary(
            trades=r.n, pnl_kzt=r.total_pnl_kzt,
            pnl_per_day_kzt=r.pnl_per_day_kzt,
            return_pct=r.return_pct(PAPER.capital_kzt), hours=r.hours,
            outcomes=r.outcomes(),
            working_capital_kzt=PAPER.working_capital_kzt,
            breakeven_pct=be * 100 if be is not None else None,
            start_capital_kzt=r.start_capital_kzt,
            final_capital_kzt=r.final_capital_kzt,
            growth_pct=r.growth_pct,
            max_drawdown_pct=r.max_drawdown_pct,
            next_position_kzt=r.final_capital_kzt * PAPER.deploy_pct / 100)
    except Exception:                                     # noqa: BLE001
        pass          # симуляция не должна ломать дайджест

    def _q(v: list[Decimal], frac: float) -> Decimal | None:
        if not v:
            return None
        w = sorted(v)
        return w[min(len(w) - 1, int(len(w) * frac))]

    strata = tuple(
        StratumRow(
            name=n,
            moments=len(st_spreads[n]),
            ads=len(st_ads[n]),
            advertisers=len(st_advs[n]),
            median_spread=_q(st_spreads[n], .5),
            p25=_q(st_spreads[n], .25),
            p75=_q(st_spreads[n], .75),
        )
        for n in _bands
    )

    return Digest(host=host, amount_kzt=cfg.amount_kzt, window_from=since,
                  window_to=now, moments=len(times), buckets=buckets,
                  bucket_totals=totals, filtered_out=len(filtered),
                  losing=losing, poll_gaps=gaps, strata=strata, paper=paper)
