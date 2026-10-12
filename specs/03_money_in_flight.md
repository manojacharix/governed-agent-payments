# 03 · Money in Flight

**Problem:** between "allowed" and "paid", how do caps and budgets stay true?
**Status:** design approved 28 Sep 2026; rewritten infrastructure-only 28 Sep 2026. Reviewed 9 Oct 2026.
**Invariants referenced:** 14–17 (`README.md`). **Parameters:** `budget.window`, `money.max_pending_age`.

---

## 1. Accounts

For every wheel and every spoke, per currency, per budget window (`budget.window`):

| Account | Meaning |
|---|---|
| `available` | room left under the limit |
| `held` | reserved for authorised, not-yet-settled payments |
| `spent` | captured payments, net of completed refunds |

**Invariant:** `available + held + spent = limit`, always. `available` goes below zero only through an over-capture.

---

## 2. Postings

Append-only, hash-chained, signed; balances derived, never stored. A spoke and all its ancestors are posted together, atomically.

| Posting | Movement | Trigger |
|---|---|---|
| `hold` | available → held | Authorisation, after the budget re-check passes |
| `capture` | held → spent | Definitive success signal, at the actual amount |
| `release` | held → available | Definitive failure; remainder after a lower capture; suspend or revoke before execution |
| `overcapture` | available → spent (excess only) | Actual charge above the hold |
| `refund` | spent → available, original window | Completed refund only |

```
posting_id, txn_group_id, account_set {owner, currency, window}, kind, amount,
payment_ref, signal_ref, posted_at, previous_hash, signature
```

---

## 3. Lifecycle

1. **Hold at authorisation**, atomically, one payment at a time per wheel. `explicit_tap` holds exactly the authorised total; `silent_with_notice` holds the instruction's worst case (total plus `instruction.price_tolerance`). A failed check holds nothing and re-presents. The hold counts in the window in which it is placed.
2. **Certain outcome:** success → `capture` actual, `release` remainder; failure → `release` all.
3. **Unknown outcome:** `pending_outcome`, still counting; only definitive signals (merchant, rail, reconciliation) move it; escalated to reconciliation after `money.max_pending_age`. Agent claims never move balances.
4. **Over-capture:** capture the hold, `overcapture` the excess; notify the payer; open a discrepancy (`05`); no new holds on the affected accounts until `available ≥ 0`; attribute to the merchant.
5. **Refund:** completed refunds only; original window only; partial amounts only.
6. **Late settlement** (approved 12 Oct): capture, release and overcapture post to the window in which the hold was placed, even after it has closed. "No new holds while `available < 0`" is checked on the current window. An overcapture in a closed window still opens a finding and notifies the payer.
7. **Suspend / revoke / expiry:** release pending holds not yet executed; executed payments proceed to their outcome.

---

## 4. Tests

- The balance equation holds for every account set and window under concurrent authorisations.
- Across spokes, `held + spent` never exceeds an ancestor's limit except via over-capture.
- No capture without a hold; no release above the hold; ancestors always posted together.
- No agent message produces a posting.
- A refund never increases a later window's `available`.
- While `available < 0`, no hold succeeds.
- Replaying all postings reproduces every balance.

---

## 5. Decisions

| # | Decision |
|---|---|
| D1 | Hold at authorisation, atomically, one per wheel at a time; exact total or instruction worst case |
| D2 | Unknown outcome keeps the hold; only definitive signals move it |
| D3 | Capture actual; lower releases the rest; higher is an over-capture exception |
| D4 | Refunds restore the original window only |
| D5 | Double-entry postings, derived balances |
