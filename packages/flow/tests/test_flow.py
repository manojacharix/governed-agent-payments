"""Phase 6: the whole lifecycle, from an agent's request to a settled payment."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from gap_adapters.fakes import FakeMerchant
from gap_core.authority import Caller, CartLineTemplate, Limit, RailProfile, Spoke, StandingInstruction, Wheel, Window
from gap_core.clock import FixedClock
from gap_core.codec import decode
from gap_core.evaluate import IntentConflict, Repeat, evaluate
from gap_core.canonical import canonical
from gap_core.facts import Fact, TermSet
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee
from gap_core.testing import PARAMS, rule
from gap_flow.flow import Deployment, Flow, FlowError, ProposalRequest
from gap_flow.surface import ReferenceSurface
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_ledger.money import replay
from gap_profile.store import LocalKek, ProfileStore
from gap_recon.recon import BankFeed

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
M = Window("calendar_month", "Etc/UTC")
inr = lambda x: Money(x, "INR")  # noqa: E731
UPI = RailProfile("xx:upi", "human_per_payment", "bound_at_rail", ("explicit_tap",))
WALLET = RailProfile("xx:wallet", "none", "not_bound", ("explicit_tap", "silent_with_notice"))
LINES = (CartLine("item_milk", 2, inr(6000)), CartLine("item_bread", 1, inr(4500)))
CAT = Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "std:mcc@1", "m@1"), "merchant_adapter", True, "capturer_signed")
AGENT = Caller("clr_agent", "session_bound")
SCHED = Caller("clr_sched", "key_bound")


class FakeMerchantXX(FakeMerchant):
    """fake-b offering this test jurisdiction's rails."""

    def describe(self):
        from dataclasses import replace
        return replace(super().describe(), rails_offered=("xx:upi", "xx:wallet"))


@pytest.fixture
def world(ledger, profile_conn):
    clock = FixedClock(T0)
    host = LocalSigner.generate("key_host")
    keylog = KeyLog(ledger, "host", host, clock)
    keylog.bootstrap()
    flow = Flow(ledger, ProfileStore(profile_conn, LocalKek.generate(), clock), keylog, clock, random.Random(1), evaluator_digest="sha256:" + "e" * 64)
    m = FakeMerchantXX("b", clock)
    ins = StandingInstruction("mrc_shop", "fake-b", (CartLineTemplate("item_milk", 2), CartLineTemplate("item_bread", 1)), "dst_home", "xx:wallet", 1000, inr(19000))
    spokes = {
        "spk_grocery": Spoke("spk_grocery", "whl_1", "explicit_tap", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=("clr_agent",), caps=(Limit(inr(400000), M),),
                             rules=(rule("max_lines", "items", max_lines=3),)),
        "spk_repeat": Spoke("spk_repeat", "whl_1", "silent_with_notice", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=("clr_sched",), instruction=ins,
                            caps=(Limit(inr(100000), M),)),
    }
    params = PARAMS.__class__(**{**PARAMS.__dict__, "silent_max_amount": inr(200000),
                                 "silent_preconditions": ({"fact": "notice.delivered", "op": "eq", "value": True},)})
    bank = BankFeed()
    dep = Deployment(Wheel("whl_1", "pyr_1", "xx", budgets=(Limit(inr(1500000), M),)), spokes,
                     {"params": params, "rails": {"xx:upi": UPI, "xx:wallet": WALLET}, "tier0_rules": (), "jurisdiction_rules": (), "jurisdiction": "xx"},
                     {"fake-b": m}, bank)
    flow.register(dep)
    surface = ReferenceSurface("key_surface", "pyr_1", clock)
    flow.enrol_surface("whl_1", surface)
    return flow, m, surface, clock, bank


def request(cart="cart-1", intent="i-1", caller=AGENT, spoke="spk_grocery", **kw):
    return ProposalRequest(spoke, caller, intent, "mrc_shop", "fake-b", cart, "dst_home", kw.pop("confidence", 950), facts=(CAT,), **kw)


def add_cart(m, cart="cart-1", lines=LINES):
    m.add_cart(cart, lines)


def pay_bank(m, bank, res):
    o = m.get_order(res.merchant_order_ref)
    bank.debit(f"bank-{o.merchant_order_ref}", o.merchant_order_ref, o.charged, T0)


def test_propose_approve_execute_settle(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    p = flow.propose("whl_1", request())
    assert p.status == "awaiting_approval" and p.determination.rail_id == "xx:upi"
    out = flow.approve(p.proposal_id, surface.sign("authorise", p.proposal_id, p.determination.presented_hash))
    assert out.status == "executed" and out.result.outcome == "success"
    pay_bank(m, bank, out.result)
    flow.tick(sweep=True)
    det2 = out.events[0].determination_id
    assert replay(flow.ledger, "whl_1").payments[det2].captured > 0
    kinds = [r["record_type"] for r in flow.ledger.records("whl_1")]
    for k in ("proposal", "determination", "surface_assertion", "resolution", "posting", "execution_attempt", "sweep", "finding"):
        assert k in kinds
    assert flow.ledger.verify_integrity(flow.keylog.public_keys()).ok


def test_an_adapter_registered_under_the_wrong_id_is_refused(world):
    flow, m, surface, clock, bank = world
    from gap_flow.flow import Deployment
    dep = flow.deployments["whl_1"]
    with pytest.raises(FlowError, match="reports itself"):
        flow.register(Deployment(dep.wheel, dep.spokes, dep.ctx, {"fake-x": m}, bank))


def test_stored_inputs_replay_to_the_same_bytes(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    p = flow.propose("whl_1", request())
    rec = next(r for r in flow.ledger.records("whl_1", "determination") if r["determination_id"] == p.determination.determination_id)
    inp = decode(flow.profile.get(rec["input_ref"]))
    replayed = evaluate(inp).to_record()
    stored = {k: v for k, v in rec.items() if k not in ("chain", "signature", "input_hash", "input_ref")}
    assert canonical(replayed) == canonical(stored)


def test_a_forged_or_misdirected_assertion_is_refused(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    p = flow.propose("whl_1", request())
    stranger = ReferenceSurface("key_stranger", "pyr_1", clock)  # never enrolled
    with pytest.raises(FlowError):
        flow.approve(p.proposal_id, stranger.sign("authorise", p.proposal_id, p.determination.presented_hash))
    with pytest.raises(FlowError):
        flow.approve(p.proposal_id, surface.sign("authorise", p.proposal_id, "sha256:" + "0" * 64))  # not what was shown
    a = surface.sign("authorise", p.proposal_id, p.determination.presented_hash)
    a["value"] = surface.sign("authorise", "prp_other", p.determination.presented_hash)["value"]
    with pytest.raises(FlowError):
        flow.approve(p.proposal_id, a)


def test_price_change_before_approval_is_re_presented_never_executed(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    p = flow.propose("whl_1", request())
    m.set_price("cart-1", "item_milk", inr(6100))
    new = flow.approve(p.proposal_id, surface.sign("authorise", p.proposal_id, p.determination.presented_hash))
    assert flow.proposals[p.proposal_id].status == "superseded"
    assert new.status == "awaiting_approval" and new.determination.presented_hash != p.determination.presented_hash
    assert m.place_calls == 0


def test_override_of_a_payer_rule_for_one_payment(world):
    flow, m, surface, clock, bank = world
    add_cart(m, lines=LINES + (CartLine("item_eggs", 1, inr(900)), CartLine("item_jam", 1, inr(800))))
    p = flow.propose("whl_1", request())
    assert p.determination.outcome == "refuse" and p.status == "awaiting_approval"
    subject = __import__("gap_core.hashing", fromlist=["hash_value"]).hash_value({"presented_hash": p.determination.presented_hash, "overridden": ["max_lines"]})
    out = flow.override(p.proposal_id, ["max_lines"], surface.sign("override", p.proposal_id, subject))
    assert out.status == "executed"
    res = [r for r in flow.ledger.records("whl_1", "resolution") if r["proposal_id"] == p.proposal_id][0]
    assert res["state"] == "approved_with_override" and res["overridden_clauses"] == ["max_lines"]


def test_decline_and_expiry(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    add_cart(m, "cart-2")
    a = flow.propose("whl_1", request())
    flow.decline(a.proposal_id, surface.sign("decline", a.proposal_id, a.determination.presented_hash))
    b = flow.propose("whl_1", request("cart-2", "i-2"))
    clock.advance(timedelta(minutes=16))
    flow.tick()
    assert (a.status, b.status) == ("declined", "expired") and m.place_calls == 0


def test_same_intent_twice(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    flow.propose("whl_1", request())
    assert isinstance(flow.propose("whl_1", request()), Repeat)
    add_cart(m, "cart-2", LINES[:1])
    assert isinstance(flow.propose("whl_1", request("cart-2")), IntentConflict)


def test_silent_repeat_runs_after_a_delivered_notice(world):
    flow, m, surface, clock, bank = world
    add_cart(m, "weekly")
    p = flow.propose("whl_1", request("weekly", "si:spk_repeat:2026-10-12", caller=SCHED, spoke="spk_repeat"))
    assert p.status == "notice"
    flow.notice_delivered(p.proposal_id)
    clock.advance(timedelta(minutes=6))
    flow.tick()
    assert p.status == "executed"
    res = [r for r in flow.ledger.records("whl_1", "resolution") if r["proposal_id"] == p.proposal_id][0]
    assert res["state"] == "proceeded_on_silence" and res["by"] == "system" and "assertion_id" not in res


def test_undelivered_notice_falls_back_to_a_tap(world):
    flow, m, surface, clock, bank = world
    add_cart(m, "weekly")
    p = flow.propose("whl_1", request("weekly", "si:spk_repeat:2026-10-12", caller=SCHED, spoke="spk_repeat"))
    clock.advance(timedelta(minutes=6))
    flow.tick()
    assert p.status == "awaiting_approval" and m.place_calls == 0


def test_payer_can_cancel_during_the_notice(world):
    flow, m, surface, clock, bank = world
    add_cart(m, "weekly")
    p = flow.propose("whl_1", request("weekly", "si:spk_repeat:2026-10-12", caller=SCHED, spoke="spk_repeat"))
    flow.decline(p.proposal_id, surface.sign("decline", p.proposal_id, p.determination.presented_hash))
    clock.advance(timedelta(minutes=6))
    flow.tick()
    assert p.status == "declined" and m.place_calls == 0


def test_an_open_overcapture_forces_taps_on_that_spoke(world):
    flow, m, surface, clock, bank = world
    add_cart(m)
    m.script = ["overcharge:500"]
    p = flow.propose("whl_1", request())
    out = flow.approve(p.proposal_id, surface.sign("authorise", p.proposal_id, p.determination.presented_hash))
    pay_bank(m, bank, out.result)
    flow.tick(sweep=True)
    assert flow.recon.open_tap_findings_by_spoke("whl_1") == {"spk_grocery": 1}
    # the weekly repeat is on another spoke and still runs silently (past the duplicate window)
    clock.advance(timedelta(minutes=11))
    add_cart(m, "weekly")
    q = flow.propose("whl_1", request("weekly", "si:spk_repeat:w2", caller=SCHED, spoke="spk_repeat"))
    assert q.status == "notice"
