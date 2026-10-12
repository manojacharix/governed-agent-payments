"""Phase 2 demo (make evaluate): one evaluator, six situations. Example values in XTS minor units."""

from dataclasses import replace

from gap_core.authority import Caller
from gap_core.evaluate import Determination, PriorIntent, Usage, evaluate
from gap_core.proposal import CartLine
from gap_core.testing import SPOKE, WHEEL, body, repeat_spoke, rule, scenario, xts


def show(title, result):
    print(f"\n{title}")
    if not isinstance(result, Determination):
        print(f"  {type(result).__name__}: {result}")
        return
    print(f"  outcome        {result.outcome}")
    print(f"  approval       {result.approval_mode}  (set by {result.approval_mode_set_by})")
    if result.rail_id:
        print(f"  rail           {result.rail_id}  chosen by {result.rail_chosen_by}, amount {result.amount_binding}")
    for c in result.failing_clauses:
        print(f"  failing        {c.rule_id:<22} {c.dimension:<16} by {c.author}{'  (payer may override once)' if c.overridable else ''}")
    for f in result.clarify_fields:
        print(f"  clarify        {f}")


base = scenario()
show("1. Allowed: 2 milk + 1 bread + delivery = 19,500, nothing used yet this month", evaluate(base))

near = replace(base, usage=dict(base.usage) | {(SPOKE, "2026-10"): Usage(6, {"XTS": 380000})})
show("2. Near the grocery cap: 380,000 of 400,000 used, order of 19,500 still fits", evaluate(near))

rules = (
    rule("no_alcohol_shops", "merchant", block_categories=["std:mcc:5921"], author="operator"),
    rule("max_one_line", "items", max_lines=1),
    rule("small_orders_only", "amount", per_transaction_max=xts(10000), author="jurisdiction"),
    rule("office_only", "destination", allow=["dst_office"]),
)
show("3. Refused: four rules in force, three broken, every one reported with its author", evaluate(replace(base, wheel=replace(base.wheel, rules=rules))))

show("4. Same intent, different cart: rejected, nothing created", evaluate(replace(base, prior_intents={"intent-1": PriorIntent("sha256:" + "9" * 64, "det_0")})))

silent = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler", caller=Caller("clr_scheduler", "key_bound"))
show("5. Standing instruction: the weekly repeat may run silently, after notice", evaluate(silent))

shown = evaluate(base).presented_hash
pricier = body(lines=(CartLine("item_milk", 2, xts(6100)), CartLine("item_bread", 1, xts(4500))))
show("6. Price changed after the payer approved: stopped, re-present", evaluate(scenario(body=pricier, stage="authorisation", authorised_presented_hash=shown)))
