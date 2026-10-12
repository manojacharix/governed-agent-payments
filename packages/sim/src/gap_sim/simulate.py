"""Seeded synthetic workloads for the whole stack (Phases 1 to 4).

Generates payers (personal and B2B procurement, on the shipped India configs), months of proposals with
messy outcomes (exact, lower and higher charges, failures, unknown outcomes resolved later, refunds) and
a share of adversarial inputs (wrong caller, unknown spoke, blocked merchant categories, unverified claims).
Every proposal goes through evaluate(); allowed ones are held, settled and refunded in a real Postgres
ledger. A shadow ledger kept with plain arithmetic is compared with the real one at the end, and a set of
invariants is checked. Same seed, same run.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gap_adapters.execute import Authorisation, ExecutionRefused, Executor
from gap_adapters.fakes import FakeMerchant
from gap_core.authority import CartLineTemplate, Caller, Limit, Spoke, StandingInstruction, Wheel, Window
from gap_core.canonical import canonical
from gap_core.clock import FixedClock
from gap_core.evaluate import Determination, EvaluationInput, IntentConflict, PriorIntent, Repeat, Usage, evaluate
from gap_core.facts import Fact, FactSet, TermSet
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee, Proposal, ProposalBody, proposal_hash
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_ledger.money import Account, HoldRefused, MoneyInFlight, Signal, replay
from gap_mapping.load import evaluation_context, load_mapping, load_tier0

ROOT = Path(__file__).resolve().parents[4]
START = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)  # 09:00 in Kolkata
MERCHANTS = [  # (ref, category) -- 5921 liquor and 7995 betting are blocked by the personal operator
    ("mrc_grocer", "std:mcc:5411"), ("mrc_cafe", "std:mcc:5812"), ("mrc_travel", "std:mcc:4722"),
    ("mrc_stationer", "std:mcc:5111"), ("mrc_liquor", "std:mcc:5921"), ("mrc_betting", "std:mcc:7995"),
]
BLOCKED_FOR_PERSONAL = {"mrc_liquor", "mrc_betting"}


def inr(x: int) -> Money:
    return Money(x, "INR")


@dataclass
class Payer:
    kind: str  # personal | b2b
    wheel: Wheel
    spokes: dict
    leaves: list  # spoke ids that proposals cite
    callers: dict  # spoke_id -> Caller
    ctx: dict
    merchant: FakeMerchant = None


@dataclass
class Report:
    seed: int
    events: int
    outcomes: Counter = field(default_factory=Counter)
    settlements: Counter = field(default_factory=Counter)
    adversarial: Counter = field(default_factory=Counter)
    clauses: Counter = field(default_factory=Counter)
    violations: list = field(default_factory=list)
    records: int = 0
    chains: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations


class Simulation:
    def __init__(self, conn, seed: int, payers: int) -> None:
        self.rng = random.Random(seed)
        self.seed = seed
        self.clock = FixedClock(START)
        self.ledger = Ledger(conn)
        self.host = LocalSigner.generate("key_sim_host")
        self.keylog = KeyLog(self.ledger, "sim-host", self.host, self.clock)
        self.keylog.bootstrap()
        self.money = MoneyInFlight(self.ledger, self.host, self.clock, random.Random(seed + 1))
        self.executor = Executor(self.ledger, self.host, self.clock, random.Random(seed + 2))
        india = load_mapping(ROOT / "mappings/in/v0.1.0.yaml")
        self.personal_ctx = evaluation_context(india, load_tier0(ROOT / "tier0/examples/personal_in.yaml", india))
        self.b2b_ctx = evaluation_context(india, load_tier0(ROOT / "tier0/examples/b2b_procurement_in.yaml", india))
        self.month = Window("calendar_month", "Asia/Kolkata")
        self.payers = [self._payer(i) for i in range(payers)]
        for p in self.payers:  # each payer shops at a merchant of shape a or b
            p.merchant = FakeMerchant(self.rng.choice("ab"), self.clock)
        self.executions: dict = {}   # det_id -> (merchant, cart_ref, attempts)
        self.shadow: dict = {}       # (owner, window_id) -> [held, spent]
        self.limits: dict = {}       # (owner, window_id) -> limit amount
        self.overcaptured: set = set()
        self.payments: dict = {}     # det_id -> {wheel, accounts, held, captured, refunded}
        self.pending: list = []      # (due, action, det_id)
        self.intents: dict = {}      # intent_id -> (wheel_id, proposal_hash, det_id, body, spoke_id, caller)
        self.allowed_ids: set = set()
        self.samples: list = []
        self.report = Report(seed, 0)

    # --- world generation -------------------------------------------------------

    def _spoke(self, sid, wid, cap, callers, parent=None, instruction=None):
        return Spoke(sid, wid, "silent_with_notice" if instruction else "explicit_tap",
                     START - timedelta(days=30), START + timedelta(days=365), parent_id=parent, callers=callers,
                     instruction=instruction, caps=(Limit(inr(cap), self.month),) if cap else ())

    def _payer(self, i: int) -> Payer:
        r, wid = self.rng, f"whl_{i:04d}"
        if r.random() < 0.7:
            wheel = Wheel(wid, f"pyr_{i:04d}", "in", budgets=(Limit(inr(r.randint(30, 200) * 100_000), self.month),))
            ins = StandingInstruction("mrc_grocer", "fake-a", (CartLineTemplate("item_milk", 2), CartLineTemplate("item_bread", 1)),
                                      "dst_home", "in:merchant_wallet", 1000, inr(19_500))
            spokes = {
                f"{wid}_grocery": self._spoke(f"{wid}_grocery", wid, r.randint(10, 40) * 100_000, (f"clr_{i}_agent",)),
                f"{wid}_travel": self._spoke(f"{wid}_travel", wid, r.randint(20, 80) * 100_000, (f"clr_{i}_agent",)),
                f"{wid}_repeat": self._spoke(f"{wid}_repeat", wid, 2_000_000, (f"clr_{i}_sched",), instruction=ins),
            }
            callers = {f"{wid}_grocery": Caller(f"clr_{i}_agent", "session_bound"), f"{wid}_travel": Caller(f"clr_{i}_agent", "session_bound"),
                       f"{wid}_repeat": Caller(f"clr_{i}_sched", "key_bound")}
            return Payer("personal", wheel, spokes, list(spokes), callers, self.personal_ctx)
        wheel = Wheel(wid, f"pyr_{i:04d}", "in", budgets=(Limit(inr(r.randint(20, 60) * 10_000_000), self.month),))
        spokes = {
            f"{wid}_company": self._spoke(f"{wid}_company", wid, 50_000_000, None),
            f"{wid}_team": self._spoke(f"{wid}_team", wid, 15_000_000, None, parent=f"{wid}_company"),
            f"{wid}_agent": self._spoke(f"{wid}_agent", wid, 4_000_000, (f"clr_{i}_buyer",), parent=f"{wid}_team"),
        }
        return Payer("b2b", wheel, spokes, [f"{wid}_agent"], {f"{wid}_agent": Caller(f"clr_{i}_buyer", "key_bound")}, self.b2b_ctx)

    # --- one event --------------------------------------------------------------

    def _body(self, payer: Payer, spoke: Spoke) -> tuple[ProposalBody, str]:
        """The proposal is built from the merchant's own cart, as a real agent would read it."""
        r, m = self.rng, payer.merchant
        cart_ref = f"cart-{self.report.events}"
        adapter = m.describe().adapter_id
        if spoke.instruction is not None:
            drift = r.randint(-15, 15)  # percent; beyond +10 the silent ceiling forces a tap
            m.add_cart(cart_ref, (CartLine("item_milk", 2, inr(6000 + 60 * drift)), CartLine("item_bread", 1, inr(4500))), (Fee("delivery", inr(3000)),))
            v = m.get_cart(cart_ref)
            return ProposalBody("mrc_grocer", adapter, cart_ref, v.lines, v.total, "dst_home", 980, fees=v.fees), "std:mcc:5411"
        merchant, category = r.choice(MERCHANTS)
        scale = 50 if payer.kind == "b2b" else 1
        lines = tuple(CartLine(f"item_{r.randint(1, 60)}", r.randint(1, 3), inr(r.randint(20, 1500) * 100 * scale)) for _ in range(r.randint(1, 4)))
        m.add_cart(cart_ref, lines, (Fee("delivery", inr(r.choice([0, 2000, 4000]))),))
        v = m.get_cart(cart_ref)
        rail = r.choice([None, None, None, "in:merchant_wallet", "in:cash_on_delivery", "in:upi_intent"])
        return ProposalBody(merchant, adapter, cart_ref, v.lines, v.total, "dst_home", r.randint(820, 1000),
                            fees=v.fees, rail_id=rail), category

    def _facts(self, payer: Payer, category: str, adversarial: set) -> FactSet:
        r = self.rng
        offered = Fact("merchant.rails_offered", payer.merchant.describe().rails_offered, "merchant_adapter", True, "capturer_signed")
        verified_cat = r.random() < 0.95
        if not verified_cat:
            adversarial.add("unverified_category")
        facts = [Fact("merchant.categories", TermSet(frozenset({category}), "std:mcc@1", "fake-a@1"), "merchant_adapter", verified_cat, "capturer_signed"),
                 Fact("notice.delivered", True, "host", r.random() < 0.9, "injected"), offered]
        if payer.kind == "b2b":
            x = r.random()
            if x < 0.8:
                facts += [Fact("ext.procurement.po_matched", True, "erp", True, "source_signed"),
                          Fact("ext.procurement.supplier_approved", True, "erp", True, "source_signed")]
            elif x < 0.9:
                adversarial.add("unverified_po_claim")
                facts += [Fact("ext.procurement.po_matched", True, "agent", False, "capturer_signed"),
                          Fact("ext.procurement.supplier_approved", True, "agent", False, "capturer_signed")]
            else:
                adversarial.add("no_po")
        return FactSet(facts)

    def _chain(self, payer: Payer, spoke_id: str) -> list:
        chain, cur = [], payer.spokes.get(spoke_id)
        while cur is not None:
            chain.append(cur)
            cur = payer.spokes.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def _accounts(self, payer: Payer, chain: list) -> list[Account]:
        accts = [Account(s.spoke_id, self.month, s.caps[0].limit if s.caps else None) for s in chain]
        return accts + [Account(payer.wheel.wheel_id, self.month, payer.wheel.budgets[0].limit)]

    def step(self) -> None:
        r, rep = self.rng, self.report
        self._settle_due()
        payer = r.choice(self.payers)
        spoke_id = r.choice(payer.leaves)
        spoke = payer.spokes[spoke_id]
        adversarial: set = set()
        caller = payer.callers[spoke_id]
        x = r.random()
        if x < 0.03:
            caller, adversarial = Caller("clr_intruder", "key_bound"), adversarial | {"wrong_caller"}
        elif x < 0.05:
            spoke_id, adversarial = "spk_does_not_exist", adversarial | {"unknown_spoke"}
        body, category = self._body(payer, spoke)
        if payer.kind == "personal" and body.merchant_ref in BLOCKED_FOR_PERSONAL:
            adversarial.add("blocked_category")
        facts = self._facts(payer, category, adversarial)

        intent = f"intent-{rep.events}"
        prior = {}
        if self.intents and r.random() < 0.02:  # a retry: same intent, sometimes with a changed cart
            intent, (wid, ph, det0, body0, sid0, caller0) = r.choice(list(self.intents.items()))
            payer = next(p for p in self.payers if p.wheel.wheel_id == wid)
            spoke_id, caller = sid0, caller0
            body = body0 if r.random() < 0.5 else replace(body0, agent_confidence=max(0, body0.agent_confidence - 1))
            prior = {intent: PriorIntent(ph, det0)}

        at = self.clock.now()
        chain = self._chain(payer, spoke_id)
        usage = {(a.owner, self.month.window_id(at)): Usage(0, {}) for a in self._accounts(payer, chain)} if chain else {}
        usage.update(replay(self.ledger, payer.wheel.wheel_id).usage())
        det_id = f"det_{rep.events:06d}"
        inp = replace(
            EvaluationInput(det_id, Proposal(f"prp_{rep.events:06d}", payer.wheel.wheel_id, spoke_id, caller.caller_id, intent, body),
                            caller, payer.wheel, payer.spokes, (), (), "in", {}, payer.ctx["params"], facts, usage, at,
                            {"wheel": 1, "spoke": 1, "tier0": 1, "mapping": "in@0.1.0", "evaluator": "sha256:" + "0" * 64},
                            prior_intents=prior),
            **payer.ctx,
        )
        result = evaluate(inp)
        rep.events += 1
        if isinstance(result, Repeat):
            rep.outcomes["repeat"] += 1
            return
        if isinstance(result, IntentConflict):
            rep.outcomes["intent_conflict"] += 1
            return
        det: Determination = result
        rep.outcomes[det.outcome] += 1
        for c in det.failing_clauses:
            rep.clauses[c.rule_id] += 1
        for f in det.clarify_fields:
            rep.clauses[f"clarify:{f}"] += 1
        for a in adversarial:
            rep.adversarial[f"{a}:{det.outcome}"] += 1
        if r.random() < 0.02:
            self.samples.append((inp, canonical(det.to_record())))
        self.ledger.append(payer.wheel.wheel_id, det.to_record(), self.host, record_id=det_id)
        self.intents[intent] = (payer.wheel.wheel_id, det.proposal_hash, det_id, body, spoke_id, caller)

        # invariants on the decision itself
        hard_adversarial = adversarial & {"wrong_caller", "unknown_spoke", "blocked_category", "unverified_po_claim", "no_po"}
        if det.outcome == "allow" and hard_adversarial:
            rep.violations.append(f"{det_id}: allowed despite {sorted(hard_adversarial)}")
        if det.outcome != "allow":
            return

        # the payer approved what was shown: stage 2 re-checks on fresh facts before any hold (02 Stage 2)
        det = evaluate(replace(inp, determination_id=det_id + "_auth", stage="authorisation", prior_intents={},
                               authorised_presented_hash=det.presented_hash))
        self.ledger.append(payer.wheel.wheel_id, det.to_record(), self.host, record_id=det.determination_id)
        if det.outcome != "allow":
            rep.violations.append(f"{det_id}: allowed at proposal but {det.outcome} at authorisation with nothing changed")
            return
        det_id = det.determination_id

        amount = body.total
        if det.approval_mode == "silent_with_notice":
            bp = spoke.instruction.price_tolerance_bp
            amount = inr(amount.amount_minor + (amount.amount_minor * bp + 9999) // 10000)
        accts = self._accounts(payer, chain)
        for a in accts:  # the evaluator said there was room: check against the shadow ledger
            if a.limit is not None:
                key = (a.owner, a.window.window_id(at))
                held, spent = self.shadow.get(key, [0, 0])
                if held + spent + amount.amount_minor > a.limit.amount_minor:
                    rep.violations.append(f"{det_id}: allowed with no room on {a.owner}")
        try:
            self.money.hold(payer.wheel.wheel_id, det_id, amount, accts)
        except HoldRefused as e:
            rep.violations.append(f"{det_id}: evaluator allowed but the ledger refused the hold: {e}")
            return
        self.allowed_ids.add(det_id)
        wid_ = {a.owner: a.window.window_id(at) for a in accts}
        for a in accts:
            self.limits[(a.owner, wid_[a.owner])] = a.limit.amount_minor if a.limit else None
            self.shadow.setdefault((a.owner, wid_[a.owner]), [0, 0])[0] += amount.amount_minor
        self.payments[det_id] = {"wheel": payer.wheel.wheel_id, "keys": [(a.owner, wid_[a.owner]) for a in accts],
                                 "held": amount.amount_minor, "captured": 0, "refunded": 0, "actual": body.total.amount_minor}
        self._execute(payer, det, body, spoke)

    BEHAVIOURS = (["ok"] * 78 + ["decline"] * 6 + ["timeout_after_create"] * 4 + ["timeout_before_create"] * 3
                  + ["error:RATE_LIMITED"] * 3 + ["error_after_create:RATE_LIMITED"] * 1 + ["processing"] * 2 + ["overcharge"] * 3)

    def _execute(self, payer: Payer, det: Determination, body: ProposalBody, spoke: Spoke) -> None:
        r, rep, m = self.rng, self.report, payer.merchant
        script = []
        for _ in range(3):
            b = r.choice(self.BEHAVIOURS)
            script.append(f"overcharge:{r.randint(100, 50_000)}" if b == "overcharge" else b)
        m.script = script
        rail = payer.ctx["rails"].get(det.rail_id)
        auth = Authorisation("standing_instruction") if det.approval_mode == "silent_with_notice" else Authorisation("payer_approval", True)
        try:
            if rail is None:
                raise ExecutionRefused("no rail")
            res = self.executor.execute(payer.wheel.wheel_id, det, body.merchant_cart_ref, body.destination_ref, rail, m, auth)
        except ExecutionRefused:
            rep.settlements["refused_at_execution"] += 1
            self._release(det.determination_id, "rail")
            return
        self.executions[det.determination_id] = (m, body.merchant_cart_ref, len(res.attempts), rail)
        rep.settlements[f"executed:{res.outcome}"] += 1
        if res.outcome == "success":
            self._capture(det.determination_id, res.charged.amount_minor)
        elif res.outcome == "awaiting_payer":
            if r.random() < 0.95:  # the payer completes the payment on the rail
                m.confirm_payment(res.merchant_order_ref)
                self._capture(det.determination_id, m.get_order(res.merchant_order_ref).charged.amount_minor)
            else:
                rep.settlements["payer_abandoned"] += 1
                self._release(det.determination_id, "rail")
        elif res.outcome == "failure":
            self._release(det.determination_id, "rail")
        else:  # unknown: the hold stays until the merchant's history settles it
            self.pending.append((self.clock.now() + timedelta(hours=r.randint(1, 48)), "resolve", det.determination_id))

    def _release(self, det_id: str, source: str) -> None:
        p = self.payments[det_id]
        self.money.release(p["wheel"], det_id, Signal(source, "definitive-failure"))
        for k in p["keys"]:
            self.shadow[k][0] -= p["held"]
        p["held"] = 0

    def _resolve(self, det_id: str) -> None:
        """An unknown outcome, settled later from the merchant's own record (reconciliation proper is Phase 6)."""
        m, cart_ref, _, _ = self.executions[det_id]
        orders = m.orders_for_cart(cart_ref)
        rep = self.report
        if not orders:
            rep.settlements["resolved:no_order"] += 1
            return self._release(det_id, "reconciliation")
        o = orders[0]
        if o.status in ("PENDING_PAYMENT", "PROCESSING", "processing"):
            m.confirm_payment(o.merchant_order_ref) if m.shape == "a" else None
            o = m.get_order(o.merchant_order_ref)
            charged = o.charged or o.total
        else:
            charged = o.charged or o.total
        rep.settlements["resolved:order_found"] += 1
        self._capture(det_id, charged.amount_minor)

    def _capture(self, det_id: str, actual: int) -> None:
        p, rep = self.payments[det_id], self.report
        wheel, held = p["wheel"], p["held"]
        rep.settlements["as_approved" if actual == p["actual"] else "higher" if actual > held else "lower"] += 1
        self.money.capture(wheel, det_id, inr(actual), Signal("merchant", f"order-{det_id}"))
        for k in p["keys"]:
            self.shadow[k][0] -= held
            self.shadow[k][1] += actual
            if actual > held:
                self.overcaptured.add(k)
        p["held"], p["captured"] = 0, actual
        if self.rng.random() < 0.05:
            self.pending.append((self.clock.now() + timedelta(days=self.rng.randint(1, 40)), "refund", det_id))

    def _settle_due(self) -> None:
        now = self.clock.now()
        due = [x for x in self.pending if x[0] <= now]
        self.pending = [x for x in self.pending if x[0] > now]
        for _, action, det_id in sorted(due):
            p = self.payments[det_id]
            if action == "resolve":
                self._resolve(det_id)
            elif action == "refund" and p["captured"] - p["refunded"] > 0:
                amt = self.rng.randint(1, p["captured"] - p["refunded"])
                self.money.refund(p["wheel"], det_id, inr(amt), Signal("merchant", f"refund-{det_id}"))
                for k in p["keys"]:
                    self.shadow[k][1] -= amt
                p["refunded"] += amt
                self.report.settlements["refund"] += 1

    # --- the run and the final checks -----------------------------------------------

    def run(self, events: int, minutes_per_event: int) -> Report:
        for _ in range(events):
            self.clock.advance(timedelta(minutes=self.rng.randint(1, 2 * minutes_per_event)))
            self.step()
        self.clock.advance(timedelta(days=60))
        self._settle_due()
        self._check()
        return self.report

    def _check(self) -> None:
        rep, v = self.report, self.report.violations
        integrity = self.ledger.verify_integrity(self.keylog.public_keys())
        rep.records, rep.chains = integrity.records_checked, integrity.chains_checked
        v += [f"integrity {c}: {m}" for c, m in integrity.problems]
        real: dict = {}
        for p in self.payers:
            for (owner, _cur, window_id), a in replay(self.ledger, p.wheel.wheel_id).accounts.items():
                real[(owner, window_id)] = [a.held, a.spent]
        if real != {k: s for k, s in self.shadow.items()}:
            diff = {k for k in set(real) | set(self.shadow) if real.get(k) != self.shadow.get(k)}
            v.append(f"shadow ledger and real ledger differ on {len(diff)} accounts, e.g. {sorted(diff)[:3]}")
        for k, (held, spent) in real.items():
            if held < 0 or spent < 0:
                v.append(f"{k}: negative balance held={held} spent={spent}")
            lim = self.limits.get(k)
            if lim is not None and held + spent > lim and k not in self.overcaptured:
                v.append(f"{k}: {held + spent} used of {lim} without an overcapture")
        for det_id, p in self.payments.items():
            if p["refunded"] > p["captured"]:
                v.append(f"{det_id}: refunded more than captured")
        for p in self.payers:
            for rec in self.ledger.records(p.wheel.wheel_id, "posting"):
                if rec["payment_ref"] not in self.allowed_ids:
                    v.append(f"posting for {rec['payment_ref']}, which was never allowed")
        for det_id, (m, cart_ref, n_attempts, rail) in self.executions.items():
            n_orders = len(m.orders_for_cart(cart_ref))
            if n_orders > 1:
                v.append(f"{det_id}: {n_orders} merchant orders for one decision")
            if n_attempts > rail.retry_policy.get("max_retries", 0) + 1:
                v.append(f"{det_id}: {n_attempts} attempts exceed the rail's retry limit")
        for p in self.payers:  # every order a merchant holds traces to an allowed decision
            for o in p.merchant.orders.values():
                if not any(c == o.cart_ref for (_m, c, _n, _r) in self.executions.values()):
                    v.append(f"{o.merchant_order_ref}: an order no decision placed")
        for inp, expected in self.samples:
            if canonical(evaluate(inp).to_record()) != expected:
                v.append(f"{inp.determination_id}: replay gave different bytes")


def run(conn, *, seed: int, payers: int, events: int, minutes_per_event: int = 15) -> Report:
    return Simulation(conn, seed, payers).run(events, minutes_per_event)
