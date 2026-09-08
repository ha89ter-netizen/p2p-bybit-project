"""Локальная панель наблюдения.

Слушает ТОЛЬКО 127.0.0.1 — снаружи машины недоступна физически, никакого
хостинга и никакой публикации. Это и есть контроль доступа: чтобы открыть
панель, нужно сидеть за этим компьютером.

    python3 -m dashboard.server          # http://127.0.0.1:8787
    python3 -m dashboard.server --port 9000

База открывается ТОЛЬКО НА ЧТЕНИЕ. Панель не может помешать сбору и не
может испортить данные: ни одного запроса на запись здесь нет.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
sys.path.insert(0, str(PROJECT))

from analysis.matching import band_names, evaluate, iter_pairs_by_spread, quality_bands  # noqa: E402
from analysis.replay import book_at, observation_times  # noqa: E402
from config.settings import COLLECTOR, MATCHING, PAPER, TELEGRAM  # noqa: E402
from analysis.paper import two_ledgers  # noqa: E402
from analysis.segments import collect as collect_segments, quality_vs_spread  # noqa: E402
from capital.allocation import DEFAULT_RATE, AllocationPolicy, allocate, summarize  # noqa: E402
from capital.portfolio import PORTFOLIO_MODELS, build_portfolios  # noqa: E402
from capital.prices import FxProvider, NullPriceProvider  # noqa: E402
from capital.study import run_study  # noqa: E402
from storage.db import Store  # noqa: E402

GLOBAL = "api2.bybit.com"
AMOUNT = Decimal(os.getenv("DASH_AMOUNT_KZT", "300000"))

# Окно подтверждающего прогона — из PROTOCOL.md, чтобы панель показывала
# прогресс, а не абстрактное «работает».
FREEZE_START = 1788797772.0          # 2026-09-07T16:16:12Z
FREEZE_END = FREEZE_START + 30 * 86400


def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{COLLECTOR.db_path}?mode=ro", uri=True)


def _f(x) -> float:
    return float(x)


def collector_alive() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "collector.poller"],
                             capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:                                  # noqa: BLE001
        return False


def health() -> dict:
    c = _db()
    try:
        n, first, last = c.execute(
            "SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM poll_run"
        ).fetchone()
        bad = c.execute("SELECT COUNT(*) FROM poll_run WHERE complete=0").fetchone()[0]
        conf, conf_bad = c.execute(
            "SELECT COUNT(*), SUM(complete=0) FROM poll_run WHERE started_at>=?",
            (FREEZE_START,)).fetchone()
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("ad", "ad_state", "advertiser", "advertiser_state",
                            "ad_presence", "raw_response")}
        recent = [dict(zip(("started", "host", "side", "items", "complete", "ms", "err"), r))
                  for r in c.execute(
                      "SELECT started_at, host, side, n_items, complete, latency_ms, error"
                      " FROM poll_run ORDER BY started_at DESC LIMIT 12")]
    finally:
        c.close()

    now = time.time()
    du = shutil.disk_usage(str(PROJECT))
    db_bytes = sum(p.stat().st_size for p in Path(COLLECTOR.db_path).parent.glob("p2p.sqlite*"))
    hours = (last - first) / 3600 if last and first else 0
    return {
        "alive": collector_alive(),
        "lag_sec": round(now - last) if last else None,
        "polls": n, "incomplete": bad,
        "coverage_pct": round(100 * (n - bad) / n, 1) if n else 0,
        "confirm_polls": conf or 0,
        "confirm_coverage_pct": round(100 * ((conf or 0) - (conf_bad or 0)) / conf, 1) if conf else 0,
        "hours": round(hours, 1),
        "counts": counts,
        "recent": recent,
        "window_pct": round(100 * (now - FREEZE_START) / (FREEZE_END - FREEZE_START), 1),
        "window_days_left": round((FREEZE_END - now) / 86400, 1),
        "disk_free_gb": round(du.free / 2**30, 1),
        "db_mb": round(db_bytes / 2**20, 1),
        "growth_mb_per_day": round(db_bytes / 2**20 / max(hours / 24, 0.01), 1) if hours else 0,
        "telegram": bool(TELEGRAM.enabled),
        "digest_hours": list(TELEGRAM.digest_at_hours),
        "amount": int(AMOUNT),
    }


def backups() -> list[dict]:
    from scripts.backup import DEFAULT_DIR, OFFSITE_DIR, missing_days
    out = []
    for p in sorted(DEFAULT_DIR.glob("p2p-*.sqlite.gz")) if DEFAULT_DIR.exists() else []:
        off = (OFFSITE_DIR / p.name).exists() if OFFSITE_DIR else False
        out.append({"name": p.name, "mb": round(p.stat().st_size / 2**20, 1),
                    "offsite": off})
    try:
        gaps = missing_days(COLLECTOR.db_path)
    except Exception:                                  # noqa: BLE001
        gaps = []
    return {"files": out, "missing": gaps}


def _latest_book(store: Store) -> list:
    times = observation_times(store, GLOBAL, min_spacing_sec=1)
    if not times:
        return [], None
    return book_at(store, GLOBAL, times[-1]), times[-1]


def live() -> dict:
    """Текущая книга: что видно прямо сейчас."""
    store = Store(COLLECTOR.db_path)
    try:
        book, t = _latest_book(store)
        if not book:
            return {"at": None, "bands": [], "top": [], "buy": 0, "sell": 0}

        mine = MATCHING
        buys = [a for a in book if a.side == "1"]
        sells = [a for a in book if a.side == "0"]

        bands = []
        for name, band in quality_bands(book):
            sp = [_f(p.gross_spread_pct) for p in iter_pairs_by_spread(band, AMOUNT)]
            bands.append({
                "name": name,
                "ads": len(band),
                "advertisers": len({a.advertiser.key for a in band}),
                "pairs": len(sp),
                "best": round(sp[0], 2) if sp else None,
                "median": round(statistics.median(sp), 2) if sp else None,
            })

        top = []
        for p in iter_pairs_by_spread(book, AMOUNT):
            if len(top) >= 12:
                break
            top.append({
                "spread": round(_f(p.gross_spread_pct), 2),
                "buy": str(p.buy_ad.price), "sell": str(p.sell_ad.price),
                "buy_nick": p.buy_ad.advertiser.nick,
                "sell_nick": p.sell_ad.advertiser.nick,
                "buy_orders": p.buy_ad.advertiser.recent_order_num,
                "sell_orders": p.sell_ad.advertiser.recent_order_num,
                "buy_rate": p.buy_ad.advertiser.recent_execute_rate,
                "sell_rate": p.sell_ad.advertiser.recent_execute_rate,
                "buy_left": str(p.buy_ad.last_quantity),
                "sell_left": str(p.sell_ad.last_quantity),
                "pays": list(p.common_payments)[:4],
            })

        prices_b = sorted(_f(a.price) for a in buys)
        prices_s = sorted(_f(a.price) for a in sells)
        return {
            "at": t, "buy": len(buys), "sell": len(sells),
            "bands": bands, "top": top,
            "best_buy": prices_b[0] if prices_b else None,
            "best_sell": prices_s[-1] if prices_s else None,
        }
    finally:
        store.close()


def window(hours: float = 12.0, max_moments: int = 80) -> dict:
    """Агрегат за окно: устойчив ли порядок полос."""
    store = Store(COLLECTOR.db_path)
    try:
        since = time.time() - hours * 3600
        times = [t for t in observation_times(store, GLOBAL, min_spacing_sec=300)
                 if t >= since][-max_moments:]
        names = band_names()
        best = {n: [] for n in names}
        allp = {n: [] for n in names}
        advs = {n: set() for n in names}
        dyads = {n: set() for n in names}
        viol = ok = 0
        series = []
        for t in times:
            cur = []
            row = {"t": t}
            for name, band in quality_bands(book_at(store, GLOBAL, t)):
                for a in band:
                    advs[name].add(a.advertiser.key)
                sp = []
                for p in iter_pairs_by_spread(band, AMOUNT):
                    sp.append(_f(p.gross_spread_pct))
                    dyads[name].add(p.advertiser_pair_key)
                if sp:
                    best[name].append(sp[0])
                    allp[name].append(statistics.median(sp))
                    cur.append(sp[0])
                    row[name] = round(sp[0], 2)
                else:
                    cur.append(None)
                    row[name] = None
            series.append(row)
            c = [x for x in cur if x is not None]
            if len(c) > 1:
                ok += 1
                if not all(c[i] >= c[i + 1] for i in range(len(c) - 1)):
                    viol += 1

        def q(v, f):
            w = sorted(v)
            return round(w[min(len(w) - 1, int(len(w) * f))], 2) if w else None

        rows = []
        for n in names:
            rows.append({
                "name": n,
                "best_median": round(statistics.median(best[n]), 2) if best[n] else None,
                "p25": q(best[n], .25), "p75": q(best[n], .75),
                "all_median": round(statistics.median(allp[n]), 2) if allp[n] else None,
                "advertisers": len(advs[n]), "dyads": len(dyads[n]),
                "thin": len(dyads[n]) < 30,
            })
        return {"moments": len(times), "hours": hours, "rows": rows,
                "violations": viol, "checked": ok,
                "violation_pct": round(100 * viol / ok, 0) if ok else None,
                "series": series}
    finally:
        store.close()


# Журналы прогоняют всю историю дважды — секунды, а не миллисекунды.
# Считать их на каждый запрос значит держать панель в блокировке, поэтому
# они пересчитываются фоновым потоком, а запрос всегда получает готовое.
_LEDGERS: dict = {"data": None, "at": 0.0, "error": None, "busy": False}
_LEDGER_TTL = 180.0


def _ledger_worker():
    while True:
        try:
            _LEDGERS["busy"] = True
            data = _compute_ledgers()
            _LEDGERS.update(data=data, at=time.time(), error=None)
        except Exception as exc:                        # noqa: BLE001
            _LEDGERS["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            _LEDGERS["busy"] = False
        time.sleep(_LEDGER_TTL)


def ledgers() -> dict:
    """Готовый снимок журналов. Никогда не блокирует запрос."""
    d = _LEDGERS.get("data")
    if d is None:
        return {"pending": True, "error": _LEDGERS.get("error"),
                "busy": _LEDGERS.get("busy", False)}
    out = dict(d)
    out["age_sec"] = round(time.time() - _LEDGERS["at"])
    out["error"] = _LEDGERS.get("error")
    return out


def _research(realistic) -> dict:
    """Слой Capital Lab: сегменты, распределение, портфели.

    Считается там же, где журналы — один прогон истории на всё.
    """
    prices = NullPriceProvider()          # источника цен активов нет
    store = Store(COLLECTOR.db_path)
    try:
        seg_dis = collect_segments(store, GLOBAL, AMOUNT, hours=24, mode="disjoint")
        seg_nest = collect_segments(store, GLOBAL, AMOUNT, hours=24, mode="nested")
    finally:
        store.close()

    policy = AllocationPolicy(rate_pct=DEFAULT_RATE)
    events = allocate(realistic.trades, policy)
    summary = summarize(events, policy,
                        total_net_pnl_kzt=realistic.total_pnl_kzt)
    ports = {n: p.valuation(prices)
             for n, p in build_portfolios(events, PORTFOLIO_MODELS, prices).items()}

    study = run_study(realistic, PAPER.capital_kzt, prices=prices)

    fx = FxProvider(COLLECTOR.db_path).quote()

    def dec(x):
        return None if x is None else float(x)

    return {
        "segments": {
            "disjoint": [m.to_dict() for m in seg_dis],
            "nested": [m.to_dict() for m in seg_nest],
            "quality_vs_spread": quality_vs_spread(seg_dis),
            "selected": "good",
        },
        "allocation": {
            "policy": summary.policy,
            "rate_pct": float(summary.rate_pct),
            "events": summary.events,
            "contributing": summary.contributing,
            "allocatable_kzt": dec(summary.allocatable_kzt),
            "allocated_kzt": dec(summary.allocated_kzt),
            "retained_kzt": dec(summary.retained_kzt),
            "losses_kzt": dec(summary.losses_kzt),
            "check_ok": summary.check_ok,
            "recent": [{
                "at": e.at, "cycle_kzt": dec(e.cycle_capital_kzt),
                "net_kzt": dec(e.net_pnl_kzt), "rate": float(e.rate_pct),
                "allocated_kzt": dec(e.allocated_kzt),
                "retained_kzt": dec(e.retained_kzt), "id": e.event_id,
            } for e in events[-10:][::-1]],
        },
        "portfolios": {n: {
            "model": v["model"],
            "weights": {k: float(w) for k, w in v["weights"].items()},
            "contributions": v["contributions"],
            "contributed_kzt": dec(v["contributed_kzt"]),
            "market_value_kzt": dec(v["market_value_kzt"]),
            "total_return_pct": dec(v["total_return_pct"]),
            "priced": v["priced"],
            "by_asset": {k: {"contributed_kzt": dec(a["contributed_kzt"]),
                             "market_value_kzt": dec(a["market_value_kzt"]),
                             "weight_pct": dec(a["weight_pct"])}
                         for k, a in v["by_asset"].items()},
        } for n, v in ports.items()},
        "study": study,
        "fx": (None if fx is None else {
            "rate": float(fx.rate_kzt_per_usd), "source": fx.source,
            "note": fx.note, "sample": fx.sample, "at": fx.at}),
        "price_source": prices.name,
        "priced": prices.available(),
    }


def _compute_ledgers() -> dict:
    """Два журнала: реалистичное участие и недостижимый эталон 100%."""
    store = Store(COLLECTOR.db_path)
    try:
        r = two_ledgers(store, GLOBAL, cfg=PAPER,
                        participation=PAPER.participation_pct)
    finally:
        store.close()

    def pack(x, label):
        eq = [{"t": e.at, "cap": float(e.capital_kzt)} for e in x.equity[-240:]]
        return {
            "label": label,
            "trades": x.n,
            "start": float(x.start_capital_kzt),
            "final": float(x.final_capital_kzt),
            "pnl": float(x.total_pnl_kzt),
            "return_pct": round(float(x.return_pct(x.start_capital_kzt)), 2),
            "per_day": float(x.pnl_per_day_kzt),
            "hours": round(x.hours, 1),
            "outcomes": x.outcomes(),
            "skipped_absent": x.entries_skipped_absent,
            "skipped_no_capital": x.entries_skipped_no_capital,
            "skipped_no_pair": x.entries_skipped_no_pair,
            "breakeven": (round(float(x.breakeven_success_rate()) * 100, 1)
                          if x.breakeven_success_rate() is not None else None),
            "equity": eq,
            "last": [{
                "t": t.decided_at,
                "spread": round(float((t.sell_price_expected / t.buy_price - 1) * 100), 2),
                "buy": str(t.buy_price), "sell": str(t.sell_price_actual or "—"),
                "pnl": float(t.pnl_kzt), "outcome": t.outcome,
            } for t in x.trades[-14:][::-1]],
        }

    return {
        "round_kzt": int(PAPER.capital_kzt * PAPER.deploy_pct / 100),
        "compound": PAPER.compound,
        "participation": float(PAPER.participation_pct),
        "realistic": pack(r["realistic"], f"участие {int(PAPER.participation_pct)}%"),
        "ceiling": pack(r["ceiling"], "эталон 100%"),
        "research": _research(r["realistic"]),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                   # noqa: N802
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                html = (ROOT / "index.html").read_bytes()
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/api/state":
                data = {"health": health(), "live": live(),
                        "window": window(), "backups": backups(),
                        "ledgers": ledgers(), "now": time.time()}
                return self._send(200, json.dumps(data, ensure_ascii=False).encode(),
                                  "application/json; charset=utf-8")
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:                        # noqa: BLE001
            body = json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                              ensure_ascii=False).encode()
            self._send(500, body, "application/json; charset=utf-8")

    def log_message(self, *a):                          # тише в консоли
        pass


def main() -> int:
    port = 8787
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    # 127.0.0.1, а не 0.0.0.0: снаружи машины сокет не существует.
    threading.Thread(target=_ledger_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"панель: http://127.0.0.1:{port}   (Ctrl+C — остановить)")
    print("доступна только с этого компьютера; база открыта только на чтение")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
