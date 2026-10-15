"""Phase 6: evidence bundles and the open verifier (06 §6)."""

import copy
import random

import pytest

from gap_evidence.bundle import EvidenceBuilder, classify, evaluator_digest
from gap_evidence.verify import verify_bundle
from gap_flow.testing import LINES, make_world
from gap_core.proposal import CartLine
from gap_flow.testing import inr


@pytest.fixture
def w(ledger, profile_conn):
    return make_world(ledger, profile_conn, evaluator_digest=evaluator_digest())


def paid(w, script=("ok",), lines=LINES):
    w.merchant.add_cart("cart-1", lines)
    w.merchant.script = list(script)
    p = w.propose(w.request())
    out = w.approve(p)
    w.pay_bank(out.result)
    w.flow.tick(sweep=True)
    return p


def builder(w):
    return EvidenceBuilder(w.flow.ledger, w.flow.keylog, w.flow.profile, w.clock, random.Random(5))


def test_a_payer_bundle_verifies_completely(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    r = verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())
    assert r["ok"], r["problems"]
    assert (r["replay"], r["presented_hash"], r["chain"], r["signatures"], r["keys"]) == ("reproduced", "matches", "ok", "ok", "ok")
    assert any(a["fact"] == "merchant.categories" and a["class"] == "capturer_signed" for a in r["attestations"])
    assert b["divergence"] == {"primary": "no_divergence", "contributing": [], "step_refs": []}


@pytest.mark.parametrize("attack", ["record", "skeleton", "input", "bundle", "key_log"])
def test_the_verifier_rejects_any_tampering(w, attack):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    t = copy.deepcopy(b)
    if attack == "record":
        next(r for r in t["records"] if r["record_type"] == "posting")["amount"]["amount_minor"] += 1
    elif attack == "skeleton":
        del t["skeleton"][1]
    elif attack == "input":
        det_id = next(iter(t["inputs"]))
        t["inputs"][det_id]["at"]["v"] = "2026-10-12T10:00:00+00:00"
    elif attack == "bundle":
        t["divergence"]["primary"] = "merchant_divergence"
    elif attack == "key_log":
        t["key_log"][0]["public_key"]["x"] = t["key_log"][0]["public_key"]["y"]
    assert not verify_bundle(t, w.host.public_jwk, evaluator=evaluator_digest())["ok"]


def test_a_different_evaluator_is_reported_not_trusted(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    assert verify_bundle(b, w.host.public_jwk, evaluator="sha256:" + "f" * 64)["replay"] == "evaluator_unpublished"


def test_a_merchant_sees_proof_of_authorisation_and_nothing_else(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="merchant", requester_ref="mrc_shop")
    assert {r["record_type"] for r in b["records"]} == {"resolution"}
    assert b["inputs"] == {}
    r = verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())
    assert r["ok"] and r["replay"] == "not_disclosed"


def test_every_release_is_logged(w):
    p = paid(w)
    bl = builder(w)
    bl.build("whl_1", p.proposal_id, role="merchant", requester_ref="mrc_shop")
    bl.build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    rel = w.flow.ledger.records("releases:host", "release")
    assert [(r["requester_role"], r["basis"]) for r in rel] == [("merchant", "default"), ("payer", "default")]


def test_after_shredding_the_bundle_still_verifies_without_inputs(w):
    p = paid(w)
    w.flow.profile.shred("pyr_1")
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    r = verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())
    assert r["ok"] and r["replay"] == "not_disclosed" and b["withheld"] == "payer data shredded"


def test_divergence_overcharge_points_to_the_merchant(w):
    p = paid(w, script=("overcharge:700",))
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    assert b["divergence"]["primary"] == "merchant_divergence"


def test_divergence_payer_approved_then_disputed(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1", claim="not_wanted")
    assert b["divergence"]["primary"] == "payer_authorised"


def test_divergence_primary_and_contributing(w):
    p = paid(w, script=("overcharge:700",))
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1", claim="not_wanted")
    assert b["divergence"]["primary"] == "payer_authorised" and b["divergence"]["contributing"] == ["merchant_divergence"]


def test_no_divergence_is_never_combined():
    assert classify([], None) == {"primary": "no_divergence", "contributing": [], "step_refs": []}


# --- payer consent rests on the device's signature, not the host's word (audit 2026-10-14) -----------------------

def test_the_payer_signature_is_in_the_bundle_and_verifies(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    asr = [r for r in b["records"] if r["record_type"] == "surface_assertion"]
    assert asr and all(a["device_proof"]["format"] == "raw_es256" and a["challenge"]["subject_hash"] == a["subject_hash"] for a in asr)
    assert verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())["payer_signatures"] == "ok"


def test_a_host_cannot_record_an_approval_the_payer_never_signed(w, monkeypatch):
    """A dishonest host signs every record properly but invents the payer's approval: the verifier catches it."""
    from gap_keys.local import LocalSigner

    real = w.flow.identity.verify_assertion
    forger = LocalSigner.generate("key_not_the_phone")

    def forged(assertion, **kw):
        info = real(assertion, **kw)
        info["device_proof"] = {"format": "raw_es256", "value": forger.sign(b"anything")}
        return info

    monkeypatch.setattr(w.flow.identity, "verify_assertion", forged)
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    r = verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())
    assert r["signatures"] == "ok" and r["chain"] == "ok"  # every host signature is genuine
    assert r["payer_signatures"] == "failed" and not r["ok"]


def test_an_approval_recorded_without_the_device_proof_does_not_verify(w, monkeypatch):
    real = w.flow.identity.verify_assertion

    def stripped(assertion, **kw):
        info = real(assertion, **kw)
        info.pop("device_proof")
        return info

    monkeypatch.setattr(w.flow.identity, "verify_assertion", stripped)
    p = paid(w)
    r = verify_bundle(builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1"), w.host.public_jwk, evaluator=evaluator_digest())
    assert r["payer_signatures"] == "failed" and not r["ok"]


def _resign(w, b):
    from gap_evidence.bundle import _bundle_bytes

    b = {k: v for k, v in b.items() if k != "signature"}
    b["signature"] = {"key_id": w.flow.keylog.signer.key_id, "alg": "ES256", "value": w.flow.keylog.signer.sign(_bundle_bytes(b))}
    return b


def test_a_host_cannot_hide_the_missing_payer_signature_by_leaving_the_assertion_out(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    b = _resign(w, {**b, "records": [r for r in b["records"] if r["record_type"] != "surface_assertion"]})
    r = verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())
    assert r["bundle_signature"] == "ok" and r["payer_signatures"] == "failed" and not r["ok"]


def test_a_merchant_bundle_without_assertions_still_verifies(w):
    p = paid(w)
    r = verify_bundle(builder(w).build("whl_1", p.proposal_id, role="merchant", requester_ref="mrc_shop"), w.host.public_jwk,
                      evaluator=evaluator_digest())
    assert r["ok"] and r["payer_signatures"] == "not_disclosed"


def test_an_override_bundle_binds_the_signature_to_the_overridden_clauses(w):
    w.merchant.add_cart("cart-1", LINES + (CartLine("item_eggs", 1, inr(900)), CartLine("item_jam", 1, inr(800))))
    p = w.propose(w.request())
    assert w.override(p, ["max_lines"]).status == "executed"
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    assert verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest())["payer_signatures"] == "ok"


def _verify(w, b, **kw):
    return verify_bundle(b, w.host.public_jwk, evaluator=evaluator_digest(), **kw)


def test_a_payer_signature_cannot_be_replayed_onto_another_proposal_with_the_same_cart(w, monkeypatch):
    w.merchant.add_cart("cart-1", LINES)
    w.merchant.add_cart("cart-2", LINES)
    a = w.propose(w.request("cart-1", "i-1"))
    real = w.flow.identity.verify_assertion
    kept = {}

    def capture(assertion, **kw):
        info = real(assertion, **kw)
        kept.setdefault("first", {k: info[k] for k in ("challenge", "device_proof")})
        return info

    monkeypatch.setattr(w.flow.identity, "verify_assertion", capture)
    w.approve(a)
    b = w.propose(w.request("cart-2", "i-2"))  # the same cart and total: the same presented hash

    def replay_first(assertion, **kw):
        info = real(assertion, **kw)
        return {**info, **kept["first"]}  # the host swaps in the payer's signature from the first proposal

    monkeypatch.setattr(w.flow.identity, "verify_assertion", replay_first)
    w.approve(b)
    r = _verify(w, builder(w).build("whl_1", b.proposal_id, role="payer", requester_ref="pyr_1"))
    assert r["payer_signatures"] == "failed" and not r["ok"]


def test_a_payer_bundle_hiding_the_decision_that_was_shown_fails(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    b = _resign(w, {**b, "inputs": {}, "records": [r for r in b["records"] if not (r["record_type"] == "determination" and r["stage"] == "proposal")]})
    r = _verify(w, b)
    assert r["payer_signatures"] == "failed" and not r["ok"]


def test_a_payer_bundle_relabelled_for_a_merchant_fails_when_the_payer_says_who_asked(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    b = _resign(w, {**b, "requester_role": "merchant", "records": [r for r in b["records"] if r["record_type"] != "surface_assertion"]})
    assert _verify(w, b)["ok"]  # alone, the label is the host's word
    r = _verify(w, b, expect_role="payer")
    assert r["payer_signatures"] == "failed" and not r["ok"]


def test_an_order_with_no_disclosed_approval_fails(w):
    p = paid(w)
    b = builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    b = _resign(w, {**b, "records": [r for r in b["records"] if r["record_type"] not in ("resolution", "surface_assertion")]})
    r = _verify(w, b)
    assert r["payer_signatures"] == "failed" and not r["ok"]


def test_a_payer_decline_bundle_verifies(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    w.decline(p)
    r = _verify(w, builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1"))
    assert r["ok"], r["problems"]
    assert r["payer_signatures"] == "ok"


def test_an_approval_must_resolve_the_decision_that_executed(w, monkeypatch):
    """A host records the payer's approval against the first-stage decision instead of the one that executed."""
    real = type(w.flow)._resolve

    def misbind(self, p, det_id, state, by, *a, **kw):
        if state == "approved":
            det_id = p.determination.determination_id  # the proposal-stage decision, not the authorisation one
        return real(self, p, det_id, state, by, *a, **kw)

    monkeypatch.setattr(type(w.flow), "_resolve", misbind)
    p = paid(w)
    r = _verify(w, builder(w).build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1"))
    assert r["payer_signatures"] == "failed" and not r["ok"]
