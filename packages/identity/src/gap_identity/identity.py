"""Identity and keys (07): who is acting, proven by keys the payer enrolled, never by a name.

- Challenges (07 §3): single-use, expiring, bound to an action and to what was shown (subject_hash).
- Surface assertions: a raw ES256 signature or a WebAuthn assertion over the challenge. The strength
  achieved is derived from the key's protection class and the user verification, on the jurisdiction's
  scale; a surface never states it.
- Enrolment: the first surface at onboarding; further surfaces and recovery keys only with the approval
  of an active surface. Recovery keys may only take part in recovery.
- Callers (07 §4): enrolled only when the payer approves. Key-bound callers sign every proposal;
  session-bound callers get an OAuth 2.1 grant with PKCE (S256), short access tokens, rotating refresh
  tokens. Claimed names are recorded, never trusted. Proofs older than identity.proof_max_age or with a
  reused nonce are refused.
- Recovery (07 §3 and the 9 Oct amendment): recovery questions (Argon2id, lockout), then either the
  recovery key (no delay) or the delayed path, which suspends payments and can be cancelled. Recovery
  never widens authority: it only replaces surface keys.
- Compromise (07 §2): a compromised surface key suspends the payer's wheels; a compromised caller key
  suspends that caller. Resuming needs an uncompromised surface at auth.strength.resume.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from gap_core.authority import Caller
from gap_core.canonical import canonical
from gap_core.clock import Clock
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.signatures import SignatureError, b64url_decode, public_key_from_jwk, verify
from gap_core.timefmt import format_ts, parse_ts
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger

from .webauthn import WebAuthnError, verify_assertion as verify_webauthn

PROTECTION_RANK = {"software": 0, "hardware": 1, "hardware_attested": 2}
UV_FACTOR = {"inherence": "inherence", "knowledge": "knowledge", "none": None}
MAX_ANSWER_FAILURES, LOCKOUT = 5, timedelta(hours=24)


MAX_NONCE = 128

class IdentityError(RuntimeError):
    """An identity check failed. `reason` is a short machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass
class CallerState:
    caller_id: str
    payer_ref: str
    assurance: str
    key_id: str | None
    valid_until: datetime
    status: str = "active"
    nonces: dict = field(default_factory=dict)  # nonce -> seen at


@dataclass
class Recovery:
    recovery_id: str
    payer_ref: str
    path: str
    new_key_id: str
    new_jwk: dict
    completes_at: datetime
    status: str = "pending"


class Identity:
    def __init__(self, ledger: Ledger, keylog: KeyLog, clock: Clock, *, scale, params: dict, host_ref: str,
                 rp_id: str = "gap.example", origin: str = "https://gap.example", secrets_conn=None, attestation=None) -> None:
        self.ledger, self.keylog, self.clock = ledger, keylog, clock
        self.scale = [(lvl.level, lvl.requires) for lvl in scale]
        self.params, self.rp_id, self.origin = params, rp_id, origin
        self.chain_id = f"host:{host_ref}"
        self.secrets_conn = secrets_conn
        self.attestation = attestation  # callable(attestation_blob) -> protection class, for real platforms
        self._rng = secrets.SystemRandom()
        self._ph = PasswordHasher()
        self.challenges: dict[str, dict] = {}
        self.used_challenges: set[str] = set()
        self.clients: dict[str, dict] = {}
        self.enrolments: dict[str, dict] = {}
        self.callers: dict[str, CallerState] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}   # sha256(token) -> grant
        self.refresh: dict[str, dict] = {}
        self.recoveries: dict[str, Recovery] = {}
        self.failures: dict[str, list] = {}
        self.suspended_payers: dict[str, str] = {}
        self.notices: list[tuple[str, str]] = []  # (payer_ref, message): delivery is the product's job

    # --- small helpers -----------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now()

    def _record(self, body: dict, record_id: str) -> dict:
        return self.ledger.append(self.chain_id, {**body, "at": format_ts(self._now())}, self.keylog.signer, record_id=record_id)

    def _rank(self, level: str) -> int:
        for i, (lvl, _req) in enumerate(self.scale):
            if lvl == level:
                return i
        raise IdentityError("unknown_level", level)

    def strength(self, protection_class: str, user_verification: str | None) -> str:
        """The strongest level on the jurisdiction's scale satisfied by possession of the key plus the user check."""
        factors = {"possession"} | ({user_verification} if user_verification in ("inherence", "knowledge") else set())
        best = self.scale[0][0]
        for lvl, requires in self.scale:
            if all((r in factors) if isinstance(r, str) else bool(set(r["any_of"]) & factors) for r in requires):
                best = lvl
        return best

    def _need(self, achieved: str, param: str) -> None:
        if self._rank(achieved) < self._rank(self.params[param]):
            raise IdentityError("strength_too_low", f"{achieved} < {self.params[param]} ({param})")

    def _payer_surfaces(self, payer_ref: str, role: str = "surface"):
        return [k for k in self.keylog.state().values() if k.principal_ref == payer_ref and k.principal_role == role and k.status == "active"]

    # --- challenges and assertions ------------------------------------------------------------

    def challenge(self, *, payer_ref: str, action: str, subject_hash: str, ttl_s: int = 300) -> dict:
        cid = new_id("chl", clock=self.clock, rng=self._rng)
        ch = {"challenge_id": cid, "nonce": base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode(),
              "action": action, "subject_hash": subject_hash, "payer_ref": payer_ref,
              "issued_at": format_ts(self._now()), "expires_at": format_ts(self._now() + timedelta(seconds=ttl_s))}
        self.challenges[cid] = ch
        self._record({"record_type": "challenge", **ch}, cid)
        return ch

    @staticmethod
    def challenge_bytes(ch: dict) -> bytes:
        return hashlib.sha256(canonical({k: ch[k] for k in ("challenge_id", "nonce", "action", "subject_hash")})).digest()

    def verify_assertion(self, assertion: dict, *, payer_ref: str, action: str, subject_hash: str,
                         required: str | None = None, roles: tuple = ("surface",)) -> dict:
        """Check an assertion against its challenge, key, signature and strength. Consumes the challenge."""
        if not isinstance(assertion, dict) or not isinstance(assertion.get("challenge_id"), str):
            raise IdentityError("no_challenge", "an assertion is an object naming its challenge")
        cid = assertion.get("challenge_id")
        ch = self.challenges.get(cid)
        if ch is None:
            raise IdentityError("no_challenge")
        if cid in self.used_challenges:
            raise IdentityError("challenge_reused")
        if self._now() > parse_ts(ch["expires_at"]):
            raise IdentityError("challenge_expired")
        if (ch["action"], ch["subject_hash"], ch["payer_ref"]) != (action, subject_hash, payer_ref):
            raise IdentityError("challenge_mismatch", "action, subject or payer differs from what was presented")
        key = self.keylog.state().get(assertion.get("key_id"))
        if key is None or key.principal_role not in roles or key.principal_ref != payer_ref:
            raise IdentityError("key_not_allowed")
        if key.status != "active":
            raise IdentityError("key_not_active", key.status)
        proof = assertion.get("proof", {})
        if proof.get("format") == "raw_es256":
            try:
                verify(key.public_key, self.challenge_bytes(ch), proof.get("value", ""))
            except SignatureError as e:
                raise IdentityError("bad_signature", str(e)) from None
            uv = assertion.get("user_verification", "none")  # the platform enforced it on the key (07 §3)
        elif proof.get("format") == "webauthn":
            try:
                uv = "inherence" if verify_webauthn(key.public_key, self.challenge_bytes(ch), proof, rp_id=self.rp_id, origin=self.origin) else "none"
            except WebAuthnError as e:
                raise IdentityError("bad_signature", str(e)) from None
        else:
            raise IdentityError("bad_proof_format")
        achieved = self.strength(key.protection_class, uv)
        if required:
            self._need(achieved, required)
        self.used_challenges.add(cid)
        aid = new_id("asr", clock=self.clock, rng=self._rng)
        self._record({"record_type": "surface_assertion", "assertion_id": aid, "challenge_id": cid, "key_id": key.key_id, "action": action,
                      "subject_hash": subject_hash, "user_verification": uv or "none", "auth_strength_achieved": achieved}, aid)
        return {"assertion_id": aid, "key_id": key.key_id, "strength": achieved, "role": key.principal_role}

    # --- surfaces and recovery keys ---------------------------------------------------------------

    def _protection(self, attestation) -> str:
        cls = self.attestation(attestation) if (self.attestation and attestation is not None) else "software"
        if PROTECTION_RANK[cls] < PROTECTION_RANK[self.params["identity.surface.min_assurance"]]:
            raise IdentityError("protection_too_low", f"{cls} < {self.params['identity.surface.min_assurance']}")
        return cls

    def enrol_first_surface(self, payer_ref: str, key_id: str, public_jwk: dict, *, attestation=None, user_verification="inherence") -> None:
        """Onboarding: the payer's first device. Only when the payer has no surface yet."""
        if self._payer_surfaces(payer_ref):
            raise IdentityError("already_enrolled", "further surfaces need an existing surface's approval")
        cls = self._protection(attestation)
        self._need(self.strength(cls, user_verification), "auth.strength.enrol_surface")
        self.keylog.enrol(key_id, role="surface", principal_ref=payer_ref, public_key=public_jwk, protection_class=cls, enrolled_by="onboarding")

    @staticmethod
    def key_subject(key_id: str, public_jwk: dict, role: str) -> str:
        return hash_value({"key_id": key_id, "public_key": public_jwk, "role": role})

    def enrol_key(self, payer_ref: str, key_id: str, public_jwk: dict, approval: dict, *, role: str = "surface", attestation=None) -> None:
        """A further surface, or a recovery-only key, approved by an existing active surface."""
        if role not in ("surface", "recovery_key"):
            raise IdentityError("bad_role")
        info = self.verify_assertion(approval, payer_ref=payer_ref, action="enrol", subject_hash=self.key_subject(key_id, public_jwk, role),
                                     required="auth.strength.enrol_surface")
        cls = self._protection(attestation) if role == "surface" else "hardware"
        self.keylog.enrol(key_id, role=role, principal_ref=payer_ref, public_key=public_jwk, protection_class=cls, enrolled_by=info["assertion_id"])

    # --- callers: enrolment, OAuth 2.1 + PKCE, proposal proofs -------------------------------------------

    def register_client(self, client_name: str, redirect_uri: str) -> str:
        """Dynamic client registration. The name is a claim, recorded and never trusted. Redirects go to https, or to
        loopback for native apps (RFC 8252); nothing else (javascript:, data:, plain http to a remote host)."""
        if not isinstance(client_name, str) or not 1 <= len(client_name) <= 200 or "\x00" in client_name:
            raise IdentityError("bad_request", "client_name must be 1 to 200 characters")
        if not isinstance(redirect_uri, str) or len(redirect_uri) > 2048 or not (
                redirect_uri.startswith("https://") or redirect_uri.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))):
            raise IdentityError("bad_request", "redirect_uri must be https, or loopback http")
        cid = f"cli_{secrets.token_hex(8)}"
        self.clients[cid] = {"claimed_name": client_name, "redirect_uri": redirect_uri}
        self._record({"record_type": "client_registration", "client_id": cid, "claimed_name_hash": hash_value(client_name)}, cid)
        return cid

    def request_caller(self, payer_ref: str, *, client_id: str | None = None, public_jwk: dict | None = None) -> dict:
        if (client_id is None) == (public_jwk is None):
            raise IdentityError("bad_request", "a caller brings either a key (key_bound) or an OAuth client (session_bound)")
        if client_id is not None and (not isinstance(client_id, str) or client_id not in self.clients):
            raise IdentityError("unknown_client")
        if public_jwk is not None:  # the payer must never be asked to approve a key that cannot verify anything
            try:
                public_key_from_jwk(public_jwk)
            except Exception:
                raise IdentityError("invalid_key", "not a P-256 public key") from None
        eid = new_id("enr", clock=self.clock, rng=self._rng)
        e = {"enrolment_id": eid, "payer_ref": payer_ref, "kind": "caller", "client_id": client_id, "public_jwk": public_jwk, "status": "pending"}
        e["subject_hash"] = hash_value({k: v for k, v in e.items() if k not in ("status",) and v is not None})
        self.enrolments[eid] = e
        self._record({"record_type": "enrolment", "enrolment_id": eid, "kind": "caller", "status": "pending", "subject_hash": e["subject_hash"]}, eid)
        return e

    def approve_caller(self, enrolment_id: str, approval: dict, *, code_challenge: str | None = None) -> dict:
        e = self.enrolments.get(enrolment_id)
        if e is None or e["status"] != "pending":
            raise IdentityError("no_pending_enrolment")
        info = self.verify_assertion(approval, payer_ref=e["payer_ref"], action="enrol", subject_hash=e["subject_hash"], required="auth.strength.enrol_caller")
        caller_id = new_id("clr", clock=self.clock, rng=self._rng)
        until = self._now() + timedelta(seconds=self.params["identity.caller.max_validity"])
        e["status"] = "approved"
        if e["public_jwk"] is not None:
            key_id = f"key_{caller_id}"
            self.keylog.enrol(key_id, role="caller", principal_ref=caller_id, public_key=e["public_jwk"], protection_class="software", enrolled_by=info["assertion_id"])
            self.callers[caller_id] = CallerState(caller_id, e["payer_ref"], "key_bound", key_id, until)
            out = {"caller_id": caller_id, "key_id": key_id}
        else:
            if not code_challenge:
                raise IdentityError("pkce_required", "OAuth 2.1 requires PKCE")
            self.callers[caller_id] = CallerState(caller_id, e["payer_ref"], "session_bound", None, until)
            code = secrets.token_urlsafe(32)
            self.codes[code] = {"caller_id": caller_id, "client_id": e["client_id"], "challenge": code_challenge,
                                "expires": self._now() + timedelta(minutes=5)}
            out = {"caller_id": caller_id, "code": code}
        self._record({"record_type": "caller", "caller_id": caller_id, "assurance": self.callers[caller_id].assurance,
                      "valid_until": format_ts(until), "enrolled_by": info["assertion_id"], "status": "active"}, caller_id)
        return out

    def token(self, code: str, code_verifier: str, client_id: str) -> dict:
        c = self.codes.pop(code, None)  # single use
        if c is None or c["client_id"] != client_id or self._now() > c["expires"]:
            raise IdentityError("invalid_grant")
        expected = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
        if expected != c["challenge"]:
            raise IdentityError("invalid_grant", "PKCE verifier does not match")
        return self._issue(c["caller_id"], client_id)

    def _issue(self, caller_id: str, client_id: str) -> dict:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens[hashlib.sha256(access.encode()).hexdigest()] = {"caller_id": caller_id, "expires": self._now() + timedelta(minutes=15)}
        self.refresh[hashlib.sha256(refresh.encode()).hexdigest()] = {"caller_id": caller_id, "client_id": client_id}
        return {"access_token": access, "refresh_token": refresh, "expires_in": 900, "token_type": "Bearer"}

    def refresh_token(self, refresh: str, client_id: str) -> dict:
        r = self.refresh.pop(hashlib.sha256(refresh.encode()).hexdigest(), None)  # rotation: each refresh token works once
        if r is None or r["client_id"] != client_id or self.callers[r["caller_id"]].status != "active":
            raise IdentityError("invalid_grant")
        return self._issue(r["caller_id"], client_id)

    @staticmethod
    def proof_payload(p: dict) -> bytes:
        return canonical({k: p[k] for k in ("caller_id", "intent_id", "nonce", "created_at", "digest")})

    def authenticate_proposal(self, proof: dict, *, payer_ref: str, intent_id: str, digest: str) -> Caller:
        """07 §4.2: who is proposing, proven. Returns the core Caller with its assurance, or raises with a sys.* reason."""
        now = self._now()
        if not isinstance(proof, dict) or any(not isinstance(proof.get(k, ""), str) for k in ("format", "token", "caller_id", "nonce", "created_at", "value", "intent_id", "digest")):
            raise IdentityError("sys.proof_invalid", "a proof is an object of strings")
        if len(proof.get("nonce", "")) > MAX_NONCE or len(proof.get("token", "")) > 512:
            raise IdentityError("sys.proof_invalid", "nonce or token too long")
        fmt = proof.get("format")
        if fmt == "bearer_session":
            grant = self.tokens.get(hashlib.sha256(proof.get("token", "").encode()).hexdigest())
            if grant is None or now > grant["expires"]:
                raise IdentityError("sys.proof_invalid", "unknown or expired token")
            caller_id = grant["caller_id"]
        elif fmt == "signature":
            caller_id = proof.get("caller_id")
        else:
            raise IdentityError("sys.proof_invalid", "unknown proof format")
        c = self.callers.get(caller_id)
        if c is None or c.payer_ref != payer_ref or c.status != "active" or now > c.valid_until:
            raise IdentityError("sys.caller_not_bound")
        try:
            created = parse_ts(proof["created_at"])
        except (KeyError, ValueError):
            raise IdentityError("sys.proof_invalid", "no creation time") from None
        if now - created > timedelta(seconds=self.params["identity.proof_max_age"]) or created - now > timedelta(seconds=30):
            raise IdentityError("sys.proof_stale")
        if fmt == "signature":
            if proof.get("intent_id") != intent_id or proof.get("digest") != digest:
                raise IdentityError("sys.proof_invalid", "proof does not cover this proposal")
            key = self.keylog.state().get(c.key_id)
            try:
                if key is None or key.status != "active":
                    raise SignatureError("caller key not active")
                verify(key.public_key, self.proof_payload(proof), proof.get("value", ""))
            except SignatureError as e:
                raise IdentityError("sys.proof_invalid", str(e)) from None
        nonce = proof.get("nonce")
        if not nonce or nonce in c.nonces:
            raise IdentityError("sys.nonce_reused")
        c.nonces = {n: t for n, t in c.nonces.items() if now - t <= timedelta(seconds=self.params["identity.proof_max_age"] * 2)}
        c.nonces[nonce] = now
        return Caller(caller_id, c.assurance)

    def rotate_caller_key(self, caller_id: str, new_key_id: str, new_jwk: dict, signed_by_old: str) -> None:
        """A key-bound caller rotates: the new key is signed by the old one. Spokes bind caller_id, so nothing breaks."""
        c = self.callers[caller_id]
        old = self.keylog.state()[c.key_id]
        try:
            verify(old.public_key, canonical({"rotate_to": new_key_id, "public_key": new_jwk}), signed_by_old)
        except SignatureError as e:
            raise IdentityError("bad_signature", str(e)) from None
        self.keylog.enrol(new_key_id, role="caller", principal_ref=caller_id, public_key=new_jwk, protection_class="software", enrolled_by=f"rotation:{c.key_id}")
        self.keylog.retire(c.key_id)
        c.key_id = new_key_id

    def suspend_caller(self, caller_id: str) -> None:
        self.callers[caller_id].status = "suspended"

    # --- recovery questions -----------------------------------------------------------------------

    @staticmethod
    def _norm(answer: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", answer).casefold().split())

    def set_recovery_questions(self, payer_ref: str, answers: list[str], approval: dict) -> None:
        if len(answers) != 3 or any(len(self._norm(a)) < 3 for a in answers):
            raise IdentityError("bad_answers", "three answers, each at least three characters")
        self.verify_assertion(approval, payer_ref=payer_ref, action="wheel_event", subject_hash=hash_value({"recovery_questions": payer_ref}),
                              required="auth.strength.widen")
        with self.secrets_conn.transaction():
            for slot, a in enumerate(answers):
                self.secrets_conn.execute(
                    "INSERT INTO recovery_secrets (payer_ref, slot, argon2_hash) VALUES (%s, %s, %s)"
                    " ON CONFLICT (payer_ref, slot) DO UPDATE SET argon2_hash = EXCLUDED.argon2_hash",
                    (payer_ref, slot, self._ph.hash(self._norm(a))))

    def check_answers(self, payer_ref: str, answers: list[str]) -> bool:
        now = self._now()
        recent = [t for t in self.failures.get(payer_ref, []) if now - t < LOCKOUT]
        if len(recent) >= MAX_ANSWER_FAILURES:
            raise IdentityError("locked", "too many wrong answers; try again in 24 hours")
        rows = self.secrets_conn.execute("SELECT slot, argon2_hash FROM recovery_secrets WHERE payer_ref = %s ORDER BY slot", (payer_ref,)).fetchall()
        ok = len(rows) == 3 and len(answers) == 3
        for (slot, h), a in zip(rows, answers):
            try:
                self._ph.verify(h, self._norm(a))
            except VerifyMismatchError:
                ok = False
        if not ok:
            recent.append(now)
            self.failures[payer_ref] = recent
            if len(recent) >= MAX_ANSWER_FAILURES:
                self.notices.append((payer_ref, "recovery locked after five wrong answers"))
        return ok

    # --- recovery ---------------------------------------------------------------------------------

    @staticmethod
    def recovery_subject(new_key_id: str, new_jwk: dict) -> str:
        return hash_value({"recover_to": new_key_id, "public_key": new_jwk})

    def start_recovery(self, payer_ref: str, *, new_key_id: str, new_jwk: dict, new_key_user_verification: str, answers: list[str],
                       recovery_key_assertion: dict | None = None, external_verified: bool = False) -> Recovery:
        if not self.check_answers(payer_ref, answers):
            raise IdentityError("wrong_answers")
        if new_key_user_verification not in ("inherence", "knowledge"):
            raise IdentityError("no_user_verification", "the new device must verify the user (biometric or passcode)")
        path = None
        if recovery_key_assertion is not None:
            self.verify_assertion(recovery_key_assertion, payer_ref=payer_ref, action="recover",
                                  subject_hash=self.recovery_subject(new_key_id, new_jwk), roles=("recovery_key",))
            path = "with_recovery_key"
        elif external_verified:
            path = "delayed"
        if path is None:
            raise IdentityError("insufficient_factors", "a recovery key, or the external check for the delayed path")
        delay = 0 if path == "with_recovery_key" else self.params["identity.recovery_delay"]
        rid = new_id("rcv", clock=self.clock, rng=self._rng)
        r = Recovery(rid, payer_ref, path, new_key_id, new_jwk, self._now() + timedelta(seconds=delay))
        self.recoveries[rid] = r
        self._record({"record_type": "recovery_case", "recovery_id": rid, "path": path, "status": "pending",
                      "completes_at": format_ts(r.completes_at), "factors": ["knowledge", "inherence"] + (["possession"] if path == "with_recovery_key" else ["external"])}, rid)
        self.notices.append((payer_ref, f"recovery started ({path})"))
        if delay == 0:
            self._complete(r)
        return r

    def recovery_pending(self, payer_ref: str) -> bool:
        return any(r.payer_ref == payer_ref and r.status == "pending" for r in self.recoveries.values())

    def cancel_recovery(self, recovery_id: str, assertion: dict) -> None:
        r = self.recoveries.get(recovery_id)
        if r is None or r.status != "pending":
            raise IdentityError("no_pending_recovery")
        self.verify_assertion(assertion, payer_ref=r.payer_ref, action="recover", subject_hash=hash_value({"cancel": recovery_id}),
                              roles=("surface", "recovery_key"))
        r.status = "cancelled"
        self._record({"record_type": "recovery_case", "recovery_id": recovery_id, "status": "cancelled"}, recovery_id)

    def _complete(self, r: Recovery) -> None:
        for k in self._payer_surfaces(r.payer_ref):
            self.keylog.revoke(k.key_id)
        self.keylog.enrol(r.new_key_id, role="surface", principal_ref=r.payer_ref, public_key=r.new_jwk, protection_class="software",
                          enrolled_by=r.recovery_id)
        r.status = "completed"
        self.suspended_payers.pop(r.payer_ref, None)
        self._record({"record_type": "recovery_case", "recovery_id": r.recovery_id, "status": "completed"}, r.recovery_id)
        self.notices.append((r.payer_ref, "recovery completed; every old device was removed"))

    def tick(self) -> None:
        for r in list(self.recoveries.values()):
            if r.status == "pending" and self._now() >= r.completes_at:
                self._complete(r)

    # --- compromise and suspension ----------------------------------------------------------------

    def report_compromise(self, key_id: str, since: datetime) -> str:
        key = self.keylog.state().get(key_id)
        if key is None:
            raise IdentityError("unknown_key")
        self.keylog.compromise(key_id, since)
        if key.principal_role == "surface":
            self.suspended_payers[key.principal_ref] = f"surface key {key_id} compromised"
            self.notices.append((key.principal_ref, "a device key was reported compromised; payments are suspended"))
            return "wheel_suspended"
        if key.principal_role == "caller" and key.principal_ref in self.callers:
            self.suspend_caller(key.principal_ref)
            return "caller_suspended"
        return "recorded"

    def wheel_suspended(self, payer_ref: str) -> bool:
        return payer_ref in self.suspended_payers

    def resume(self, payer_ref: str, assertion: dict) -> None:
        self.verify_assertion(assertion, payer_ref=payer_ref, action="wheel_event", subject_hash=hash_value({"resume": payer_ref}),
                              required="auth.strength.resume")
        self.suspended_payers.pop(payer_ref, None)
