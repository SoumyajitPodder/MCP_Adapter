"""Child process for the crash test: reserve a key on a real database, report that the request
was sent, then hang inside the connector until killed.

Usage: python -m tests.integration.crash_worker <database-url>
"""

import asyncio
import sys

import asyncpg

from adapter_kernel.context import CallContext
from adapter_kernel.pipeline import ToolRequest, ToolResult
from adapter_verify.common.adapters.system import SystemClock, SystemEntropy
from tests.integration import idempotency_support
from tests.unit.idempotency.support import ARGS, context

SENT = "SENT"


async def _hang(ctx: CallContext, request: ToolRequest) -> ToolResult:
    del ctx, request
    print(SENT, flush=True)  # noqa: T201 - the parent reads this line
    await asyncio.Event().wait()
    raise AssertionError  # unreachable


async def main(database_url: str) -> None:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
    stage = idempotency_support.stage(pool, SystemClock(), SystemEntropy())
    await stage(context(cid="crash-1"), ToolRequest(arguments=ARGS), _hang)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
