from datetime import UTC, datetime

import pytest

from adapter_verify.common.adapters.system import SystemClock, SystemEntropy
from adapter_verify.common.fakes import ManualClock, SeededEntropy

pytestmark = pytest.mark.unit

_START = datetime(2026, 1, 1, tzinfo=UTC)


def test_manual_clock_moves_only_when_advanced() -> None:
    clock = ManualClock(_START)
    assert clock.now() == _START
    clock.advance(1.5)
    assert (clock.now() - _START).total_seconds() == 1.5
    assert clock.monotonic() == 1.5


def test_manual_clock_rejects_naive_start_and_going_back() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValueError, match="forward"):
        ManualClock(_START).advance(-1)


def test_seeded_entropy_is_reproducible_and_seed_dependent() -> None:
    assert SeededEntropy(b"a").token_bytes(70) == SeededEntropy(b"a").token_bytes(70)
    assert SeededEntropy(b"a").token_bytes(16) != SeededEntropy(b"b").token_bytes(16)
    stream = SeededEntropy(b"a")
    assert stream.token_bytes(8) != stream.token_bytes(8)


def test_system_adapters_honour_the_port_contract() -> None:
    assert SystemClock().now().tzinfo is not None
    assert SystemClock().monotonic() <= SystemClock().monotonic()
    assert len(SystemEntropy().token_bytes(10)) == 10
