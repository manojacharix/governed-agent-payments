# 18 · Silence, Approval and Consent: Indian Rules

**Compiled:** 2026-09-24; rewritten infrastructure-only 9 Oct 2026.
**Question:** what do Indian rules, payment-network practice and industry precedent say about (a) explicit approval before an agent pays, and (b) a payment proceeding when the payer stays silent?
**Method:** web research on the compile date. Some primary sources were unreachable, so several items rest on law-firm and press summaries. Tags: **[P]** primary or official text quoted by a reliable secondary, **[S]** secondary reporting. **Research, not legal advice;** the questions in §7 are for a lawyer.
**Later update:** NPCI's Unified Agent Protocol status is superseded by `21`.

---

## 1. Summary

1. **Explicit approval before payment is what every rule and precedent expects.** Indian consumer law, RBI's authentication rules, NPCI's agentic pilots and the global agent-payment protocols all assume it. This is the infrastructure's default (`explicit_tap`, `../specs/01_authority.md`).
2. **Silence is not consent to a purchase in Indian consumer law.** The E-Commerce Rules 2020 require purchase consent to be "explicit and affirmative" and forbid recording it "automatically". Silent execution is defensible only when **the consent is an earlier explicit act** (a standing instruction created by the payer) and the silent step is an opt-out of one execution. That is how RBI's e-mandate framework works, and it is how the specs define a standing-instruction spoke.
3. **The regulated version of "proceed unless you object" uses 24 hours' notice.** RBI's E-mandate Framework 2026 allows recurring debits up to ₹15,000 without re-authentication only after additional-factor authentication at registration, a pre-debit notice **at least 24 hours** before each debit with an opt-out, and a notification after each debit.
4. **India's agentic-payments direction is "pre-authorised, bounded, revocable".** NPCI's pilots use UPI Reserve Pay: the payer authorises a per-merchant limit once, later debits need no PIN, and the agent completes the purchase "upon confirmation".
5. **Countdown timers that end in a purchase are a live dark-pattern concern.** CCPA's 2023 guidelines name "false urgency", "basket sneaking" and "subscription trap"; e-commerce platforms filed self-audit declarations under a June 2025 advisory.

**Implication for the infrastructure:** silence exists only through a standing instruction with the e-mandate protections, which the specs already require: explicit creation at `auth.strength.create` with terms hashed, a required expiry, notice before execution (`approval.silent.notice_duration`), a notice after, revocation at any time, and a ceiling (`approval.silent.max_amount`).

---

## 2. Consumer law: consent to purchase

### Consumer Protection (E-Commerce) Rules, 2020, Rule 4(9) [P]

> "Every e-commerce entity shall only record the consent of a consumer for the purchase of any good or service offered on its platform where such consent is expressed through an explicit and affirmative action, and no such entity shall record such consent automatically, including in the form of pre-ticked checkboxes."

The wording follows GDPR Recital 32: "Silence, pre-ticked boxes or inactivity should not therefore constitute consent."

**For the infrastructure:**
- A payment that proceeds because the payer did nothing is, on its face, consent recorded automatically. Treating the silent window itself as consent conflicts with Rule 4(9).
- The defensible structure: consent is the **explicit act that created the standing instruction**, with the exact terms shown and hashed (`WheelEvent.presented_hash`). Each silent execution is recorded as `proceeded_on_silence`, never `approved` (`../specs/02_decision_engine.md` §2).
- **Open:** whether an operator of this layer is itself an "e-commerce entity" under the Rules (§7).

### CCPA Guidelines for Prevention and Regulation of Dark Patterns, 2023 [P/S]

In force 30 November 2023; applies to platforms systematically offering goods or services in India, advertisers and sellers. Thirteen named patterns, including false urgency, basket sneaking, confirm shaming, forced action, subscription trap, interface interference, drip pricing and nagging. On 5 June 2025 CCPA required e-commerce platforms to self-audit within three months; 26 platforms filed self-declarations [S].

**For the infrastructure:**
- **False urgency:** where a timer ends in a purchase, it must never be designed to push the payer to pay. Expiry of an `explicit_tap` proposal resolves as `expired` (no payment), which is safe by construction.
- **Basket sneaking:** `Extras` are denied unless a rule allows (`01` §3, dimension 13).
- **Subscription trap:** standing instructions require `valid_until` and are revocable at any time.
- **Drip pricing:** the presented hash covers fees and total (`02` §1), so the payer approves the full payable amount.

### DPDP Act 2023, s.6 and DPDP Rules 2025 [P/S]

Consent to process personal data must be "free, specific, informed, unconditional and unambiguous with a clear affirmative action". This governs data consent, not purchase consent; it applies to the profile store. Phasing [S]: Data Protection Board from November 2025; **Consent Manager framework from 13 November 2026**; remaining obligations from May 2027.

---

## 3. RBI: authentication and recurring debits

### Authentication Mechanisms for Digital Payment Transactions Directions, 2025 [P via Khaitan & Co, KPMG]

- Issued 25 September 2025; **in force 1 April 2026** for domestic transactions.
- At least **two distinct factors**; for non-card-present payments at least one must be dynamic or "capable of being proven (eg, biometric authentication)".
- Exemptions include small-value contactless card payments, **recurring transactions under the e-mandate framework**, and gift PPIs.
- Applies to payment system providers and participants, banks and non-banks.

**For the infrastructure:** a rail's own authentication is the rail's obligation. The layer's Trusted Surface authentication is its own control; it does not satisfy anyone's RBI obligation, and documentation must not claim it does. A rail whose authentication basis is unclear is profiled as `authentication = none` (`../jurisdictions/india.md`).

### Digital Payments: E-mandate Framework, 2026 [S, quoting the framework]

- Registration "only after successful validation of additional factor of authentication (AFA)".
- Recurring debits up to **₹15,000** without AFA (₹1,00,000 for insurance premiums, mutual funds, credit card bills).
- Pre-debit notification **at least 24 hours** before each debit; opt-out of any transaction or the mandate; post-debit notification; modify or withdraw at any time.

**The closest regulated precedent for silent execution**, mapped to the specs:

| E-mandate requirement | Infrastructure construct |
|---|---|
| Registration with AFA | Standing-instruction spoke created at `auth.strength.create`, terms hashed |
| Scoped mandate | Spoke rules across the 14 dimensions; `instruction.price_tolerance` |
| Expiry | `valid_until` required; `instruction.max_validity` |
| Pre-debit notice with opt-out | `approval.silent.notice_duration`; delivery confirmed as a precondition fact |
| Post-debit notification | `notifications.channels` |
| Withdraw any time | Payer revoke; system narrowing on pre-agreed triggers |

Deployments that want to match the e-mandate norm set `approval.silent.notice_duration` near 24 hours for scheduled repeats.

### Prepaid instruments [S]

The draft Master Direction on PPIs, 2026 (comments closed 22 May 2026) replaces the 2021 framework. Small PPIs: ₹10,000 maximum balance and monthly debit. Closed-system PPIs "are not regulated or supervised by RBI".

---

## 4. NPCI: how agentic UPI payments are being built

- **UPI Reserve Pay** [P, see `21`]: the payer blocks funds once for a merchant; later debits need no PIN. Up to ₹10,000 and 90 days; one block per merchant.
- **UPI Circle** [P/S, see `21`]: partial delegation (the secondary initiates, the primary completes with PIN) and full delegation (₹5,000 per transaction, ₹15,000 per month). Designed for human secondary users; treat as an analogy for agents until NPCI says otherwise.
- **Agentic pilots** [P/S]: Razorpay, NPCI and OpenAI (announced 30 October 2025) and Razorpay and NPCI on Claude (February 2026), both on Reserve Pay. Payers authorise a spending limit per merchant, the agent "presents options, and completes the transaction upon confirmation", with real-time visibility and one-tap revocation.

**For the infrastructure:** the Indian pattern is pre-authorise a bounded limit, then confirm each purchase, with revocation: the `explicit_tap` default. None of the pilots is described as proceeding on silence. Reference caps (UPI Circle ₹5,000 / ₹15,000, Reserve Pay ₹10,000, e-mandate ₹15,000) bound any sensible `approval.silent.max_amount`.

### What a rail-level agent scheme would not cover

If a scheme like UAP gives agents identity, a power of attorney and a logbook at the rail (`21`), these still sit above the rail by construction:
- **The basket.** The rail sees payee and amount, not items, quantities, substitutions or item attributes.
- **What the payer was shown.** A logbook records what the agent did; the layer records what the payer saw and approved.
- **Non-UPI and cross-rail spend.** Wallets, cards and cash on delivery; one budget across rails.
- **Operator floors.** Tier 0 stacked with payer rules.
- **The approval step before payment.**

Mappings to keep: spoke ↔ power of attorney; ledger ↔ logbook export; caller credential ↔ agent identity.

---

## 5. Global agent-payment protocols

- **AP2** [S, see `23` for v0.2]: an open mandate holds the conditions for human-not-present purchases, signed in advance; a closed mandate is the authorisation of a specific checkout.
- **Visa Intelligent Commerce / Mastercard Agent Pay** [S]: agent tokens bound to an agent, a merchant scope and a consent policy.

**Mapping:** an `explicit_tap` approval is a closed mandate; a standing-instruction spoke is an open mandate (`../specs/01_authority.md` §8).

---

## 6. Requirements this places on the infrastructure

All already in the specs; listed so a mapping reviewer can check them:
1. Consent is the creation of a standing instruction, never the silence (`01`, `02`).
2. Standing instructions expire and are revocable (`01` §2, §5).
3. Notice before each silent execution, with delivery as a precondition fact (`02`, `approval.silent.preconditions`).
4. Notice after each execution (`notifications.channels`).
5. The full payable total is part of what is approved (`02` §1, invariant 10).
6. Extras denied by default (`01` §3).
7. Silent ceiling is a versioned jurisdiction value, unset until a regulator publishes one (`../jurisdictions/india.md`).

---

## 7. Open questions

**For a lawyer, before any deployment with real payers:**
1. Is an operator of a layer that records purchase approvals an "e-commerce entity" under the E-Commerce Rules 2020, and does a standing instruction created by an explicit act satisfy Rule 4(9) for later silent executions?
2. Which notice durations before a silent execution avoid dark-pattern risk (false urgency, subscription trap)?

**Watch:** NPCI's UAP and agent registry (see `21`); Reserve Pay availability beyond pilots; the final PPI Master Direction 2026.

---

## 8. Sources

- [Lexology: RBI Authentication Mechanisms Directions 2025](https://www.lexology.com/library/detail.aspx?g=5481786f-8d45-48a6-97cd-a2b218f82d73)
- [Khaitan & Co: RBI Authentication Mechanisms Directions](https://www.khaitanco.com/thought-leadership/RBI-Authentication-Mechanisms-for-Digital-Payments-Transactions-Directions)
- [KPMG: RBI Authentication Mechanisms Directions 2025](https://kpmg.com/in/en/insights/2025/12/reserve-bank-of-india-rbi-authentication-mechanisms-for-digital-payment-transactions-directions-2025.html)
- [The Tribune: RBI tightens auto-debit rules, 24-hour prior alert](https://www.tribuneindia.com/news/additional-authentication/rbi-tightens-auto-debit-rules-24-hour-prior-alert-now-mandatory-for-recurring-payments)
- [ANI: RBI e-mandate 24-hour alert](https://www.aninews.in/news/business/rbi-tightens-auto-debit-rules-24-hour-prior-alert-now-mandatory-for-recurring-payments20260421202816/)
- [Mondaq: Consumer Protection (E-Commerce) Rules 2020](https://www.mondaq.com/india/dodd-frank-consumer-protection-act/995148/the-consumer-protection-e-commerce-rules-2020)
- [Lexology: E-Commerce Rules 2020, novelty and nuances](https://www.lexology.com/library/detail.aspx?g=3112c012-043e-4b0e-9674-fbf29ded0b1a)
- [SCC Online: CCPA Dark Patterns Guidelines 2023](https://www.scconline.com/blog/post/2023/12/04/ccpa-notifies-guidelines-for-prevention-and-regulation-of-dark-patterns-2023-legal-news/)
- [dpdpa.com: DPDP Act s.6](https://www.dpdpa.com/dpdpa2023/chapter-2/section6.html)
- [ComplianceHub: Consent Manager deadline 13 Nov 2026](https://compliancehub.wiki/india-dpdp-consent-manager-november-2026-phase-two-deadline-compliance/)
- [MediaNama: draft PPI Master Direction 2026](https://www.medianama.com/2026/04/223-rbi-prepaid-payment-instruments-rules-wallet-limits-escrow-norms/)
- [Razorpay newsroom: Razorpay, NPCI and OpenAI agentic payments](https://razorpay.com/newsroom/razorpay-npci-and-openai-come-together-to-launch-agentic-payments-ushering-in-ai-driven-commerce-at-national-scale/)
- [The Paypers: Razorpay and NPCI launch agentic payments on Claude](https://thepaypers.com/payments/news/razorpay-and-npci-launch-agentic-payments-on-claude)
- [Google Cloud: Announcing AP2](https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol)
- [AP2 documentation](https://ap2-protocol.org/)
- [Visa Intelligent Commerce](https://developer.visa.com/capabilities/visa-intelligent-commerce)
