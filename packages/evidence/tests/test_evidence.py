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
    p = w.flow.propose("whl_1", w.request())
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
