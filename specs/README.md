# Governed Agent Payments: Infrastructure

**What this folder is:** the infrastructure only. No merchant, device, app, currency amount or threshold value appears in these specs. Anything that is a preference is a **named policy parameter** (`policy_parameters.md`) set by whoever deploys the infrastructure. Products built on the layer live in their own repositories.

**What the infrastructure does:** when software (an AI agent) spends a payer's money at a merchant, it records what the payer authorised, enforces it before money moves, and makes every decision provable to a party that does not trust the operator.

**Scope: merchant commerce** (decided 9 Oct 2026). An agent pays a merchant for an order of goods or services: online and quick commerce, food, travel, software subscriptions, B2B procurement. Every payment has a merchant, an order to check against, and a merchant record to reconcile with. Out of scope for now: bill payments, transfers to people, government payments and taxes, investments, taking on credit, cross-border remittances, stored-value top-ups and gift cards, donations and inbound money. Item, substitution, item-attribute, destination and extras dimensions (`01` §3) are therefore core, not optional.

---

## Four layers

| Layer | Set by | Changes per deployment? | Where |
|---|---|---|---|
| **1. Invariants** | The infrastructure | **Never.** These are the guarantees | This folder, below and in each spec |
| **2. Mechanisms + policy parameters** | Operator (Tier 0) and payer (Tier 1) | Values yes; mechanisms no | This folder + `policy_parameters.md` |
| **3. Jurisdiction mappings** | Versioned data per country | Per jurisdiction | `../jurisdictions/` (notes now; machine-readable mappings later) |
| **4. Product** | A product built on the infrastructure | Entirely | Separate repositories, never this one |

The only trade-offs made in this folder are **structural** (determinism over flexibility, double-entry over counters, conservatism under uncertainty), and they are the same for every adopter.

---

## Parties

| Party | Role |
|---|---|
| **Operator** | Deploys the infrastructure; sets its Tier 0 floor; the customer |
| **Payer** | Person or organisation whose money it is; owns the wheel; the protected party |
| **Regulator** | The acceptance test: can a rogue agent be controlled, and a dispute resolved? |
| **Merchant** | Checks payments against the wheel; never reads it |
| **Agent provider** | Answers for proposal quality; identified by origin |

---

## Invariants (apply to every deployment; not configurable)

**Authority**
1. **Default deny.** Anything not granted is refused; any attribute an agent varies that no rule dimension models is refused.
2. **Every proposal cites the spoke it acts under.** No citation, or a spoke not bound to the caller, is refused; a spoke is never inferred.
3. **Authority only narrows**, except when the payer widens it through payer authentication on a Trusted Surface.
4. **Layers intersect:** operator Tier 0 ∩ payer hub ∩ spoke chain. Changes cascade down, never up.
5. **Strictest approval mode wins** across every layer, rail profile and fact.
6. **Operators may only suspend** a payer's wheel or spoke, with a stated reason and payer notification; never edit or widen.
7. **Agents never authorise.** Only the payer, on a Trusted Surface registered to them, authorises; agents and MCP clients may only propose or hand off.

**Decision**
8. **Capture, freeze, decide.** `evaluate()` is a pure function of frozen facts, authority versions and injected time.
9. **Uncertain inputs only add caution.** Only verified sources can clear a check.
10. **Approval binds to exactly what was shown** (hash of the presented cart, total and rail). Any change after approval means re-present.
11. **One request, at most one order** (`intent_id`; layer-computed IDs for standing-instruction slots).
12. **Overrides apply only to payer-authored clauses**, one payment at a time, under payer authentication; operator and jurisdiction clauses are never overridable.
13. **Every failing clause is reported, attributed to its author.**

**Money**
14. **Non-custodial.** The infrastructure never holds or moves funds.
15. **Double-entry:** `available + held + spent = limit` per account set and window, always. Balances are derived from postings.
16. **Hold before execution**; unknown outcomes keep their hold; only definitive signals move balances; agent claims never do.
17. **Refunds correct the original window only.**

**Rails**
18. **The core never names a rail**; it reads capability profiles.
19. **The rail is part of what is approved**; never switched without a new approval; the safest permitted rail is the default.
20. **No retry without proving no order exists**, and only where the rail profile marks the error safe.

**Records and parties**
21. **Append-only, hash-chained, signed records; no personal data in the ledger.**
22. **Verify, don't see.** Each party verifies what concerns it; no party reads another's rules or spending.
23. **Attribute, don't adjudicate.** The record pinpoints where authority and reality diverged; liability is for the dispute process.
24. **Jurisdiction-free core.** No currency, rail or regulator names in the core; per-currency budgets; no FX inside `evaluate()`.

**Identity** (`07`, approved 9 Oct 2026)
25. **Identity is a key, not a name.** Callers, surfaces, hosts and operators are known only by enrolled keys; self-declared names are recorded as claimed.
26. **Only the payer enrols** callers and Trusted Surfaces, on a Trusted Surface; recovery never widens authority.
27. **Keys are never deleted from the record.** Rotation, revocation and compromise are logged; signatures after a compromise are flagged, not erased.
28. **Every fact says who vouches for it**, and evidence never claims more than that attestation supports.

---

## Specs

| # | Problem | Status |
|---|---|---|
| `01_authority.md` | What may an agent do? (the wheel) | Reviewed 9 Oct |
| `02_decision_engine.md` | How does one proposal become one decision? | Reviewed 9 Oct |
| `03_money_in_flight.md` | How do limits stay true between allowed and paid? | Reviewed 9 Oct |
| `04_rails.md` | How does a decision execute across payment methods? | Reviewed 9 Oct |
| `05_reconciliation.md` | Did what was decided actually happen? | Reviewed 9 Oct |
| `06_evidence.md` | Who is right in a dispute? | Reviewed 9 Oct |
| `07_identity_and_keys.md` | Who signed this, and can they be trusted? | Approved 9 Oct |
| `policy_parameters.md` | Every configurable value | Maintained with each spec |

Background: `../research/23_external_protocols_review.md` (AP2 v0.2, ACP). Jurisdiction-specific material (layer 3) is in `../jurisdictions/`.
