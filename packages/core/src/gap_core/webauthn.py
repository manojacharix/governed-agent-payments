"""Minimal WebAuthn assertion verification (for recovery keys and passkey surfaces), plus a software
authenticator for tests. Checks: ceremony type, challenge, origin, relying-party id hash, user presence,
and the signature over authenticatorData || SHA-256(clientDataJSON). Reports whether the user was verified.
"""

from __future__ import annotations

import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from gap_core.signatures import b64url_decode, b64url_encode, public_key_from_jwk

FLAG_UP, FLAG_UV = 0x01, 0x04


class WebAuthnError(ValueError):
    """The WebAuthn assertion does not verify."""


def verify_assertion(public_jwk: dict, challenge: bytes, proof: dict, *, rp_id: str, origin: str) -> bool:
    """Returns True if the user was verified (UV flag). Raises WebAuthnError otherwise."""
    try:
        auth_data = b64url_decode(proof["authenticator_data"])
        cdj = b64url_decode(proof["client_data_json"])
        sig = b64url_decode(proof["signature"])
        client = json.loads(cdj)
    except (KeyError, ValueError) as e:
        raise WebAuthnError(f"malformed assertion: {e}") from None
    if client.get("type") != "webauthn.get":
        raise WebAuthnError("not an authentication ceremony")
    if client.get("challenge") != b64url_encode(challenge):
        raise WebAuthnError("challenge does not match")
    if client.get("origin") != origin:
        raise WebAuthnError("origin does not match")
    if len(auth_data) < 37 or auth_data[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise WebAuthnError("relying party id does not match")
    flags = auth_data[32]
    if not flags & FLAG_UP:
        raise WebAuthnError("user was not present")
    try:
        public_key_from_jwk(public_jwk).verify(sig, auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise WebAuthnError("signature does not verify") from None
    return bool(flags & FLAG_UV)


class SoftwareAuthenticator:
    """Stands in for a security key or passkey in tests: a P-256 key that produces WebAuthn assertions."""

    def __init__(self, key_id: str, *, rp_id: str, origin: str, user_verifies: bool = True) -> None:
        self.key_id, self.rp_id, self.origin, self.user_verifies = key_id, rp_id, origin, user_verifies
        self._key = ec.generate_private_key(ec.SECP256R1())
        n = self._key.public_key().public_numbers()
        self.public_jwk = {"kty": "EC", "crv": "P-256", "x": b64url_encode(n.x.to_bytes(32, "big")), "y": b64url_encode(n.y.to_bytes(32, "big"))}

    def get(self, challenge: bytes) -> dict:
        cdj = json.dumps({"type": "webauthn.get", "challenge": b64url_encode(challenge), "origin": self.origin}).encode()
        flags = FLAG_UP | (FLAG_UV if self.user_verifies else 0)
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")
        sig = self._key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"format": "webauthn", "authenticator_data": b64url_encode(auth_data),
                "client_data_json": b64url_encode(cdj), "signature": b64url_encode(sig)}
