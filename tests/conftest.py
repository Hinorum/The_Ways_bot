import os
import tempfile

# Отдельная БД для тестов хендлеров: переменная окружения сильнее .env,
# задаём ДО первых импортов app.*. Файл пересоздаём при каждом прогоне,
# чтобы схема всегда соответствовала текущим моделям.
_DB_PATH = os.path.join(tempfile.gettempdir(), "the_ways_handlers_test.db")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + _DB_PATH)
for _suffix in ("", "-journal", "-wal", "-shm"):
    try:
        os.remove(_DB_PATH + _suffix)
    except FileNotFoundError:
        pass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Base


@pytest.fixture(scope="session", autouse=True)
async def _global_db_schema():
    """Глобальная БД (SessionLocal) пересоздаётся по текущим моделям каждый прогон."""
    from app.db import init_db

    await init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Рейт-лимиты хендлеров не должны перетекать между тестами."""
    from app import handlers

    handlers._WALLET_LAST.clear()
    yield
    handlers._WALLET_LAST.clear()


@pytest.fixture(scope="module", autouse=True)
async def _clean_global_db_per_module():
    """Каждый тестовый модуль стартует с пустой глобальной БД.

    Тесты не должны зависеть от порядка запуска файлов: любые сиды,
    оставшиеся в SessionLocal от предыдущего модуля (чаты, игроки,
    состояния watcher'а), затираются до первого теста модуля.
    """
    from sqlalchemy import delete

    from app.db import SessionLocal

    async with SessionLocal() as db:
        for table in reversed(Base.metadata.sorted_tables):
            await db.execute(delete(table))
        await db.commit()
    yield


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        yield session
    await engine.dispose()
