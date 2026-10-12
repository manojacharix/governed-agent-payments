"""The proposal flow (02, 03, 04, 05 together): one request from an agent to a settled, provable payment.

propose()  the host captures the cart from the merchant itself, evaluates, and stores the exact evaluation
           input (encrypted, its hash in the ledger) so the decision can be replayed by anyone later.
approve() / override() / decline()   the payer acts on a Trusted Surface; the assertion is verified.
tick()     expires stale proposals, runs silent repeats whose notice has passed, and sweeps.
Approval runs stage 2: the cart is fetched again and must still match what was shown (or, for a
silent repeat, the instruction and its tolerance). Then: hold, execute, and a reconciliation sweep.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from gap_adapters.execute import Authorisation, ExecutionRefused, Executor
from gap_core.authority import Caller, Spoke, Wheel
from gap_core.clock import Clock
from gap_core.codec import encode
from gap_core.evaluate import (Determination, EvaluationInput, IntentConflict, PriorIntent, RecentProposal, Repeat, Usage,
                               can_override, evaluate)
from gap_core.facts import Fact, FactSet
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.money import Money
from gap_core.proposal import Extra, Proposal, ProposalBody, cart_hash, content_hash
from gap_core.timefmt import format_ts
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_ledger.money import Account, HoldRefused, MoneyInFlight, Signal, replay
from gap_profile.store import ProfileStore
from gap_recon.recon import BankFeed, Reconciler

from .surface import check


class FlowError(RuntimeError):
    """The request was refused before anything was recorded (unknown proposal, bad assertion, wrong state)."""


@dataclass
class Deployment:
    """Everything the host knows about one wheel: authority, configuration, merchants and money sources."""

    wheel: Wheel
    spokes: dict
    ctx: dict  # from gap_mapping.evaluation_context: params, rails, tier0_rules, jurisdiction_rules, jurisdiction
    adapters: dict  # adapter_id -> MerchantAdapter
    bank: BankFeed | None = None
    notice_duration_s: int = 300
    proposal_ttl_s: int = 900
    sweep_before_hold: bool = False


@dataclass(frozen=True)
class ProposalRequest:
    spoke_id: str
    caller: Caller
    intent_id: str
    merchant_ref: str
    adapter_id: str
    cart_ref: str
    destination_ref: str
    agent_confidence: int
    rail_id: str | None = None
    extras: tuple[Extra, ...] = ()
    substitution_allowed: bool | None = None
    facts: tuple[Fact, ...] = ()  # what the agent supplies; only verified sources can clear checks


@dataclass
class Pending:
    proposal_id: str
    wheel_id: str
    request: ProposalRequest
    determination: Determination
    status: str  # awaiting_approval | notice | executed | declined | expired | refused | superseded | clarify | no_authority | failed
    received_at: datetime
    notice_until: datetime | None = None
    notice_delivered: bool = False
    result: object = None
    events: list = field(default_factory=list)


class Flow:
    def __init__(self, ledger: Ledger, profile: ProfileStore, keylog: KeyLog, clock: Clock, rng, *, evaluator_digest: str,
                 max_pending_age_s: int = 3600, refund_stall_s: int = 3 * 86400) -> None:
        self.ledger, self.profile, self.keylog, self.clock, self.rng = ledger, profile, keylog, clock, rng
        self.signer = keylog.signer
        self.evaluator_digest = evaluator_digest
        self.money = MoneyInFlight(ledger, self.signer, clock, rng)
        self.executor = Executor(ledger, self.signer, clock, rng)
        self.recon = Reconciler(ledger, self.signer, clock, rng, self.money, max_pending_age_s=max_pending_age_s, refund_stall_s=refund_stall_s)
        self.deployments: dict[str, Deployment] = {}
        self.proposals: dict[str, Pending] = {}
        self.intents: dict[str, dict] = {}  # wheel_id -> {intent_id: PriorIntent}
        self.recent: dict[str, list] = {}

    # --- setup -----------------------------------------------------------------------------

    def register(self, dep: Deployment) -> None:
        """Record the wheel's and spokes' versions; their documents go to the profile store (19 §1.6)."""
        w = dep.wheel
        self.deployments[w.wheel_id] = dep
        self.intents.setdefault(w.wheel_id, {})
        self.recent.setdefault(w.wheel_id, [])
        for kind, doc, rid in [("wheel", w, w.wheel_id)] + [("spoke", s, s.spoke_id) for s in dep.spokes.values()]:
            ref, _ = self.profile.put(w.payer_ref, kind, encode(doc))
            self.ledger.append(w.wheel_id, {"record_type": f"{kind}_version", f"{kind}_id": rid, "version": doc.version,
                                            "document_hash": hash_value(encode(doc)), "document_ref": ref,
                                            "at": format_ts(self.clock.now())}, self.signer, record_id=rid)

    def enrol_surface(self, wheel_id: str, surface) -> None:
        self.keylog.enrol(surface.key_id, role="surface", principal_ref=self.deployments[wheel_id].wheel.payer_ref,
                          public_key=surface.signer.public_jwk, protection_class="software", enrolled_by="reference")

    # --- helpers ----------------------------------------------------------------------------

    def _chain(self, dep: Deployment, spoke_id: str) -> list[Spoke]:
        chain, cur = [], dep.spokes.get(spoke_id)
        while cur is not None:
            chain.append(cur)
            cur = dep.spokes.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def _accounts(self, dep: Deployment, spoke_id: str) -> list[Account]:
        budget = dep.wheel.budgets[0]
        accts = [Account(s.spoke_id, s.caps[0].window if s.caps else budget.window, s.caps[0].limit if s.caps else None)
                 for s in self._chain(dep, spoke_id)]
        return accts + [Account(dep.wheel.wheel_id, budget.window, budget.limit)]

    def _usage(self, dep: Deployment, spoke_id: str, at: datetime) -> dict:
        usage = {(a.owner, a.window.window_id(at)): Usage(0, {}) for a in self._accounts(dep, spoke_id)} if spoke_id in dep.spokes else {}
        usage.update(replay(self.ledger, dep.wheel.wheel_id).usage())
        return usage

    def _host_facts(self, dep: Deployment, req: ProposalRequest, extra: tuple = ()) -> FactSet:
        adapter = dep.adapters[req.adapter_id]
        host = [
            Fact("merchant.rails_offered", adapter.describe().rails_offered, "host", True, "capturer_signed"),
            Fact("findings.open_on_spoke", self.recon.open_tap_findings_by_spoke(dep.wheel.wheel_id).get(req.spoke_id, 0), "host", True, "injected"),
        ]
        names = {f.name for f in host + list(extra)}
        return FactSet([f for f in req.facts if f.name not in names] + host + list(extra))

    def _body(self, dep: Deployment, req: ProposalRequest) -> tuple[ProposalBody, object]:
        view = dep.adapters[req.adapter_id].get_cart(req.cart_ref)  # the host captures the cart itself (02 step 1)
        extras_total = sum(e.amount.amount_minor for e in req.extras if e.amount is not None)
        body = ProposalBody(req.merchant_ref, req.adapter_id, req.cart_ref, view.lines, Money(view.total.amount_minor + extras_total, view.total.currency),
                            req.destination_ref, req.agent_confidence, fees=view.fees, extras=req.extras, rail_id=req.rail_id,
                            substitution_allowed=req.substitution_allowed)
        return body, view

    def _evaluate(self, dep: Deployment, pid: str, det_id: str, req: ProposalRequest, body: ProposalBody, facts: FactSet, **kw) -> tuple[Determination, EvaluationInput]:
        at = self.clock.now()
        inp = EvaluationInput(
            det_id, Proposal(pid, dep.wheel.wheel_id, req.spoke_id, req.caller.caller_id, req.intent_id, body), req.caller,
            dep.wheel, dep.spokes, dep.ctx["tier0_rules"], dep.ctx["jurisdiction_rules"], dep.ctx["jurisdiction"], dep.ctx["rails"],
            dep.ctx["params"], facts, self._usage(dep, req.spoke_id, at), at,
            {"wheel": dep.wheel.version, "spoke": dep.spokes[req.spoke_id].version if req.spoke_id in dep.spokes else 0,
             "tier0": 1, "mapping": dep.ctx.get("mapping_ref", "unknown"), "evaluator": self.evaluator_digest}, **kw)
        return evaluate(inp), inp

    def _record_determination(self, dep: Deployment, det: Determination, inp: EvaluationInput) -> dict:
        encoded = encode(inp)
        ref, _ = self.profile.put(dep.wheel.payer_ref, "evaluation_input", encoded)
        rec = det.to_record() | {"input_hash": hash_value(encoded), "input_ref": ref}
        return self.ledger.append(dep.wheel.wheel_id, rec, self.signer, record_id=det.determination_id)

    # --- propose ----------------------------------------------------------------------------

    def propose(self, wheel_id: str, req: ProposalRequest):
        dep = self.deployments[wheel_id]
        body, view = self._body(dep, req)
        pid = new_id("prp", clock=self.clock, rng=self.rng)
        at = self.clock.now()
        window = timedelta(seconds=dep.ctx["params"].duplicate_window_s)
        recent = tuple(r for r in self.recent[wheel_id] if at - r.at <= window)
        det, inp = self._evaluate(dep, pid, new_id("det", clock=self.clock, rng=self.rng), req, body, self._host_facts(dep, req),
                                  prior_intents=dict(self.intents[wheel_id]), recent=recent)
        if isinstance(det, (Repeat, IntentConflict)):
            return det
        body_ref, body_commitment = self.profile.put(dep.wheel.payer_ref, "proposal_body", encode(body))
        self.ledger.append(wheel_id, {
            "record_type": "proposal", "proposal_id": pid, "wheel_id": wheel_id, "spoke_id": req.spoke_id,
            "caller_id": req.caller.caller_id, "intent_id": req.intent_id, "proposal_hash": det.proposal_hash,
            "body_commitment": body_commitment, "body_ref": body_ref, "total": body.total.to_json(), "merchant_ref": body.merchant_ref,
            "merchant_cart_ref": req.cart_ref, "content_hash": content_hash(view.lines, view.fees), "cart_hash": cart_hash(body),
            "received_at": format_ts(at),
        }, self.signer, record_id=pid)
        self._record_determination(dep, det, inp)
        self.intents[wheel_id][req.intent_id] = PriorIntent(det.proposal_hash, det.determination_id)
        self.recent[wheel_id].append(RecentProposal(pid, req.intent_id, body.merchant_ref, cart_hash(body), at))
        p = Pending(pid, wheel_id, req, det, det.outcome, at)
        if det.outcome == "allow" and det.approval_mode == "silent_with_notice":
            p.status, p.notice_until = "notice", at + timedelta(seconds=dep.notice_duration_s)
        elif det.outcome == "allow" or (det.outcome == "refuse" and det.presented_hash and all(c.overridable for c in det.failing_clauses)):
            p.status = "awaiting_approval"
        elif det.outcome == "refuse":
            p.status = "refused"
        self.proposals[pid] = p
        return p

    # --- the payer's actions --------------------------------------------------------------------

    def _verify(self, p: Pending, assertion: dict, action: str, subject: str) -> None:
        dep = self.deployments[p.wheel_id]
        key = self.keylog.state().get(assertion.get("surface_key_id"))
        if (key is None or key.principal_role != "surface" or key.status != "active" or key.principal_ref != dep.wheel.payer_ref
                or assertion.get("action") != action or assertion.get("proposal_id") != p.proposal_id
                or assertion.get("subject_hash") != subject or not check(assertion, key.public_key)):
            raise FlowError("assertion refused: wrong key, action, proposal, subject or signature")

    def _record_assertion(self, p: Pending, assertion: dict) -> str:
        aid = new_id("asr", clock=self.clock, rng=self.rng)
        self.ledger.append(p.wheel_id, {"record_type": "surface_assertion", "assertion_id": aid, **assertion}, self.signer, record_id=aid)
        return aid

    def _resolve(self, p: Pending, det_id: str, state: str, by: str, assertion_id=None, overridden=None, superseded_by=None) -> dict:
        rid = new_id("res", clock=self.clock, rng=self.rng)
        body = {"record_type": "resolution", "resolution_id": rid, "determination_id": det_id, "proposal_id": p.proposal_id,
                "state": state, "by": by, "occurred_at": format_ts(self.clock.now())}
        if assertion_id:
            body["assertion_id"] = assertion_id
        if overridden:
            body["overridden_clauses"] = sorted(overridden)
        if superseded_by:
            body["superseded_by"] = superseded_by
        return self.ledger.append(p.wheel_id, body, self.signer, record_id=rid)

    def _get(self, proposal_id: str, *states) -> Pending:
        p = self.proposals.get(proposal_id)
        if p is None or p.status not in states:
            raise FlowError(f"proposal {proposal_id} is not {' or '.join(states)}")
        return p

    def approve(self, proposal_id: str, assertion: dict):
        p = self._get(proposal_id, "awaiting_approval")
        if p.determination.outcome != "allow":
            raise FlowError("a refused proposal can only be overridden, not approved")
        self._verify(p, assertion, "authorise", p.determination.presented_hash)
        return self._authorise(p, "approved", self._record_assertion(p, assertion))

    def override(self, proposal_id: str, clause_ids: list[str], assertion: dict):
        p = self._get(proposal_id, "awaiting_approval")
        if not can_override(p.determination, clause_ids):
            raise FlowError("only payer-authored clauses can be overridden, and every failing clause must be named")
        subject = hash_value({"presented_hash": p.determination.presented_hash, "overridden": sorted(clause_ids)})
        self._verify(p, assertion, "override", subject)
        return self._authorise(p, "approved_with_override", self._record_assertion(p, assertion), set(clause_ids))

    def decline(self, proposal_id: str, assertion: dict) -> Pending:
        p = self._get(proposal_id, "awaiting_approval", "notice")
        self._verify(p, assertion, "decline", p.determination.presented_hash or p.determination.proposal_hash)
        self._resolve(p, p.determination.determination_id, "declined", "payer", self._record_assertion(p, assertion))
        p.status = "declined"
        return p

    def notice_delivered(self, proposal_id: str) -> None:
        """The host received the device's delivery receipt for a silent repeat's notice."""
        self._get(proposal_id, "notice").notice_delivered = True

    # --- time passes ------------------------------------------------------------------------------

    def tick(self, *, sweep: bool = False) -> list:
        now, out = self.clock.now(), []
        for p in list(self.proposals.values()):
            dep = self.deployments[p.wheel_id]
            if p.status == "awaiting_approval" and now - p.received_at > timedelta(seconds=dep.proposal_ttl_s):
                self._resolve(p, p.determination.determination_id, "expired", "system")
                p.status = "expired"
                out.append(p)
            elif p.status == "notice" and now >= p.notice_until:
                out.append(self._authorise(p, "proceeded_on_silence", None))
        if sweep:
            for wid, dep in self.deployments.items():
                self.recon.sweep(wid, dep.adapters, dep.bank, trigger="scheduled")
        return out

    # --- stage 2, hold, execute, reconcile ------------------------------------------------------------

    def _authorise(self, p: Pending, kind: str, assertion_id: str | None, overridden: set = frozenset()):
        dep = self.deployments[p.wheel_id]
        req, det1 = p.request, p.determination
        body, _view = self._body(dep, req)
        extra = (Fact("notice.delivered", p.notice_delivered, "host", True, "injected"),) if kind == "proceeded_on_silence" else ()
        det2, inp2 = self._evaluate(dep, p.proposal_id, new_id("det", clock=self.clock, rng=self.rng), req, body,
                                    self._host_facts(dep, req, extra), stage="authorisation", authorised_presented_hash=det1.presented_hash)
        self._record_determination(dep, det2, inp2)
        p.events.append(det2)
        ok = det2.outcome == "allow" or (kind == "approved_with_override" and det2.outcome == "refuse" and can_override(det2, overridden))
        if kind == "proceeded_on_silence" and ok and det2.approval_mode != "silent_with_notice":
            p.status = "awaiting_approval"  # silence no longer allowed (e.g. notice not delivered): fall back to a tap
            return p
        if not ok:
            if any(c.rule_id == "sys.drift" for c in det2.failing_clauses):
                new = self.propose(p.wheel_id, replace(req, intent_id=f"{req.intent_id}:re{len(p.events)}"))
                self._resolve(p, det2.determination_id, "superseded", "system", superseded_by=getattr(new, "proposal_id", None))
                p.status, p.result = "superseded", new
                return new  # re-presented to the payer
            self._resolve(p, det2.determination_id, "declined", "system")
            p.status = "refused"
            return p
        by = "system" if kind == "proceeded_on_silence" else "payer"
        self._resolve(p, det2.determination_id, kind, by, assertion_id, overridden or None)
        if dep.sweep_before_hold:
            self.recon.sweep(p.wheel_id, dep.adapters, dep.bank, trigger="before_hold")
        amount = body.total
        if det2.approval_mode == "silent_with_notice":
            bp = dep.spokes[req.spoke_id].instruction.price_tolerance_bp
            amount = Money(amount.amount_minor + (amount.amount_minor * bp + 9999) // 10000, amount.currency)
        try:
            self.money.hold(p.wheel_id, det2.determination_id, amount, self._accounts(dep, req.spoke_id))
        except HoldRefused:
            p.status = "failed"
            return p
        rail = dep.ctx["rails"].get(det2.rail_id)
        auth = (Authorisation("standing_instruction") if kind == "proceeded_on_silence"
                else Authorisation("payer_approval", no_auth_rail_ok=True, overridden=frozenset(overridden)))
        try:
            if rail is None:
                raise ExecutionRefused("no rail")
            res = self.executor.execute(p.wheel_id, det2, req.cart_ref, req.destination_ref, rail, dep.adapters[req.adapter_id], auth)
        except ExecutionRefused:
            self.money.release(p.wheel_id, det2.determination_id, Signal("reconciliation", "not-executed"))
            p.status = "failed"
            return p
        if res.outcome == "failure":  # a definitive decline from the merchant
            self.money.release(p.wheel_id, det2.determination_id, Signal("merchant", "declined"))
        else:
            self.recon.sweep(p.wheel_id, dep.adapters, dep.bank, trigger="event")
        p.status, p.result = "executed", res
        return p
