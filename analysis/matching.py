"""
Поиск исполнимых пар.

Не best-bid/best-ask. Самое дешёвое объявление с лимитом 80k бесполезно,
если нам нужно 300k — поэтому фильтрация по сумме идёт ДО поиска лучшей цены.

Функции чистые и детерминированные: одна и та же книга даёт один и тот же
результат. Это условие воспроизводимости всего исследования.
"""

from __future__ import annotations

import heapq
from decimal import Decimal
from typing import Iterator

from config.settings import MATCHING, MatchingConfig, QualityStratum
from domain.models import Ad, Pair


def eligible(ads: list[Ad], amount_kzt: Decimal, side: str,
             stratum: QualityStratum | None = None,
             now: float | None = None,
             cfg: MatchingConfig = MATCHING) -> list[Ad]:
    """Объявления, способные провести amount_kzt, нужной стороны и качества."""
    out = []
    for ad in ads:
        if ad.side != side:
            continue
        if not ad.supports(amount_kzt):
            continue
        if stratum and not ad.meets(stratum):
            continue
        if now is not None and (now - ad.observed_at) > cfg.max_data_age_sec:
            continue
        out.append(ad)
    return out


def evaluate(buy_ad: Ad, sell_ad: Ad, amount_kzt: Decimal,
             cfg: MatchingConfig = MATCHING) -> Pair:
    """Оценить одну пару. Возвращает Pair с причинами отказа, а не None:
    отказы — это тоже данные, по ним считается, почему возможностей нет."""
    reasons: list[str] = []

    if cfg.forbid_same_advertiser and buy_ad.advertiser.key == sell_ad.advertiser.key:
        reasons.append("same_counterparty")

    common = tuple(sorted(set(buy_ad.payments) & set(sell_ad.payments)))
    if cfg.require_common_payment and not common:
        reasons.append("no_common_payment")
    elif cfg.my_payments and not (set(common) & cfg.my_payments):
        # Общий метод у ног есть, но счёта в этом банке нет у нас.
        reasons.append("no_payment_we_hold")

    if not buy_ad.supports(amount_kzt):
        reasons.append("buy_leg_cannot_fill")
    if not sell_ad.supports(amount_kzt):
        reasons.append("sell_leg_cannot_fill")

    # Требования объявления к нам прочитать можем, соответствие — нет.
    # Поэтому это флаг риска, а не отказ: иначе мы бы выбросили
    # объявления, которые, возможно, нам доступны.
    if buy_ad.prefs.is_restrictive or sell_ad.prefs.is_restrictive:
        reasons.append("restrictive_prefs__unverifiable")

    return Pair(amount_kzt=amount_kzt, buy_ad=buy_ad, sell_ad=sell_ad,
                common_payments=common, rejection_reasons=tuple(reasons))


HARD_REJECTIONS = Pair.HARD_REJECTIONS


def is_hard_rejected(pair: Pair) -> bool:
    return not pair.executable


def iter_pairs_by_spread(ads: list[Ad], amount_kzt: Decimal,
                         stratum: QualityStratum | None = None,
                         now: float | None = None,
                         cfg: MatchingConfig = MATCHING) -> Iterator[Pair]:
    """Пары в порядке УБЫВАНИЯ gross-спреда, лениво.

    Наивный двойной цикл здесь неприменим: 90 buy x 310 sell = 28 000 пар
    на каждую (сумма, страта), а отчёт перебирает 12 таких комбинаций на
    каждый момент наблюдения. За 72 часа сбора это порядка миллиарда
    вычислений — отчёт не досчитается.

    Здесь: buy отсортированы по возрастанию цены, sell по убыванию, поэтому
    для фиксированного buy спреды идут строго по убыванию. Получаем
    len(buys) отсортированных потоков и сливаем их кучей. Потребитель,
    которому нужна одна лучшая пара, останавливается после первой.

    Сортировка по gross, а не по net, — сознательно: net зависит от
    UNKNOWN-параметров, ранжировать по нему значит ранжировать по догадке.
    """
    buys = [a for a in eligible(ads, amount_kzt, "1", stratum, now, cfg)
            if a.price > 0]
    sells = eligible(ads, amount_kzt, "0", stratum, now, cfg)
    if not buys or not sells:
        return

    buys.sort(key=lambda a: a.price)
    sells.sort(key=lambda a: a.price, reverse=True)

    def spread(bi: int, si: int) -> Decimal:
        return (sells[si].price / buys[bi].price - 1) * 100

    heap: list[tuple[Decimal, int, int]] = [
        (-spread(bi, 0), bi, 0) for bi in range(len(buys))]
    heapq.heapify(heap)

    while heap:
        neg, bi, si = heapq.heappop(heap)
        if -neg < cfg.min_gross_spread_pct:
            return          # дальше только хуже — обход закончен
        pair = evaluate(buys[bi], sells[si], amount_kzt, cfg)
        if pair.executable:
            yield pair
        if si + 1 < len(sells):
            heapq.heappush(heap, (-spread(bi, si + 1), bi, si + 1))


def find_pairs(ads: list[Ad], amount_kzt: Decimal,
               stratum: QualityStratum | None = None,
               now: float | None = None,
               cfg: MatchingConfig = MATCHING,
               limit: int | None = None) -> list[Pair]:
    """Исполнимые пары по убыванию спреда. limit=None — все.

    ВНИМАНИЕ: limit смещает выборку в сторону больших спредов. Для анализа
    распределения по бакетам берите limit=None и реже сэмплируйте моменты.
    """
    it = iter_pairs_by_spread(ads, amount_kzt, stratum, now, cfg)
    if limit is None:
        return list(it)
    out = []
    for pair in it:
        out.append(pair)
        if len(out) >= limit:
            break
    return out


def best_pair(ads: list[Ad], amount_kzt: Decimal,
              stratum: QualityStratum | None = None,
              now: float | None = None,
              cfg: MatchingConfig = MATCHING) -> Pair | None:
    """Лучшая исполнимая пара — то, что реально доступно, а не top-of-book."""
    return next(iter_pairs_by_spread(ads, amount_kzt, stratum, now, cfg), None)
