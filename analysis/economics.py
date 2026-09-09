"""
Экономика пары.

Здесь НЕТ единого estimated_friction. Есть раздельные члены, и те из них,
что помечены UNKNOWN, не превращаются в число молча — функция возвращает
разложение, а отчёт обязан показать, какие компоненты выдуманы.

Ключевая функция — не net_pnl, а breakeven_p_completion: вместо
"сколько мы заработаем" она отвечает "при какой вероятности успеха
это перестаёт быть убыточным". Это фальсифицируемое утверждение,
а прогноз PnL — нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config.settings import ECONOMICS, EconomicsConfig
from domain.models import Pair


@dataclass(frozen=True)
class Economics:
    amount_kzt: Decimal
    gross_pnl_kzt: Decimal
    gross_spread_pct: Decimal
    exchange_fees_kzt: Decimal
    bank_fees_kzt: Decimal
    fx_drift_kzt: Decimal
    known_net_pnl_kzt: Decimal      # только по KNOWN/ESTIMATED компонентам
    known_net_spread_pct: Decimal
    unknown_components: tuple[str, ...]   # что НЕ учтено, потому что неизвестно

    @property
    def is_trustworthy(self) -> bool:
        """False означает: число посчитано, но опираться на него нельзя."""
        return not self.unknown_components


def compute(pair: Pair, roundtrip_minutes: Decimal | None = None,
            cfg: EconomicsConfig = ECONOMICS) -> Economics:
    amount = pair.amount_kzt
    gross = pair.gross_pnl_kzt

    exchange_fees = amount * cfg.exchange_fee_taker.value * 2   # две ноги
    bank_fees = cfg.bank_transfer_fee_kzt.value * 2

    if roundtrip_minutes is None:
        roundtrip_minutes = pair.expected_roundtrip_sec / 60
    fx_drift = amount * (cfg.fx_drift_bps_per_minute.value / 10000) * roundtrip_minutes

    net = gross - exchange_fees - bank_fees - fx_drift

    unknown = tuple(
        name for name, a in (
            ("bank_transfer_fee_kzt", cfg.bank_transfer_fee_kzt),
            ("fx_drift_bps_per_minute", cfg.fx_drift_bps_per_minute),
            ("p_leg_completes", cfg.p_leg_completes),
            ("p_bank_freeze_per_trade", cfg.p_bank_freeze_per_trade),
        ) if a.confidence == "UNKNOWN")

    return Economics(
        amount_kzt=amount,
        gross_pnl_kzt=gross,
        gross_spread_pct=pair.gross_spread_pct,
        exchange_fees_kzt=exchange_fees,
        bank_fees_kzt=bank_fees,
        fx_drift_kzt=fx_drift,
        known_net_pnl_kzt=net,
        known_net_spread_pct=(net / amount) * 100 if amount else Decimal(0),
        unknown_components=unknown,
    )


def breakeven_p_completion(econ: Economics, loss_if_stuck_kzt: Decimal) -> Decimal | None:
    """При какой вероятности успешного круга матожидание становится нулевым.

        E = p * net - (1 - p) * loss   =>   p* = loss / (net + loss)

    Возвращает None, если net <= 0 (безубыточность недостижима ни при какой
    вероятности) или если loss = 0 (вопрос не имеет смысла).

    Это и есть главный вывод исследования в форме, которую можно проверить
    реальными сделками, а не досимулировать.
    """
    net = econ.known_net_pnl_kzt
    if net <= 0 or loss_if_stuck_kzt <= 0:
        return None
    return loss_if_stuck_kzt / (net + loss_if_stuck_kzt)


def required_spread_pct(amount_kzt: Decimal, p_completion: Decimal,
                        loss_if_stuck_kzt: Decimal) -> Decimal:
    """Обратная задача: какой gross-спред нужен, чтобы при заданной
    вероятности успеха матожидание было положительным."""
    if p_completion <= 0:
        return Decimal("Infinity")
    required_net = (1 - p_completion) * loss_if_stuck_kzt / p_completion
    return (required_net / amount_kzt) * 100


# --------------------------------------------------------------------------
# Результат как функция от НЕИЗМЕРИМОГО
#
# Вероятность того, что перевод дойдёт, и вероятность блокировки счёта в
# API не наблюдаемы ни в каком виде. До сих пор они молча подставлялись
# единицей и нулём — то есть модель считала, что риска нет. Отсюда и
# бралось «4.7% в сутки»: это не найденная неэффективность, а цена
# предположения, что сделка не срывается никогда.
#
# Измерить их можно только реальными сделками. Но перестать выдавать одно
# число вместо диапазона можно прямо сейчас: ниже результат считается на
# сетке допущений, и читатель видит, при каких из них он положителен.
# --------------------------------------------------------------------------

#: вероятности успешного завершения круга, для которых строится сетка
COMPLETION_GRID: tuple[Decimal, ...] = (
    Decimal("0.90"), Decimal("0.95"), Decimal("0.98"),
    Decimal("0.99"), Decimal("0.995"), Decimal("1.00"),
)

#: доля возврата при сорванном круге: полная потеря, половина, почти всё
RECOVERY_GRID: tuple[Decimal, ...] = (
    Decimal("0"), Decimal("0.5"), Decimal("0.85"),
)


def expected_pnl_per_trade(net_kzt: Decimal, ring_kzt: Decimal,
                           p_complete: Decimal, recovery: Decimal) -> Decimal:
    """Матожидание одной сделки при заданных допущениях.

    При срыве теряется не «упущенная прибыль», а непокрытая часть тела:
    деньги уже ушли, актив не получен или не продан.
    """
    loss = ring_kzt * (Decimal(1) - recovery)
    return p_complete * net_kzt - (Decimal(1) - p_complete) * loss


def risk_grid(avg_net_kzt: Decimal, ring_kzt: Decimal, trades_per_day: Decimal,
              capital_kzt: Decimal,
              probs: tuple[Decimal, ...] = COMPLETION_GRID,
              recoveries: tuple[Decimal, ...] = RECOVERY_GRID) -> dict:
    """Суточная доходность на сетке (вероятность успеха × доля возврата).

    Возвращает проценты от капитала в сутки. Ячейка с p=1 — это то самое
    число, которое система показывала как результат: верхний угол сетки,
    а не её середина.
    """
    if capital_kzt <= 0:
        return {"rows": [], "capital_kzt": 0}
    rows = []
    for r in recoveries:
        cells = []
        for p in probs:
            e = expected_pnl_per_trade(avg_net_kzt, ring_kzt, p, r)
            daily = e * trades_per_day / capital_kzt * 100
            cells.append({"p": float(p), "daily_pct": round(float(daily), 3),
                          "positive": daily > 0})
        rows.append({"recovery": float(r), "cells": cells})
    return {"rows": rows, "probs": [float(p) for p in probs],
            "capital_kzt": float(capital_kzt),
            "trades_per_day": float(trades_per_day),
            "ring_kzt": float(ring_kzt)}


def breakeven_completion(avg_net_kzt: Decimal, ring_kzt: Decimal,
                         recovery: Decimal) -> Decimal | None:
    """Вероятность успеха, при которой матожидание обнуляется."""
    loss = ring_kzt * (Decimal(1) - recovery)
    if avg_net_kzt <= 0 or loss <= 0:
        return None
    return loss / (avg_net_kzt + loss)
