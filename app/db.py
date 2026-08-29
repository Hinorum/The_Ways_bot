from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import postgres_connect_args, settings
from app.models import Base


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
            # Сезон мира и место действия: память географии между днями.
            await conn.execute(text("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS season VARCHAR(7)"))
            await conn.execute(text("ALTER TABLE rounds ADD COLUMN IF NOT EXISTS place VARCHAR(80)"))
            # Доля дня, ушедшая в копилку недели (для поста итогов).
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS weekly_nanotons BIGINT NOT NULL DEFAULT 0"
            ))
            # Глухой день: закон запечатан до итогов.
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS sealed BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            # «Денежный режим» дня: ставки живут только в помеченных днях
            # (/panel переключает), снимок дня — Round.money_mode.
            await conn.execute(text(
                "ALTER TABLE rounds ADD COLUMN IF NOT EXISTS money_mode BOOLEAN NOT NULL DEFAULT TRUE"
            ))
            await conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_address VARCHAR(80)"))
            await conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_linked_at TIMESTAMPTZ"))
            # Призвание собаки и жетоны «Второго нюха» (Правила Стаи).
            await conn.execute(text("ALTER TABLE players ADD COLUMN IF NOT EXISTS calling VARCHAR(32)"))
            await conn.execute(text(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS inspiration INTEGER NOT NULL DEFAULT 0"
            ))
            # Подтверждение владения кошельком (мемо bv:<код>): привязка чужого
            # адреса перестаёт быть ресурсом для кражи призов.
            await conn.execute(text(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verified BOOLEAN NOT NULL DEFAULT FALSE"
            ))
            await conn.execute(text(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verify_code VARCHAR(16)"
            ))
            await conn.execute(text(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS wallet_verify_created TIMESTAMPTZ"
            ))
            # Личные дубликаты рассылок: подписанные игроки получают итоги и
            # анонсы в личку (/start тумблером). (Старым игрокам — да.)
            await conn.execute(text(
                "ALTER TABLE players ADD COLUMN IF NOT EXISTS dm_subscribed BOOLEAN NOT NULL DEFAULT TRUE"
            ))
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
            # Причина последней неудачи отправки: /payouts и алерты показывают её сами.
            await conn.execute(text(
                "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS last_error VARCHAR(200)"
            ))
            # Свободный комментарий перевода (возвраты при паузе игры).
            await conn.execute(text(
                "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS comment_override VARCHAR(120)"
            ))
            # Метка сети в ledger доходов: сверка казны не смешивает контуры
            # TON (mainnet/testnet). Старые строки по умолчанию — mainnet.
            await conn.execute(text(
                "ALTER TABLE incomes ADD COLUMN IF NOT EXISTS network VARCHAR(16) NOT NULL DEFAULT 'mainnet'"
            ))
            # План Хозяина Ошибки не влезал в VARCHAR(255): ронял тик до
            # создания следующего дня. Расширяем до TEXT, но только когда это
            # действительно нужно: безусловный ALTER при каждом старте
            # инвалидирует prepared-statement кэши asyncpg и будит ошибки
            # «cached statement plan is invalid» на живых пулах соединений.
            await conn.execute(text(
                """
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
            ))
            # Доли казны (рейк/копилка месяца) уходят без игрока.
            await conn.execute(text("ALTER TABLE payouts ALTER COLUMN player_id DROP NOT NULL"))
            # Авто-возвраты «ничейных» переводов не привязаны к раунду.
            await conn.execute(text("ALTER TABLE payouts ALTER COLUMN round_id DROP NOT NULL"))
            await conn.execute(text("UPDATE rounds SET status = lower(status) WHERE status = upper(status)"))
            await conn.execute(text("UPDATE rounds SET win_rule = lower(win_rule) WHERE win_rule = upper(win_rule)"))
        elif conn.dialect.name == "sqlite":
            # WAL: читатели не блокируют писателя и наоборот — фоновые задачи
            # (диспетчер выплат, преген) перестают ронять тик «database is locked».
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(_ensure_sqlite_columns)
