import pytest

from adapter_verify.observability.fakes import MemoryPayloadStore
from adapter_verify.observability.ports import PayloadStore
from tests.contracts import payload_store_contract

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def _memory() -> PayloadStore:
    return MemoryPayloadStore()


async def test_memory_payload_store_honours_the_contract() -> None:
    await payload_store_contract(_memory)
