"""Финальный анализ подтверждающего прогона — строго по PROTOCOL.md.

Окно: 2026-09-07T16:16:12Z — 2026-10-07T16:16:12Z. Контур: api2.bybit.com
(основной), api2.bybit.kz — отдельное описательное сравнение.

Что считается (номера — разделы протокола):

  §3  основная переменная: спред КАЖДОЙ исполнимой пары внутри
      взаимоисключающей полосы качества; точечная оценка — медиана
      объединённого распределения за окно, интервал — блочный бутстрап
      по суткам. Вторичная: лучший исполнимый спред по полосам.
  §4/5 регрессия спреда пары на качество покупающей и продающей ноги
      раздельно, с фиксированным эффектом момента (поглощает и час, и
      день) и контролем платёжных рельсов. Ошибки: двусторонняя
      кластеризация по обоим контрагентам (Cameron-Gelbach-Miller) и,
      отдельно, по суткам; берётся бо́льшая. Позиционирование —
      описательное.
  §5  порог тонкой выборки: < 30 уникальных кластеров -> thin.
  §6  номинал 300 000 основной; 100k / 500k / 1M — устойчивость.
  §7  два контура раздельно.
      + разбивка по часу суток (исследовательский вопрос).
      + раздельно по версиям методики сбора 1.0.0 / 1.1.0.

Перебор пар полный: все исполнимые пары каждого момента. Единственная
выборка — для регрессии берётся до REG_PER_MOMENT пар на момент
(случайно, с фиксированным сидом), иначе 3.7 млн строк не помещаются в
чистый Python. Это выборка из ряда, а не отбор по значению.
"""

from __future__ import annotations

import collections
import datetime
import json
import math
import random
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.matching import iter_pairs_by_spread, quality_bands  # noqa: E402
from analysis.replay import book_at, observation_times  # noqa: E402
from config.settings import COLLECTOR  # noqa: E402
from storage.db import Store  # noqa: E402

F = int(datetime.datetime(2026, 9, 7, 16, 16, 12, tzinfo=datetime.timezone.utc).timestamp())
E = F + 30 * 86400
HOST = "api2.bybit.com"
KZ = "api2.bybit.kz"
AMOUNTS = (Decimal("100000"), Decimal("300000"), Decimal("500000"), Decimal("1000000"))
PRIMARY = Decimal("300000")
SPACING = 1800             # момент наблюдения раз в 30 минут
REG_PER_MOMENT = 200
BOOT = 1000
SEED = 20261008
BIN = 100                  # гистограмма спреда с шагом 0.01 п.п.
ALMATY = 5 * 3600

rng = random.Random(SEED)


def day_of(t):
    return time.strftime("%Y-%m-%d", time.gmtime(t + ALMATY))


def hour_of(t):
    return int(time.strftime("%H", time.gmtime(t + ALMATY)))


def hist_median(h: dict) -> float | None:
    n = sum(h.values())
    if not n:
        return None
    half, acc = n / 2, 0
    for k in sorted(h):
        acc += h[k]
        if acc >= half:
            return k / BIN
    return None


def hist_quant(h: dict, q: float) -> float | None:
    n = sum(h.values())
    if not n:
        return None
    lim, acc = n * q, 0
    for k in sorted(h):
        acc += h[k]
        if acc >= lim:
            return k / BIN
    return None


def boot_ci(by_day: dict, stat, B=BOOT):
    """Блочный бутстрап по суткам: сутки — блок, внутри них зависимость."""
    days = list(by_day)
    if len(days) < 2:
        return None, None
    vals = []
    for _ in range(B):
        pick = [days[rng.randrange(len(days))] for _ in days]
        v = stat(pick)
        if v is not None:
            vals.append(v)
    if len(vals) < B * 0.5:
        return None, None
    vals.sort()
    return vals[int(len(vals) * .025)], vals[int(len(vals) * .975) - 1]


# --------------------------------------------------------------------------
# линейная алгебра без numpy
# --------------------------------------------------------------------------

def inv(m):
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-12:
            raise ValueError("вырожденная матрица")
        a[c], a[p] = a[p], a[c]
        d = a[c][c]
        a[c] = [x / d for x in a[c]]
        for r in range(n):
            if r != c and a[r][c]:
                f = a[r][c]
                a[r] = [x - f * y for x, y in zip(a[r], a[c])]
    return [row[n:] for row in a]


def mm(a, b):
    return [[sum(x * y for x, y in zip(r, c)) for c in zip(*b)] for r in a]


def cluster_vcov(X, e, groups, Binv):
    k = len(X[0])
    sc = collections.defaultdict(lambda: [0.0] * k)
    for x, r, g in zip(X, e, groups):
        s = sc[g]
        for j in range(k):
            s[j] += x[j] * r
    G = len(sc)
    meat = [[0.0] * k for _ in range(k)]
    for s in sc.values():
        for i in range(k):
            si = s[i]
            if si:
                row = meat[i]
                for j in range(k):
                    row[j] += si * s[j]
    c = G / (G - 1) if G > 1 else 1.0
    V = mm(mm(Binv, meat), Binv)
    return [[c * v for v in row] for row in V], G


# --------------------------------------------------------------------------

def main() -> int:
    t_start = time.time()
    s = Store(COLLECTOR.db_path)
    v11 = s.conn.execute(
        "SELECT MIN(started_at) FROM poll_run WHERE method_version='1.1.0'").fetchone()[0]
    times = [t for t in observation_times(s, HOST, min_spacing_sec=SPACING) if F <= t <= E]
    import os
    if os.getenv("FA_LIMIT"):
        times = times[::max(1, len(times) // int(os.getenv("FA_LIMIT")))]
    print(f"моментов: {len(times)}", flush=True)

    names = None
    # гистограммы спреда пар: [номинал][полоса][сутки] -> Counter(бин)
    H = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(collections.Counter)))
    # лучший спред по моменту: [номинал][полоса] -> list[(t, best)]
    BEST = collections.defaultdict(lambda: collections.defaultdict(list))
    advs = collections.defaultdict(set)      # полоса -> контрагенты (300k)
    dyads = collections.defaultdict(set)
    pairs_n = collections.Counter()
    viol = checked = 0
    hour_best = collections.defaultdict(list)    # час -> лучший спред, вся книга, 300k
    hour_med = collections.defaultdict(list)     # час -> медиана пар момента, вся книга
    hour_n = collections.defaultdict(list)       # час -> число исполнимых пар
    ver_hist = {"1.0.0": collections.Counter(), "1.1.0": collections.Counter()}
    ver_moments = collections.Counter()
    reg_rows = []    # (moment, y, x[], buy_adv, sell_adv, dyad, day)
    kz_rows = []

    for i, t in enumerate(times):
        book = book_at(s, HOST, t)
        bands = quality_bands(book)
        if names is None:
            names = [n for n, _ in bands]
        d = day_of(t)
        ver = "1.1.0" if (v11 and t >= v11) else "1.0.0"
        ver_moments[ver] += 1

        for amt in AMOUNTS:
            cur = []
            for name, band in bands:
                ps = list(iter_pairs_by_spread(band, amt))
                if ps:
                    BEST[amt][name].append((t, float(ps[0].gross_spread_pct)))
                    cur.append(float(ps[0].gross_spread_pct))
                else:
                    cur.append(None)
                h = H[amt][name][d]
                for p in ps:
                    h[round(float(p.gross_spread_pct) * BIN)] += 1
                if amt == PRIMARY:
                    pairs_n[name] += len(ps)
                    for a in band:
                        advs[name].add(a.advertiser.key)
                    for p in ps:
                        dyads[name].add(p.advertiser_pair_key)
            if amt == PRIMARY:
                c = [x for x in cur if x is not None]
                if len(c) > 1:
                    checked += 1
                    if not all(c[j] >= c[j + 1] for j in range(len(c) - 1)):
                        viol += 1

        # вся книга, основной номинал: час суток, версии, регрессия
        allp = list(iter_pairs_by_spread(book, PRIMARY))
        hh = hour_of(t)
        hour_n[hh].append(len(allp))
        if allp:
            sp = [float(p.gross_spread_pct) for p in allp]
            hour_best[hh].append(sp[0])
            hour_med[hh].append(statistics.median(sp))
            for x in sp:
                ver_hist[ver][round(x * BIN)] += 1
            sample = allp if len(allp) <= REG_PER_MOMENT else rng.sample(allp, REG_PER_MOMENT)
            for p in sample:
                b, sl = p.buy_ad, p.sell_ad
                rails = set(p.common_payments)
                x = [math.log1p(b.advertiser.recent_order_num), float(b.advertiser.recent_execute_rate),
                     math.log1p(sl.advertiser.recent_order_num), float(sl.advertiser.recent_execute_rate),
                     1.0 if "150" in rails else 0.0, 1.0 if "203" in rails else 0.0,
                     1.0 if "549" in rails else 0.0]
                reg_rows.append((i, float(p.gross_spread_pct), x,
                                 b.advertiser.key, sl.advertiser.key, p.advertiser_pair_key, d))

        # лицензированный контур — только описательно
        kb = book_at(s, KZ, t)
        if kb:
            kp = list(iter_pairs_by_spread(kb, PRIMARY))
            kz_rows.append({
                "t": t, "ads": len(kb),
                "advs": len({a.advertiser.key for a in kb}),
                "asks": sorted(float(a.price) for a in kb if a.side == "1")[:1],
                "bids": sorted((float(a.price) for a in kb if a.side == "0"), reverse=True)[:1],
                "best": float(kp[0].gross_spread_pct) if kp else None,
            })

        if i % 100 == 0:
            print(f"  {i}/{len(times)}  {time.time() - t_start:.0f} с", flush=True)

    out = {"window": [F, E], "moments": len(times), "spacing_sec": SPACING,
           "versions_moments": dict(ver_moments), "bands": names}

    # ---- §3 основная и вторичная переменные по полосам и номиналам ----
    res = {}
    for amt in AMOUNTS:
        rows = []
        for name in names:
            byday = H[amt][name]
            pooled = collections.Counter()
            for h in byday.values():
                pooled.update(h)

            def stat(pick, byday=byday):
                acc = collections.Counter()
                for dd in pick:
                    acc.update(byday[dd])
                return hist_median(acc)

            lo, hi = boot_ci(byday, stat) if amt in (PRIMARY,) else (None, None)
            bests = [b for _, b in BEST[amt][name]]
            bday = collections.defaultdict(list)
            for tt, b in BEST[amt][name]:
                bday[day_of(tt)].append(b)

            def bstat(pick, bday=bday):
                v = [x for dd in pick for x in bday.get(dd, [])]
                return statistics.median(v) if v else None

            blo, bhi = boot_ci(bday, bstat) if amt == PRIMARY else (None, None)
            rows.append({
                "band": name,
                "pairs": sum(pooled.values()),
                "median": hist_median(pooled), "ci": [lo, hi],
                "p10": hist_quant(pooled, .10), "p90": hist_quant(pooled, .90),
                "share_positive": (sum(v for k, v in pooled.items() if k > 0) / sum(pooled.values())
                                   if pooled else None),
                "best_median": statistics.median(bests) if bests else None,
                "best_ci": [blo, bhi],
                "moments_with_pairs": len(bests),
            })
        res[str(int(amt))] = rows
    out["bands_by_amount"] = res
    out["clusters_300k"] = {n: {"advertisers": len(advs[n]), "dyads": len(dyads[n]),
                                "thin": len(dyads[n]) < 30} for n in names}
    out["order_violations_300k"] = {"violated": viol, "checked": checked}

    # ---- час суток ----
    out["by_hour"] = [{
        "hour": h, "moments": len(hour_n[h]),
        "pairs_median": statistics.median(hour_n[h]) if hour_n[h] else None,
        "best_median": statistics.median(hour_best[h]) if hour_best[h] else None,
        "pair_median": statistics.median(hour_med[h]) if hour_med[h] else None,
    } for h in range(24)]

    # ---- версии методики ----
    out["by_version"] = {v: {"pairs": sum(h.values()), "median": hist_median(h),
                             "moments": ver_moments[v]} for v, h in ver_hist.items()}

    # ---- §4/5 регрессия ----
    print("регрессия…", flush=True)
    by_m = collections.defaultdict(list)
    for r in reg_rows:
        by_m[r[0]].append(r)
    X, y, gb, gs, gd, gday = [], [], [], [], [], []
    for m, rs in by_m.items():
        if len(rs) < 2:
            continue
        k = len(rs[0][2])
        mx = [sum(r[2][j] for r in rs) / len(rs) for j in range(k)]
        my = sum(r[1] for r in rs) / len(rs)
        for r in rs:
            X.append([r[2][j] - mx[j] for j in range(k)])
            y.append(r[1] - my)
            gb.append(r[3]); gs.append(r[4]); gd.append(r[5]); gday.append(r[6])
    labels = ["log(1+сделок) покупающей", "успех% покупающей",
              "log(1+сделок) продающей", "успех% продающей",
              "рельс Kaspi", "рельс Halyk", "рельс Freedom"]
    # убрать столбцы без вариации (иначе матрица вырождена)
    keep = [j for j in range(len(labels)) if any(abs(x[j]) > 1e-12 for x in X)]
    X = [[x[j] for j in keep] for x in X]
    labels = [labels[j] for j in keep]
    k = len(keep)
    XtX = [[sum(x[a] * x[b] for x in X) for b in range(k)] for a in range(k)]
    Xty = [sum(x[a] * yy for x, yy in zip(X, y)) for a in range(k)]
    Binv = inv(XtX)
    beta = [sum(Binv[a][b] * Xty[b] for b in range(k)) for a in range(k)]
    e = [yy - sum(bj * xj for bj, xj in zip(beta, x)) for x, yy in zip(X, y)]
    Vb, Gb = cluster_vcov(X, e, gb, Binv)
    Vs, Gs = cluster_vcov(X, e, gs, Binv)
    Vd, Gd = cluster_vcov(X, e, gd, Binv)
    Vday, Gday = cluster_vcov(X, e, gday, Binv)
    coefs = []
    for j in range(k):
        v2 = Vb[j][j] + Vs[j][j] - Vd[j][j]
        if v2 <= 0:
            v2 = max(Vb[j][j], Vs[j][j])
        se_cgm = math.sqrt(v2)
        se_day = math.sqrt(max(Vday[j][j], 0))
        se = max(se_cgm, se_day)
        coefs.append({"var": labels[j], "beta": beta[j], "se_cgm": se_cgm,
                      "se_day": se_day, "se": se, "t": beta[j] / se if se else None})
    out["regression"] = {"n": len(y), "moments": len(by_m), "coefs": coefs,
                         "clusters": {"buy": Gb, "sell": Gs, "dyad": Gd, "day": Gday},
                         "spec": "спред пары (п.п.) ~ качество ног + рельсы | FE момента"}

    # ---- лицензированный контур ----
    kb = [r["best"] for r in kz_rows if r["best"] is not None]
    out["kz"] = {"moments": len(kz_rows),
                 "ads_median": statistics.median([r["ads"] for r in kz_rows]) if kz_rows else None,
                 "advs_median": statistics.median([r["advs"] for r in kz_rows]) if kz_rows else None,
                 "moments_with_pair": len(kb),
                 "best_median": statistics.median(kb) if kb else None}

    out["runtime_sec"] = round(time.time() - t_start)
    Path("reports").mkdir(exist_ok=True)
    Path("reports/_final_core.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("ГОТОВО", out["runtime_sec"], "с", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
