"""Phase 6: reconciliation (05 §4, §6). Every finding kind, settlement only on consistent evidence."""

import random
from datetime import datetime, timedelta, timezone

import pytest

from gap_adapters.execute import Authorisation, Executor
from gap_adapters.fakes import FakeMerchant
from gap_core.authority import RailProfile, Window
from gap_core.clock import FixedClock
from gap_core.evaluate import evaluate
from gap_core.facts import Fact
from gap_core.money import Money
from gap_core.proposal import content_hash
from gap_core.testing import SPOKE, WHEEL, body, scenario
from gap_keys.local import LocalSigner
from gap_ledger.money import Account, MoneyInFlight, replay
from gap_recon.recon import BankFeed, Reconciler

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
SIGNER = LocalSigner.generate("key_host")
UPI = RailProfile("in:upi_intent", "human_per_payment", "bound_at_rail", ("explicit_tap",), placement="pending_then_confirm")
WALLET = RailProfile("in:merchant_wallet", "none", "not_bound", ("explicit_tap",),
                     retry_policy={"safe_errors": ["RATE_LIMITED"], "max_retries": 1, "window_s": 600})
MONTH = Window("calendar_month", "Etc/UTC")
inr = lambda x: Money(x, "INR")  # noqa: E731


class World:
    def __init__(self, ledger):
        self.ledger, self.clock = ledger, FixedClock(T0)
        self.money = MoneyInFlight(ledger, SIGNER, self.clock, random.Random(1))
        self.ex = Executor(ledger, SIGNER, self.clock, random.Random(2))
        self.recon = Reconciler(ledger, SIGNER, self.clock, random.Random(3), self.money, max_pending_age_s=3600, refund_stall_s=86400 * 3)
        self.bank = BankFeed()
        self.n = 0

    def pay(self, shape, script, rail=WALLET, history_last_n=None):
        """Propose, approve, hold and execute one payment; return (det_id, merchant, result)."""
        self.n += 1
        m = FakeMerchant(shape, self.clock, script=script, history_last_n=history_last_n)
        b = body("INR", rail_id=rail.rail_id)
        m.add_cart(f"cart-{self.n}", b.lines, b.fees)
        view = m.get_cart(f"cart-{self.n}")
        rails = {rail.rail_id: rail}
        first = evaluate(scenario("INR", body=b, rails=rails))
        det = evaluate(scenario("INR", body=b, rails=rails, stage="authorisation", authorised_presented_hash=first.presented_hash,
                                determination_id=f"det_{self.n}"))
        self.ledger.append(WHEEL, {"record_type": "proposal", "proposal_id": det.proposal_id + f"_{self.n}", "merchant_cart_ref": f"cart-{self.n}",
                                   "content_hash": content_hash(view.lines, view.fees)}, SIGNER, record_id=f"p{self.n}")
        rec = det.to_record() | {"proposal_id": det.proposal_id + f"_{self.n}"}
        self.ledger.append(WHEEL, rec, SIGNER, record_id=det.determination_id)
        self.money.hold(WHEEL, det.determination_id, view.total, [Account(SPOKE, MONTH, inr(400000)), Account(WHEEL, MONTH, inr(1500000))])
        res = self.ex.execute(WHEEL, det, f"cart-{self.n}", "dst_home", rail, m, Authorisation("payer_approval", True))
        return det.determination_id, m, res

    def sweep(self, m, bank=True):
        return self.recon.sweep(WHEEL, {m.describe().adapter_id: m}, self.bank if bank else None)

    def held(self, det_id):
        p = replay(self.ledger, WHEEL).payments.get(det_id)
        return p.held if p else 0

    def kinds(self, status=None):
        return sorted(f["kind"] for f in self.recon.findings(WHEEL).values() if status is None or f["status"] == status)


@pytest.fixture
def w(ledger):
    return World(ledger)


def test_consistent_evidence_captures_and_closes_as_matched(w):
    det, m, res = w.pay("b", ["ok"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    r = w.sweep(m)
    assert r.captured == 1 and w.held(det) == 0
    assert w.kinds() == ["matched"] and w.kinds("open") == []


def test_overcharge_opens_a_finding_that_forces_a_tap(w):
    det, m, res = w.pay("b", ["overcharge:900"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    assert w.kinds("open") == ["overcapture"]
    assert w.recon.open_tap_findings_by_spoke(WHEEL) == {SPOKE: 1}
    # the evaluator turns the open finding into a forced tap on that spoke
    f = Fact("findings.open_on_spoke", 1, "host", True, "injected")
    from gap_core.testing import repeat_spoke
    from gap_core.authority import Caller
    from dataclasses import replace
    silent = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler", caller=Caller("clr_scheduler", "key_bound"))
    assert evaluate(replace(silent, facts=silent.facts.with_fact(f))).approval_mode_set_by == "finding:open"


def test_bank_and_merchant_disagree_so_nothing_moves(w):
    det, m, res = w.pay("b", ["ok"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, inr(o.charged.amount_minor + 1), T0)
    w.sweep(m)
    assert w.kinds("open") == ["source_conflict"] and w.held(det) > 0  # no winner picked silently
    f = next(f for f in w.recon.open_findings(WHEEL))
    assert {s["class"] for s in f["sources"]} == {"merchant", "bank"}


def test_unknown_outcome_with_complete_history_and_no_order_releases(w):
    det, m, res = w.pay("b", ["timeout_before_create"])
    assert res.outcome == "unknown" and w.held(det) > 0
    w.sweep(m)
    assert w.held(det) == 0 and w.kinds() == ["pending_resolved"]


def test_incomplete_history_opens_a_coverage_gap_and_keeps_the_hold(w):
    det, m, res = w.pay("a", ["error:RATE_LIMITED"])
    w.sweep(m)
    w.sweep(m)  # a second sweep does not open a second finding
    assert w.kinds("open") == ["coverage_gap"] and w.held(det) > 0


def test_findings_never_close_by_timeout(w):
    det, m, res = w.pay("a", ["error:RATE_LIMITED"])
    w.sweep(m)
    w.clock.advance(timedelta(days=90))
    w.sweep(m)
    assert w.kinds("open") == ["coverage_gap"]


def test_payer_acceptance_closes_a_finding(w):
    det, m, res = w.pay("a", ["error:RATE_LIMITED"])
    w.sweep(m)
    (f,) = w.recon.open_findings(WHEEL)
    w.recon.accept(WHEEL, f["finding_id"], "asr_payer_1")
    assert w.kinds("open") == []
    closed = w.recon.findings(WHEEL)[f["finding_id"]]
    assert closed["closed_by"] == "payer_acceptance"


def test_awaiting_payer_settles_once_paid(w):
    det, m, res = w.pay("a", [], rail=UPI)
    assert res.outcome == "awaiting_payer"
    w.sweep(m)
    assert w.held(det) > 0  # not paid yet: nothing moves
    m.confirm_payment(res.merchant_order_ref)
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    assert w.held(det) == 0 and "pending_resolved" in w.kinds("closed")


def test_paid_at_the_merchant_but_no_bank_debit_after_max_age(w):
    det, m, res = w.pay("b", ["ok"])
    w.sweep(m)
    assert w.kinds() == [] and w.held(det) > 0  # wait for the bank
    w.clock.advance(timedelta(hours=2))
    w.sweep(m)
    assert w.kinds("open") == ["source_conflict"]


def test_substituted_items_open_an_item_mismatch(w):
    det, m, res = w.pay("b", ["substitute"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    assert "item_mismatch" in w.kinds("open")


def test_an_order_nobody_decided_is_external_and_never_an_agents(w):
    det, m, res = w.pay("b", ["ok"])
    m.add_cart("cart-outside", body("INR").lines)
    m.place("cart-outside", "in:upi_intent", "someone-else", "dst_home")  # ordered directly at the merchant
    w.sweep(m, bank=False)
    ext = [f for f in w.recon.findings(WHEEL).values() if f["kind"] == "external_order"]
    assert len(ext) == 1 and ext[0]["detail"] == {"attributed_to": "nobody"} and "payment_ref" not in ext[0]
    w.sweep(m, bank=False)
    assert len([f for f in w.recon.findings(WHEEL).values() if f["kind"] == "external_order"]) == 1


def test_missing_order(w):
    det, m, res = w.pay("b", ["ok"])
    del m.orders[res.merchant_order_ref]  # the merchant lost an order the ledger says was placed
    w.sweep(m)
    assert "missing_order" in w.kinds("open")


def test_completed_refund_posts_to_the_original_window(w):
    det, m, res = w.pay("b", ["ok"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    w.clock.advance(timedelta(days=25))  # November
    rf = m.refund(o.merchant_order_ref, inr(5000))
    w.bank.credit("bank-2", rf.refund_ref, inr(5000), w.clock.now())
    r = w.sweep(m)
    w.sweep(m)  # posted once only
    assert r.refunded == 1
    p = replay(w.ledger, WHEEL).payments[det]
    assert p.refunded == 5000
    acct = replay(w.ledger, WHEEL).accounts
    assert acct[(WHEEL, "INR", "2026-10")].spent == o.charged.amount_minor - 5000
    assert (WHEEL, "INR", "2026-11") not in acct


def test_stalled_refund_opens_then_closes_on_credit(w):
    det, m, res = w.pay("b", ["ok"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    rf = m.refund(o.merchant_order_ref, inr(3000), completes=False)
    w.clock.advance(timedelta(days=4))
    w.sweep(m)
    assert "refund_stalled" in w.kinds("open")
    m.complete_refund(rf.refund_ref)
    w.bank.credit("bank-2", rf.refund_ref, inr(3000), w.clock.now())
    w.sweep(m)
    assert "refund_stalled" not in w.kinds("open")
    assert replay(w.ledger, WHEEL).payments[det].refunded == 3000


def test_sweeps_are_signed_records_and_the_ledger_verifies(w):
    det, m, res = w.pay("b", ["ok"])
    r = w.sweep(m)
    assert r.sweep["record_type"] == "sweep" and r.sweep["coverage"] == {"fake-b": {"complete": True}}
    assert w.ledger.verify_integrity({SIGNER.key_id: SIGNER.public_jwk}).ok


def test_replaying_a_sweep_changes_nothing(w):
    det, m, res = w.pay("b", ["ok"])
    o = m.get_order(res.merchant_order_ref)
    w.bank.debit("bank-1", o.merchant_order_ref, o.charged, T0)
    w.sweep(m)
    before = (w.kinds(), replay(w.ledger, WHEEL).accounts)
    w.sweep(m)
    assert (w.kinds(), replay(w.ledger, WHEEL).accounts) == before
