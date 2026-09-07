"""
SQLite-хранилище.

Стратегия: события, а не снапшоты.

Наивный вариант — писать всю книгу каждые 15 секунд — даёт ~150 объявлений
x 2 стороны x 5760 циклов = 1.7 млн строк в сутки, 99% из которых дубли.
Здесь ad_state пишется только когда состояние реально изменилось, а факт
присутствия объявления хранится интервалами в ad_presence.

poll_run — не служебная таблица, а часть данных. Без неё нельзя отличить
"объявление исчезло" от "коллектор спал / упала сеть", и вся статистика
времени жизни превращается в вымысел. На ноутбуке это критично.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from domain.models import Ad

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Каждый обход одной стороны книги, включая неудачные.
CREATE TABLE IF NOT EXISTS poll_run (
    id           INTEGER PRIMARY KEY,
    started_at   REAL NOT NULL,
    finished_at  REAL NOT NULL,
    host         TEXT NOT NULL,
    side         TEXT NOT NULL,
    complete     INTEGER NOT NULL,   -- 0 = обход прерван, к выводам не годится
    n_items      INTEGER NOT NULL,
    n_pages      INTEGER NOT NULL,
    total_count  INTEGER NOT NULL,   -- сколько объявлений заявляет сервер
    latency_ms   INTEGER NOT NULL,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS ix_poll_run_t ON poll_run(host, side, started_at);

CREATE TABLE IF NOT EXISTS advertiser (
    key         TEXT PRIMARY KEY,
    nick        TEXT NOT NULL,
    user_type   TEXT NOT NULL,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

-- Профиль КОНТРАГЕНТА во времени: пишется только на изменениях.
--
-- Здесь ТОЛЬКО 30-дневные скользящие метрики человека. finishNum/orderNum
-- сюда не входят — проверено на живых данных, что это счётчики конкретного
-- ОБЪЯВЛЕНИЯ (один контрагент: 100 в одном объявлении, 24 в другом, при
-- общем recentOrderNum=1107). Они живут в ad_state.
CREATE TABLE IF NOT EXISTS advertiser_state (
    key                 TEXT NOT NULL,
    host                TEXT NOT NULL,
    side                TEXT NOT NULL,
    observed_at         REAL NOT NULL,
    recent_order_num    INTEGER NOT NULL,
    recent_execute_rate INTEGER NOT NULL,
    latest_pay_ms       INTEGER NOT NULL,
    latest_release_ms   INTEGER NOT NULL,
    auth_tags           TEXT NOT NULL,
    is_online           INTEGER NOT NULL,
    PRIMARY KEY (key, host, side, observed_at)
);

CREATE TABLE IF NOT EXISTS ad (
    ad_id           TEXT PRIMARY KEY,
    host            TEXT NOT NULL,
    side            TEXT NOT NULL,
    advertiser_key  TEXT NOT NULL,
    token           TEXT NOT NULL,
    currency        TEXT NOT NULL,
    payments        TEXT NOT NULL,   -- JSON-массив id методов оплаты
    prefs           TEXT NOT NULL,   -- JSON tradingPreferenceSet
    remark          TEXT NOT NULL,
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ad_adv ON ad(advertiser_key);
CREATE INDEX IF NOT EXISTS ix_ad_hs  ON ad(host, side);

-- Цена/объём/лимиты. Строка появляется ТОЛЬКО при изменении.
CREATE TABLE IF NOT EXISTS ad_state (
    ad_id        TEXT NOT NULL,
    observed_at  REAL NOT NULL,
    price        TEXT NOT NULL,   -- TEXT, чтобы Decimal был точным
    quantity     TEXT NOT NULL,   -- ИСХОДНЫЙ объём объявления
    frozen_qty   TEXT NOT NULL,
    min_amount   TEXT NOT NULL,
    max_amount   TEXT NOT NULL,
    version      INTEGER NOT NULL,
    status       INTEGER NOT NULL,
    finish_num   INTEGER NOT NULL,   -- сделок через ЭТО объявление
    order_num    INTEGER NOT NULL,
    last_qty     TEXT NOT NULL,      -- ДОСТУПНЫЙ остаток (quantity-exec-frozen)
    executed_qty TEXT NOT NULL,
    PRIMARY KEY (ad_id, observed_at)
);
CREATE INDEX IF NOT EXISTS ix_ad_state_t ON ad_state(observed_at);

-- Интервалы присутствия. disappeared_at IS NULL => объявление живо.
CREATE TABLE IF NOT EXISTS ad_presence (
    id              INTEGER PRIMARY KEY,
    ad_id           TEXT NOT NULL,
    host            TEXT NOT NULL,
    side            TEXT NOT NULL,
    appeared_at     REAL NOT NULL,
    disappeared_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_presence_open ON ad_presence(host, side, disappeared_at);
CREATE INDEX IF NOT EXISTS ix_presence_ad   ON ad_presence(ad_id);

-- Страховка от смены формата API. Сэмплируется, не пишется каждый цикл.
CREATE TABLE IF NOT EXISTS raw_response (
    id           INTEGER PRIMARY KEY,
    captured_at  REAL NOT NULL,
    host         TEXT NOT NULL,
    side         TEXT NOT NULL,
    page         INTEGER NOT NULL,
    body_gz      BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_t ON raw_response(captured_at);

CREATE TABLE IF NOT EXISTS payment_method (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
"""


# Колонки, добавленные после первых сборов. CREATE TABLE IF NOT EXISTS их
# не добавит в уже существующую БД, и запросы упадут на ровном месте.
_REQUIRED_COLUMNS = {
    "ad_state": {"finish_num", "order_num", "last_qty", "executed_qty"},
    "advertiser_state": {"host", "side"},
}


class SchemaMismatch(RuntimeError):
    pass


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._check_schema(path)

    def _check_schema(self, path: str) -> None:
        for table, needed in _REQUIRED_COLUMNS.items():
            have = {r["name"] for r in self.conn.execute(
                f"PRAGMA table_info({table})").fetchall()}
            missing = needed - have
            if missing:
                raise SchemaMismatch(
                    f"БД {path} создана старой версией: в таблице {table} нет "
                    f"колонок {sorted(missing)}. Схема менялась после того, как "
                    f"этот файл был создан. Удалите его и начните сбор заново "
                    f"(история несовместима) либо мигрируйте вручную.")

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---------- запись цикла ----------

    def record_poll(self, *, started_at: float, finished_at: float, host: str,
                    side: str, complete: bool, n_items: int, n_pages: int,
                    total_count: int, latency_ms: int, error: str | None) -> None:
        self.conn.execute(
            "INSERT INTO poll_run (started_at, finished_at, host, side, complete,"
            " n_items, n_pages, total_count, latency_ms, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (started_at, finished_at, host, side, int(complete), n_items,
             n_pages, total_count, latency_ms, error))

    def upsert_ads(self, ads: list[Ad]) -> tuple[int, int]:
        """Возвращает (сколько состояний записано, сколько новых объявлений)."""
        import json as _json
        c = self.conn
        new_states = 0
        new_ads = 0

        for ad in ads:
            adv = ad.advertiser
            c.execute(
                "INSERT INTO advertiser (key, nick, user_type, first_seen, last_seen)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen,"
                " nick=excluded.nick",
                (adv.key, adv.nick, adv.user_type, ad.observed_at, ad.observed_at))

            prev_adv = c.execute(
                "SELECT recent_order_num, recent_execute_rate, latest_pay_ms,"
                " latest_release_ms, is_online FROM advertiser_state"
                " WHERE key=? AND host=? AND side=? ORDER BY observed_at DESC LIMIT 1",
                (adv.key, ad.host, ad.side)).fetchone()
            adv_now = (adv.recent_order_num, adv.recent_execute_rate,
                       adv.latest_pay_ms, adv.latest_release_ms, int(adv.is_online))
            if prev_adv is None or tuple(prev_adv) != adv_now:
                c.execute(
                    "INSERT OR IGNORE INTO advertiser_state (key, host, side,"
                    " observed_at, recent_order_num, recent_execute_rate,"
                    " latest_pay_ms, latest_release_ms, auth_tags, is_online)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (adv.key, ad.host, ad.side, ad.observed_at, *adv_now[:4],
                     _json.dumps(list(adv.auth_tags)), adv_now[4]))

            existed = c.execute("SELECT 1 FROM ad WHERE ad_id=?",
                                (ad.ad_id,)).fetchone() is not None
            c.execute(
                "INSERT INTO ad (ad_id, host, side, advertiser_key, token, currency,"
                " payments, prefs, remark, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(ad_id) DO UPDATE SET last_seen=excluded.last_seen",
                (ad.ad_id, ad.host, ad.side, adv.key, ad.token, ad.currency,
                 _json.dumps(list(ad.payments)), _json.dumps(ad.prefs.raw),
                 ad.remark, ad.observed_at, ad.observed_at))
            if not existed:
                new_ads += 1
                c.execute(
                    "INSERT INTO ad_presence (ad_id, host, side, appeared_at)"
                    " VALUES (?,?,?,?)", (ad.ad_id, ad.host, ad.side, ad.observed_at))

            # ВНИМАНИЕ: набор полей обязан совпадать с Ad.state_fingerprint().
            # Рассинхрон отключает дедупликацию молча — БД начинает расти
            # линейно по времени вместо роста по событиям.
            prev = c.execute(
                "SELECT price, quantity, frozen_qty, min_amount, max_amount,"
                " version, status, finish_num, order_num, last_qty, executed_qty"
                " FROM ad_state WHERE ad_id=? ORDER BY observed_at DESC LIMIT 1",
                (ad.ad_id,)).fetchone()
            fp = ad.state_fingerprint()
            if prev is None or tuple(prev) != fp:
                c.execute(
                    "INSERT OR IGNORE INTO ad_state (ad_id, observed_at, price,"
                    " quantity, frozen_qty, min_amount, max_amount, version,"
                    " status, finish_num, order_num, last_qty, executed_qty)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ad.ad_id, ad.observed_at, *fp))
                new_states += 1

        return new_states, new_ads

    def close_absent(self, host: str, side: str, live_ids: set[str], at: float) -> int:
        """Закрыть интервалы присутствия для объявлений, пропавших из книги.

        ВЫЗЫВАТЬ ТОЛЬКО ПОСЛЕ ПОЛНОГО УСПЕШНОГО ОБХОДА. Иначе один сетевой
        таймаут объявит половину книги умершей и испортит ровно ту метрику,
        ради которой всё затевалось.
        """
        open_rows = self.conn.execute(
            "SELECT id, ad_id FROM ad_presence WHERE host=? AND side=?"
            " AND disappeared_at IS NULL", (host, side)).fetchall()
        gone = [r["id"] for r in open_rows if r["ad_id"] not in live_ids]
        if gone:
            self.conn.executemany(
                "UPDATE ad_presence SET disappeared_at=? WHERE id=?",
                [(at, i) for i in gone])
        return len(gone)

    def reopen_after_gap(self, host: str, side: str, live_ids: set[str],
                         at: float) -> int:
        """Если коллектор спал, а объявление всё ещё в книге — открыть новый
        интервал присутствия, а не притворяться, что мы наблюдали всё время."""
        cur_open = {r["ad_id"] for r in self.conn.execute(
            "SELECT ad_id FROM ad_presence WHERE host=? AND side=?"
            " AND disappeared_at IS NULL", (host, side)).fetchall()}
        missing = live_ids - cur_open
        if missing:
            self.conn.executemany(
                "INSERT INTO ad_presence (ad_id, host, side, appeared_at)"
                " VALUES (?,?,?,?)", [(i, host, side, at) for i in missing])
        return len(missing)

    def save_raw(self, captured_at: float, host: str, side: str,
                 page: int, body_gz: bytes) -> None:
        self.conn.execute(
            "INSERT INTO raw_response (captured_at, host, side, page, body_gz)"
            " VALUES (?,?,?,?,?)", (captured_at, host, side, page, body_gz))

    def save_payment_methods(self, methods: dict[str, str]) -> None:
        now = time.time()
        self.conn.executemany(
            "INSERT INTO payment_method (id, name, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " updated_at=excluded.updated_at",
            [(k, v, now) for k, v in methods.items()])

    def prune_raw(self, older_than_days: int) -> int:
        cutoff = time.time() - older_than_days * 86400
        cur = self.conn.execute("DELETE FROM raw_response WHERE captured_at < ?",
                                (cutoff,))
        return cur.rowcount

    def commit(self) -> None:
        self.conn.commit()
