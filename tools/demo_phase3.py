"""Phase 3 demo (make mappings): one evaluator, configuration swapped underneath it."""

from dataclasses import replace

from gap_core.authority import Caller
from gap_core.evaluate import evaluate
from gap_core.facts import Fact
from gap_core.testing import scenario
from gap_mapping.load import evaluation_context, load_mapping, load_tier0


def run(title, mapping_file, tier0_file, currency, **kw):
    m = load_mapping(mapping_file)
    t0 = load_tier0(tier0_file, m)
    inp = replace(scenario(currency), **evaluation_context(m, t0), **kw)
    det = evaluate(inp)
    print(f"\n{title}\n  config         {m.ref} + {t0.operator_ref}")
    print(f"  outcome        {det.outcome}" + (f" on {det.rail_id} ({det.amount_binding})" if det.rail_id and det.outcome == "allow" else ""))
    for c in det.failing_clauses:
        print(f"  failing        {c.rule_id:<28} by {c.author}")


IN, UK = "mappings/in/v0.1.0.yaml", "mappings/uk/v0.1.0.yaml"
run("1. A 195-rupee grocery order, India, personal operator", IN, "tier0/examples/personal_in.yaml", "INR")
run("2. The same rupee order under the UK mapping", UK, "tier0/examples/personal_uk.yaml", "INR")
run("3. The same order in pounds under the UK mapping", UK, "tier0/examples/personal_uk.yaml", "GBP")

b2b = "tier0/examples/b2b_procurement_in.yaml"
key = Caller("clr_agent_a", "key_bound")
run("4. Same order, procurement operator, no purchase order on record", IN, b2b, "INR", caller=key)
base = scenario("INR")
erp = base.facts.with_fact(Fact("ext.procurement.po_matched", True, "erp", True, "source_signed")).with_fact(
    Fact("ext.procurement.supplier_approved", True, "erp", True, "source_signed"))
run("5. Procurement operator, PO matched and supplier approved by the ERP (verified)", IN, b2b, "INR", caller=key, facts=erp)
fake = base.facts.with_fact(Fact("ext.procurement.po_matched", True, "agent", False, "capturer_signed")).with_fact(
    Fact("ext.procurement.supplier_approved", True, "agent", False, "capturer_signed"))
run("6. Procurement operator, the agent claims a PO match (unverified)", IN, b2b, "INR", caller=key, facts=fake)
