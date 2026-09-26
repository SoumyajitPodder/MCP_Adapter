"""Brief §7.10 crash case on real Postgres: a process killed mid-call leaves its row RESERVED.
Once the lease expires the sweeper marks it UNKNOWN, a retry gets RECONCILIATION_PENDING, and an
operator resolves it with the CLI. Sync module: it drives the CLI and a child process."""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from click.testing import CliRunner

from adapter_kernel.errors import ErrorCode
from adapter_kernel.pipeline import ToolRequest, ToolSuccess
from adapter_verify.cli.main import cli
from adapter_verify.common.fakes import ManualClock, SeededEntropy
from adapter_verify.idempotency.fakes import FaultyConnector
from tests.integration import idempotency_support
from tests.integration.crash_worker import SENT
from tests.unit.idempotency.support import ARGS, TOOL, context, error

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


def _crash_mid_call(database_url: str) -> None:
    child = subprocess.Popen(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", "tests.integration.crash_worker", database_url],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().strip()
    finally:
        child.kill()
        _, stderr = child.communicate(timeout=30)
    assert line == SENT, stderr


async def _retry(database_url: str, clock: ManualClock) -> tuple[ErrorCode | None, int]:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    try:
        backend = FaultyConnector()
        stage = idempotency_support.stage(pool, clock, SeededEntropy(b"retry"))
        result = await stage(context(cid="crash-retry"), ToolRequest(arguments=ARGS), backend)
        if isinstance(result.outcome, ToolSuccess):
            return None, backend.calls
        return error(result), backend.calls
    finally:
        await pool.close()


async def _sweep(database_url: str, clock: ManualClock) -> tuple[str, int]:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    try:
        before = await pool.fetchval("SELECT state FROM idempotency_records")
        swept = await idempotency_support.maintenance(pool, clock).sweep()
        return before, len(swept)
    finally:
        await pool.close()


def test_crash_mid_call_ends_in_unknown_and_an_operator_resolves_it(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADAPTER_DATABASE_DSN", database_url)
    runner = CliRunner()
    assert runner.invoke(cli, ["db", "migrate"]).exit_code == 0

    _crash_mid_call(database_url)

    after_lease = ManualClock(datetime.now(UTC) + timedelta(seconds=31))
    assert asyncio.run(_sweep(database_url, after_lease)) == ("RESERVED", 1)
    assert asyncio.run(_retry(database_url, after_lease)) == (ErrorCode.RECONCILIATION_PENDING, 0)

    listed = runner.invoke(cli, ["idem", "unknown"])
    assert listed.exit_code == 0
    assert f"writer\t{TOOL}\tk-1\tUNKNOWN" in listed.output

    resolved = runner.invoke(
        cli,
        [
            "idem",
            "resolve",
            *("--agent", "writer", "--tool", TOOL, "--key", "k-1"),
            *("--as", "failed", "--reason", "no order found upstream", "--operator", "ops"),
        ],
    )
    assert resolved.exit_code == 0, resolved.output
    assert "FAILED_RETRYABLE" in resolved.output

    # A person confirmed nothing happened: the key is reusable and the retry executes once.
    assert asyncio.run(_retry(database_url, after_lease)) == (None, 1)

    assert runner.invoke(cli, ["audit", "verify"]).exit_code == 0
    purged = runner.invoke(cli, ["idem", "purge"])
    assert (purged.exit_code, purged.output.strip()) == (0, "purged 0 row(s)")
    swept = runner.invoke(cli, ["idem", "sweep"])
    assert swept.exit_code == 0
    assert "0 expired lease(s)" in swept.output
