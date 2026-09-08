"""Распределение симулированной прибыли P2P.

Один закрытый цикл с ПОЛОЖИТЕЛЬНЫМ результатом порождает одно событие
распределения: часть уходит в бумажный инвестиционный портфель, часть
остаётся в обороте P2P.

Три правила, без которых учёт разъедется.

1. **Только прибыль.** Ноль и убыток не порождают взноса. Инвестиционный
   портфель НЕ вскрывается для покрытия убытков P2P — это отдельный
   долгосрочный карман. Другая логика, если понадобится, должна быть
   отдельной явной политикой, а не частным случаем этой.

2. **Идемпотентность.** Ключ события выводится из самой сделки
   (момент решения и оба объявления), а не из счётчика. Пересчёт истории
   с нуля — обычная операция в этом проекте, и он не имеет права
   удваивать взносы.

3. **Ничего не совершается.** Это симуляция поверх записанной истории.
   Ни одного платёжного поручения здесь нет и быть не может.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

# Доли, доступные для исследования. 50% — значение по умолчанию, а не
# истина: какой процент лучше, должны показать данные, а не конфиг.
ALLOCATION_RATES: tuple[Decimal, ...] = (
    Decimal("0"), Decimal("25"), Decimal("50"), Decimal("75"), Decimal("100"),
)
DEFAULT_RATE = Decimal("50")

_TENGE = Decimal("0.01")


@dataclass(frozen=True)
class AllocationPolicy:
    """Сколько процентов положительной симулированной прибыли уходит в портфель."""
    rate_pct: Decimal = DEFAULT_RATE
    name: str = ""

    def __post_init__(self):
        if not (Decimal(0) <= self.rate_pct <= Decimal(100)):
            raise ValueError(f"доля вне 0..100: {self.rate_pct}")

    @property
    def label(self) -> str:
        return self.name or f"{int(self.rate_pct)}%"


@dataclass(frozen=True)
class AllocationEvent:
    """Событие распределения по одному циклу."""
    event_id: str
    at: float                      # момент расчёта цикла
    source_ref: str                # ссылка на исходную сделку
    cycle_capital_kzt: Decimal
    gross_pnl_kzt: Decimal
    net_pnl_kzt: Decimal
    rate_pct: Decimal
    allocated_kzt: Decimal
    retained_kzt: Decimal
    policy: str

    @property
    def contributes(self) -> bool:
        return self.allocated_kzt > 0


def trade_ref(trade) -> str:
    """Устойчивая ссылка на сделку: момент решения и обе ноги.

    Индекс в списке не годится — он меняется при смене окна пересчёта,
    и то же самое событие получило бы другой идентификатор.
    """
    return f"{trade.decided_at:.3f}|{trade.buy_ad_id}|{trade.sell_ad_id}"


def event_id(ref: str, policy: AllocationPolicy) -> str:
    raw = f"{ref}|{policy.rate_pct}|{policy.label}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _round(v: Decimal) -> Decimal:
    return v.quantize(_TENGE, rounding=ROUND_DOWN)


def allocate_trade(trade, policy: AllocationPolicy) -> AllocationEvent | None:
    """Событие по одной сделке. None — распределять нечего.

    net берётся из PnL сделки: он уже посчитан после комиссий в
    симуляторе. Валовая величина сохраняется отдельно, чтобы позже
    можно было разделить эффект комиссий и эффект правил входа.
    """
    net = trade.pnl_kzt
    if net <= 0:
        return None

    gross = trade.amount_kzt * trade.spread_expected_pct / 100
    allocated = _round(net * policy.rate_pct / 100)
    ref = trade_ref(trade)
    return AllocationEvent(
        event_id=event_id(ref, policy),
        at=trade.settled_at,
        source_ref=ref,
        cycle_capital_kzt=trade.amount_kzt,
        gross_pnl_kzt=gross,
        net_pnl_kzt=net,
        rate_pct=policy.rate_pct,
        allocated_kzt=allocated,
        retained_kzt=_round(net - allocated),
        policy=policy.label,
    )


def allocate(trades, policy: AllocationPolicy,
             seen: set[str] | None = None) -> list[AllocationEvent]:
    """События по списку сделок, без повторов.

    `seen` — уже учтённые идентификаторы. Передайте его, чтобы дозапись
    к существующему журналу не удвоила взносы.
    """
    seen = set() if seen is None else seen
    out: list[AllocationEvent] = []
    for t in trades:
        ev = allocate_trade(t, policy)
        if ev is None or ev.event_id in seen:
            continue
        seen.add(ev.event_id)
        out.append(ev)
    return out


@dataclass(frozen=True)
class AllocationSummary:
    policy: str
    rate_pct: Decimal
    events: int
    contributing: int
    total_net_pnl_kzt: Decimal        # ВЕСЬ результат, включая убытки
    allocatable_kzt: Decimal          # только положительная часть
    allocated_kzt: Decimal
    retained_kzt: Decimal

    @property
    def check_ok(self) -> bool:
        """Распределённое и оставленное обязаны давать РАСПРЕДЕЛЯЕМУЮ базу.

        Сверять с полным PnL нельзя: убыточные циклы в базу не входят,
        и такая проверка падала бы всегда, когда был хоть один минус.
        Расхождение больше копейки на событие — ошибка округления, а она
        в учёте недопустима в любую сторону.
        """
        diff = abs(self.allocated_kzt + self.retained_kzt - self.allocatable_kzt)
        return diff <= Decimal("0.01") * max(self.events, 1)

    @property
    def losses_kzt(self) -> Decimal:
        """Убытки остаются в P2P: портфель их не покрывает."""
        return self.total_net_pnl_kzt - self.allocatable_kzt


def summarize(events: list[AllocationEvent], policy: AllocationPolicy,
              total_net_pnl_kzt: Decimal | None = None) -> AllocationSummary:
    alloc = sum((e.allocated_kzt for e in events), Decimal(0))
    ret = sum((e.retained_kzt for e in events), Decimal(0))
    base = sum((e.net_pnl_kzt for e in events), Decimal(0))
    net = total_net_pnl_kzt if total_net_pnl_kzt is not None else base
    return AllocationSummary(
        policy=policy.label, rate_pct=policy.rate_pct,
        events=len(events),
        contributing=sum(1 for e in events if e.contributes),
        total_net_pnl_kzt=net, allocatable_kzt=base,
        allocated_kzt=alloc, retained_kzt=ret,
    )
