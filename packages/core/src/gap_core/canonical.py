"""Canonical bytes (19 §1.3): RFC 8785 (JCS) over a restricted JSON model.

Allowed values: dict with str keys, list, str (NFC-normalised), bool, int within ±(2^53 - 1).
Refused: float, None (optional keys are omitted, never null), U+0000 and unpaired surrogates in any string
(Postgres jsonb cannot hold the first and UTF-8 cannot encode the second), nesting deeper than MAX_DEPTH,
anything else.
"""

from __future__ import annotations

import unicodedata

import rfc8785

MAX_SAFE = 2**53 - 1
MAX_DEPTH = 32


class CanonicalError(ValueError):
    """The value cannot be canonicalised under the model's rules."""


def _text(s: str, path: str) -> str:
    if "\x00" in s:
        raise CanonicalError(f"{path}: U+0000 is not allowed in strings")
    if any("\ud800" <= ch <= "\udfff" for ch in s):
        raise CanonicalError(f"{path}: unpaired surrogate in string")
    return unicodedata.normalize("NFC", s)


def _normalise(value, path: str = "$", depth: int = 0):
    if depth > MAX_DEPTH:
        raise CanonicalError(f"{path}: nested deeper than {MAX_DEPTH}")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_SAFE <= value <= MAX_SAFE:
            raise CanonicalError(f"{path}: integer outside ±(2^53 - 1)")
        return value
    if isinstance(value, str):
        return _text(value, path)
    if isinstance(value, list):
        return [_normalise(v, f"{path}[{i}]", depth + 1) for i, v in enumerate(value)]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(f"{path}: object keys must be strings")
            nk = _text(k, path)
            if nk in out:
                raise CanonicalError(f"{path}: duplicate key after NFC normalisation: {nk!r}")
            out[nk] = _normalise(v, f"{path}.{nk}", depth + 1)
        return out
    if value is None:
        raise CanonicalError(f"{path}: null is not allowed; omit optional keys instead")
    if isinstance(value, float):
        raise CanonicalError(f"{path}: floats are not allowed")
    raise CanonicalError(f"{path}: unsupported type {type(value).__name__}")


def canonical(value) -> bytes:
    """Return the canonical UTF-8 bytes of a value."""
    return rfc8785.dumps(_normalise(value))
