# 02 · Decision Engine

**Problem:** how does one proposal become one decision, reproducibly?
**Status:** design approved 28 Sep 2026; rewritten infrastructure-only 28 Sep 2026. Reviewed 9 Oct 2026.
**Invariants referenced:** 7–13 (`README.md`). **Parameters:** `decision.clarify_threshold`, `decision.duplicate_window`, `facts.verified_sources`, `approval.silent.*`, `auth.strength.override`.

---

## 1. Two stages

### Stage 1: at proposal

| Step | What happens | Short-circuits? |
|---|---|---|
| 0 Intake | Validate shape; require `intent_id`; resolve cited spoke and caller; compute `proposal_hash`. A repeated `intent_id` with the **same** `proposal_hash` returns the original decision; with a **different** one it is rejected as `intent_conflict` and creates nothing | Yes: `no_authority`, `intent_conflict` |
| 1 Capture | Fresh cart from the merchant adapter; product data; attribute sources; agent confidence; other advisory signals; notice-delivery and payer-context facts; account facts; time. **Freeze and hash** | No |
| 2 Authority | `01` §4 | No |
| 3 Safety | Product-attribute and stated-quantity rules; only `facts.verified_sources` can clear; other sources can only block | No |
| 4 Confidence | Below `decision.clarify_threshold` → `clarify` | No |
| 5 Duplicate | New `intent_id` + same merchant + same cart hash within `decision.duplicate_window` → force `explicit_tap` | No |
| 6 Approval mode | Strictest wins across layers, rail profile and facts | No |
| 7 Budget | Cap and ancestor budgets (holds: `03`) | No |
| 8 Determination | Signed; every failing clause listed | – |

### Stage 2: at authorisation (or silent execution)
1. Re-fetch the cart.
2. `explicit_tap`: if the fresh hash differs from the authorised hash (items, quantities, fees, total, rail) → stop and re-present; nothing executes. `silent_with_notice`: compare against the standing instruction's signed `instruction.price_tolerance`.
3. Re-run steps 1–7 on fresh facts.
4. If allowed: hold (`03`), execute (`04`). A change between this re-fetch and placement is caught by the rail (`amount_binding = bound_at_rail`) or by reconciliation (`04` §1).

---

## 2. Outcomes and resolutions

**Outcomes:** `allow` (with approval mode and setter) · `clarify` (ambiguous fields) · `refuse` (every failing clause with author and `overridable`) · `no_authority`.

**Resolutions (separate record):** `pending` · `approved` · `approved_with_override` · `edited` · `declined` · `expired` · `proceeded_on_silence` · `superseded`.

**Overrides:** `overridable` is true only for payer-authored clauses. An override covers one payment, requires `auth.strength.override` on a Trusted Surface, presents exactly what is overridden, and is recorded as `approved_with_override` listing the clauses. It never changes a rule.

---

## 3. Duplicate prevention

1. `intent_id` required. A repeat with the same `proposal_hash` (canonical hash of the proposal body: spoke, merchant, items, quantities, rail, destination, extras) returns the original decision; a repeat with a different `proposal_hash` is rejected as `intent_conflict`. An `intent_id` is never reused for a different request, so an earlier "allow" can never be carried onto new content.
2. Content guard as in Stage 1 step 5: never silently allowed, never silently dropped.
3. Standing instructions: the layer computes `intent_id` = instruction ID + scheduled slot; a slot executes at most once.

---

## 4. Facts

```
name, value (exactly as returned), source, source_version, captured_at,
verified   true only for sources listed in facts.verified_sources
attestation {class, attested_by, capturer, raw_hash?}   who vouches for the value (07 §6)
```
A fact clears a check only if its source is verified **and** its attestation meets the deployment's floor. Caller assurance (`07` §4.1) is itself an injected, verified fact.
The frozen set is hashed into the Determination. Unverified facts can only block, force `explicit_tap`, or force `clarify`.

---

## 5. Determination

```
determination_id, proposal_id, intent_id, proposal_hash, spoke_id, stage (proposal|authorisation),
outcome, approval_mode + set_by, failing_clauses [{rule_id, dimension, author, overridable}],
clarify_fields, facts_hash, fact_refs [...], presented_hash / fetched_hash,
rail_id + rail_chosen_by, duplicate_check, versions {wheel, spoke, tier0, mapping, evaluator},
evaluated_at (injected), links, signature
```

---

## 6. Tests

- Replay: every recorded Determination reproduces from frozen facts, versions and time.
- Monotonic caution: adding any unverified fact never makes an outcome more permissive.
- Overriding an operator or jurisdiction clause always fails.
- Forced drift after authorisation never executes.
- A repeated `intent_id` never creates a second Determination or order.
- A repeated `intent_id` with a different `proposal_hash` is always rejected as `intent_conflict`, never answered with the original decision.
- A standing-instruction slot never executes twice under concurrency.
- A proposal violating *k* clauses reports all *k*.
- Import-graph purity of `evaluate()`.

---

## 7. Decisions

| # | Decision |
|---|---|
| D1 | Capture, freeze, decide |
| D2 | Payer may override only payer-authored clauses, one payment, under payer authentication |
| D3 | Authorisation binds to the presented hash; any change → re-present; silent follows the instruction's tolerance |
| D4 | `intent_id` required; same ID + same payload returns the original, same ID + different payload is `intent_conflict` (approved 9 Oct); content guard; layer-computed slot IDs |
| D5 | Unverified inputs only add caution; confidence below the threshold → clarify |
