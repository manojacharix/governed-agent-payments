# United Kingdom

**Status:** reference notes; mapping `status: reference`. Sources: `../research/23_external_protocols_review.md` (Bratby), and an earlier UK PSR 2017 research pack (not in this repository). Claims marked [M] are from memory, not re-verified.

## Rail profiles (reference)

| Rail | authentication | Notes |
|---|---|---|
| Open Banking VRP | human_at_setup (mandate) | Max per payment, per period, expiry; revocable at the bank. Closest national construct to a spoke [M] |
| Cards | SCA, with exemptions (low value, merchant-initiated under mandate) [M] | Dispute route: chargeback |
| Faster Payments | human_per_payment | APP fraud reimbursement rules [M] |

## Law relevant to the mapping

- **Consent:** PSRs 2017 reg 67 (authorised only with payer consent); reg 76 (refund of unauthorised transactions). Agent discretion over timing, amount and payee is legally uncertain; tightly bounded mandates are defensible (Bratby, 2026).
- **Regulators:** FCA reviewing agentic payments (25 Mar 2026); PSRs replacement not expected before 2028.
- **Data:** UK GDPR: allergy and health data is special category (Article 9), so item-attribute rules involving health data need the stricter data class.
