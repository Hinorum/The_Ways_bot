import logging
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import postgres_connect_args, settings
from app.models import Base

logger = logging.getLogger("way.db")


def _sqlite_connect_args() -> dict:
    """SQLite: ждём освобождения записи до 30 с — фоновые задачи (тизер,
    преген) и тик могут пересекаться в тестах и на слабых дисках."""
    return {"timeout": 30}


engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=(
        _sqlite_connect_args()
        if settings.async_database_url.startswith("sqlite")
        else postgres_connect_args(settings.database_url)
    ),
)

# Safety net: if a connection is returned to the pool with a failed
# transaction (InFailedSQLTransactionError), roll it back so the next
# session doesn't inherit the poison.
if settings.async_database_url.startswith("postgresql"):
    @event.listens_for(engine.sync_engine, "checkout")
    def _reset_on_checkout(dbapi_conn, connection_record, connection_proxy):
        try:
            dbapi_conn.rollback()
        except Exception:
            pass

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
        "season": "ALTER TABLE rounds ADD COLUMN season VARCHAR(7)",
        "place": "ALTER TABLE rounds ADD COLUMN place VARCHAR(80)",
        "sealed": "ALTER TABLE rounds ADD COLUMN sealed BOOLEAN NOT NULL DEFAULT 0",
        "weekly_nanotons": "ALTER TABLE rounds ADD COLUMN weekly_nanotons BIGINT NOT NULL DEFAULT 0",
        "money_mode": "ALTER TABLE rounds ADD COLUMN money_mode BOOLEAN NOT NULL DEFAULT 1",
    },
    "cards": {
        "tag": "ALTER TABLE cards ADD COLUMN tag VARCHAR(16) NOT NULL DEFAULT 'care'",
    },
    "players": {
        "wallet_address": "ALTER TABLE players ADD COLUMN wallet_address VARCHAR(80)",
        "wallet_linked_at": "ALTER TABLE players ADD COLUMN wallet_linked_at DATETIME",
        "calling": "ALTER TABLE players ADD COLUMN calling VARCHAR(32)",
        "inspiration": "ALTER TABLE players ADD COLUMN inspiration INTEGER NOT NULL DEFAULT 0",
        "wallet_verified": "ALTER TABLE players ADD COLUMN wallet_verified BOOLEAN NOT NULL DEFAULT 0",
        "wallet_verify_code": "ALTER TABLE players ADD COLUMN wallet_verify_code VARCHAR(16)",
        "wallet_verify_created": "ALTER TABLE players ADD COLUMN wallet_verify_created DATETIME",
        "dm_subscribed": "ALTER TABLE players ADD COLUMN dm_subscribed BOOLEAN NOT NULL DEFAULT 1",
    },
    "stakes": {
        "network": "ALTER TABLE stakes ADD COLUMN network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    },
    "payouts": {
        "network": "ALTER TABLE payouts ADD COLUMN network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
        "attempts": "ALTER TABLE payouts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "alerted": "ALTER TABLE payouts ADD COLUMN alerted BOOLEAN NOT NULL DEFAULT 0",
        "last_error": "ALTER TABLE payouts ADD COLUMN last_error VARCHAR(200)",
        "comment_override": "ALTER TABLE payouts ADD COLUMN comment_override VARCHAR(120)",
    },
    "incomes": {
        "network": "ALTER TABLE incomes ADD COLUMN network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    },
}


def _ensure_sqlite_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    for table, statements in _SQLITE_COLUMN_DDL.items():
        columns = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in statements.items():
            if name not in columns:
                sync_conn.execute(text(ddl))


_PG_MIGRATIONS: list[str] = [
    "ALTER TABLE rounds ALTER COLUMN rule_commitment TYPE VARCHAR(128)",
    "ALTER TABLE rounds ALTER COLUMN chapter_title TYPE VARCHAR(300)",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS cover_path VARCHAR(400) NOT NULL DEFAULT ''",
    "ALTER TABLE cards ADD COLUMN IF NOT EXISTS tag VARCHAR(16) NOT NULL DEFAULT 'care'",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS pot_nanotons BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS rake_nanotons BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS payouts_finalized BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS epilogue_text VARCHAR(700) NOT NULL DEFAULT ''",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS announced_at TIMESTAMPTZ",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS tie_note VARCHAR(200)",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS season VARCHAR(7)",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS place VARCHAR(80)",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS weekly_nanotons BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS sealed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS money_mode BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_address VARCHAR(80)",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_linked_at TIMESTAMPTZ",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS calling VARCHAR(32)",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS inspiration INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verified BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verify_code VARCHAR(16)",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verify_created TIMESTAMPTZ",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS dm_subscribed BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS current_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE players ADD COLUMN IF NOT EXISTS best_streak INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE stakes ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS alerted BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS last_error VARCHAR(200)",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS comment_override VARCHAR(120)",
    "ALTER TABLE incomes ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'",
    "ALTER TABLE payouts ALTER COLUMN player_id DROP NOT NULL",
    "ALTER TABLE payouts ALTER COLUMN round_id DROP NOT NULL",
    "UPDATE rounds SET status = lower(status) WHERE status = upper(status)",
    "UPDATE rounds SET win_rule = lower(win_rule) WHERE win_rule = upper(win_rule)",
    "ALTER TABLE ai_generated_pools ADD COLUMN IF NOT EXISTS is_ai_generated BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE npc_profiles ADD COLUMN IF NOT EXISTS is_ai_generated BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE prologue_beats ADD COLUMN IF NOT EXISTS is_ai_generated BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE season_arcs ADD COLUMN IF NOT EXISTS is_ai_generated BOOLEAN NOT NULL DEFAULT FALSE",
]

_WATCHER_TYPE_FIX = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'watcher_state'
      AND column_name = 'value'
      AND data_type <> 'text'
  ) THEN
    ALTER TABLE watcher_state ALTER COLUMN value TYPE TEXT;
  END IF;
END $$;
"""


async def _run_pg_migration_sql(sql: str) -> None:
    """Execute one DDL statement in its own transaction.

    If the statement fails the transaction is rolled back completely
    (no poison leaks to other connections in the pool).
    """
    async with engine.begin() as conn:
        await conn.execute(text(sql))


async def init_db() -> None:
    Path("data").mkdir(exist_ok=True)

    # Phase 1: schema + create_all in one transaction (fast, must succeed).
    async with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            await conn.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
            await conn.execute(text("SET search_path TO public"))
        await conn.run_sync(Base.metadata.create_all)

    if settings.async_database_url.startswith("postgresql"):
        # Phase 2: each migration in its own transaction.
        # If one fails the transaction is rolled back cleanly — no poison.
        for sql in _PG_MIGRATIONS:
            try:
                await _run_pg_migration_sql(sql)
            except Exception as exc:
                logger.warning("PG migration failed (non-fatal): %s — %s", sql[:80], exc)
        try:
            await _run_pg_migration_sql(_WATCHER_TYPE_FIX)
        except Exception as exc:
            logger.warning("PG watcher_state TYPE fix failed: %s", exc)

        # Phase 3: nuke the entire connection pool after init_db.
        # Any connections that may have been poisoned by failed migrations
        # are destroyed. Subsequent sessions get fresh connections.
        await engine.dispose()
    elif settings.async_database_url.startswith("sqlite"):
        async with engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(_ensure_sqlite_columns)
