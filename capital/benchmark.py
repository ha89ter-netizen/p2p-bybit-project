"""Эталон сравнения: тенговый депозит.

Без этой строки всё сравнение портфелей бессмысленно. Ставка по тенговым
вкладам в Казахстане двузначная — следствие высокой базовой ставки. Значит
вложение в долларовые активы сравнивается **не с нулём**, а с этой ставкой.

Портфель, давший 8% годовых в долларах, может проиграть обычному депозиту
в тенге. Это не провал стратегии, а её честный результат — и узнать о нём
нужно из расчёта, а не постфактум.

Конкретная ставка здесь НЕ зашита. Она меняется вслед за решениями
Нацбанка, и подставлять число из памяти значило бы делать ровно ту
выдумку, от которой весь проект защищается. Ставка приходит параметром;
если её не задали, эталон честно сообщает, что посчитать нечем.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

SECONDS_PER_YEAR = Decimal(365 * 24 * 3600)


@dataclass
class DepositBenchmark:
    """Те же взносы, положенные на тенговый вклад.

    Проценты начисляются непрерывно по времени — так взносы, сделанные в
    разные моменты, сравниваются честно, без привязки к календарным
    периодам капитализации.
    """
    annual_rate_pct: Decimal | None = None
    contributions: list[tuple[float, Decimal]] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.annual_rate_pct is not None and self.annual_rate_pct >= 0

    def add(self, at: float, amount_kzt: Decimal) -> None:
        if amount_kzt > 0:
            self.contributions.append((at, amount_kzt))

    @property
    def deposited_kzt(self) -> Decimal:
        return sum((a for _, a in self.contributions), Decimal(0))

    def value_at(self, now: float) -> Decimal | None:
        """Сколько было бы на вкладе. None — ставка не задана."""
        if not self.known:
            return None
        r = self.annual_rate_pct / 100
        total = Decimal(0)
        for at, amount in self.contributions:
            years = Decimal(str(max(now - at, 0))) / SECONDS_PER_YEAR
            total += amount * (Decimal(1) + r * years)
        return total

    def interest_at(self, now: float) -> Decimal | None:
        v = self.value_at(now)
        return None if v is None else v - self.deposited_kzt

    def summary(self, now: float) -> dict:
        v = self.value_at(now)
        return {
            "known": self.known,
            "annual_rate_pct": (None if self.annual_rate_pct is None
                                else float(self.annual_rate_pct)),
            "contributions": len(self.contributions),
            "deposited_kzt": float(self.deposited_kzt),
            "value_kzt": None if v is None else float(v),
            "interest_kzt": (None if v is None
                             else float(v - self.deposited_kzt)),
            "note": ("ставка не задана — сравнивать не с чем"
                     if not self.known else
                     "простые проценты по времени, без капитализации"),
        }
