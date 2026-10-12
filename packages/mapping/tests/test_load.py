"""Phase 3: jurisdiction mappings and Tier 0 configs load strictly and drive the same evaluator."""

import copy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from gap_core.authority import Caller
from gap_core.evaluate import evaluate
from gap_core.facts import Fact
from gap_core.money import Money
from gap_core.testing import scenario
from gap_mapping.load import REQUIRED_TIER0, ConfigError, evaluation_context, load_mapping, load_tier0

ROOT = Path(__file__).resolve().parents[3]
IN = ROOT / "mappings/in/v0.1.0.yaml"
UK = ROOT / "mappings/uk/v0.1.0.yaml"
PERSONAL_IN = ROOT / "tier0/examples/personal_in.yaml"
PERSONAL_UK = ROOT / "tier0/examples/personal_uk.yaml"
B2B_IN = ROOT / "tier0/examples/b2b_procurement_in.yaml"


def _write(tmp_path, doc, name="x.yaml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    return p


# --- the shipped files ----------------------------------------------------------------

def test_shipped_mappings_and_configs_load():
    india, uk = load_mapping(IN), load_mapping(UK)
    assert india.ref == "in@0.1.0" and india.status == "interpretation"
    assert uk.ref == "uk@0.1.0" and uk.status == "reference"
    for path, m in [(PERSONAL_IN, india), (B2B_IN, india), (PERSONAL_UK, uk)]:
        load_tier0(path, m)


def test_india_rails_match_the_jurisdiction_notes():
    rails = load_mapping(IN).rails
    assert rails["in:upi_intent"].amount_binding == "bound_at_rail"
    assert rails["in:upi_intent"].authentication == "human_per_payment"
    assert rails["in:merchant_wallet"].amount_binding == "not_bound"
    rp = rails["in:upi_reserve_pay"]
    assert rp.per_transaction_max == Money(1000000, "INR") and rp.max_duration_s == 90 * 86400
    assert rp.status == "ready_no_merchant"
    assert rails["in:uap"].status == "placeholder"


def test_india_silent_ceiling_is_unset_and_retention_is_three_years():
    india = load_mapping(IN)
    assert india.silent_max_amount is None
    assert india.evidence_retention_s == 3 * 365 * 86400


# --- strictness -----------------------------------------------------------------------

def _mapping_doc():
    return yaml.safe_load(IN.read_text())


def _tier0_doc():
    return yaml.safe_load(PERSONAL_IN.read_text())


@pytest.mark.parametrize("mutate", [
    lambda d: d["mapping"].update(colour="blue"),
    lambda d: d["mapping"]["rails"][0].update(speed="fast"),
    lambda d: d["mapping"]["rails"][0].update(rail_id="upi_intent"),            # not namespaced
    lambda d: d["mapping"]["rails"][0].update(rail_id="uk:upi_intent"),         # wrong namespace
    lambda d: d["mapping"]["rails"][0].update(dispute_route="in:nowhere"),
    lambda d: d["mapping"]["rails"][0].update(amount_binding="maybe"),
    lambda d: d["mapping"].update(version="1"),
    lambda d: d["mapping"].update(evidence_retention_s=None),                    # required for interpretation
    lambda d: d["mapping"].update(auth_strength_scale=d["mapping"]["auth_strength_scale"][:1]),   # one level is not a scale
    lambda d: d["mapping"]["auth_strength_scale"][1].update(requires=["telepathy"]),
    lambda d: d["mapping"]["clauses"][0].update(dimension="colour"),
    lambda d: d["mapping"]["silent"].update(max_amount={"amount_minor": 1.5, "currency": "INR"}),
])
def test_mapping_rejects_malformed_documents(tmp_path, mutate):
    doc = _mapping_doc()
    mutate(doc)
    with pytest.raises(ConfigError):
        load_mapping(_write(tmp_path, doc))


@pytest.mark.parametrize("param", sorted(REQUIRED_TIER0))
def test_tier0_missing_any_required_parameter_fails_to_load(tmp_path, param):
    doc = _tier0_doc()
    del doc["tier0"]["parameters"][param]
    with pytest.raises(ConfigError):
        load_tier0(_write(tmp_path, doc), load_mapping(IN))


@pytest.mark.parametrize("mutate", [
    lambda p: p.update({"auth.strength.create": "retina_and_dna"}),            # not on the mapping's scale
    lambda p: p.update({"keys.algorithms": ["RS256"]}),
    lambda p: p.update({"decision.duplicate_window": -1}),
    lambda p: p.update({"identity.caller.min_assurance": {"explicit_tap": "claimed", "silent_with_notice": "key_bound"}}),
    lambda p: p.update({"approval.silent.preconditions": [{"fact": "x", "op": "eq", "value": 0.5}]}),
    lambda p: p.update({"recon.sweep_before_hold": "yes"}),
    lambda p: p.update({"unknown.parameter": 1}),
])
def test_tier0_rejects_bad_values(tmp_path, mutate):
    doc = _tier0_doc()
    mutate(doc["tier0"]["parameters"])
    with pytest.raises((ConfigError, TypeError, ValueError)):
        load_tier0(_write(tmp_path, doc), load_mapping(IN))


def test_tier0_must_match_the_mapping_jurisdiction():
    with pytest.raises(ConfigError):
        load_tier0(PERSONAL_IN, load_mapping(UK))


def test_operator_silent_ceiling_never_exceeds_the_jurisdiction(tmp_path):
    doc = _mapping_doc()
    doc["mapping"]["silent"]["max_amount"] = {"amount_minor": 100000, "currency": "INR"}
    india = load_mapping(_write(tmp_path, doc, "m.yaml"))
    assert load_tier0(PERSONAL_IN, india).params.silent_max_amount == Money(100000, "INR")


def test_operator_rules_are_operator_authored_and_clauses_jurisdiction_authored():
    india = load_mapping(IN)
    t0 = load_tier0(PERSONAL_IN, india)
    assert {r.author for r in t0.rules} == {"operator"}
    assert {r.author for r in india.clauses} == {"jurisdiction"}


# --- the same evaluator, different configuration ------------------------------------------

def _inp(mapping_path, tier0_path, currency, **kw):
    m = load_mapping(mapping_path)
    return replace(scenario(currency), **evaluation_context(m, load_tier0(tier0_path, m)), **kw)


def test_india_personal_allows_an_ordinary_order_on_upi():
    det = evaluate(_inp(IN, PERSONAL_IN, "INR"))
    assert det.outcome == "allow"
    assert det.rail_id == "in:upi_intent" and det.rail_chosen_by == "default"
    assert det.amount_binding == "bound_at_rail"


def test_the_same_rupee_order_under_the_uk_mapping_is_refused():
    det = evaluate(_inp(UK, PERSONAL_UK, "INR"))
    assert det.outcome == "refuse"
    assert "uk.currency_gbp" in {c.rule_id for c in det.failing_clauses}


def test_a_pound_order_under_the_uk_mapping_is_allowed():
    det = evaluate(_inp(UK, PERSONAL_UK, "GBP"))
    assert det.outcome == "allow" and det.rail_id.startswith("uk:")


def test_procurement_operator_needs_a_key_bound_caller():
    det = evaluate(_inp(IN, B2B_IN, "INR"))
    assert det.outcome == "no_authority" and "sys.caller_assurance" in {c.rule_id for c in det.failing_clauses}


def _b2b(*facts):
    inp = _inp(IN, B2B_IN, "INR", caller=Caller("clr_agent_a", "key_bound"))
    fs = inp.facts
    for f in facts:
        fs = fs.with_fact(f)
    return replace(inp, facts=fs)


def test_procurement_refuses_without_a_matched_po_and_approved_supplier():
    det = evaluate(_b2b())
    assert {c.rule_id for c in det.failing_clauses} >= {"op.po_matched", "op.approved_supplier"}
    assert not any(c.overridable for c in det.failing_clauses)  # operator clauses: no payer override


def test_procurement_allows_with_verified_erp_facts():
    det = evaluate(_b2b(
        Fact("ext.procurement.po_matched", True, "erp", True, "source_signed"),
        Fact("ext.procurement.supplier_approved", True, "erp", True, "source_signed"),
    ))
    assert det.outcome == "allow" and det.approval_mode == "explicit_tap"


def test_procurement_is_not_fooled_by_unverified_facts():
    det = evaluate(_b2b(
        Fact("ext.procurement.po_matched", True, "agent", False, "capturer_signed"),
        Fact("ext.procurement.supplier_approved", True, "agent", False, "capturer_signed"),
    ))
    assert {"op.po_matched", "op.approved_supplier"} <= {c.rule_id for c in det.failing_clauses}
