"""ES256 signature verification (19 §1.4): P-256, SHA-256, 64-byte r‖s, base64url, low-s only.

Only public keys appear here; signing lives in gap_keys.
"""

from __future__ import annotations

import base64
import re
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


class SignatureError(ValueError):
    """A signature or key is malformed, or the signature does not verify."""


class Signer(Protocol):
    key_id: str
    public_jwk: dict

    def sign(self, data: bytes) -> str: ...


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not _B64URL.fullmatch(text):
        raise SignatureError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    if not isinstance(jwk, dict) or set(jwk) != {"kty", "crv", "x", "y"} or jwk["kty"] != "EC" or jwk["crv"] != "P-256":
        raise SignatureError("public key must be a P-256 JWK with exactly kty, crv, x, y")
    x, y = b64url_decode(jwk["x"]), b64url_decode(jwk["y"])
    if len(x) != 32 or len(y) != 32:
        raise SignatureError("P-256 coordinates must be 32 bytes")
    try:
        return ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError as e:
        raise SignatureError(f"invalid public key: {e}") from None


def verify(jwk: dict, data: bytes, signature: str) -> None:
    """Raise SignatureError unless signature is a valid low-s ES256 signature over data."""
    raw = b64url_decode(signature)
    if len(raw) != 64:
        raise SignatureError("ES256 signatures are 64 bytes")
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    if not (0 < r < P256_ORDER and 0 < s <= P256_ORDER // 2):
        raise SignatureError("signature out of range or not low-s")
    try:
        public_key_from_jwk(jwk).verify(encode_dss_signature(r, s), data, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise SignatureError("signature does not verify") from None
