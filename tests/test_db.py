"""Симметрия create_all ↔ alembic.

Бутстрап схемы идёт через create_all (init_db) и таблицы совпадают с
моделями, но alembic_version при этом не создавалась: ручной
`alembic upgrade head` на такой базе упёрся бы в «table already exists».
init_db теперь ставит якорь версии на head — повторный upgrade становится
честным no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import SessionLocal, _alembic_head, init_db

_REPO_ROOT = Path(__file__).resolve().parents[1]


async def test_init_db_stamps_alembic_version_at_head() -> None:
    head = _alembic_head()
    assert head, "в migrations/versions должны быть ревизии"

    await init_db()

    async with SessionLocal() as session:
        row = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert row == head


def test_upgrade_head_is_noop_after_bootstrap() -> None:
    """`alembic upgrade head` на базе, бутстрапнутой через create_all со штампом,
    отрабатывает без «table already exists»."""
    db_url = os.environ.get("DATABASE_URL")
    assert db_url and db_url.startswith("sqlite"), "conftest задал DATABASE_URL"
    env = dict(os.environ) | {"DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "already exists" not in proc.stdout.lower() + proc.stderr.lower()