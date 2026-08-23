from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import postgres_connect_args, settings
from app.models import Base


engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=postgres_connect_args(settings.database_url),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

_SQLITE_COLUMN_DDL = {
    "rounds": {
        "cover_path": "ALTER TABLE rounds ADD COLUMN cover_path VARCHAR(400) NOT NULL DEFAULT ''",
        "pot_nanotons": "ALTER TABLE rounds ADD COLUMN pot_nanotons BIGINT NOT NULL DEFAULT 0",
        "rake_nanotons": "ALTER TABLE rounds ADD COLUMN rake_nanotons BIGINT NOT NULL DEFAULT 0",
        "payouts_finalized": "ALTER TABLE rounds ADD COLUMN payouts_finalized BOOLEAN NOT NULL DEFAULT 0",
        "epilogue_text": "ALTER TABLE rounds ADD COLUMN epilogue_text VARCHAR(700) NOT NULL DEFAULT ''",
        "announced_at": "ALTER TABLE rounds ADD COLUMN announced_at DATETIME",
        "tie_note": "ALTER TABLE rounds ADD COLUMN tie_note VARCHAR(200)",
    },
    "cards": {
        "tag": "ALTER TABLE cards ADD COLUMN tag VARCHAR(16) NOT NULL DEFAULT 'care'",
    },
    "players": {
        "wallet_address": "ALTER TABLE players ADD COLUMN wallet_address VARCHAR(80)",
        "wallet_linked_at": "ALTER TABLE players ADD COLUMN wallet_linked_at DATETIME",
    },
    "stakes": {
        "network": "ALTER TABLE stakes ADD COLUMN network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    },
    "payouts": {
        "network": "ALTER TABLE payouts ADD COLUMN network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
        "attempts": "ALTER TABLE payouts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "alerted": "ALTER TABLE payouts ADD COLUMN alerted BOOLEAN NOT NULL DEFAULT 0",
    },
}


def _ensure_sqlite_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    for table, statements in _SQLITE_COLUMN_DDL.items():
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in statements.items():
            if name not in columns:
                sync_conn.execute(text(ddl))


async def init_db() -> None:
    Path("data").mkdir(exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            await conn.execute(text("ALTER TABLE rounds ALTER COLUMN rule_commitment TYPE VARCHAR(128)"))
            await conn.execute(text("ALTER TABLE rounds ALTER COLUMN chapter_title TYPE VARCHAR(300)"))
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS cover_path VARCHAR(400) NOT NULL DEFAULT ''"
            ))
            await conn.execute(text(
                "ALTER TABLE cards ADD COLUMN IF NOT EXISTS tag VARCHAR(16) NOT NULL DEFAULT 'care'"
            ))
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS pot_nanotons BIGINT NOT NULL DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS rake_nanotons BIGINT NOT NULL DEFAULT 0"
            ))
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS payouts_finalized BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS epilogue_text VARCHAR(700) NOT NULL DEFAULT ''"
            ))
            # Метка первого анонса дня: защита от дублей при деплое.
            await conn.execute(text("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ"))
            # Объяснение ничьей при выборе победившего пути.
            await conn.execute(text("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS tie_note VARCHAR(200)"))
            await conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_address VARCHAR(80)"))
            await conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_linked_at TIMESTAMPTZ"))
            await conn.execute(text(
                "ALTER TABLE stakes ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'"
            ))
            await conn.execute(text(
                "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'"
            ))
            # Счётчик попыток отправки: failed-выплаты ретраятся до лимита.
            await conn.execute(text(
                "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
            ))
            # Дедуп алертов о выплатах в БД: безопасен при рестартах и репликах.
            await conn.execute(text(
                "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS alerted BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            # Доли казны (рейк/копилка месяца) уходят без игрока.
            await conn.execute(text("ALTER TABLE payouts ALTER COLUMN player_id DROP NOT NULL"))
            # Авто-возвраты «ничейных» переводов не привязаны к раунду.
            await conn.execute(text("ALTER TABLE payouts ALTER COLUMN round_id DROP NOT NULL"))
            await conn.execute(text("UPDATE rounds SET status = lower(status) WHERE status = upper(status)"))
            await conn.execute(text("UPDATE rounds SET win_rule = lower(win_rule) WHERE win_rule = upper(win_rule)"))
        elif conn.dialect.name == "sqlite":
            await conn.run_sync(_ensure_sqlite_columns)
