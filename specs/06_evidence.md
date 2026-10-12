# 06 · Evidence and Disputes

**Problem:** when someone asks "who authorised this, and was it followed?", produce an answer that convinces a party who does not trust the operator.
**Status:** design approved 29 Sep 2026, infrastructure-only. Reviewed 9 Oct 2026.
**Invariants referenced:** 21–23 (`README.md`). **Parameters:** `evidence.retention` (set by the jurisdiction mapping).

---

## 1. The evidence pack

A **machine-verifiable bundle**. The bundle is the evidence; any readable view is a rendering of it.

**Contents (for the disputed payment):**
- the spoke and wheel with their version chains; the operator Tier 0 and jurisdiction mapping versions
- both Determinations (proposal, authorisation) and the frozen facts disclosed for this case
- the presented hash and the authorisation event, with the authentication strength achieved
- `ExecutionAttempt`s (`04`), postings (`03`), reconciliation findings (`05`)
- hash-chain proofs, signatures, public keys, evaluator version

**Open verifier** (a standalone tool anyone can run) checks:
1. every signature is valid;
2. every chain is unbroken;
3. the decision **reproduces** from the disclosed facts and versions;
4. the presented hash matches what was authorised;
5. every signing key is in the key log and was valid when used (`07` §2), and the evaluator digest has a published release (`07` §7);
6. each fact's attestation (`07` §6), so the pack never claims more than it supports;
7. for a wheel that moved hosts, the chain continues across each handover (`01` §6, `19` §10.1), using the previous host's imported key log.

**Readable view:** a timeline generated from the bundle (which rule allowed or refused, who acted at each step, where authority and reality diverged), labelled as a rendering.

---

## 2. The divergence finding

Every pack ends by naming, for each disputed payment, **exactly one primary** category (the first step where authority and reality diverged) and **any contributing** categories (later steps that also diverged, in order). For example, a merchant substitutes an item (`merchant_divergence`, primary) that a too-loose payer rule then let through (`rule_permitted_wrongly`, contributing). It names steps and roles, **never a liability amount or a share of one**.

```
divergence {primary: category, contributing: [category], step_refs: [record ids]}
```

| Category | Points to |
|---|---|
| `rule_permitted_wrongly` | the rule's author (payer or operator) |
| `unmodelled_dimension` | operator / infrastructure gap |
| `evaluator_fault` (replay disagrees) | infrastructure |
| `poor_proposal_within_rules` | agent provider (quality, not breach) |
| `payer_authorised` (including override) | payer's authorisation |
| `fact_misreported` | the party that attested the fact (`07` §6) |
| `merchant_divergence` | merchant |
| `rail_divergence` | rail / bank |
| `external` (no ledger record) | not through the layer |
| `no_divergence` | everything matched |

---

## 3. Dispute routes and formats

- A pack can be shaped for the rail profile's `dispute_route` (for example a jurisdiction's online dispute resolution process, or card chargebacks).
- Where applicable, the relevant parts export in **AP2 v0.2 dispute-evidence form** (mandates plus receipts), so AP2-aware parties can verify them with their own tools.

---

## 4. Disclosure

**Role-scoped by default:**

| Requester | Default scope |
|---|---|
| Payer | Everything about their own payments |
| Operator | Its Tier 0, its receipts, decisions made under them |
| Merchant | Proof the order was authorised under a valid grant; no other rules, spend or personal data |
| Rail / bank dispute desk | Authorisation, amount, execution, settlement for the disputed payment |
| Regulator / court | Per lawful demand |

- Other parties' data appears only as **commitments** (hashes): verifiable existence and application, contents undisclosed.
- **Fuller disclosure** requires payer consent or a lawful demand; the basis is recorded.
- **Every release is logged:** `release_id, requester_role, requester_ref, scope, basis (default|consent|lawful_demand), released_at, pack_hash, signature`.

---

## 5. Retention and erasure

- On a payer's deletion request, everything not needed as evidence is **crypto-shredded immediately**.
- The minimum evidence for payments still within `evidence.retention`, or tied to an open dispute or finding, is kept under a **retention hold**: evidence use only, legal basis recorded, payer told what is held, why, and until when.
- At the end of the period, or when the dispute closes, held keys are **shredded automatically** and the shred recorded. The ledger continues to verify; commitments can no longer be opened.

---

## 6. Tests

- The verifier rejects any tampered record, reordered chain or altered signature.
- A pack's decision reproduces; if not, the verifier reports it, and the finding is `evaluator_fault`.
- A merchant-scoped pack never contains the payer's other rules, spend or personal data.
- Every release appears in the disclosure log.
- After retention, held keys are shredded and the ledger still verifies.
- Every pack names exactly one primary divergence category per disputed payment, and every contributing category points to a recorded step.
- `no_divergence` is never combined with any other category.

---

## 7. Decisions

| # | Decision |
|---|---|
| D1 | The evidence is a machine-verifiable bundle with an open verifier; the readable view is a rendering; AP2-aligned |
| D2 | Role-scoped disclosure; others' data as commitments; fuller disclosure by consent or lawful demand; releases logged |
| D3 | Retention hold for minimum evidence, then automatic shred at `evidence.retention` or dispute close |
| D4 | One primary divergence plus ordered contributing categories; steps and roles, never liability (approved 9 Oct) |
