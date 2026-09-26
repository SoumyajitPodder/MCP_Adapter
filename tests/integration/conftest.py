"""Real Postgres for integration tests (brief §12). Needs a running Docker daemon.

One container per session; every test gets its own freshly migrated database, so tests are
isolated even though the audit table forbids TRUNCATE.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from itertools import count

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.community.postgres import PostgresContainer

from adapter_verify.composition import run_migrations

POSTGRES_IMAGE = "postgres:17-alpine"
_databases = count(1)


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def database_url(postgres_url: str) -> str:
    name = f"test_{next(_databases)}"

    async def create() -> None:
        admin = await asyncpg.connect(postgres_url)
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()

    asyncio.run(create())
    base, _, _ = postgres_url.rpartition("/")
    return f"{base}/{name}"


@pytest_asyncio.fixture
async def pool(database_url: str) -> AsyncIterator[asyncpg.Pool]:
    created = await asyncpg.create_pool(database_url, min_size=1, max_size=20)
    await run_migrations(created)
    try:
        yield created
    finally:
        await created.close()
