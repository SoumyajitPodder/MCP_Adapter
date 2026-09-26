import pytest

from adapter_verify.idempotency.fakes import MemoryIdempotencyStore
from adapter_verify.idempotency.ports import IdempotencyStore
from tests.contracts import idempotency_store_contract

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_memory_store_meets_the_contract() -> None:
    async def make() -> IdempotencyStore:
        return MemoryIdempotencyStore()

    await idempotency_store_contract(make)
