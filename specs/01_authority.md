# 01 · Authority: the Wheel

**Problem:** what is an agent allowed to do on a payer's behalf?
**Status:** design approved 25 Sep 2026; rewritten infrastructure-only 28 Sep 2026. Reviewed 9 Oct 2026.
**Invariants referenced:** 1–7, 21–24 (`README.md`). **Parameters:** `policy_parameters.md`.

---

## 1. Structure

```
  OPERATOR Tier 0      per operator, per jurisdiction, versioned; the payer cannot change it
        ∩
  HUB (the Wheel)      owned by the payer: Tier 1 rules, budget per currency, home jurisdiction,
        │              status (active / suspended / revoked)
        │
  SPOKES               grants hanging off the hub (or off another spoke, up to wheel.max_depth)
```

A payment is allowed only if it passes **operator Tier 0 ∩ hub rules ∩ the rules of the cited spoke and every ancestor**, and fits **the cited spoke's cap and every ancestor's budget, including the hub's**.

---

## 2. Entities

Fields are provisional; the model specification fixes names. All records are signed over canonical serialisation; money is minor units plus currency; time is RFC 3339 UTC.

### `Wheel`
```
wheel_id, payer_ref (opaque), home_jurisdiction, rules [Rule],
budgets {currency: {limit, window}}, status, version, host_ref, signature
```

### `Spoke`
```
spoke_id, wheel_id, parent_id,
bindings        {callers?, purpose?, instruction?}
rules           [Rule]             may only narrow the parent
cap             {currency: {limit, window}}   a ceiling, not a reservation
approval_mode   explicit_tap | silent_with_notice
                silent_with_notice is valid only when an instruction binding is set
valid_from, valid_until (required), status, version, signature
```
A spoke with an `instruction` binding is a **standing instruction**: an exact, repeatable order under signed terms (including its `instruction.price_tolerance`). In AP2 v0.2 terms, an open mandate the agent may close repeatedly.

### `Rule`
```
rule_id, dimension (§3), constraint (typed per dimension),
author  operator | payer | system,  author_ref, version
```

### `WheelEvent`
```
event_id, target (wheel|spoke + id),
event_type   create | narrow | widen | suspend | resume | revoke | expire | renew | change_requested
previous_hash, new_hash, initiated_by (payer|operator|system|agent),
reason (required for operator and system), presented_hash (what the payer was shown),
auth_strength (achieved), occurred_at, signature
```

### `AuthorityReceipt`
Returned by the wheel host to the operator for each check:
```
receipt_id, outcome (allowed|refused), spoke_id, deciding_clauses [{rule_id, dimension, author}],
approval_mode + set_by, versions {wheel, spoke, tier0, mapping, evaluator}, reserved, signature
```
A receipt never reveals other spokes, other rules, or budget figures beyond what the operator needs to act.

---

## 3. Rule dimensions (the catalogue)

Every power an agent can exercise must be one of these. The infrastructure defines the **dimension and its evaluation**; deployments set the **values**.

| # | Dimension | Constrains |
|---|---|---|
| 1 | Caller | which agents or caller classes may cite the spoke |
| 2 | Merchant | allowlist, blocklist, categories |
| 3 | Items | item identifiers, item categories, quantities |
| 4 | Substitution | none / same item other variant / same category within a bound |
| 5 | Item attributes | attribute terms or classifications to block or require (e.g. contents, categories) |
| 6 | Amount | per-transaction maximum; aggregate per window |
| 7 | Velocity | maximum count per window |
| 8 | Payment method | permitted rails (by rail profile) |
| 9 | Time | validity (required); permitted and excluded time windows |
| 10 | Approval mode | `explicit_tap` / `silent_with_notice` |
| 11 | Destination | permitted delivery or payee destinations; quarantine rules for new ones |
| 12 | Conditions on facts | predicates over injected facts (e.g. account age, success rate, dispute state, notice-delivery confirmation, payer context) |
| 13 | Extras | optional additions to an order or payment (e.g. gratuities, discounts, instructions to the merchant): **denied unless a rule allows** |
| 14 | Currency / geography | per jurisdiction |

---

## 4. Authority check (inside `evaluate()`)

1. **Spoke:** refuse if missing, inactive, outside validity, or not bound to the caller. Never infer.
2. **Chain:** operator Tier 0 (for the payment's jurisdiction) + hub + spoke ancestors + spoke.
3. **Every dimension:** each rule in the chain must pass; any unmodelled attribute fails.
4. **Approval mode:** start from the spoke; any layer, rail profile, or failing `approval.silent.preconditions` forces `explicit_tap`; strictest wins; record which layer set it.
5. **Amount:** fits the spoke cap and every ancestor's remaining budget (holds: `03`).
6. **Receipt:** every failing clause, attributed to its author.

---

## 5. Lifecycle

| Change | Who | Requires | In-flight effect |
|---|---|---|---|
| Create spoke | Payer | `auth.strength.create`; terms presented and hashed | – |
| Narrow | Payer; system on pre-agreed triggers held as rules (`conditional_unlocks` re-lock conditions, expiry) | Payer: authenticated action. System: the triggering fact recorded | Pending proposals re-checked at authorisation |
| Widen | **Payer only** | `auth.strength.widen`; new terms presented and hashed | Never retroactive |
| Suspend / resume | Payer; **operator may suspend**; **system suspends on key compromise** (`07` §2) | Operator: reason + payer notified. System: the compromise record. Resume: `auth.strength.resume` from a surface that is not compromised | Suspend cancels pending proposals and releases holds |
| Revoke | Payer | Authenticated action | As suspend, permanent; placed payments proceed to their outcome |
| Expire | System at `valid_until` | – | As revoke |
| Renew | Payer | `auth.strength.widen`; a new grant | New version |
| Request change | Any caller | – | Nothing until the payer approves on a Trusted Surface |

Every change is a new signed, chained version; determinations record the versions they used.

---

## 6. The wheel host

The wheel is held by **one neutral host chosen by the payer**. Operators query it; it evaluates, places holds (`03`), records the decision, and returns a signed `AuthorityReceipt`. The wheel is a payer-owned signed document and can move between hosts (the Account Aggregator / consent-manager pattern).

---

## 7. Jurisdictions

The core is jurisdiction-free. Each jurisdiction mapping supplies rail profiles, the authentication-strength scale, silent-approval limits, consent clauses, dispute routes and data-protection classes. A wheel has a home jurisdiction; each payment is evaluated under its own jurisdiction's mapping; both versions are recorded. Budgets are per currency; conversions are injected facts, never computed.

---

## 8. Outward mappings (interoperability, not dependence)

| External construct | Wheel equivalent |
|---|---|
| AP2 v0.2 open Checkout/Payment Mandate | A spoke; dimensions map to AP2 constraint types |
| AP2 v0.2 closed mandate | A proposal citing its spoke |
| AP2 Trusted Surface | The payer's registered Trusted Surface |
| ACP delegated-payment Allowance | A one-time grant emitted for one approved payment |

Jurisdiction-specific schemes (national delegation, mandate and agent-payment schemes) are mapped in each jurisdiction's notes under `../jurisdictions/`, not here.

---

## 9. Tests

- No event sequence except a payer widen at `auth.strength.widen` enlarges any permitted set.
- Total spend across spokes never exceeds the hub budget.
- Adding any rule or fact never loosens the approval mode.
- Uncited, foreign or inactive spoke is always refused.
- Any unmodelled attribute is always refused.
- A receipt never contains another spoke's rules, caps or spend.
- Same proposal, facts, time and versions always give the same receipt.
- Operator events other than `suspend` are always rejected.
- A compromised surface key suspends its wheel; that key can never resume it.
- Narrowing or widening a parent never widens a child.

---

## 10. Decisions

| # | Decision |
|---|---|
| Framing | Operator = customer; payer = protected; regulator = acceptance test; verify-not-see; attribute-not-adjudicate |
| Shape | Layered hub + spokes |
| D1 | Spokes have optional bindings; every proposal cites its spoke |
| D2 | Spoke limits are caps; ancestors' budgets are binding |
| D3 | Approval mode is a rule dimension; strictest wins |
| D4 | Neutral, portable wheel host; signed receipts |
| D5 | 14-dimension catalogue; unmodelled = denied |
| D6 | Jurisdiction-free core; versioned mappings; per-currency budgets |
| D7 | Lifecycle as §5; operator may only suspend |
