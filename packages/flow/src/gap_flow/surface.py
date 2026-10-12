"""A software reference Trusted Surface (build plan Phase 6). Real devices implement the same contract in
their own repositories; the full challenge protocol is Phase 7 (07 §3).

An assertion is the payer's signed statement: action + what was shown (subject_hash) for one proposal.
"""

from __future__ import annotations

from gap_core.canonical import canonical
from gap_core.clock import Clock
from gap_core.signatures import SignatureError, verify
from gap_core.timefmt import format_ts
from gap_keys.local import LocalSigner

ACTIONS = ("authorise", "override", "decline")


def assertion_payload(a: dict) -> bytes:
    return canonical({k: a[k] for k in ("action", "proposal_id", "subject_hash", "surface_key_id", "occurred_at")})


class ReferenceSurface:
    def __init__(self, key_id: str, payer_ref: str, clock: Clock) -> None:
        self.signer, self.payer_ref, self.clock = LocalSigner.generate(key_id), payer_ref, clock

    @property
    def key_id(self) -> str:
        return self.signer.key_id

    def sign(self, action: str, proposal_id: str, subject_hash: str) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action}")
        a = {"action": action, "proposal_id": proposal_id, "subject_hash": subject_hash,
             "surface_key_id": self.key_id, "occurred_at": format_ts(self.clock.now())}
        a["value"] = self.signer.sign(assertion_payload(a))
        return a


def check(assertion: dict, public_jwk: dict) -> bool:
    try:
        verify(public_jwk, assertion_payload(assertion), assertion["value"])
        return True
    except (SignatureError, KeyError):
        return False
