"""
Восстановление книги на произвольный момент времени.

Это фундамент честной симуляции. Решение принимается по book_at(t),
результат оценивается по book_at(t + Δ). Look-ahead bias структурно
невозможен: функция физически не видит строк с observed_at > ts.

Никакого «предположим задержку 30 секунд». Только: что было на самом деле
через Δ после того, как мы бы приняли решение.
"""

from __future__ import annotations

import json
from decimal import Decimal

from domain.models import Ad, Advertiser, TradingPrefs
from config.settings import COLLECTOR
from storage.db import Store

_BOOK_SQL = """
SELECT a.ad_id, a.host, a.side, a.advertiser_key, a.token, a.currency,
       a.payments, a.prefs, a.remark,
       s.price, s.quantity, s.frozen_qty, s.min_amount, s.max_amount,
       s.version, s.status, s.finish_num, s.order_num,
       s.last_qty, s.executed_qty, s.observed_at,
       v.nick, v.user_type,
       av.recent_order_num, av.recent_execute_rate,
       av.latest_pay_ms, av.latest_release_ms, av.auth_tags, av.is_online
FROM ad a
JOIN ad_presence p ON p.ad_id = a.ad_id
JOIN advertiser v  ON v.key = a.advertiser_key
JOIN ad_state s ON s.ad_id = a.ad_id AND s.observed_at = (
        SELECT MAX(s2.observed_at) FROM ad_state s2
        WHERE s2.ad_id = a.ad_id AND s2.observed_at <= :ts)
LEFT JOIN advertiser_state av ON av.key = a.advertiser_key
     AND av.host = a.host AND av.side = a.side
     AND av.observed_at = (SELECT MAX(a2.observed_at) FROM advertiser_state a2
                           WHERE a2.key = a.advertiser_key AND a2.host = a.host
                             AND a2.side = a.side AND a2.observed_at <= :ts)
WHERE a.host = :host
  AND p.appeared_at <= :ts
  AND (p.disappeared_at IS NULL OR p.disappeared_at > :ts)
"""


def book_at(store: Store, host: str, ts: float) -> list[Ad]:
    """Книга, какой она была в момент ts. Только данные с observed_at <= ts."""
    rows = store.conn.execute(_BOOK_SQL, {"ts": ts, "host": host}).fetchall()
    ads: list[Ad] = []
    for r in rows:
        prefs_raw = json.loads(r["prefs"] or "{}")
        ads.append(Ad(
            ad_id=r["ad_id"], host=r["host"], side=r["side"],
            advertiser=Advertiser(
                key=r["advertiser_key"], nick=r["nick"] or "",
                user_type=r["user_type"] or "",
                auth_tags=tuple(json.loads(r["auth_tags"] or "[]")),
                recent_order_num=r["recent_order_num"] or 0,
                recent_execute_rate=r["recent_execute_rate"] or 0,
                latest_pay_ms=r["latest_pay_ms"] or 0,
                latest_release_ms=r["latest_release_ms"] or 0,
                is_online=bool(r["is_online"]),
            ),
            token=r["token"], currency=r["currency"],
            price=Decimal(r["price"]), quantity=Decimal(r["quantity"]),
            frozen_quantity=Decimal(r["frozen_qty"]),
            min_amount=Decimal(r["min_amount"]), max_amount=Decimal(r["max_amount"]),
            payments=tuple(json.loads(r["payments"] or "[]")),
            prefs=TradingPrefs(
                requires_kyc=bool(prefs_raw.get("isKyc")),
                min_orders_30d=int(prefs_raw.get("orderFinishNumberDay30") or 0),
                min_complete_rate_30d=int(prefs_raw.get("completeRateDay30") or 0),
                has_national_limit=bool(prefs_raw.get("hasNationalLimit")),
                has_single_user_order_limit=bool(
                    prefs_raw.get("hasSingleUserOrderLimit")),
                raw=prefs_raw,
            ),
            remark=r["remark"] or "", version=r["version"], status=r["status"],
            observed_at=r["observed_at"],
            finish_num=r["finish_num"] or 0, order_num=r["order_num"] or 0,
            last_quantity=Decimal(r["last_qty"]),
            executed_quantity=Decimal(r["executed_qty"]),
        ))
    return ads


def observation_times(store: Store, host: str,
                      min_spacing_sec: int = 60,
                      max_skew_sec: int | None = None) -> list[float]:
    """Моменты, на которые есть ПОЛНЫЕ наблюдения обеих сторон книги.

    Два РАЗНЫХ параметра, которые раньше были одним и тем же числом:

      min_spacing_sec — как редко брать моменты (вопрос объёма вычислений)
      max_skew_sec    — насколько сторона "0" вправе отставать от стороны "1"
                        (вопрос достоверности: книга, сшитая из наблюдений
                        с разницей в 15 минут, даёт вымышленные пары)

    Слипшись, они давали такой эффект: отчёт просит редкую выборку и молча
    получает разрешение сшивать книгу как попало. Теперь допуск на
    рассинхрон фиксирован и не зависит от частоты выборки.

    Неполные обходы исключаются: строить на них выводы об исчезновении
    объявлений нельзя. Это защита от дыр в данных ноутбука.
    """
    if max_skew_sec is None:
        # один пропущенный цикл ещё терпим, два — уже нет
        max_skew_sec = COLLECTOR.poll_interval_sec * 3

    rows = store.conn.execute(
        "SELECT started_at, side FROM poll_run"
        " WHERE host=? AND complete=1 ORDER BY started_at", (host,)).fetchall()
    by_side: dict[str, list[float]] = {}
    for r in rows:
        by_side.setdefault(r["side"], []).append(r["started_at"])

    a, b = by_side.get("1", []), by_side.get("0", [])
    if not a or not b:
        return []

    out: list[float] = []
    j = -1                  # индекс последнего наблюдения стороны "0" ДО t
    last = float("-inf")
    for t in a:
        while j + 1 < len(b) and b[j + 1] <= t:
            j += 1
        if j < 0:
            # Противоположная сторона ещё ни разу не опрашивалась к моменту t.
            # Раньше здесь брался b[0] из БУДУЩЕГО: это и look-ahead, и —
            # на практике — момент с пустой половиной книги, из-за чего
            # отчёт молча показывал нули вместо пар.
            continue
        if t - b[j] <= max_skew_sec and t - last >= min_spacing_sec:
            out.append(t)
            last = t
    return out
