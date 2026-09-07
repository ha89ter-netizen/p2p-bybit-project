"""
Отправка дайджеста в Telegram.

Только исходящие сообщения. Ни команд, ни обработчиков, ни webhook —
принимать что-либо от пользователя эта система не умеет по построению.
Тем самым исключён и класс ошибок «команда случайно инициировала сделку».

Токен и chat_id берутся ИСКЛЮЧИТЕЛЬНО из окружения и никогда не
логируются: в сообщениях об ошибках Telegram API URL маскируется.
"""

from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from analysis.digest import Digest, Row
from config.settings import DIGEST_BUCKETS, TELEGRAM, TelegramConfig

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None

TELEGRAM_MAX_CHARS = 4096


class TelegramError(RuntimeError):
    pass


def _tz(cfg: TelegramConfig):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(cfg.timezone)
    except Exception:                                 # noqa: BLE001
        return timezone.utc


def _hhmm(ts: float, tz) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _dur(sec: float) -> str:
    if sec < 60:
        return f"{int(sec)}с"
    if sec < 3600:
        return f"{int(sec // 60)}м"
    return f"{sec / 3600:.1f}ч"


def esc(text: object) -> str:
    """Экранировать данные, попадающие в HTML-сообщение.

    Имя бакета "<1%" Telegram разбирает как открывающий тег и отвечает
    400 Bad Request: can't parse entities. То же ждёт любой текст из
    книги — ники и remark пишут люди, и там встречается что угодно.
    """
    return html.escape(str(text), quote=False)


def render(digest: Digest, cfg: TelegramConfig = TELEGRAM) -> str:
    """Текст сообщения. Чистая функция — тестируется без сети."""
    tz = _tz(cfg)
    lines = [
        "<b>P2P USDT/KZT</b>",
        f"{_hhmm(digest.window_from, tz)}–{_hhmm(digest.window_to, tz)} · "
        f"{int(digest.amount_kzt):,} KZT".replace(",", " "),
        "",
    ]

    any_rows = False
    for name, _, _ in DIGEST_BUCKETS:
        rows = digest.buckets.get(name, [])
        total = digest.bucket_totals.get(name, 0)
        head = f"<b>{esc(name)}</b> — {total}"
        if not rows:
            lines.append(f"{head}, пусто")
            lines.append("")
            continue
        any_rows = True
        if total > len(rows):
            head += f" (показаны {len(rows)} лучших)"
        lines.append(head)
        table = ["  buy      sell    спред   жил"]
        for r in rows:
            table.append(f"{r.buy_price:>8} {r.sell_price:>8} "
                         f"{float(r.spread_pct):>6.2f}% {_dur(r.lifetime_sec):>5}")
        lines.append("<pre>" + "\n".join(table) + "</pre>")
        lines.append("")

    if not any_rows:
        lines.append("Ни одной пары, прошедшей фильтр качества.")
        lines.append("")

    p = digest.paper
    if p is not None:
        n = lambda v: f"{float(v):,.0f}".replace(",", " ")
        sn = lambda v: f"{float(v):+,.0f}".replace(",", " ")

        lines.append("<b>БАЛАНС</b>")
        lines.append("<pre>" + "\n".join([
            f"старт      {n(p.start_capital_kzt):>12} ₸",
            f"сейчас     {n(p.final_capital_kzt):>12} ₸",
            f"прибыль    {sn(p.pnl_kzt):>12} ₸",
            f"рост       {float(p.growth_pct):>11.2f}%",
            f"просадка   {float(p.max_drawdown_pct):>11.2f}%",
            f"───────────────────────",
            f"след.сделка {n(p.next_position_kzt):>11} ₸",
        ]) + "</pre>")
        lines.append("")

        lines.append("<b>Бумажная торговля</b>")
        rows = [f"сделок      {p.trades}  за {p.hours:.0f}ч",
                f"в день      {sn(p.pnl_per_day_kzt)} ₸"]
        if p.breakeven_pct is not None:
            rows.append(f"безубыток   {float(p.breakeven_pct):.0f}% успешных")
        lines.append("<pre>" + "\n".join(rows) + "</pre>")
        if p.outcomes:
            lines.append("<i>исходы: " + esc(", ".join(
                f"{k}={v}" for k, v in sorted(p.outcomes.items()))) + "</i>")
        lines.append("")

    tail = (f"моментов: {digest.moments} · отсеяно по качеству: "
            f"{digest.filtered_out}")
    if digest.losing:
        tail += f" · убыточных: {digest.losing}"
    lines.append(f"<i>{tail}</i>")
    if digest.poll_gaps:
        lines.append(f"<i>⚠ неполных обходов: {digest.poll_gaps} — "
                     f"в данных дыра</i>")
    lines.append("<i>SIMULATION ONLY · сделки не совершаются</i>")

    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[:TELEGRAM_MAX_CHARS - 20].rsplit("\n", 1)[0] + "\n<i>…</i>"
    return text


def send(text: str, cfg: TelegramConfig = TELEGRAM) -> None:
    if not cfg.enabled:
        raise TelegramError(
            "не заданы TELEGRAM_BOT_TOKEN и/или TELEGRAM_CHAT_ID")

    payload = urllib.parse.urlencode({
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()

    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    req = urllib.request.Request(url, data=payload)

    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=cfg.http_timeout_sec) as r:
                body = json.loads(r.read())
            if not body.get("ok"):
                raise TelegramError(f"Telegram отказал: "
                                    f"{body.get('description')!r}")
            return
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read()).get("description", "")
            except Exception:                         # noqa: BLE001
                pass
            # 401/400 не лечатся повтором: неверный токен или chat_id
            if exc.code in (400, 401, 403, 404):
                raise TelegramError(
                    f"Telegram HTTP {exc.code}: {detail or exc.reason}. "
                    f"Проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID "
                    f"(бот должен быть добавлен в чат и ему нужно хотя бы "
                    f"одно сообщение от вас)") from None
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as exc:
            last = exc
        if attempt < 2:
            time.sleep(2 ** attempt)

    raise TelegramError(f"не удалось отправить после 3 попыток: {last}")
