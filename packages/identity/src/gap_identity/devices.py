"""Software stand-ins for a payer's device and an agent, used by tests and demos. Real devices and agents
implement the same messages in their own repositories."""

from __future__ import annotations

import secrets

from gap_core.canonical import canonical
from gap_core.clock import Clock
from gap_core.timefmt import format_ts
from gap_keys.local import LocalSigner

from .identity import Identity


class Device:
    """A payer's Trusted Surface: a key the platform unlocks only after user verification."""

    def __init__(self, key_id: str, *, user_verification: str = "inherence") -> None:
        self.signer, self.user_verification = LocalSigner.generate(key_id), user_verification

    @property
    def key_id(self) -> str:
        return self.signer.key_id

    @property
    def public_jwk(self) -> dict:
        return self.signer.public_jwk

    def respond(self, ch: dict) -> dict:
        return {"challenge_id": ch["challenge_id"], "key_id": self.key_id, "user_verification": self.user_verification,
                "proof": {"format": "raw_es256", "value": self.signer.sign(Identity.challenge_bytes(ch))}}


def webauthn_response(authenticator, ch: dict) -> dict:
    return {"challenge_id": ch["challenge_id"], "key_id": authenticator.key_id, "proof": authenticator.get(Identity.challenge_bytes(ch))}


class KeyAgent:
    """A key-bound caller: signs every proposal with its own key."""

    def __init__(self, key_id: str, clock: Clock) -> None:
        self.signer, self.clock, self.caller_id = LocalSigner.generate(key_id), clock, None

    def proof(self, intent_id: str, digest: str, *, nonce: str | None = None, created_at=None) -> dict:
        p = {"format": "signature", "caller_id": self.caller_id, "intent_id": intent_id, "digest": digest,
             "nonce": nonce or secrets.token_urlsafe(16), "created_at": format_ts(created_at or self.clock.now())}
        p["value"] = self.signer.sign(Identity.proof_payload(p))
        return p


def bearer_proof(token: str, clock: Clock, *, nonce: str | None = None, created_at=None) -> dict:
    return {"format": "bearer_session", "token": token, "nonce": nonce or secrets.token_urlsafe(16),
            "created_at": format_ts(created_at or clock.now())}
