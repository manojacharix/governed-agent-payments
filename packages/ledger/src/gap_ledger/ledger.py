"""The ledger (19 §1.4, §1.5, §12): signed, hash-chained records in one append-only Postgres table.

append() takes a per-chain advisory lock, so appends to one chain are serialised; this is also how
"one hold per wheel at a time" is enforced (03 D1). verify_integrity() re-checks everything from the
stored bytes: chain order, links, record hashes, signatures and the columns that mirror the body.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from gap_core.chain import ChainError, append as chain_append, record_hash, verify_chain
from gap_core.signatures import Signer


class LedgerError(RuntimeError):
    """A ledger operation was refused."""


@dataclass
class IntegrityReport:
    chains_checked: int = 0
    records_checked: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)  # (chain_id, message)

    @property
    def ok(self) -> bool:
        return not self.problems


class Ledger:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    # --- writing ------------------------------------------------------------------

    def lock_chain(self, chain_id: str) -> None:
        """Serialise writers on one chain until the end of the current transaction."""
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (chain_id,))

    def append(self, chain_id: str, body: dict, signer: Signer, *, record_id: str, continues: dict | None = None) -> dict:
        if "record_type" not in body:
            raise LedgerError("every ledger record has a record_type")
        with self.conn.transaction():
            self.lock_chain(chain_id)
            prev = self.head(chain_id)
            if continues is not None and prev is not None:
                raise LedgerError("a continuation can only start an empty chain")
            rec = chain_append(body, chain_id=chain_id, previous=prev, signer=signer, continues=continues)
            unsigned = {k: v for k, v in rec.items() if k != "signature"}
            self.conn.execute(
                "INSERT INTO ledger_records (chain_id, seq, record_type, record_id, body, record_hash, previous_hash, signature)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (chain_id, rec["chain"]["seq"], body["record_type"], record_id, Jsonb(unsigned), record_hash(rec),
                 rec["chain"]["previous_hash"], Jsonb(rec["signature"])),
            )
            return rec

    # --- reading ------------------------------------------------------------------

    def head(self, chain_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT body, signature FROM ledger_records WHERE chain_id = %s ORDER BY seq DESC LIMIT 1", (chain_id,)
        ).fetchone()
        return None if row is None else {**row[0], "signature": row[1]}

    def records(self, chain_id: str, record_type: str | None = None) -> list[dict]:
        sql = "SELECT body, signature FROM ledger_records WHERE chain_id = %s"
        args: tuple = (chain_id,)
        if record_type is not None:
            sql += " AND record_type = %s"
            args += (record_type,)
        return [{**b, "signature": s} for b, s in self.conn.execute(sql + " ORDER BY seq", args).fetchall()]

    def chains(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT chain_id FROM ledger_records ORDER BY chain_id").fetchall()]

    # --- verification -------------------------------------------------------------

    def verify_integrity(self, keys: dict[str, dict]) -> IntegrityReport:
        """Check every chain from the stored rows. keys maps key_id to public JWK (from the key log)."""
        report = IntegrityReport()
        rows_by_chain: dict[str, list] = {}
        for chain_id, seq, body, sig, stored_hash, stored_prev in self.conn.execute(
            "SELECT chain_id, seq, body, signature, record_hash, previous_hash FROM ledger_records ORDER BY chain_id, seq"
        ).fetchall():
            rows_by_chain.setdefault(chain_id, []).append((seq, {**body, "signature": sig}, stored_hash, stored_prev))
        records_by_chain = {cid: [r for _, r, _, _ in rows] for cid, rows in rows_by_chain.items()}
        for chain_id, rows in rows_by_chain.items():
            report.chains_checked += 1
            report.records_checked += len(rows)
            recs = records_by_chain[chain_id]
            try:
                for seq, rec, stored_hash, stored_prev in rows:
                    if rec["chain"]["seq"] != seq or rec["chain"]["chain_id"] != chain_id:
                        raise ChainError(f"row {seq}: columns disagree with the chain link")
                    if stored_hash != record_hash(rec):
                        raise ChainError(f"row {seq}: stored record_hash does not match the record")
                    if stored_prev != rec["chain"]["previous_hash"]:
                        raise ChainError(f"row {seq}: stored previous_hash does not match the record")
                cont = recs[0]["chain"].get("continues")
                previous_chain = records_by_chain.get(cont["chain_id"]) if cont else None
                verify_chain(recs, keys, previous_chain=previous_chain)
            except ChainError as e:
                report.problems.append((chain_id, str(e)))
        return report
