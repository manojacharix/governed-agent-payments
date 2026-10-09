# 05 · Reconciliation

**Problem:** did what was decided actually happen, and what happens when it didn't?
**Status:** design approved 28–29 Sep 2026, infrastructure-only. Reviewed 9 Oct 2026.
**Invariants referenced:** 14–17, 21–23 (`README.md`). **Parameters:** `recon.sweep_interval`, `recon.sweep_before_hold`, `external.count_spend`, `external.notify`, `external.unusual_predicates`, `money.max_pending_age`.

---

## 1. Source adapters

Each source is a pluggable adapter declaring:
```
source_id
class            bank | rail | merchant | receipt
coverage         limits, e.g. "only the most recent n records", "incomplete timestamps"
freshness        how current its data is
```
New sources (account aggregator feeds, email, SMS) are new adapters, not redesigns.

## 2. Authority by question

| Question | Authoritative | Corroborating |
|---|---|---|
| Did money move, and how much? | `bank`, else `rail` | `merchant` bill, `receipt` |
| What was ordered and delivered? | `merchant` | `receipt` |
| What was authorised? | the ledger | – |

Every conclusion records the class it rests on. Disagreement between authoritative sources is a `source_conflict`; no winner is picked silently.

---

## 3. When it runs

- **Event-driven:** after every `ExecutionAttempt` (`04`) and every `pending_outcome` (`03`).
- **Scheduled:** every `recon.sweep_interval`; and before each hold when `recon.sweep_before_hold` is true.
- **Every sweep is recorded:** time, source, hash of what it saw, coverage. Where a source's coverage could hide orders, a `coverage_gap` is recorded explicitly.

---

## 4. Findings

| Finding | Meaning | Handling |
|---|---|---|
| `matched` | Everything agrees | Auto-close |
| `pending_resolved` | A `pending_outcome` settled by definitive, consistent evidence | Capture / release (`03`); close |
| `overcapture` | Charged more than held | Open; payer notified |
| `missing_order` | Ledger executed; merchant has no order | Open |
| `item_mismatch` | Merchant's order differs from the authorised cart | Open |
| `refund_stalled` | Refund initiated, not completed | Open until complete |
| `source_conflict` | Authoritative sources disagree | Open, stating which said what |
| `coverage_gap` | Orders may be unseen | Recorded; open while unresolved |
| `external_order` | At the merchant, not in the ledger | Recorded as external; counting per `external.count_spend`; notices per `external.notify` and `external.unusual_predicates` |

```
finding_id, kind, payment_ref / merchant_order_ref, sources [{source_id, class, observed_hash}],
status open | closed, closed_by (evidence | payer_acceptance), suggested_next_step,
opened_at, closed_at, previous_hash, signature
```

---

## 5. Resolution

- **Auto-resolve only on definitive, consistent evidence** under §2.
- **Otherwise open**, visible to the payer with evidence gathered and a suggested next step from the rail profile's `dispute_route`.
- **Closes only** by definitive evidence or explicit payer acceptance; never by timeout, never by an agent.
- **No edits:** resolutions are compensating records; money changes go through `03` postings.
- **An open `overcapture` or `missing_order` on a spoke is a failing fact** that forces `explicit_tap` on that spoke until closed.
- **External orders are never attributed to an agent.**

---

## 6. Tests

- Replaying a sweep yields the same findings.
- No finding closes without definitive evidence or payer acceptance.
- `external_order` is never attributed to a caller.
- `coverage_gap` is raised whenever a source's coverage could hide orders.
- `source_conflict` is raised whenever authoritative sources disagree.
- Resolutions never modify existing records.

---

## 7. Decisions

| # | Decision |
|---|---|
| D1 | Event-driven plus scheduled sweeps; every sweep recorded; coverage gaps explicit |
| D2 | Orders outside the layer are `external_order`; counting and notices are payer parameters; never attributed to an agent |
| D3 | Evidence decides; only definitive, consistent cases auto-resolve; others open with a next step |
| D4 | Authority by question (money: bank, else rail; goods: merchant; authorisation: ledger); pluggable adapters |
