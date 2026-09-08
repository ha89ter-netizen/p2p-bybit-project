"""Источники цен для инвестиционного слоя.

Здесь НЕТ ни одной выдуманной цены. Проект собирает книгу P2P и больше
ничего: котировок акций и облигаций у него нет и взяться им неоткуда.
Поэтому вместо правдоподобных чисел тут абстракция и честное «нет данных».

Два разных вопроса, которые нельзя смешивать:

* **Цена актива.** Сколько стоит доля широкого рынка акций или облигаций.
  Источника нет. `NullPriceProvider` возвращает None, и весь слой выше
  обязан это пережить, показав «недостаточно данных», а не ноль.

* **Курс KZT→USD.** Нужен, чтобы вообще сопоставить тенговый капитал с
  долларовыми активами. Здесь источник ЕСТЬ, но он особенный: середина
  собственной книги P2P USDT/KZT. Это розничный P2P-курс, а не
  официальный курс Нацбанка и не межбанк. Он систематически отличается,
  и подписан соответственно.
"""

from __future__ import annotations

import sqlite3
import statistics
import time
from dataclasses import dataclass
from decimal import Decimal


class MarketPriceProvider:
    """Интерфейс поставщика цен активов.

    Реализация появится, когда появится источник. До тех пор наследники
    обязаны возвращать None, а не выдумывать значение.
    """

    name = "abstract"

    def price(self, symbol: str, at: float | None = None) -> Decimal | None:
        raise NotImplementedError

    def available(self) -> bool:
        return False


class NullPriceProvider(MarketPriceProvider):
    """Источника цен нет. Единственный честный ответ — None."""

    name = "none"

    def price(self, symbol: str, at: float | None = None) -> Decimal | None:
        return None

    def available(self) -> bool:
        return False


@dataclass(frozen=True)
class FxQuote:
    rate_kzt_per_usd: Decimal
    source: str
    at: float
    note: str
    sample: int


class FxProvider:
    """Курс KZT за 1 USD.

    Источник — середина между лучшим бидом и лучшим аском USDT/KZT в
    собственной книге. USDT привязан к доллару, поэтому как ПРОКСИ он
    годится; официальным курсом он не является и совпадать с ним не обязан.
    """

    def __init__(self, db_path: str, max_age_sec: float = 900.0):
        self.db_path = db_path
        self.max_age_sec = max_age_sec

    def quote(self, at: float | None = None) -> FxQuote | None:
        at = at if at is not None else time.time()
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT a.side, CAST(s.price AS REAL), s.observed_at"
                " FROM ad_state s JOIN ad a ON a.ad_id = s.ad_id"
                " WHERE a.host='api2.bybit.com' AND s.observed_at BETWEEN ? AND ?",
                (at - self.max_age_sec, at)).fetchall()
        finally:
            conn.close()
        buys = [p for side, p, _ in rows if side == "1"]
        sells = [p for side, p, _ in rows if side == "0"]
        if not buys or not sells:
            return None
        # Лучший аск (дешевле всего купить) и лучший бид (дороже всего продать).
        mid = (min(buys) + max(sells)) / 2
        return FxQuote(
            rate_kzt_per_usd=Decimal(str(round(mid, 4))),
            source="p2p_usdt_kzt_mid",
            at=max(t for _, _, t in rows),
            note="середина книги P2P USDT/KZT; розничный курс, не официальный",
            sample=len(rows),
        )


def kzt_to_usd(amount_kzt: Decimal, fx: FxQuote | None) -> Decimal | None:
    """Перевести тенге в доллары. Без курса — None, а не ноль."""
    if fx is None or fx.rate_kzt_per_usd <= 0:
        return None
    return amount_kzt / fx.rate_kzt_per_usd
