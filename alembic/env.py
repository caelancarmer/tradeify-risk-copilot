"""
alembic/env.py -- migration environment for the payout automation tables.

Reads POSTGRES_URL from the environment (falls back to the compose value).
Migrations are the ONLY supported way to create the payout schema; production
code must not call Base.metadata.create_all().
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `src/` importable when alembic runs from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from payout_models import Base  # noqa: E402  (import after sys.path fix)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Database URL: environment wins, then the ini placeholder.
config.set_main_option(
    "sqlalchemy.url",
    os.environ.get(
        "POSTGRES_URL",
        config.get_main_option("sqlalchemy.url")
        or "postgresql://copilot:copilot@postgres:5432/copilot",
    ),
)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (`alembic upgrade --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()