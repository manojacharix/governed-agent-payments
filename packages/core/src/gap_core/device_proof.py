"""What a payer's device signs, and how anyone checks it (07 §3). Shared by the host, which checks an assertion once
when it arrives, and the open evidence verifier, which checks it again later from the ledger alone: the record keeps
the challenge and the device's proof, so payer consent never rests on the host's word."""

from __future__ import annotations

import hashlib

from .canonical import canonical
from .signatures import SignatureError, verify
from .webauthn import WebAuthnError, verify_assertion as verify_webauthn

CHALLENGE_FIELDS = ("challenge_id", "nonce", "action", "subject_hash")
PROOF_FIELDS = {"raw_es256": ("format", "value"), "webauthn": ("format", "authenticator_data", "client_data_json", "signature")}


class ProofError(ValueError):
    """The device's proof does not verify against the challenge and key."""


def challenge_bytes(ch: dict) -> bytes:
    return hashlib.sha256(canonical({k: ch[k] for k in CHALLENGE_FIELDS})).digest()


def kept_proof(proof: dict) -> dict:
    """The parts of a verified proof worth keeping: exactly what a verifier needs, nothing a client added."""
    return {k: proof[k] for k in PROOF_FIELDS[proof["format"]]}


def verify_device_proof(public_jwk: dict, challenge: dict, proof: dict, *, rp_id: str | None = None, origin: str | None = None) -> bool:
    """Checks the device's signature over the challenge. Returns whether the authenticator itself reported user
    verification (WebAuthn's UV flag); a raw signature reports nothing, so False. Raises ProofError otherwise."""
    if not isinstance(proof, dict) or proof.get("format") not in PROOF_FIELDS:
        raise ProofError("unknown proof format")
    try:
        cb = challenge_bytes(challenge)
    except (KeyError, TypeError) as e:
        raise ProofError(f"malformed challenge: {e}") from None
    if proof["format"] == "raw_es256":
        try:
            verify(public_jwk, cb, proof.get("value", ""))
        except SignatureError as e:
            raise ProofError(str(e)) from None
        return False
    try:
        return verify_webauthn(public_jwk, cb, proof, rp_id=rp_id or "", origin=origin or "")
    except WebAuthnError as e:
        raise ProofError(str(e)) from None
