"""
Бумажная торговля по записанной истории.

НИКАКИХ РЕАЛЬНЫХ ОРДЕРОВ. Симулятор проходит по сохранённым снимкам книги
и считает, что было бы, если бы мы входили в сделки по своим правилам.

Три принципа, без которых число получилось бы выдуманным:

1. КАПИТАЛ КОНЕЧЕН. С 300 000 KZT нельзя войти в две сделки сразу: деньги
   заперты, пока круг не закроется. Наивный подсчёт «сколько было
   возможностей x спред» завышает результат в разы, потому что молча
   предполагает бесконечные деньги.

2. НЕТ ЗАГЛЯДЫВАНИЯ В БУДУЩЕЕ. Решение принимается по книге в момент t.
   Исполнение проверяется по книге в t+delay — по РЕАЛЬНО ЗАПИСАННЫМ
   данным, а не по предположению. Это измерение, а не симуляция.

3. ИСПОЛНЕНИЕ НЕ МГНОВЕННО. Между решением и переводом проходит время, и
   объявление может исчезнуть. Симулятор проверяет, дожило ли оно.

4. КАПИТАЛ РЕИНВЕСТИРУЕТСЯ. Торгуем фиксированной ДОЛЕЙ капитала, а не
   фиксированной суммой: заработали — следующая сделка больше, потеряли —
   меньше. Это и защита (просадка автоматически уменьшает ставку), и
   честность (иначе прибыль не капитализируется).

5. НАШИ СДЕЛКИ СЪЕДАЮТ ОБЪЯВЛЕНИЕ. Без этого симулятор входит в одну и ту
   же пару снова и снова, прокачивая через неё больше денег, чем там было.
   Именно так получаются отчёты вида «15% в день».

Чего этот модуль НЕ умеет и уметь не может: оценить вероятность того, что
перевод дойдёт и что счёт не заблокируют. Эти два числа не наблюдаемы в
API, а знак результата определяют именно они. Поэтому наряду с PnL
считается breakeven — при какой вероятности успеха результат обнуляется.
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass, field
from decimal import Decimal

from analysis.matching import iter_pairs_by_spread
from analysis.replay import book_at, observation_times
from analysis.screening import (Blacklist, MyProfile, blocking_reasons,
                                pair_conflicts, price_outlier)
from config.settings import PAPER, PaperConfig, QualityStratum
from domain.models import Ad, Pair
from storage.db import Store


@dataclass(frozen=True)
class PaperTrade:
    decided_at: float
    executed_at: float
    settled_at: float
    amount_kzt: Decimal
    buy_price: Decimal
    sell_price_expected: Decimal
    sell_price_actual: Decimal | None      # None = продать не удалось
    usdt: Decimal
    pnl_kzt: Decimal
    outcome: str                           # filled | slipped | buy_leg_vanished | stuck
    buy_ad_id: str
    sell_ad_id: str
    buy_adv: str = ""
    sell_adv: str = ""

    @property
    def spread_expected_pct(self) -> Decimal:
        return (self.sell_price_expected / self.buy_price - 1) * 100

    @property
    def spread_actual_pct(self) -> Decimal:
        if self.sell_price_actual is None:
            return Decimal(0)
        return (self.sell_price_actual / self.buy_price - 1) * 100


@dataclass
class Equity:
    """Точка кривой капитала."""
    at: float
    capital_kzt: Decimal
    position_kzt: Decimal


@dataclass
class PaperResult:
    trades: list[PaperTrade] = field(default_factory=list)
    equity: list[Equity] = field(default_factory=list)
    start_capital_kzt: Decimal = Decimal(0)
    final_capital_kzt: Decimal = Decimal(0)
    blacklisted: list[str] = field(default_factory=list)
    entries_screened_out: int = 0
    window_from: float = 0.0
    window_to: float = 0.0
    moments: int = 0
    entries_skipped_no_capital: int = 0
    entries_skipped_no_pair: int = 0
    entries_skipped_exhausted: int = 0
    entries_skipped_absent: int = 0      # возможность была, нас не было

    # Диагностика достоверности: сколько раз мы «торговали» объявление,
    # у которого за всё время наблюдения не сдвинулся executedQuantity.
    # Если объявление простояло час и никто его не тронул, а мы якобы
    # прокрутили через него 1.5 млн — значит оно НЕ было исполнимым,
    # и виноваты условия, которых мы не видим в API.
    trades_on_untouched_ads: int = 0
    reprices: int = 0                    # пришлось искать другую ногу
    entries_skipped_adv_limit: int = 0   # упёрлись в лимит на контрагента

    # ---- агрегаты ----

    @property
    def n(self) -> int:
        return len(self.trades)

    def profit_by_advertiser(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for t in self.trades:
            if t.sell_adv:
                out[t.sell_adv] = out.get(t.sell_adv, Decimal(0)) + t.pnl_kzt
        return out

    def concentration_hhi(self) -> float | None:
        """Индекс Херфиндаля по прибыли на контрагента, 0..1.

        1.0 — вся прибыль от одного человека. Замерено без лимита: двое
        давали 69%, и такой результат описывает не рынок, а двоих людей.
        Показатель нужен на видном месте, чтобы перекос замечался сразу,
        а не всплывал через месяц при разборе.
        """
        pos = {k: float(v) for k, v in self.profit_by_advertiser().items() if v > 0}
        tot = sum(pos.values())
        if tot <= 0:
            return None
        return round(sum((v / tot) ** 2 for v in pos.values()), 4)

    def top_share(self, k: int = 1) -> float | None:
        pos = sorted((float(v) for v in self.profit_by_advertiser().values() if v > 0),
                     reverse=True)
        tot = sum(pos)
        if tot <= 0:
            return None
        return round(100 * sum(pos[:k]) / tot, 1)

    def daily_return_pct(self) -> Decimal | None:
        if self.hours <= 0 or self.start_capital_kzt <= 0:
            return None
        return self.return_pct(self.start_capital_kzt) / Decimal(str(self.hours / 24))

    def implausible(self, cfg: PaperConfig = PAPER) -> bool:
        """Правдоподобен ли результат для розничного валютного рынка.

        Это не исправление расчёта, а пометка. Когда симуляция даёт
        десятки процентов в сутки, вероятнее ошибка модели, чем найденная
        неэффективность, и относиться к числу нужно соответственно.
        """
        d = self.daily_return_pct()
        return d is not None and d > cfg.plausible_daily_return_pct

    @property
    def filled(self) -> list[PaperTrade]:
        return [t for t in self.trades if t.outcome in ("filled", "slipped")]

    @property
    def total_pnl_kzt(self) -> Decimal:
        return sum((t.pnl_kzt for t in self.trades), Decimal(0))

    @property
    def hours(self) -> float:
        return max((self.window_to - self.window_from) / 3600, 1e-9)

    @property
    def pnl_per_day_kzt(self) -> Decimal:
        return self.total_pnl_kzt / Decimal(str(self.hours)) * 24

    def return_pct(self, capital: Decimal) -> Decimal:
        return self.total_pnl_kzt / capital * 100 if capital else Decimal(0)

    @property
    def growth_pct(self) -> Decimal:
        if not self.start_capital_kzt:
            return Decimal(0)
        return (self.final_capital_kzt / self.start_capital_kzt - 1) * 100

    @property
    def max_drawdown_pct(self) -> Decimal:
        """Худшая просадка от достигнутого пика."""
        peak = self.start_capital_kzt
        worst = Decimal(0)
        for e in self.equity:
            peak = max(peak, e.capital_kzt)
            if peak > 0:
                worst = min(worst, (e.capital_kzt / peak - 1) * 100)
        return worst

    def outcomes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.trades:
            out[t.outcome] = out.get(t.outcome, 0) + 1
        return out

    def breakeven_success_rate(self) -> Decimal | None:
        """Какая доля сделок должна доходить до конца, чтобы выйти в ноль.

        Если сделка сорвалась после перевода KZT, мы теряем не «упущенную
        прибыль», а застрявшие деньги. Здесь консервативно считается, что
        неудачный круг стоит нам одного спреда в минус.
        """
        good = [t for t in self.filled if t.pnl_kzt > 0]
        if not good:
            return None
        avg_win = sum((t.pnl_kzt for t in good), Decimal(0)) / len(good)
        avg_loss = avg_win          # симметричное допущение, явное и грубое
        return avg_loss / (avg_win + avg_loss)


def _quality_ok(ad: Ad, cfg: PaperConfig) -> bool:
    return (ad.finish_num >= cfg.min_ad_finish_num
            and ad.advertiser.recent_order_num >= cfg.min_recent_orders
            and ad.advertiser.recent_execute_rate >= cfg.min_execute_rate)


def _find_entry(book: list[Ad], amount: Decimal,
                cfg: PaperConfig) -> Pair | None:
    for pair in iter_pairs_by_spread(book, amount):
        if pair.gross_spread_pct < cfg.min_spread_pct:
            return None                     # дальше только хуже
        if _quality_ok(pair.buy_ad, cfg) and _quality_ok(pair.sell_ad, cfg):
            return pair
    return None


def _ad_at(book: list[Ad], ad_id: str) -> Ad | None:
    for ad in book:
        if ad.ad_id == ad_id:
            return ad
    return None


def _real_volume_moved(store: Store, ad_id: str) -> Decimal:
    """Сколько USDT реально прошло через объявление за время наблюдения.

    executedQuantity — счётчик самой биржи. Если он не двигался, объявление
    стояло нетронутым: никто не смог или не захотел его взять.
    """
    row = store.conn.execute(
        "SELECT MIN(CAST(executed_qty AS REAL)), MAX(CAST(executed_qty AS REAL))"
        " FROM ad_state WHERE ad_id=?", (ad_id,)).fetchone()
    if not row or row[0] is None:
        return Decimal(0)
    return Decimal(str(row[1] - row[0]))


def _traded_ad_ids(store: Store) -> set[str]:
    """Объявления, у которых счётчик исполнения реально двигался."""
    return {r[0] for r in store.conn.execute(
        "SELECT ad_id FROM ad_state GROUP BY ad_id"
        " HAVING MAX(CAST(executed_qty AS REAL)) >"
        "        MIN(CAST(executed_qty AS REAL))").fetchall()}


def _last_volume_move(store: Store) -> dict[str, float]:
    """Когда у каждого объявления последний раз рос executedQuantity."""
    out: dict[str, float] = {}
    prev: dict[str, float] = {}
    for r in store.conn.execute(
            "SELECT ad_id, observed_at, CAST(executed_qty AS REAL) e"
            " FROM ad_state ORDER BY ad_id, observed_at"):
        k = r["ad_id"]
        if k in prev and r["e"] > prev[k]:
            out[k] = r["observed_at"]
        prev[k] = r["e"]
    return out


def _market_median(book: list[Ad]) -> dict[str, Decimal]:
    """Медианная цена по каждой стороне — базa для отсева выбросов."""
    out: dict[str, Decimal] = {}
    for side in ("1", "0"):
        prices = sorted(a.price for a in book if a.side == side)
        if prices:
            out[side] = prices[len(prices) // 2]
    return out


def simulate(store: Store, host: str, cfg: PaperConfig = PAPER,
             since: float | None = None) -> PaperResult:
    """Пройти историю и посчитать, что было бы.

    Детерминированно: одни и те же данные дают один и тот же результат,
    поэтому пересчёт с нуля безопаснее, чем накопление состояния в БД —
    оно рассинхронизировалось бы при любой смене правил.
    """
    times = observation_times(store, host, min_spacing_sec=60)
    if since is not None:
        times = [t for t in times if t >= since]
    res = PaperResult(moments=len(times))
    if not times:
        return res
    res.window_from, res.window_to = times[0], times[-1]

    capital = cfg.capital_kzt
    res.start_capital_kzt = capital
    busy_until = 0.0                        # капитал занят до этого момента
    consumed: dict[str, Decimal] = {}       # сколько KZT мы уже забрали из объявления
    tradable = _traded_ad_ids(store) if cfg.require_ad_shows_volume else None
    last_volume = _last_volume_move(store)  # когда объявление торговалось в последний раз
    me = MyProfile(completed_orders_30d=cfg.my_orders_30d,
                   completion_rate_30d=cfg.my_rate_30d)
    black = Blacklist(min_appearances=cfg.blacklist_after)
    adv_trades: dict[str, int] = {}       # сделок с контрагентом
    adv_volume: dict[str, Decimal] = {}   # объём через контрагента
    # Отбор участия детерминирован сидом: пересчёт даёт тот же результат.
    rng = _random.Random(cfg.participation_seed)

    def position_size() -> Decimal:
        base = capital if cfg.compound else cfg.capital_kzt
        return base * cfg.deploy_pct / 100

    def adv_has_room(ad: Ad, amount: Decimal) -> bool:
        """Не исчерпан ли лимит на этого контрагента.

        Без него симулятор девять раз подряд забирает у одного и того же
        по невыгодной ему цене, а тот не реагирует. Это не рынок.
        """
        k = ad.advertiser.key
        if cfg.max_trades_per_advertiser and \
                adv_trades.get(k, 0) >= cfg.max_trades_per_advertiser:
            return False
        if cfg.max_volume_per_advertiser_kzt and \
                adv_volume.get(k, Decimal(0)) + amount > cfg.max_volume_per_advertiser_kzt:
            return False
        return True

    def has_room(ad: Ad, amount: Decimal) -> bool:
        if tradable is not None and ad.ad_id not in tradable:
            return False
        if not adv_has_room(ad, amount):
            return False
        return ad.liquidity_kzt - consumed.get(ad.ad_id, Decimal(0)) >= amount

    def screen_ad(ad: Ad, t: float, median: Decimal) -> bool:
        """Шаги 2, 4, 5 плюс выбросы цены и чёрный список."""
        if cfg.screen_requirements and blocking_reasons(ad, me):
            return False
        if cfg.recent_volume_sec and last_volume.get(ad.ad_id, 0) < t - cfg.recent_volume_sec:
            return False
        if cfg.max_release_sec and ad.advertiser.latest_release_sec > cfg.max_release_sec:
            return False
        if price_outlier(ad, median, cfg.max_price_deviation_pct):
            return False
        if black.is_blocked(ad.advertiser.key):
            return False
        return True

    for t in times:
        amount = position_size()
        res.equity.append(Equity(at=t, capital_kzt=capital, position_kzt=amount))

        if t < busy_until:
            res.entries_skipped_no_capital += 1
            continue

        book = book_at(store, host, t)
        # Чёрный список набирается по всем наблюдениям, а не только по сделкам.
        for a in book:
            black.observe(a.advertiser.key,
                          last_volume.get(a.ad_id, 0) >= t - cfg.recent_volume_sec)

        median = _market_median(book)
        available = [a for a in book if has_room(a, amount)]
        if len(available) < len(book):
            res.entries_skipped_exhausted += 1
        # Отдельно, ради диагностики: скольких отсёк именно лимит на
        # контрагента, а не исчерпанная ликвидность объявления.
        if any(not adv_has_room(a, amount) for a in book):
            res.entries_skipped_adv_limit += 1
        screened = [a for a in available
                    if screen_ad(a, t, median.get(a.side, Decimal(0)))]
        if len(screened) < len(available):
            res.entries_screened_out += 1

        pair = _find_entry(screened, amount, cfg)
        if pair is not None and cfg.screen_conflicts and pair_conflicts(pair):
            pair = None                     # шаг 3: несовместимые условия ног
        if pair is None:
            res.entries_skipped_no_pair += 1
            continue

        # Возможность найдена. Успели ли мы к ней? Бросок делается ТОЛЬКО
        # здесь, иначе последовательность зависела бы от числа пустых
        # моментов и результат перестал бы быть воспроизводимым.
        if cfg.participation_pct < 100 and \
                rng.random() * 100 >= float(cfg.participation_pct):
            res.entries_skipped_absent += 1
            continue

        # --- решение принято в t. Дальше только реально записанные данные ---
        exec_at = t + cfg.execution_delay_sec
        settle_at = t + cfg.roundtrip_sec
        future = book_at(store, host, exec_at)

        buy_still = _ad_at(future, pair.buy_ad.ad_id)
        if buy_still is None or buy_still.price != pair.buy_ad.price:
            # покупка не состоялась — деньги не ушли, потерь нет
            res.trades.append(PaperTrade(
                decided_at=t, executed_at=exec_at, settled_at=exec_at,
                amount_kzt=amount, buy_price=pair.buy_ad.price,
                sell_price_expected=pair.sell_ad.price, sell_price_actual=None,
                usdt=Decimal(0), pnl_kzt=Decimal(0),
                outcome="buy_leg_vanished",
                buy_ad_id=pair.buy_ad.ad_id, sell_ad_id=pair.sell_ad.ad_id,
                buy_adv=pair.buy_ad.advertiser.key,
                sell_adv=pair.sell_ad.advertiser.key))
            continue

        usdt = amount / buy_still.price

        # USDT куплены. Теперь их НАДО продать — обратной дороги нет.
        settle_book = book_at(store, host, settle_at)
        sell_still = _ad_at(settle_book, pair.sell_ad.ad_id)

        # Продаём по ЛУЧШЕЙ доступной цене, а не обязательно тому же
        # контрагенту. Заглядывания в будущее здесь нет: книга на момент
        # расчёта наблюдаема целиком.
        #
        # Прежняя версия слепо исполняла исходное объявление, если оно ещё
        # существовало. Наблюдённый случай: контрагент переставил цену с
        # 485.00 на 467.00 за время перевода, а рядом стояло 128 подходящих
        # объявлений с лучшей ценой 486.00. Симулятор продавал по 467.00 и
        # терял 11 935 ₸ на ровном месте, моделируя трейдера, который этого
        # не заметил.
        # Фильтр на выходе ТОТ ЖЕ, что на входе. Иначе получалось, что мы
        # продаём контрагенту, которому на входе отказали бы: не прошедшему
        # требования к нам, простоявшему без сделок, медленному на релизе,
        # выбросу по цене или из чёрного списка. Такая асимметрия завышает
        # результат бесплатно.
        settle_median = _market_median(settle_book)
        alts = [a for a in settle_book
                if a.side == "0" and a.supports(amount)
                and _quality_ok(a, cfg)
                and screen_ad(a, settle_at, settle_median.get(a.side, Decimal(0)))]
        if sell_still is not None and sell_still.supports(amount) \
                and _quality_ok(sell_still, cfg) and sell_still not in alts:
            alts.append(sell_still)

        if alts:
            best = max(alts, key=lambda a: a.price)
            same_ad = sell_still is not None and best.ad_id == sell_still.ad_id
            unchanged = same_ad and best.price == pair.sell_ad.price
            sell_price = best.price
            outcome = "filled" if unchanged else "slipped"
            if not unchanged:
                res.reprices += 1
        else:
            if True:
                # продать некому: остались с USDT на руках
                res.trades.append(PaperTrade(
                    decided_at=t, executed_at=exec_at, settled_at=settle_at,
                    amount_kzt=amount, buy_price=buy_still.price,
                    sell_price_expected=pair.sell_ad.price,
                    sell_price_actual=None, usdt=usdt, pnl_kzt=Decimal(0),
                    outcome="stuck",
                    buy_ad_id=pair.buy_ad.ad_id, sell_ad_id=pair.sell_ad.ad_id,
                buy_adv=pair.buy_ad.advertiser.key,
                sell_adv=pair.sell_ad.advertiser.key))
                busy_until = settle_at
                continue

        pnl = usdt * sell_price - amount

        # Наши сделки съедают обе ноги и расходуют лимит контрагентов.
        for ad_id in (pair.buy_ad.ad_id, pair.sell_ad.ad_id):
            consumed[ad_id] = consumed.get(ad_id, Decimal(0)) + amount
        for a in (pair.buy_ad, pair.sell_ad):
            k = a.advertiser.key
            adv_trades[k] = adv_trades.get(k, 0) + 1
            adv_volume[k] = adv_volume.get(k, Decimal(0)) + amount

        # Достоверность: двигался ли счётчик исполнения у этих объявлений?
        if (_real_volume_moved(store, pair.buy_ad.ad_id) == 0
                or _real_volume_moved(store, pair.sell_ad.ad_id) == 0):
            res.trades_on_untouched_ads += 1

        capital += pnl                      # реинвестирование
        res.trades.append(PaperTrade(
            decided_at=t, executed_at=exec_at, settled_at=settle_at,
            amount_kzt=amount, buy_price=buy_still.price,
            sell_price_expected=pair.sell_ad.price, sell_price_actual=sell_price,
            usdt=usdt, pnl_kzt=pnl, outcome=outcome,
            buy_ad_id=pair.buy_ad.ad_id, sell_ad_id=pair.sell_ad.ad_id,
            buy_adv=pair.buy_ad.advertiser.key,
            sell_adv=pair.sell_ad.advertiser.key))
        busy_until = settle_at

    res.final_capital_kzt = capital
    res.blacklisted = black.blocked()
    return res


def two_ledgers(store: Store, host: str, cfg: PaperConfig = PAPER,
                since: float | None = None,
                participation: Decimal = Decimal("45")) -> dict:
    """Два журнала на одних и тех же данных.

    «Реалистичный» — берём только `participation`% возможностей, потому что
    круглые сутки у экрана никто не сидит. «Эталон» — 100%: верхняя граница,
    показывающая, сколько стоит наше отсутствие, а не план действий.

    Оба считаются по одной истории и с одним размером круга, поэтому
    сравнимы напрямую.
    """
    from dataclasses import replace as _replace
    out = {}
    for name, pct in (("realistic", participation), ("ceiling", Decimal("100"))):
        out[name] = simulate(store, host,
                             cfg=_replace(cfg, participation_pct=pct),
                             since=since)
    out["participation_pct"] = participation
    return out
