"""Correlation IDs: validation of caller-supplied IDs and UUIDv7 generation (RFC 9562 §5.7).

Python 3.12 has no ``uuid.uuid7`` (D-002), so the bit layout lives here as a pure function;
the clock and randomness come from ports.
"""

import re
from uuid import UUID

MAX_CORRELATION_ID_LENGTH = 128
_CORRELATION_ID = re.compile(rf"[A-Za-z0-9._:-]{{1,{MAX_CORRELATION_ID_LENGTH}}}")

UUID7_RANDOM_BYTES = 10
_MAX_UNIX_MS = 1 << 48


def is_valid_correlation_id(value: str) -> bool:
    """Caller-supplied IDs are rejected, never replaced, when this fails (M1-Q4)."""
    return _CORRELATION_ID.fullmatch(value) is not None


def uuid7_from(unix_ms: int, rand: bytes) -> UUID:
    """Build a UUIDv7 from a millisecond Unix timestamp and 10 random bytes.

    Layout: 48-bit timestamp | version 7 | 12 random bits | variant 0b10 | 62 random bits.
    """
    if not 0 <= unix_ms < _MAX_UNIX_MS:
        msg = "unix_ms must fit in 48 bits"
        raise ValueError(msg)
    if len(rand) != UUID7_RANDOM_BYTES:
        msg = f"need exactly {UUID7_RANDOM_BYTES} random bytes"
        raise ValueError(msg)
    r = int.from_bytes(rand, "big")
    rand_a = (r >> 68) & 0xFFF
    rand_b = r & ((1 << 62) - 1)
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return UUID(int=value)


def uuid7_unix_ms(value: UUID) -> int:
    """The timestamp a UUIDv7 was built from."""
    return value.int >> 80
