# How this layer maps to SAFR

**SAFR** (Safeguards for Agentic Finance at Runtime) is an industry white paper developed under the Monetary Authority of Singapore's BuildFin.ai initiative (v1.0, 3 July 2026; v1.1, 9 October 2026). It proposes a runtime governance checkpoint between an AI agent's proposed action and its execution, built from four components: Agent Identity, Controls Repository, Disposition Engine and Audit Log, exchanging a Governance Envelope.

SAFR states that it "does not constitute regulatory guidance or supervisory expectations". This repository is not affiliated with MAS or the SAFR contributors, and nothing here claims conformance to SAFR or approval by any regulator. The mapping below is a design comparison, so readers who know one can find their way around the other.

Scope differs. SAFR covers any consequential agent action in financial services (payments, trading, credit, claims, filings). This layer covers **merchant commerce only**: an agent paying a merchant for an order (`specs/README.md`, "Scope").

Status: `v0.1.0`, not in production. Every merchant is a test fake and no real money moves (see the README).

---

## Component by component

| SAFR | This layer | Where |
|---|---|---|
| **Governance Envelope**: action, action trace, context metadata (identity, mandate, state, policy) | A **proposal** citing its spoke, with `intent_id`, nonce and timestamp under the caller's proof; then **Capture**: a fresh cart from the merchant adapter, attribute sources, agent confidence, account facts and time, frozen and hashed | `specs/02_decision_engine.md` §1, `specs/07_identity_and_keys.md` §4.2 |
| **Action trace** checked against records captured independently of the agent | The layer does not take the agent's description of the order at all: the cart is re-fetched from the merchant by the host, and every fact records who vouches for it. Only verified sources can clear a check | Invariants 9 and 28, `specs/07_identity_and_keys.md` §6 |
| **Agent Identity**: bind each action to a registered agent, verified against its registry entry | **Callers** are known only by credentials the payer enrolled on a Trusted Surface. Self-declared names are recorded, never trusted. Assurance (`key_bound`, `session_bound`) becomes a verified fact rules can act on; `claimed` is never accepted for a proposal | Invariants 25 and 26, `specs/07_identity_and_keys.md` §4 |
| **Controls Repository**: organisational policy, regulatory requirements, product rules, user-provided mandates | Three layers that intersect: operator **Tier 0** ∩ payer **wheel** ∩ **spoke** chain, plus **jurisdiction mappings** as versioned data. 14 rule dimensions. Default deny | Invariants 1 and 4, `specs/01_authority.md`, `jurisdictions/`, `mappings/` |
| **Mandate**: delegated authority in machine-readable form; the agent cannot extend it | A **spoke**. Every proposal cites one; it is never inferred; authority only narrows unless the payer widens it on a Trusted Surface | Invariants 2 and 3, `specs/01_authority.md` §2 |
| **Disposition Engine**: deterministic evaluation, one of four outcomes | `evaluate()`: a pure function of frozen facts, authority versions and injected time, returning a signed Determination listing every failing clause and its author | Invariants 8 and 13, `packages/core/src/gap_core/evaluate.py` |
| **Audit Log**: tamper-evident, append-only record of every decision | Append-only, hash-chained, signed records; key log that never deletes keys; external anchoring of chain heads, with payer devices as witnesses; **evidence bundles** checked by the **open verifier**, which reproduces each decision from the disclosed facts | Invariants 21, 22 and 27, `specs/06_evidence.md`, `packages/verify` |

## The four outcomes

| SAFR outcome | Closest here | Difference |
|---|---|---|
| **Deny** | `refuse` (every failing clause, attributed) or `no_authority` | Overrides exist only for payer-authored clauses, one payment at a time, under payer authentication; operator and jurisdiction clauses are never overridable |
| **Escalate** | `allow` with approval mode `explicit_tap`, or `clarify` | The human is the **payer** on their own Trusted Surface, not a reviewer at the institution. Agents never authorise (invariant 7). An unanswered request resolves as `expired` |
| **Auto-Execute** | `allow` with approval mode `silent_with_notice` | Only for a spoke with a standing-instruction binding, and only when `approval.silent.preconditions` are met by verified facts. Strictest approval mode wins across every layer |
| **Observe** | **No equivalent** | An action either needs the payer or it does not; there is no "proceed but flag for review" outcome. Near misses are not recorded |

## Two places the layers differ on purpose

**Agent confidence can only add caution.** SAFR's evidence-quality control routes an action to human review when the agent's *stated* confidence is below a threshold, which makes a minimum confidence one of the conditions for autonomous execution. Here, agent confidence is captured as an advisory input: below `decision.clarify_threshold` it adds `clarify` (step 4); above it, it changes nothing. It can never produce `allow` or `silent_with_notice` on its own. This follows invariant 9: uncertain inputs only add caution, and only verified sources can clear a check.

**Every step is decided again, and approval binds to what was shown.** SAFR says an Auto-Execute at one step carries no authority into the next. Here, Stage 2 re-fetches the cart at authorisation and re-runs steps 1 to 7 on fresh facts; an approval binds to the hash of the presented cart, total and rail, and any change means re-presenting (invariant 10).

## What this layer adds after execution

SAFR stops at execution and hands over to the rail's own rules. This layer keeps going: holds before execution and double-entry money in flight (`specs/03_money_in_flight.md`), at most one order per request and no retry without proving no order exists (invariants 11 and 20), and reconciliation against merchant and bank records (`specs/05_reconciliation.md`). It is non-custodial throughout (invariant 14).

## What SAFR covers that this layer does not

- An **Observe** outcome and structured near-miss records.
- Institutional **escalation operations**: reviewer capacity, review timeouts that default to block or to a senior reviewer, reviewer authority.
- Choosing the **authoritative registry** when an agent is registered in several (an institution's, a payment network's, an inter-institutional directory).
- Actions other than paying a merchant: trading, credit, claims, filings, transfers, bill payments.

## Source

MAS, *Safeguards for Agentic Finance at Runtime*, white paper v1.1, 9 October 2026: https://www.mas.gov.sg/publications/monographs-or-information-paper/2026/safeguards-for-agentic-finance-at-runtime

This file is licensed with the rest of the documentation outside `specs/` (PolyForm Noncommercial 1.0.0; see `LICENSING.md`).
