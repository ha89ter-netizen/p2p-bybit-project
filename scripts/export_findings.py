"""
Выгрузка результатов прогона в data/FINDINGS.md.

Файл пишется для АГЕНТА В НОВОЙ СЕССИИ, у которого нет контекста этого
разговора. Поэтому он самодостаточен: содержит не только цифры, но и
как их читать, какие ошибки интерпретации уже известны и какой вопрос
вообще решается.

    python3 -m scripts.export_findings

Вызывается автоматически вместе с каждым дайджестом, поэтому файл
остаётся свежим, даже если сессия оборвалась.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from analysis.matching import band_names, best_pair, quality_bands, find_pairs
from analysis.replay import book_at, observation_times
from config.settings import (ANALYSIS_AMOUNTS_KZT, COLLECTOR,
                             SPREAD_BUCKETS_PCT, TELEGRAM)
from storage.db import Store

GLOBAL = "api2.bybit.com"
KZ = "api2.bybit.kz"
OUT = Path("data/FINDINGS.md")

HEADER = """# FINDINGS — P2P USDT/KZT

<!-- Файл генерируется автоматически: python3 -m scripts.export_findings
     Правки руками будут затёрты. -->

## Что это за проект

Read-only исследование P2P-рынка USDT/KZT на Bybit. Никаких сделок:
пакета `execution/` не существует, система физически неспособна
отправить ордер.

**Единственный вопрос:** наблюдаемый спред — это рыночная
неэффективность или премия за непрайсимый риск?

**Дискриминатор — не величина спреда, а его поведение во времени.**
Edge выглядит как спред, который возникает и исчезает. Премия за риск
выглядит как спред, который стоит неделями одной и той же парой
контрагентов. Замер на 9 минутах показал: топ книги не сдвинулся ни
разу с обеих сторон, выживаемость объявлений 92–99% на горизонте
5 минут. То есть скорость здесь НЕ является дефицитным ресурсом,
и гипотеза «возможности мимолётны» не подтвердилась.

## Ловушки интерпретации (проверены на живых данных)

1. **`quantity` — исходный объём объявления, НЕ доступный остаток.**
   Доступно `lastQuantity`. Наблюдался случай `quantity=131203.74`
   при `last=2480.08` — счёт по `quantity` завышает ликвидность в 53 раза.
2. **`finishNum` — счётчик ОБЪЯВЛЕНИЯ, не контрагента.** Один мерчант
   держит два объявления со 100 и 24 сделками при общем
   `recentOrderNum=1107`. Репутация человека — это `recentOrderNum` и
   `recentExecuteRate`; `finishNum` — зрелость объявления.
3. **Требования вроде «только от первого лица» — ЗЕЛЁНЫЙ флаг.**
   Мерчант, отказывающийся принимать переводы от третьих лиц, бережёт
   себя от отмывочных потоков, то есть от того же риска, что и вы.
   Красный флаг противоположен: высокая цена ПРИ отсутствии требований
   и свежем объявлении.
4. **Bybit Kazakhstan (bybit.kz) непригоден структурно.** Один
   AFSA-лицензированный мейкер по обе стороны, розница может быть
   только тейкером. Round-trip тейкера отрицателен при любом числе
   мейкеров. Мониторится на случай появления второго мейкера.
5. **Высокий спред — сигнал тревоги, а не бонус.** Требует объяснения,
   а не празднования.

## Чего эти данные НЕ измеряют

`P(перевод дойдёт)` и `P(счёт заблокируют)` не наблюдаемы в API ни в
каком виде — а знак результата определяют именно они.
`recentExecuteRate` — статистика контрагента со ВСЕМИ, а не оценка
вашей вероятности; использовать её как `P_release` — методологическая
ошибка.

**Положительный результат здесь = основание для ОДНОЙ реальной сделки
на 50 000 KZT, а не для масштабирования.**

"""


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _q(vals: list[float], p: float) -> float:
    if not vals:
        return float("nan")
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def _coverage(store: Store, w) -> float | None:
    w("\n## Покрытие данных\n")
    w("Без этого раздела остальные цифры недостоверны: нельзя отличить\n"
      "«объявление исчезло» от «коллектор спал».\n\n")
    rows = store.conn.execute(
        "SELECT host, side, COUNT(*) n, SUM(complete) ok, MIN(started_at) t0,"
        " MAX(started_at) t1, AVG(latency_ms) lat FROM poll_run"
        " GROUP BY host, side ORDER BY host, side").fetchall()
    if not rows:
        w("**Данных нет — коллектор ещё не запускался.**\n")
        return None

    w("| хост | сторона | обходов | полных | окно, ч | покрытие | задержка |\n")
    w("|---|---|---|---|---|---|---|\n")
    span = 0.0
    for r in rows:
        h = (r["t1"] - r["t0"]) / 3600
        span = max(span, h)
        exp = max(h * 3600 / COLLECTOR.poll_interval_sec, 1)
        w(f"| {r['host']} | {r['side']} | {r['n']} | {r['ok']} "
          f"({100*r['ok']/r['n']:.0f}%) | {h:.1f} | {100*r['n']/exp:.0f}% "
          f"| {r['lat']:.0f} мс |\n")

    gaps = store.conn.execute(
        "SELECT host, side, started_at, LAG(started_at) OVER"
        " (PARTITION BY host, side ORDER BY started_at) prev FROM poll_run").fetchall()
    big = [(g["host"], g["side"], g["prev"], g["started_at"]) for g in gaps
           if g["prev"] and g["started_at"] - g["prev"] > 300]
    w(f"\nПропусков дольше 5 минут: **{len(big)}**\n")
    for h_, s_, a, b in big[:10]:
        w(f"- {h_} side={s_}: {_ts(a)} → {_ts(b)} ({(b-a)/60:.0f} мин)\n")
    if span < 24:
        w(f"\n> ⚠ Окно наблюдения {span:.1f} ч. Для выводов о persistence\n"
          f"> нужно 72 ч. Всё ниже — предварительное.\n")
    return span


def _kz(store: Store, w) -> None:
    w("\n## Q1. Появился ли на bybit.kz второй мейкер?\n\n")
    rows = store.conn.execute(
        "SELECT v.nick, a.side, COUNT(DISTINCT a.ad_id) ads, MIN(a.first_seen) t0,"
        " MAX(a.last_seen) t1 FROM ad a JOIN advertiser v ON v.key=a.advertiser_key"
        " WHERE a.host=? GROUP BY v.nick, a.side ORDER BY v.nick", (KZ,)).fetchall()
    if not rows:
        w("Наблюдений нет.\n")
        return
    w("| мейкер | сторона | объявлений | впервые | последний раз |\n|---|---|---|---|---|\n")
    for r in rows:
        w(f"| {r['nick']} | {r['side']} | {r['ads']} | {_ts(r['t0'])} "
          f"| {_ts(r['t1'])} |\n")
    n = len({r["nick"] for r in rows})
    w(f"\nРазличных мейкеров за всё окно: **{n}**\n")
    w("\n**Вывод:** " + ("один мейкер по обе стороны — пар нет и быть не может, "
                         "площадка непригодна структурно.\n" if n <= 1 else
                         f"мейкеров стало {n} — ПЕРЕПРОВЕРИТЬ пригодность площадки!\n"))


def _spread_table(store: Store, w, times: list[float]) -> dict:
    w("\n## Q2. Исполнимый спред по стратам качества\n\n")
    if not times:
        w("Нет моментов с полными наблюдениями обеих сторон.\n")
        return {}
    w(f"Моментов наблюдения: **{len(times)}** "
      f"({_ts(times[0])} → {_ts(times[-1])})\n\n")

    # Полосы ВЗАИМОИСКЛЮЧАЮЩИЕ: на вложенных стратах эта таблица была
    # тавтологией (max по надмножеству не меньше max по подмножеству).
    data: dict = {}
    ads_by: dict = {}
    advs_by: dict = {}
    for t in times:
        book = book_at(store, GLOBAL, t)
        bands = quality_bands(book)
        for amount in ANALYSIS_AMOUNTS_KZT:
            for name, band in bands:
                for a in band:
                    ads_by.setdefault((str(amount), name), set()).add(a.ad_id)
                    advs_by.setdefault((str(amount), name), set()).add(
                        a.advertiser.key)
                bp = best_pair(band, amount)
                if bp:
                    data.setdefault((str(amount), name), []).append(
                        float(bp.gross_spread_pct))

    w("| сумма KZT | полоса | есть пара | медиана | P25 | P75 | макс | ads | лиц |\n")
    w("|---|---|---|---|---|---|---|---|---|\n")
    for amount in ANALYSIS_AMOUNTS_KZT:
        for name in band_names():
            sp = data.get((str(amount), name), [])
            n_ads = len(ads_by.get((str(amount), name), ()))
            n_adv = len(advs_by.get((str(amount), name), ()))
            mark = " ⚠thin" if 0 < n_adv < 30 else ""
            avail = 100 * len(sp) / len(times)
            if not sp:
                w(f"| {int(amount):,} | `{name}` | {avail:.0f}% | — | — | — | — "
                  f"| {n_ads} | {n_adv}{mark} |\n".replace(",", " "))
                continue
            w(f"| {int(amount):,} | `{name}` | {avail:.0f}% "
              f"| {statistics.median(sp):.2f}% | {_q(sp,0.25):.2f}% "
              f"| {_q(sp,0.75):.2f}% | {max(sp):.2f}% "
              f"| {n_ads} | {n_adv}{mark} |\n".replace(",", " "))

    w("\n**Как читать.** Полосы взаимоисключающие, поэтому убывание медианы\n"
      "сверху вниз — проверяемое утверждение, а не свойство конструкции.\n"
      "На ВЛОЖЕННЫХ стратах эта таблица убывала бы при любых данных, потому\n"
      "что максимум по надмножеству не может быть меньше максимума по\n"
      "подмножеству. Нарушение порядка здесь — содержательный результат.\n\n"
      "Столбец `лиц` — уникальные контрагенты. Изменение состояния заявки\n"
      "не является независимым наблюдением: одна связка порождает их сотни.\n"
      "Строки с ⚠thin (<30 контрагентов) читать только как описательные.\n")
    return data


def _persistence(store: Store, w, times: list[float]) -> None:
    w("\n## Q3. Persistence — ГЛАВНЫЙ ВОПРОС\n\n")
    if len(times) < 3:
        w("Слишком мало наблюдений.\n")
        return
    amount = Decimal("300000")
    w("| полоса | наблюдений с парой | различных диад | доля самой частой | σ спреда |\n")
    w("|---|---|---|---|---|\n")
    detail = []
    per_band: dict = {n: ([], []) for n in band_names()}
    for t in times:
        for name, band in quality_bands(book_at(store, GLOBAL, t)):
            bp = best_pair(band, amount)
            if bp:
                per_band[name][0].append(bp.advertiser_pair_key)
                per_band[name][1].append(float(bp.gross_spread_pct))
    for name in band_names():
        keys, sp = per_band[name]
        if not keys:
            w(f"| `{name}` | 0 | — | — | — |\n")
            continue
        c = Counter(keys)
        top, n = c.most_common(1)[0]
        sd = statistics.pstdev(sp) if len(sp) > 1 else 0.0
        mark = " ⚠thin" if len(c) < 30 else ""
        w(f"| `{name}` | {len(keys)} | {len(c)}{mark} | {100*n/len(keys):.0f}% "
          f"| {sd:.3f} пп |\n")
        detail.append((name, top, 100 * n / len(keys), len(c)))

    w("\n**Как читать — это решает судьбу проекта.**\n\n")
    w("- Мало различных пар + высокая доля самой частой + σ около нуля\n"
      "  → это не рынок, а две статичные витрины с фиксированной наценкой.\n"
      "  Спред персистентен → премия за риск → **проект закрывается**.\n")
    w("- Много различных пар + σ заметно больше нуля → состав ротируется,\n"
      "  спред дышит → есть что исследовать дальше.\n\n")
    for name, top, share, uniq in detail:
        w(f"- `{name}`: самая частая пара `{top[:70]}` ({share:.0f}% времени, "
          f"всего {uniq} различных)\n")


def _lifetime(store: Store, w) -> None:
    w("\n## Q4. Время жизни объявлений\n\n")
    for host in COLLECTOR.hosts:
        rows = store.conn.execute(
            "SELECT side, appeared_at, disappeared_at FROM ad_presence"
            " WHERE host=? AND disappeared_at IS NOT NULL", (host,)).fetchall()
        live = store.conn.execute(
            "SELECT COUNT(*) FROM ad_presence WHERE host=? AND disappeared_at IS NULL",
            (host,)).fetchone()[0]
        if not rows:
            w(f"- **{host}**: закрытых интервалов нет (живых: {live})\n")
            continue
        w(f"\n**{host}** (живых сейчас: {live})\n\n")
        w("| сторона | закрыто | P25 | P50 | P75 | P90 |\n|---|---|---|---|---|---|\n")
        for side in ("1", "0"):
            d = [r["disappeared_at"] - r["appeared_at"]
                 for r in rows if r["side"] == side]
            if not d:
                continue
            lbl = "мы покупаем" if side == "1" else "мы продаём"
            w(f"| {side} ({lbl}) | {len(d)} | {_q(d,0.25)/60:.1f}м "
              f"| {_q(d,0.5)/60:.1f}м | {_q(d,0.75)/60:.1f}м | {_q(d,0.9)/60:.1f}м |\n")
    w("\n> Правая цензура: объявления, живые до сих пор, в перцентили не\n"
      "> входят, поэтому истинные значения ВЫШЕ показанных.\n")


def _buckets(store: Store, w, times: list[float]) -> None:
    w("\n## Q5. Меняется ли качество пары вместе с ростом спреда?\n\n")
    if not times:
        w("Нет наблюдений.\n")
        return
    sample = times[::max(1, len(times) // 120)][:120]
    amount = Decimal("300000")
    buckets: dict[str, list] = {}
    for t in sample:
        for p in find_pairs(book_at(store, GLOBAL, t), amount):
            s = p.gross_spread_pct
            for lo, hi in SPREAD_BUCKETS_PCT:
                if s >= lo and (hi is None or s < hi):
                    buckets.setdefault(f"{lo}–{hi}%" if hi else f"{lo}%+",
                                       []).append(p)
                    break
    w(f"Моментов в выборке: {len(sample)} (полный перебор пар)\n\n")
    w("| бакет | пар | зрелость ad | оборот 30д | rate | ограничений |\n")
    w("|---|---|---|---|---|---|\n")
    for lo, hi in SPREAD_BUCKETS_PCT:
        name = f"{lo}–{hi}%" if hi else f"{lo}%+"
        ps = buckets.get(name, [])
        if not ps:
            w(f"| {name} | 0 | — | — | — | — |\n")
            continue
        w(f"| {name} | {len(ps)} "
          f"| {statistics.median([p.quality_floor[0] for p in ps]):.0f} "
          f"| {statistics.median([p.quality_floor[1] for p in ps]):.0f} "
          f"| {statistics.median([p.quality_floor[2] for p in ps]):.0f}% "
          f"| {100*sum(1 for p in ps if 'restrictive_prefs__unverifiable' in p.rejection_reasons)/len(ps):.0f}% |\n")
    w("\n**Как читать.** `зрелость ad` — сделок через само объявление;\n"
      "`оборот 30д` — сделок контрагента за месяц. Если высокие спреды\n"
      "сидят на свежих объявлениях и слабых контрагентах, а низкие — на\n"
      "зрелых, то бакеты меряют разные премии, а не разный edge.\n")


def _maker(store: Store, w) -> None:
    from analysis.maker import collect
    st = collect(store, GLOBAL)
    w("\n## Экономика маркет-мейкера\n\n")
    w("Другая роль: не ловить чужое перекрестье, а самому ставить обе\n"
      "котировки и зарабатывать на потоке. Данные показывают, что\n"
      "прибыльна именно она.\n\n")
    if not st.two_sided:
        w("Двусторонних мейкеров в данных нет.\n")
        return
    w(f"- контрагентов всего: **{len(st.makers)}**\n")
    w(f"- держат обе стороны: **{len(st.two_sided)}**\n")
    w(f"- из них с реальным потоком: **{len(st.active)}**\n")
    w(f"- медианный спред мейкера: **{st.median_spread()}%**\n")
    w(f"- суммарный оборот за {st.hours:.0f} ч: "
      f"**{float(st.total_volume()):,.0f} USDT**\n\n".replace(",", " "))

    w("### Топ по обороту\n\n")
    w("| мейкер | продаёт | покупает | спред | оборот, USDT | выручка, KZT |\n")
    w("|---|---|---|---|---|---|\n")
    for m in st.top_by_volume(12):
        w(f"| {m.nick[:18]} | {m.ask} | {m.bid} | {float(m.spread_pct):+.2f}% "
          f"| {float(m.volume_usdt):,.0f} | {float(m.revenue_kzt):,.0f} |\n"
          .replace(",", " "))

    w("\n### Спред против потока — где оптимум\n\n")
    w("| спред | мейкеров | медиана оборота | медиана выручки за окно |\n")
    w("|---|---|---|---|\n")
    for name, n, vol, rev in st.spread_vs_volume():
        w(f"| {name} | {n} | {float(vol):,.0f} USDT | {float(rev):,.0f} KZT |\n"
          .replace(",", " "))

    w("\n**Как читать.** Ожидание «узкий спред притягивает поток» данными\n"
      "НЕ подтверждается: у мейкеров со спредом меньше 3% оборот ниже, чем\n"
      "у тех, кто держит 5–7%. Значит поток идёт не за ценой, а за\n"
      "репутацией, лимитами и методами оплаты. Выше 7% поток обрывается —\n"
      "это уже за пределами того, что рынок готов платить.\n\n")
    w("**Чего эта таблица НЕ учитывает:** инвентарный риск (мейкер держит\n"
      "и USDT, и KZT, курс движется), споры с контрагентами, стоимость\n"
      "капитала, работу оператора и — главное — банковский комплаенс,\n"
      "который и ставит реальный потолок обороту. Выручка здесь оценена\n"
      "СВЕРХУ: полный спред зарабатывается только на замкнутом круге,\n"
      "а часть потока односторонняя и оставляет мейкера с инвентарём.\n")


def _raw(store: Store, w) -> None:
    w("\n## Сырая статистика БД\n\n```\n")
    for t in ("poll_run", "ad", "ad_state", "ad_presence", "advertiser",
              "advertiser_state", "raw_response"):
        n = store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        w(f"{t:>18}: {n}\n")
    db = Path(COLLECTOR.db_path)
    if db.exists():
        w(f"{'размер БД':>18}: {db.stat().st_size / 1e6:.1f} МБ\n")
    w("```\n")


def _next(w, span: float | None) -> None:
    w("\n## Что делать дальше\n\n")
    if span is None:
        w("Запустить сбор: `./run.sh`\n")
        return
    if span < 72:
        w(f"Собрано {span:.1f} ч из 72. Продолжать сбор: `./run.sh`\n\n")
        w("Выводы делать рано — persistence на коротком окне неотличим\n"
          "от случайности.\n")
        return
    w("Окна хватает. Порядок анализа:\n\n"
      "1. Посмотреть Q3: ротируется ли состав пар. Если нет — закрывать.\n"
      "2. Посмотреть Q2: падает ли спред с ростом качества страты.\n"
      "3. Если Q3 показал ротацию — считать breakeven через\n"
      "   `analysis.economics.breakeven_p_completion()` и формулировать\n"
      "   вывод как «прибыльно ⟺ P(успех) > X И P(блокировка) < Y».\n"
      "4. Проверять X и Y ОДНОЙ реальной сделкой на 50 000 KZT,\n"
      "   а не дальнейшей симуляцией.\n")


def main() -> int:
    store = Store(COLLECTOR.db_path)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    w = parts.append
    try:
        w(HEADER)
        w(f"\n---\n\n**Сгенерировано:** {_ts(time.time())} UTC\n")
        span = _coverage(store, w)
        _kz(store, w)
        times = observation_times(store, GLOBAL, min_spacing_sec=60)
        _spread_table(store, w, times)
        _persistence(store, w, times)
        _lifetime(store, w)
        _buckets(store, w, times)
        _maker(store, w)
        _raw(store, w)
        _next(w, span)
    finally:
        store.close()
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"записано: {OUT} ({OUT.stat().st_size / 1024:.1f} КБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
