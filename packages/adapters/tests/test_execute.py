"""Phase 5 acceptance tests: 04 §5."""

import random
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from gap_adapters.execute import Authorisation, ExecutionRefused, Executor, idempotency_key
from gap_adapters.fakes import FakeMerchant
from gap_core.authority import RailProfile, Window
from gap_core.clock import FixedClock
from gap_core.evaluate import evaluate
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee
from gap_core.testing import WHEEL, body, scenario
from gap_keys.local import LocalSigner
from gap_ledger.money import Account, MoneyInFlight, Signal, replay

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
SIGNER = LocalSigner.generate("key_host")
PAYER = Authorisation("payer_approval")

UPI = RailProfile("in:upi_intent", "human_per_payment", "bound_at_rail", ("explicit_tap",), placement="pending_then_confirm",
                  retry_policy={"safe_errors": [], "max_retries": 0, "window_s": 0})
WALLET = RailProfile("in:merchant_wallet", "none", "not_bound", ("explicit_tap", "silent_with_notice"),
                     retry_policy={"safe_errors": ["TIMEOUT", "RATE_LIMITED"], "max_retries": 2, "window_s": 600})
RAILS = {UPI.rail_id: UPI, WALLET.rail_id: WALLET}


def authorised(rail_id):
    """An authorisation-stage allow for body() on the given rail."""
    b = body("INR", rail_id=rail_id)
    first = evaluate(scenario("INR", body=b, rails=RAILS))
    return evaluate(scenario("INR", body=b, rails=RAILS, stage="authorisation", authorised_presented_hash=first.presented_hash)), b


def merchant(shape, **kw):
    m = FakeMerchant(shape, FixedClock(T0), **kw)
    b = body("INR")
    m.add_cart("cart-1", b.lines, b.fees)
    return m


@pytest.fixture
def ex(ledger):
    return Executor(ledger, SIGNER, FixedClock(T0), random.Random(3))


# --- guards ----------------------------------------------------------------------------------

def test_only_an_authorisation_stage_allow_executes(ex):
    det, _ = authorised("in:upi_intent")
    with pytest.raises(ExecutionRefused):
        ex.execute(WHEEL, replace(det, stage="proposal"), "cart-1", "dst_home", UPI, merchant("a"), PAYER)
    with pytest.raises(ExecutionRefused):
        ex.execute(WHEEL, replace(det, outcome="refuse"), "cart-1", "dst_home", UPI, merchant("a"), PAYER)


def test_no_rail_change_without_a_new_approval(ex):
    det, _ = authorised("in:upi_intent")
    m = merchant("a")
    with pytest.raises(ExecutionRefused, match="never switched"):
        ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, PAYER)
    assert m.place_calls == 0


def test_a_payer_override_executes_only_if_it_covers_every_failing_clause(ex):
    det, _ = authorised("in:upi_intent")
    refused = replace(det, outcome="refuse", failing_clauses=(__import__("gap_core.evaluate", fromlist=["FailingClause"]).FailingClause("p1", "items", "payer", True),))
    with pytest.raises(ExecutionRefused):
        ex.execute(WHEEL, refused, "cart-1", "dst_home", UPI, merchant("b"), PAYER)
    res = ex.execute(WHEEL, refused, "cart-1", "dst_home", UPI, merchant("b"), Authorisation("payer_approval", overridden=frozenset({"p1"})))
    assert res.outcome == "success"


def test_a_rail_the_merchant_does_not_offer_is_refused(ex):
    det, _ = authorised("in:upi_intent")
    other = replace(UPI, rail_id="in:card")
    with pytest.raises(ExecutionRefused):
        ex.execute(WHEEL, replace(det, rail_id="in:card"), "cart-1", "dst_home", other, merchant("a"), PAYER)


def test_a_rail_that_is_not_live_is_refused(ex):
    det, _ = authorised("in:upi_intent")
    with pytest.raises(ExecutionRefused):
        ex.execute(WHEEL, det, "cart-1", "dst_home", replace(UPI, status="placeholder"), merchant("a"), PAYER)


def test_an_unauthenticated_rail_needs_the_strength_or_a_standing_instruction(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("a")
    with pytest.raises(ExecutionRefused, match="no authentication"):
        ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, PAYER)
    assert m.place_calls == 0
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", no_auth_rail_ok=True))
    assert res.outcome == "success"


# --- the happy paths ---------------------------------------------------------------------------

def test_pending_then_confirm_hands_off_to_the_payer(ex, ledger):
    det, _ = authorised("in:upi_intent")
    m = merchant("a")
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", UPI, m, PAYER)
    assert res.outcome == "awaiting_payer" and res.handoff and res.merchant_order_ref
    assert res.needs_reconciliation  # not settled until a definitive signal arrives
    (attempt,) = res.attempts
    assert attempt["idempotency_key"] == idempotency_key(det.determination_id) and attempt["attempt_number"] == 1
    assert ledger.verify_integrity({SIGNER.key_id: SIGNER.public_jwk}).ok


def test_immediate_success_on_a_bound_rail_needs_no_reconciliation(ex):
    det, _ = authorised("in:upi_intent")
    m = merchant("b")
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", UPI, m, PAYER)
    assert res.outcome == "success" and not res.needs_reconciliation


def test_not_bound_rail_always_needs_reconciliation(ex):
    det, _ = authorised("in:merchant_wallet")
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, merchant("b"), Authorisation("payer_approval", True))
    assert res.outcome == "success" and res.needs_reconciliation  # 04 §1: not_bound means check after every execution


# --- ambiguity: never a second order -------------------------------------------------------------

@pytest.mark.parametrize("shape", ["a", "b"])
def test_an_order_created_then_timed_out_never_produces_a_second_order(ex, shape):
    det, _ = authorised("in:merchant_wallet")
    m = merchant(shape, script=["timeout_after_create", "ok", "ok"])
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert res.outcome == "success" and len(m.orders_for_cart("cart-1")) == 1
    assert res.attempts[0]["response_status"] == "TIMEOUT+FOUND_IN_HISTORY"
    assert m.place_calls == 1


def test_proven_absence_and_a_safe_error_allow_a_retry(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("b", script=["error:RATE_LIMITED", "ok"])  # fake-b has complete history: absence is provable
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert res.outcome == "success" and len(res.attempts) == 2
    assert res.attempts[1]["retry_of"] == res.attempts[0]["attempt_id"]
    assert len(m.orders_for_cart("cart-1")) == 1


def test_incomplete_history_is_not_proof_so_no_retry(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("b", script=["error:RATE_LIMITED", "ok"], history_last_n=5)
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert res.outcome == "unknown" and res.needs_reconciliation and m.place_calls == 1


def test_errors_not_marked_safe_are_never_retried(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("b", script=["error:INTERNAL", "ok"])
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert res.outcome == "unknown" and m.place_calls == 1


def test_retries_never_exceed_the_profile_limit(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("b", script=["error:RATE_LIMITED"] * 10)
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert m.place_calls == WALLET.retry_policy["max_retries"] + 1 and res.outcome == "unknown"


def test_error_after_create_is_found_not_retried(ex):
    det, _ = authorised("in:merchant_wallet")
    m = merchant("b", script=["error_after_create:RATE_LIMITED", "ok"])
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", True))
    assert res.outcome == "success" and len(m.orders_for_cart("cart-1")) == 1 and m.place_calls == 1


def test_decline_is_a_failure(ex):
    det, _ = authorised("in:upi_intent")
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", UPI, merchant("b", script=["decline"]), PAYER)
    assert res.outcome == "failure"


# --- into money in flight -------------------------------------------------------------------------

def test_success_captures_what_was_actually_charged(ex, ledger):
    det, b = authorised("in:upi_intent")
    clock = FixedClock(T0)
    mif = MoneyInFlight(ledger, SIGNER, clock, random.Random(9))
    month = Window("calendar_month", "Etc/UTC")
    m = merchant("b", script=["overcharge:700"])
    approved = m.get_cart("cart-1").total  # what the payer saw is the merchant's own cart (fake-b: flat fee)
    mif.hold(WHEEL, det.determination_id, approved, [Account(WHEEL, month, Money(1500000, "INR"))])
    res = ex.execute(WHEEL, det, "cart-1", "dst_home", UPI, m, PAYER)
    out = mif.capture(WHEEL, det.determination_id, res.charged, Signal("merchant", res.merchant_order_ref))
    assert out == {"overcaptured": 700}
    assert replay(ledger, WHEEL).accounts[(WHEEL, "INR", "2026-10")].spent == approved.amount_minor + 700
