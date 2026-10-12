"""Phase 2 acceptance tests: 01 §9 and 02 §6, plus each of the 14 dimensions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gap_core.authority import Caller, Limit, Params, Rule, Window
from gap_core.canonical import canonical
from gap_core.chain import append, verify_chain
from gap_core.evaluate import Determination, IntentConflict, PriorIntent, RecentProposal, Repeat, Usage, can_override, evaluate
from gap_core.facts import Fact, FactSet, TermSet
from gap_core.proposal import CartLine, Extra, Fee, cart_hash, presented_hash, proposal_hash
from gap_core.testing import AT, CALLER, MONTH, PARAMS, SPOKE, WHEEL, body, item_facts, repeat_spoke, rule, scenario, xts
from gap_keys.local import LocalSigner


def ids(det):
    return {c.rule_id for c in det.failing_clauses}


# --- the clean path ------------------------------------------------------------------

def test_clean_proposal_is_allowed_with_a_tap():
    det = evaluate(scenario())
    assert isinstance(det, Determination)
    assert det.outcome == "allow"
    assert det.approval_mode == "explicit_tap" and det.approval_mode_set_by == f"spoke:{SPOKE}"
    assert det.rail_id == "xx:pin" and det.rail_chosen_by == "default"  # safest permitted rail (04 §2)
    assert det.amount_binding == "bound_at_rail"
    assert det.presented_hash == presented_hash(body(), "xx:pin")
    assert det.failing_clauses == () and det.clarify_fields == ()


def test_a_rail_limit_refuses_as_a_jurisdiction_clause():
    inp = scenario()
    capped = replace(inp.rails["xx:pin"], per_transaction_max=xts(10000))
    det = evaluate(replace(inp, rails={**inp.rails, "xx:pin": capped}))
    clause = next(c for c in det.failing_clauses if c.rule_id == "rail:xx:pin:per_transaction")
    assert det.outcome == "refuse" and clause.author == "jurisdiction" and not clause.overridable


def test_agent_may_propose_a_permitted_rail():
    det = evaluate(scenario(body=body(rail_id="xx:wallet")))
    assert det.rail_id == "xx:wallet" and det.rail_chosen_by == "agent"


def test_same_inputs_give_identical_bytes():
    a, b = evaluate(scenario()), evaluate(scenario())
    assert canonical(a.to_record()) == canonical(b.to_record())


def test_determination_record_signs_and_chains():
    signer = LocalSigner.generate("key_test")
    rec = append(evaluate(scenario()).to_record(), chain_id=WHEEL, previous=None, signer=signer)
    verify_chain([rec], {signer.key_id: signer.public_jwk})


# --- one request, at most one order (invariant 11) ---------------------------------------

def test_repeated_intent_with_same_body_returns_the_original():
    ph = proposal_hash(body())
    res = evaluate(scenario(prior_intents={"intent-1": PriorIntent(ph, "det_original")}))
    assert res == Repeat("det_original")


def test_repeated_intent_with_different_body_is_a_conflict():
    res = evaluate(scenario(prior_intents={"intent-1": PriorIntent("sha256:" + "1" * 64, "det_original")}))
    assert isinstance(res, IntentConflict)
    assert res.new_proposal_hash == proposal_hash(body())


def test_proposal_hash_ignores_line_order():
    a = body()
    b = body(lines=tuple(reversed(a.lines)))
    assert proposal_hash(a) == proposal_hash(b)


# --- no authority (01 §9: uncited, foreign or inactive spoke is always refused) --------------

@pytest.mark.parametrize(
    "kw, clause",
    [
        (dict(spoke_id="spk_missing"), "sys.no_spoke"),
        (dict(caller_id="clr_other", caller=Caller("clr_other", "session_bound")), "sys.caller_not_bound"),
        (dict(caller=None), "sys.caller_not_bound"),
        (dict(caller=Caller(CALLER, "session_bound", status="suspended")), "sys.caller_not_bound"),
        (dict(at=datetime(2027, 1, 1, tzinfo=timezone.utc)), "sys.spoke_expired"),  # valid_until is exclusive
        (dict(at=datetime(2025, 12, 31, tzinfo=timezone.utc)), "sys.spoke_expired"),
    ],
)
def test_no_authority(kw, clause):
    det = evaluate(scenario(**kw))
    assert det.outcome == "no_authority"
    assert clause in ids(det)


def test_foreign_spoke_is_no_authority():
    inp = scenario()
    foreign = replace(inp.spokes[SPOKE], wheel_id="whl_other")
    det = evaluate(replace(inp, spokes={SPOKE: foreign}))
    assert det.outcome == "no_authority" and "sys.no_spoke" in ids(det)


@pytest.mark.parametrize("status", ["suspended", "revoked", "expired"])
def test_inactive_spoke_is_no_authority(status):
    inp = scenario()
    det = evaluate(replace(inp, spokes={SPOKE: replace(inp.spokes[SPOKE], status=status)}))
    assert det.outcome == "no_authority" and "sys.spoke_inactive" in ids(det)


def test_suspended_wheel_is_no_authority():
    inp = scenario()
    det = evaluate(replace(inp, wheel=replace(inp.wheel, status="suspended")))
    assert det.outcome == "no_authority"


def test_caller_below_the_tap_floor_is_no_authority():
    params = replace(PARAMS, caller_min_assurance={"explicit_tap": "key_bound"})
    det = evaluate(scenario(params=params))
    assert det.outcome == "no_authority" and "sys.caller_assurance" in ids(det)


def test_parent_binding_also_binds():
    inp = scenario()
    parent = replace(inp.spokes[SPOKE], spoke_id="spk_parent", callers=("clr_someone_else",))
    child = replace(inp.spokes[SPOKE], parent_id="spk_parent")
    usage = dict(inp.usage) | {("spk_parent", "2026-10"): Usage(0, {})}
    det = evaluate(replace(inp, spokes={SPOKE: child, "spk_parent": parent}, usage=usage))
    assert det.outcome == "no_authority" and "sys.caller_not_bound" in ids(det)


def test_depth_beyond_max_is_no_authority():
    inp = scenario(params=replace(PARAMS, max_depth=1))
    parent = replace(inp.spokes[SPOKE], spoke_id="spk_parent")
    child = replace(inp.spokes[SPOKE], parent_id="spk_parent")
    det = evaluate(replace(inp, spokes={SPOKE: child, "spk_parent": parent}))
    assert det.outcome == "no_authority" and "sys.spoke_inactive" in ids(det)


# --- the 14 dimensions ------------------------------------------------------------------

DIMENSION_CASES = [
    ("caller", rule("r1", "caller", any_of_callers=["clr_someone"]), {}),
    ("caller", rule("r1", "caller", min_assurance="key_bound"), {}),
    ("merchant", rule("r1", "merchant", allow=["mrc_other"]), {}),
    ("merchant", rule("r1", "merchant", block=["mrc_shop"]), {}),
    ("merchant", rule("r1", "merchant", block_categories=["std:mcc:5411"]), {}),
    ("merchant", rule("r1", "merchant", allow_categories=["std:mcc:5812"]), {}),
    ("items", rule("r1", "items", block_items=["item_bread"]), {}),
    ("items", rule("r1", "items", allow_items=["item_milk"]), {}),
    ("items", rule("r1", "items", max_quantity_per_line=1), {}),
    ("items", rule("r1", "items", max_lines=1), {}),
    ("items", rule("r1", "items", block_categories=["std:gpc:bread"]), {}),
    ("substitution", rule("r1", "substitution", mode="none"), {}),
    ("item_attributes", rule("r1", "item_attributes", block_terms=["std:allergen:peanut"]), {"attr_terms": {"std:allergen:peanut"}}),
    ("item_attributes", rule("r1", "item_attributes", require_terms=["std:diet:vegan"]), {}),
    ("amount", rule("r1", "amount", per_transaction_max=xts(10000)), {}),
    ("amount", rule("r1", "amount", aggregate={"limit": xts(19000), "window": MONTH}), {}),
    ("velocity", rule("r1", "velocity", max_count=1, window=MONTH), {"usage_count": 1}),
    ("payment_method", rule("r1", "payment_method", allow_rails=["xx:other"]), {}),
    ("time", rule("r1", "time", exclude=[{"days": ["mon"], "from": "13:00", "to": "14:00", "tz": "Etc/UTC"}]), {}),
    ("time", rule("r1", "time", allow=[{"days": ["sat", "sun"], "from": "00:00", "to": "23:59", "tz": "Etc/UTC"}]), {}),
    ("destination", rule("r1", "destination", allow=["dst_office"]), {}),
    ("destination", rule("r1", "destination", quarantine_new=86400), {}),
    ("conditions", rule("r1", "conditions", when={"fact": "account.open_disputes", "op": "gt", "value": 0}, effect="block"), {"disputes": 2}),
    ("extras", rule("r1", "extras", allow=[{"kind": "tip", "max": xts(1000)}]), {"extras": (Extra("tip", xts(5000)),)}),
    ("extras", rule("r1", "extras", allow=[{"kind": "tip"}]), {"extras": (Extra("merchant_note"),)}),
    ("currency_geography", rule("r1", "currency_geography", allow_currencies=["XXX"]), {}),
    ("currency_geography", rule("r1", "currency_geography", allow_jurisdictions=["yy"]), {}),
]


def _case_input(r, extra):
    b = body(extras=extra.get("extras", ()))
    facts = [Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "std:mcc@1", "fake-a@1"), "merchant", True, "capturer_signed")]
    facts += item_facts(b, terms=extra.get("attr_terms", frozenset()))
    if "disputes" in extra:
        facts.append(Fact("account.open_disputes", extra["disputes"], "host", True, "injected"))
    inp = scenario(body=b, facts=FactSet(facts))
    if "usage_count" in extra:
        inp = replace(inp, usage=dict(inp.usage) | {(WHEEL, "2026-10"): Usage(extra["usage_count"], {})})
    return replace(inp, wheel=replace(inp.wheel, rules=(r,)))


@pytest.mark.parametrize("dimension, r, extra", DIMENSION_CASES, ids=[f"{d}-{i}" for i, (d, _, _) in enumerate(DIMENSION_CASES)])
def test_each_dimension_can_refuse(dimension, r, extra):
    det = evaluate(_case_input(r, extra))
    assert det.outcome == "refuse", det.failing_clauses
    clause = next(c for c in det.failing_clauses if c.rule_id == "r1")
    assert clause.dimension == dimension
    assert clause.author == "payer" and clause.overridable


def test_k_violated_clauses_report_all_k():
    rules = (
        rule("a", "merchant", block=["mrc_shop"]),
        rule("b", "items", max_lines=1, author="operator"),
        rule("c", "amount", per_transaction_max=xts(100), author="jurisdiction"),
        rule("d", "destination", allow=["dst_x"]),
    )
    inp = scenario()
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=rules)))
    assert ids(det) == {"a", "b", "c", "d"}
    assert {c.rule_id: c.overridable for c in det.failing_clauses} == {"a": True, "b": False, "c": False, "d": True}


def test_layers_intersect():
    inp = scenario(
        jurisdiction_rules=(rule("j", "amount", per_transaction_max=xts(100000), author="jurisdiction"),),
        tier0_rules=(rule("t", "merchant", block=["mrc_bad"], author="operator"),),
    )
    spoke = replace(inp.spokes[SPOKE], rules=(rule("s", "items", block_items=["item_milk"]),))
    det = evaluate(replace(inp, spokes={SPOKE: spoke}))
    assert ids(det) == {"s"}


def test_approval_mode_rule_forces_a_tap_without_refusing():
    inp = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler",
                   caller=Caller("clr_scheduler", "key_bound"), body=body(rail_id=None))
    assert evaluate(inp).approval_mode == "silent_with_notice"
    inp2 = replace(inp, wheel=replace(inp.wheel, rules=(rule("r_tap", "approval_mode", at_most="explicit_tap"),)))
    det = evaluate(inp2)
    assert det.outcome == "allow" and det.approval_mode == "explicit_tap" and det.approval_mode_set_by == "rule:r_tap"


# --- facts: only verified sources clear (invariant 9) --------------------------------------

def test_unverified_category_fact_cannot_clear_a_block_rule():
    r = rule("r1", "merchant", block_categories=["std:mcc:7995"])
    facts = FactSet([Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "v", "m"), "ad_hoc", False, "capturer_signed")] + item_facts(body()))
    inp = scenario(facts=facts)
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=(r,))))
    assert "r1" in ids(det)


def test_verified_clean_category_clears():
    inp = scenario()
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=(rule("r1", "merchant", block_categories=["std:mcc:7995"]),))))
    assert det.outcome == "allow"


def test_unverified_blocked_term_still_blocks():
    facts = FactSet([Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "v", "m"), "merchant", True, "capturer_signed")]
                    + item_facts(body(), terms={"std:allergen:peanut"}, verified=False))
    inp = scenario(facts=facts)
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=(rule("r1", "item_attributes", block_terms=["std:allergen:peanut"]),))))
    assert "r1" in ids(det)


@pytest.mark.parametrize("unknown, refused", [("block", True), ("allow", False)])
def test_missing_item_attributes_follow_the_unknown_policy(unknown, refused):
    facts = FactSet([Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "v", "m"), "merchant", True, "capturer_signed")])
    inp = scenario(facts=facts)
    r = rule("r1", "item_attributes", block_terms=["std:allergen:peanut"], unknown=unknown)
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=(r,))))
    assert ("r1" in ids(det)) is refused


def test_attestation_floor_treats_weaker_facts_as_unknown():
    inp = scenario()
    r = rule("r1", "item_attributes", block_terms=["std:allergen:peanut"], min_attestation="source_signed")
    det = evaluate(replace(inp, wheel=replace(inp.wheel, rules=(r,))))
    assert "r1" in ids(det)  # capturer_signed is below source_signed, so it cannot clear


def test_destination_quarantine_clears_with_an_old_verified_fact():
    old = Fact("destination.first_used_at", AT - timedelta(days=30), "host", True, "injected")
    inp = scenario()
    inp = replace(inp, facts=inp.facts.with_fact(old), wheel=replace(inp.wheel, rules=(rule("r1", "destination", quarantine_new=86400),)))
    assert evaluate(inp).outcome == "allow"


def test_condition_effects():
    disputes = Fact("account.open_disputes", 1, "host", True, "injected")
    for effect, outcome, mode in [("clarify", "clarify", "explicit_tap"), ("force_explicit_tap", "allow", "explicit_tap")]:
        inp = scenario()
        r = rule("c1", "conditions", when={"fact": "account.open_disputes", "op": "ge", "value": 1}, effect=effect)
        det = evaluate(replace(inp, facts=inp.facts.with_fact(disputes), wheel=replace(inp.wheel, rules=(r,))))
        assert det.outcome == outcome and det.approval_mode == mode
        if effect == "clarify":
            assert det.clarify_fields == ("c1",)


def test_unverified_fact_cannot_switch_off_a_negated_condition():
    """Regression (found by the property test in CI): `not exists` must not be defeated by an unverified fact."""
    inp = scenario()
    r = rule("c1", "conditions", when={"not": {"fact": "notice.delivered", "op": "exists"}}, effect="clarify")
    inp = replace(inp, wheel=replace(inp.wheel, rules=(r,)))
    assert evaluate(inp).outcome == "clarify"
    unverified = inp.facts.with_fact(Fact("notice.delivered", True, "anyone", False, "capturer_signed"))
    assert evaluate(replace(inp, facts=unverified)).outcome == "clarify"
    verified = inp.facts.with_fact(Fact("notice.delivered", True, "host", True, "injected"))
    assert evaluate(replace(inp, facts=verified)).outcome == "allow"


def test_condition_on_a_missing_fact_has_no_effect():
    inp = scenario()
    r = rule("c1", "conditions", when={"fact": "account.open_disputes", "op": "ge", "value": 1}, effect="block")
    assert evaluate(replace(inp, wheel=replace(inp.wheel, rules=(r,)))).outcome == "allow"


# --- confidence and duplicates ----------------------------------------------------------

def test_low_confidence_asks_to_clarify():
    det = evaluate(scenario(body=body(agent_confidence=899)))
    assert det.outcome == "clarify" and det.clarify_fields == ("agent_confidence",)


def test_same_cart_new_intent_forces_a_tap_and_is_recorded():
    inp = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler",
                   caller=Caller("clr_scheduler", "key_bound"))
    recent = (RecentProposal("prp_0", "intent-0", "mrc_shop", cart_hash(body()), AT - timedelta(minutes=5)),)
    det = evaluate(replace(inp, recent=recent))
    assert det.approval_mode == "explicit_tap" and det.approval_mode_set_by == "duplicate"
    assert det.duplicate_of == "prp_0"


@pytest.mark.parametrize("recent", [
    (RecentProposal("prp_0", "intent-0", "mrc_shop", "sha256:" + "2" * 64, AT - timedelta(minutes=5)),),  # different cart
    (RecentProposal("prp_0", "intent-0", "mrc_shop", "CART", AT - timedelta(minutes=11)),),  # outside the window
    (RecentProposal("prp_0", "intent-1", "mrc_shop", "CART", AT - timedelta(minutes=5)),),  # same intent: not a duplicate
])
def test_not_a_duplicate(recent):
    recent = tuple(replace(r, cart_hash=cart_hash(body())) if r.cart_hash == "CART" else r for r in recent)
    assert evaluate(scenario(recent=recent)).duplicate_of is None


# --- silent execution (approval mode, strictest wins) ----------------------------------------

def _silent(**kw):
    inp = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler",
                   caller=Caller("clr_scheduler", "key_bound"))
    return replace(inp, **kw) if kw else inp


def test_standing_instruction_runs_silently():
    det = evaluate(_silent())
    assert det.outcome == "allow" and det.approval_mode == "silent_with_notice"
    assert det.rail_id == "xx:wallet" and det.rail_chosen_by == "instruction"


@pytest.mark.parametrize(
    "change, reason",
    [
        (lambda i: replace(i, rails={**i.rails, "xx:wallet": replace(i.rails["xx:wallet"], approval_modes=("explicit_tap",))}), "rail:xx:wallet"),
        (lambda i: replace(i, params=replace(i.params, silent_max_amount=xts(100))), "param:approval.silent.max_amount"),
        (lambda i: replace(i, params=replace(i.params, silent_max_amount=None)), "param:approval.silent.max_amount"),
        (lambda i: replace(i, params=replace(i.params, silent_preconditions=({"fact": "notice.delivered", "op": "eq", "value": True},))), "param:approval.silent.preconditions"),
        (lambda i: replace(i, caller=Caller("clr_scheduler", "session_bound")), "caller:assurance"),
        (lambda i: replace(i, facts=i.facts.with_fact(Fact("findings.open_on_spoke", 1, "host", True, "injected"))), "finding:open"),
    ],
)
def test_every_layer_can_force_a_tap(change, reason):
    det = evaluate(change(_silent()))
    assert det.approval_mode == "explicit_tap" and det.approval_mode_set_by == reason


def test_unverified_fact_cannot_satisfy_a_silent_precondition():
    pre = ({"fact": "notice.delivered", "op": "eq", "value": True},)
    inp = _silent(params=replace(PARAMS, silent_preconditions=pre))
    unverified = inp.facts.with_fact(Fact("notice.delivered", True, "device", False, "capturer_signed"))
    verified = inp.facts.with_fact(Fact("notice.delivered", True, "host", True, "injected"))
    assert evaluate(replace(inp, facts=unverified)).approval_mode == "explicit_tap"
    assert evaluate(replace(inp, facts=verified)).approval_mode == "silent_with_notice"


def test_a_tap_parent_makes_the_child_tap():
    inp = _silent()
    parent = replace(repeat_spoke(), spoke_id="spk_parent", approval_mode="explicit_tap", instruction=None)
    child = replace(repeat_spoke(), parent_id="spk_parent")
    usage = dict(inp.usage) | {("spk_parent", "2026-10"): Usage(0, {})}
    det = evaluate(replace(inp, spokes={"spk_repeat": child, "spk_parent": parent}, usage=usage))
    assert det.approval_mode == "explicit_tap" and det.approval_mode_set_by == "spoke:spk_parent"


# --- caps and budgets ---------------------------------------------------------------------

def test_spoke_cap_is_enforced():
    inp = scenario()
    inp = replace(inp, usage=dict(inp.usage) | {(SPOKE, "2026-10"): Usage(3, {"XTS": 390000})})
    det = evaluate(inp)
    assert det.outcome == "refuse" and "sys.cap_exceeded" in ids(det)


def test_hub_budget_binds_across_spokes():
    inp = scenario()
    inp = replace(inp, usage=dict(inp.usage) | {(WHEEL, "2026-10"): Usage(9, {"XTS": 1490000})})
    det = evaluate(inp)
    assert "sys.budget_exceeded" in ids(det)
    assert not can_override(det, ids(det))  # a budget is not a payer clause


def test_silent_holds_the_worst_case():
    inp = _silent()
    # total 19500, tolerance 10%: worst case 21450. Room for 20000 passes a tap but not silence.
    inp = replace(inp, usage=dict(inp.usage) | {("spk_repeat", "2026-10"): Usage(0, {"XTS": 400000 - 20000})})
    assert "sys.cap_exceeded" in ids(evaluate(inp))
    tapped = replace(inp, params=replace(inp.params, silent_max_amount=None))
    assert evaluate(tapped).outcome == "allow"


def test_missing_usage_is_refused_not_assumed_zero():
    inp = scenario()
    det = evaluate(replace(inp, usage={}))
    assert "sys.usage_unknown" in ids(det)


def test_no_budget_in_the_currency_means_no_room():
    inp = scenario()
    det = evaluate(replace(inp, wheel=replace(inp.wheel, budgets=(Limit(__import__("gap_core").money.Money(1, "XXX"), MONTH),))))
    assert "sys.budget_exceeded" in ids(det)


# --- stage 2: authorisation binds to what was shown (invariant 10) ---------------------------

def test_unchanged_cart_at_authorisation_is_allowed():
    shown = evaluate(scenario()).presented_hash
    det = evaluate(scenario(stage="authorisation", authorised_presented_hash=shown))
    assert det.outcome == "allow" and det.fetched_hash == shown


def test_any_drift_after_approval_never_executes():
    shown = evaluate(scenario()).presented_hash
    pricier = body(lines=(CartLine("item_milk", 2, xts(6100)), CartLine("item_bread", 1, xts(4500))))
    det = evaluate(scenario(body=pricier, stage="authorisation", authorised_presented_hash=shown))
    assert det.outcome == "refuse" and "sys.drift" in ids(det)


def test_silent_repeat_within_tolerance_runs_and_beyond_it_stops():
    ok = body(fees=(Fee("delivery", xts(4900)),))   # 21400, within +10% of 19500
    det = evaluate(_silent(proposal=replace(_silent().proposal, body=ok), stage="authorisation"))
    assert det.outcome == "allow"
    too_much = body(fees=(Fee("delivery", xts(5000)),))  # 21500 > 21450
    det = evaluate(_silent(proposal=replace(_silent().proposal, body=too_much), stage="authorisation"))
    assert "sys.drift" in ids(det)


def test_silent_repeat_with_different_items_stops():
    other = body(lines=(CartLine("item_milk", 3, xts(6000)), CartLine("item_bread", 1, xts(4500))))
    det = evaluate(_silent(proposal=replace(_silent().proposal, body=other), stage="authorisation"))
    assert "sys.drift" in ids(det)


# --- overrides (invariant 12) ---------------------------------------------------------------

def _refused_by(*rules):
    inp = scenario()
    return evaluate(replace(inp, wheel=replace(inp.wheel, rules=rules)))


def test_payer_clauses_can_be_overridden_for_one_payment():
    det = _refused_by(rule("p1", "items", max_lines=1))
    assert can_override(det, {"p1"})
    assert not can_override(det, set())


@pytest.mark.parametrize("author", ["operator", "jurisdiction"])
def test_operator_and_jurisdiction_clauses_never_are(author):
    det = _refused_by(rule("p1", "items", max_lines=1), rule("o1", "merchant", block=["mrc_shop"], author=author))
    assert not can_override(det, {"p1", "o1"})


# --- types ------------------------------------------------------------------------------

def test_total_must_add_up():
    with pytest.raises(ValueError):
        body(total=xts(1))


@pytest.mark.parametrize("bad", [
    dict(rule_id="x", dimension="colour", constraint={"a": 1}, author="payer"),
    dict(rule_id="x", dimension="items", constraint={"max_lines": 0}, author="payer"),
    dict(rule_id="x", dimension="items", constraint={"unknown_key": 1}, author="payer"),
    dict(rule_id="x", dimension="velocity", constraint={"max_count": 3}, author="payer"),
    dict(rule_id="x", dimension="items", constraint={"max_lines": 2}, author="merchant"),
    dict(rule_id="x", dimension="conditions", constraint={"when": {"fact": "a", "op": "eq", "value": 1.5}, "effect": "block"}, author="payer"),
    dict(rule_id="x", dimension="conditions", constraint={"when": {"fact": "a", "op": "exists", "value": 1}, "effect": "block"}, author="payer"),
    dict(rule_id="x", dimension="time", constraint={"allow": [{"days": ["mon"], "from": "25:00", "to": "26:00", "tz": "Etc/UTC"}]}, author="payer"),
])
def test_rules_are_closed_and_validated(bad):
    with pytest.raises((ValueError, TypeError)):
        Rule(**bad)


def test_silent_spoke_needs_an_instruction():
    with pytest.raises(ValueError):
        replace(scenario().spokes[SPOKE], approval_mode="silent_with_notice")


@pytest.mark.parametrize("window, expected", [
    (Window("calendar_month", "Etc/UTC"), "2026-10"),
    (Window("calendar_day", "Pacific/Kiritimati"), "2026-10-13"),  # UTC+14: already Tuesday
    (Window("calendar_week", "Etc/UTC"), "2026-W42"),
    (Window("rolling", "Etc/UTC", 3600), "rolling:2026-10-12T12:30:00.000Z"),
])
def test_window_ids(window, expected):
    assert window.window_id(AT) == expected


def test_window_rejects_bad_shapes():
    with pytest.raises(ValueError):
        Window("rolling", "Etc/UTC")
    with pytest.raises(ValueError):
        Window("calendar_month", "Etc/UTC", 10)
