"""Резервные копии базы: ротация файлов, раз в сутки и при старте.

Для SQLite используется штатный backup-API — снимок консистентен даже при
живых записях (WAL). Для PostgreSQL файловый бэкап не нужен: документированный
путь — pg_dump по крону вне контейнера (см. README «Безопасность»).
Копии пишутся рядом с базой в data/backups и ротируются до KEEP штук:
канон истории и кошельки игроков переживают смерть диска Render-контейнера,
пока копии забираются оттуда хотя бы раз в сутки.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

KEEP = 7


def sqlite_file_path() -> Path | None:
    """Путь к файлу БД, если это локальный SQLite; иначе None."""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return None
    raw = url.split("///", 1)[-1]
    raw = raw.split("?", 1)[0]
    return Path(raw)


def _backup_sync(source: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(str(source))
    try:
        dst_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _prune(directory: Path, keep: int) -> int:
    backups = sorted(directory.glob("backup-*.db"))
    removed = 0
    for stale in backups[:-keep] if len(backups) > keep else []:
        stale.unlink(missing_ok=True)
        removed += 1
    return removed


async def backup_now(keep: int = KEEP) -> Path | None:
    """Создаёт копию БД (только SQLite) и подрезает хвост. Путь или None."""
    source = sqlite_file_path()
    if source is None or not source.exists():
        return None
    directory = source.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    dest = directory / f"backup-{stamp}.db"
    import asyncio

    await asyncio.to_thread(_backup_sync, source, dest)
    pruned = await asyncio.to_thread(_prune, directory, keep)
    logger.info(
        "Бэкап БД готов: %s (%.1f КБ)%s",
        dest.name,
        dest.stat().st_size / 1024,
        f", удалено старых: {pruned}" if pruned else "",
    )
    return dest


async def backup_job() -> None:
    """Задача планировщика: молча пропускает не-SQLite конфигурации."""
    try:
        result = await backup_now()
        if result is None:
            logger.info("Файловый бэкап пропущен: БД не является SQLite (используй pg_dump).")
    except Exception:
        logger.exception("Бэкап БД не удался")
