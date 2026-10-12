"""hash(x) = "sha256:" + hex(SHA-256(canonical(x)))  (19 §1.3)."""

import hashlib
import re

from .canonical import canonical

ZERO_HASH = "sha256:" + "0" * 64
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_value(value) -> str:
    return hash_bytes(canonical(value))


def is_hash(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None
