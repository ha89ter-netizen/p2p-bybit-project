"""
Расписание отчётов по стенным часам.

Раньше отчёт уходил каждые N циклов опроса. Это уплывает: цикл идёт
20 секунд в норме и дольше при медленной сети, поэтому «раз в 3 часа»
постепенно превращалось в «раз в 3 часа с четвертью».

Здесь — привязка к местному времени. Плюс защита от повторной отправки:
факт отправки помечается в файле, поэтому перезапуск коллектора не
приводит к дублю, а пропущенный из-за простоя слот отправляется один раз
при следующем запуске.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:                                   # pragma: no cover
    ZoneInfo = None


def local_now(tz_name: str) -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:                             # noqa: BLE001
            pass
    return datetime.now(timezone.utc)


def last_slot(now: datetime, hours: tuple[int, ...]) -> datetime:
    """Ближайший наступивший момент отправки (не в будущем)."""
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0)
             for h in sorted(hours)]
    past = [t for t in today if t <= now]
    if past:
        return past[-1]
    prev = now - timedelta(days=1)
    return prev.replace(hour=max(hours), minute=0, second=0, microsecond=0)


def slot_key(slot: datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H")


class SentLog:
    """Какие слоты уже отправлены. Хранится файлом, а не в памяти, чтобы
    перезапуск коллектора не вызвал повторную отправку."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def is_due(self, key: str) -> bool:
        return self._read() != key

    def mark(self, key: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(key, encoding="utf-8")
        except OSError:
            pass          # не смогли записать — хуже дубль, чем молчание
