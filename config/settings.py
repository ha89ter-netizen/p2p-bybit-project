"""
Единственное место, где живут допущения.

Правило проекта: ни одно число, влияющее на экономику, не должно появляться
внутри формулы. Всё сюда, и у каждого — честная пометка уверенности.

Confidence:
    KNOWN     — прочитано из API или официальной документации, проверяемо
    ESTIMATED — выведено из наблюдений, имеет разброс
    UNKNOWN   — выдумано; требует измерения в реальном мире, а не в коде
"""

import os
from dataclasses import dataclass, field

# .env читается ДО объявления конфигов: значения по умолчанию в полях
# вычисляются в момент импорта модуля, позже подгружать бессмысленно.
from config.dotenv import load as _load_dotenv

_load_dotenv()
from decimal import Decimal
from typing import Literal

Confidence = Literal["KNOWN", "ESTIMATED", "UNKNOWN"]


@dataclass(frozen=True)
class Assumption:
    """Число + откуда оно взялось. Никаких голых констант в формулах."""
    value: Decimal
    confidence: Confidence
    source: str


# --------------------------------------------------------------------------
# Сбор данных
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Версия МЕТОДИКИ СБОРА. Меняется только когда меняется то, КАК получены
# наблюдения: интервал опроса, набор хостов, правило закрытия интервалов
# присутствия, разбор полей. Изменения в отчётах и анализе сюда не входят —
# они пересчитываются из тех же данных.
#
# Штамп пишется в каждую строку poll_run, чтобы прогоны, собранные разными
# способами, нельзя было молча смешать в одной выборке.
# --------------------------------------------------------------------------
METHOD_VERSION = "1.0.0"


@dataclass(frozen=True)
class CollectorConfig:
    # Недокументированный публичный эндпоинт. Авторизация не нужна.
    # Может измениться или закрыться в любой момент — поэтому изолирован
    # в collector/bybit_public.py и дублируется сырыми ответами.
    hosts: tuple[str, ...] = ("api2.bybit.com", "api2.bybit.kz")
    token: str = "USDT"
    currency: str = "KZT"

    # side=1 — объявления, у которых мы ПОКУПАЕМ USDT (наш ask)
    # side=0 — объявления, которым мы ПРОДАЁМ USDT (наш bid)
    sides: tuple[str, ...] = ("1", "0")

    poll_interval_sec: int = 20
    page_size: int = 50
    # ВАЖНО: должен покрывать книгу ЦЕЛИКОМ. Если обход упрётся в этот
    # предел, объявление, вытолкнутое за последнюю страницу, будет ложно
    # засчитано как исчезнувшее — и persistence-статистика станет мусором.
    # Поллер логирует упор в предел и НЕ закрывает интервалы в этом случае.
    max_pages: int = 14         # 700 объявлений; книга side=0 сейчас ~400

    http_timeout_sec: int = 25
    max_retries: int = 3
    # Обычный бэкофф для сетевых сбоев. На HTTP 429/403/503 клиент
    # переключается на THROTTLE_BACKOFF_SEC (60 с) — см. bybit_public.py:
    # долбить недокументированный эндпоинт с паузой в полторы секунды
    # это способ получить бан по IP на третьи сутки сбора.
    backoff_base_sec: float = 1.5
    min_gap_between_requests_sec: float = 0.25   # ~4 rps, лимит биржи 600/5s

    # Сырые ответы — страховка на случай смены формата поля.
    # Хранить всё нельзя (гигабайты), поэтому сэмплируем.
    raw_capture_every_n_cycles: int = 60         # при 20с = раз в 20 минут
    raw_retention_days: int = 30                 # ~9 МБ/сутки, потолок ~270 МБ

    db_path: str = "data/p2p.sqlite"


# --------------------------------------------------------------------------
# Суммы, на которых считаем исполнимость
# --------------------------------------------------------------------------

ANALYSIS_AMOUNTS_KZT: tuple[Decimal, ...] = (
    Decimal("200000"),
    Decimal("300000"),
    Decimal("400000"),
)

BASE_AMOUNT_KZT = Decimal("300000")
CAPITAL_KZT = Decimal("1000000")


# --------------------------------------------------------------------------
# Бумажная торговля (paper trading)
#
# Никаких реальных ордеров. Симулятор проходит по ЗАПИСАННОЙ истории и
# считает, что было бы, если бы мы входили в сделки по своим правилам.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PaperConfig:
    # Доля общего капитала, которой работаем.
    deploy_pct: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_DEPLOY_PCT", "30")))

    capital_kzt: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_CAPITAL_KZT", "1000000")))

    # Минимальный спред для входа, %.
    min_spread_pct: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_MIN_SPREAD", "0.5")))

    # Задержка между решением и исполнением первой ноги.
    # Перевод делает ЧЕЛОВЕК через приложение банка — 15 секунд это фантазия.
    execution_delay_sec: int = field(
        default_factory=lambda: int(os.getenv("PAPER_EXEC_DELAY", "120")))

    # На сколько капитал заперт в одном круге: перевод + ожидание релиза +
    # обратная нога. Замеренная медиана релиза мерчанта 41-80 с, p90 216-287 с,
    # плюс наши собственные действия.
    roundtrip_sec: int = field(
        default_factory=lambda: int(os.getenv("PAPER_ROUNDTRIP", "600")))

    # Пороги качества контрагента — те же, что в дайджесте.
    min_recent_orders: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MIN_ORDERS", "200")))
    min_execute_rate: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MIN_RATE", "97")))
    min_ad_finish_num: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MIN_AD_FINISH", "50")))

    # --- наши собственные показатели, чтобы проверять требования К НАМ ---
    # По мере роста истории эти числа надо поднимать: откроется больше
    # объявлений, которые сейчас нас не пускают.
    my_orders_30d: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MY_ORDERS", "0")))
    my_rate_30d: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MY_RATE", "0")))

    # --- фильтры исполнимости (шаги 2-5) ---
    screen_requirements: bool = True     # проходим ли мы требования объявления
    screen_conflicts: bool = True        # взаимно несовместимые условия ног
    recent_volume_sec: int = field(
        default_factory=lambda: int(os.getenv("PAPER_RECENT_VOLUME_SEC", "3600")))
    max_release_sec: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MAX_RELEASE_SEC", "300")))
    max_price_deviation_pct: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_MAX_DEVIATION", "8")))
    blacklist_after: int = field(
        default_factory=lambda: int(os.getenv("PAPER_BLACKLIST_AFTER", "20")))

    # --- лимит на одного контрагента ---
    #
    # Симулятор «съедает» ликвидность объявления, но сам контрагент никак
    # не реагирует. Замерено: 69% бумажной прибыли давали ДВА человека,
    # 37% — один, и у него забирали по 300 000 девять раз подряд. В жизни
    # он переставит цену или откажет: систематически невыгодная сделка
    # с одним и тем же партнёром не повторяется девять раз.
    #
    # Ноль отключает лимит и возвращает прежнее поведение.
    max_trades_per_advertiser: int = field(
        default_factory=lambda: int(os.getenv("PAPER_MAX_PER_ADV", "3")))
    max_volume_per_advertiser_kzt: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_MAX_VOL_ADV", "1000000")))

    # --- потолок правдоподобия ---
    #
    # Розничный валютный рынок не даёт 10% в сутки. Если симуляция такое
    # показывает, вероятнее ошибка модели, чем найденная неэффективность.
    # Превышение не исправляет расчёт, а помечает его как подозрительный.
    plausible_daily_return_pct: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_PLAUSIBLE_DAILY", "1.0")))

    # --- участие: мы не сидим у экрана круглые сутки ---
    #
    # Доля возможностей, которые мы РЕАЛЬНО успеваем взять. 100% — верхняя
    # граница, недостижимая для человека; она нужна как эталон, а не как
    # план. Отбор детерминированный (сид ниже), поэтому один и тот же
    # набор данных всегда даёт один и тот же результат.
    #
    # ВАЖНО про интерпретацию: случайные 45% — НЕ то же самое, что «сплю
    # ночью». Сон это непрерывный кусок суток, и если лучшие спреды
    # приходятся на те часы, случайная выборка даст завышенный результат.
    # Пока распределение по часам не измерено, это верхняя оценка.
    participation_pct: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("PAPER_PARTICIPATION", "100")))
    participation_seed: int = field(
        default_factory=lambda: int(os.getenv("PAPER_SEED", "20260908")))

    # Реинвестирование: размер сделки пересчитывается от ТЕКУЩЕГО капитала,
    # а не от стартового. Заработали 10 000 — следующая сделка больше.
    compound: bool = field(
        default_factory=lambda: os.getenv("PAPER_COMPOUND", "true").lower() == "true")

    # Входить только в объявления, у которых за время наблюдения реально
    # двигался executedQuantity, то есть кто-то их брал.
    #
    # Без этого фильтра 72% бумажной прибыли приходит с объявлений, которые
    # простояли сутки нетронутыми. Объявление, висящее с выгодной ценой и
    # ненулевым остатком, которое никто не берёт, — это не возможность,
    # а свидетельство, что взять его нельзя по причинам, невидимым в API.
    require_ad_shows_volume: bool = field(
        default_factory=lambda: os.getenv("PAPER_REQUIRE_VOLUME", "true").lower()
        == "true")

    @property
    def working_capital_kzt(self) -> Decimal:
        return self.capital_kzt * self.deploy_pct / 100


# --------------------------------------------------------------------------
# Страты качества контрагента
#
# ВАЖНО: эти метрики измеряют РОВНО ОДНО — отдаст ли контрагент USDT.
# Они ничего не говорят о происхождении KZT, которые он пришлёт.
# Фильтрация по ним отделяет counterparty risk premium и НЕ отделяет
# banking/AML premium. См. review, п.2.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityStratum:
    """Три РАЗНЫХ измерения, которые легко перепутать:

      min_ad_finish_num  — сколько сделок прошло через ЭТО ОБЪЯВЛЕНИЕ
                           (зрелость объявления, а не репутация человека)
      min_recent_orders  — оборот КОНТРАГЕНТА за 30 дней
      min_execute_rate   — доля успешных сделок КОНТРАГЕНТА за 30 дней
    """
    name: str
    min_ad_finish_num: int
    min_recent_orders: int
    min_execute_rate: int


QUALITY_STRATA: tuple[QualityStratum, ...] = (
    QualityStratum("any",     min_ad_finish_num=0,   min_recent_orders=0,    min_execute_rate=0),
    QualityStratum("basic",   min_ad_finish_num=10,  min_recent_orders=50,   min_execute_rate=90),
    QualityStratum("good",    min_ad_finish_num=50,  min_recent_orders=200,  min_execute_rate=97),
    QualityStratum("premium", min_ad_finish_num=200, min_recent_orders=1000, min_execute_rate=98),
)


# Бакеты спреда для сегментации (нижняя граница включительно, в процентах)
SPREAD_BUCKETS_PCT: tuple[tuple[Decimal, Decimal | None], ...] = (
    (Decimal("0.0"), Decimal("0.5")),
    (Decimal("0.5"), Decimal("1.0")),
    (Decimal("1.0"), Decimal("1.5")),
    (Decimal("1.5"), Decimal("2.0")),
    (Decimal("2.0"), Decimal("3.0")),
    (Decimal("3.0"), None),
)


# --------------------------------------------------------------------------
# Экономика
#
# Здесь сознательно НЕТ поля "estimated_friction_bps". Единый коэффициент
# трения — это фальшивая точность: он смешивает измеримое (комиссия банка)
# с неизмеримым (вероятность нерелиза, вероятность блокировки счёта).
# Вместо него — раздельные параметры, и те, что помечены UNKNOWN,
# в отчётах выводятся как ДИАПАЗОН, а не как число.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class EconomicsConfig:
    # Bybit не берёт комиссию с тейкера на P2P.
    # symbolInfo.buyFeeRate / sellFeeRate приходят пустыми — подтверждено замером.
    exchange_fee_taker: Assumption = field(default_factory=lambda: Assumption(
        Decimal("0"), "KNOWN",
        "symbolInfo.buyFeeRate/sellFeeRate пусты; тейкер P2P не платит комиссию"))

    # Межбанковская комиссия KZT. Зависит от пары банков и от того, кто её
    # платит — часть объявлений явно перекладывает её на покупателя
    # (видно в remark). Ноль по умолчанию: пусть отчёт покажет
    # чувствительность, а не прячет догадку в результат.
    bank_transfer_fee_kzt: Assumption = field(default_factory=lambda: Assumption(
        Decimal("0"), "UNKNOWN",
        "зависит от пары банков; часть объявлений перекладывает на покупателя"))

    # Дрейф курса USDT/KZT за время round-trip. Измеряется из наших же
    # снапшотов (медиана книги), не выдумывается. До первого замера — ноль.
    fx_drift_bps_per_minute: Assumption = field(default_factory=lambda: Assumption(
        Decimal("0"), "UNKNOWN",
        "подлежит измерению из истории медианы книги; см. analysis/economics.py"))

    # Вероятность, что нога дойдёт до релиза.
    # recentExecuteRate — статистика рекламодателя со ВСЕМИ контрагентами,
    # а не оценка нашей вероятности. Использовать её как P_release —
    # методологическая ошибка. Меряется реальными сделками, не скрейпингом.
    p_leg_completes: Assumption = field(default_factory=lambda: Assumption(
        Decimal("1"), "UNKNOWN",
        "НЕ выводится из recentExecuteRate; требует реальных сделок"))

    # Вероятность блокировки банковского счёта на сделку.
    # Это не издержка в bps, а ruin-барьер: реализация означает потерю
    # доступа к основному счёту. Моделируется отдельно, не вычитается.
    p_bank_freeze_per_trade: Assumption = field(default_factory=lambda: Assumption(
        Decimal("0"), "UNKNOWN",
        "ruin-риск, не издержка; не наблюдаем в API ни в каком виде"))


# --------------------------------------------------------------------------
# Матчинг
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MatchingConfig:
    # Пара из одного и того же рекламодателя — не возможность.
    # На bybit.kz это отсекает вообще всё, и это правильный результат.
    forbid_same_advertiser: bool = True

    # Требовать хотя бы один общий метод оплаты у обеих ног.
    require_common_payment: bool = True

    # Методы оплаты, которыми РЕАЛЬНО располагаем.
    #
    # Без этого пересечение считалось только между двумя объявлениями:
    # пара на банке, счёта в котором у нас нет, проходила как исполнимая
    # и попадала в статистику страты. Прошлые цифры по good этим завышены —
    # насколько, покажет только пересбор.
    #
    # 150 Kaspi Bank, 203 Halyk Bank, 549 Freedom Bank.
    # Пустое значение MY_PAYMENTS отключает проверку (прежнее поведение).
    my_payments: frozenset[str] = field(
        default_factory=lambda: frozenset(
            x.strip() for x in os.getenv("MY_PAYMENTS", "150,203,549").split(",")
            if x.strip()))

    # Данные старше этого — не используем.
    max_data_age_sec: int = 90

    # Порог отсечки при построении отчёта (в процентах).
    # НЕ фильтр стратегии — просто чтобы не тащить в БД шум.
    min_gross_spread_pct: Decimal = Decimal("-5.0")


# --------------------------------------------------------------------------
# Инвестиционный слой: издержки покупки и эталон сравнения
# --------------------------------------------------------------------------

#: минимальная комиссия брокера за сделку, USD
BROKER_COMMISSION_USD = Decimal(os.getenv("INV_COMMISSION_USD", "1.00"))

#: какую долю покупки комиссии позволено съесть; отсюда считается порог
BROKER_MAX_FEE_SHARE = Decimal(os.getenv("INV_MAX_FEE_SHARE", "0.01"))

#: спред на конвертации ₸→USD, процентов; часто дороже самой комиссии
FX_SPREAD_PCT = Decimal(os.getenv("INV_FX_SPREAD_PCT", "0.5"))

# Ставка тенгового вклада — ЭТАЛОН, с которым сравнивается портфель.
# Намеренно пустая по умолчанию: ставка меняется вслед за Нацбанком, и
# подставлять число из памяти значило бы выдумывать. Пока не задана,
# эталон честно сообщает, что сравнивать не с чем.
KZT_DEPOSIT_RATE_PCT = (Decimal(os.getenv("INV_KZT_DEPOSIT_PCT"))
                        if os.getenv("INV_KZT_DEPOSIT_PCT") else None)


# --------------------------------------------------------------------------
# Telegram: только ОТПРАВКА дайджеста. Ни одной команды, способной
# инициировать финансовую операцию, здесь нет и быть не должно.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TelegramConfig:
    # ВНИМАНИЕ: default_factory, а НЕ os.getenv(...) прямо в поле.
    # Значение по умолчанию в теле dataclass вычисляется один раз при
    # импорте модуля, поэтому TelegramConfig() навсегда запоминал бы
    # окружение на момент первого import — и переменные, заданные позже,
    # молча игнорировались бы.
    #
    # Секреты только из окружения. В репозиторий не попадают никогда.
    bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Часы по местному времени, когда отправлять отчёт.
    # Привязка к стенным часам, а не к счётчику циклов: при замедлении
    # обходов расписание не уплывает.
    digest_at_hours: tuple[int, ...] = field(
        default_factory=lambda: tuple(sorted({
            int(x) % 24 for x in os.getenv("TELEGRAM_DIGEST_AT", "8,20").split(",")
            if x.strip()})) or (8, 20))

    # Окно, за которое считается таблица спредов. По умолчанию равно
    # промежутку между отчётами, чтобы соседние отчёты не пересекались.
    digest_hours: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_DIGEST_HOURS", "12")))
    timezone: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_TZ", "Asia/Almaty"))

    # Сумма, на которой считается дайджест
    amount_kzt: Decimal = field(
        default_factory=lambda: Decimal(os.getenv("TELEGRAM_AMOUNT_KZT", "300000")))

    # Сколько строк показывать в каждом бакете
    rows_per_bucket: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_ROWS", "5")))

    # Пары ниже этой планки качества в таблицу не попадают, но считаются
    # в строке «отфильтровано». Показывать спред 4% от контрагента с
    # четырьмя сделками за месяц как возможность — значит вводить в
    # заблуждение себя же.
    min_recent_orders: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_MIN_ORDERS", "200")))
    min_execute_rate: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_MIN_RATE", "97")))
    min_ad_finish_num: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_MIN_AD_FINISH", "50")))

    http_timeout_sec: int = 20

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


# Бакеты дайджеста (нижняя граница включительно, %).
# Нижняя граница первого бакета — ноль: пара с отрицательным спредом это
# не «маленькая возможность», а убыток. Такие считаются отдельной строкой,
# чтобы ничего не пряталось, но в таблицу не попадают.
DIGEST_BUCKETS: tuple[tuple[str, Decimal, Decimal | None], ...] = (
    ("<1%",   Decimal("0"), Decimal("1")),
    ("1-3%",  Decimal("1"), Decimal("3")),
    ("3%+",   Decimal("3"), None),
)


COLLECTOR = CollectorConfig()
ECONOMICS = EconomicsConfig()
MATCHING = MatchingConfig()
TELEGRAM = TelegramConfig()
PAPER = PaperConfig()
