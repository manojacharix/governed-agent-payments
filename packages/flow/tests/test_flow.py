"""Phase 6 + 7: the whole lifecycle, from an agent's proven request to a settled payment."""

from datetime import timedelta

import pytest

from gap_core.canonical import canonical
from gap_core.codec import decode
from gap_core.evaluate import IntentConflict, Repeat, evaluate
from gap_core.hashing import hash_value
from gap_core.proposal import CartLine
from gap_flow.flow import Deployment, FlowError, Rejected
from gap_flow.testing import LINES, inr, make_world
from gap_identity.devices import Device, bearer_proof
from gap_ledger.money import replay


@pytest.fixture
def w(ledger, profile_conn):
    return make_world(ledger, profile_conn)


def test_propose_approve_execute_settle(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    assert p.status == "awaiting_approval" and p.determination.rail_id == "xx:upi"
    out = w.approve(p)
    assert out.status == "executed" and out.result.outcome == "success"
    w.pay_bank(out.result)
    w.flow.tick(sweep=True)
    assert replay(w.flow.ledger, "whl_1").payments[out.events[0].determination_id].captured > 0
    kinds = {r["record_type"] for r in w.flow.ledger.records("whl_1")}
    assert {"proposal", "determination", "surface_assertion", "resolution", "posting", "execution_attempt", "sweep", "finding"} <= kinds
    assert w.flow.ledger.verify_integrity(w.flow.keylog.public_keys()).ok


def test_stored_inputs_replay_to_the_same_bytes(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    rec = next(r for r in w.flow.ledger.records("whl_1", "determination") if r["determination_id"] == p.determination.determination_id)
    replayed = evaluate(decode(w.flow.profile.get(rec["input_ref"]))).to_record()
    assert canonical(replayed) == canonical({k: v for k, v in rec.items() if k not in ("chain", "signature", "input_hash", "input_ref")})


def test_an_adapter_registered_under_the_wrong_id_is_refused(w):
    dep = w.flow.deployments["whl_1"]
    with pytest.raises(FlowError, match="reports itself"):
        w.flow.register(Deployment(dep.wheel, dep.spokes, dep.ctx, {"fake-x": w.merchant}, w.bank))


# --- who is proposing --------------------------------------------------------------------------------

def test_an_unproven_or_replayed_request_never_reaches_the_evaluator(w):
    w.merchant.add_cart("cart-1", LINES)
    req = w.request()
    assert w.flow.propose("whl_1", req, bearer_proof("not-a-token", w.clock)) == Rejected("sys.proof_invalid")
    assert w.propose(req, nonce="n-1").status == "awaiting_approval"
    assert w.propose(w.request(intent="i-2"), nonce="n-1") == Rejected("sys.nonce_reused")
    stale = w.propose(w.request(intent="i-3"), created_at=w.clock.now() - timedelta(minutes=10))
    assert stale == Rejected("sys.proof_stale")
    assert len(w.flow.ledger.records("whl_1", "proposal")) == 1  # nothing recorded for the refused ones


def test_the_caller_is_who_the_proof_says_not_who_the_request_claims(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    assert p.request.caller.caller_id != "unproven" and p.request.caller.assurance == "session_bound"


# --- the payer's actions -------------------------------------------------------------------------------

def test_a_forged_misdirected_or_replayed_assertion_is_refused(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    stranger = Device("key_stranger")
    with pytest.raises(FlowError, match="key_not_allowed"):
        w.flow.approve(p.proposal_id, stranger.respond(w.flow.challenge(p.proposal_id, "authorise")))
    wrong = w.flow.challenge(p.proposal_id, "decline")  # signed for a different action
    with pytest.raises(FlowError, match="challenge_mismatch"):
        w.flow.approve(p.proposal_id, w.device.respond(wrong))
    a = w.respond(p, "authorise")
    w.flow.approve(p.proposal_id, a)
    w.merchant.add_cart("cart-2", LINES[:1])
    q = w.propose(w.request("cart-2", "i-2"))
    with pytest.raises(FlowError, match="challenge_mismatch|challenge_reused"):
        w.flow.approve(q.proposal_id, a)  # an old approval cannot be reused


def test_price_change_before_approval_is_re_presented_never_executed(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    w.merchant.set_price("cart-1", "item_milk", inr(6100))
    new = w.approve(p)
    assert w.flow.proposals[p.proposal_id].status == "superseded"
    assert new.status == "awaiting_approval" and new.determination.presented_hash != p.determination.presented_hash
    assert w.merchant.place_calls == 0


def test_override_of_a_payer_rule_for_one_payment(w):
    w.merchant.add_cart("cart-1", LINES + (CartLine("item_eggs", 1, inr(900)), CartLine("item_jam", 1, inr(800))))
    p = w.propose(w.request())
    assert p.determination.outcome == "refuse" and p.status == "awaiting_approval"
    out = w.override(p, ["max_lines"])
    assert out.status == "executed"
    res = next(r for r in w.flow.ledger.records("whl_1", "resolution") if r["proposal_id"] == p.proposal_id)
    assert res["state"] == "approved_with_override" and res["overridden_clauses"] == ["max_lines"]


def test_decline_and_expiry(w):
    w.merchant.add_cart("cart-1", LINES)
    w.merchant.add_cart("cart-2", LINES[:1])
    a = w.propose(w.request())
    w.decline(a)
    b = w.propose(w.request("cart-2", "i-2"))
    w.clock.advance(timedelta(minutes=16))
    w.flow.tick()
    assert (a.status, b.status) == ("declined", "expired") and w.merchant.place_calls == 0


def test_same_intent_twice(w):
    w.merchant.add_cart("cart-1", LINES)
    w.propose(w.request())
    assert isinstance(w.propose(w.request()), Repeat)
    w.merchant.add_cart("cart-2", LINES[:1])
    assert isinstance(w.propose(w.request("cart-2")), IntentConflict)


# --- silent repeats ----------------------------------------------------------------------------------

def repeat_request(w, n="w1"):
    w.merchant.add_cart(f"weekly-{n}", LINES)
    return w.request(f"weekly-{n}", f"si:spk_repeat:{n}", spoke="spk_repeat")


def test_silent_repeat_runs_after_a_delivered_notice(w):
    p = w.propose(repeat_request(w))
    assert p.status == "notice" and p.request.caller.assurance == "key_bound"
    w.flow.notice_delivered(p.proposal_id)
    w.clock.advance(timedelta(minutes=6))
    w.flow.tick()
    assert p.status == "executed"
    res = next(r for r in w.flow.ledger.records("whl_1", "resolution") if r["proposal_id"] == p.proposal_id)
    assert res["state"] == "proceeded_on_silence" and res["by"] == "system" and "assertion_id" not in res


def test_undelivered_notice_falls_back_to_a_tap(w):
    p = w.propose(repeat_request(w))
    w.clock.advance(timedelta(minutes=6))
    w.flow.tick()
    assert p.status == "awaiting_approval" and w.merchant.place_calls == 0


def test_payer_can_cancel_during_the_notice(w):
    p = w.propose(repeat_request(w))
    w.decline(p)
    w.clock.advance(timedelta(minutes=6))
    w.flow.tick()
    assert p.status == "declined" and w.merchant.place_calls == 0


def test_an_open_overcapture_forces_taps_on_that_spoke_only(w):
    w.merchant.add_cart("cart-1", LINES)
    w.merchant.script = ["overcharge:500"]
    out = w.approve(w.propose(w.request()))
    w.pay_bank(out.result)
    w.flow.tick(sweep=True)
    assert w.flow.recon.open_tap_findings_by_spoke("whl_1") == {"spk_grocery": 1}
    w.clock.advance(timedelta(minutes=11))  # past the duplicate window
    assert w.propose(repeat_request(w, "w2")).status == "notice"


# --- identity events change what can be spent ----------------------------------------------------------

def test_a_compromised_device_suspends_spending_until_another_device_resumes(w):
    tablet = Device("key_tablet")
    i = w.flow.identity
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=i.key_subject(tablet.key_id, tablet.public_jwk, "surface"))
    i.enrol_key("pyr_1", tablet.key_id, tablet.public_jwk, w.device.respond(ch))
    i.report_compromise(w.device.key_id, w.clock.now())
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    assert p.determination.outcome == "no_authority"
    i.resume("pyr_1", tablet.respond(i.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"resume": "pyr_1"}))))
    w.merchant.add_cart("cart-2", LINES)
    assert w.propose(w.request("cart-2", "i-2")).determination.outcome == "allow"


def test_no_payment_while_a_delayed_recovery_is_pending(w):
    i = w.flow.identity
    i.set_recovery_questions("pyr_1", ["one two three", "four five six", "seven eight"],
                             w.device.respond(i.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"recovery_questions": "pyr_1"}))))
    new = Device("key_new_phone")
    i.start_recovery("pyr_1", new_key_id=new.key_id, new_jwk=new.public_jwk, new_key_user_verification="inherence",
                     answers=["one two three", "four five six", "seven eight"], external_verified=True)
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    assert p.determination.outcome == "refuse" and "sys.recovery_pending" in {c.rule_id for c in p.determination.failing_clauses}
