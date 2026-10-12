"""Evidence bundles (06): a machine-verifiable answer to "who authorised this, and was it followed?"

build() collects the signed records for one payment, a hash-only skeleton of the wheel chain around
them (so the records can be placed in the chain without disclosing anyone else's), the key log, and,
for roles allowed to see them, the exact evaluation inputs so the decision can be replayed. It names
one primary divergence and any contributing ones, signs the bundle, and logs the release (06 §4).
verify() (in verify.py) needs nothing but the bundle, a pinned host key and the evaluator.
"""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

from gap_core.chain import record_hash
from gap_core.clock import Clock
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.timefmt import format_ts
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_profile.store import ProfileStore, Shredded

ROLES = ("payer", "operator", "merchant", "rail", "regulator")
SCOPE = {  # 06 §4: what each requester sees by default
    "payer": {"proposal", "determination", "surface_assertion", "resolution", "posting", "execution_attempt", "finding", "sweep"},
    "regulator": {"proposal", "determination", "surface_assertion", "resolution", "posting", "execution_attempt", "finding", "sweep"},
    "operator": {"determination", "resolution"},
    "merchant": {"resolution"},
    "rail": {"resolution", "execution_attempt", "posting"},
}
INPUTS_FOR = ("payer", "regulator")
CATEGORIES = ("rule_permitted_wrongly", "unmodelled_dimension", "evaluator_fault", "poor_proposal_within_rules", "payer_authorised",
              "fact_misreported", "merchant_divergence", "rail_divergence", "external", "no_divergence")


def evaluator_digest() -> str:
    """Digest of the evaluator's source (gap_core): stands in for a reproducible build digest (07 §7)."""
    root = Path(str(resources.files("gap_core")))
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        if f.name == "testing.py":
            continue
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return "sha256:" + h.hexdigest()


def related(records: list[dict], proposal_id: str) -> list[dict]:
    """Every record about one proposal: its decisions, the payer's assertions, resolutions, money, execution, findings."""
    dets = {r["determination_id"] for r in records if r["record_type"] == "determination" and r.get("proposal_id") == proposal_id}
    res = [r for r in records if r["record_type"] == "resolution" and r.get("proposal_id") == proposal_id]
    asr = {r.get("assertion_id") for r in res}
    fnd = {r["finding_id"] for r in records if r["record_type"] == "finding" and r.get("payment_ref") in dets}
    out = []
    for r in records:
        t = r["record_type"]
        if ((t == "proposal" and r["proposal_id"] == proposal_id) or (t == "determination" and r["determination_id"] in dets)
                or r in res or (t == "surface_assertion" and r["assertion_id"] in asr)
                or (t == "posting" and r["payment_ref"] in dets) or (t == "execution_attempt" and r["determination_id"] in dets)
                or (t == "finding" and r["finding_id"] in fnd) or (t == "sweep" and set(r.get("finding_ids", [])) & fnd)):
            out.append(r)
    return out


def classify(recs: list[dict], claim: str | None = None, replay_ok: bool = True) -> dict:
    """06 §2: the step where authority and reality first diverged (primary) and any later ones (contributing)."""
    steps: list[tuple[int, str, str]] = []
    res = [r for r in recs if r["record_type"] == "resolution"]
    if not replay_ok:
        steps.append((0, "evaluator_fault", "replay"))
    for r in res:
        if claim == "not_wanted":
            if r["state"] in ("approved", "approved_with_override"):
                steps.append((1, "payer_authorised", r["resolution_id"]))
            elif r["state"] == "proceeded_on_silence":
                steps.append((1, "rule_permitted_wrongly", r["resolution_id"]))
    for r in recs:
        if r["record_type"] != "finding" or r["status"] != "open" and r.get("closed_by") != "payer_acceptance":
            continue
        k = r["kind"]
        if k in ("overcapture", "item_mismatch", "missing_order"):
            steps.append((3, "merchant_divergence", r["finding_id"]))
        elif k == "source_conflict":
            steps.append((2, "rail_divergence", r["finding_id"]))
        elif k == "external_order":
            steps.append((4, "external", r["finding_id"]))
    if not steps:
        return {"primary": "no_divergence", "contributing": [], "step_refs": []}
    steps.sort()
    primary = steps[0][1]
    contributing = []
    for _, c, _ref in steps[1:]:
        if c != primary and c not in contributing:
            contributing.append(c)
    return {"primary": primary, "contributing": contributing, "step_refs": [ref for _, _, ref in steps]}


class EvidenceBuilder:
    def __init__(self, ledger: Ledger, keylog: KeyLog, profile: ProfileStore, clock: Clock, rng) -> None:
        self.ledger, self.keylog, self.profile, self.clock, self.rng = ledger, keylog, profile, clock, rng

    def build(self, wheel_id: str, proposal_id: str, *, role: str, requester_ref: str, basis: str = "default",
              claim: str | None = None) -> dict:
        if role not in ROLES:
            raise ValueError(f"unknown role {role}")
        chain = self.ledger.records(wheel_id)
        rel = related(chain, proposal_id)
        if not rel:
            raise ValueError(f"no records for {proposal_id}")
        scope = SCOPE[role] if basis == "default" else SCOPE["payer"]  # consent or lawful demand widens the scope
        disclosed = [r for r in rel if r["record_type"] in scope]
        lo, hi = min(r["chain"]["seq"] for r in rel), chain[-1]["chain"]["seq"]
        skeleton = [[r["chain"]["seq"], r["chain"]["previous_hash"], record_hash(r)] for r in chain if lo <= r["chain"]["seq"] <= hi]
        inputs, withheld = {}, None
        if role in INPUTS_FOR or basis != "default":
            for r in disclosed:
                if r["record_type"] == "determination" and "input_ref" in r:
                    try:
                        inputs[r["determination_id"]] = self.profile.get(r["input_ref"])
                    except Shredded:
                        withheld = "payer data shredded"
        bundle = {
            "bundle_id": new_id("bnd", clock=self.clock, rng=self.rng), "wheel_id": wheel_id, "proposal_id": proposal_id,
            "requester_role": role, "basis": basis, "records": disclosed, "skeleton": skeleton,
            "key_log": self.ledger.records(self.keylog.chain_id), "inputs": inputs, "evaluator": evaluator_digest(),
            "divergence": classify(rel, claim), "generated_at": format_ts(self.clock.now()),
        }
        if withheld:
            bundle["withheld"] = withheld
        bundle["signature"] = {"key_id": self.keylog.signer.key_id, "alg": "ES256",
                               "value": self.keylog.signer.sign(_bundle_bytes(bundle))}
        rid = new_id("rls", clock=self.clock, rng=self.rng)
        self.ledger.append(f"releases:{self.keylog.host_ref}", {
            "record_type": "release", "release_id": rid, "bundle_hash": hash_value(bundle), "requester_role": role,
            "requester_ref": requester_ref, "scope": sorted(scope), "basis": basis, "released_at": format_ts(self.clock.now()),
        }, self.keylog.signer, record_id=rid)
        return bundle


def _bundle_bytes(bundle: dict) -> bytes:
    from gap_core.canonical import canonical
    return canonical({k: v for k, v in bundle.items() if k != "signature"})
