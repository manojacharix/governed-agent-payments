# 04 · Rails

**Problem:** how does one decision execute safely across payment methods with different guarantees?
**Status:** design approved 28 Sep 2026; rewritten infrastructure-only 28 Sep 2026. Reviewed 9 Oct 2026.
**Invariants referenced:** 18–20 (`README.md`). **Parameters:** `auth.strength.no_auth_rail`; rail profiles live in jurisdiction mappings.

---

## 1. Rail capability profile

Versioned data in each jurisdiction mapping. The core reads properties, never names.

```
rail_id              namespaced by jurisdiction
authentication       human_per_payment | human_at_setup | none
placement            pending_then_confirm | immediate
amount_binding       bound_at_rail | not_bound
                     bound_at_rail: the rail's own authentication shows the payer the payee and amount
                     and will not settle a different amount; not_bound: nothing on the rail stops a change
definitive_signals   statuses meaning success / failure / unknown, per merchant adapter
retry_policy         errors safe to retry, max retries, window
reversibility        refund_only | chargeback | …
dispute_route        identifier of the jurisdiction's dispute process for this rail
limits               per-transaction, per-period, duration
approval_modes       modes the jurisdiction permits on this rail
status               live | excluded | ready_no_merchant | placeholder | reference
```

The profile is one more layer in the decision: **strictest wins**. A rail with `authentication = none` requires `auth.strength.no_auth_rail` on a Trusted Surface unless a permitted, unlocked standing instruction applies.

**The gap between check and placement.** Stage 2 (`02`) re-fetches the cart just before execution, but merchant interfaces generally cannot place an order *only if* the cart still has a given hash. A change in that window is caught as follows:
- `bound_at_rail`: the payer sees the amount again at the rail's authentication; a different amount is not paid.
- `not_bound`: nothing stops it in real time. Reconciliation (`05`) compares the merchant's order and the charge against the authorised hash after every `ExecutionAttempt`, and raises `item_mismatch` or `overcapture`.

`not_bound` is recorded in every Determination that uses the rail, so evidence packs state which protection applied. Deployments can make `amount_binding` a condition for `silent_with_notice` through `approval.silent.preconditions`.

Concrete rail profiles belong to jurisdiction mappings (`../jurisdictions/`), not to this spec.

---

## 2. Choosing the rail

1. Default: the **safest permitted rail** (highest authentication class the spoke allows).
2. The agent may propose any permitted rail; never one outside the spoke's set.
3. `explicit_tap`: the rail is presented; the payer may switch among permitted rails; switching re-runs the engine and creates a new presented hash.
4. `silent_with_notice`: the rail fixed in the standing instruction.
5. The Determination records `rail_id` and `rail_chosen_by` (`default` | `agent` | `payer` | `instruction`).

---

## 3. Execution

### `ExecutionAttempt`
```
attempt_id, determination_id, rail_id, merchant_adapter, idempotency_key,
request_hash (payload in the profile store), response_status, outcome (success|failure|unknown),
merchant_order_ref, started_at, ended_at, retry_of, signature
```

### Ambiguous failures
1. Check the merchant adapter's order history for a matching order. Found → success; capture per `03`.
2. Not found → retry only if the rail profile marks the error safe, within its limits.
3. Never switch rail automatically; a rail failure re-presents to the payer.
4. Still unclear → `pending_outcome`; reconciliation (`05`).

---

## 4. Protocol adapters

After the core specs are implemented and verified:
- **AP2 v0.2:** export a spoke as an open Checkout/Payment Mandate (SD-JWT, selective disclosure); verify an incoming closed mandate against its open mandate as a proposal citing that spoke; publish dimensions without an AP2 equivalent as AP2 constraint types.
- **ACP:** documented mapping (one-time Allowance emitted per approved card payment); not implemented until a deployment needs it.

---

## 5. Tests

- A fake that creates the order and then times out never produces a second order.
- No rail change executes without a new authorisation.
- A rail outside the permitted set is always refused.
- An `authentication = none` rail never executes without the required authentication or a permitted, unlocked standing instruction.
- Retries never exceed the profile's limits or occur for errors not marked safe.
- An AP2 closed mandate outside its open mandate's constraints is always refused.
- The core never references a rail by name.
- Every execution on a `not_bound` rail is followed by a reconciliation comparison against the authorised hash.

---

## 6. Decisions

| # | Decision |
|---|---|
| D1 | Rails are capability profiles in mapping data; strictest wins |
| D2 | Rail is part of the authorisation; safest permitted by default |
| D3 | AP2 v0.2 adapter after the core; ACP mapping only |
| D4 | `ExecutionAttempt`; prove no order before retry; never auto-switch |
| D5 | Rail profiles declare `amount_binding`; the check-to-placement gap on `not_bound` rails is closed by mandatory post-execution reconciliation, and stated in evidence (approved 9 Oct) |
