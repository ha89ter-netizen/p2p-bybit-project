#!/bin/bash
# Компактная сводка состояния прогона. Используется наблюдателем и вручную.
cd "$(dirname "$0")/.."
python3 - <<'PY'
import sqlite3, time, os
from datetime import datetime, timezone
db = "data/p2p.sqlite"
if not os.path.exists(db):
    print("БД ещё не создана"); raise SystemExit
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
q = lambda s, *a: c.execute(s, a).fetchone()[0]
t0, t1 = q("SELECT MIN(started_at) FROM poll_run"), q("SELECT MAX(started_at) FROM poll_run")
span = (t1 - t0) / 3600
cycles = q("SELECT COUNT(*) FROM poll_run") // 4
bad = q("SELECT COUNT(*) FROM poll_run WHERE complete=0")
gap = (time.time() - t1) / 60
size = os.path.getsize(db) / 1e6
print(f"собрано {span:.1f}ч / 72ч · циклов {cycles} · неполных обходов {bad}")
print(f"последний обход {gap:.1f} мин назад · БД {size:.1f} МБ · "
      f"объявлений {q('SELECT COUNT(*) FROM ad')} · состояний {q('SELECT COUNT(*) FROM ad_state')}")
kz = q("SELECT COUNT(DISTINCT advertiser_key) FROM ad WHERE host='api2.bybit.kz'")
print(f"мейкеров на bybit.kz: {kz}" + ("  <-- ПОЯВИЛСЯ ВТОРОЙ, перепроверить площадку!" if kz > 1 else ""))
PY
