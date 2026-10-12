# India

**Status:** notes, not yet a machine-readable mapping. Sources: `../research/18_silence_and_approval_rules.md`, `../research/21_npci_uap_deep_dive.md`.

## Rail profiles (candidates)

| Rail | authentication | placement | amount_binding | Notes |
|---|---|---|---|---|
| UPI intent | human_per_payment (UPI PIN) | pending_then_confirm | bound_at_rail (UPI app shows payee and amount at PIN) | Dispute route: UPI Online Dispute Resolution; RBI 2019 turnaround-time and compensation rules |
| Merchant wallet (closed or bank-issued PPI) | none (as observed on at least one merchant) | immediate | not_bound | Authentication basis under RBI's 2025 directions unclear per merchant; treat as `none` |
| Cash on delivery | none | immediate | not_bound (amount seen only at the door) | Places an order with no payment authentication |
| UPI Reserve Pay (NPCI OC-228, 8 Oct 2025) | human_at_setup | immediate per debit | not_bound per debit (bounded only by the block) | Block ≤ ₹10,000, ≤ 90 days, one block per merchant per mobile number; timeout = decline; ≤ 3 retries in 24h for timeouts; issuer notices on create, modify, debit, revoke, expiry |
| UAP (Unified Agent Protocol) | TBD | TBD | TBD | Held back at GFF 2026 pending RBI clearance; placeholder |

## National schemes → wheel constructs

| Scheme | Wheel equivalent |
|---|---|
| NPCI UAP power of attorney / logbook / agent identity (not launched) | Caller-bound spoke / ledger export / caller binding + origin |
| UPI Reserve Pay block | Merchant-bound spoke within the limits above |
| UPI Circle partial delegation (OC-201) | `explicit_tap` spoke: agent proposes, payer completes with PIN |
| UPI Circle full delegation | Capped spoke (₹5,000 per transaction, ₹15,000 per month, as reported) |
| RBI E-mandate Framework 2026 | Standing-instruction spoke; AFA at registration; pre-debit notice ≥ 24h with opt-out; post-debit notice |

## Law relevant to the mapping

- **Authentication:** RBI Authentication Mechanisms Directions 2025 (in force 1 Apr 2026): two factors, one dynamic or provable (biometric qualifies); exemptions include e-mandate recurring transactions.
- **Consent to purchase:** Consumer Protection (E-Commerce) Rules 2020, r. 4(9): explicit and affirmative; never recorded automatically. Silent execution only as a standing instruction created by an explicit act.
- **Dark patterns:** CCPA Guidelines 2023 (false urgency, basket sneaking, subscription trap, drip pricing).
- **Data:** DPDP Act 2023 (no special category); Consent Manager framework from 13 Nov 2026; remaining obligations May 2027.
- **Agent payments:** NPCI chairman (GFF, 10 Sep 2026): AI may recommend; authentication and settlement follow deterministic auditable rules. CERT-In: human-in-the-loop above thresholds (thresholds unpublished) → silent-approval limit is a versioned mapping value, currently unset.
- **Food labelling (relevant to item-attribute rules):** FSSAI Labelling and Display Regulations 2020 (allergen declaration, exempt where the product is itself the allergen); FSSAI 2022 e-commerce direction to display ingredients and allergens.

## To decide when building the mapping

- Authentication-strength scale (e.g. none < possession < possession + PIN or biometric).
- ~~`evidence.retention`~~ **Decided 12 Oct: 3 years by default; 5 years when the operator is a regulated reporting entity.** Basis (from memory, to verify): Consumer Protection Act 2019 complaints within 2 years of the cause of action; Limitation Act 1963, 3 years for most contract claims; PMLA record-keeping rules, 5 years for reporting entities such as banks and payment system operators; DPDP Act pushes toward the shortest defensible period. **Confirm with a lawyer before real payers.**
