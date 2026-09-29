"""Postgres plumbing shared by component adapters: pool creation and the migration runner."""

from pathlib import Path
from typing import Final

import asyncpg

from adapter_verify.common.domain.migrations import Migration, parse, plan

# Arbitrary constant key for the migration advisory lock; only one runner at a time.
_MIGRATION_LOCK: Final = 7_318_441_902

_CREATE_LEDGER: Final = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer     PRIMARY KEY,
    name        text        NOT NULL,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


def load_migrations(directory: Path) -> list[Migration]:
    return [parse(p.name, p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.sql"))]


async def migrate(pool: asyncpg.Pool, migrations: list[Migration]) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns the names applied."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK)
        try:
            await conn.execute(_CREATE_LEDGER)
            rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
            pending = plan(migrations, {r["version"]: r["checksum"] for r in rows})
            for migration in pending:
                async with conn.transaction():
                    await conn.execute(migration.sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum) "
                        "VALUES ($1, $2, $3)",
                        migration.version,
                        migration.name,
                        migration.checksum,
                    )
            return [m.name for m in pending]
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK)
