"""Фикстуры — реальные ответы api2.bybit.com, снятые 28.08.2026.
Не выдуманные: структура полей должна совпадать с боевой."""

from decimal import Decimal
from domain.models import Ad

BASE_ITEM = {
    "id": "0", "nickName": "x", "userMaskId": "", "tokenId": "USDT",
    "currencyId": "KZT", "side": 1, "price": "480.00", "quantity": "1000",
    "lastQuantity": "1000", "executedQuantity": "0", "frozenQuantity": "0",
    "minAmount": "100000.00", "maxAmount": "1000000.00",
    "payments": ["150", "203"], "finishNum": 500, "orderNum": 600,
    "recentOrderNum": 100, "recentExecuteRate": 99, "latestPayTime": "60000",
    "latestReleaseTime": "45000", "isOnline": True, "userType": "PERSONAL",
    "authTag": ["VA"], "remark": "", "version": 1, "status": 10,
    "tradingPreferenceSet": {
        "isKyc": 1, "orderFinishNumberDay30": 0, "completeRateDay30": "",
        "hasNationalLimit": 0, "hasSingleUserOrderLimit": 0,
    },
}


def make_ad(ad_id="1", side="1", price="480.00", nick="a", **over) -> Ad:
    """Объявление для тестов.

    lastQuantity по умолчанию равен quantity (ничего не исполнено) — так же,
    как в живых данных у свежего объявления. Чтобы смоделировать частично
    выкупленное объявление, задайте lastQuantity явно.
    """
    item = dict(BASE_ITEM)
    item.update({"id": ad_id, "side": int(side), "price": price, "nickName": nick})
    prefs = dict(BASE_ITEM["tradingPreferenceSet"])
    prefs.update(over.pop("prefs", {}))
    item["tradingPreferenceSet"] = prefs
    item.update(over)
    if "lastQuantity" not in over:
        item["lastQuantity"] = item["quantity"]
    return Ad.from_api(item, host="api2.bybit.com", observed_at=1000.0)
