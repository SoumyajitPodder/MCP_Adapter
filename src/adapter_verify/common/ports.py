"""Ports for the two sources of non-determinism: time and randomness.

Everything that decides on time (leases, expiry, token checks) or needs random bytes takes
these, so tests control both (DESIGN.md X-1).
"""

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware (UTC)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...


class Entropy(Protocol):
    def token_bytes(self, n: int) -> bytes:
        """``n`` cryptographically secure random bytes."""
        ...
