"""
Экономика маркет-мейкера.

Другая сторона вопроса. Тейкер пытается поймать перекрестье между чужими
котировками; мейкер сам ставит обе и зарабатывает на потоке. Данные
показывают, что прибыльна вторая роль — поэтому её и надо мерить.

Ключевая величина здесь не спред, а ОБОРОТ ПРИ СПРЕДЕ: спред 8% без
потока стоит ноль, спред 3% с потоком кормит. Считается из
executedQuantity — счётчика самой биржи, а не из наших предположений.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from storage.db import Store


@dataclass
class Maker:
    key: str
    nick: str
    ask: Decimal | None = None          # продаёт USDT нам (наш buy)
    bid: Decimal | None = None          # покупает USDT у нас (наш sell)
    volume_usdt: Decimal = Decimal(0)   # реально прошло за окно наблюдения
    ads: int = 0
    recent_orders: int = 0
    execute_rate: int = 0

    @property
    def two_sided(self) -> bool:
        return self.ask is not None and self.bid is not None

    @property
    def spread_pct(self) -> Decimal | None:
        if not self.two_sided or not self.bid:
            return None
        return (self.ask / self.bid - 1) * 100

    @property
    def revenue_kzt(self) -> Decimal:
        """Грубая оценка выручки за окно.

        Полный спред зарабатывается только на ЗАМКНУТОМ круге — купил и
        продал. Оборот считаем суммарным по обеим сторонам, поэтому кругов
        примерно вдвое меньше. Это оценка сверху: часть потока односторонняя
        и оставляет мейкера с инвентарём, а не с прибылью.
        """
        if not self.two_sided:
            return Decimal(0)
        return (self.volume_usdt / 2) * (self.ask - self.bid)


@dataclass
class MakerStats:
    makers: list[Maker] = field(default_factory=list)
    hours: float = 0.0

    @property
    def two_sided(self) -> list[Maker]:
        return [m for m in self.makers if m.two_sided]

    @property
    def active(self) -> list[Maker]:
        """Мейкеры, у которых реально шёл поток."""
        return [m for m in self.two_sided if m.volume_usdt > 0]

    def median_spread(self) -> Decimal | None:
        sp = [float(m.spread_pct) for m in self.two_sided if m.spread_pct is not None]
        return Decimal(str(round(statistics.median(sp), 2))) if sp else None

    def total_volume(self) -> Decimal:
        return sum((m.volume_usdt for m in self.makers), Decimal(0))

    def top_by_volume(self, n: int = 10) -> list[Maker]:
        return sorted(self.active, key=lambda m: m.volume_usdt, reverse=True)[:n]

    def spread_vs_volume(self) -> list[tuple[str, int, Decimal, Decimal]]:
        """Связь агрессивности цены и потока: (бакет, мейкеров, медиана
        оборота, медиана выручки). Отвечает на вопрос, окупается ли
        узкий спред большим потоком."""
        buckets = [("< 3%", Decimal(0), Decimal(3)),
                   ("3-5%", Decimal(3), Decimal(5)),
                   ("5-7%", Decimal(5), Decimal(7)),
                   ("7%+", Decimal(7), None)]
        out = []
        for name, lo, hi in buckets:
            grp = [m for m in self.two_sided
                   if m.spread_pct is not None and m.spread_pct >= lo
                   and (hi is None or m.spread_pct < hi)]
            if not grp:
                out.append((name, 0, Decimal(0), Decimal(0)))
                continue
            vol = statistics.median([float(m.volume_usdt) for m in grp])
            rev = statistics.median([float(m.revenue_kzt) for m in grp])
            out.append((name, len(grp), Decimal(str(round(vol))),
                        Decimal(str(round(rev)))))
        return out


def collect(store: Store, host: str) -> MakerStats:
    rows = store.conn.execute("""
        SELECT a.advertiser_key k, v.nick, a.side, a.ad_id,
               (SELECT price FROM ad_state s WHERE s.ad_id = a.ad_id
                 ORDER BY observed_at DESC LIMIT 1) price,
               (SELECT MAX(CAST(executed_qty AS REAL)) - MIN(CAST(executed_qty AS REAL))
                  FROM ad_state s WHERE s.ad_id = a.ad_id) vol,
               (SELECT recent_order_num FROM advertiser_state st
                 WHERE st.key = a.advertiser_key ORDER BY observed_at DESC LIMIT 1) ro,
               (SELECT recent_execute_rate FROM advertiser_state st
                 WHERE st.key = a.advertiser_key ORDER BY observed_at DESC LIMIT 1) er
          FROM ad a JOIN advertiser v ON v.key = a.advertiser_key
         WHERE a.host = ?""", (host,)).fetchall()

    by_key: dict[str, Maker] = {}
    for r in rows:
        if r["price"] is None:
            continue
        m = by_key.setdefault(r["k"], Maker(key=r["k"], nick=r["nick"] or ""))
        price = Decimal(str(r["price"]))
        m.ads += 1
        m.volume_usdt += Decimal(str(r["vol"] or 0))
        m.recent_orders = r["ro"] or 0
        m.execute_rate = r["er"] or 0
        if r["side"] == "1":                        # продаёт нам — берём лучшую
            m.ask = price if m.ask is None else min(m.ask, price)
        else:                                       # покупает у нас
            m.bid = price if m.bid is None else max(m.bid, price)

    span = store.conn.execute(
        "SELECT MAX(started_at) - MIN(started_at) FROM poll_run WHERE host=?",
        (host,)).fetchone()[0] or 0
    return MakerStats(makers=list(by_key.values()), hours=span / 3600)
