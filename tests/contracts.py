"""Port contract suites: every adapter of a port must pass the same tests.

Each suite takes a factory so the production adapter can be added next to the fake once it
exists (the payload store's real adapter is pending brief §15).
"""

from collections.abc import Awaitable, Callable

import pytest

from adapter_kernel.jsontypes import JsonObject
from adapter_verify.observability.ports import PayloadKind, PayloadStore


async def payload_store_contract(make: Callable[[], Awaitable[PayloadStore]]) -> None:
    store = await make()
    body: JsonObject = {"order_id": "88213", "items": [{"sku": "SKU-1"}], "note": None}
    ref = await store.put("c-1", PayloadKind.RESULT, body)
    assert isinstance(ref, str)
    assert "88213" not in ref
    assert await store.get(ref) == body

    other = await store.put("c-1", PayloadKind.RESULT, body)
    assert other != ref

    body["order_id"] = "changed-after-put"
    assert await store.get(ref) != body

    with pytest.raises(KeyError):
        await store.get("payload:does-not-exist")
