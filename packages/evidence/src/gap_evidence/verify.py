"""The open verifier (06 §1): checks an evidence bundle with nothing but the bundle, a pinned host key and the
evaluator. It depends only on gap_core: no database, no network, no trust in whoever produced the bundle.

Checks: the bundle's own signature; the key log (self-signed anchor = the pinned key); every disclosed
record's signature, and that each signing key was valid when used; the chain skeleton links and every
disclosed record sits where it claims; each disclosed input hashes to what the decision recorded and
replays to the same bytes; the payer's assertion matches what was shown; and which attestations the facts carry.
"""

from __future__ import annotations

from gap_core.canonical import canonical
from gap_core.chain import ChainError, record_hash, unsigned_bytes, verify_chain
from gap_core.codec import CodecError, decode
from gap_core.evaluate import evaluate
from gap_core.hashing import hash_value
from gap_core.device_proof import ProofError, verify_device_proof
from gap_core.signatures import SignatureError, verify
from gap_core.timefmt import parse_ts

TIME_FIELDS = ("posted_at", "occurred_at", "evaluated_at", "issued_at", "released_at", "received_at", "at", "started_at", "opened_at")


def _key_states(key_log: list[dict]) -> dict:
    states: dict = {}
    for r in key_log:
        cur = states.get(r["key_id"])
        if cur is None:
            states[r["key_id"]] = {"jwk": r["public_key"], "role": r["principal_role"], "status": r["status"], "since": None, "changed": r["at"]}
        else:
            cur.update(status=r["status"], changed=r["at"], since=r.get("compromised_since", cur["since"]))
    return states


def _time(rec: dict):
    for f in TIME_FIELDS:
        if f in rec:
            return parse_ts(rec[f])
    return None


def verify_bundle(bundle: dict, pinned_host_jwk: dict, *, evaluator: str) -> dict:
    report = {"bundle_signature": "ok", "key_log": "ok", "signatures": "ok", "chain": "ok", "keys": "ok",
              "replay": "not_disclosed", "presented_hash": "not_disclosed", "payer_signatures": "not_disclosed", "attestations": [],
              "problems": []}
    bad = report["problems"].append

    sig = bundle.get("signature", {})
    try:
        verify(pinned_host_jwk, canonical({k: v for k, v in bundle.items() if k != "signature"}), sig.get("value", ""))
    except SignatureError as e:
        report["bundle_signature"] = "failed"
        bad(f"bundle signature: {e}")

    key_log = bundle.get("key_log", [])
    try:
        if not key_log or key_log[0]["public_key"] != pinned_host_jwk:
            raise ChainError("key log does not start with the pinned host key")
        anchor = {key_log[0]["key_id"]: pinned_host_jwk}
        verify_chain(key_log, {**anchor, **{r["key_id"]: r["public_key"] for r in key_log if "public_key" in r}})
    except (ChainError, KeyError) as e:
        report["key_log"] = "failed"
        bad(f"key log: {e}")
    states = _key_states(key_log)

    by_seq = {s[0]: s for s in bundle.get("skeleton", [])}
    skel = sorted(by_seq.values())
    for a, b in zip(skel, skel[1:]):
        if b[0] != a[0] + 1 or b[1] != a[2]:
            report["chain"] = "broken"
            bad(f"skeleton breaks between {a[0]} and {b[0]}")
            break

    for rec in bundle.get("records", []):
        s = rec.get("signature", {})
        k = states.get(s.get("key_id"))
        try:
            if k is None:
                raise SignatureError("unknown key")
            verify(k["jwk"], unsigned_bytes(rec), s.get("value", ""))
        except SignatureError as e:
            report["signatures"] = "failed"
            bad(f"{rec.get('record_type')} {rec['chain']['seq']}: {e}")
        entry = by_seq.get(rec["chain"]["seq"])
        if entry is None or entry[2] != record_hash(rec) or entry[1] != rec["chain"]["previous_hash"]:
            report["chain"] = "broken"
            bad(f"record {rec['chain']['seq']} is not where the skeleton says")
        t = _time(rec)
        if k and k["status"] == "compromised" and (t is None or t >= parse_ts(k["since"])):
            report["keys"] = "signed_after_compromise"
        if rec.get("record_type") == "surface_assertion":
            sk = states.get(rec.get("surface_key_id"))
            if sk is None or sk["role"] != "surface":
                report["keys"] = "assertion_key_unknown"
            elif sk["status"] == "compromised" and t >= parse_ts(sk["since"]):
                report["keys"] = "signed_after_compromise"

    # The payer's consent, checked from the device's own signature rather than the host's record of it.
    for a in (r for r in bundle.get("records", []) if r.get("record_type") == "surface_assertion"):
        report["payer_signatures"] = "ok" if report["payer_signatures"] == "not_disclosed" else report["payer_signatures"]
        ch, proof, sk, rp = a.get("challenge"), a.get("device_proof"), states.get(a.get("surface_key_id")), a.get("relying_party") or {}
        try:
            if not isinstance(ch, dict) or not isinstance(proof, dict):
                raise ProofError("the device's proof was not kept")
            if ch.get("subject_hash") != a.get("subject_hash") or ch.get("action") != a.get("action"):
                raise ProofError("the signed challenge is for a different action or subject")
            if sk is None:
                raise ProofError("unknown surface key")
            verify_device_proof(sk["jwk"], ch, proof, rp_id=rp.get("rp_id"), origin=rp.get("origin"))
        except ProofError as e:
            report["payer_signatures"] = "failed"
            bad(f"payer assertion {a.get('assertion_id')}: {e}")

    dets = {r["determination_id"]: r for r in bundle.get("records", []) if r.get("record_type") == "determination"}
    inputs = bundle.get("inputs", {})
    if inputs:
        report["replay"] = "reproduced"
        for det_id, encoded in inputs.items():
            rec = dets.get(det_id)
            try:
                if rec is None or hash_value(encoded) != rec.get("input_hash"):
                    raise CodecError("input does not hash to what the decision recorded")
                inp = decode(encoded)
                replayed = evaluate(inp).to_record()
                stored = {k: v for k, v in rec.items() if k not in ("chain", "signature", "input_hash", "input_ref")}
                if canonical(replayed) != canonical(stored):
                    report["replay"] = "differs"
                    bad(f"{det_id}: replay differs")
                for f in inp.facts:
                    report["attestations"].append({"fact": f.name, "class": f.attestation, "verified": f.verified})
            except (CodecError, ValueError, TypeError) as e:
                report["replay"] = "differs"
                bad(f"{det_id}: {e}")
        if report["replay"] == "reproduced" and any(r.get("versions", {}).get("evaluator") != evaluator for r in dets.values()):
            report["replay"] = "evaluator_unpublished"

    asr = [r for r in bundle.get("records", []) if r.get("record_type") == "surface_assertion" and r.get("action") == "authorise"]
    first = [r for r in dets.values() if r.get("stage") == "proposal"]
    if asr and first:
        shown = {r.get("presented_hash") for r in first}
        report["presented_hash"] = "matches" if all(a.get("subject_hash") in shown for a in asr) else "differs"
        if report["presented_hash"] == "differs":
            bad("the payer approved something other than what was shown")

    report["ok"] = not report["problems"]
    return report
