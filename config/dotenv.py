"""
Минимальный загрузчик .env — без зависимостей.

Переменные окружения имеют приоритет над файлом: то, что уже задано
через export, .env НЕ перезатирает. Так отладочный запуск с временным
токеном не ломается о забытую строку в файле.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load(path: Path | str | None = None) -> dict[str, str]:
    """Прочитать .env в os.environ. Возвращает применённые ключи."""
    path = Path(path) if path else PROJECT_ROOT / ".env"
    if not path.exists():
        return {}

    applied: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or key in os.environ:
            continue            # уже задано через export — не трогаем
        os.environ[key] = value
        applied[key] = value
    return applied
