"""The profile store (07 §8, 19 §11): personal data encrypted under a per-payer data key.

put() encrypts a value with AES-256-GCM under the payer's data key and returns a ProfileRef plus a
salted commitment for the ledger. shred() destroys the payer's wrapped data key: from then on nothing
of theirs can be decrypted and no commitment can be opened, while the ledger still verifies.
A retention hold (06 §5) keeps the key until a date, for evidence use only.
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime
from typing import Protocol

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gap_core.canonical import canonical
from gap_core.clock import Clock
from gap_core.commitment import SALT_BYTES, commit
from gap_core.ids import new_id

import json


class ProfileError(RuntimeError):
    """A profile store operation was refused."""


class Shredded(ProfileError):
    """The payer's data key was destroyed; the value can never be read again."""


class RetentionHeld(ProfileError):
    """The payer's data is under a retention hold until a later date (06 §5)."""


class Kek(Protocol):
    """Key-encryption key: wraps and unwraps data keys. A KMS in production; LocalKek in development."""

    kek_ref: str

    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKek:
    def __init__(self, kek_ref: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("KEK must be 32 bytes")
        self.kek_ref, self._aead = kek_ref, AESGCM(key)

    @classmethod
    def generate(cls, kek_ref: str = "local-kek") -> LocalKek:
        return cls(kek_ref, secrets.token_bytes(32))

    def wrap(self, dek: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._aead.encrypt(nonce, dek, self.kek_ref.encode())

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._aead.decrypt(wrapped[:12], wrapped[12:], self.kek_ref.encode())


class ProfileStore:
    def __init__(self, conn: psycopg.Connection, kek: Kek, clock: Clock) -> None:
        self.conn, self.kek, self.clock = conn, kek, clock
        self._rng = secrets.SystemRandom()

    def _live_key(self, payer_ref: str, create: bool) -> tuple[str, bytes]:
        row = self.conn.execute(
            "SELECT dek_id, wrapped FROM data_keys WHERE payer_ref = %s AND status <> 'shredded'", (payer_ref,)
        ).fetchone()
        if row is not None:
            return row[0], self.kek.unwrap(bytes(row[1]))
        if not create:
            raise Shredded(f"no live data key for {payer_ref}")
        dek = secrets.token_bytes(32)
        dek_id = f"dek_{secrets.token_hex(12)}"
        self.conn.execute(
            "INSERT INTO data_keys (dek_id, payer_ref, wrapped, kek_ref, status) VALUES (%s, %s, %s, %s, 'active')",
            (dek_id, payer_ref, self.kek.wrap(dek), self.kek.kek_ref),
        )
        return dek_id, dek

    def put(self, payer_ref: str, kind: str, value) -> tuple[str, str]:
        """Store a value; return (profile_ref, commitment). The commitment is what the ledger holds."""
        with self.conn.transaction():
            dek_id, dek = self._live_key(payer_ref, create=True)
            record_id = new_id("prf", clock=self.clock, rng=self._rng)
            ref = f"prof://{payer_ref}/{kind}/{record_id}"
            salt = secrets.token_bytes(SALT_BYTES)
            plaintext = canonical({"value": value, "salt": base64.urlsafe_b64encode(salt).decode()})
            nonce = secrets.token_bytes(12)
            self.conn.execute(
                "INSERT INTO profile_records (record_id, profile_ref, payer_ref, kind, dek_id, nonce, ciphertext)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (record_id, ref, payer_ref, kind, dek_id, nonce, AESGCM(dek).encrypt(nonce, plaintext, ref.encode())),
            )
            return ref, commit(value, salt)

    def _decrypt(self, profile_ref: str) -> dict:
        row = self.conn.execute(
            "SELECT r.nonce, r.ciphertext, k.wrapped, k.status FROM profile_records r JOIN data_keys k USING (dek_id)"
            " WHERE r.profile_ref = %s", (profile_ref,)
        ).fetchone()
        if row is None:
            raise ProfileError(f"no record {profile_ref}")
        nonce, ct, wrapped, status = row
        if status == "shredded" or wrapped is None:
            raise Shredded(f"{profile_ref} belongs to a shredded payer")
        dek = self.kek.unwrap(bytes(wrapped))
        return json.loads(AESGCM(dek).decrypt(bytes(nonce), bytes(ct), profile_ref.encode()))

    def get(self, profile_ref: str):
        return self._decrypt(profile_ref)["value"]

    def opening(self, profile_ref: str) -> tuple[object, bytes]:
        """(value, salt): what an evidence pack discloses to open a commitment."""
        d = self._decrypt(profile_ref)
        return d["value"], base64.urlsafe_b64decode(d["salt"])

    def place_hold(self, payer_ref: str, basis: str, until: datetime) -> None:
        with self.conn.transaction():
            n = self.conn.execute(
                "UPDATE data_keys SET status = 'held', hold_basis = %s, hold_until = %s WHERE payer_ref = %s AND status <> 'shredded'",
                (basis, until, payer_ref),
            ).rowcount
            if n == 0:
                raise ProfileError(f"no live data key for {payer_ref}")

    def shred(self, payer_ref: str) -> None:
        """Destroy the payer's data key, unless a retention hold is still running."""
        now = self.clock.now()
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT dek_id, status, hold_until FROM data_keys WHERE payer_ref = %s AND status <> 'shredded' FOR UPDATE", (payer_ref,)
            ).fetchone()
            if row is None:
                return
            dek_id, status, hold_until = row
            if status == "held" and hold_until is not None and hold_until > now:
                raise RetentionHeld(f"{payer_ref}: retention hold until {hold_until.isoformat()}")
            self.conn.execute(
                "UPDATE data_keys SET status = 'shredded', wrapped = NULL, shredded_at = %s WHERE dek_id = %s", (now, dek_id)
            )
