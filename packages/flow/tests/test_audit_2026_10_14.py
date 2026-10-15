"""Regressions for the 14 October 2026 audit: each loophole reproduced on 4c7f590, now closed.

1. The cited merchant was never bound to the adapter or cart, so an allowlist could be bypassed.
2. A silent repeat could be redirected to another adapter without a tap.
3. Rolling windows never accumulated (each hold got its own window id).
4. A velocity or aggregate rule whose window differed from the cap's always refused (usage unknown).
5. A second-currency payment passed evaluation, then the hold used the first budget and cap.
6. A raw signature's user-verification claim was not signed, yet set the strength achieved.
7. A negative extra lowered the total every limit was checked against.
"""

import random
from dataclasses import replace
from datetime import timedelta

import pytest

from gap_core.authority import Limit, Window
from gap_core.clock import FixedClock
from gap_core.money import Money
from gap_core.proposal import CartLine, Extra
from gap_core.testing import rule
from gap_flow.flow import FlowError, Invalid
from gap_flow.testing import LINES, T0, XXMerchant, inr, make_world
from gap_identity.devices import Device
from gap_keys.local import LocalSigner
from gap_ledger.money import Account, HoldRefused, MoneyInFlight, replay

HOUR = Window("rolling", "Etc/UTC", 3600)
DAY = Window("calendar_day", "Etc/UTC")


@pytest.fixture
def w(ledger, profile_conn):
    return make_world(ledger, profile_conn)


class OtherAdapter(XXMerchant):
    def describe(self):
        return replace(super().describe(), adapter_id="fake-other")


def _other(w, merchant_ref):
    m = OtherAdapter("b", w.clock, merchant_ref=merchant_ref)
    w.flow.deployments["whl_1"].adapters["fake-other"] = m
    return m


def _propose_later(w, first, cart, intent):
    """Propose as the same proven caller after the agent's 15-minute session has lapsed (the session is not the subject)."""
    with w.flow.store.unit("pyr_1"):
        return w.flow._propose_authenticated("whl_1", replace(w.request(cart, intent), caller=first.request.caller))


def _set_spoke(w, spoke_id, **changes):
    dep = w.flow.deployments["whl_1"]
    dep.spokes[spoke_id] = replace(dep.spokes[spoke_id], **changes)


# --- 1. the merchant is the one the cart is at --------------------------------------------------------

def test_citing_an_allowed_merchant_through_another_adapter_is_refused(w):
    s = w.flow.deployments["whl_1"].spokes["spk_grocery"]
    _set_spoke(w, "spk_grocery", rules=s.rules + (rule("only_shop", "merchant", allow=["mrc_shop"]),))
    other = _other(w, "mrc_elsewhere")
    other.add_cart("cart-x", LINES)
    out = w.propose(replace(w.request("cart-x", "i-x"), adapter_id="fake-other"))  # cites mrc_shop, cart is elsewhere
    assert isinstance(out, Invalid) and out.code == "merchant_mismatch"
    assert other.place_calls == 0 and not list(w.flow.ledger.records("whl_1", "proposal"))


def test_a_marketplace_cart_at_another_seller_is_refused(w):
    w.merchant.add_cart("cart-x", LINES, merchant_ref="mrc_blocked_seller")  # same adapter, different seller
    out = w.propose(w.request("cart-x", "i-x"))
    assert isinstance(out, Invalid) and out.code == "merchant_mismatch"


def test_a_cart_that_moves_seller_after_it_was_shown_never_executes(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(w.request())
    w.merchant.carts["cart-1"].merchant_ref = "mrc_elsewhere"
    out = w.approve(p)
    assert out.status == "refused" and w.merchant.place_calls == 0


# --- 2. a silent repeat cannot be redirected ------------------------------------------------------------

def test_a_silent_repeat_through_another_adapter_falls_back_and_never_orders_silently(w):
    other = _other(w, "mrc_shop")  # even an adapter that also fronts the same merchant
    other.add_cart("weekly-x", LINES)
    req = replace(w.request("weekly-x", "si:spk_repeat:x", spoke="spk_repeat"), adapter_id="fake-other")
    p = w.propose(req)
    w.flow.notice_delivered(p.proposal_id)
    w.clock.advance(timedelta(minutes=6))
    w.flow.tick()
    assert w.flow.proposals[p.proposal_id].status != "executed" and other.place_calls == 0


def test_the_instructed_adapter_still_runs_silently(w):
    w.merchant.add_cart("weekly-1", LINES)
    p = w.propose(w.request("weekly-1", "si:spk_repeat:1", spoke="spk_repeat"))
    assert p.status == "notice"
    w.flow.notice_delivered(p.proposal_id)
    w.clock.advance(timedelta(minutes=6))
    w.flow.tick()
    assert w.flow.proposals[p.proposal_id].status == "executed"


# --- 3. rolling windows accumulate -----------------------------------------------------------------------

def test_rolling_window_holds_accumulate_and_expire(ledger):
    clock = FixedClock(T0)
    mif = MoneyInFlight(ledger, LocalSigner.generate("key_host"), clock, random.Random(1))
    accts = [Account("spk_a", HOUR, inr(5000))]
    mif.hold("whl_1", "det_1", inr(3000), accts)
    clock.advance(timedelta(minutes=1))
    with pytest.raises(HoldRefused):
        mif.hold("whl_1", "det_2", inr(3000), accts)  # 6000 in one hour against 5000
    mif.hold("whl_1", "det_3", inr(2000), accts)      # exactly at the cap
    clock.advance(timedelta(minutes=59, seconds=1))    # det_1 has left the window, det_3 has not
    mif.hold("whl_1", "det_4", inr(3000), accts)
    with pytest.raises(HoldRefused):
        mif.hold("whl_1", "det_5", inr(1), accts)
    assert replay(ledger, "whl_1").usage_in("spk_a", HOUR, clock.now()).amounts == {"INR": 5000}


def test_a_rolling_cap_refuses_at_proposal_once_spent(w):
    _set_spoke(w, "spk_grocery", caps=(Limit(inr(30000), HOUR),))
    w.merchant.add_cart("cart-1", LINES)  # 19000 with the fee
    first = w.propose(w.request())
    assert w.approve(first).status == "executed"
    w.merchant.add_cart("cart-2", LINES)
    second = w.propose(w.request("cart-2", "i-2"))
    assert second.determination.outcome == "refuse"
    assert [c.rule_id for c in second.determination.failing_clauses] == ["sys.cap_exceeded"]
    w.clock.advance(timedelta(hours=1, seconds=1))
    w.merchant.add_cart("cart-3", LINES)
    assert _propose_later(w, first, "cart-3", "i-3").determination.outcome == "allow"


# --- 4. rule windows that differ from the cap's ------------------------------------------------------------

def test_a_daily_velocity_rule_counts_under_a_monthly_cap(w):
    s = w.flow.deployments["whl_1"].spokes["spk_grocery"]
    _set_spoke(w, "spk_grocery", rules=s.rules + (rule("two_a_day", "velocity", max_count=2, window=DAY),))
    for n in (1, 2):
        w.merchant.add_cart(f"cart-{n}", LINES)
        p = w.propose(w.request(f"cart-{n}", f"i-{n}"))
        assert p.determination.outcome == "allow"
        assert w.approve(p).status == "executed"
    w.merchant.add_cart("cart-3", LINES)
    third = w.propose(w.request("cart-3", "i-3"))
    assert [c.rule_id for c in third.determination.failing_clauses] == ["two_a_day"]
    w.clock.advance(timedelta(days=1))
    w.merchant.add_cart("cart-4", LINES)
    assert _propose_later(w, third, "cart-4", "i-4").determination.outcome == "allow"


def test_a_daily_aggregate_rule_counts_under_a_monthly_cap(w):
    s = w.flow.deployments["whl_1"].spokes["spk_grocery"]
    agg = rule("daily_spend", "amount", aggregate={"limit": inr(30000), "window": DAY})
    _set_spoke(w, "spk_grocery", rules=s.rules + (agg,))
    w.merchant.add_cart("cart-1", LINES)
    assert w.approve(w.propose(w.request())).status == "executed"
    w.merchant.add_cart("cart-2", LINES)
    second = w.propose(w.request("cart-2", "i-2"))
    assert [c.rule_id for c in second.determination.failing_clauses] == ["daily_spend"]


# --- 5. a second currency holds against its own cap and budget ----------------------------------------------

def test_a_second_currency_holds_against_its_own_cap_and_budget(w):
    dep = w.flow.deployments["whl_1"]
    month = Window("calendar_month", "Etc/UTC")
    dep.wheel = replace(dep.wheel, budgets=dep.wheel.budgets + (Limit(Money(500000, "GBP"), month),))
    _set_spoke(w, "spk_grocery", caps=dep.spokes["spk_grocery"].caps + (Limit(Money(100000, "GBP"), month),))
    gbp = XXMerchant("b", w.clock, currency="GBP")
    dep.adapters["fake-b"] = gbp
    gbp.add_cart("cart-g", (replace(LINES[0], unit_price=Money(500, "GBP")),))
    p = w.propose(w.request("cart-g", "i-g"))
    assert p.determination.outcome == "allow"
    out = w.approve(p)
    assert out.status == "executed" and gbp.place_calls == 1
    held = {(k[0], k[1]) for k in replay(w.flow.ledger, "whl_1").accounts}
    assert held == {("spk_grocery", "GBP"), ("whl_1", "GBP")}


# --- 6. user verification is what the key enforces, not what the assertion says --------------------------------

def _enrol_surface(w, device, uv):
    i = w.flow.identity
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=i.key_subject(device.key_id, device.public_jwk, "surface"))
    i.enrol_key("pyr_1", device.key_id, device.public_jwk, w.device.respond(ch), user_verification=uv)


def _over_lines(w):
    w.merchant.add_cart("cart-1", LINES + (CartLine("item_eggs", 1, inr(900)), CartLine("item_jam", 1, inr(800))))
    p = w.propose(w.request())
    assert p.status == "awaiting_approval"
    return p


def test_a_key_without_user_verification_cannot_claim_a_biometric(w):
    weak = Device("key_weak", user_verification="inherence")  # the device lies: the platform never checks the user
    _enrol_surface(w, weak, "none")
    p = _over_lines(w)
    with pytest.raises(FlowError, match="strength_too_low"):
        w.flow.override(p.proposal_id, ["max_lines"], weak.respond(w.flow.challenge(p.proposal_id, "override", ["max_lines"])))
    assert w.merchant.place_calls == 0


def test_stripping_the_claim_does_not_weaken_a_verified_key(w):
    p = _over_lines(w)
    a = dict(w.respond(p, "override", ["max_lines"]))
    a["user_verification"] = "none"  # ignored: the key was enrolled as one the platform unlocks only after a biometric
    assert w.flow.override(p.proposal_id, ["max_lines"], a).status == "executed"


def test_the_enrolled_user_verification_is_in_the_key_log(w):
    weak = Device("key_weak")
    _enrol_surface(w, weak, "knowledge")
    assert w.flow.identity.keylog.state()["key_weak"].user_verification == "knowledge"
    assert w.flow.identity.keylog.state()[w.device.key_id].user_verification == "inherence"


# --- 7. extras can only add to what the merchant charges ----------------------------------------------------------

def test_a_negative_extra_is_refused_before_anything_is_recorded(w):
    w.merchant.add_cart("cart-big", (CartLine("item_tv", 1, inr(390000)),))
    out = w.propose(replace(w.request("cart-big", "i-big"), extras=(Extra("discount", inr(-389000)),)))
    assert isinstance(out, Invalid) and out.code == "extras_invalid"
    assert not list(w.flow.ledger.records("whl_1", "proposal"))


def test_an_extra_in_another_currency_is_refused(w):
    w.merchant.add_cart("cart-1", LINES)
    out = w.propose(replace(w.request(), extras=(Extra("tip", Money(100, "GBP")),)))
    assert isinstance(out, Invalid) and out.code == "extras_invalid"


def test_a_tip_still_adds_to_the_total(w):
    w.merchant.add_cart("cart-1", LINES)
    p = w.propose(replace(w.request(), extras=(Extra("tip", inr(1000)),)))
    rec = next(r for r in w.flow.ledger.records("whl_1", "proposal") if r["proposal_id"] == p.proposal_id)
    assert rec["total"]["amount_minor"] == 19000 + 1000
