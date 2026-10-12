"""A ready-made scenario for tests and demos: one payer, one hub budget, a grocery spoke, two rails.

Example values only (currency XTS, the ISO 4217 testing code). Override anything with keyword
arguments or dataclasses.replace.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from .authority import CartLineTemplate, Caller, Limit, Params, RailProfile, Rule, Spoke, StandingInstruction, Wheel, Window
from .evaluate import EvaluationInput, Usage
from .facts import Fact, FactSet, TermSet
from .money import Money
from .proposal import CartLine, Fee, Proposal, ProposalBody

AT = datetime(2026, 10, 12, 13, 30, tzinfo=timezone.utc)  # a Monday
MONTH = Window("calendar_month", "Etc/UTC")
WHEEL, SPOKE, CALLER = "whl_test", "spk_grocery", "clr_agent_a"


def xts(minor: int) -> Money:
    return Money(minor, "XTS")


RAILS = {
    "xx:pin": RailProfile("xx:pin", "human_per_payment", "bound_at_rail", ("explicit_tap",)),
    "xx:wallet": RailProfile("xx:wallet", "none", "not_bound", ("explicit_tap", "silent_with_notice")),
}

PARAMS = Params(
    clarify_threshold=900,
    duplicate_window_s=600,
    verified_sources=frozenset({"merchant"}),
    max_depth=2,
    silent_max_amount=xts(200000),
    caller_min_assurance={"explicit_tap": "session_bound", "silent_with_notice": "key_bound"},
)


def body(**kw) -> ProposalBody:
    lines = kw.pop("lines", (
        CartLine("item_milk", 2, xts(6000), category="std:gpc:milk"),
        CartLine("item_bread", 1, xts(4500), category="std:gpc:bread"),
    ))
    fees = kw.pop("fees", (Fee("delivery", xts(3000)),))
    extras = kw.pop("extras", ())
    total = kw.pop("total", None)
    if total is None:
        t = sum(l.unit_price.amount_minor * l.quantity for l in lines) + sum(f.amount.amount_minor for f in fees)
        t += sum(e.amount.amount_minor for e in extras if e.amount is not None)
        total = xts(t)
    return ProposalBody(
        merchant_ref=kw.pop("merchant_ref", "mrc_shop"), adapter_id=kw.pop("adapter_id", "fake-a"),
        merchant_cart_ref=kw.pop("merchant_cart_ref", "cart-1"), lines=lines, fees=fees, extras=extras, total=total,
        destination_ref=kw.pop("destination_ref", "dst_home"), agent_confidence=kw.pop("agent_confidence", 950), **kw,
    )


def item_facts(b: ProposalBody, terms=frozenset(), verified=True, attestation="capturer_signed") -> list[Fact]:
    return [
        Fact(f"merchant.item_attributes.{l.item_id}", TermSet(frozenset(terms), "std:allergen@1", "fake-a@1"), "merchant", verified, attestation)
        for l in b.lines
    ]


def scenario(**kw) -> EvaluationInput:
    b = kw.pop("body", None) or body()
    spoke = kw.pop("spoke", None) or Spoke(
        spoke_id=SPOKE, wheel_id=WHEEL, approval_mode="explicit_tap",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
        callers=(CALLER,), caps=(Limit(xts(400000), MONTH),),
    )
    spokes = kw.pop("spokes", None) or {spoke.spoke_id: spoke}
    wheel = kw.pop("wheel", None) or Wheel(WHEEL, "pyr_test", "xx", budgets=(Limit(xts(1500000), MONTH),))
    facts = kw.pop("facts", None)
    if facts is None:
        facts = FactSet([Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "std:mcc@1", "fake-a@1"), "merchant", True, "capturer_signed")] + item_facts(b))
    usage = kw.pop("usage", None)
    if usage is None:
        usage = {(sid, "2026-10"): Usage(0, {}) for sid in spokes} | {(wheel.wheel_id, "2026-10"): Usage(0, {})}
    proposal = Proposal("prp_1", wheel.wheel_id, kw.pop("spoke_id", spoke.spoke_id), kw.pop("caller_id", CALLER), kw.pop("intent_id", "intent-1"), b)
    inp = EvaluationInput(
        determination_id="det_1", proposal=proposal, caller=kw.pop("caller", Caller(CALLER, "session_bound")),
        wheel=wheel, spokes=spokes, tier0_rules=kw.pop("tier0_rules", ()), jurisdiction_rules=kw.pop("jurisdiction_rules", ()),
        jurisdiction="xx", rails=kw.pop("rails", RAILS), params=kw.pop("params", PARAMS), facts=facts, usage=usage, at=kw.pop("at", AT),
        versions={"wheel": 1, "spoke": 1, "tier0": 1, "mapping": "xx@0.1.0", "evaluator": "sha256:" + "0" * 64},
    )
    return replace(inp, **kw) if kw else inp


def repeat_spoke(**kw) -> Spoke:
    """A standing-instruction spoke that may run silently, matching body()."""
    b = body()
    ins = StandingInstruction(
        merchant_ref=b.merchant_ref, adapter_id=b.adapter_id,
        lines=tuple(CartLineTemplate(l.item_id, l.quantity) for l in b.lines),
        destination_ref=b.destination_ref, rail_id="xx:wallet", price_tolerance_bp=1000, reference_total=b.total,
    )
    base = dict(
        spoke_id="spk_repeat", wheel_id=WHEEL, approval_mode="silent_with_notice",
        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc), valid_until=datetime(2027, 1, 1, tzinfo=timezone.utc),
        callers=("clr_scheduler",), instruction=ins, caps=(Limit(xts(400000), MONTH),),
    )
    base.update(kw)
    return Spoke(**base)


def rule(rule_id: str, dimension: str, author: str = "payer", **constraint) -> Rule:
    return Rule(rule_id, dimension, constraint, author)
