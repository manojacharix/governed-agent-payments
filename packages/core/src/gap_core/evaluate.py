"""evaluate(): one proposal, frozen inputs, one decision (02 Stage 1 and Stage 2).

Pure. Everything it needs arrives in EvaluationInput: authority versions, frozen facts, usage
figures for the windows involved, prior intents, recent proposals and the injected time.
It returns one of: Determination, Repeat (same intent, same body) or IntentConflict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .authority import ASSURANCE_RANK, ATTESTATION_RANK, AUTH_CLASS_RANK, DAYS, Caller, Params, RailProfile, Rule, Spoke, Wheel
from .facts import FactSet, evaluate_predicate
from .money import Money
from .proposal import Proposal, cart_hash, presented_hash, proposal_hash
from .timefmt import format_ts

OUTCOME_RANK = {"allow": 0, "clarify": 1, "refuse": 2, "no_authority": 3}


@dataclass(frozen=True)
class Usage:
    """Held plus spent in one window for one owner: payment count and amounts per currency."""

    count: int
    amounts: dict  # {currency: amount_minor}


@dataclass(frozen=True)
class PriorIntent:
    proposal_hash: str
    determination_id: str


@dataclass(frozen=True)
class RecentProposal:
    proposal_id: str
    intent_id: str
    merchant_ref: str
    cart_hash: str
    at: datetime


@dataclass(frozen=True)
class EvaluationInput:
    determination_id: str
    proposal: Proposal
    caller: Caller | None
    wheel: Wheel
    spokes: dict  # {spoke_id: Spoke}
    tier0_rules: tuple[Rule, ...]
    jurisdiction_rules: tuple[Rule, ...]
    jurisdiction: str
    rails: dict  # {rail_id: RailProfile}
    params: Params
    facts: FactSet
    usage: dict  # {(owner_id, window_id): Usage}
    at: datetime
    versions: dict
    prior_intents: dict = field(default_factory=dict)  # {intent_id: PriorIntent}
    recent: tuple[RecentProposal, ...] = ()
    stage: str = "proposal"
    authorised_presented_hash: str | None = None  # stage 2, explicit_tap


@dataclass(frozen=True)
class FailingClause:
    rule_id: str
    dimension: str
    author: str
    overridable: bool

    def to_json(self) -> dict:
        return {"rule_id": self.rule_id, "dimension": self.dimension, "author": self.author, "overridable": self.overridable}


@dataclass(frozen=True)
class Repeat:
    """Same intent_id, same body: the original decision stands; nothing new is created (02 §3)."""

    original_determination_id: str


@dataclass(frozen=True)
class IntentConflict:
    """Same intent_id, different body: rejected; nothing is created (02 §1 step 0)."""

    intent_id: str
    original_proposal_hash: str
    new_proposal_hash: str


@dataclass(frozen=True)
class Determination:
    determination_id: str
    proposal_id: str
    intent_id: str
    proposal_hash: str
    spoke_id: str
    stage: str
    outcome: str
    approval_mode: str
    approval_mode_set_by: str
    failing_clauses: tuple[FailingClause, ...]
    clarify_fields: tuple[str, ...]
    facts_hash: str
    evaluated_at: datetime
    versions: dict
    rail_id: str | None = None
    rail_chosen_by: str | None = None
    amount_binding: str | None = None
    presented_hash: str | None = None
    fetched_hash: str | None = None
    duplicate_of: str | None = None
    duplicate_window_s: int = 0

    def to_record(self) -> dict:
        """The ledger record body (19 §5.2), before chain link and signature."""
        rec = {
            "record_type": "determination",
            "determination_id": self.determination_id, "proposal_id": self.proposal_id, "intent_id": self.intent_id,
            "proposal_hash": self.proposal_hash, "spoke_id": self.spoke_id, "stage": self.stage, "outcome": self.outcome,
            "approval_mode": self.approval_mode, "approval_mode_set_by": self.approval_mode_set_by,
            "failing_clauses": [c.to_json() for c in self.failing_clauses],
            "clarify_fields": list(self.clarify_fields), "facts_hash": self.facts_hash, "fact_refs": [],
            "duplicate_check": {"window": self.duplicate_window_s, **({"matched_proposal_id": self.duplicate_of} if self.duplicate_of else {})},
            "versions": dict(self.versions), "evaluated_at": format_ts(self.evaluated_at), "links": {},
        }
        for k in ("rail_id", "rail_chosen_by", "amount_binding", "presented_hash", "fetched_hash"):
            v = getattr(self, k)
            if v is not None:
                rec[k] = v
        return rec


def can_override(det: Determination, clause_ids: list[str] | set[str]) -> bool:
    """A one-payment override is possible only for a refusal whose every failing clause is payer-authored
    and named in the override (02 §2, invariant 12)."""
    if det.outcome != "refuse" or not det.failing_clauses:
        return False
    ids = set(clause_ids)
    return all(c.overridable for c in det.failing_clauses) and {c.rule_id for c in det.failing_clauses} <= ids


# --- the evaluation context ------------------------------------------------------

class _Ctx:
    def __init__(self, inp: EvaluationInput) -> None:
        self.inp = inp
        self.failing: dict[tuple, FailingClause] = {}
        self.clarify: set[str] = set()
        self.tap_reasons: list[str] = []

    def fail(self, rule_id: str, dimension: str, author: str) -> None:
        c = FailingClause(rule_id, dimension, author, author == "payer")
        self.failing[(c.author, c.dimension, c.rule_id)] = c

    def sys(self, rule_id: str) -> None:
        self.fail(rule_id, "system", "system")

    def rule_fail(self, rule: Rule) -> None:
        self.fail(rule.rule_id, rule.dimension, rule.author)

    def tap(self, reason: str) -> None:
        self.tap_reasons.append(reason)

    def sorted_failing(self) -> tuple[FailingClause, ...]:
        return tuple(self.failing[k] for k in sorted(self.failing))


def evaluate(inp: EvaluationInput) -> Determination | Repeat | IntentConflict:
    p, body = inp.proposal, inp.proposal.body
    ph = proposal_hash(body)

    # Step 0, intake: one request, at most one order (invariant 11)
    if inp.stage == "proposal" and p.intent_id in inp.prior_intents:
        prior = inp.prior_intents[p.intent_id]
        if prior.proposal_hash == ph:
            return Repeat(prior.determination_id)
        return IntentConflict(p.intent_id, prior.proposal_hash, ph)

    ctx = _Ctx(inp)
    chain = _resolve(inp, ctx)
    if chain is None:  # no authority: short-circuit with every reason found
        return _finish(inp, ctx, ph, "no_authority", None, None, None, mode="explicit_tap", set_by="no_authority")

    spoke = chain[-1]
    rail, chosen_by = _choose_rail(inp, chain)

    # Step 2 + 3: authority and safety, every dimension, every layer
    for rule, owner in _rules_in_force(inp, chain):
        _check(rule, owner, inp, ctx, rail)
    if rail is None:
        ctx.sys("sys.no_rail")
    elif not _offered(inp, rail.rail_id):
        ctx.sys("sys.rail_not_offered")  # the merchant cannot take this rail: refuse before the payer approves
    elif rail.per_transaction_max is not None and not _le(body.total, rail.per_transaction_max):
        ctx.fail(f"rail:{rail.rail_id}:per_transaction", "payment_method", "jurisdiction")  # the rail's own limit (04 §1)
    if (rf := inp.facts.get("recovery.pending")) is not None and rf.value is True:
        ctx.sys("sys.recovery_pending")

    # Step 4: confidence
    if body.agent_confidence < inp.params.clarify_threshold:
        ctx.clarify.add("agent_confidence")

    # Step 5: content-based duplicate guard
    dup = _duplicate(inp)
    if dup is not None:
        ctx.tap("duplicate")

    # Step 6: approval mode, strictest wins
    mode, set_by = _approval_mode(inp, chain, rail, ctx)

    # Step 7: caps and budgets (worst case for silent repeats, 03 §3)
    _budget(inp, chain, mode, ctx)

    # Stage 2: what was authorised must still be what is there
    fetched = None
    if inp.stage == "authorisation":
        fetched = presented_hash(body, rail.rail_id) if rail else None
        _drift(inp, spoke, mode, rail, fetched, ctx)

    outcome = "refuse" if ctx.failing else "clarify" if ctx.clarify else "allow"
    return _finish(inp, ctx, ph, outcome, rail, chosen_by, fetched, mode=mode, set_by=set_by, duplicate_of=dup)


# --- step 0: resolve the cited spoke and its chain -----------------------------------

def _resolve(inp: EvaluationInput, ctx: _Ctx) -> list[Spoke] | None:
    p, at = inp.proposal, inp.at
    if inp.wheel.wheel_id != p.wheel_id:
        ctx.sys("sys.no_spoke")
        return None
    spoke = inp.spokes.get(p.spoke_id)
    if spoke is None or spoke.wheel_id != p.wheel_id:
        ctx.sys("sys.no_spoke")
        return None
    chain, seen, cur = [], set(), spoke
    while cur is not None:
        if cur.spoke_id in seen:
            ctx.sys("sys.no_spoke")
            return None
        seen.add(cur.spoke_id)
        chain.append(cur)
        if cur.parent_id is None:
            break
        cur = inp.spokes.get(cur.parent_id)
        if cur is None:
            ctx.sys("sys.no_spoke")
            return None
    chain.reverse()  # root first, cited spoke last
    if len(chain) > inp.params.max_depth or inp.wheel.status != "active":
        ctx.sys("sys.spoke_inactive")
    for s in chain:
        if s.status != "active":
            ctx.sys("sys.spoke_inactive")
        if not (s.valid_from <= at < s.valid_until):  # valid_until is exclusive
            ctx.sys("sys.spoke_expired")
    caller = inp.caller
    if caller is None or caller.caller_id != p.caller_id or caller.status != "active":
        ctx.sys("sys.caller_not_bound")
    else:
        for s in chain:
            if s.callers is not None and caller.caller_id not in s.callers:
                ctx.sys("sys.caller_not_bound")
        floor = inp.params.caller_min_assurance.get("explicit_tap")
        if floor and ASSURANCE_RANK[caller.assurance] < ASSURANCE_RANK[floor]:
            ctx.sys("sys.caller_assurance")
    return None if ctx.failing else chain


def _rules_in_force(inp: EvaluationInput, chain: list[Spoke]):
    """Jurisdiction ∩ operator Tier 0 ∩ hub ∩ spoke chain (invariant 4). Owner scopes aggregate rules."""
    w = inp.wheel.wheel_id
    for r in inp.jurisdiction_rules:
        yield r, w
    for r in inp.tier0_rules:
        yield r, w
    for r in inp.wheel.rules:
        yield r, w
    for s in chain:
        for r in s.rules:
            yield r, s.spoke_id


# --- rail choice (04 §2) ---------------------------------------------------------------

def _choose_rail(inp: EvaluationInput, chain: list[Spoke]) -> tuple[RailProfile | None, str | None]:
    spoke, body = chain[-1], inp.proposal.body
    if spoke.instruction is not None:
        rid, by = spoke.instruction.rail_id, "instruction"
    elif body.rail_id is not None:
        rid, by = body.rail_id, "agent"
    else:
        permitted = [
            r for r in inp.rails.values()
            if r.status == "live" and _offered(inp, r.rail_id) and all(_rail_ok(rule, r) for rule, _ in _rules_in_force(inp, chain) if rule.dimension == "payment_method")
        ]
        if not permitted:
            return None, None
        best = max(permitted, key=lambda r: (AUTH_CLASS_RANK[r.authentication], r.amount_binding == "bound_at_rail", _neg(r.rail_id)))
        return best, "default"
    rail = inp.rails.get(rid)
    if rail is None or rail.status != "live":
        return None, None
    return rail, by


def _offered(inp: EvaluationInput, rail_id: str) -> bool:
    """False only when a fact says the merchant does not offer the rail. Any fact may block; none is needed to pass,
    because execution checks the merchant's own list again before placing (04 §3)."""
    f = inp.facts.get("merchant.rails_offered")
    return f is None or not isinstance(f.value, (tuple, list, frozenset, set)) or rail_id in f.value


def _neg(s: str) -> tuple:
    return tuple(-ord(c) for c in s)  # so max() prefers the alphabetically first id on ties


def _rail_ok(rule: Rule, rail: RailProfile) -> bool:
    c = rule.constraint
    if "allow_rails" in c and rail.rail_id not in c["allow_rails"]:
        return False
    if "allow_authentication" in c and rail.authentication not in c["allow_authentication"]:
        return False
    if "require_amount_binding" in c and rail.amount_binding != "bound_at_rail":
        return False
    return True


# --- the 14 dimensions (01 §3, 19 §3.3) ----------------------------------------------------

def _check(rule: Rule, owner: str, inp: EvaluationInput, ctx: _Ctx, rail: RailProfile | None) -> None:
    c, body, facts = rule.constraint, inp.proposal.body, inp.facts
    d = rule.dimension

    if d == "caller":
        caller = inp.caller
        if "any_of_callers" in c and inp.proposal.caller_id not in c["any_of_callers"]:
            ctx.rule_fail(rule)
        if "min_assurance" in c and ASSURANCE_RANK[caller.assurance] < ASSURANCE_RANK[c["min_assurance"]]:
            ctx.rule_fail(rule)

    elif d == "merchant":
        if "allow" in c and body.merchant_ref not in c["allow"]:
            ctx.rule_fail(rule)
        if "block" in c and body.merchant_ref in c["block"]:
            ctx.rule_fail(rule)
        if "allow_categories" in c or "block_categories" in c:
            any_f, ver_f = facts.get("merchant.categories"), facts.get("merchant.categories", verified_only=True)
            if "block_categories" in c:
                if any_f is not None and any_f.value.terms & set(c["block_categories"]):
                    ctx.rule_fail(rule)  # any source may block
                elif ver_f is None:
                    ctx.rule_fail(rule)  # only a verified source may clear
            if "allow_categories" in c and (ver_f is None or not ver_f.value.terms & set(c["allow_categories"])):
                ctx.rule_fail(rule)

    elif d == "items":
        if "max_lines" in c and len(body.lines) > c["max_lines"]:
            ctx.rule_fail(rule)
        for line in body.lines:
            if "allow_items" in c and line.item_id not in c["allow_items"]:
                ctx.rule_fail(rule)
            if "block_items" in c and line.item_id in c["block_items"]:
                ctx.rule_fail(rule)
            if "allow_categories" in c and line.category not in c["allow_categories"]:
                ctx.rule_fail(rule)
            if "block_categories" in c and (line.category is None or line.category in c["block_categories"]):
                ctx.rule_fail(rule)
            if "max_quantity_per_line" in c and line.quantity > c["max_quantity_per_line"]:
                ctx.rule_fail(rule)

    elif d == "substitution":
        if c["mode"] == "none" and body.substitution_allowed is not False:
            ctx.rule_fail(rule)

    elif d == "item_attributes":
        floor = ATTESTATION_RANK.get(c.get("min_attestation", "injected"), 0)
        unknown_blocks = c.get("unknown", "block") == "block"
        for line in body.lines:
            name = f"merchant.item_attributes.{line.item_id}"
            any_f = facts.get(name)
            ver_f = facts.get(name, verified_only=True)
            if ver_f is not None and ATTESTATION_RANK[ver_f.attestation] < floor:
                ver_f = None
            if "block_terms" in c:
                if any_f is not None and any_f.value.terms & set(c["block_terms"]):
                    ctx.rule_fail(rule)
                elif ver_f is None and unknown_blocks:
                    ctx.rule_fail(rule)
            if "require_terms" in c:
                if ver_f is None:
                    if unknown_blocks:
                        ctx.rule_fail(rule)
                elif not set(c["require_terms"]) <= ver_f.value.terms:
                    ctx.rule_fail(rule)

    elif d == "amount":
        if "per_transaction_max" in c and not _le(body.total, c["per_transaction_max"]):
            ctx.rule_fail(rule)
        if "aggregate" in c:
            agg = c["aggregate"]
            u = inp.usage.get((owner, agg["window"].window_id(inp.at)))
            if u is None:
                ctx.fail("sys.usage_unknown", d, "system")
            elif agg["limit"].currency != body.total.currency or u.amounts.get(body.total.currency, 0) + body.total.amount_minor > agg["limit"].amount_minor:
                ctx.rule_fail(rule)

    elif d == "velocity":
        u = inp.usage.get((owner, c["window"].window_id(inp.at)))
        if u is None:
            ctx.fail("sys.usage_unknown", d, "system")
        elif u.count + 1 > c["max_count"]:
            ctx.rule_fail(rule)

    elif d == "payment_method":
        if rail is None or not _rail_ok(rule, rail):
            ctx.rule_fail(rule)

    elif d == "time":
        if "allow" in c and not any(_in_span(inp.at, s) for s in c["allow"]):
            ctx.rule_fail(rule)
        if "exclude" in c and any(_in_span(inp.at, s) for s in c["exclude"]):
            ctx.rule_fail(rule)

    elif d == "approval_mode":
        if c["at_most"] == "explicit_tap":
            ctx.tap(f"rule:{rule.rule_id}")

    elif d == "destination":
        if "allow" in c and body.destination_ref not in c["allow"]:
            ctx.rule_fail(rule)
        if "quarantine_new" in c:
            f = facts.get("destination.first_used_at", verified_only=True)
            if f is None or not isinstance(f.value, datetime) or inp.at - f.value < timedelta(seconds=c["quarantine_new"]):
                ctx.rule_fail(rule)

    elif d == "conditions":
        # Effects only add caution. The condition fires if it holds with all facts OR with verified facts
        # only, so an unverified fact can never switch it off, even through `not` (found by property test).
        if evaluate_predicate(c["when"], facts, verified_only=False) or evaluate_predicate(c["when"], facts, verified_only=True):
            if c["effect"] == "block":
                ctx.rule_fail(rule)
            elif c["effect"] == "force_explicit_tap":
                ctx.tap(f"rule:{rule.rule_id}")
            else:
                ctx.clarify.add(rule.rule_id)

    elif d == "extras":
        allowed = {e["kind"]: e for e in c["allow"]}
        for extra in body.extras:
            spec = allowed.get(extra.kind)
            if spec is None or ("max" in spec and (extra.amount is None or not _le(extra.amount, spec["max"]))):
                ctx.rule_fail(rule)

    elif d == "currency_geography":
        if "allow_currencies" in c and body.total.currency not in c["allow_currencies"]:
            ctx.rule_fail(rule)
        if "allow_jurisdictions" in c and inp.jurisdiction not in c["allow_jurisdictions"]:
            ctx.rule_fail(rule)


def _le(a: Money, b: Money) -> bool:
    """a <= b; a different currency is never within a limit."""
    return a.currency == b.currency and a.amount_minor <= b.amount_minor


def _in_span(at: datetime, span: dict) -> bool:
    local = at.astimezone(ZoneInfo(span["tz"]))
    if DAYS[local.weekday()] not in span["days"]:
        return False
    hm = f"{local.hour:02d}:{local.minute:02d}"
    start, end = span["from"], span["to"]
    return start <= hm < end if start < end else (hm >= start or hm < end)


# --- steps 5 to 7 and stage 2 ------------------------------------------------------------

def _duplicate(inp: EvaluationInput) -> str | None:
    p, ch = inp.proposal, cart_hash(inp.proposal.body)
    window = timedelta(seconds=inp.params.duplicate_window_s)
    matches = [
        r for r in inp.recent
        if r.intent_id != p.intent_id and r.merchant_ref == p.body.merchant_ref and r.cart_hash == ch and timedelta(0) <= inp.at - r.at <= window
    ]
    return min(matches, key=lambda r: (r.at, r.proposal_id)).proposal_id if matches else None


def _approval_mode(inp: EvaluationInput, chain: list[Spoke], rail: RailProfile | None, ctx: _Ctx) -> tuple[str, str]:
    spoke = chain[-1]
    for s in chain:
        if s.approval_mode == "explicit_tap":
            return "explicit_tap", f"spoke:{s.spoke_id}"
    reasons = list(ctx.tap_reasons)
    if rail is None or "silent_with_notice" not in rail.approval_modes:
        reasons.append(f"rail:{rail.rail_id}" if rail else "rail:none")
    worst = _worst_case(spoke, inp.proposal.body.total)
    cap = inp.params.silent_max_amount
    if cap is None or not _le(worst, cap):
        reasons.append("param:approval.silent.max_amount")
    for pre in inp.params.silent_preconditions:  # permissive, so verified facts only
        if not evaluate_predicate(pre, inp.facts, verified_only=True):
            reasons.append("param:approval.silent.preconditions")
            break
    floor = inp.params.caller_min_assurance.get("silent_with_notice")
    if floor and ASSURANCE_RANK[inp.caller.assurance] < ASSURANCE_RANK[floor]:
        reasons.append("caller:assurance")
    f = inp.facts.get("findings.open_on_spoke")
    if f is not None and isinstance(f.value, int) and f.value > 0:
        reasons.append("finding:open")
    if reasons:
        return "explicit_tap", reasons[0]
    return "silent_with_notice", f"spoke:{spoke.spoke_id}"


def _worst_case(spoke: Spoke, total: Money) -> Money:
    if spoke.instruction is None:
        return total
    bp = spoke.instruction.price_tolerance_bp
    return Money(total.amount_minor + (total.amount_minor * bp + 9999) // 10000, total.currency)


def _budget(inp: EvaluationInput, chain: list[Spoke], mode: str, ctx: _Ctx) -> None:
    total = inp.proposal.body.total
    amount = total if mode == "explicit_tap" else _worst_case(chain[-1], total)
    for s in chain:
        for cap in s.caps:
            if cap.limit.currency != amount.currency:
                continue
            u = inp.usage.get((s.spoke_id, cap.window.window_id(inp.at)))
            if u is None:
                ctx.sys("sys.usage_unknown")
            elif u.amounts.get(amount.currency, 0) + amount.amount_minor > cap.limit.amount_minor:
                ctx.sys("sys.cap_exceeded")
    budget = next((b for b in inp.wheel.budgets if b.limit.currency == amount.currency), None)
    if budget is None:
        ctx.sys("sys.budget_exceeded")  # no budget in this currency means no room
        return
    u = inp.usage.get((inp.wheel.wheel_id, budget.window.window_id(inp.at)))
    if u is None:
        ctx.sys("sys.usage_unknown")
    elif u.amounts.get(amount.currency, 0) + amount.amount_minor > budget.limit.amount_minor:
        ctx.sys("sys.budget_exceeded")


def _drift(inp: EvaluationInput, spoke: Spoke, mode: str, rail: RailProfile | None, fetched: str | None, ctx: _Ctx) -> None:
    body = inp.proposal.body
    if mode == "explicit_tap":
        if inp.authorised_presented_hash is None or fetched != inp.authorised_presented_hash:
            ctx.sys("sys.drift")
        return
    ins = spoke.instruction
    want = sorted((l.item_id, l.variant_id or "", l.quantity) for l in ins.lines)
    got = sorted((l.item_id, l.variant_id or "", l.quantity) for l in body.lines)
    limit = _worst_case(spoke, ins.reference_total)
    if (
        want != got or body.merchant_ref != ins.merchant_ref or body.destination_ref != ins.destination_ref
        or rail is None or rail.rail_id != ins.rail_id or not _le(body.total, limit)
    ):
        ctx.sys("sys.drift")


def _finish(inp, ctx, ph, outcome, rail, chosen_by, fetched, *, mode, set_by, duplicate_of=None) -> Determination:
    p = inp.proposal
    presented = None
    if rail is not None:
        presented = inp.authorised_presented_hash if inp.stage == "authorisation" else presented_hash(p.body, rail.rail_id)
    return Determination(
        determination_id=inp.determination_id, proposal_id=p.proposal_id, intent_id=p.intent_id, proposal_hash=ph,
        spoke_id=p.spoke_id, stage=inp.stage, outcome=outcome, approval_mode=mode, approval_mode_set_by=set_by,
        failing_clauses=ctx.sorted_failing(), clarify_fields=tuple(sorted(ctx.clarify)), facts_hash=inp.facts.hash(),
        evaluated_at=inp.at, versions=dict(inp.versions), rail_id=rail.rail_id if rail else None, rail_chosen_by=chosen_by,
        amount_binding=rail.amount_binding if rail else None, presented_hash=presented, fetched_hash=fetched,
        duplicate_of=duplicate_of, duplicate_window_s=inp.params.duplicate_window_s,
    )
