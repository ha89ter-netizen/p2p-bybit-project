"""
Отчёт «Блок A» — пять вопросов, которые решают судьбу проекта.

    python3 -m scripts.report_block_a

Сознательно НЕ печатает прогноз PnL. Прогноз PnL до ответа на эти пять
вопросов становится якорем и начинает управлять решениями, под которыми
нет фундамента. Это ровно та ошибка, из-за которой предыдущий проект
дошёл до продакшена с отрицательным матожиданием.
"""

from __future__ import annotations

import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from analysis.matching import best_pair, find_pairs, quality_bands
from analysis.replay import book_at, observation_times
from config.settings import (ANALYSIS_AMOUNTS_KZT, COLLECTOR,
                             SPREAD_BUCKETS_PCT)
from storage.db import Store

KZ = "api2.bybit.kz"
GLOBAL = "api2.bybit.com"


def h(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def pct(x) -> str:
    return f"{float(x):+.3f}%"


def q(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p))]


# --------------------------------------------------------------------------

def coverage(store: Store) -> None:
    h("ПОКРЫТИЕ ДАННЫХ  (без этого остальные цифры недостоверны)")
    rows = store.conn.execute(
        "SELECT host, side, COUNT(*) n, SUM(complete) ok,"
        " MIN(started_at) t0, MAX(started_at) t1, AVG(latency_ms) lat"
        " FROM poll_run GROUP BY host, side ORDER BY host, side").fetchall()
    if not rows:
        print("  данных нет — коллектор ещё не запускался")
        return
    for r in rows:
        span_h = (r["t1"] - r["t0"]) / 3600
        expected = span_h * 3600 / COLLECTOR.poll_interval_sec
        print(f"  {r['host']:<18} side={r['side']}  обходов={r['n']:>6} "
              f"полных={r['ok']:>6} ({100*r['ok']/r['n']:.1f}%)  "
              f"окно={span_h:.1f}ч  покрытие={100*r['n']/max(expected,1):.0f}%  "
              f"lat={r['lat']:.0f}ms")
        print(f"  {'':18} {ts(r['t0'])} .. {ts(r['t1'])}")

    gaps = store.conn.execute(
        "SELECT started_at, LAG(started_at) OVER (PARTITION BY host, side"
        " ORDER BY started_at) prev, host, side FROM poll_run").fetchall()
    big = [(g["host"], g["side"], g["prev"], g["started_at"]) for g in gaps
           if g["prev"] and g["started_at"] - g["prev"] > 300]
    print(f"\n  пропусков >5 мин: {len(big)}")
    for hh, sd, a, b in big[:5]:
        print(f"    {hh} side={sd}  {ts(a)} .. {ts(b)}  ({(b-a)/60:.0f} мин)")


def question_1_kz_makers(store: Store) -> None:
    h("Q1. Появлялся ли на bybit.kz второй мейкер?")
    rows = store.conn.execute(
        "SELECT v.nick, a.side, COUNT(DISTINCT a.ad_id) ads,"
        " MIN(a.first_seen) t0, MAX(a.last_seen) t1"
        " FROM ad a JOIN advertiser v ON v.key=a.advertiser_key"
        " WHERE a.host=? GROUP BY v.nick, a.side ORDER BY v.nick", (KZ,)).fetchall()
    if not rows:
        print("  наблюдений по bybit.kz нет")
        return
    for r in rows:
        print(f"  {r['nick']:<24} side={r['side']}  объявлений={r['ads']:>3}  "
              f"{ts(r['t0'])} .. {ts(r['t1'])}")
    n = len({r["nick"] for r in rows})
    print(f"\n  различных мейкеров за всё окно: {n}")
    if n <= 1:
        print("  ВЫВОД: один мейкер по обе стороны. Пар нет и быть не может —")
        print("  тейкер платит спред дважды. Площадка непригодна структурно.")


def question_2_spread_by_stratum(store: Store) -> None:
    h("Q2. Исполнимый спред по стратам качества  (главная таблица)")
    times = observation_times(store, GLOBAL, min_spacing_sec=60)
    if not times:
        print("  нет моментов с полными наблюдениями обеих сторон")
        return
    print(f"  моментов наблюдения: {len(times)}  "
          f"({ts(times[0])} .. {ts(times[-1])})\n")

    # Книга читается ОДИН раз на момент. Раньше book_at() вызывался внутри
    # двойного цикла — 12 одинаковых SQL-запросов на каждый момент.
    # Полосы ВЗАИМОИСКЛЮЧАЮЩИЕ. На вложенных стратах эта таблица была
    # тавтологией: max по надмножеству не меньше max по подмножеству,
    # поэтому убывание сверху вниз получалось при любых данных.
    spreads_by: dict[tuple, list[float]] = {}
    ads_by: dict[tuple, set] = {}
    advs_by: dict[tuple, set] = {}
    for t in times:
        book = book_at(store, GLOBAL, t)
        bands = quality_bands(book)
        for amount in ANALYSIS_AMOUNTS_KZT:
            for name, band in bands:
                for a in band:
                    ads_by.setdefault((amount, name), set()).add(a.ad_id)
                    advs_by.setdefault((amount, name), set()).add(a.advertiser.key)
                bp = best_pair(band, amount)
                if bp:
                    spreads_by.setdefault((amount, name), []).append(
                        float(bp.gross_spread_pct))

    print(f"  {'сумма':>8} {'полоса':<14} {'набл':>5} {'есть пара':>10} "
          f"{'медиана':>9} {'P25':>8} {'P75':>8} {'макс':>8} {'ads':>6} {'лиц':>5}")
    print("  " + "-" * 90)
    violations = 0
    for amount in ANALYSIS_AMOUNTS_KZT:
        meds: list[float] = []
        for name, _ in quality_bands([]):
            spreads = spreads_by.get((amount, name), [])
            n_ads = len(ads_by.get((amount, name), ()))
            n_adv = len(advs_by.get((amount, name), ()))
            avail = 100 * len(spreads) / len(times)
            if not spreads:
                print(f"  {int(amount):>8} {name:<14} {len(times):>5} "
                      f"{avail:>9.0f}% {'—':>9} {'':>8} {'':>8} {'':>8} "
                      f"{n_ads:>6} {n_adv:>5}")
                continue
            med = statistics.median(spreads)
            meds.append(med)
            thin = " thin" if n_adv < 30 else ""
            print(f"  {int(amount):>8} {name:<14} {len(times):>5} "
                  f"{avail:>9.0f}% {med:>8.2f}% "
                  f"{q(spreads,0.25):>7.2f}% {q(spreads,0.75):>7.2f}% "
                  f"{max(spreads):>7.2f}% {n_ads:>6} {n_adv:>5}{thin}")
        if len(meds) > 1 and not all(meds[i] >= meds[i + 1]
                                     for i in range(len(meds) - 1)):
            violations += 1
    print(f"\n  номиналов с нарушенным порядком полос: {violations} "
          f"из {len(ANALYSIS_AMOUNTS_KZT)}")
    print("\n  Читать так: полосы взаимоисключающие, поэтому убывание медианы")
    print("  сверху вниз — проверяемое утверждение, а не свойство конструкции.")
    print("  Нарушение порядка — содержательный результат, а не сбой.")
    print("  'лиц' — уникальные контрагенты; при значении ниже 30 строка")
    print("  помечена thin и асимптотический вывод по ней не применяется.")


def question_3_persistence(store: Store) -> None:
    h("Q3. Persistence: стоит ли возможность или возникает и исчезает?")
    times = observation_times(store, GLOBAL, min_spacing_sec=60)
    if len(times) < 3:
        print("  слишком мало наблюдений")
        return
    amount = Decimal("300000")
    names = [n for n, _ in quality_bands([])]
    by_band: dict[str, tuple[list, list]] = {n: ([], []) for n in names}
    for t in times:                     # книга — один раз на момент
        book = book_at(store, GLOBAL, t)
        for name, band in quality_bands(book):
            bp = best_pair(band, amount)
            if bp:
                by_band[name][0].append(bp.advertiser_pair_key)
                by_band[name][1].append(float(bp.gross_spread_pct))

    for name in names:
        pair_keys, spreads = by_band[name]
        if not pair_keys:
            print(f"  {name:<14} пар не было")
            continue
        c = Counter(pair_keys)
        top_key, top_n = c.most_common(1)[0]
        share = 100 * top_n / len(pair_keys)
        uniq = len(c)
        std = statistics.pstdev(spreads) if len(spreads) > 1 else 0.0
        thin = "  [thin: <30 диад]" if uniq < 30 else ""
        print(f"  {name:<14} наблюдений с парой={len(pair_keys):>4}  "
              f"различных диад={uniq:>3}  "
              f"доля самой частой={share:>5.1f}%  σ(спред)={std:.3f}пп{thin}")
        print(f"  {'':14} самая частая пара: {top_key[:60]}")
    print("\n  Читать так: мало различных пар + высокая доля самой частой +")
    print("  σ близкая к нулю  =>  это не рынок, это две статичные витрины")
    print("  с фиксированной наценкой. Транзиентность — признак edge,")
    print("  персистентность — признак премии за риск.")


def question_4_lifetime(store: Store) -> None:
    h("Q4. Время жизни объявлений")
    for host in COLLECTOR.hosts:
        rows = store.conn.execute(
            "SELECT p.side, p.appeared_at, p.disappeared_at FROM ad_presence p"
            " WHERE p.host=? AND p.disappeared_at IS NOT NULL", (host,)).fetchall()
        live = store.conn.execute(
            "SELECT COUNT(*) FROM ad_presence WHERE host=? AND disappeared_at IS NULL",
            (host,)).fetchone()[0]
        if not rows:
            print(f"  {host}: закрытых интервалов нет "
                  f"(живых сейчас: {live}) — окно наблюдения мало")
            continue
        for side in ("1", "0"):
            d = [r["disappeared_at"] - r["appeared_at"]
                 for r in rows if r["side"] == side]
            if not d:
                continue
            label = "мы покупаем" if side == "1" else "мы продаём"
            print(f"  {host} side={side} ({label})  закрыто={len(d):>4} "
                  f"живых={live:>4}")
            print(f"    P25={q(d,0.25)/60:>6.1f}м  P50={q(d,0.5)/60:>6.1f}м  "
                  f"P75={q(d,0.75)/60:>6.1f}м  P90={q(d,0.9)/60:>6.1f}м")
        print("    ВНИМАНИЕ: правая цензура — объявления, живые до сих пор,")
        print("    в перцентили не входят, поэтому реальные значения ВЫШЕ.")


# Полный перебор пар на момент — это ~28 000 оценок. Для распределения по
# бакетам limit применять НЕЛЬЗЯ (сместит выборку к большим спредам),
# поэтому вместо ограничения пар ограничиваем число моментов.
Q5_MAX_MOMENTS = 120


def question_5_buckets(store: Store) -> None:
    h("Q5. Меняется ли качество пары вместе с ростом спреда?")
    times = observation_times(store, GLOBAL, min_spacing_sec=900)
    if not times:
        print("  нет наблюдений")
        return
    if len(times) > Q5_MAX_MOMENTS:
        step = len(times) / Q5_MAX_MOMENTS
        times = [times[int(i * step)] for i in range(Q5_MAX_MOMENTS)]
    print(f"  моментов в выборке: {len(times)} (полный перебор пар, без limit)")
    amount = Decimal("300000")
    buckets: dict[str, list] = {}
    for t in times:
        ads = book_at(store, GLOBAL, t)
        for p in find_pairs(ads, amount):
            s = p.gross_spread_pct
            for lo, hi in SPREAD_BUCKETS_PCT:
                if s >= lo and (hi is None or s < hi):
                    name = f"{lo}–{hi}%" if hi else f"{lo}%+"
                    buckets.setdefault(name, []).append(p)
                    break

    print(f"  {'бакет':<12} {'пар':>7} {'зрелость ad':>12} {'оборот 30д':>11} "
          f"{'rate':>6} {'restrict':>9} {'подозр.remark':>14}")
    print("  " + "-" * 82)
    flags = ("только 1", "1-го лица", "однофамил", "точную сумму", "НЕ ОКРУГЛ")
    for lo, hi in SPREAD_BUCKETS_PCT:
        name = f"{lo}–{hi}%" if hi else f"{lo}%+"
        ps = buckets.get(name, [])
        if not ps:
            print(f"  {name:<12} {0:>7}")
            continue
        fin = statistics.median([p.quality_floor[0] for p in ps])
        rec = statistics.median([p.quality_floor[1] for p in ps])
        rate = statistics.median([p.quality_floor[2] for p in ps])
        restr = 100 * sum(1 for p in ps
                          if "restrictive_prefs__unverifiable" in p.rejection_reasons) / len(ps)
        susp = 100 * sum(1 for p in ps if any(
            f.lower() in (p.buy_ad.remark + p.sell_ad.remark).lower()
            for f in flags)) / len(ps)
        print(f"  {name:<12} {len(ps):>7} {fin:>12.0f} {rec:>11.0f} "
              f"{rate:>5.0f}% {restr:>8.0f}% {susp:>13.0f}%")
    print("\n  'зрелость ad' — сколько сделок прошло через само объявление;")
    print("  'оборот 30д' — сделки КОНТРАГЕНТА за месяц. Это разные вещи:")
    print("  ветеран с 1107 сделками может держать объявление с finishNum=3.")
    print("\n  Гипотеза для проверки: если 3%+ живёт у слабых контрагентов,")
    print("  а 1–2% у сильных — это разные премии. Если качество одинаковое")
    print("  во всех бакетах — спред не объясняется качеством вообще.")


def main() -> int:
    store = Store(COLLECTOR.db_path)
    try:
        n = store.conn.execute("SELECT COUNT(*) FROM poll_run").fetchone()[0]
        print(f"БД: {COLLECTOR.db_path}   обходов записано: {n}")
        coverage(store)
        question_1_kz_makers(store)
        question_2_spread_by_stratum(store)
        question_3_persistence(store)
        question_4_lifetime(store)
        question_5_buckets(store)
        h("НАПОМИНАНИЕ")
        print("  Ни один из этих ответов не измеряет P(перевод дойдёт) и")
        print("  P(счёт заблокируют). Они не наблюдаемы в API ни в каком виде.")
        print("  Положительный результат здесь = основание для ОДНОЙ реальной")
        print("  сделки на 50 000 KZT, а не для масштабирования.")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
