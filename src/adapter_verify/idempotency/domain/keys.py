"""Idempotency keys (brief §7.2, M3-Q1) and argument fingerprints (§7.3)."""

import hashlib
import re
from typing import Final

import rfc8785

from adapter_kernel.jsontypes import JsonObject

MAX_KEY_LENGTH: Final = 128
KEY_PATTERN: Final = rf"^[A-Za-z0-9._:-]{{1,{MAX_KEY_LENGTH}}}$"
_KEY = re.compile(KEY_PATTERN)
FINGERPRINT_BYTES: Final = 32


class FingerprintError(ValueError):
    """The arguments have no RFC 8785 form (non-finite float, integer beyond I-JSON range)."""


def is_valid_key(value: str) -> bool:
    """Invalid keys are rejected, never truncated or normalized."""
    return _KEY.fullmatch(value) is not None


def fingerprint(arguments: JsonObject) -> bytes:
    """SHA-256 of the RFC 8785 (JCS) form: key order and whitespace never change it."""
    try:
        canonical = rfc8785.dumps(arguments)
    except rfc8785.CanonicalizationError:
        raise FingerprintError("arguments cannot be canonicalized") from None
    return hashlib.sha256(canonical).digest()
