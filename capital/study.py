"""Исследование доли распределения прибыли.

Вопрос: что происходит с капиталом, если систематически уводить часть
симулированной прибыли P2P в долгосрочный бумажный портфель?

Все доли моделируются на ОДНОЙ И ТОЙ ЖЕ истории сделок, поэтому различия
между ними идут только от правила распределения.

Чего этот модуль намеренно НЕ делает.

Он не объявляет 100% лучшей долей. Уведённые деньги перестают работать в
обороте P2P, а вернутся ли они с доходностью — зависит от цен активов,
которых у проекта нет. Пока источника цен нет, сравнение честно
показывает только одно: сколько ликвидности каждая доля забирает из
оборота. Это половина ответа, и она подписана как половина.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from capital.allocation import (ALLOCATION_RATES, AllocationPolicy, allocate,
                                summarize)
from capital.portfolio import PORTFOLIO_MODELS, build_portfolios
from capital.prices import MarketPriceProvider, NullPriceProvider


@dataclass
class StrategyOutcome:
    rate_pct: Decimal
    label: str
    trades: int
    net_pnl_kzt: Decimal
    allocated_kzt: Decimal            # ушло в портфель
    retained_kzt: Decimal             # осталось в обороте P2P
    p2p_capital_kzt: Decimal          # оборотный капитал на конец
    portfolio_value_kzt: Decimal | None   # None = нет цен
    combined_kzt: Decimal | None
    liquidity_kzt: Decimal            # доступно для следующих кругов P2P
    portfolios: dict                  # оценка по каждой модели весов
    priced: bool

    def to_dict(self) -> dict:
        return {
            "rate_pct": float(self.rate_pct), "label": self.label,
            "trades": self.trades,
            "net_pnl_kzt": float(self.net_pnl_kzt),
            "allocated_kzt": float(self.allocated_kzt),
            "retained_kzt": float(self.retained_kzt),
            "p2p_capital_kzt": float(self.p2p_capital_kzt),
            "portfolio_value_kzt": (None if self.portfolio_value_kzt is None
                                    else float(self.portfolio_value_kzt)),
            "combined_kzt": (None if self.combined_kzt is None
                             else float(self.combined_kzt)),
            "liquidity_kzt": float(self.liquidity_kzt),
            "priced": self.priced,
            "portfolios": self.portfolios,
        }


def run_study(paper_result, start_capital_kzt: Decimal,
              rates=ALLOCATION_RATES,
              prices: MarketPriceProvider | None = None,
              model_name: str = "balanced") -> dict:
    """Прогнать все доли распределения по одному результату симуляции."""
    prices = prices or NullPriceProvider()
    trades = paper_result.trades
    net_total = sum((t.pnl_kzt for t in trades), Decimal(0))

    out: list[StrategyOutcome] = []
    for rate in rates:
        policy = AllocationPolicy(rate_pct=rate)
        events = allocate(trades, policy)
        s = summarize(events, policy, total_net_pnl_kzt=net_total)

        ports = build_portfolios(events, models=PORTFOLIO_MODELS, prices=prices)
        vals = {n: p.valuation(prices) for n, p in ports.items()}

        chosen = vals.get(model_name) or next(iter(vals.values()))
        pv = chosen["market_value_kzt"]

        # Оборотный капитал P2P: старт плюс ВСЯ прибыль минус уведённое.
        # Убытки остаются в P2P целиком — портфель их не покрывает.
        p2p_cap = start_capital_kzt + net_total - s.allocated_kzt

        out.append(StrategyOutcome(
            rate_pct=rate, label=policy.label, trades=len(trades),
            net_pnl_kzt=net_total,
            allocated_kzt=s.allocated_kzt, retained_kzt=s.retained_kzt,
            p2p_capital_kzt=p2p_cap,
            portfolio_value_kzt=pv,
            combined_kzt=None if pv is None else p2p_cap + pv,
            liquidity_kzt=p2p_cap,
            portfolios={n: _pack(v) for n, v in vals.items()},
            priced=bool(chosen["priced"]),
        ))

    return {
        "start_capital_kzt": float(start_capital_kzt),
        "net_pnl_kzt": float(net_total),
        "trades": len(trades),
        "price_source": prices.name,
        "priced": prices.available(),
        "valuation_model": model_name,
        "strategies": [o.to_dict() for o in out],
        "caveat": (
            "Без источника цен активов стоимость портфеля неизвестна, "
            "поэтому сравнение долей показывает только изъятие ликвидности "
            "из оборота P2P, но не итоговую доходность."
        ) if not prices.available() else "",
    }


def _pack(v: dict) -> dict:
    def f(x):
        return None if x is None else float(x)
    return {
        "model": v["model"],
        "weights": {k: float(x) for k, x in v["weights"].items()},
        "contributions": v["contributions"],
        "contributed_kzt": f(v["contributed_kzt"]),
        "market_value_kzt": f(v["market_value_kzt"]),
        "unrealized_pnl_kzt": f(v["unrealized_pnl_kzt"]),
        "total_return_pct": f(v["total_return_pct"]),
        "priced": v["priced"],
        "price_source": v["price_source"],
        "by_asset": {k: {"contributed_kzt": f(a["contributed_kzt"]),
                         "market_value_kzt": f(a["market_value_kzt"]),
                         "weight_pct": f(a["weight_pct"])}
                     for k, a in v["by_asset"].items()},
    }
