"""Property tests: the guarantees of 01 §9 and 02 §6 hold for any combination of rules and facts."""

from dataclasses import replace
from datetime import timedelta

from hypothesis import HealthCheck, given, settings, strategies as st

from gap_core.authority import Caller
from gap_core.canonical import canonical
from gap_core.evaluate import OUTCOME_RANK, Usage, evaluate
from gap_core.facts import Fact, TermSet
from gap_core.testing import AT, MONTH, WHEEL, repeat_spoke, rule, scenario, xts

RULE_POOL = [
    rule("caller_any", "caller", any_of_callers=["clr_agent_a", "clr_scheduler"]),
    rule("caller_key", "caller", min_assurance="key_bound"),
    rule("m_allow", "merchant", allow=["mrc_shop"]),
    rule("m_block", "merchant", block=["mrc_shop"], author="operator"),
    rule("m_cat", "merchant", block_categories=["std:mcc:7995"]),
    rule("i_lines", "items", max_lines=1),
    rule("i_qty", "items", max_quantity_per_line=5),
    rule("i_block", "items", block_items=["item_bread"]),
    rule("subst", "substitution", mode="none"),
    rule("attr", "item_attributes", block_terms=["std:allergen:peanut"]),
    rule("attr_req", "item_attributes", require_terms=["std:diet:vegan"], unknown="allow"),
    rule("amt", "amount", per_transaction_max=xts(20000), author="jurisdiction"),
    rule("amt_low", "amount", per_transaction_max=xts(100)),
    rule("agg", "amount", aggregate={"limit": xts(50000), "window": MONTH}),
    rule("vel", "velocity", max_count=3, window=MONTH),
    rule("rail", "payment_method", allow_rails=["xx:pin"]),
    rule("auth", "payment_method", allow_authentication=["human_per_payment"]),
    rule("night", "time", exclude=[{"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "from": "23:00", "to": "07:00", "tz": "Etc/UTC"}]),
    rule("weekday", "time", allow=[{"days": ["sat"], "from": "00:00", "to": "23:59", "tz": "Etc/UTC"}]),
    rule("tap", "approval_mode", at_most="explicit_tap"),
    rule("dst", "destination", allow=["dst_home"]),
    rule("cond_block", "conditions", when={"fact": "account.open_disputes", "op": "gt", "value": 0}, effect="block"),
    rule("cond_tap", "conditions", when={"fact": "account.age_days", "op": "lt", "value": 30}, effect="force_explicit_tap"),
    rule("cond_clarify", "conditions", when={"not": {"fact": "notice.delivered", "op": "exists"}}, effect="clarify"),
    rule("extras", "extras", allow=[{"kind": "tip", "max": xts(1000)}]),
    rule("cur", "currency_geography", allow_currencies=["XTS"]),
    rule("geo", "currency_geography", allow_jurisdictions=["yy"], author="operator"),
]

FACT_POOL = [
    Fact("account.open_disputes", 1, "anyone", False, "capturer_signed"),
    Fact("account.age_days", 3, "anyone", False, "capturer_signed"),
    Fact("notice.delivered", True, "anyone", False, "capturer_signed"),
    Fact("findings.open_on_spoke", 2, "anyone", False, "capturer_signed"),
    Fact("merchant.categories", TermSet(frozenset({"std:mcc:7995"}), "v", "m"), "anyone", False, "capturer_signed"),
    Fact("merchant.item_attributes.item_milk", TermSet(frozenset({"std:allergen:peanut"}), "v", "m"), "anyone", False, "capturer_signed"),
    Fact("recovery.pending", True, "anyone", False, "injected"),
]


def _tap_base():
    inp = scenario()
    return replace(inp, usage=dict(inp.usage) | {(WHEEL, "2026-10"): Usage(1, {"XTS": 10000})})


def _silent_base():
    inp = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler",
                   caller=Caller("clr_scheduler", "key_bound"))
    return replace(inp, usage=dict(inp.usage) | {(WHEEL, "2026-10"): Usage(1, {"XTS": 10000})})


BASES = [_tap_base, _silent_base]
layers = st.sampled_from(["wheel", "spoke"])


def _with_rules(inp, rules, layer):
    if layer == "wheel":
        return replace(inp, wheel=replace(inp.wheel, rules=tuple(rules)))
    sid = inp.proposal.spoke_id
    return replace(inp, spokes={**inp.spokes, sid: replace(inp.spokes[sid], rules=tuple(rules))})


def _no_looser(before, after):
    assert OUTCOME_RANK[after.outcome] >= OUTCOME_RANK[before.outcome]
    assert {c.rule_id for c in before.failing_clauses} <= {c.rule_id for c in after.failing_clauses}
    if before.approval_mode == "explicit_tap":
        assert after.approval_mode == "explicit_tap"


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(st.sampled_from(BASES), st.lists(st.sampled_from(RULE_POOL), unique_by=lambda r: r.rule_id, max_size=6), st.sampled_from(RULE_POOL), layers)
def test_adding_a_rule_never_loosens_anything(base, rules, extra, layer):
    """01 §9: no event except a payer widen enlarges a permitted set; adding a rule never loosens approval."""
    inp = base()
    before = evaluate(_with_rules(inp, rules, layer))
    after = evaluate(_with_rules(inp, [*rules, extra] if extra not in rules else rules, layer))
    _no_looser(before, after)


@settings(max_examples=300)
@given(st.sampled_from(BASES), st.lists(st.sampled_from(RULE_POOL), unique_by=lambda r: r.rule_id, max_size=6), st.lists(st.sampled_from(FACT_POOL), unique_by=lambda f: f.name, min_size=1, max_size=4))
def test_unverified_facts_never_make_an_outcome_more_permissive(base, rules, extra_facts):
    """02 §6: monotonic caution. Unverified facts can only block, force a tap or force clarify."""
    inp = _with_rules(base(), rules, "wheel")
    before = evaluate(inp)
    facts = inp.facts
    for f in extra_facts:
        facts = facts.with_fact(f)
    _no_looser(before, evaluate(replace(inp, facts=facts)))


@settings(max_examples=200)
@given(st.integers(0, 1_600_000), st.integers(1, 400_000))
def test_an_allowed_payment_always_fits_the_hub_budget(used, price):
    """01 §9: total spend across spokes never exceeds the hub budget."""
    from gap_core.proposal import CartLine, Fee
    from gap_core.testing import body

    b = body(lines=(CartLine("item_milk", 1, xts(price)),), fees=(Fee("delivery", xts(0)),))
    inp = scenario(body=b)
    inp = replace(inp, usage=dict(inp.usage) | {(WHEEL, "2026-10"): Usage(0, {"XTS": used})})
    det = evaluate(inp)
    if det.outcome == "allow":
        assert used + price <= 1_500_000
    if used + price > 1_500_000:
        assert det.outcome != "allow"


@settings(max_examples=100)
@given(st.sampled_from(BASES), st.lists(st.sampled_from(RULE_POOL), unique_by=lambda r: r.rule_id, max_size=6), st.integers(0, 3600))
def test_replay_is_deterministic(base, rules, seconds):
    """02 §6: the same proposal, facts, versions and time always give the same bytes."""
    inp = replace(_with_rules(base(), rules, "wheel"), at=AT + timedelta(seconds=seconds))
    assert canonical(evaluate(inp).to_record()) == canonical(evaluate(inp).to_record())


@settings(max_examples=200)
@given(st.lists(st.sampled_from(RULE_POOL), unique_by=lambda r: r.rule_id, max_size=8))
def test_failing_clauses_name_rules_that_exist(rules):
    """Every reported clause is a rule in force or a system clause, and each appears once."""
    det = evaluate(_with_rules(_tap_base(), rules, "wheel"))
    known = {r.rule_id for r in rules}
    names = [c.rule_id for c in det.failing_clauses]
    assert len(names) == len(set(names))
    assert all(n in known or n.startswith("sys.") for n in names)
    assert (det.outcome in ("refuse", "no_authority")) == bool(names)
