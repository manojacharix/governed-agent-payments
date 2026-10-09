# External Review: Bratby (UK consent law), ACP, AP2 v0.2, awesome-agentic-payments

**Compiled:** 2026-09-25, from four shared links. ACP and AP2 were read from their repositories (cloned today), not summaries.
**Why it matters:** checks the authority wheel (`../specs/01_authority.md`, originally doc 22) against the two main agent-payment protocols and the UK legal analysis, before Problem 2.

---

## 1. Bratby Law: "Agentic AI payments and consent regulation" (UK)

Rob Bratby, Bratby Law; published 28 March 2026, updated June 2026.

- **The core problem, stated from the law:** PSRs 2017 **reg 67** says a payment is authorised only if the payer consented. "If the payer sets broad parameters … and the AI agent determines the timing, amount and recipient, has the payer consented to the specific transaction?"
- **Reg 76:** for an unauthorised transaction the payer's PSP must "refund the amount immediately", but nothing says who bears the loss when an agent exceeds its scope: PISP, bank or AI provider.
- **SCA** "was not designed for agentic AI payments". **VRPs** may fit agents working within "tightly defined parameters"; wider discretion creates legal uncertainty.
- **Regulators:** the FCA said on 25 March 2026 it "will consider whether regulation needs to change" (sandbox, AI sprints, framework review due end-2026); the Mills Review reports summer 2026; HM Treasury payments consultation; a PSRs replacement "not expected before 2028".
- **His advice to firms now:** "map your consent architecture against regulation 67", **define the scope of the agent's authority**, and **allocate liability in customer agreements**.

**Read-across:** this is the UK version of what RBI said when it held back UAP (`21`). The wheel is literally the "defined scope of agent authority" Bratby tells firms to build, and the archived UK work (reg 67/76 mapping) plus D6 (UK mapping) already point at it. His point that tightly bounded parameters are defensible and broad discretion is not supports the spec's default deny, cited spokes and silence only on exact repeats.

---

## 2. Agentic Commerce Protocol (ACP), OpenAI and Stripe

Beta; latest stable spec **2026-04-17** (date-versioned). Covers checkout, cart, feeds, orders, delegated authentication, **delegated payment**, capability negotiation, MCP.

**Delegated payment, from `openapi.delegate_payment.yaml`:** the agent's platform tokenises a payment credential for the merchant's PSP under an **Allowance**:

| Allowance field | Meaning |
|---|---|
| `reason` | enum, **only `one_time`** |
| `max_amount` | minor units |
| `currency` | ISO-4217 |
| `checkout_session_id` | this one checkout |
| `merchant_id` | this one merchant |
| `expires_at` | expiry |

"Exactly one credential type is currently supported: **card**." There is also an "intent traces" RFC (why an agent abandoned a checkout, for merchants).

**Read-across:** ACP is **merchant- and checkout-side**: it makes one purchase at one merchant safe. It has no payer-side standing authority, no cross-merchant budget, no approval-mode logic, no item or substitution rules, and no UPI. An ACP allowance is what the wheel would **emit for one approved payment**: a closed, one-time grant derived from a spoke after `evaluate()` allows it. Complement, not competitor.

---

## 3. Agent Payments Protocol (AP2) v0.2, Google

**Version 0.2.0, 28 April 2026.** Positioned as "a security feature within a Commerce Protocol", compatible with **UCP (Universal Commerce Protocol)**. v0.1's "Intent / Cart / Payment Mandate" terms are replaced.

**Concepts (from `docs/ap2/specification.md`):**
- **Two mandate types:** a **Checkout Mandate** (proof the agent may buy *this* checkout; verified by the merchant) and a **Payment Mandate** (proof the agent may pay for it; verified by credential provider, network and processor), bound together by a hash.
- **Open vs closed mandates.** *Human present:* the user sees the closed checkout on a **Trusted Surface** and signs. *Human not present:* the user signs **open** mandates (constraints) on the Trusted Surface; the agent later signs a **closed** mandate with its own key (bound by a `cnf` claim), and presents both.
- **Selective disclosure (SD-JWT VCs):** "Only constraints relevant to the closed Mandates are shared with the verifier."
- **One at a time:** agents "MUST NOT present any subsequent open Payment or Checkout Mandates without receiving a rejection receipt from the previous one."
- **Receipts** from merchant and processor; **dispute evidence** = checkout mandate + receipt + payment mandate + receipt, "a non-repudiable picture"; retention and dispute process out of scope.
- **Constraint types defined:** `checkout.allowed_merchants`, `checkout.line_items`, `payment.allowed_payees`, `payment.allowed_payment_instruments`, `payment.allowed_pisps`, `payment.amount_range`, `payment.budget`, `payment.agent_recurrence`, `payment.reference`, `payment.execution_date`. **Extension point:** new constraint types need a unique `type`, a schema, and an **evaluation algorithm**.
- **Payment methods:** cards today; x402 (crypto) sample; roadmap names "real-time bank transfers (e.g., **UPI** and PIX)".

**Read-across, the most important of the four:**

| AP2 v0.2 | Wheel (`../specs/01_authority.md`) |
|---|---|
| Trusted Surface | The payer's Trusted Surface (`../specs/07_identity_and_keys.md` §3) |
| Open mandate (constraints, user-signed) | A **spoke** |
| Closed mandate (agent-signed) | An agent **proposal citing its spoke** |
| Selective disclosure to verifiers | **Verify, don't see** (D4 receipts) |
| One open mandate at a time | One hold at a time per wheel (`../specs/03_money_in_flight.md`) |
| Checkout/Payment receipts, dispute evidence | `AuthorityReceipt`, ledger, evidence pack (`../specs/06_evidence.md`) |
| Constraint types + evaluation algorithm | The **14-dimension catalogue**, evaluated by `evaluate()` |

**Where the wheel goes beyond AP2 v0.2:**
- **A hub across mandates:** AP2 budgets live per mandate; nothing binds several mandates (several agents, merchants) to one payer-wide limit.
- **Dimensions AP2 does not define:** substitution, ingredients, velocity beyond recurrence, time-of-day and quiet hours, approval mode, delivery address, conditions on facts, extras. Each could be published as an **AP2 constraint type** using its extension point.
- **Operator floor (Tier 0)** and payer rules stacking by intersection.
- **Lifecycle and attribution:** who narrowed or suspended what, and why.
- **India rails and mapping** (UPI, Reserve Pay, UPI Circle, RBI/NPCI rules).

**Design consequence:** the wheel should be able to **issue AP2 v0.2 open mandates from spokes** and **evaluate closed mandates** against them. That makes the layer an AP2-compatible Trusted Surface plus constraint evaluator, and its extra dimensions a candidate contribution to AP2's constraint registry.

---

## 4. awesome-agentic-payments (Bitrefill)

A curated list: commerce protocols (ACP, AP2, UCP), crypto rails (x402 and others), fiat rails (Visa, Mastercard agent tokens, Stripe Shared Payment Tokens), Ethereum ERCs, ecosystem.

**Spend-control and governance entries are protocol-level**, not payer-side engines:
- **ERC-7710 / ERC-7715:** spending-policy and session-key standards for bounded agent permissions (on-chain).
- **ERC-8196:** "policy-bound transaction execution" for agent wallets (on-chain).
- **Stripe Shared Payment Tokens, Mastercard Agentic Tokens:** scoped, time-limited credentials.
- **OpenID Agent Identity Claims** (IETF draft): agent id, owner, capabilities, spend limit as identity claims.
- **ERC-8004, ERC-8126, Solana Agent Registry:** agent identity, reputation, verification registries.

**No India or UPI entries.** The list itself notes spend controls appear "primarily as protocol-level standards … rather than standalone audit or compliance platforms".

**Read-across:** confirms the gap. Everyone scopes **one credential, one token or one wallet**; nobody offers a **payer-side, cross-merchant, cross-rail authority and evidence layer**, and nobody covers India. OpenID Agent Identity Claims is worth tracking for caller identity (D1 bindings, order origin).

---

## 5. What changes

1. **Authority spec §8 (was doc 22 §9) terminology:** replace AP2 v0.1 "intent mandate / cart mandate" with v0.2 **open / closed Checkout and Payment Mandates**, and add ACP's one-time Allowance as an emitted artifact. (Done in this commit.)
2. **Rails (`04`) should include "protocol adapters"**: emit AP2 open mandates and ACP allowances from spokes; accept AP2 closed mandates as proposals.
3. **Evidence (`06`)** should align with AP2's dispute evidence (mandates + receipts) so packs are verifiable by AP2-aware parties.
4. **Difference:** AP2 and ACP secure *a* purchase; the wheel governs the *payer's authority across all of them*. India/UPI coverage is empty in the global ecosystem.

---

## 6. Sources

- [Bratby Law: Agentic AI payments and consent regulation](https://bratby.law/agentic-ai-payments-consent-regulation/)
- [Agentic Commerce Protocol repository](https://github.com/agentic-commerce-protocol/agentic-commerce-protocol) (spec `2026-04-17`, `openapi.delegate_payment.yaml`)
- [AP2 repository](https://github.com/google-agentic-commerce/AP2) (v0.2.0, `docs/ap2/specification.md`, `payment_mandate.md`, `checkout_mandate.md`)
- [awesome-agentic-payments](https://github.com/bitrefill/awesome-agentic-payments)
