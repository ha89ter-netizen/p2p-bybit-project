"""Бумажный инвестиционный портфель.

Ни одного брокерского поручения. Взносы приходят из симулированной
прибыли P2P и раскладываются по классам активов согласно весам модели.

Про честность оценки — главное в этом модуле.

Взнос известен точно: это тенге, посчитанные из симуляции. А вот
СТОИМОСТЬ вложенного зависит от цен акций и облигаций, которых у проекта
нет. Поэтому портфель раздельно хранит:

* `contributed` — сколько внесено. Известно точно, в тенге.
* `market_value` — сколько это стоит сейчас. Без источника цен — None.

Пока источника цен нет, модели различаются РАСКЛАДКОЙ взносов, но не
доходностью: сравнивать «агрессивную» и «консервативную» по результату
невозможно, и делать вид, что возможно, значит рисовать цифры. Как
только появится `MarketPriceProvider`, оценка включится без переделки.

Денежная часть — исключение: наличные не требуют котировки, их значение
известно и равно внесённому.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from capital.prices import MarketPriceProvider, NullPriceProvider

EQUITIES = "equities"
BONDS = "bonds"
CASH = "cash"
ASSET_CLASSES = (EQUITIES, BONDS, CASH)

# Инструменты-ориентиры. Символы заданы, чтобы слой цен знал, что
# запрашивать; собственных котировок в проекте по-прежнему нет.
PROXY_SYMBOLS = {
    EQUITIES: "BROAD_US_EQUITY",      # широкий рынок акций США
    BONDS: "BROAD_GOV_BOND",          # широкие государственные облигации
}

_TENGE = Decimal("0.01")


@dataclass(frozen=True)
class PortfolioModel:
    """Набор весов. Это исследовательская пресетка, а не рекомендация."""
    name: str
    weights: dict[str, Decimal]

    def __post_init__(self):
        total = sum(self.weights.values())
        if total != Decimal(100):
            raise ValueError(f"веса модели {self.name} дают {total}, а не 100")
        unknown = set(self.weights) - set(ASSET_CLASSES)
        if unknown:
            raise ValueError(f"неизвестные классы активов: {sorted(unknown)}")


PORTFOLIO_MODELS: tuple[PortfolioModel, ...] = (
    PortfolioModel("aggressive", {EQUITIES: Decimal(90), BONDS: Decimal(10), CASH: Decimal(0)}),
    PortfolioModel("growth", {EQUITIES: Decimal(70), BONDS: Decimal(20), CASH: Decimal(10)}),
    PortfolioModel("balanced", {EQUITIES: Decimal(60), BONDS: Decimal(30), CASH: Decimal(10)}),
    PortfolioModel("conservative", {EQUITIES: Decimal(30), BONDS: Decimal(50), CASH: Decimal(20)}),
)
MODELS_BY_NAME = {m.name: m for m in PORTFOLIO_MODELS}


@dataclass
class Sleeve:
    """Один класс активов внутри портфеля."""
    asset: str
    contributed_kzt: Decimal = Decimal(0)
    units: Decimal | None = None          # None = цена неизвестна, единиц не считаем

    def market_value_kzt(self, price_kzt: Decimal | None) -> Decimal | None:
        if self.asset == CASH:
            return self.contributed_kzt          # наличные не требуют котировки
        if price_kzt is None or self.units is None:
            return None
        return self.units * price_kzt


@dataclass
class PaperPortfolio:
    """Бумажный портфель по одной модели."""
    model: PortfolioModel
    sleeves: dict[str, Sleeve] = field(default_factory=dict)
    contributions: int = 0
    contributed_kzt: Decimal = Decimal(0)
    started_at: float | None = None
    last_at: float | None = None
    applied: set[str] = field(default_factory=set)

    def __post_init__(self):
        for a in ASSET_CLASSES:
            self.sleeves.setdefault(a, Sleeve(asset=a))

    def contribute(self, event, prices: MarketPriceProvider | None = None) -> bool:
        """Принять событие распределения. False — уже применялось.

        Повторное применение того же события молча удвоило бы портфель,
        поэтому идентификаторы запоминаются.
        """
        if event.event_id in self.applied:
            return False
        if event.allocated_kzt <= 0:
            return False
        self.applied.add(event.event_id)

        for asset, weight in self.model.weights.items():
            if weight <= 0:
                continue
            part = (event.allocated_kzt * weight / 100).quantize(
                _TENGE, rounding=ROUND_DOWN)
            sl = self.sleeves[asset]
            sl.contributed_kzt += part
            if asset != CASH and prices is not None and prices.available():
                px = prices.price(PROXY_SYMBOLS[asset], event.at)
                if px:
                    sl.units = (sl.units or Decimal(0)) + part / px

        self.contributions += 1
        self.contributed_kzt += event.allocated_kzt
        self.started_at = event.at if self.started_at is None else min(self.started_at, event.at)
        self.last_at = event.at if self.last_at is None else max(self.last_at, event.at)
        return True

    # ---- оценка ----

    def valuation(self, prices: MarketPriceProvider | None = None) -> dict:
        prices = prices or NullPriceProvider()
        by_asset: dict[str, dict] = {}
        priced = True
        total: Decimal | None = Decimal(0)

        for asset in ASSET_CLASSES:
            sl = self.sleeves[asset]
            px = (prices.price(PROXY_SYMBOLS[asset]) if asset != CASH
                  and prices.available() else None)
            mv = sl.market_value_kzt(px)
            if mv is None and sl.contributed_kzt > 0:
                priced = False
            by_asset[asset] = {
                "contributed_kzt": sl.contributed_kzt,
                "market_value_kzt": mv,
                "weight_pct": self.model.weights.get(asset, Decimal(0)),
            }
            if total is not None and mv is not None:
                total += mv

        if not priced:
            total = None

        return {
            "model": self.model.name,
            "weights": dict(self.model.weights),
            "contributions": self.contributions,
            "contributed_kzt": self.contributed_kzt,
            "by_asset": by_asset,
            "market_value_kzt": total,
            "unrealized_pnl_kzt": (None if total is None
                                   else total - self.contributed_kzt),
            "total_return_pct": (None if total is None or self.contributed_kzt <= 0
                                 else (total / self.contributed_kzt - 1) * 100),
            "priced": priced and prices.available(),
            "price_source": prices.name,
            "started_at": self.started_at,
            "last_at": self.last_at,
        }


def build_portfolios(events, models=PORTFOLIO_MODELS,
                     prices: MarketPriceProvider | None = None
                     ) -> dict[str, PaperPortfolio]:
    """Одни и те же события во все модели.

    Взносы обязаны быть одинаковыми, иначе модели несравнимы: разница
    между ними должна идти только от весов, а не от разного притока.
    """
    out = {}
    for m in models:
        p = PaperPortfolio(model=m)
        for ev in events:
            p.contribute(ev, prices=prices)
        out[m.name] = p
    return out
