# NPCI's Unified Agent Protocol: Deep Dive

**Compiled:** 2026-09-25
**Supersedes:** `18` §4 on UAP status and design, which was built on pre-GFF press reports and a third party's diagram; this document adds NPCI's own circulars and what happened at GFF.
**Method:** web research, AnySearch as a second provider, and NPCI circular OC-228 read from the scanned original. Tags: **[P]** primary (NPCI circular, on-record quote), **[S]** secondary reporting, **[U]** unconfirmed or conflicting.

---

## 1. Summary

1. **UAP did not launch.** It was due at Global Fintech Fest (Mumbai, 8–11 September 2026). NPCI told the industry late on the night of 10 September that it was holding the protocol back **pending RBI clearance** [S]. Business Standard (12 Sep) reports it "temporarily put on hold" while NPCI builds safeguards against liability risks and "rogue AI agents" [S]. No new date.
2. **The reason is the problem this project exists to solve.** Reported framing: "UPI's entire dispute-resolution machinery was built around a human tapping 'approve' on every transaction"; the open question is "how do you control a machine going rogue, and how does everyone involved get the information they need when something goes wrong" [S].
3. **NPCI's chairman stated this project's architecture on record.** Ajay Kumar Choudhary, NPCI's non-executive chairman, GFF, 10 Sep 2026 [P, quoted by MediaNama]:
   - "An artificial intelligence agent may work out what a user wants. It should not be the thing that approves the payment."
   - "Decision making and execution must remain separate."
   - "AI may recommend, but authentication and final settlement must follow deterministic auditable rules."
4. **The rail will not see the basket.** On UAP's design: "NPCI's role would remain limited to validating whether a payment request is genuine, similar to the current UPI system, **without visibility into what is being purchased**" [S, Business Standard via Outlook Business, 9 Jul 2026].
5. **The government's cyber agency points the same way.** CERT-In's Digital Threat Report 2025-26 recommends: "Mandate human-in-the-loop controls for agentic AI actions above defined financial thresholds, with full audit trails" [S]. Thresholds not published.
6. **What did ship at GFF:** AiNxt (NPCI's open-source agent-building platform), AtOM (agent orchestration producing "signed, machine-readable audit trails" for UPI integration and partner onboarding), MyUPI (consolidated view of UPI transactions and AutoPay mandates, with a "Safety Switch" to freeze debits), and UPI Tap & Pay (PIN-free NFC up to ₹5,000) [S].

**Bottom line for this project:** UAP is not a rail to build on yet. It is a **regulatory signal**: the regulator stopped it on exactly the gap this layer fills (evidence and control when no human approves each payment), and NPCI's chairman described this layer's architecture as the principle. The rails that *do* exist (Reserve Pay, UPI Circle) are documented below from NPCI's own circulars.

---

## 2. Timeline

| Date | Event | Source |
|---|---|---|
| 31 Jul 2024 | Single Block Multiple Debits introduced (NPCI/UPI/OC No. 200/2024-25) | [P] cited in OC-228 |
| 13 Aug 2024 | UPI Circle, delegated payments (NPCI/UPI/OC No. 201/2024-25) | [P/S] |
| 13 Aug 2025 | RBI's FREE-AI committee report (responsible AI in finance; 7 principles, 26 recommendations) | [P] |
| 8 Oct 2025 | OC-228: SBMD renamed **UPI Reserve Pay**, extended to all UPI funding sources | [P] |
| 30 Oct 2025 | Razorpay, NPCI and OpenAI announce agentic payments on ChatGPT (Reserve Pay) | [P, Razorpay] |
| Feb 2026 | Razorpay and NPCI launch agentic payments on Claude, with food and quick-commerce merchants | [S]; one source says Feb 2025 [U] |
| 21 Apr 2026 | RBI E-mandate Framework 2026 (24-hour pre-debit notice) | [S] |
| 8–9 Jul 2026 | Business Standard: NPCI developing UAP; register, verify, authorise agents; limits; accountability | [S] |
| Jul 2026 | CERT-In Digital Threat Report: human-in-the-loop above thresholds | [S] |
| 1 Sep 2026 | Reuters: UAP built, to be unveiled at GFF | [S] |
| 9 Sep 2026 | AiNxt and AtOM launched at GFF | [S] |
| 10 Sep 2026 | NPCI chairman: AI recommends, deterministic rules authorise; UAP held back that night | [P]/[S] |
| 12 Sep 2026 | Business Standard: UAP "temporarily on hold" as NPCI builds safeguards | [S] |

---

## 3. What UAP was designed to do (as reported)

- **Register and verify agents**, check an agent "is authorised to act on behalf of a user, define the limits of that authority and establish accountability" [S].
- **Built on UPI Circle and Reserve Pay**, "without changing the underlying payment rails" [S].
- **No per-transaction approval** within pre-set limits [S].
- **Agents from anywhere**: merchant apps, payment apps, general assistants (ChatGPT, Claude), dedicated agent platforms [S].
- **First use cases**: low-value, repetitive purchases such as groceries and dairy [S].
- **NPCI sees the payment, not the purchase** [S].

A third party's diagram (agent identity, power of attorney, logbook) matches this reporting in shape but remains a third party's model.

---

## 4. The rails that exist today, from NPCI's circulars

### UPI Reserve Pay (NPCI/UPI/OC-228/2025-26, 8 Oct 2025) [P, read from the original]

- The customer **blocks funds** in their account for multiple debits, "which can be initiated by the customer on the merchant's platform", until the funds are used up, the block is revoked, or it expires.
- **Limits:** up to **₹10,000** and up to **90 days**, amount and end date chosen by the user. **One block per mobile number per merchant** at a time.
- **Funding:** all UPI-permitted sources (savings, current, overdraft, RuPay credit card, pre-sanctioned credit lines).
- **Merchants:** "only for online verified merchants with low ticket and high frequency transactions" to begin with.
- **The block is not a payment guarantee.** Only a successful debit counts. A timeout is a decline, reversed in real time, never a deemed debit; retries only for timeouts, at most 3 in 24 hours.
- **Debit on purchase:** "The purchase action by the customer must result into instant debit request without any delay"; for quick commerce and food delivery, **delivery only after confirmed debit**.
- **Issuer banks:** mandatory notifications (SMS, email or other) for **block creation, modification, debit, revoke and expiry**; reserve shown in statements.
- **Merchants and acquirers:** let the user update and revoke on the merchant platform, check the remaining block before each debit, and display original block, remaining balance, expiry and full history.
- **UPI apps:** easy revoke; a consolidated view of all active blocks across merchants.
- **Disputes and reconciliation:** RBI's 2019 turnaround-time and compensation rules apply; Online Dispute Resolution must be available; transactions identified by purpose code 77.

### UPI Circle (NPCI/UPI/OC No. 201/2024-25, 13 Aug 2024) [P/S]

- **Partial delegation:** the secondary user initiates a payment request; "the Primary user shall complete the UPI transaction with a UPI PIN" [P]. This is structurally this project's default: the agent proposes, the human authenticates.
- **Full delegation:** the secondary user initiates and completes within limits: **₹5,000 per transaction, ₹15,000 per month** [S]. Up to 5 secondary users per primary; a secondary accepts only one primary; the secondary's app requires passcode or biometrics [S].
- Designed for **human** secondary users. UAP was the attempt to extend it to software.

---

## 5. What this means for the design

1. **The explicit-approval default is the regulator-aligned position.** Explicit approval on the payer's Trusted Surface, "AI recommends, deterministic rules decide", and a signed evidence trail match NPCI's chairman, CERT-In's recommendation and the reason RBI paused UAP. Keep it the default. This is the strongest external validation the project has.
2. **Silent repeats sit exactly in the contested zone.** They are bounded, standing-instruction based and low-cap, which is the shape a threshold rule would allow, but no threshold is published. Keep the Tier 0 caps conservative and **make the silent-path threshold a versioned mapping value**, so a published CERT-In/RBI/NPCI threshold becomes a data change, not a redesign.
3. **The basket stays ours by NPCI's own design.** "Without visibility into what is being purchased" confirms items, quantities, substitutions and item attributes will never be governed at the rail.
4. **Reserve Pay is merchant-scoped.** One block per merchant, so a cross-merchant budget can never come from the rail. It is a good fit for a *per-merchant* silent rail where a merchant exposes it.
5. **UPI Circle's partial delegation is the rail-level twin of this project's approval model.** Worth citing in the India mapping, as an analogy until NPCI says agents may be secondary users.
6. **Disputes must map to UPI's actual machinery.** Evidence packs should be shaped for Online Dispute Resolution and RBI's 2019 turnaround-time rules, since those govern Reserve Pay and UPI.
7. **Signed audit trails are becoming the norm.** AtOM's "signed, machine-readable audit trails" matches the ledger's signed, hash-chained records. Watch whether NPCI standardises a format; export to it if so.
8. **Kill switches exist at two levels.** MyUPI's Safety Switch freezes debits across banks; the layer's kill switch freezes proposals across merchants and rails. They complement each other.

**Design consequence:** design to plug into UAP when RBI clears it. Until then the India rails are UPI intent (PIN) and merchant wallets as `not_bound` profiles; Reserve Pay is the candidate rail for silent standing instructions where a merchant exposes it.

---

## 6. Still unknown

- UAP's technical specification, and whether it will go to public consultation.
- The CERT-In and RBI thresholds for human-in-the-loop.
- Liability when an agent mis-executes a mandate, and the dispute path for agent-initiated debits.
- How agent-initiated purchases square with the E-mandate Framework's 24-hour pre-debit notice (MediaNama flags the conflict).
- Whether agents may be UPI Circle secondary users.
- The Claude pilot's date (Feb 2025 or Feb 2026; sources disagree).

**Watch:** RBI statements on UAP clearance; any NPCI consultation (worth responding to); CERT-In threshold values; UAP specification publication.

---

## 7. Sources

- [NPCI OC-228/2025-26, UPI Reserve Pay (primary)](https://www.npci.org.in/uploads/UPI_OC_No_228_FY_2025_26_Enhancement_in_UPI_Single_Block_Multiple_Debits_UPI_Reserve_Pay_a9095c181d.pdf)
- [NPCI OC-200/2024-25, Single Block Multiple Debits](https://www.npci.org.in/PDF/npci/upi/circular/2024/UPI-OC-No-200-FY-24-25%E2%80%93Enablement-of-UPI-Mandate-feature-of-Single-Block-Multiple-Debits.pdf)
- [TeamLease RegTech: NPCI UPI Circle notification (OC 201)](https://www.teamleaseregtech.com/updates/article/34761/npci-issued-a-notification-regarding-the-introduction-of-upi-circle-de/)
- [Business Standard: UPI Circle launch](https://www.business-standard.com/finance/personal-finance/npci-launches-upi-circle-for-secure-delegated-payments-with-trusted-users-124081900536_1.html)
- [MediaNama: NPCI says AI shouldn't approve UPI payments (GFF 2026)](https://www.medianama.com/2026/09/223-npci-ai-agents-upi-payments/)
- [Business Standard: UPI's AI protocol temporarily on hold](https://www.business-standard.com/finance/news/upi-s-ai-protocol-temporarily-on-hold-as-npci-builds-safeguards-126091101413_1.html)
- [Business Standard: India may allow agentic AI-led UPI transactions](https://www.business-standard.com/finance/news/india-may-allow-agentic-ai-led-upi-transactions-under-new-npci-protocol-126070801343_1.html)
- [Outlook Business: UAP framework](https://www.outlookbusiness.com/news/india-plans-ai-powered-upi-payments-framework-through-unified-agent-protocol)
- [ClearingPost: UAP](https://clearingpost.com/insights/npci-unified-agent-protocol-agentic-upi/)
- [India Fintech Diaries: UAP pulled at GFF](https://indiafintechdiaries.substack.com/p/ai-agents-land-titles-and-a-new-name)
- [MediaNama: products launched at GFF 2026](https://www.medianama.com/2026/09/223-agentic-ai-products-fintechs-gff-2026/)
- [Inc42: GFF 2026 NPCI launches](https://inc42.com/buzz/gff-2026-npci-launches-instant-card-issuing-atms-deepens-ai-push/)
- [MediaNama: MeitY/CERT-In proposes human-in-the-loop for agentic payments](https://www.medianama.com/2026/07/223-meity-proposes-mandatory-human-interventions-agentic-ai-payments/)
- [RBI FREE-AI report](https://rbidocs.rbi.org.in/rdocs/PublicationReport/Pdfs/FREEAIR130820250A24FF2D4578453F824C72ED9F5D5851.PDF)
- [Razorpay newsroom: ChatGPT agentic payments](https://razorpay.com/newsroom/razorpay-npci-and-openai-come-together-to-launch-agentic-payments-ushering-in-ai-driven-commerce-at-national-scale/)
- [The Paypers: Razorpay and NPCI on Claude](https://thepaypers.com/payments/news/razorpay-and-npci-launch-agentic-payments-on-claude)
- [Pinsent Masons: what rules for agentic AI payments might look like](https://www.pinsentmasons.com/out-law/analysis/what-new-rules-agentic-ai-payments-look-like)
