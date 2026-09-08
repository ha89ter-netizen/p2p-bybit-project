"""Сравнительный слой по сегментам качества.

Существующая сегментация не меняется: те же ANY / BASIC / GOOD / PREMIUM
из `config.settings.QUALITY_STRATA`. Здесь только считаются метрики,
позволяющие сравнивать сегменты между собой.

Одна вещь про сравнение, без которой весь слой был бы бесполезен.

Пороги сегментов ВЛОЖЕНЫ: `premium ⊂ good ⊂ basic ⊂ any`. Максимум спреда
по надмножеству не может быть меньше максимума по подмножеству, поэтому
таблица «лучший спред по сегментам» убывает сверху вниз при ЛЮБЫХ данных.
Сравнивать сегменты в таком виде нельзя — убывание будет получено даже
на случайном шуме.

Поэтому считаются два разреза:

* `nested` — сегменты как заданы. Годится, чтобы ответить «что доступно
  участнику, готовому работать с контрагентами не хуже такого-то».
  Для сравнения между собой НЕ годится, и помечен `comparable=False`.
* `disjoint` — взаимоисключающие полосы (прошёл порог и не прошёл
  следующий). Именно на них проверяется исследовательский вопрос.

Метрики, для которых источника данных нет, возвращаются как None.
Выдуманных значений здесь нет.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from analysis.matching import iter_pairs_by_spread, quality_bands
from analysis.replay import book_at, observation_times
from config.settings import QUALITY_STRATA
from storage.db import Store


def _med(v: list[float]) -> float | None:
    return round(statistics.median(v), 4) if v else None


def _mean(v: list[float]) -> float | None:
    return round(statistics.fmean(v), 4) if v else None


def _pct(v: list[float], f: float) -> float | None:
    if not v:
        return None
    w = sorted(v)
    return round(w[min(len(w) - 1, int(len(w) * f))], 4)


@dataclass
class SegmentMetrics:
    """Метрики одного сегмента. None означает «данных нет», а не ноль."""
    segment: str
    comparable: bool                  # можно ли сравнивать с соседями

    # спред
    median_spread_pct: float | None = None
    mean_spread_pct: float | None = None
    p10_spread_pct: float | None = None
    p90_spread_pct: float | None = None
    best_spread_pct: float | None = None
    worst_spread_pct: float | None = None

    # возможности
    opportunity_count: int = 0        # всех пар в сегменте
    usable_opportunity_count: int = 0  # прошедших фильтр исполнимости

    # качество контрагентов
    median_completion_rate: float | None = None
    median_orders_30d: float | None = None

    # ликвидность — СРЕДНЕЕ НА МОМЕНТ, а не сумма по окну.
    # Суммирование давало бы «227 005 млн ₸» на рынке, где в книге стоит
    # несколько сотен миллионов: одна и та же заявка считалась бы заново
    # в каждом наблюдении.
    available_liquidity_kzt: float | None = None
    estimated_executable_kzt: float | None = None

    # размеры выборки — без них медианы читать нельзя
    ads: int = 0
    advertisers: int = 0
    dyads: int = 0
    moments: int = 0

    # симуляция: считается отдельным модулем, здесь точка расширения
    simulated_pnl_kzt: float | None = None
    simulated_trades: int | None = None
    simulated_wins: int | None = None
    simulated_losses: int | None = None
    avg_return_per_cycle_pct: float | None = None

    @property
    def thin(self) -> bool:
        """Меньше 30 независимых кластеров — асимптотика неприменима."""
        return self.dyads < 30

    def to_dict(self) -> dict:
        d = asdict(self)
        d["thin"] = self.thin
        return d


def _segment_slices(book, mode: str):
    """(имя, объявления) для выбранного разреза."""
    if mode == "disjoint":
        return quality_bands(book)
    return [(q.name, [a for a in book if a.meets(q)]) for q in QUALITY_STRATA]


def collect(store: Store, host: str, amount_kzt: Decimal,
            hours: float = 24.0, mode: str = "disjoint",
            max_moments: int = 120, now: float | None = None
            ) -> list[SegmentMetrics]:
    """Метрики по сегментам за окно.

    `mode='disjoint'` — разрез, на котором сравнение осмысленно.
    `mode='nested'` — исходные сегменты; сравнение между ними тавтологично.
    """
    import time
    now = now if now is not None else time.time()
    times = [t for t in observation_times(store, host, min_spacing_sec=300)
             if t >= now - hours * 3600][-max_moments:]

    acc: dict[str, dict] = {}
    for t in times:
        book = book_at(store, host, t)
        for name, part in _segment_slices(book, mode):
            a = acc.setdefault(name, {
                "spreads": [], "usable": 0, "all": 0, "rates": [], "orders": [],
                "liq": Decimal(0), "exec": Decimal(0),
                "ads": set(), "advs": set(), "dyads": set(), "moments": 0,
            })
            a["moments"] += 1
            for ad in part:
                a["ads"].add(ad.ad_id)
                a["advs"].add(ad.advertiser.key)
                a["rates"].append(float(ad.advertiser.recent_execute_rate))
                a["orders"].append(float(ad.advertiser.recent_order_num))
                a["liq"] += ad.liquidity_kzt
                if ad.supports(amount_kzt):
                    a["exec"] += amount_kzt
            for p in iter_pairs_by_spread(part, amount_kzt):
                a["all"] += 1
                a["usable"] += 1
                a["spreads"].append(float(p.gross_spread_pct))
                a["dyads"].add(p.advertiser_pair_key)

    names = [n for n, _ in _segment_slices([], mode)]
    out = []
    for n in names:
        a = acc.get(n)
        if a is None:
            out.append(SegmentMetrics(segment=n, comparable=(mode == "disjoint")))
            continue
        sp = a["spreads"]
        out.append(SegmentMetrics(
            segment=n, comparable=(mode == "disjoint"),
            median_spread_pct=_med(sp), mean_spread_pct=_mean(sp),
            p10_spread_pct=_pct(sp, .10), p90_spread_pct=_pct(sp, .90),
            best_spread_pct=round(max(sp), 4) if sp else None,
            worst_spread_pct=round(min(sp), 4) if sp else None,
            opportunity_count=a["all"], usable_opportunity_count=a["usable"],
            median_completion_rate=_med(a["rates"]),
            median_orders_30d=_med(a["orders"]),
            available_liquidity_kzt=(float(a["liq"]) / a["moments"]
                                     if a["liq"] and a["moments"] else None),
            estimated_executable_kzt=(float(a["exec"]) / a["moments"]
                                      if a["exec"] and a["moments"] else None),
            ads=len(a["ads"]), advertisers=len(a["advs"]),
            dyads=len(a["dyads"]), moments=a["moments"],
        ))
    return out


def quality_vs_spread(metrics: list[SegmentMetrics]) -> dict:
    """Данные для графика «качество контрагента против спреда».

    Вывод НЕ зашит: направление связи считается из самих чисел, и если
    зависимость не монотонна, так и сообщается.
    """
    pts = [(m.segment, m.median_orders_30d, m.median_spread_pct, m.dyads)
           for m in metrics if m.median_spread_pct is not None]
    comparable = all(m.comparable for m in metrics)
    meds = [p[2] for p in pts]
    if len(meds) < 2:
        direction = "insufficient_data"
    elif all(meds[i] >= meds[i + 1] for i in range(len(meds) - 1)):
        direction = "decreasing"
    elif all(meds[i] <= meds[i + 1] for i in range(len(meds) - 1)):
        direction = "increasing"
    else:
        direction = "non_monotonic"
    return {
        "points": [{"segment": s, "orders_30d": o, "median_spread_pct": sp,
                    "dyads": d, "thin": d < 30} for s, o, sp, d in pts],
        "direction": direction,
        "comparable": comparable,
        "note": ("сравнение осмысленно: сегменты не пересекаются" if comparable
                 else "сегменты вложены — убывание здесь тавтологично"),
    }
