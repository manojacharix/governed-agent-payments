"""Seeded synthetic workloads through the full Phase 6 flow.

Payers (personal and procurement, on the shipped India configs) with agents proposing against fake merchants;
payers who approve, decline, override or ignore; silent weekly repeats whose notices are or are not delivered;
merchants that time out, decline, overcharge, substitute or leave outcomes unknown; a bank feed that is usually
prompt, sometimes late and occasionally wrong; refunds that complete or stall; orders placed directly at a
merchant. Reconciliation runs after every execution and on a schedule. At the end: invariants, and evidence
bundles for a sample of payments verified by the open verifier.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gap_adapters.fakes import FakeMerchant
from gap_core.authority import Caller, CartLineTemplate, Limit, Spoke, StandingInstruction, Wheel, Window
from gap_core.clock import FixedClock
from gap_core.evaluate import IntentConflict, Repeat
from gap_core.facts import Fact, TermSet
from gap_core.hashing import hash_value
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee
from gap_core.testing import rule
from gap_evidence.bundle import EvidenceBuilder, evaluator_digest
from gap_evidence.verify import verify_bundle
from gap_flow.flow import Deployment, Flow, FlowError, ProposalRequest
from gap_flow.surface import ReferenceSurface
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_ledger.money import replay
from gap_mapping.load import evaluation_context, load_mapping, load_tier0
from gap_profile.store import LocalKek, ProfileStore
from gap_recon.recon import BankFeed

ROOT = Path(__file__).resolve().parents[4]
START = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
MERCHANTS = [("mrc_grocer", "std:mcc:5411"), ("mrc_cafe", "std:mcc:5812"), ("mrc_travel", "std:mcc:4722"),
             ("mrc_stationer", "std:mcc:5111"), ("mrc_liquor", "std:mcc:5921"), ("mrc_betting", "std:mcc:7995")]
BLOCKED_FOR_PERSONAL = {"mrc_liquor", "mrc_betting"}
BEHAVIOURS = (["ok"] * 76 + ["decline"] * 5 + ["timeout_after_create"] * 4 + ["timeout_before_create"] * 3 + ["error:RATE_LIMITED"] * 3
              + ["processing"] * 2 + ["overcharge"] * 3 + ["substitute"] * 2 + ["error_after_create:RATE_LIMITED"] * 2)


def inr(x: int) -> Money:
    return Money(x, "INR")


@dataclass
class Payer:
    kind: str
    wheel_id: str
    leaves: list
    callers: dict
    merchant: FakeMerchant
    surface: ReferenceSurface
    bank: BankFeed


@dataclass
class Report:
    seed: int
    proposals: int = 0
    statuses: Counter = field(default_factory=Counter)
    actions: Counter = field(default_factory=Counter)
    findings: Counter = field(default_factory=Counter)
    adversarial: Counter = field(default_factory=Counter)
    bundles: Counter = field(default_factory=Counter)
    violations: list = field(default_factory=list)
    records: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations


class FlowSimulation:
    def __init__(self, ledger_conn, profile_conn, seed: int, payers: int) -> None:
        self.rng, self.seed = random.Random(seed), seed
        self.clock = FixedClock(START)
        self.ledger = Ledger(ledger_conn)
        self.host = LocalSigner.generate("key_sim_host")
        self.keylog = KeyLog(self.ledger, "sim", self.host, self.clock)
        self.keylog.bootstrap()
        self.digest = evaluator_digest()
        self.flow = Flow(self.ledger, ProfileStore(profile_conn, LocalKek.generate(), self.clock), self.keylog, self.clock,
                         random.Random(seed + 1), evaluator_digest=self.digest, max_pending_age_s=6 * 3600, refund_stall_s=5 * 86400)
        india = load_mapping(ROOT / "mappings/in/v0.1.0.yaml")
        self.ctx = {k: {**evaluation_context(india, load_tier0(ROOT / f"tier0/examples/{f}.yaml", india)), "mapping_ref": india.ref}
                    for k, f in (("personal", "personal_in"), ("b2b", "b2b_procurement_in"))}
        self.month = Window("calendar_month", "Asia/Kolkata")
        self.payers = [self._payer(i) for i in range(payers)]
        self.report = Report(seed)
        self.adversarial: dict = {}   # proposal_id -> set of reasons
        self.external: set = set()    # orders placed directly at merchants
        self.late_debits: list = []   # (due, bank, ref, order_ref, amount)

    def _payer(self, i: int) -> Payer:
        r, wid, pyr = self.rng, f"whl_{i:04d}", f"pyr_{i:04d}"
        ago, until = START - timedelta(days=30), START + timedelta(days=365)
        if r.random() < 0.7:
            kind = "personal"
            ins = StandingInstruction("mrc_grocer", "fake", (CartLineTemplate("item_milk", 2), CartLineTemplate("item_bread", 1)), "dst_home",
                                      "in:merchant_wallet", 1000, inr(19_500))
            spokes = {f"{wid}_grocery": Spoke(f"{wid}_grocery", wid, "explicit_tap", ago, until, callers=(f"clr_{i}_a",), caps=(Limit(inr(r.randint(10, 40) * 100_000), self.month),),
                                              rules=(rule(f"{wid}.max_three_lines", "items", max_lines=3),)),  # payer-authored: overridable
                      f"{wid}_travel": Spoke(f"{wid}_travel", wid, "explicit_tap", ago, until, callers=(f"clr_{i}_a",), caps=(Limit(inr(r.randint(20, 80) * 100_000), self.month),)),
                      f"{wid}_repeat": Spoke(f"{wid}_repeat", wid, "silent_with_notice", ago, until, callers=(f"clr_{i}_s",), instruction=ins, caps=(Limit(inr(2_000_000), self.month),))}
            callers = {f"{wid}_grocery": Caller(f"clr_{i}_a", "session_bound"), f"{wid}_travel": Caller(f"clr_{i}_a", "session_bound"),
                       f"{wid}_repeat": Caller(f"clr_{i}_s", "key_bound")}
            wheel = Wheel(wid, pyr, "in", budgets=(Limit(inr(r.randint(30, 200) * 100_000), self.month),))
            leaves = list(spokes)
        else:
            kind = "b2b"
            spokes = {f"{wid}_company": Spoke(f"{wid}_company", wid, "explicit_tap", ago, until, caps=(Limit(inr(50_000_000), self.month),)),
                      f"{wid}_team": Spoke(f"{wid}_team", wid, "explicit_tap", ago, until, parent_id=f"{wid}_company", caps=(Limit(inr(15_000_000), self.month),)),
                      f"{wid}_agent": Spoke(f"{wid}_agent", wid, "explicit_tap", ago, until, parent_id=f"{wid}_team", callers=(f"clr_{i}_b",), caps=(Limit(inr(4_000_000), self.month),))}
            callers = {f"{wid}_agent": Caller(f"clr_{i}_b", "key_bound")}
            wheel = Wheel(wid, pyr, "in", budgets=(Limit(inr(r.randint(20, 60) * 10_000_000), self.month),))
            leaves = [f"{wid}_agent"]
        m = FakeMerchant(r.choice("ab"), self.clock)
        bank = BankFeed()
        self.flow.register(Deployment(wheel, spokes, self.ctx[kind], {m.describe().adapter_id: m}, bank, notice_duration_s=600, proposal_ttl_s=1800))
        s = ReferenceSurface(f"key_surface_{i}", pyr, self.clock)
        self.flow.enrol_surface(wid, s)
        return Payer(kind, wid, leaves, callers, m, s, bank)

    # --- one event ------------------------------------------------------------------------

    def _request(self, p: Payer, n: int):
        r, m, reasons = self.rng, p.merchant, set()
        spoke_id = r.choice(p.leaves)
        caller = p.callers[spoke_id]
        if r.random() < 0.03:
            caller, reasons = Caller("clr_intruder", "key_bound"), reasons | {"wrong_caller"}
        cart = f"{p.wheel_id}-cart-{n}"
        if spoke_id.endswith("_repeat"):
            drift = r.randint(-15, 15)
            m.add_cart(cart, (CartLine("item_milk", 2, inr(6000 + 60 * drift)), CartLine("item_bread", 1, inr(4500))), (Fee("delivery", inr(3000)),))
            merchant, category, intent = "mrc_grocer", "std:mcc:5411", f"si:{spoke_id}:{n}"
        else:
            merchant, category = r.choice(MERCHANTS)
            scale = 50 if p.kind == "b2b" else 1
            m.add_cart(cart, tuple(CartLine(f"item_{r.randint(1, 60)}", r.randint(1, 3), inr(r.randint(20, 1500) * 100 * scale)) for _ in range(r.randint(1, 4))),
                       (Fee("delivery", inr(r.choice([0, 2000, 4000]))),))
            intent = f"intent-{n}"
            if p.kind == "personal" and merchant in BLOCKED_FOR_PERSONAL:
                reasons.add("blocked_category")
        verified = r.random() < 0.95
        facts = [Fact("merchant.categories", TermSet(frozenset({category}), "std:mcc@1", "fake@1"), "merchant_adapter", verified, "capturer_signed")]
        if p.kind == "b2b":
            x = r.random()
            if x < 0.8:
                facts += [Fact("ext.procurement.po_matched", True, "erp", True, "source_signed"), Fact("ext.procurement.supplier_approved", True, "erp", True, "source_signed")]
            elif x < 0.9:
                reasons.add("unverified_po_claim")
                facts += [Fact("ext.procurement.po_matched", True, "agent", False, "capturer_signed"), Fact("ext.procurement.supplier_approved", True, "agent", False, "capturer_signed")]
            else:
                reasons.add("no_po")
        rail = None if spoke_id.endswith("_repeat") else r.choice([None, None, None, "in:merchant_wallet", "in:upi_intent", "in:cash_on_delivery"])
        return ProposalRequest(spoke_id, caller, intent, merchant, m.describe().adapter_id, cart, "dst_home", 980 if spoke_id.endswith("_repeat") else r.randint(820, 1000),
                               rail_id=rail, facts=tuple(facts)), reasons

    def step(self, n: int) -> None:
        r, rep = self.rng, self.report
        p = r.choice(self.payers)
        req, reasons = self._request(p, n)
        script = []
        for _ in range(3):
            b = r.choice(BEHAVIOURS)
            script.append(f"overcharge:{r.randint(100, 50_000)}" if b == "overcharge" else b)
        p.merchant.script = script
        res = self.flow.propose(p.wheel_id, req)
        if isinstance(res, (Repeat, IntentConflict)):
            rep.statuses[type(res).__name__] += 1
            return
        rep.proposals += 1
        if reasons:
            self.adversarial[res.proposal_id] = reasons
        if res.status == "notice":
            if r.random() < 0.9:
                self.flow.notice_delivered(res.proposal_id)
            if r.random() < 0.05:
                self._act(p, res, "decline")
        elif res.status == "awaiting_approval":
            x = r.random()
            if res.determination.outcome == "refuse":
                if x < 0.5:
                    self._act(p, res, "override")
            elif x < 0.85:
                self._act(p, res, "authorise")
            elif x < 0.92:
                self._act(p, res, "decline")
            # else: the payer ignores it and it expires
        if r.random() < 0.01:  # someone orders directly at the merchant, outside the layer
            p.merchant.add_cart(f"direct-{n}", (CartLine("item_direct", 1, inr(5000)),))
            saved, p.merchant.script = p.merchant.script, ["ok"]
            o = p.merchant.place(f"direct-{n}", p.merchant.describe().rails_offered[0], f"direct-{n}", "dst_home")
            p.merchant.script = saved
            self.external.add((p.wheel_id, o.merchant_order_ref))

    def _act(self, p: Payer, pending, action: str) -> None:
        det, pid = pending.determination, pending.proposal_id
        try:
            if action == "override":
                ids = [c.rule_id for c in det.failing_clauses]
                subject = hash_value({"presented_hash": det.presented_hash, "overridden": sorted(ids)})
                out = self.flow.override(pid, ids, p.surface.sign("override", pid, subject))
            elif action == "authorise":
                out = self.flow.approve(pid, p.surface.sign("authorise", pid, det.presented_hash))
            else:
                out = self.flow.decline(pid, p.surface.sign("decline", pid, det.presented_hash or det.proposal_hash))
            self.report.actions[action] += 1
            self._bank_follow_up(p, out)
        except FlowError:
            self.report.actions[f"{action}_refused"] += 1

    def _bank_follow_up(self, p: Payer, out) -> None:
        """The bank records debits for paid orders: usually at once, sometimes late, occasionally wrong."""
        res = getattr(out, "result", None)
        if getattr(out, "status", None) != "executed" or res is None or not res.merchant_order_ref:
            return
        m, r = p.merchant, self.rng
        o = m.get_order(res.merchant_order_ref)
        if o.status == "PENDING_PAYMENT":
            if r.random() < 0.95:
                m.confirm_payment(o.merchant_order_ref)
                o = m.get_order(o.merchant_order_ref)
            else:
                return
        if o.charged is None:
            return
        x = r.random()
        amount = o.charged if x < 0.98 else inr(o.charged.amount_minor + 1)
        if x < 0.9 or x >= 0.98:
            p.bank.debit(f"bank-{o.merchant_order_ref}", o.merchant_order_ref, amount, self.clock.now())
        else:
            self.late_debits.append((self.clock.now() + timedelta(hours=r.randint(1, 10)), p.bank, o.merchant_order_ref, amount))
        if r.random() < 0.05:
            m.refund(o.merchant_order_ref, inr(r.randint(1, o.charged.amount_minor)), completes=r.random() < 0.8)

    def _due(self) -> None:
        now = self.clock.now()
        for item in [x for x in self.late_debits if x[0] <= now]:
            self.late_debits.remove(item)
            _, bank, ref, amount = item
            bank.debit(f"bank-{ref}", ref, amount, now)
        for p in self.payers:
            for rf in p.merchant.refunds.values():
                if rf.status == "completed" and p.bank.find("credit", rf.refund_ref) is None:
                    p.bank.credit(f"bank-{rf.refund_ref}", rf.refund_ref, rf.amount, now)

    # --- the run and the checks -----------------------------------------------------------------

    def run(self, events: int, minutes_per_event: int, bundles: int = 25) -> Report:
        for n in range(events):
            self.clock.advance(timedelta(minutes=self.rng.randint(1, 2 * minutes_per_event)))
            self._due()
            for out in self.flow.tick(sweep=n % 50 == 0):
                if getattr(out, "status", None) == "executed":
                    self._bank_follow_up(next(p for p in self.payers if p.wheel_id == out.wheel_id), out)
            self.step(n)
        self.clock.advance(timedelta(days=10))
        self._due()
        self.flow.tick(sweep=True)
        self._check(bundles)
        return self.report

    def _check(self, n_bundles: int) -> None:
        rep, v, led = self.report, self.report.violations, self.ledger
        integrity = led.verify_integrity(self.keylog.public_keys())
        rep.records = integrity.records_checked
        v += [f"integrity {c}: {m}" for c, m in integrity.problems]
        for pend in self.flow.proposals.values():
            rep.statuses[pend.status] += 1
            reasons = self.adversarial.get(pend.proposal_id, set())
            hard = reasons & {"wrong_caller", "blocked_category", "unverified_po_claim", "no_po"}
            if hard and pend.status == "executed":
                res = [r for r in led.records(pend.wheel_id, "resolution") if r["proposal_id"] == pend.proposal_id]
                if not any(r["state"] == "approved_with_override" for r in res):  # a payer may override only payer rules
                    v.append(f"{pend.proposal_id}: executed despite {sorted(hard)}")
                elif reasons & {"wrong_caller", "unverified_po_claim", "no_po", "blocked_category"}:
                    v.append(f"{pend.proposal_id}: overridden despite non-payer clauses {sorted(hard)}")
        for p in self.payers:
            recs = led.records(p.wheel_id)
            res_by_det = {r["determination_id"]: r for r in recs if r["record_type"] == "resolution"}
            allowed = {d for d, r in res_by_det.items() if r["state"] in ("approved", "approved_with_override", "proceeded_on_silence")}
            for r in recs:
                if r["record_type"] == "posting" and r["payment_ref"] not in allowed:
                    v.append(f"{p.wheel_id}: posting for {r['payment_ref']} with no approval")
            st = replay(led, p.wheel_id)
            for key, a in st.accounts.items():
                if a.held < 0 or a.spent < 0:
                    v.append(f"{key}: negative balance")
            for det_id, ps in st.payments.items():
                if ps.refunded > ps.captured:
                    v.append(f"{det_id}: refunded more than captured")
            open_payments = {f.get("payment_ref") for f in self.flow.recon.open_findings(p.wheel_id)}
            awaiting = {a["determination_id"] for a in recs if a["record_type"] == "execution_attempt" and a["outcome"] == "awaiting_payer"}
            for det_id, ps in st.payments.items():
                if ps.held > 0 and det_id not in open_payments and det_id not in awaiting:
                    v.append(f"{det_id}: money still held with no open finding to explain it")
            carts = {}
            for r in recs:
                if r["record_type"] == "proposal":
                    carts[r["merchant_cart_ref"]] = r["proposal_id"]
            for o in p.merchant.orders.values():
                if o.cart_ref.startswith("direct-"):
                    continue
                if o.cart_ref not in carts:
                    v.append(f"{o.merchant_order_ref}: an order no proposal led to")
            per_cart = Counter(o.cart_ref for o in p.merchant.orders.values())
            for cart, n in per_cart.items():
                if n > 1:
                    v.append(f"{cart}: {n} orders for one cart")
            ext = {f.get("merchant_order_ref") for f in self.flow.recon.findings(p.wheel_id).values() if f["kind"] == "external_order"}
            directs = {ref for wid, ref in self.external if wid == p.wheel_id}
            for ref in ext - directs:
                o = p.merchant.orders.get(ref)
                v.append(f"{p.wheel_id} {ref}: labelled external but placed for cart {o.cart_ref if o else '?'} ({o.status if o else '?'})")
            for wid, ref in self.external:
                if wid == p.wheel_id and ref not in ext:
                    v.append(f"{ref}: a direct order was not recorded as external")
            for f in self.flow.recon.findings(p.wheel_id).values():
                rep.findings[f"{f['kind']}:{f['status']}"] += 1
        # evidence: bundles for a sample of executed payments, all verified by the open verifier
        builder = EvidenceBuilder(led, self.keylog, self.flow.profile, self.clock, random.Random(self.seed + 9))
        executed = [p for p in self.flow.proposals.values() if p.status == "executed"]
        for pend in self.rng.sample(executed, min(n_bundles, len(executed))):
            for role in ("payer", "merchant"):
                b = builder.build(pend.wheel_id, pend.proposal_id, role=role, requester_ref="sim")
                result = verify_bundle(b, self.host.public_jwk, evaluator=self.digest)
                rep.bundles[f"{role}:{'verified' if result['ok'] else 'FAILED'}"] += 1
                rep.bundles[f"divergence:{b['divergence']['primary']}"] += role == "payer"
                if not result["ok"]:
                    v.append(f"{pend.proposal_id} {role} bundle: {result['problems'][:2]}")
                if role == "payer" and result["replay"] != "reproduced":
                    v.append(f"{pend.proposal_id}: replay {result['replay']}")


def run(ledger_conn, profile_conn, *, seed: int, payers: int, events: int, minutes_per_event: int = 15, bundles: int = 25) -> Report:
    return FlowSimulation(ledger_conn, profile_conn, seed, payers).run(events, minutes_per_event, bundles)
