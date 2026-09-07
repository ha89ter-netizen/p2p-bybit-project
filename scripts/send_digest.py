"""
Отправить дайджест в Telegram один раз.

    python3 -m scripts.send_digest            # отправить
    python3 -m scripts.send_digest --dry-run  # только показать в терминале

Удобно для проверки токена и для запуска по cron, если не хочется
держать отправку внутри коллектора.
"""

from __future__ import annotations

import sys

from analysis import digest as digest_mod
from config.settings import COLLECTOR, TELEGRAM
from notify.telegram import TelegramError, render, send
from storage.db import Store

GLOBAL = "api2.bybit.com"


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    store = Store(COLLECTOR.db_path)
    try:
        d = digest_mod.build(store, GLOBAL,
                             window_sec=TELEGRAM.digest_hours * 3600)
        text = render(d)
    finally:
        store.close()

    if dry:
        print(text)
        return 0

    if not TELEGRAM.enabled:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы.\n"
              "  export TELEGRAM_BOT_TOKEN=...\n"
              "  export TELEGRAM_CHAT_ID=...\n"
              "Проверить без отправки: --dry-run", file=sys.stderr)
        return 2
    try:
        send(text)
    except TelegramError as exc:
        print(f"не отправлено: {exc}", file=sys.stderr)
        return 1
    print("отправлено")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
