"""Salted commitments (07 §8, 19 §11): the ledger's stand-in for personal data.

commitment = "sha256:" + hex(SHA-256(salt ‖ canonical(value))), with a 32-byte random salt kept,
encrypted, beside the value in the profile store. Without the salt a commitment cannot be brute-forced,
even over a small value space such as addresses or item names.
"""

import hashlib
import hmac

from .canonical import canonical

SALT_BYTES = 32


def commit(value, salt: bytes) -> str:
    if not isinstance(salt, bytes) or len(salt) != SALT_BYTES:
        raise ValueError("salt must be 32 bytes")
    return "sha256:" + hashlib.sha256(salt + canonical(value)).hexdigest()


def opens(commitment: str, value, salt: bytes) -> bool:
    return hmac.compare_digest(commitment, commit(value, salt))
