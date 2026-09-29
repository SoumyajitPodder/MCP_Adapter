"""Deterministic fakes of the common ports.

Shipped in the package, not in tests, so unit tests, the golden sandbox and local runs share
one implementation that mypy checks against the same Protocols (DESIGN.md X-10).
"""

import hashlib
from datetime import datetime, timedelta


class ManualClock:
    """A clock that only moves when told to."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            msg = "ManualClock needs a timezone-aware start time"
            raise ValueError(msg)
        self._now = start
        self._monotonic = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            msg = "time only moves forward"
            raise ValueError(msg)
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class SeededEntropy:
    """Reproducible byte stream (SHA-256 in counter mode). Never use outside tests or sandboxes."""

    def __init__(self, seed: bytes) -> None:
        self._seed = seed
        self._counter = 0

    def token_bytes(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            out += hashlib.sha256(self._seed + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
        return bytes(out[:n])
