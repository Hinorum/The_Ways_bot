from app.config import postgres_connect_args, sqlalchemy_url


def test_render_postgres_url() -> None:
    assert sqlalchemy_url("postgres://u:p@host/db").startswith("postgresql+asyncpg://")
    assert "postgresql+asyncpg://" in sqlalchemy_url("postgresql://u:p@host/db")


def test_strips_libpq_sslmode_for_asyncpg() -> None:
    converted = sqlalchemy_url("postgres://u:p@ep-x.neon.tech/db?sslmode=require")
    assert "sslmode" not in converted
    assert converted.startswith("postgresql+asyncpg://")
    assert postgres_connect_args("postgres://u:p@ep-x.neon.tech/db?sslmode=require") == {
        "ssl": True,
        "server_settings": {"search_path": "public"},
    }


def test_local_postgres_skips_ssl() -> None:
    assert postgres_connect_args("postgresql://u:p@localhost:5432/db") == {
        "server_settings": {"search_path": "public"},
    }
