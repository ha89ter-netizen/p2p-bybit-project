"""
Коллектор. Единственное, что должно работать непрерывно.

Запуск:  python3 -m collector.poller
Стоп:    Ctrl+C  (корректно закрывает БД, ничего не теряет)

Работает на ноутбуке. Разрывы сети и сон машины не портят данные: каждый
обход пишется в poll_run, а интервалы присутствия закрываются ТОЛЬКО после
полного успешного обхода. Пропущенное время видно в данных как дыра, а не
как ложное "объявление умерло".
"""

from __future__ import annotations

import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from collector.bybit_public import BybitPublicClient, FetchError, gz
from config.settings import COLLECTOR, TELEGRAM
from domain.models import Ad
from notify.schedule import SentLog, last_slot, local_now, slot_key
from storage.db import Store

_stop = False


def _handle_signal(signum, frame):
    # Только флаг: print() из обработчика сигнала падает с
    # RuntimeError: reentrant call inside BufferedWriter, если сигнал
    # пришёл посреди другого print. Сообщение печатает основной цикл.
    global _stop
    _stop = True


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}Z  {msg}", flush=True)


def poll_once(client: BybitPublicClient, store: Store, cycle: int,
              cfg=COLLECTOR) -> None:
    capture_raw = (cycle % cfg.raw_capture_every_n_cycles == 0)

    for host in cfg.hosts:
        for side in cfg.sides:
            started = time.time()
            error: str | None = None
            pages, complete = [], False
            try:
                pages, complete = client.fetch_book(host, side)
                if not complete:
                    error = ("page_cap_reached" if len(pages) >= cfg.max_pages
                             else "incomplete_pagination")
            except FetchError as exc:
                error = str(exc)[:500]
            except Exception as exc:                      # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"[:500]

            observed_at = started
            ads = [Ad.from_api(item, host, observed_at)
                   for page in pages for item in page.items]

            # close_absent() ищет открытые интервалы по ЗАПРОШЕННОЙ стороне,
            # а ad_presence пишется по стороне из ответа. Если они разойдутся,
            # объявления чужой стороны будут ложно объявлены исчезнувшими.
            mismatched = [a for a in ads if a.side != side]
            if mismatched:
                log(f"ВНИМАНИЕ: {len(mismatched)} объявлений со стороной "
                    f"!= запрошенной ({side}), исключены из обхода")
                ads = [a for a in ads if a.side == side]

            live_ids = {a.ad_id for a in ads}

            # Запись в БД тоже может упасть. Обход всё равно обязан попасть
            # в poll_run, иначе в данных появится тихая дыра, неотличимая
            # от «коллектор не работал», и статистика времени жизни соврёт.
            n_states = n_new = gone = reopened = 0
            try:
                n_states, n_new = store.upsert_ads(ads)
                if complete:
                    reopened = store.reopen_after_gap(host, side, live_ids,
                                                      observed_at)
                    gone = store.close_absent(host, side, live_ids, observed_at)
                if capture_raw:
                    for page in pages:
                        store.save_raw(observed_at, host, side, page.page,
                                       gz(page.raw))
            except Exception as exc:                      # noqa: BLE001
                complete = False
                error = f"store_failed: {type(exc).__name__}: {exc}"[:500]

            store.record_poll(
                started_at=started, finished_at=time.time(), host=host, side=side,
                complete=complete, n_items=len(ads), n_pages=len(pages),
                total_count=pages[0].total_count if pages else 0,
                latency_ms=sum(p.latency_ms for p in pages), error=error)
            store.commit()

            status = "ok" if complete else f"INCOMPLETE ({error})"
            log(f"{host} side={side} ads={len(ads):>3} new={n_new:>2} "
                f"Δstate={n_states:>3} gone={gone:>2} back={reopened:>2} {status}")


def run_periodic(store: Store) -> None:
    """Периодическая работа: отчёт на диск и уведомление.

    Ни один сбой здесь не имеет права остановить сбор — данные
    невосполнимы, а отчёт и уведомление воспроизводимы в любой момент.
    Поэтому каждая часть обёрнута отдельно: упавший Telegram не должен
    лишать нас FINDINGS.md, и наоборот.
    """
    # Снимок ПЕРВЫМ делом: данные невосполнимы, отчёты — нет.
    try:
        from scripts.backup import snapshot
        from config.settings import COLLECTOR as _C
        from scripts.backup import missing_days
        out = snapshot(_C.db_path)
        log(f"бэкап: {out.name}" if out
            else "бэкап: снимок за сегодня уже есть")
        gaps = missing_days(_C.db_path)
        if gaps:
            # Молчаливый пропуск дня — то же, что отсутствие копии.
            log(f"БЭКАП: ПРОПУЩЕНЫ ДНИ {', '.join(gaps)}")
    except Exception as exc:                          # noqa: BLE001
        log(f"БЭКАП НЕ СДЕЛАН: {exc}")

    try:
        from scripts.export_findings import main as export_findings
        export_findings()
    except Exception as exc:                          # noqa: BLE001
        log(f"FINDINGS: не записан ({exc})")

    if not TELEGRAM.enabled:
        return
    try:
        send_digest(store)
        log("Telegram: дайджест отправлен")
    except Exception as exc:                          # noqa: BLE001
        log(f"Telegram: не отправлено ({exc})")


def send_digest(store: Store) -> None:
    from analysis.digest import build
    from notify.telegram import render, send
    d = build(store, "api2.bybit.com", window_sec=TELEGRAM.digest_hours * 3600)
    send(render(d))


def main() -> int:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    cfg = COLLECTOR
    store = Store(cfg.db_path)
    client = BybitPublicClient(cfg)

    try:
        methods = client.fetch_payment_dictionary(cfg.hosts[0])
        store.save_payment_methods(methods)
        store.commit()
        log(f"справочник методов оплаты обновлён: {len(methods)} записей")
    except Exception as exc:                              # noqa: BLE001
        log(f"справочник методов оплаты недоступен: {exc}")

    log(f"старт: {cfg.token}/{cfg.currency}, хосты={cfg.hosts}, "
        f"интервал={cfg.poll_interval_sec}с, БД={cfg.db_path}")

    hh = ", ".join(f"{h:02d}:00" for h in TELEGRAM.digest_at_hours)
    if TELEGRAM.enabled:
        log(f"Telegram: отчёт в {hh} ({TELEGRAM.timezone}), "
            f"окно {TELEGRAM.digest_hours}ч")
    else:
        log("Telegram: выключен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")

    sent_log = SentLog(Path(cfg.db_path).parent / "last_digest.txt")
    # При первом запуске текущий слот считается уже отправленным: иначе
    # старт коллектора в 21:00 немедленно вышлет отчёт за слот 20:00.
    if not sent_log.path.exists():
        sent_log.mark(slot_key(last_slot(local_now(TELEGRAM.timezone),
                                         TELEGRAM.digest_at_hours)))

    cycle = 0
    try:
        while not _stop:
            t0 = time.time()
            try:
                poll_once(client, store, cycle, cfg)
            except Exception as exc:                      # noqa: BLE001
                log(f"ЦИКЛ УПАЛ, продолжаю: {type(exc).__name__}: {exc}")
            cycle += 1
            if _stop:
                log("получен сигнал остановки, завершаю")

            # Дайджест не должен ронять сбор: сеть Telegram может лежать,
            # токен протухнуть, чат — исчезнуть. Данные важнее уведомления.
            slot = last_slot(local_now(TELEGRAM.timezone),
                             TELEGRAM.digest_at_hours)
            key = slot_key(slot)
            if sent_log.is_due(key):
                run_periodic(store)
                sent_log.mark(key)

            if cycle % 240 == 0:
                removed = store.prune_raw(cfg.raw_retention_days)
                if removed:
                    log(f"retention: удалено сырых ответов: {removed}")
                store.commit()

            elapsed = time.time() - t0
            sleep_for = max(0.0, cfg.poll_interval_sec - elapsed)
            # дробим сон, чтобы Ctrl+C отвечал сразу
            while sleep_for > 0 and not _stop:
                chunk = min(0.5, sleep_for)
                time.sleep(chunk)
                sleep_for -= chunk
    finally:
        store.close()
        log(f"остановлен корректно, циклов выполнено: {cycle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
