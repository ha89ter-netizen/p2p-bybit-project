"""Денежный буфер и покупка пачками.

Взнос от одного цикла P2P — около 1 952 ₸, то есть 4.23 доллара. Купить
на такую сумму нельзя: при комиссии в три доллара брокеру уходит 71%
взноса. Считать, что каждый взнос немедленно превращается в актив, значит
моделировать операцию, которую невозможно совершить.

Поэтому событие распределения и покупка актива здесь — **две разные
операции**. Взносы копятся на денежном остатке, покупка происходит, когда
накопленного хватает, чтобы комиссия стала пренебрежимой.

Порог считается от комиссии, а не назначается: при доле издержек не выше
`max_fee_share` покупка на сумму `commission / max_fee_share` окупает
себя. При комиссии 1 USD и пороге 1% это 100 USD, то есть покупка раз в
двое суток при текущем притоке.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

_TENGE = Decimal("0.01")


@dataclass(frozen=True)
class PurchasePolicy:
    """Правила превращения накопленного в покупку."""

    #: минимальная комиссия брокера за сделку, USD
    commission_usd: Decimal = Decimal("1.00")

    #: какую долю покупки комиссии позволено съесть
    max_fee_share: Decimal = Decimal("0.01")

    #: спред на конвертации ₸→USD; часто дороже самой комиссии
    fx_spread_pct: Decimal = Decimal("0.5")

    def min_purchase_usd(self) -> Decimal:
        if self.max_fee_share <= 0:
            return Decimal(0)
        return self.commission_usd / self.max_fee_share

    def min_purchase_kzt(self, rate_kzt_per_usd: Decimal) -> Decimal:
        return self.min_purchase_usd() * rate_kzt_per_usd

    def fee_kzt(self, rate_kzt_per_usd: Decimal) -> Decimal:
        return self.commission_usd * rate_kzt_per_usd


@dataclass
class Purchase:
    """Одна фактическая покупка."""
    at: float
    asset: str
    gross_kzt: Decimal          # снято с буфера
    commission_kzt: Decimal
    fx_cost_kzt: Decimal
    invested_kzt: Decimal       # дошло до актива
    invested_usd: Decimal | None
    units: Decimal | None       # None — цены нет, единицы неизвестны


@dataclass
class CashBuffer:
    """Накопитель между взносом и покупкой.

    Хранит тенге. Покупка совершается только когда накопленного хватает на
    осмысленный лот; до этого деньги лежат и ждут — как и в жизни.
    """
    policy: PurchasePolicy = field(default_factory=PurchasePolicy)
    balance_kzt: Decimal = Decimal(0)
    purchases: list[Purchase] = field(default_factory=list)
    commission_kzt: Decimal = Decimal(0)
    fx_cost_kzt: Decimal = Decimal(0)
    contributions: int = 0

    def add(self, amount_kzt: Decimal) -> None:
        if amount_kzt <= 0:
            return
        self.balance_kzt += amount_kzt
        self.contributions += 1

    def ready(self, rate_kzt_per_usd: Decimal | None) -> bool:
        if rate_kzt_per_usd is None or rate_kzt_per_usd <= 0:
            return False        # без курса покупать нечем и не во что
        return self.balance_kzt >= self.policy.min_purchase_kzt(rate_kzt_per_usd)

    def buy(self, at: float, asset: str, rate_kzt_per_usd: Decimal | None,
            price_kzt: Decimal | None = None) -> Purchase | None:
        """Потратить накопленное. None — ещё рано или нет курса."""
        if not self.ready(rate_kzt_per_usd):
            return None
        gross = self.balance_kzt
        fee = self.policy.fee_kzt(rate_kzt_per_usd)
        fx = (gross - fee) * self.policy.fx_spread_pct / 100
        invested = (gross - fee - fx).quantize(_TENGE, rounding=ROUND_DOWN)
        if invested <= 0:
            return None

        units = None
        if price_kzt and price_kzt > 0:
            units = invested / price_kzt

        p = Purchase(
            at=at, asset=asset, gross_kzt=gross,
            commission_kzt=fee, fx_cost_kzt=fx,
            invested_kzt=invested,
            invested_usd=invested / rate_kzt_per_usd,
            units=units,
        )
        self.balance_kzt = Decimal(0)
        self.commission_kzt += fee
        self.fx_cost_kzt += fx
        self.purchases.append(p)
        return p

    # ---- отчётность ----

    @property
    def invested_kzt(self) -> Decimal:
        return sum((p.invested_kzt for p in self.purchases), Decimal(0))

    @property
    def total_costs_kzt(self) -> Decimal:
        return self.commission_kzt + self.fx_cost_kzt

    def cost_share_pct(self) -> Decimal | None:
        """Какую долю притока съели издержки. Главное число этого слоя."""
        moved = self.invested_kzt + self.total_costs_kzt
        if moved <= 0:
            return None
        return self.total_costs_kzt / moved * 100
