# 19 · Model Spec

**Problem:** the specs `01` to `07` define entities loosely ("fields are provisional"). This document fixes every entity's fields and types, the canonical form that is hashed and signed, how records are chained, and where each record is stored. Code is written against this document.
**Status:** drafted 9 Oct 2026; the five open questions resolved and approved 12 Oct 2026 (§1.6, §3.3, §5.1, §5.3, §6, §10.1). Awaiting review of the whole.
**Rule:** where this document and `01` to `07` disagree, the numbered spec wins and this document is corrected.

---

## 1. Conventions

### 1.1 Notation
`Type?` is optional (the key is omitted when absent, never `null`). `[Type]` is a list. `{a, b}` is a nested object. `A | B` is an enum. Every object is **closed**: unknown keys are rejected at parse time.

### 1.2 Primitive types

| Type | Form | Example |
|---|---|---|
| `Id<prefix>` | `prefix_` + 26-character ULID, lowercase prefix | `spk_01j9xk3m2q8v7c4r5t6w8y0z1a` |
| `Timestamp` | RFC 3339, UTC, exactly millisecond precision, `Z` suffix | `2026-10-09T13:05:00.000Z` |
| `Duration` | integer seconds, non-negative | `86400` |
| `Money` | `{amount_minor: int, currency: str}`; ISO 4217 code; `amount_minor` an integer in ±(2^53 − 1); **no floats anywhere** | `{"amount_minor": 120000, "currency": "XTS"}` (XTS: the ISO 4217 testing code) |
| `PerMille` | integer 0 to 1000 (used instead of fractions) | `900` |
| `BasisPoints` | integer, 1 bp = 0.01% | `1000` (= 10%) |
| `Hash` | `sha256:` + 64 lowercase hex characters | `sha256:9f86…` |
| `Ref` | opaque string ≤ 128 characters, `[A-Za-z0-9._:-]` | `mrc_01j9…`, `fake-a:cart:42` |
| `ProfileRef` | `prof://<payer_ref>/<kind>/<id>`; points into the profile store | `prof://pyr_01…/cart/crt_01…` |
| `Version` | integer ≥ 1, incremented per change of that object | `3` |
| `Window` | `{kind: calendar_month | calendar_week | calendar_day | rolling, tz: IANA name, length?: Duration}`; `length` only for `rolling` | `{"kind":"calendar_month","tz":"Etc/UTC"}` |
| `WindowId` | the window instance a time falls in: `2026-10` (month), `2026-W41` (week), `2026-10-09` (day), or the rolling start `Timestamp` | `2026-10` |

**ID prefixes:** `whl` wheel, `spk` spoke, `rul` rule, `evt` wheel event, `rcp` authority receipt, `prp` proposal, `det` determination, `res` resolution, `pst` posting, `txg` posting group, `exa` execution attempt, `swp` sweep, `fnd` finding, `bnd` evidence bundle, `rls` release, `key` key log entry, `chl` challenge, `asr` assertion, `clr` caller, `enr` enrolment, `rcv` recovery, `pyr` payer, `opr` operator, `mrc` merchant ref, `dst` destination ref, `prf` profile record.

### 1.3 Canonical form

- **Canonical bytes** of an object are its **JCS** serialisation (RFC 8785): sorted keys, no whitespace, UTF-8, numbers in the RFC 8785 form. Because every number is an integer within ±(2^53 − 1), number formatting is unambiguous.
- `hash(x) = "sha256:" + hex(SHA-256(canonical(x)))`.
- **Strings are NFC-normalised** before canonicalisation. Enums are lowercase snake_case.

### 1.4 Signed records

Every ledger record has the shape:
```
{ ...fields, chain: ChainLink, signature: Signature }
ChainLink   {chain_id: Ref, seq: int ≥ 0, previous_hash: Hash, continues?: {chain_id: Ref, host_ref: HostRef, handover_hash: Hash}}
Signature   {key_id: Id<key>, alg: "ES256", value: base64url(r ‖ s)}   (64-byte IEEE P1363 form)
```
- The signature covers `canonical(record without "signature")`, so it covers the chain link.
- `record_hash = hash(record including "signature")`; the next record's `previous_hash` is this value.
- Genesis: `seq = 0`, `previous_hash = "sha256:" + 64 zeros`.
- A chain that **continues a wheel from another host** (§10.1) starts with `seq = 0` and carries `continues: {chain_id: Ref, host_ref: HostRef, handover_hash: Hash}` in its `ChainLink`; `previous_hash` is then `handover_hash` instead of zeros.
- The value is byte-identical to a JWS ES256 signature over the same bytes, so records export to JWS (for AP2) without re-signing.

### 1.5 Chains

| Chain | `chain_id` | Holds |
|---|---|---|
| Wheel chain | the `wheel_id` | everything about one wheel: versions, events, proposals, determinations, resolutions, postings, attempts, sweeps' findings, recoveries |
| Key log | `keylog:<host_ref>` | key log entries (`07` §2) |
| Release log | `releases:<host_ref>` | evidence releases (`06` §4) |
| Host chain | `host:<host_ref>` | callers, enrolments and challenges not yet tied to a wheel |

Appends to one chain are serialised (one writer at a time per `chain_id`). This is also how "one hold per wheel at a time" (`03` D1) is enforced.

### 1.6 Ledger vs profile store

**The ledger holds only:** IDs, refs, enums, `Money`, timestamps, durations, integers, hashes, signatures, and commitments. **Never** names, item descriptions, addresses, free text, raw responses or rule contents that reveal preferences. Anything else lives in the profile store, encrypted under the payer's data key (§11), and the ledger holds its commitment.

Consequence: **wheel and spoke documents (their rules) are profile-store records.** The ledger holds their version, hash and signature. A verifier with the disclosed document checks it against the ledger; a verifier without it still sees that a version existed and was signed.

---

## 2. Parties and references

```
PayerRef      Id<pyr>          opaque; never a name, phone number or account number
OperatorRef   Id<opr>
HostRef       Ref
MerchantRef   Id<mrc>          opaque; display name lives in the profile store
DestinationRef Id<dst>         opaque; the address lives in the profile store
AdapterId     Ref              e.g. "fake-a", "fake-b"
RailId        Ref              namespaced by jurisdiction: "<jurisdiction>:<rail>"
JurisdictionId Ref             a key of a jurisdiction mapping (`../jurisdictions/`)
```

---

## 3. Authority (`01`)

### 3.1 Wheel (profile store; ledger holds `WheelVersion`)
```
Wheel {
  wheel_id: Id<whl>, payer_ref: PayerRef, home_jurisdiction: JurisdictionId,
  rules: [Rule],                                   payer (Tier 1) rules
  budgets: [{limit: Money, window: Window}],       at most one per currency
  status: active | suspended | revoked,
  host_ref: HostRef, version: Version
}
WheelVersion (ledger) {
  wheel_id, version, document_hash: Hash, status, home_jurisdiction,
  budgets: [{limit: Money, window: Window}],       amounts are not personal data; needed for postings
  event_id: Id<evt>, chain, signature
}
```

### 3.2 Spoke (profile store; ledger holds `SpokeVersion`)
```
Spoke {
  spoke_id: Id<spk>, wheel_id, parent_id: Id<spk>?,         absent = hangs off the hub
  bindings: {
    callers?: [Id<clr>],
    purpose?: Ref,
    instruction?: StandingInstruction
  },
  rules: [Rule],
  caps: [{limit: Money, window: Window}],                   at most one per currency
  approval_mode: explicit_tap | silent_with_notice,         silent only if bindings.instruction is set
  valid_from: Timestamp, valid_until: Timestamp,            valid_until required
  status: active | suspended | revoked | expired,
  version: Version
}
StandingInstruction {
  merchant_ref: MerchantRef, adapter_id: AdapterId,
  lines: [CartLineTemplate], destination_ref: DestinationRef, rail_id: RailId,
  schedule: {kind: on_request | fixed, every?: Duration, anchor?: Timestamp},
  price_tolerance: BasisPoints,
  reference_total: Money
}
CartLineTemplate { item_id: Ref, variant_id: Ref?, quantity: int ≥ 1 }
SpokeVersion (ledger) {
  spoke_id, wheel_id, parent_id?, version, document_hash, status,
  approval_mode, caps, valid_from, valid_until, depth: int,
  event_id, chain, signature
}
```

### 3.3 Rule and the 14 dimensions
```
Rule {
  rule_id: Id<rul>, dimension: Dimension, constraint: Constraint<dimension>,
  author: operator | payer | system | jurisdiction, author_ref: Ref, version: Version
}
```
`Dimension` and its constraint type:

| # | `dimension` | `constraint` |
|---|---|---|
| 1 | `caller` | `{any_of_callers?: [Id<clr>], min_assurance?: key_bound | session_bound}` |
| 2 | `merchant` | `{allow?: [MerchantRef], block?: [MerchantRef], allow_categories?: [Ref], block_categories?: [Ref]}` |
| 3 | `items` | `{allow_items?: [Ref], block_items?: [Ref], allow_categories?: [Ref], block_categories?: [Ref], max_quantity_per_line?: int, max_lines?: int}` |
| 4 | `substitution` | `{mode: none | same_item_variant | same_category, max_price_increase?: BasisPoints}` |
| 5 | `item_attributes` | `{block_terms?: [Ref], require_terms?: [Ref], unknown: block | allow, min_attestation?: AttestationClass}` |
| 6 | `amount` | `{per_transaction_max?: Money, aggregate?: {limit: Money, window: Window}}` |
| 7 | `velocity` | `{max_count: int, window: Window}` |
| 8 | `payment_method` | `{allow_rails?: [RailId], allow_authentication?: [human_per_payment | human_at_setup | none], require_amount_binding?: bound_at_rail}` |
| 9 | `time` | `{allow?: [TimeSpan], exclude?: [TimeSpan]}`; `TimeSpan {days: [mon..sun], from: "HH:MM", to: "HH:MM", tz: IANA}` |
| 10 | `approval_mode` | `{at_most: explicit_tap | silent_with_notice}` (a rule can only make approval stricter) |
| 11 | `destination` | `{allow?: [DestinationRef], quarantine_new?: Duration}` |
| 12 | `conditions` | `{when: Predicate, effect: block | force_explicit_tap | clarify}` |
| 13 | `extras` | `{allow: [{kind: tip | discount | merchant_note | packaging | other, max?: Money}]}`; absent kinds are denied |
| 14 | `currency_geography` | `{allow_currencies?: [str], allow_jurisdictions?: [JurisdictionId]}` |

**Predicate** (for dimension 12, `approval.silent.preconditions`, `conditional_unlocks`, `external.unusual_predicates`): a small expression tree, no code.
```
Predicate = {all: [Predicate]} | {any: [Predicate]} | {not: Predicate}
          | {fact: str, op: eq | ne | lt | le | gt | ge | in | exists, value?: Scalar | [Scalar]}
Scalar    = str | int | bool | Money
```
Comparisons on a missing fact are false; `exists` tests presence. Fact names are listed in §5.3.

**Category and attribute vocabularies** (approved 12 Oct). `allow_categories`, `block_terms` and similar fields hold **vocabulary terms**:
- **Format:** `<namespace>:<scheme>:<code>`, e.g. `std:mcc:5812`, `std:gpc:10000025`, `std:allergen:AN`, `op:<operator>:office_snacks`. Each vocabulary is a versioned, signed document; the Tier 0 config names the versions it uses (§13.1).
- **Starter vocabulary (`std:`):** merchant categories from ISO 18245 merchant category codes; item categories from GS1 Global Product Classification bricks; item attributes from GS1 allergen type codes ("contains", "may contain"), extended as needed. **Before shipping any of these lists in the repository, confirm their redistribution licences.**
- **Operators** add terms only under their own `op:<operator>:` namespace.
- **Merchant adapters map** their own categories and attributes onto terms in a versioned mapping document; the mapping version is recorded on the fact, so a wrong mapping is attributable (`fact_misreported`).
- **An unmapped value is unknown,** and the rule's `unknown: block | allow` decides; a dimension without an `unknown` field treats unknown as a failing clause.

### 3.4 Tier 0 (operator floor)
Tier 0 is a config document (§13.1). Its rules have `author = operator`; jurisdiction mapping clauses have `author = jurisdiction`. The ledger holds `Tier0Version {operator_ref, jurisdiction, version, document_hash, chain, signature}` on the host chain.

### 3.5 WheelEvent (ledger)
```
WheelEvent {
  event_id: Id<evt>, target: {kind: wheel | spoke, id: Id<whl> | Id<spk>},
  event_type: create | narrow | widen | suspend | resume | revoke | expire | renew | change_requested,
  previous_document_hash: Hash?, new_document_hash: Hash?,
  initiated_by: payer | operator | system | caller,
  initiator_ref: Ref,                       assertion_id, operator_ref, trigger id or caller_id
  reason_code: Ref?,                        required for operator and system
  presented_hash: Hash?,                    what the payer was shown (payer events)
  auth_strength_achieved: Ref?,             a level on the jurisdiction scale
  occurred_at: Timestamp, chain, signature
}
```
Operator events other than `suspend` are rejected at write time. `change_requested` changes nothing.

### 3.6 AuthorityReceipt (ledger)
```
AuthorityReceipt {
  receipt_id: Id<rcp>, determination_id: Id<det>, outcome: allowed | refused,
  spoke_id, deciding_clauses: [{rule_id, dimension, author}],
  approval_mode, approval_mode_set_by: Ref,
  versions: Versions, reserved: Money?, issued_to: OperatorRef, chain, signature
}
Versions { wheel: Version, spoke: Version, tier0: Version, mapping: Ref, evaluator: Hash }
```
`mapping` is `<jurisdiction>@<semver>`; `evaluator` is the build digest (`07` §7). A receipt carries no other spoke's data and no budget figures beyond `reserved`.

---

## 4. Proposals (`02`)

### 4.1 Proposal (profile store body; ledger record)
```
ProposalBody (profile store) {
  merchant_ref, adapter_id, merchant_cart_ref: Ref,
  lines: [CartLine], fees: [{kind: delivery | platform | packaging | tax | other, amount: Money}],
  extras: [{kind, amount?: Money, note_ref?: ProfileRef}],
  total: Money, rail_id?: RailId, destination_ref: DestinationRef,
  substitution?: {allowed: bool},
  agent_confidence: PerMille,
  reasoning_ref?: ProfileRef
}
CartLine { item_id: Ref, variant_id?: Ref, quantity: int ≥ 1, unit_price: Money, category?: Ref }

Proposal (ledger) {
  proposal_id: Id<prp>, wheel_id, spoke_id, caller_id: Id<clr>,
  intent_id: Ref, proposal_hash: Hash,           hash(ProposalBody) after normalisation
  body_commitment: Hash, total: Money, merchant_ref,
  proof: ProposalProof, received_at: Timestamp, chain, signature (host)
}
ProposalProof {
  format: dpop | signature | bearer_session,
  nonce: str (≥ 16 bytes, base64url), created_at: Timestamp,
  key_id?: Id<key>, value?: str,                 absent only for bearer_session
  session_ref?: Ref                              the grant, for bearer_session
}
```
- **`proposal_hash`** is over the normalised body: lines sorted by `(item_id, variant_id)`, fees sorted by `kind`.
- **Unmodelled attributes:** the body is closed (§1.1). A field the schema does not know fails parsing and yields `no_authority` with the system clause `sys.unmodelled_attribute`.
- **`intent_id`:** caller-supplied (any `Ref`), or layer-computed for standing instructions as `si:<spoke_id>:<WindowId or slot Timestamp>`.

### 4.2 System clauses
Failing clauses not written by an operator or payer use `author = system` and these `rule_id`s:
`sys.no_spoke`, `sys.spoke_inactive`, `sys.spoke_expired`, `sys.caller_not_bound`, `sys.caller_assurance`, `sys.proof_invalid`, `sys.proof_stale`, `sys.nonce_reused`, `sys.unmodelled_attribute`, `sys.intent_conflict`, `sys.cap_exceeded`, `sys.budget_exceeded`, `sys.open_finding`, `sys.recovery_pending`, `sys.drift`.

---

## 5. Facts and determinations (`02`, `07` §6)

### 5.1 Fact
```
Fact {
  name: str, value: FactValue,                   typed by the fact name (§5.3); personal values are a ProfileRef
  source: Ref, source_version: Ref, captured_at: Timestamp,
  verified: bool,                                true only if source ∈ facts.verified_sources
  attestation: {class: source_signed | capturer_signed | payer_presented | injected,
                attested_by: Id<key>, capturer: host | operator, raw_hash?: Hash}
}
FrozenFacts { facts: [Fact] sorted by (name, source) }     facts_hash = hash(FrozenFacts)
FactValue = str | int | bool | Timestamp | Duration | Money | PerMille | Ratio | TermSet | ProfileRef
          | [one of the above, all the same type]
Ratio     = {num: int, den: int ≥ 1}
TermSet   = {terms: [vocabulary term], vocabulary: Ref, mapping_version: Ref}
```

### 5.2 Determination (ledger)
```
Determination {
  determination_id: Id<det>, proposal_id, intent_id, proposal_hash, spoke_id,
  stage: proposal | authorisation,
  outcome: allow | clarify | refuse | no_authority,
  approval_mode: explicit_tap | silent_with_notice, approval_mode_set_by: Ref,
  failing_clauses: [{rule_id: Ref, dimension: Dimension | system, author, overridable: bool}],
  clarify_fields: [str],
  facts_hash: Hash, fact_refs: [ProfileRef],
  presented_hash?: Hash, fetched_hash?: Hash,
  rail_id?: RailId, rail_chosen_by?: default | agent | payer | instruction, amount_binding?: bound_at_rail | not_bound,
  duplicate_check: {matched_proposal_id?: Id<prp>, window: Duration},
  versions: Versions, evaluated_at: Timestamp,
  links: {previous_determination_id?: Id<det>, receipt_id?: Id<rcp>},
  chain, signature
}
```
- `overridable` is true only when `author = payer`.
- `failing_clauses` is sorted by `(author, dimension, rule_id)` so the same inputs give identical bytes.
- **`presented_hash`** = `hash({lines, fees, extras, total, rail_id, destination_ref})` as rendered to the payer.

### 5.3 Fact types (approved 12 Oct)

**Values are a closed set of types** (`FactValue`, §5.1). Rules:
- **No floats.** A source value with a fraction (a 4.5 rating) is converted at capture to an integer with a declared scale (`45` with scale 10, or a `PerMille`, or a `Ratio`), and the conversion is part of the fact's `source_version`.
- **Anything unstructured** (a raw response, a document) is stored as a `ProfileRef` with its hash. Predicates cannot compare a `ProfileRef` except with `exists`; it matters only through a typed fact derived from it.
- **Every fact name declares its type.** A predicate comparing a fact with a value of another type fails when the config loads, not at payment time.

| Fact name | Type |
|---|---|
| `time.now` | `Timestamp` |
| `caller.id` | `str` |
| `caller.assurance` | `str` (`key_bound` \| `session_bound`) |
| `notice.delivered` | `bool` |
| `notice.delivered_at` | `Timestamp` |
| `payer.context.<name>` | declared per name in the Tier 0 config |
| `account.age_days` | `int` |
| `account.success_rate_permille` | `PerMille` |
| `account.open_disputes` | `int` |
| `findings.open_on_spoke` | `int` |
| `merchant.item_attributes.<item_id>` | `TermSet` |
| `merchant.cart_hash` | `str` (a `Hash`) |
| `fx.<from>.<to>` | `Ratio` |
| `recovery.pending` | `bool` |
| `ext.<namespace>.<name>` | declared with the namespace; unknown names are treated as unverified |

### 5.4 Resolution (ledger)
```
Resolution {
  resolution_id: Id<res>, determination_id, proposal_id,
  state: pending | approved | approved_with_override | edited | declined | expired
       | proceeded_on_silence | superseded,
  by: payer | system | caller,
  assertion_id?: Id<asr>,                       required for approved, approved_with_override, edited, declined
  overridden_clauses?: [Ref],                   approved_with_override only
  superseded_by?: Id<prp>,
  occurred_at: Timestamp, chain, signature
}
```
`proceeded_on_silence` is only valid on a spoke with `bindings.instruction`; `approved` always carries a `SurfaceAssertion`.

---

## 6. Money in flight (`03`)

```
AccountSet { owner: Id<whl> | Id<spk>, currency: str, window_id: WindowId }
Posting (ledger) {
  posting_id: Id<pst>, txn_group_id: Id<txg>, account_set: AccountSet,
  kind: hold | capture | release | overcapture | refund,
  amount: Money,                                 positive; direction comes from kind
  payment_ref: Id<det>, signal_ref?: Ref,        execution attempt, finding or rail signal
  posted_at: Timestamp, chain, signature
}
```
- A posting group contains one posting per account set touched (the spoke and every ancestor including the hub), all in one chain append.
- **Balances are never stored.** `available = limit − held − spent`, derived by replaying postings; a materialised view may cache them but is rebuilt from postings and checked in `verify_integrity()`.
- **Windows (approved 12 Oct).** A hold's `window_id` is fixed when placed; its capture, release, overcapture and refund all post to that same `window_id`, even after the window has closed. So a payment is always counted under the limits it was approved against.
- A release into a closed window returns room nobody can use; this is harmless.
- The rule "no new holds while `available < 0`" (`03` §3) is checked on the **current** window of each account set. An overcapture posted to a closed window still opens a finding (`05`) and notifies the payer.

---

## 7. Rails and execution (`04`)

### 7.1 RailProfile (mapping data, §13.2)
```
RailProfile {
  rail_id: RailId, authentication: human_per_payment | human_at_setup | none,
  placement: pending_then_confirm | immediate,
  amount_binding: bound_at_rail | not_bound,
  definitive_signals: {success: [Ref], failure: [Ref], unknown: [Ref]},
  retry_policy: {safe_errors: [Ref], max_retries: int, window: Duration},
  reversibility: refund_only | chargeback | none,
  dispute_route: Ref,
  limits: {per_transaction?: Money, per_period?: {limit: Money, window: Window}, max_duration?: Duration},
  approval_modes: [explicit_tap | silent_with_notice],
  status: live | excluded | ready_no_merchant | placeholder | reference
}
```

### 7.2 ExecutionAttempt (ledger)
```
ExecutionAttempt {
  attempt_id: Id<exa>, determination_id, rail_id, adapter_id,
  idempotency_key: Hash,                         hash({determination_id, attempt_number})
  attempt_number: int ≥ 1, request_hash: Hash,   request body in the profile store
  response_status: Ref, outcome: success | failure | unknown,
  merchant_order_ref?: Ref, started_at, ended_at, retry_of?: Id<exa>,
  chain, signature
}
```

### 7.3 Merchant adapter interface
Every adapter implements these operations; contract tests run the same suite against every adapter.

| Operation | Input | Output |
|---|---|---|
| `describe()` | | `{adapter_id, version, rails_offered: [RailId], supports_cancel: bool, history_coverage: Coverage}` |
| `get_cart(cart_ref)` | `Ref` | `{lines, fees, total, merchant_cart_hash: Hash}` |
| `get_item_attributes(item_ids)` | `[Ref]` | `{item_id: {terms: [Ref], source: Ref}}` |
| `place(cart_ref, rail_id, idempotency_key, destination_ref)` | | `{status: Ref, merchant_order_ref?: Ref, handoff?: {kind: rail_redirect, ref: Ref}}` |
| `get_order(merchant_order_ref)` | | `{status: Ref, lines, charged?: Money}` |
| `list_orders(since)` | `Timestamp` | `[{merchant_order_ref, placed_at, total, status}]` plus `Coverage` |
| `cancel(merchant_order_ref)` | | `{status: Ref}` (only if `supports_cancel`) |

`Coverage {complete: bool, limit_code?: last_n | time_bounded | partial_fields, detail?: int}`.
Status strings are adapter-specific `Ref`s; the rail profile's `definitive_signals` say which mean success, failure or unknown.

---

## 8. Reconciliation (`05`)

```
SourceAdapter { source_id: Ref, class: bank | rail | merchant | receipt, coverage: Coverage, freshness: Duration }
Sweep (ledger) {
  sweep_id: Id<swp>, source_id, trigger: event | scheduled | before_hold,
  started_at, ended_at, observed_hash: Hash, coverage: Coverage, finding_ids: [Id<fnd>],
  chain, signature
}
Finding (ledger) {
  finding_id: Id<fnd>, kind: matched | pending_resolved | overcapture | missing_order | item_mismatch
                        | refund_stalled | source_conflict | coverage_gap | external_order,
  payment_ref?: Id<det>, merchant_order_ref?: Ref,
  sources: [{source_id, class, observed_hash: Hash}],
  status: open | closed, closed_by?: evidence | payer_acceptance,
  closing_ref?: Ref,                              evidence sweep or assertion
  suggested_next_step?: Ref,                      from the rail profile's dispute_route
  opened_at, closed_at?, chain, signature
}
```
A finding's status change is a new `Finding` record with the same `finding_id` and a higher `seq`; the earlier record is never modified.

---

## 9. Evidence (`06`)

```
EvidenceBundle {
  bundle_id: Id<bnd>, payment_ref: Id<det>, requester_role: payer | operator | merchant | rail | regulator,
  scope: [record kinds disclosed],
  records: [signed ledger records],               in chain order
  chain_proofs: [{chain_id, from_seq, to_seq, head_hash: Hash}],
  disclosed: [{commitment: Hash, value: any, salt: base64url}],      opened commitments only
  keys: [KeyLogEntry],                            every key that signed an included record
  evaluator: Hash, mapping: Ref,
  divergence: {primary: Category, contributing: [Category], step_refs: [Ref]},
  generated_at, signature
}
Category = rule_permitted_wrongly | unmodelled_dimension | evaluator_fault | poor_proposal_within_rules
         | payer_authorised | fact_misreported | merchant_divergence | rail_divergence | external | no_divergence
Release (ledger, release log) {
  release_id: Id<rls>, bundle_hash: Hash, requester_role, requester_ref: Ref, scope,
  basis: default | consent | lawful_demand, basis_ref?: Ref, released_at, chain, signature
}
```
**Verifier output:** `{signatures: ok | failed, chains: ok | broken, replay: reproduced | differs | evaluator_unpublished, presented_hash: matches | differs, keys: ok | signed_after_compromise | unknown_key, attestations: [{fact, class}]}`.

---

## 10. Identity and keys (`07`)

```
KeyLogEntry (key log) {
  key_id: Id<key>, principal_role: surface | recovery_key | caller | host | operator | source | evaluator_release,
  principal_ref: Ref, public_key: {kty: "EC", crv: "P-256", x: base64url, y: base64url},
  algorithm: "ES256", protection_class: hardware_attested | hardware | software,
  attestation_ref?: ProfileRef,                   platform attestation evidence, if any
  valid_from, valid_until?, status: active | retired | revoked | compromised,
  compromised_since?: Timestamp, enrolled_by: Id<evt> | Id<enr>, chain, signature
}
Challenge (host chain) {
  challenge_id: Id<chl>, nonce: base64url (32 bytes), action: Action, subject_hash: Hash,
  wheel_id?: Id<whl>, issued_at, expires_at, chain, signature
}
Action = authorise | decline | edit | override | wheel_event | enrol | recover
SurfaceAssertion (wheel chain) {
  assertion_id: Id<asr>, surface_key_id: Id<key>, challenge_id, action, subject_hash,
  user_verification: Ref,                         class on the jurisdiction scale
  auth_strength_achieved: Ref,                    derived by the host, never stated by the surface
  proof: {format: raw_es256, value: base64url}
       | {format: webauthn, authenticator_data: base64url, client_data_json: base64url, signature: base64url},
  occurred_at, chain, signature (host)
}
Caller (host chain) {
  caller_id: Id<clr>, payer_ref, credential: {key_id: Id<key>} | {grant_ref: Ref},
  assurance: key_bound | session_bound, claimed_ref?: ProfileRef,     self-declared name and version
  valid_until: Timestamp, status: pending | active | suspended | revoked | expired,
  enrolled_by?: Id<asr>, chain, signature
}
Enrolment (host chain) {
  enrolment_id: Id<enr>, kind: surface | recovery_key | caller, payer_ref?,
  candidate_key?: public key, candidate_grant?: Ref, claimed_ref?: ProfileRef,
  status: pending | approved | rejected | expired, decided_by?: Id<asr>,
  requested_at, decided_at?, chain, signature
}
RecoveryCase (wheel chain) {
  recovery_id: Id<rcv>, wheel_id, path: with_recovery_key | delayed,
  factors: [{class: possession | knowledge | inherence, verifier: recovery_key | surface | external, ref: Ref}],
  new_surface_key_id?: Id<key>, started_at, completes_at, status: pending | completed | cancelled,
  cancelled_by?: Id<asr> | Ref, chain, signature
}
```
- A challenge is single-use; its `subject_hash` must equal the assertion's.
- **Signing a challenge:** `raw_es256` signs `canonical({challenge_id, nonce, action, subject_hash})`. `webauthn` uses that hash as the WebAuthn challenge.
- `recovery_key` principals may only appear in assertions with `action = recover`; any other action is rejected.
- While a `RecoveryCase` with `path = delayed` is pending, the fact `recovery.pending = true` is injected and the system clause `sys.recovery_pending` refuses every proposal on the wheel.

---

### 10.1 Handover between hosts (approved 12 Oct)
```
Handover (old wheel chain, last record) {
  wheel_id, from_host: HostRef, to_host: HostRef,
  final_balances_hash: Hash,                      hash of derived balances at handover
  carried_over: {holds: [Id<pst>], open_findings: [Id<fnd>]},
  key_log_export_hash: Hash,                      hash of the old host's key log export
  approved_by: Id<asr>,                           payer approval on a Trusted Surface
  occurred_at, chain, signature (old host)
}
```
- **Clean point only:** a handover is refused while any payment on the wheel is `pending_outcome`. Open holds are carried over as postings of kind `hold` on the new chain that reference the old `posting_id`s; open findings are carried over by id.
- **Old chain closed:** no record may be appended after the `Handover`.
- **New chain linked:** its first record carries `continues` (§1.4) with the handover's hash.
- **Imported, never re-signed:** the new host keeps a read-only copy of the old chain and the old host's key log export, so old signatures verify after the old host is gone.
- **Payer check:** the payer's Trusted Surface holds the last chain head it was sent and confirms that the `Handover` follows it, so no history can be dropped in the move.

## 11. Profile store and data keys (`07` §8)

```
DataKey { payer_ref, dek_id: Ref, wrapped: base64url, kek_ref: Ref, status: active | held | shredded,
          hold?: {basis: Ref, until: Timestamp}, created_at, shredded_at? }
ProfileRecord { record_id: Id<prf>, profile_ref: ProfileRef, payer_ref, kind: Ref, dek_id: Ref,
                ciphertext: base64url, nonce: base64url, created_at }          AES-256-GCM
Commitment = "sha256:" + hex(SHA-256(salt ‖ canonical(value))), salt = 32 random bytes,
             salt stored inside the encrypted ProfileRecord beside the value
```
- **Shredding** sets the `DataKey` to `shredded` and destroys `wrapped` (and schedules the KEK for deletion if it is per-payer). Ledger records are untouched; commitments can no longer be opened.
- The profile store is mutable but every change is an insert of a new `ProfileRecord`; old ciphertext is unreadable once the key is shredded.

---

## 12. Storage layout (Postgres)

**Ledger database** (role `ledger_writer`: `INSERT` and `SELECT` only):
```
ledger_records (
  chain_id text, seq bigint, record_type text, record_id text,
  body jsonb,                 canonical record without signature (PII-free by §1.6)
  record_hash text, previous_hash text, signature jsonb, created_at timestamptz,
  primary key (chain_id, seq), unique (record_hash)
)
```
- A trigger rejects `UPDATE` and `DELETE`; a second trigger rejects an insert whose `previous_hash` is not the current head of `chain_id` or whose `seq` is not head + 1.
- Typed views per `record_type` (`v_determinations`, `v_postings`, …) for queries; the table is the source of truth.
- Appends take a per-chain advisory lock (`pg_advisory_xact_lock(hashtext(chain_id))`).

**Profile database:** `profile_records`, `data_keys`, `recovery_secrets` (Argon2id hashes only), with `UPDATE` allowed only on `data_keys.status`, `wrapped` and `hold`.

**Evidence files:** bundles and raw sweep snapshots in object storage with retention lock; any personal data inside is encrypted under the payer's data key.

---

## 13. Configuration documents (YAML, loaded by `mapping/`)

### 13.1 Tier 0 config
```yaml
tier0:
  operator_ref: opr_…
  jurisdiction: <jurisdiction>
  version: 1
  vocabularies: {categories: <ref@version>, item_attributes: <ref@version>}
  parameters:                 # every parameter in policy_parameters.md; a missing required one fails startup
    auth.strength.create: <level on the mapping scale>
    wheel.max_depth: <int>
    budget.window: {kind: <window kind>, tz: <IANA tz>}
    decision.clarify_threshold: <PerMille>
    decision.duplicate_window: <Duration>
    # …
  rules: [Rule]               # author: operator
```

### 13.2 Jurisdiction mapping
```yaml
mapping:
  jurisdiction: <jurisdiction>
  version: <semver>
  status: interpretation | reference
  auth_strength_scale:        # ordered, weakest first
    - {level: none,       requires: []}
    - {level: possession, requires: [possession]}
    - {level: two_factor, requires: [possession, {any_of: [knowledge, inherence]}]}
  rails: [RailProfile]
  silent: {max_amount: null}  # unset until a regulator publishes a threshold
  evidence_retention: <Duration>   # per jurisdiction; see the jurisdiction notes
  dispute_routes: [{id: Ref, description_ref: Ref}]
  clauses: [Rule]             # author: jurisdiction; never overridable
  scheme_mappings: [{scheme: Ref, construct: spoke | standing_instruction | ledger_export | caller_identity, note: str}]
  open_questions: [str]
```

---

## 14. External mappings

| External construct | Model entity |
|---|---|
| AP2 v0.2 open Checkout/Payment Mandate | `Spoke` (+ `StandingInstruction` for repeats), exported as SD-JWT |
| AP2 v0.2 closed mandate | `Proposal` citing its spoke |
| AP2 receipts / dispute evidence | `Determination`, `Resolution`, `EvidenceBundle` |
| ACP one-time Allowance | emitted per `approved` `Resolution` on a card rail |

National schemes (delegation, mandate and agent-payment schemes) map onto `Spoke`, `StandingInstruction`, ledger exports and `Caller` in each jurisdiction mapping's `scheme_mappings` (§13.2), never here (invariant 24).

---

---

## 15. Resolved questions and follow-ups

The five open questions of the 9 Oct draft were resolved and approved on 12 Oct: handover between hosts (§10.1), vocabularies (§3.3), evidence retention (set per jurisdiction mapping; India's value is in `../jurisdictions/india.md`), windows for late captures (§6), and fact types (§5.1, §5.3).

**Follow-ups:**
1. Confirm redistribution licences for the MCC list, GS1 GPC and GS1 allergen codes before shipping them.
2. Confirm the India retention value with a lawyer.
