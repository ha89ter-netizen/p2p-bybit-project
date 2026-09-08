"""
Типизированные модели предметной области.

Деньги — только Decimal, и только из строки. float здесь запрещён:
цена 473.99 в float это 473.98999999999999488, и после умножения
на 300000 разница вылезает в отчёт.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any


def _dec(v: Any, default: str = "0") -> Decimal:
    """Decimal из чего угодно, что прислал API. Пустая строка -> default."""
    if v is None or v == "":
        return Decimal(default)
    return Decimal(str(v))


def _int(v: Any, default: int = 0) -> int:
    """Целое из поля API.

    Поля, объявленные целыми, приходят дробной строкой: в живых данных
    встречено completeRateDay30="96.5". Прежняя версия ловила ValueError
    и молча возвращала 0, то есть объявление с требованием 96.5% успешных
    сделок выглядело как объявление БЕЗ требований. Тихое смещение ровно
    в той логике, которая решает исполнимость пары.
    """
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    try:
        return int(Decimal(str(v)))          # усечение
    except (InvalidOperation, TypeError, ValueError):
        return default


def _int_ceil(v: Any, default: int = 0) -> int:
    """То же, но с округлением ВВЕРХ — для требований объявления К НАМ.

    Требование 96.5% при усечении до 96 сделало бы нас проходящими там,
    где мы не проходим. Ошибаться следует в сторону строгости.
    """
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return int(d.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Advertiser:
    """Контрагент. userId/accountId в публичной выдаче нулевые,
    поэтому ключ склеиваем из userMaskId, а при его отсутствии — из ника.

    ЗДЕСЬ ТОЛЬКО ТО, ЧТО ОТНОСИТСЯ К ЧЕЛОВЕКУ. finishNum и orderNum сюда
    НЕ входят: проверено на живых данных, что это счётчики КОНКРЕТНОГО
    ОБЪЯВЛЕНИЯ. Один и тот же контрагент показывает finishNum=100 в одном
    своём объявлении и 24 в другом при общем recentOrderNum=1107.
    Путать их — значит мерить возраст объявления вместо репутации.
    """
    key: str
    nick: str
    user_type: str
    auth_tags: tuple[str, ...]
    recent_order_num: int
    recent_execute_rate: int
    latest_pay_ms: int
    latest_release_ms: int
    is_online: bool

    @property
    def latest_release_sec(self) -> Decimal:
        return _dec(self.latest_release_ms) / 1000

    @property
    def latest_pay_sec(self) -> Decimal:
        return _dec(self.latest_pay_ms) / 1000


@dataclass(frozen=True)
class TradingPrefs:
    """Требования объявления К НАМ. Прочитать можем, соответствуем ли —
    API не скажет. Поэтому это флаги риска, а не фильтр исполнимости."""
    requires_kyc: bool
    min_orders_30d: int
    min_complete_rate_30d: int
    has_national_limit: bool
    has_single_user_order_limit: bool
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_restrictive(self) -> bool:
        return (self.min_orders_30d > 0
                or self.min_complete_rate_30d > 0
                or self.has_national_limit
                or self.has_single_user_order_limit)


@dataclass(frozen=True)
class Ad:
    """Объявление в конкретный момент наблюдения."""
    ad_id: str
    host: str
    side: str                 # "1" = мы покупаем USDT, "0" = мы продаём
    advertiser: Advertiser
    token: str
    currency: str
    price: Decimal
    quantity: Decimal         # ИСХОДНЫЙ объём объявления, НЕ доступный остаток
    frozen_quantity: Decimal
    min_amount: Decimal       # KZT
    max_amount: Decimal       # KZT
    payments: tuple[str, ...]
    prefs: TradingPrefs
    remark: str
    version: int
    status: int
    observed_at: float
    # Счётчики ЭТОГО объявления, не контрагента. См. комментарий в Advertiser.
    finish_num: int = 0
    order_num: int = 0
    # Доступный остаток. Проверено на живых данных:
    #   quantity - executed - frozen == lastQuantity,
    #   и maxAmount / price == lastQuantity почти во всех объявлениях.
    # Пример: quantity=131203.74, executed=128723.66, last=2480.08 —
    # счёт по quantity завысил бы ликвидность в 53 раза.
    last_quantity: Decimal = Decimal(0)
    executed_quantity: Decimal = Decimal(0)

    # ---- производные ----

    @property
    def available_quantity(self) -> Decimal:
        """Доступный остаток USDT: lastQuantity, а не quantity."""
        return self.last_quantity

    @property
    def liquidity_kzt(self) -> Decimal:
        """Сколько KZT реально можно провести через это объявление."""
        return self.available_quantity * self.price

    def supports(self, amount_kzt: Decimal) -> bool:
        return (self.min_amount <= amount_kzt <= self.max_amount
                and self.liquidity_kzt >= amount_kzt)

    @property
    def ad_success_rate(self) -> Decimal:
        """Доля успешных сделок ИМЕННО ЭТОГО объявления."""
        if not self.order_num:
            return Decimal(0)
        return Decimal(self.finish_num) / Decimal(self.order_num) * 100

    def meets(self, stratum) -> bool:
        """Качество = репутация контрагента (recent_*) И зрелость объявления
        (finish_num). Это два разных измерения, и смешивать их нельзя."""
        return (self.finish_num >= stratum.min_ad_finish_num
                and self.advertiser.recent_order_num >= stratum.min_recent_orders
                and self.advertiser.recent_execute_rate >= stratum.min_execute_rate)

    # ---- разбор сырого ответа ----

    @staticmethod
    def from_api(item: dict[str, Any], host: str, observed_at: float) -> "Ad":
        prefs_raw = item.get("tradingPreferenceSet") or {}
        mask = (item.get("userMaskId") or "").strip()
        nick = (item.get("nickName") or "").strip()
        # Пустой ник дал бы всем таким объявлениям общий ключ "nick:" и
        # склеил бы разных людей в одного контрагента — падаем на ad_id.
        if mask:
            key = f"mask:{mask}"
        elif nick:
            key = f"nick:{nick}"
        else:
            key = f"ad:{item['id']}"

        return Ad(
            ad_id=str(item["id"]),
            host=host,
            side=str(item.get("side", "")),
            advertiser=Advertiser(
                key=key,
                nick=nick,
                user_type=str(item.get("userType") or ""),
                auth_tags=tuple(item.get("authTag") or ()),
                recent_order_num=_int(item.get("recentOrderNum")),
                recent_execute_rate=_int(item.get("recentExecuteRate")),
                latest_pay_ms=_int(item.get("latestPayTime")),
                latest_release_ms=_int(item.get("latestReleaseTime")),
                is_online=bool(item.get("isOnline")),
            ),
            token=str(item.get("tokenId") or ""),
            currency=str(item.get("currencyId") or ""),
            price=_dec(item.get("price")),
            quantity=_dec(item.get("quantity")),
            frozen_quantity=_dec(item.get("frozenQuantity")),
            min_amount=_dec(item.get("minAmount")),
            max_amount=_dec(item.get("maxAmount")),
            payments=tuple(str(p) for p in (item.get("payments") or ())),
            prefs=TradingPrefs(
                requires_kyc=bool(_int(prefs_raw.get("isKyc"))),
                min_orders_30d=_int_ceil(prefs_raw.get("orderFinishNumberDay30")),
                min_complete_rate_30d=_int_ceil(prefs_raw.get("completeRateDay30")),
                has_national_limit=bool(_int(prefs_raw.get("hasNationalLimit"))),
                has_single_user_order_limit=bool(
                    _int(prefs_raw.get("hasSingleUserOrderLimit"))),
                raw=prefs_raw,
            ),
            remark=str(item.get("remark") or ""),
            version=_int(item.get("version")),
            status=_int(item.get("status")),
            observed_at=observed_at,
            finish_num=_int(item.get("finishNum")),
            order_num=_int(item.get("orderNum")),
            last_quantity=_dec(item.get("lastQuantity")),
            executed_quantity=_dec(item.get("executedQuantity")),
        )

    def state_fingerprint(self) -> tuple:
        """Что считается изменением состояния. Пишем ad_state только когда
        этот кортеж отличается от предыдущего — иначе БД растёт линейно
        по времени вместо линейного роста по событиям."""
        return (str(self.price), str(self.quantity), str(self.frozen_quantity),
                str(self.min_amount), str(self.max_amount),
                self.version, self.status, self.finish_num, self.order_num,
                str(self.last_quantity), str(self.executed_quantity))


@dataclass(frozen=True)
class Pair:
    """Кандидат: покупаем на buy_ad, продаём на sell_ad, на сумму amount_kzt.

    Намеренно НЕ называется Opportunity и НЕ сохраняется в БД.
    Пара — это функция от (состояние книги, конфиг). Если сохранить её,
    то при смене порога или модели придётся пересчитывать всю историю.
    """
    amount_kzt: Decimal
    buy_ad: Ad
    sell_ad: Ad
    common_payments: tuple[str, ...]
    rejection_reasons: tuple[str, ...] = ()

    #: причины, делающие пару физически неисполнимой (остальное — предупреждения)
    HARD_REJECTIONS = frozenset({
        "same_counterparty", "no_common_payment", "no_payment_we_hold",
        "buy_leg_cannot_fill", "sell_leg_cannot_fill",
    })

    @property
    def executable(self) -> bool:
        """Мягкие флаги (напр. нечитаемые требования объявления к нам)
        не делают пару неисполнимой — иначе мы выбросили бы объявления,
        которые, возможно, нам доступны."""
        return not (Pair.HARD_REJECTIONS & set(self.rejection_reasons))

    @property
    def usdt_bought(self) -> Decimal:
        if self.buy_ad.price <= 0:
            return Decimal(0)
        return self.amount_kzt / self.buy_ad.price

    @property
    def gross_spread_pct(self) -> Decimal:
        # Цена 0 в книге быть не должна, но битое поле не должно ронять отчёт.
        if self.buy_ad.price <= 0:
            return Decimal(0)
        return (self.sell_ad.price / self.buy_ad.price - 1) * 100

    @property
    def gross_pnl_kzt(self) -> Decimal:
        """Продали ровно то, что купили. Обе цены — в KZT за USDT."""
        return self.usdt_bought * self.sell_ad.price - self.amount_kzt

    @property
    def quality_floor(self) -> tuple[int, int, int]:
        """Качество пары = качество худшей из двух ног, по трём измерениям:
        (зрелость объявления, оборот контрагента, доля успешных сделок)."""
        return (min(self.buy_ad.finish_num, self.sell_ad.finish_num),
                min(self.buy_ad.advertiser.recent_order_num,
                    self.sell_ad.advertiser.recent_order_num),
                min(self.buy_ad.advertiser.recent_execute_rate,
                    self.sell_ad.advertiser.recent_execute_rate))

    @property
    def advertiser_pair_key(self) -> str:
        """Ключ повторяемости пары рекламодателей — главный признак
        для отличения рынка от двух статичных витрин."""
        return f"{self.buy_ad.advertiser.key}|{self.sell_ad.advertiser.key}"

    @property
    def expected_roundtrip_sec(self) -> Decimal:
        """Нижняя оценка времени круга по собственной статистике
        рекламодателей. Без учёта нашего ручного перевода."""
        return (self.buy_ad.advertiser.latest_release_sec
                + self.sell_ad.advertiser.latest_pay_sec)
