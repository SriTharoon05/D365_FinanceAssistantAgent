import asyncio
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import Settings
from app.models.entities import Base

config = context.config
if config.get_main_option("sqlalchemy.url") == "sqlite+aiosqlite:///./data/finance_assistant.db":
    settings = Settings()
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
url = config.get_main_option("sqlalchemy.url")
from sqlalchemy.engine import make_url

parsed = make_url(url)
if parsed.get_backend_name() == "sqlite" and parsed.database and parsed.database != ":memory:":
    Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
target_metadata = Base.metadata


def migrations(connection):
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    async with engine.connect() as connection:
        await connection.run_sync(migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
