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
from gap_core.proposal import payer_subject
from gap_core.device_proof import ProofError, verify_device_proof
from gap_core.hashing import hash_value
from gap_core.proposal import payer_subject
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


ASSERTION_SCOPES = ("payer", "regulator")  # bundles that must carry the payer's assertions (06 §4)
PAYER_STATES = ("approved", "approved_with_override")


def verify_bundle(bundle: dict, pinned_host_jwk: dict, *, evaluator: str, rp_id: str | None = None, origin: str | None = None,
                  expect_role: str | None = None) -> dict:
    """rp_id and origin: the relying party the verifier expects for passkey assertions. Without them a WebAuthn
    assertion is checked against the relying party its own record names, and the report says so.
    expect_role: who asked for the bundle. The host writes requester_role; a requester who passes their own role
    stops a host relabelling a payer's bundle as a merchant's to drop the payer's assertions from it."""
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

    # The payer's consent, checked from the device's own signature rather than the host's record of it. The host
    # signs every record, so nothing it wrote about the payer counts on its own: each approval must point to an
    # assertion the payer's enrolled key signed, over exactly what was approved, while that key was in force.
    records = bundle.get("records", [])
    assertions = {r.get("assertion_id"): r for r in records if r.get("record_type") == "surface_assertion"}
    dets_by_id = {r.get("determination_id"): r for r in records if r.get("record_type") == "determination"}
    if expect_role is not None and bundle.get("requester_role") != expect_role:
        report["payer_signatures"] = "failed"
        bad(f"bundle is labelled for {bundle.get('requester_role')!r}, but was requested by {expect_role!r}")
    must_disclose = (bundle.get("requester_role") in ASSERTION_SCOPES or expect_role in ASSERTION_SCOPES
                     or bundle.get("basis", "default") != "default")

    def payer_bad(msg: str) -> None:
        report["payer_signatures"] = "failed"
        bad(msg)

    for a in assertions.values():
        report["payer_signatures"] = "ok" if report["payer_signatures"] == "not_disclosed" else report["payer_signatures"]
        ch, proof, sk, rp = a.get("challenge"), a.get("device_proof"), states.get(a.get("surface_key_id")), a.get("relying_party") or {}
        try:
            if not isinstance(ch, dict) or not isinstance(proof, dict):
                raise ProofError("the device's proof was not kept")
            if ch.get("subject_hash") != a.get("subject_hash") or ch.get("action") != a.get("action"):
                raise ProofError("the signed challenge is for a different action or subject")
            if sk is None or sk["role"] != "surface":
                raise ProofError("not an enrolled surface key")
            t = _time(a)
            if sk["status"] in ("revoked", "retired") and (t is None or t >= parse_ts(sk["changed"])):
                raise ProofError(f"the key was {sk['status']} before it signed")
            if proof.get("format") == "webauthn":
                if (rp_id, origin) != (None, None) and (rp.get("rp_id"), rp.get("origin")) != (rp_id, origin):
                    raise ProofError("the passkey assertion is for a relying party other than the one expected")
                if rp_id is None:
                    report["relying_party"] = "unpinned"
            verify_device_proof(sk["jwk"], ch, proof, rp_id=rp_id or rp.get("rp_id"), origin=origin or rp.get("origin"))
        except ProofError as e:
            payer_bad(f"payer assertion {a.get('assertion_id')}: {e}")

    # Every payer resolution, bound to this proposal as it was presented. In a payer or regulator bundle anything
    # missing fails: a check that cannot run is a failure, never a pass.
    first_by_proposal = {}
    for d in dets_by_id.values():
        if d.get("stage") == "proposal":
            first_by_proposal.setdefault(d.get("proposal_id"), []).append(d)
    used_assertions, used_challenges = set(), set()
    bound_all = True
    for res in (r for r in records if r.get("record_type") == "resolution"):
        state, aid, rid, pid = res.get("state"), res.get("assertion_id"), res.get("resolution_id"), res.get("proposal_id")
        if state in PAYER_STATES or res.get("by") == "payer":
            if not aid:
                payer_bad(f"resolution {rid}: '{state}' by the payer names no assertion")
                continue
            a = assertions.get(aid)
            if a is None:
                if must_disclose:
                    payer_bad(f"resolution {rid}: assertion {aid} is not disclosed")
                continue
            cid = (a.get("challenge") or {}).get("challenge_id")
            if aid in used_assertions or (cid is not None and cid in used_challenges):
                payer_bad(f"resolution {rid}: assertion {aid} already backs another resolution")
            used_assertions.add(aid)
            used_challenges.add(cid)
            if a.get("proposal_id") is None or a.get("proposal_id") != pid:
                payer_bad(f"resolution {rid}: assertion {aid} is not for proposal {pid}")
            want_action = {"approved": "authorise", "approved_with_override": "override", "declined": "decline"}.get(state)
            if want_action is None or a.get("action") != want_action:
                payer_bad(f"resolution {rid}: '{state}' needs a matching assertion, not '{a.get('action')}'")
            first = first_by_proposal.get(pid, [])
            if not first:
                bound_all = False
                if must_disclose:
                    payer_bad(f"resolution {rid}: the decision the payer was shown is not disclosed")
                continue
            overridden = res.get("overridden_clauses", []) if state == "approved_with_override" else None
            subjects = {payer_subject(pid, d.get("presented_hash") or d.get("proposal_hash"), overridden) for d in first}
            if a.get("subject_hash") not in subjects:
                bound_all = False
                payer_bad(f"resolution {rid}: the payer did not sign this proposal as shown"
                          + (" with these clauses overridden" if overridden is not None else ""))
        elif state == "proceeded_on_silence":
            det = dets_by_id.get(res.get("determination_id"))
            if det is None:
                if must_disclose:
                    payer_bad(f"resolution {rid}: the decision that allowed silence is not disclosed")
            elif det.get("approval_mode") != "silent_with_notice" or det.get("proposal_id") != pid:
                payer_bad(f"resolution {rid}: proceeded on silence where a tap was required")
    if used_assertions:
        report["presented_hash"] = "matches" if bound_all and report["payer_signatures"] != "failed" else "differs"

    # Money moved or an order was placed: something must have authorised it, and in a full bundle it must be shown.
    authorised = {r.get("determination_id") for r in records if r.get("record_type") == "resolution"
                  and r.get("state") in PAYER_STATES + ("proceeded_on_silence",)}
    if must_disclose:
        for r in records:
            ref = r.get("determination_id") if r.get("record_type") == "execution_attempt" else (
                r.get("payment_ref") if r.get("record_type") == "posting" and r.get("kind") == "hold" else None)
            if ref is not None and ref not in authorised:
                report["payer_signatures"] = "failed"
                bad(f"{r.get('record_type')} for {ref}: no approval or silent proceed is disclosed for it")
                break

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

    report["ok"] = not report["problems"]
    return report
