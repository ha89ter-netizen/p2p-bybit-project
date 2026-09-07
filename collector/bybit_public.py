"""
Адаптер к публичному P2P-эндпоинту Bybit.

    POST https://{host}/fiat/otc/item/online

ЭНДПОИНТ НЕ ДОКУМЕНТИРОВАН. Авторизация не требуется (проверено 28.08.2026).
Официальный /v5/p2p/* требует статуса General Advertiser и нам недоступен.

Весь проект трогает биржу только отсюда. Если формат изменится — правится
один файл, а сырые ответы в raw_response позволяют переразобрать историю.

Книга забирается ЦЕЛИКОМ (без серверного фильтра amount). Фильтрация по
сумме делается офлайн: так один сбор отвечает на вопросы про 200k, 300k и
400k сразу, и мы не теряем объявления, которые попадут в диапазон завтра.
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from config.settings import COLLECTOR

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


class FetchError(RuntimeError):
    pass


# HTTP-коды, означающие «нас притормаживают». Обычный бэкофф в полторы
# секунды здесь только усугубляет: за 72 часа непрерывного сбора это
# единственный реалистичный способ получить блокировку по IP.
THROTTLE_STATUSES = frozenset({403, 418, 429, 503})
THROTTLE_BACKOFF_SEC = 60.0


@dataclass
class Page:
    host: str
    side: str
    page: int
    items: list[dict]
    total_count: int
    raw: bytes
    latency_ms: int


class BybitPublicClient:
    def __init__(self, cfg=COLLECTOR):
        self.cfg = cfg
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        gap = self.cfg.min_gap_between_requests_sec - (time.time() - self._last_request_at)
        if gap > 0:
            time.sleep(gap)
        self._last_request_at = time.time()

    def _post(self, host: str, path: str, payload: dict) -> tuple[dict, bytes, int]:
        body = json.dumps(payload).encode()
        last_exc: Exception | None = None

        for attempt in range(self.cfg.max_retries):
            self._throttle()
            req = urllib.request.Request(
                f"https://{host}{path}", data=body,
                headers={"Content-Type": "application/json", "User-Agent": _UA},
            )
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.http_timeout_sec) as r:
                    raw = r.read()
                latency_ms = int((time.time() - t0) * 1000)
                parsed = json.loads(raw)
                if parsed.get("ret_code") != 0:
                    raise FetchError(f"ret_code={parsed.get('ret_code')} "
                                     f"ret_msg={parsed.get('ret_msg')!r}")
                return parsed, raw, latency_ms
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries - 1:
                    time.sleep(THROTTLE_BACKOFF_SEC * (attempt + 1)
                               if exc.code in THROTTLE_STATUSES
                               else self.cfg.backoff_base_sec ** (attempt + 1))
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError, FetchError) as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries - 1:
                    time.sleep(self.cfg.backoff_base_sec ** (attempt + 1))

        raise FetchError(f"{host}{path} failed after "
                         f"{self.cfg.max_retries} attempts: {last_exc}") from last_exc

    def fetch_page(self, host: str, side: str, page: int) -> Page:
        payload = {
            "userId": "", "tokenId": self.cfg.token, "currencyId": self.cfg.currency,
            "payment": [], "side": side, "size": str(self.cfg.page_size),
            "page": str(page), "amount": "", "authMaker": False, "canTrade": False,
        }
        parsed, raw, latency_ms = self._post(host, "/fiat/otc/item/online", payload)
        result = parsed.get("result") or {}
        return Page(host=host, side=side, page=page,
                    items=result.get("items") or [],
                    total_count=int(result.get("count") or 0),
                    raw=raw, latency_ms=latency_ms)

    def fetch_book(self, host: str, side: str) -> tuple[list[Page], bool]:
        """Все страницы одной стороны.

        Возвращает (страницы, complete). complete=False означает, что обход
        прервался — такой опрос НЕЛЬЗЯ использовать для вывода об исчезновении
        объявлений, иначе половина книги будет объявлена мёртвой из-за
        одного таймаута. Это и есть главная защита lifetime-статистики.
        """
        pages: list[Page] = []
        for p in range(1, self.cfg.max_pages + 1):
            try:
                page = self.fetch_page(host, side, p)
            except FetchError:
                return pages, False
            pages.append(page)
            if len(page.items) < self.cfg.page_size:
                return pages, True
        # Упёрлись в max_pages: книга больше, чем мы способны прочитать.
        # complete=False — иначе хвост книги будет объявлен исчезнувшим.
        return pages, False

    def fetch_payment_dictionary(self, host: str) -> dict[str, str]:
        parsed, _, _ = self._post(host, "/fiat/otc/configuration/queryAllPaymentList", {})
        result = parsed.get("result") or {}
        return {str(x["paymentType"]): (x.get("paymentName") or "")
                for x in (result.get("paymentConfigVo") or [])}


def gz(raw: bytes) -> bytes:
    return gzip.compress(raw, compresslevel=6)


def gunz(blob: bytes) -> bytes:
    return gzip.decompress(blob)
