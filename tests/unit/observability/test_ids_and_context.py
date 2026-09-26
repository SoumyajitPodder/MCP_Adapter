import asyncio
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapter_kernel.context import RequestContext
from adapter_verify.observability.context import bound, current_request
from adapter_verify.observability.domain.ids import (
    MAX_CORRELATION_ID_LENGTH,
    is_valid_correlation_id,
    uuid7_from,
    uuid7_unix_ms,
)

pytestmark = pytest.mark.unit

_ms = st.integers(min_value=0, max_value=(1 << 48) - 1)
_rand = st.binary(min_size=10, max_size=10)


@pytest.mark.parametrize(
    "value", ["order-88213", "sess:abc.DEF_1", "a", "x" * MAX_CORRELATION_ID_LENGTH]
)
def test_valid_correlation_ids(value: str) -> None:
    assert is_valid_correlation_id(value)


@pytest.mark.parametrize(
    "value",
    ["", "x" * (MAX_CORRELATION_ID_LENGTH + 1), "has space", "new\nline", "slash/x", "é"],
)
def test_invalid_correlation_ids(value: str) -> None:
    assert not is_valid_correlation_id(value)


@given(unix_ms=_ms, rand=_rand)
def test_uuid7_layout(unix_ms: int, rand: bytes) -> None:
    value = uuid7_from(unix_ms, rand)
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"
    assert uuid7_unix_ms(value) == unix_ms
    assert UUID(str(value)) == value


@given(a=_ms, b=_ms, rand_a=_rand, rand_b=_rand)
def test_uuid7_sorts_by_time(a: int, b: int, rand_a: bytes, rand_b: bytes) -> None:
    if a < b:
        assert str(uuid7_from(a, rand_a)) < str(uuid7_from(b, rand_b))


def test_uuid7_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError, match="48 bits"):
        uuid7_from(1 << 48, bytes(10))
    with pytest.raises(ValueError, match="48 bits"):
        uuid7_from(-1, bytes(10))
    with pytest.raises(ValueError, match="random bytes"):
        uuid7_from(0, bytes(9))


def _ctx(cid: str) -> RequestContext:
    return RequestContext(
        correlation_id=cid,
        correlation_id_generated=False,
        trace_id="t",
        tool="order.get",
        requested_version=None,
        idempotency_key=None,
    )


def test_bound_sets_and_restores() -> None:
    assert current_request() is None
    with bound(_ctx("outer")):
        with bound(_ctx("inner")):
            assert current_request() == _ctx("inner")
        assert current_request() == _ctx("outer")
    assert current_request() is None


def test_bound_restores_after_error() -> None:
    with pytest.raises(RuntimeError), bound(_ctx("x")):
        raise RuntimeError
    assert current_request() is None


@pytest.mark.asyncio
async def test_concurrent_tasks_never_see_each_others_context() -> None:
    async def run(cid: str) -> str | None:
        with bound(_ctx(cid)):
            await asyncio.sleep(0)
            seen = current_request()
            return None if seen is None else seen.correlation_id

    results = await asyncio.gather(*(run(f"c{i}") for i in range(20)))
    assert results == [f"c{i}" for i in range(20)]
