"""The key log (07 §2, 19 §10): every key that signs anything, and what happened to it.

One chain per host (`keylog:<host_ref>`). The first entry is the host's own key, self-signed: the
trust anchor a verifier pins. Nothing is deleted. Rotation, revocation and compromise are new
entries; signatures made after a compromise are flagged by audit().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from gap_core.clock import Clock
from gap_core.signatures import Signer, public_key_from_jwk
from gap_core.timefmt import format_ts, parse_ts

from .ledger import Ledger, LedgerError

ROLES = ("surface", "recovery_key", "caller", "host", "operator", "source", "evaluator_release")
PROTECTION = ("hardware_attested", "hardware", "software")
USER_VERIFICATION = ("inherence", "knowledge", "none")
TIME_FIELDS = ("posted_at", "occurred_at", "evaluated_at", "issued_at", "released_at", "received_at", "at")


@dataclass(frozen=True)
class KeyState:
    key_id: str
    principal_role: str
    principal_ref: str
    public_key: dict
    protection_class: str
    status: str
    valid_from: datetime
    changed_at: datetime
    compromised_since: datetime | None = None
    user_verification: str = "none"  # what the platform enforces before the key will sign (07 §3), recorded at enrolment


def record_time(rec: dict) -> datetime | None:
    for f in TIME_FIELDS:
        if f in rec:
            return parse_ts(rec[f])
    return None


class KeyLog:
    def __init__(self, ledger: Ledger, host_ref: str, host_signer: Signer, clock: Clock) -> None:
        self.ledger, self.host_ref, self.signer, self.clock = ledger, host_ref, host_signer, clock
        self.chain_id = f"keylog:{host_ref}"

    def _entry(self, key_id: str, status: str, **fields) -> dict:
        body = {"record_type": "key", "key_id": key_id, "status": status, "at": format_ts(self.clock.now()), **fields}
        return self.ledger.append(self.chain_id, body, self.signer, record_id=key_id)

    def bootstrap(self) -> dict:
        """Self-signed first entry for the host key: the anchor verifiers pin."""
        if self.ledger.head(self.chain_id) is not None:
            raise LedgerError("key log already started")
        return self._entry(
            self.signer.key_id, "active", principal_role="host", principal_ref=self.host_ref, public_key=self.signer.public_jwk,
            algorithm="ES256", protection_class="software", valid_from=format_ts(self.clock.now()), enrolled_by="bootstrap",
        )

    def enrol(self, key_id: str, *, role: str, principal_ref: str, public_key: dict, protection_class: str, enrolled_by: str,
              user_verification: str = "none") -> dict:
        if role not in ROLES or protection_class not in PROTECTION or user_verification not in USER_VERIFICATION:
            raise LedgerError("unknown role, protection class or user verification")
        public_key_from_jwk(public_key)  # must be a valid P-256 key
        if key_id in self.state():
            raise LedgerError(f"{key_id} is already in the key log")
        return self._entry(key_id, "active", principal_role=role, principal_ref=principal_ref, public_key=public_key,
                           algorithm="ES256", protection_class=protection_class, valid_from=format_ts(self.clock.now()),
                           enrolled_by=enrolled_by, user_verification=user_verification)

    def _change(self, key_id: str, status: str, **extra) -> dict:
        cur = self.state().get(key_id)
        if cur is None:
            raise LedgerError(f"unknown key {key_id}")
        if cur.status in ("revoked", "compromised") and status != "compromised":
            raise LedgerError(f"{key_id} is {cur.status}; it cannot become {status}")
        return self._entry(key_id, status, **extra)

    def retire(self, key_id: str) -> dict:
        return self._change(key_id, "retired")

    def revoke(self, key_id: str) -> dict:
        return self._change(key_id, "revoked")

    def compromise(self, key_id: str, since: datetime) -> dict:
        return self._change(key_id, "compromised", compromised_since=format_ts(since))

    def state(self) -> dict[str, KeyState]:
        out: dict[str, KeyState] = {}
        for rec in self.ledger.records(self.chain_id, "key"):
            prev = out.get(rec["key_id"])
            at = parse_ts(rec["at"])
            if prev is None:
                out[rec["key_id"]] = KeyState(rec["key_id"], rec["principal_role"], rec["principal_ref"], rec["public_key"],
                                              rec["protection_class"], rec["status"], parse_ts(rec["valid_from"]), at,
                                              user_verification=rec.get("user_verification", "none"))
            else:
                cs = parse_ts(rec["compromised_since"]) if "compromised_since" in rec else prev.compromised_since
                out[rec["key_id"]] = KeyState(prev.key_id, prev.principal_role, prev.principal_ref, prev.public_key,
                                              prev.protection_class, rec["status"], prev.valid_from, at, cs, prev.user_verification)
        return out

    def public_keys(self) -> dict[str, dict]:
        """Every key ever enrolled: past signatures stay verifiable after retirement or revocation."""
        return {k: s.public_key for k, s in self.state().items()}

    def audit(self) -> list[tuple[str, int, str]]:
        """Records signed by a key after it was compromised or revoked: (chain_id, seq, flag). Nothing is deleted."""
        st = self.state()
        flags = []
        for chain_id in self.ledger.chains():
            for rec in self.ledger.records(chain_id):
                k = st.get(rec["signature"]["key_id"])
                t = record_time(rec)
                if k is None:
                    flags.append((chain_id, rec["chain"]["seq"], "unknown_key"))
                elif k.status == "compromised" and (t is None or t >= k.compromised_since):
                    flags.append((chain_id, rec["chain"]["seq"], "signed_after_compromise"))
                elif k.status == "revoked" and (t is None or t > k.changed_at):
                    flags.append((chain_id, rec["chain"]["seq"], "signed_after_revocation"))
        return flags
