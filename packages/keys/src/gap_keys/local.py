"""A software ES256 signer for development and tests. Production signs through a KMS."""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from gap_core.signatures import P256_ORDER, b64url_encode


class LocalSigner:
    def __init__(self, key_id: str, private_key: ec.EllipticCurvePrivateKey) -> None:
        if not isinstance(private_key.curve, ec.SECP256R1):
            raise ValueError("ES256 needs a P-256 key")
        self.key_id = key_id
        self._key = private_key
        nums = private_key.public_key().public_numbers()
        self.public_jwk = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64url_encode(nums.x.to_bytes(32, "big")),
            "y": b64url_encode(nums.y.to_bytes(32, "big")),
        }

    @classmethod
    def generate(cls, key_id: str) -> LocalSigner:
        return cls(key_id, ec.generate_private_key(ec.SECP256R1()))

    def sign(self, data: bytes) -> str:
        r, s = decode_dss_signature(self._key.sign(data, ec.ECDSA(hashes.SHA256())))
        if s > P256_ORDER // 2:  # low-s, so each message has exactly one accepted signature
            s = P256_ORDER - s
        return b64url_encode(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
