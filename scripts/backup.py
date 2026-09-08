"""Неизменяемый снимок базы.

Августовский массив был утрачен целиком вместе с папкой проекта: рабочая БД
существовала в одном экземпляре и не копировалась. Поэтому здесь два правила.

1. Снимок кладётся ВНЕ каталога проекта. Копия внутри той же папки не
   защищает от сценария, который уже случился, — удаления папки.
2. Снимок неизменяем: существующий файл не перезаписывается никогда.
   Порча рабочей БД не должна распространяться на историю копий.

Снимок делается через VACUUM INTO: он берёт согласованное состояние, не
останавливая сборщик и не мешая его записи.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DIR = Path(os.path.expanduser(
    os.getenv("P2P_BACKUP_DIR", "~/Documents/p2p_backups")))

# Offsite. Вторая папка на том же диске спасает от удаления каталога, но не
# от смерти SSD и не от потери ноутбука — а именно так теряются данные
# целиком. iCloud Drive синхронизируется вне машины, поэтому снимок уезжает
# и туда. Пустое значение отключает offsite (например на чужой машине).
def _offsite_from_env() -> Path | None:
    """Пустая переменная = offsite отключён. Path("") даёт ".", то есть
    текущий каталог, поэтому пустоту надо ловить до превращения в Path."""
    raw = os.getenv("P2P_OFFSITE_DIR",
                    "~/Library/Mobile Documents/com~apple~CloudDocs/p2p_backups")
    raw = raw.strip()
    return Path(os.path.expanduser(raw)) if raw else None


OFFSITE_DIR = _offsite_from_env()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(db_path: str | Path, out_dir: Path = DEFAULT_DIR,
             stamp: str | None = None,
             offsite: Path | None = None) -> Path | None:
    """Сделать снимок. Возвращает путь или None, если за этот день он уже есть."""
    db_path = Path(db_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or time.strftime("%Y%m%d", time.localtime())

    final = out_dir / f"p2p-{stamp}.sqlite.gz"
    if final.exists():
        return None                      # неизменяемость: не трогаем готовое

    raw = out_dir / f"p2p-{stamp}.sqlite.tmp"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(raw),))
    finally:
        conn.close()

    digest = _sha256(raw)
    with raw.open("rb") as src, gzip.open(final, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
    raw.unlink()

    (out_dir / "SHA256SUMS").open("a", encoding="utf-8").write(
        f"{digest}  {final.name}  (несжатый снимок {stamp})\n")

    # снимок только на чтение: случайная перезапись должна упереться в права
    final.chmod(0o444)
    try:
        copy_offsite(final, digest, offsite=offsite)
    except Exception as exc:                      # noqa: BLE001
        # Локальный снимок уже на диске и валиден. Потерять его из-за
        # недоступного облака было бы ровно тем сценарием, от которого
        # мы защищаемся.
        print(f"offsite не удался ({exc}); локальный снимок сохранён")
    return final


def copy_offsite(snap: Path, digest: str,
                 offsite: Path | None = None) -> Path | None:
    """Отправить копию вне машины. Сбой offsite не отменяет локальный снимок."""
    dst_dir = offsite if offsite is not None else OFFSITE_DIR
    if dst_dir is None:
        return None
    dst = dst_dir / snap.name
    if dst.exists():
        return dst
    dst_dir.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copyfile(snap, tmp)
    if digest_of_gz(tmp) != digest:
        tmp.unlink(missing_ok=True)
        raise OSError(f"offsite: контрольная сумма не сошлась для {snap.name}")
    tmp.replace(dst)
    (dst_dir / "SHA256SUMS").open("a", encoding="utf-8").write(
        f"{digest}  {snap.name}  (несжатый снимок)\n")
    return dst


def digest_of_gz(path: Path) -> str:
    """Хеш РАСПАКОВАННОГО содержимого — сравнивать надо данные, а не упаковку."""
    h = hashlib.sha256()
    with gzip.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def missing_days(store_path: str | Path, out_dir: Path = DEFAULT_DIR) -> list[str]:
    """Дни, за которые в базе есть наблюдения, а снимка нет.

    Нужна, потому что «снимок за сегодня уже есть» однажды прозвучало при
    отсутствующем файле, и объяснить это из журнала не удалось. Проверять
    надо фактическое наличие файлов, а не доверять одной ветке кода.
    """
    import sqlite3
    conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        days = {time.strftime("%Y%m%d", time.localtime(t)) for (t,) in
                conn.execute("SELECT DISTINCT CAST(started_at AS INTEGER)"
                             " FROM poll_run")}
    finally:
        conn.close()
    have = {p.name[4:12] for p in out_dir.glob("p2p-*.sqlite.gz")} \
        if out_dir.exists() else set()
    return sorted(days - have)


def main() -> int:
    from config.settings import COLLECTOR
    gaps_before = missing_days(COLLECTOR.db_path)
    out = snapshot(COLLECTOR.db_path)
    if out is None:
        stamp = time.strftime("%Y%m%d", time.localtime())
        print(f"снимок за сегодня уже есть: "
              f"{DEFAULT_DIR / f'p2p-{stamp}.sqlite.gz'}")
        gaps = missing_days(COLLECTOR.db_path)
        if gaps:
            print(f"ПРОПУЩЕННЫЕ ДНИ: {', '.join(gaps)}")
        return 1 if gaps else 0
    print(f"снимок: {out}  ({out.stat().st_size / 1_048_576:.1f} МБ)")
    gaps = missing_days(COLLECTOR.db_path)
    if gaps:
        print(f"ПРОПУЩЕННЫЕ ДНИ (снимка нет, наблюдения есть): {', '.join(gaps)}")
    elif gaps_before:
        print(f"закрыты пропуски: {', '.join(gaps_before)}")
    if OFFSITE_DIR is None:
        print("offsite: отключён (P2P_OFFSITE_DIR пуст)")
    else:
        off = OFFSITE_DIR / out.name
        print(f"offsite: {'да' if off.exists() else 'НЕТ'}  {off}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
