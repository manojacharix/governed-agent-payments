"""The proposal flow (02, 03, 04, 05 together): one request from an agent to a settled, provable payment.

propose()  the host captures the cart from the merchant itself, evaluates, and stores the exact evaluation
           input (encrypted, its hash in the ledger) so the decision can be replayed by anyone later.
approve() / override() / decline()   the payer acts on a Trusted Surface; the assertion is verified.
tick()     expires stale proposals, runs silent repeats whose notice has passed, and sweeps.
Approval runs stage 2: the cart is fetched again and must still match what was shown (or, for a
silent repeat, the instruction and its tolerance). Then: hold, execute, and a reconciliation sweep.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from gap_adapters.execute import Authorisation, ExecutionRefused, ExecutionResult, Executor
from gap_adapters.interface import MerchantError
from gap_core.canonical import CanonicalError
from gap_core.authority import Caller, Spoke, Wheel
from gap_core.clock import Clock
from gap_core.codec import decode, encode
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

from gap_identity.identity import Identity, IdentityError, atomic
from gap_state.store import Kind, MemoryStore


class FlowError(RuntimeError):
    """The request was refused before anything was recorded (unknown proposal, bad assertion, wrong state)."""


@dataclass(frozen=True)
class Rejected:
    """A proposal refused before evaluation: the caller could not prove who it is (07 §4.2)."""

    reason: str  # sys.proof_invalid | sys.proof_stale | sys.nonce_reused | sys.caller_not_bound


@dataclass(frozen=True)
class Invalid:
    """A proposal the host could not even build: unknown adapter, a cart the merchant cannot return, or a cart that
    breaks the money model (mixed currencies, totals beyond 2^53, malformed strings). Nothing is recorded."""

    code: str  # unknown_adapter | cart_unavailable | cart_invalid | merchant_mismatch | extras_invalid
    detail: str = ""


class _CartProblem(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def request_digest(req) -> str:
    """What a caller's proof covers: the request, not the cart the host will capture."""
    return hash_value({"spoke_id": req.spoke_id, "intent_id": req.intent_id, "merchant_ref": req.merchant_ref, "adapter_id": req.adapter_id,
                       "cart_ref": req.cart_ref, "destination_ref": req.destination_ref, **({"rail_id": req.rail_id} if req.rail_id else {})})


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
    fact_sources: tuple = ()  # host-side sources (e.g. an ERP): callables(request) -> [Fact], captured by the host
    executing_grace_s: int = 600  # how long a payment may sit "executing" before recovery asks the merchant what happened


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
    executing_since: datetime | None = None  # set at the point of no return; a crash leaves it for recovery


def _dump_result(r):
    if r is None:
        return {"kind": "none"}
    if isinstance(r, ExecutionResult):
        return {"kind": "execution", **{f.name: encode(getattr(r, f.name)) for f in dataclasses.fields(r)}}
    if isinstance(r, Invalid):
        return {"kind": "invalid", "code": r.code, "detail": r.detail}
    if isinstance(r, Pending):  # superseded: the proposal that replaced it
        return {"kind": "proposal", "proposal_id": r.proposal_id}
    if isinstance(r, str):
        return {"kind": "proposal", "proposal_id": r}
    raise TypeError(f"cannot store result {type(r).__name__}")


def _load_result(d):
    kind = d["kind"]
    if kind == "execution":
        return ExecutionResult(**{k: decode(v) for k, v in d.items() if k != "kind"})
    if kind == "invalid":
        return Invalid(d["code"], d["detail"])
    if kind == "proposal":
        return d["proposal_id"]  # the replacing proposal, by id
    return None


def dump_pending(p: "Pending") -> dict:
    return {"proposal_id": p.proposal_id, "wheel_id": p.wheel_id,
            "request": {f.name: encode(getattr(p.request, f.name)) for f in dataclasses.fields(p.request)},
            "determination": encode(p.determination), "status": p.status, "received_at": encode(p.received_at),
            "notice_until": encode(p.notice_until), "notice_delivered": p.notice_delivered, "result": _dump_result(p.result),
            "events": [encode(e) for e in p.events], "executing_since": encode(p.executing_since)}


def load_pending(d: dict) -> "Pending":
    return Pending(d["proposal_id"], d["wheel_id"], ProposalRequest(**{k: decode(v) for k, v in d["request"].items()}),
                   decode(d["determination"]), d["status"], decode(d["received_at"]), decode(d["notice_until"]), d["notice_delivered"],
                   _load_result(d["result"]), [decode(e) for e in d["events"]], decode(d.get("executing_since", {"$t": "none"})))


ACTIVE = ("awaiting_approval", "notice", "executing")


class Flow:
    """Working state (pending proposals, prior intents, the duplicate window) lives in a Store: in memory by default,
    or Postgres (gap_state.PgStore) so several host processes share it and any of them can restart (19 §12)."""

    def __init__(self, ledger: Ledger, profile: ProfileStore, keylog: KeyLog, identity: Identity, clock: Clock, rng, *, evaluator_digest: str,
                 max_pending_age_s: int = 3600, refund_stall_s: int = 3 * 86400, store=None) -> None:
        self.ledger, self.profile, self.keylog, self.identity, self.clock, self.rng = ledger, profile, keylog, identity, clock, rng
        self.signer = keylog.signer
        self.evaluator_digest = evaluator_digest
        self.money = MoneyInFlight(ledger, self.signer, clock, rng)
        self.executor = Executor(ledger, self.signer, clock, rng)
        self.recon = Reconciler(ledger, self.signer, clock, rng, self.money, max_pending_age_s=max_pending_age_s, refund_stall_s=refund_stall_s)
        self.deployments: dict[str, Deployment] = {}  # configuration: every process registers the same deployments
        self.store = store or identity.store or MemoryStore()
        payer = lambda wid: self.deployments[wid].wheel.payer_ref if wid in self.deployments else ""  # noqa: E731
        self.proposals = self.store.table(Kind("proposal", dump_pending, load_pending, scope=lambda p: payer(p.wheel_id),
                                               tag=lambda p: p.wheel_id, status=lambda p: p.status))
        # wheel|intent -> {"wheel_id", "intent_id", "prior": PriorIntent}
        self.intents = self.store.table(Kind("intent", encode, decode, scope=lambda x: payer(x["wheel_id"]), tag=lambda x: x["wheel_id"]))
        # proposal_id -> {"wheel_id", "recent": RecentProposal}: the duplicate window
        self.recent = self.store.table(Kind("recent", encode, decode, scope=lambda x: payer(x["wheel_id"]), tag=lambda x: x["wheel_id"]))

    # --- setup -----------------------------------------------------------------------------

    def register(self, dep: Deployment) -> None:
        """Record the wheel's and spokes' versions; their documents go to the profile store (19 §1.6)."""
        w = dep.wheel
        for key, ad in dep.adapters.items():  # reconciliation finds adapters by the id recorded at execution
            if ad.describe().adapter_id != key:
                raise FlowError(f"adapter registered as {key!r} reports itself as {ad.describe().adapter_id!r}")
        self.deployments[w.wheel_id] = dep
        with self.store.unit(w.payer_ref):  # idempotent: a second process (or a restart) registering the same documents adds nothing
            self._record_versions(dep)

    def _record_versions(self, dep: Deployment) -> None:
        w = dep.wheel
        recorded = {(r["record_type"], r.get("wheel_id") or r.get("spoke_id"), r["version"]): r["document_hash"]
                    for r in self.ledger.records(w.wheel_id) if r["record_type"] in ("wheel_version", "spoke_version")}
        for kind, doc, rid in [("wheel", w, w.wheel_id)] + [("spoke", s, s.spoke_id) for s in dep.spokes.values()]:
            h = hash_value(encode(doc))
            seen = recorded.get((f"{kind}_version", rid, doc.version))
            if seen == h:
                continue
            if seen is not None:
                raise FlowError(f"{kind} {rid} version {doc.version} is already recorded with different content; bump the version")
            ref, _ = self.profile.put(w.payer_ref, kind, encode(doc))
            self.ledger.append(w.wheel_id, {"record_type": f"{kind}_version", f"{kind}_id": rid, "version": doc.version,
                                            "document_hash": hash_value(encode(doc)), "document_ref": ref,
                                            "at": format_ts(self.clock.now())}, self.signer, record_id=rid)

    def _payer_of_wheel(self, wheel_id: str) -> str | None:
        dep = self.deployments.get(wheel_id)
        return dep.wheel.payer_ref if dep else None

    def _wheel(self, dep: Deployment) -> Wheel:
        """The wheel as evaluated: suspended while a surface key is compromised (07 §2, 01 §5)."""
        return replace(dep.wheel, status="suspended") if self.identity.wheel_suspended(dep.wheel.payer_ref) else dep.wheel

    # --- helpers ----------------------------------------------------------------------------

    def _chain(self, dep: Deployment, spoke_id: str) -> list[Spoke]:
        chain, cur = [], dep.spokes.get(spoke_id)
        while cur is not None:
            chain.append(cur)
            cur = dep.spokes.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def _accounts(self, dep: Deployment, spoke_id: str, currency: str) -> list[Account]:
        """The accounts a payment in `currency` posts to: each spoke's cap and the wheel's budget in that currency.
        A capped spoke with no cap in the currency, or a wheel with no budget in it, has no room (as evaluate() says)."""
        budget = next((b for b in dep.wheel.budgets if b.limit.currency == currency), None)
        if budget is None:
            raise HoldRefused(f"{dep.wheel.wheel_id}: no budget in {currency}")
        accts = []
        for s in self._chain(dep, spoke_id):
            cap = next((c for c in s.caps if c.limit.currency == currency), None)
            if s.caps and cap is None:
                raise HoldRefused(f"{s.spoke_id}: no cap in {currency}")
            accts.append(Account(s.spoke_id, cap.window if cap else budget.window, cap.limit if cap else None))
        return accts + [Account(dep.wheel.wheel_id, budget.window, budget.limit)]

    def _usage(self, dep: Deployment, spoke_id: str, at: datetime) -> dict:
        """Usage for every (owner, window) evaluate() may look up: each cap and budget, and the window of every
        aggregate and velocity rule in force, counted by when each hold was placed (so rolling windows and rule
        windows that differ from the cap's are exact). Two windows sharing an id keep the larger figures."""
        if spoke_id not in dep.spokes:
            return {}
        w = dep.wheel.wheel_id
        chain = self._chain(dep, spoke_id)
        pairs = [(s.spoke_id, c.window) for s in chain for c in s.caps] + [(w, b.window) for b in dep.wheel.budgets]
        if any(not s.caps for s in chain):
            pairs += [(s.spoke_id, b.window) for s in chain if not s.caps for b in dep.wheel.budgets]
        ruled = [(r, w) for r in (*dep.ctx["jurisdiction_rules"], *dep.ctx["tier0_rules"], *dep.wheel.rules)]
        ruled += [(r, s.spoke_id) for s in chain for r in s.rules]
        for r, owner in ruled:
            if r.dimension == "velocity":
                pairs.append((owner, r.constraint["window"]))
            elif r.dimension == "amount" and "aggregate" in r.constraint:
                pairs.append((owner, r.constraint["aggregate"]["window"]))
        st = replay(self.ledger, w)
        usage: dict = {}
        for owner, window in pairs:
            key, u = (owner, window.window_id(at)), st.usage_in(owner, window, at)
            if key in usage:
                old = usage[key]
                u = Usage(max(old.count, u.count), {c: max(old.amounts.get(c, 0), u.amounts.get(c, 0)) for c in {*old.amounts, *u.amounts}})
            usage[key] = u
        return usage

    def _host_facts(self, dep: Deployment, req: ProposalRequest, extra: tuple = ()) -> FactSet:
        adapter = dep.adapters[req.adapter_id]
        host = [
            Fact("merchant.rails_offered", adapter.describe().rails_offered, "host", True, "capturer_signed"),
            Fact("findings.open_on_spoke", self.recon.open_tap_findings_by_spoke(dep.wheel.wheel_id).get(req.spoke_id, 0), "host", True, "injected"),
            Fact("recovery.pending", self.identity.recovery_pending(dep.wheel.payer_ref), "host", True, "injected"),
        ]
        for source in dep.fact_sources:
            host += list(source(req))
        names = {f.name for f in host + list(extra)}
        return FactSet([f for f in req.facts if f.name not in names] + host + list(extra))

    def _body(self, dep: Deployment, req: ProposalRequest) -> tuple[ProposalBody, object]:
        """The host captures the cart itself (02 step 1). Anything the merchant returns that the model cannot hold is a
        _CartProblem, never a crash: the merchant is outside the trust boundary."""
        adapter = dep.adapters.get(req.adapter_id)
        if adapter is None:
            raise _CartProblem("unknown_adapter", req.adapter_id)
        try:
            view = adapter.get_cart(req.cart_ref)
        except (KeyError, MerchantError) as e:
            raise _CartProblem("cart_unavailable", str(e)) from None
        except (ValueError, TypeError, ArithmeticError) as e:
            raise _CartProblem("cart_invalid", str(e)) from None
        # The merchant is the one the cart is at, never the name the agent cites: otherwise an agent could cite an
        # allowed merchant and send the order through an adapter (or a marketplace seller) the rules never named.
        if view.merchant_ref != req.merchant_ref:
            raise _CartProblem("merchant_mismatch", f"cart {req.cart_ref!r} is not at {req.merchant_ref!r}")
        # Extras the agent adds (a tip, packaging) can only add to what the merchant charges: a "discount" the
        # merchant never gave would lower the total every cap, budget and per-payment limit is checked against.
        for e in req.extras:
            if e.amount is not None and (not isinstance(e.amount, Money) or e.amount.amount_minor < 0
                                         or e.amount.currency != view.total.currency):
                raise _CartProblem("extras_invalid", f"{e.kind}: an extra is a non-negative amount in the cart's currency")
        try:
            extras_total = sum(e.amount.amount_minor for e in req.extras if e.amount is not None)
            body = ProposalBody(req.merchant_ref, req.adapter_id, req.cart_ref, view.lines, Money(view.total.amount_minor + extras_total, view.total.currency),
                                req.destination_ref, req.agent_confidence, fees=view.fees, extras=req.extras, rail_id=req.rail_id,
                                substitution_allowed=req.substitution_allowed)
            encode(body)  # every string and number must be representable before anything is recorded
        except (ValueError, TypeError, ArithmeticError, CanonicalError) as e:
            raise _CartProblem("cart_invalid", str(e)) from None
        return body, view

    def _evaluate(self, dep: Deployment, pid: str, det_id: str, req: ProposalRequest, body: ProposalBody, facts: FactSet, **kw) -> tuple[Determination, EvaluationInput]:
        at = self.clock.now()
        inp = EvaluationInput(
            det_id, Proposal(pid, dep.wheel.wheel_id, req.spoke_id, req.caller.caller_id, req.intent_id, body), req.caller,
            self._wheel(dep), dep.spokes, dep.ctx["tier0_rules"], dep.ctx["jurisdiction_rules"], dep.ctx["jurisdiction"], dep.ctx["rails"],
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

    @atomic(lambda self, a: self._payer_of_wheel(a["wheel_id"]))
    def propose(self, wheel_id: str, req: ProposalRequest, proof: dict):
        dep = self.deployments[wheel_id]
        try:  # who is proposing, proven; a claimed name never reaches the evaluator
            caller = self.identity.authenticate_proposal(proof, payer_ref=dep.wheel.payer_ref, intent_id=req.intent_id, digest=request_digest(req))
        except IdentityError as e:
            return Rejected(e.reason if e.reason.startswith("sys.") else "sys.proof_invalid")
        return self._propose_authenticated(wheel_id, replace(req, caller=caller))

    def _propose_authenticated(self, wheel_id: str, req: ProposalRequest):
        dep = self.deployments[wheel_id]
        try:
            body, view = self._body(dep, req)
        except _CartProblem as e:
            return Invalid(e.code, e.detail)
        pid = new_id("prp", clock=self.clock, rng=self.rng)
        at = self.clock.now()
        window = timedelta(seconds=dep.ctx["params"].duplicate_window_s)
        recent = []
        for x in self.recent.where(tag=wheel_id):
            if at - x["recent"].at <= window:
                recent.append(x["recent"])
            else:
                self.recent.pop(x["recent"].proposal_id)  # outside the window: no longer needed
        recent = tuple(sorted(recent, key=lambda r: (r.at, r.proposal_id)))
        prior = self.intents.get(f"{wheel_id}|{req.intent_id}")
        det, inp = self._evaluate(dep, pid, new_id("det", clock=self.clock, rng=self.rng), req, body, self._host_facts(dep, req),
                                  prior_intents={req.intent_id: prior["prior"]} if prior else {}, recent=recent)
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
        self.intents[f"{wheel_id}|{req.intent_id}"] = {"wheel_id": wheel_id, "intent_id": req.intent_id,
                                                       "prior": PriorIntent(det.proposal_hash, det.determination_id)}
        self.recent[pid] = {"wheel_id": wheel_id, "recent": RecentProposal(pid, req.intent_id, body.merchant_ref, cart_hash(body), at)}
        p = Pending(pid, wheel_id, req, det, det.outcome, at)
        if det.outcome == "allow" and det.approval_mode == "silent_with_notice":
            p.status, p.notice_until = "notice", at + timedelta(seconds=dep.notice_duration_s)
        elif det.outcome == "allow" or (det.outcome == "refuse" and det.presented_hash and all(c.overridable for c in det.failing_clauses)):
            p.status = "awaiting_approval"
        elif det.outcome == "refuse":
            p.status = "refused"
        self.proposals[pid] = p
        return p

    def _repropose(self, wheel_id: str, req: ProposalRequest):
        """Re-present after drift: the same authenticated caller, a derived intent, the cart as it is now."""
        return self._propose_authenticated(wheel_id, req)

    # --- the payer's actions --------------------------------------------------------------------

    @staticmethod
    def subject(p: Pending, action: str, overridden: list[str] | None = None) -> str:
        det = p.determination
        if action == "override":
            return hash_value({"presented_hash": det.presented_hash, "overridden": sorted(overridden or [])})
        return det.presented_hash or det.proposal_hash

    @atomic(lambda self, a: self.proposals.scope_of(a["proposal_id"]))
    def challenge(self, proposal_id: str, action: str, overridden: list[str] | None = None) -> dict:
        """What the payer's device signs: issued when the proposal is presented (07 §3)."""
        p = self.proposals[proposal_id]
        payer = self.deployments[p.wheel_id].wheel.payer_ref
        return self.identity.challenge(payer_ref=payer, action=action, subject_hash=self.subject(p, action, overridden))

    def _verify(self, p: Pending, assertion: dict, action: str, overridden: list[str] | None = None) -> dict:
        dep = self.deployments[p.wheel_id]
        det = p.determination
        required = {"override": "auth.strength.override"}.get(action)
        try:
            info = self.identity.verify_assertion(assertion, payer_ref=dep.wheel.payer_ref, action=action,
                                                  subject_hash=self.subject(p, action, overridden), required=required)
        except IdentityError as e:
            raise FlowError(f"assertion refused: {e}") from None
        rail = dep.ctx["rails"].get(det.rail_id)
        info["no_auth_rail_ok"] = bool(rail) and self.identity._rank(info["strength"]) >= self.identity._rank(self.identity.params["auth.strength.no_auth_rail"])
        info["subject_hash"] = self.subject(p, action, overridden)
        return info

    def _record_assertion(self, p: Pending, info: dict, action: str) -> str:
        device = {k: info[k] for k in ("challenge", "device_proof", "relying_party") if k in info}
        self.ledger.append(p.wheel_id, {"record_type": "surface_assertion", "assertion_id": info["assertion_id"], "action": action,
                                        "proposal_id": p.proposal_id, "subject_hash": info["subject_hash"], "surface_key_id": info["key_id"],
                                        "auth_strength_achieved": info["strength"], "occurred_at": format_ts(self.clock.now()), **device},
                           self.signer, record_id=info["assertion_id"])
        return info["assertion_id"]

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

    @atomic(lambda self, a: self.proposals.scope_of(a["proposal_id"]))
    def approve(self, proposal_id: str, assertion: dict):
        p = self._get(proposal_id, "awaiting_approval")
        if p.determination.outcome != "allow":
            raise FlowError("a refused proposal can only be overridden, not approved")
        info = self._verify(p, assertion, "authorise")
        return self._authorise(p, "approved", self._record_assertion(p, info, "authorise"), no_auth_rail_ok=info["no_auth_rail_ok"])

    @atomic(lambda self, a: self.proposals.scope_of(a["proposal_id"]))
    def override(self, proposal_id: str, clause_ids: list[str], assertion: dict):
        p = self._get(proposal_id, "awaiting_approval")
        if not can_override(p.determination, clause_ids):
            raise FlowError("only payer-authored clauses can be overridden, and every failing clause must be named")
        info = self._verify(p, assertion, "override", clause_ids)
        return self._authorise(p, "approved_with_override", self._record_assertion(p, info, "override"), set(clause_ids),
                               no_auth_rail_ok=info["no_auth_rail_ok"])

    @atomic(lambda self, a: self.proposals.scope_of(a["proposal_id"]))
    def decline(self, proposal_id: str, assertion: dict) -> Pending:
        p = self._get(proposal_id, "awaiting_approval", "notice")
        info = self._verify(p, assertion, "decline")
        self._resolve(p, p.determination.determination_id, "declined", "payer", self._record_assertion(p, info, "decline"))
        p.status = "declined"
        return p

    @atomic(lambda self, a: self.proposals.scope_of(a["proposal_id"]))
    def notice_delivered(self, proposal_id: str) -> None:
        """The host received the device's delivery receipt for a silent repeat's notice."""
        self._get(proposal_id, "notice").notice_delivered = True

    # --- time passes ------------------------------------------------------------------------------

    def tick(self, *, sweep: bool = False) -> list:
        """Expiries, silent repeats whose notice has run, due recoveries and sweeps. Each proposal is handled in its own
        unit under its payer's lock and re-read after locking, so two processes ticking at once never act twice."""
        now, out = self.clock.now(), []
        for seen in self.proposals.where(status=("awaiting_approval", "notice", "executing")):
            if seen.wheel_id not in self.deployments:
                continue
            with self.store.unit(self._payer_of_wheel(seen.wheel_id)):
                p = self.proposals.get(seen.proposal_id)
                dep = self.deployments[p.wheel_id]
                if p.status == "executing":
                    if p.executing_since is None or now - p.executing_since > timedelta(seconds=dep.executing_grace_s):
                        out.append(self._recover_executing(p))
                elif p.status == "awaiting_approval" and now - p.received_at > timedelta(seconds=dep.proposal_ttl_s):
                    self._resolve(p, p.determination.determination_id, "expired", "system")
                    p.status = "expired"
                    out.append(p)
                elif p.status == "notice" and now >= p.notice_until:
                    out.append(self._authorise(p, "proceeded_on_silence", None))
        self.identity.tick()  # delayed recoveries that are due
        if sweep:
            for wid, dep in self.deployments.items():
                with self.store.unit(dep.wheel.payer_ref):
                    self.recon.sweep(wid, dep.adapters, dep.bank, trigger="scheduled")
        return out

    def _recover_executing(self, p: Pending) -> Pending:
        """A payment stuck "executing" past its grace period: its process died between committing the approval and
        recording the merchant's answer. Settle it from the ledger and the merchant, never by placing an order."""
        dep = self.deployments[p.wheel_id]
        det2 = p.events[-1]  # the authorisation-stage determination the approval executed
        attempts = [a for a in self.ledger.records(p.wheel_id, "execution_attempt") if a["determination_id"] == det2.determination_id]
        payment = replay(self.ledger, p.wheel_id).payments.get(det2.determination_id)
        held = payment is not None and payment.held > 0
        if attempts:  # the merchant's answer was recorded; only the status update was lost
            outcome = attempts[-1]["outcome"]
            if outcome == "failure" and held:
                self.money.release(p.wheel_id, det2.determination_id, Signal("merchant", "declined"))
            p.status = "failed" if outcome == "failure" else "executed"
            p.result = ExecutionResult(outcome, attempts[-1].get("merchant_order_ref"), None, attempts, needs_reconciliation=True)
            return p
        if not held:  # died before the hold: nothing reserved, no order possible
            p.status = "failed"
            return p
        rail = dep.ctx["rails"].get(det2.rail_id)
        adapter = dep.adapters.get(p.request.adapter_id)
        if rail is None or adapter is None:
            # Money is held and only the merchant can say what happened, but its adapter or rail is not configured on
            # this process. Fail closed: leave it "executing" (still counted against the cap, never released on a guess)
            # and retry on every tick until the configuration is back.
            return p
        since = (p.executing_since or p.received_at) - timedelta(minutes=1)
        res, absent = self.executor.recover(p.wheel_id, det2, p.request.cart_ref, p.request.destination_ref, rail, adapter, since)
        if absent:  # proven never placed: give the room back
            self.money.release(p.wheel_id, det2.determination_id, Signal("reconciliation", "not-executed"))
            p.status, p.result = "failed", res
            return p
        # found (recorded, reconciliation captures it) or not provable (recorded unknown: reconciliation chases it and
        # opens a finding if it stays unresolved, so the hold is always explained)
        self.recon.sweep(p.wheel_id, dep.adapters, dep.bank, trigger="event")
        p.status, p.result = "executed", res
        return p

    # --- stage 2, hold, execute, reconcile ------------------------------------------------------------

    def _authorise(self, p: Pending, kind: str, assertion_id: str | None, overridden: set = frozenset(), *, no_auth_rail_ok: bool = False):
        dep = self.deployments[p.wheel_id]
        req, det1 = p.request, p.determination
        try:
            body, _view = self._body(dep, req)
        except _CartProblem as e:  # the cart vanished or went bad since it was shown: refuse, recorded, never execute
            self._resolve(p, det1.determination_id, "declined", "system")
            p.status, p.result = "refused", Invalid(e.code, e.detail)
            return p
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
                new = self._repropose(p.wheel_id, replace(req, intent_id=f"{req.intent_id}:re{len(p.events)}"))
                self._resolve(p, det2.determination_id, "superseded", "system", superseded_by=getattr(new, "proposal_id", None))
                p.status, p.result = "superseded", new
                return new  # re-presented to the payer
            self._resolve(p, det2.determination_id, "declined", "system")
            p.status = "refused"
            return p
        by = "system" if kind == "proceeded_on_silence" else "payer"
        self._resolve(p, det2.determination_id, kind, by, assertion_id, overridden or None)
        # The point of no return: commit before money is held or a merchant is called. If the process dies from here on,
        # the proposal stays "executing" (never back to awaiting the payer, so it cannot be approved twice) and
        # reconciliation settles the money from the ledger and the merchant (05).
        p.status, p.executing_since = "executing", self.clock.now()
        self.store.checkpoint()
        if dep.sweep_before_hold:
            self.recon.sweep(p.wheel_id, dep.adapters, dep.bank, trigger="before_hold")
        amount = body.total
        if det2.approval_mode == "silent_with_notice":
            bp = dep.spokes[req.spoke_id].instruction.price_tolerance_bp
            amount = Money(amount.amount_minor + (amount.amount_minor * bp + 9999) // 10000, amount.currency)
        try:
            self.money.hold(p.wheel_id, det2.determination_id, amount, self._accounts(dep, req.spoke_id, amount.currency))
        except HoldRefused:
            p.status = "failed"
            return p
        rail = dep.ctx["rails"].get(det2.rail_id)
        auth = (Authorisation("standing_instruction") if kind == "proceeded_on_silence"
                else Authorisation("payer_approval", no_auth_rail_ok=no_auth_rail_ok, overridden=frozenset(overridden)))
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
