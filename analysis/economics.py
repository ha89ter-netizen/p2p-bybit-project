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
