"""Production implementations of the common ports."""

import secrets
import time
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SystemEntropy:
    def token_bytes(self, n: int) -> bytes:
        return secrets.token_bytes(n)
