"""Signed, hash-chained records (19 §1.4, §1.5, §10.1).

A record is a JSON object. append() adds its chain link and signature; verify_chain() checks
order, links, signatures and, for a continued chain, the handover it continues from.
"""

from __future__ import annotations

import copy

from .canonical import canonical
from .hashing import ZERO_HASH, hash_value, is_hash
from .signatures import SignatureError, Signer, verify


class ChainError(ValueError):
    """A record or chain fails a structural, link or signature check."""


def record_hash(record: dict) -> str:
    """Hash of the full signed record; the next record's previous_hash."""
    return hash_value(record)


def unsigned_bytes(record: dict) -> bytes:
    return canonical({k: v for k, v in record.items() if k != "signature"})


def append(body: dict, *, chain_id: str, previous: dict | None, signer: Signer, continues: dict | None = None) -> dict:
    if "chain" in body or "signature" in body:
        raise ChainError("body may not set chain or signature")
    if previous is not None and continues is not None:
        raise ChainError("a continuation starts a new chain; it has no previous record in it")
    if previous is not None:
        link = previous["chain"]
        if link["chain_id"] != chain_id:
            raise ChainError("previous record belongs to another chain")
        chain = {"chain_id": chain_id, "seq": link["seq"] + 1, "previous_hash": record_hash(previous)}
    elif continues is not None:
        if set(continues) != {"chain_id", "host_ref", "handover_hash"} or not is_hash(continues["handover_hash"]):
            raise ChainError("continues needs chain_id, host_ref and a handover_hash")
        chain = {"chain_id": chain_id, "seq": 0, "previous_hash": continues["handover_hash"], "continues": dict(continues)}
    else:
        chain = {"chain_id": chain_id, "seq": 0, "previous_hash": ZERO_HASH}
    record = copy.deepcopy(body)
    record["chain"] = chain
    record["signature"] = {"key_id": signer.key_id, "alg": "ES256", "value": signer.sign(unsigned_bytes(record))}
    return record


def verify_chain(records: list[dict], keys: dict[str, dict], previous_chain: list[dict] | None = None) -> None:
    """Raise ChainError unless records form one valid chain. keys maps key_id to public JWK."""
    if not records:
        return
    chain_id = None
    for i, rec in enumerate(records):
        link, sig = rec.get("chain"), rec.get("signature")
        if not isinstance(link, dict) or not isinstance(sig, dict):
            raise ChainError(f"record {i}: missing chain link or signature")
        chain_id = chain_id or link.get("chain_id")
        if link.get("chain_id") != chain_id:
            raise ChainError(f"record {i}: belongs to another chain")
        if link.get("seq") != i:
            raise ChainError(f"record {i}: seq {link.get('seq')} out of order")
        if i == 0:
            _check_start(link, previous_chain)
        elif link.get("previous_hash") != record_hash(records[i - 1]):
            raise ChainError(f"record {i}: previous_hash does not match record {i - 1}")
        if sig.get("alg") != "ES256":
            raise ChainError(f"record {i}: unsupported algorithm")
        key = keys.get(sig.get("key_id"))
        if key is None:
            raise ChainError(f"record {i}: unknown key {sig.get('key_id')!r}")
        try:
            verify(key, unsigned_bytes(rec), sig.get("value"))
        except SignatureError as e:
            raise ChainError(f"record {i}: {e}") from None


def _check_start(link: dict, previous_chain: list[dict] | None) -> None:
    cont = link.get("continues")
    if cont is None:
        if link.get("previous_hash") != ZERO_HASH:
            raise ChainError("genesis record must have the zero previous_hash")
        return
    if not previous_chain:
        raise ChainError("a continued chain needs the chain it continues")
    handover = previous_chain[-1]
    if handover.get("record_type") != "handover":
        raise ChainError("the continued chain must end with a handover record")
    if handover["chain"]["chain_id"] != cont.get("chain_id"):
        raise ChainError("continues.chain_id does not match the previous chain")
    h = record_hash(handover)
    if cont.get("handover_hash") != h or link.get("previous_hash") != h:
        raise ChainError("continuation does not point at the handover record")
