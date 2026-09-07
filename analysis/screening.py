"""
Фильтры исполнимости, которых нет в структурированных полях API.

Половина реальных условий сделки живёт в свободном тексте `remark`.
Матчер, который проверяет только пересечение методов оплаты и лимитов,
пропускает пары вроде такой (реальный случай из данных):

    покупаем у: «после оплаты чек в чат»
    продаём:    «Чеки не предоставляю❗️»

Формально оба Kaspi, пересечение есть, пара исполнимой не является.

Здесь же — проверка требований объявления К НАМ. Раньше мы их только
читали и помечали флагом «не проверяемо». Если задать свои собственные
показатели в конфиге, проверка становится жёсткой, и часть фальшивого
верха стакана отваливается сама.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from domain.models import Ad, Pair

# Требования, встречающиеся в тексте. Проценты — доля объявлений
# в собранных данных (n=1965).
RE_WANTS_RECEIPT = re.compile(r"чек|квитанц|скрин", re.I)          # 34%
RE_NO_RECEIPT = re.compile(r"чек[иов]?\s*не\s*(предоставл|даю|отправл)", re.I)  # 5%
RE_FIRST_PERSON = re.compile(r"перв[оы]|1[-\s]?[хго]?\s*лиц|однофамил|владельц", re.I)  # 27%
RE_NO_THIRD_PARTY = re.compile(r"треть|3[-\s]?х?\s*лиц", re.I)     # 27%
RE_EXACT_AMOUNT = re.compile(r"точн\w*\s*сумм|не\s*округл", re.I)
RE_NO_COMMENT = re.compile(r"без\s*коммент|не\s*пиш\w*\s*коммент", re.I)


@dataclass(frozen=True)
class Requirements:
    """Что объявление требует и что обещает."""
    wants_receipt: bool
    refuses_receipt: bool
    first_person_only: bool
    no_third_party: bool
    exact_amount: bool
    no_comment: bool

    @staticmethod
    def parse(remark: str) -> "Requirements":
        t = remark or ""
        no_receipt = bool(RE_NO_RECEIPT.search(t))
        return Requirements(
            # «чеки не предоставляю» не должно считаться требованием чека
            wants_receipt=bool(RE_WANTS_RECEIPT.search(t)) and not no_receipt,
            refuses_receipt=no_receipt,
            first_person_only=bool(RE_FIRST_PERSON.search(t)),
            no_third_party=bool(RE_NO_THIRD_PARTY.search(t)),
            exact_amount=bool(RE_EXACT_AMOUNT.search(t)),
            no_comment=bool(RE_NO_COMMENT.search(t)),
        )


@dataclass(frozen=True)
class MyProfile:
    """Наши собственные показатели — чтобы проверять требования К НАМ.

    Без этого верх стакана наполовину состоит из объявлений, которые нас
    просто не пустят, а мы считаем их возможностями.
    """
    completed_orders_30d: int = 0
    completion_rate_30d: int = 0
    kyc_passed: bool = True
    can_provide_receipt: bool = True
    pays_from_own_card: bool = True          # переводим со своей карты


def blocking_reasons(ad: Ad, me: MyProfile) -> tuple[str, ...]:
    """Почему это объявление нас не пустит. Пусто — препятствий нет."""
    out = []
    p = ad.prefs
    if p.requires_kyc and not me.kyc_passed:
        out.append("kyc_required")
    if p.min_orders_30d > me.completed_orders_30d:
        out.append(f"needs_{p.min_orders_30d}_orders_30d")
    if p.min_complete_rate_30d > me.completion_rate_30d:
        out.append(f"needs_{p.min_complete_rate_30d}pct_rate")
    if p.has_national_limit:
        out.append("national_limit__unverifiable")

    r = Requirements.parse(ad.remark)
    if r.wants_receipt and not me.can_provide_receipt:
        out.append("receipt_required")
    if (r.first_person_only or r.no_third_party) and not me.pays_from_own_card:
        out.append("first_person_only")
    return tuple(out)


def pair_conflicts(pair: Pair) -> tuple[str, ...]:
    """Взаимно несовместимые условия двух ног.

    Ключевой случай: одна сторона требует чек, другая отказывается его
    давать. Пересечение методов оплаты при этом есть, и обычный матчер
    такую пару пропускает.
    """
    buy = Requirements.parse(pair.buy_ad.remark)
    sell = Requirements.parse(pair.sell_ad.remark)
    out = []
    # Продаём USDT: контрагент платит нам. Если он чеков не даёт, а
    # продавец первой ноги требует подтверждения происхождения — цепочка
    # рвётся на доказательствах.
    if buy.wants_receipt and sell.refuses_receipt:
        out.append("receipt_chain_broken")
    if sell.refuses_receipt and buy.first_person_only:
        out.append("cannot_prove_first_person")
    return tuple(out)


def same_bank(pair: Pair) -> bool:
    """Обе ноги через один банк — перевод внутри банка быстрее и обычно
    без комиссии. Для Kaspi→Kaspi это мгновенно."""
    return bool(set(pair.buy_ad.payments) & set(pair.sell_ad.payments) &
                {"150"})          # 150 = Kaspi Bank


def price_outlier(ad: Ad, market_median: Decimal,
                  max_dev_pct: Decimal) -> bool:
    """Цена сильно лучше рынка почти всегда означает подвох.

    Отклонение считается ЗНАКОВЫМ: подозрительна только цена в нашу
    пользу. Покупка сильно дешевле рынка или продажа сильно дороже — это
    и есть приманка. Цена в невыгодную сторону просто неинтересна, но
    ничего не сообщает о добросовестности.
    """
    if market_median <= 0:
        return False
    dev = (ad.price / market_median - 1) * 100
    if ad.side == "1":          # мы покупаем: подозрительно дёшево
        return dev < -max_dev_pct
    return dev > max_dev_pct    # мы продаём: подозрительно дорого


class Blacklist:
    """Контрагенты, чьи объявления регулярно висят в топе и не торгуются.

    Ровно тот случай, который надул первую версию симулятора: выгодная
    цена, полный остаток, ноль сделок за сутки. Список набирается из
    наблюдений, а не задаётся руками.
    """

    def __init__(self, min_appearances: int = 20):
        self.min_appearances = min_appearances
        self._seen: dict[str, int] = {}
        self._moved: dict[str, int] = {}

    def observe(self, advertiser_key: str, volume_moved: bool) -> None:
        self._seen[advertiser_key] = self._seen.get(advertiser_key, 0) + 1
        if volume_moved:
            self._moved[advertiser_key] = self._moved.get(advertiser_key, 0) + 1

    def is_blocked(self, advertiser_key: str) -> bool:
        seen = self._seen.get(advertiser_key, 0)
        return seen >= self.min_appearances and self._moved.get(advertiser_key, 0) == 0

    def blocked(self) -> list[str]:
        return [k for k in self._seen if self.is_blocked(k)]


@dataclass(frozen=True)
class ScreenConfig:
    me: MyProfile = field(default_factory=MyProfile)
    # Объявление должно было торговаться недавно, а не «когда-нибудь».
    require_recent_volume_sec: int = 3600
    # Мейкер отпускает быстро — иначе капитал заперт дольше, чем спред живёт.
    max_release_sec: int = 300
    # Цена не должна быть выбросом относительно рынка.
    max_deviation_from_median_pct: Decimal = Decimal("8")
    # Сколько раз объявление контрагента должно появиться в топе без
    # единой сделки, чтобы попасть в чёрный список.
    blacklist_after_appearances: int = 20
    # Ограничение «обе ноги через один банк» СОЗНАТЕЛЬНО не включено:
    # замер показал, что экономия на комиссии (~500 ₸) не окупает
    # потерю ~0.5 пп спреда (~1500 ₸ на 300k).
    require_same_bank: bool = False
