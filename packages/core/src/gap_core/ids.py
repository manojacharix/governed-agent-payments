"""Typed IDs: prefix + "_" + 26-character lowercase ULID (19 §1.2).

The clock and the random source are passed in, so the core never reads either itself.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .clock import Clock

PREFIXES = frozenset({
    "whl", "spk", "rul", "evt", "rcp", "prp", "det", "res", "pst", "txg", "exa", "swp", "fnd",
    "bnd", "rls", "key", "chl", "asr", "clr", "enr", "rcv", "pyr", "opr", "mrc", "dst", "prf", "ntc",
})
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
_ID = re.compile(r"([a-z]{3})_([0-7][0-9abcdefghjkmnpqrstvwxyz]{25})")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class IdError(ValueError):
    """An ID is malformed or uses an unknown prefix."""


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str, *, clock: Clock, rng) -> str:
    """rng is any object with getrandbits(n), e.g. random.Random or random.SystemRandom."""
    if prefix not in PREFIXES:
        raise IdError(f"unknown prefix {prefix!r}")
    ms = int((clock.now() - _EPOCH).total_seconds() * 1000)
    if not 0 <= ms < 2**48:
        raise IdError("clock outside the ULID time range")
    return f"{prefix}_{_encode((ms << 80) | rng.getrandbits(80))}"


def parse_id(text: str) -> tuple[str, str]:
    m = _ID.fullmatch(text) if isinstance(text, str) else None
    if m is None or m.group(1) not in PREFIXES:
        raise IdError(f"not a model id: {text!r}")
    return m.group(1), m.group(2)
