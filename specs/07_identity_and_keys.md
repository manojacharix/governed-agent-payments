# 07 · Identity and Keys

**Problem:** every record in `01`–`06` is "signed". Signed by whom, how does anyone know that key belongs to that party, and what happens when a key is lost or stolen?
**Status:** drafted and **approved 9 Oct 2026**, infrastructure-only. Recovery-key amendment (§2 role, §3 recovery-only key, wheel suspended during a delayed recovery, zero delay allowed with a recovery key) approved the same day.
**Invariants referenced:** 2, 7, 21–23, 25–28 (`README.md`). **Parameters:** `auth.strength.enrol_surface`, `auth.strength.enrol_caller`, `auth.strength.recovery`, `identity.*`, `keys.*`.

---

## 1. Principals

Every actor that signs, proposes or authorises is a **principal**, and a principal is known only by the keys enrolled for it.

| Principal | Signs | Enrolled by |
|---|---|---|
| **Trusted Surface** | Payer actions: authorisations, overrides, wheel changes, enrolments | The payer (first surface: bootstrap, §3) |
| **Caller** | Proposals and change requests | The payer, on a Trusted Surface |
| **Wheel host** | Receipts, Determinations, postings, findings, chain heads | The operator's or host's own key ceremony; published in the key log |
| **Operator** | Tier 0 versions, suspensions, facts it captures | As above |
| **Fact source** | Facts, where the source can sign at all | Registered by the operator; most sources cannot (§6) |
| **Evaluator release** | Nothing at runtime; identified by its build digest | The infrastructure maintainers (§7) |

The payer has **no key of their own** separate from their Trusted Surfaces: a payer is the set of surfaces enrolled under one `payer_ref`. The infrastructure never verifies legal identity or account ownership; account control is proven by each rail's own authentication (`04`).

---

## 2. The key log

One append-only, hash-chained log per wheel host, readable by any party that needs to verify a signature from that host's domain. Every signature in `01`–`06` names a `key_id` in it.

```
key_id, principal_role (surface|recovery_key|caller|host|operator|source|evaluator_release),
principal_ref (opaque; payer surfaces and callers under payer_ref, never personal data),
public_key, algorithm, protection_class (hardware_attested|hardware|software),
valid_from, valid_until, status active|retired|revoked|compromised,
compromised_since?, enrolled_by (event_id), previous_hash, signature
```

- **Rotation:** a new key is enrolled by an event signed with the current key of the same principal, or by the enrolling authority in §1. The old key moves to `retired`; records it signed stay valid.
- **Revocation:** `revoked` keys sign nothing after `valid_until`; past signatures stay valid.
- **Compromise:** `compromised` with `compromised_since`. Nothing is deleted. Verifiers flag every record signed by that key after `compromised_since` as `signed_after_compromise`, and reconciliation opens a finding for each payment it touches (`05`). Compromise is also a **system suspension trigger** (`01` §5): a compromised **surface** key suspends the wheel, and resuming needs another, uncompromised surface (or recovery, §3); a compromised **caller** key suspends that caller's enrolment, so spokes bound only to it stop working until the payer enrols a new key.
- **Algorithms** are a parameter (`keys.algorithms`), so a deployment can change algorithm without a schema change.

---

## 3. Trusted Surfaces

A Trusted Surface is a payer-controlled application instance holding a signing key that can be used **only after user verification on the device** (the surface's platform enforces this, not the application code). This is what separates "the payer approved" from "something operating the payer's device tapped approve".

### Enrolment
| Case | Requires |
|---|---|
| First surface (bootstrap) | `auth.strength.enrol_surface` through the operator's onboarding; creates the wheel's `payer_ref` |
| Additional surface | Approval from an existing active surface at `auth.strength.enrol_surface` |
| Recovery key (amendment approved 9 Oct) | Enrolled from an active surface at `auth.strength.enrol_surface`. **Recovery only**: it may approve enrolling a new surface during recovery, and nothing else; it never authorises a payment, an override or a wheel change, because it cannot present anything |
| Recovery (all surfaces lost) | `auth.strength.recovery`; then `identity.recovery_delay` during which any old surface or recovery key can cancel, and the wheel is suspended (amendment approved 9 Oct); on completion every old surface key is revoked. A deployment may set `identity.recovery_delay` to zero for a recovery that includes a recovery key. **Recovery never widens authority**: the wheel keeps its rules and spokes |

At enrolment the surface's `protection_class` is recorded, using platform attestation where available. A surface below `identity.surface.min_assurance` cannot enrol.

### `SurfaceAssertion`
Every payer action in `01` (`WheelEvent` with `initiated_by = payer`), every authorisation and every override in `02` carries one:
```
assertion_id, surface_key_id, challenge (issued by the host for this presentation),
action (authorise|decline|edit|override|wheel_event|enrol|recover),
subject_hash (presented_hash, or the hash of the presented wheel terms),
user_verification (class on the jurisdiction's authentication-strength scale),
auth_strength_achieved, occurred_at, signature
```
- The host issues a fresh `challenge` with every presentation; an assertion without a matching unexpired challenge is refused (no replayed approvals).
- `auth_strength_achieved` is derived from `protection_class` + `user_verification` using the jurisdiction's scale, not stated by the surface.

---

## 4. Callers

A caller is anything that proposes: an agent, an MCP client, a scheduler. **Identity is a credential the payer enrolled, never a name the caller declared.**

### Enrolment
A caller asks to connect; the host returns a pending enrolment; nothing is active until the payer approves it on a Trusted Surface at `auth.strength.enrol_caller`. (The same "propose, then human ratifies" shape as wheel changes.)

```
Caller: caller_id, payer_ref, credential {key_id | grant_ref}, assurance (§4.1),
claimed {client_name, client_version, …} (recorded, never trusted),
valid_until (≤ identity.caller.max_validity), status, enrolled_by (assertion_id)
```
Spoke `bindings.callers` (`01` §2) list `caller_id`s, not keys, so key rotation never breaks a spoke.

### 4.1 Assurance

Not every caller can hold a key. The caller's assurance is recorded and becomes an **injected, verified fact** in every Determination, so rules can act on it (dimension 1 or 12) and strictest-wins applies.

| Assurance | Proof per proposal | Typical case |
|---|---|---|
| `key_bound` | Proposal signed with the caller's enrolled key, or a sender-constrained token with proof of possession | A caller that manages its own key |
| `session_bound` | A bearer token from a grant the payer approved on a Trusted Surface | Most current agent clients |
| `claimed` | Nothing beyond what the caller says | **Never accepted for a proposal**; recorded on refusals only |

`identity.caller.min_assurance` sets the floor per approval mode and per dimension (for example, a deployment may require `key_bound` to cite a `silent_with_notice` spoke).

### 4.2 Proposal authentication
Every proposal carries `caller_id`, `intent_id`, a nonce and a timestamp, covered by the caller's proof. Older than `identity.proof_max_age`, or a reused nonce: refused as `no_authority`.

---

## 5. Trust domains

Each of host, operator, surface and caller signs with **its own keys**; no party ever holds another's private key. What a deployment guarantees depends on whether host and operator are separate.

| Guarantee | Separate host | Operator-hosted (`host_ref` = operator) |
|---|---|---|
| Operator cannot forge a payer approval | Yes (surface keys) | **Yes** (surface keys never leave the device) |
| Operator cannot forge a caller's proposal | Yes for `key_bound`; no for `session_bound` | Same |
| Operator cannot rewrite history unnoticed | Yes | **Only with external anchoring** (below) |
| Evaluation independent of the operator | Yes | No: replay still proves consistency, not independence |
| Budget shared across operators | Yes | No: one operator only |

**Anchoring.** Every `keys.anchor_interval`, the host publishes its current ledger and key-log chain heads to each witness in `keys.anchor_witnesses`. Every notice and receipt delivered to a payer's Trusted Surface also carries the current chain head, so **the payer's own device is a witness**: an operator who rewrites history cannot make it match heads already on payer devices.

An operator-hosted deployment must state, in its Tier 0, which column it is in.

---

## 6. Fact attestation

A Determination is only as trustworthy as its facts. Every fact (`02` §4) records **who vouches for it**:

| `attestation` | Meaning | What the verifier can prove |
|---|---|---|
| `source_signed` | The source signed the value | The source said it |
| `capturer_signed` | The capturing party signed the request, the raw response hash and the time | **Only that the capturer says the source said it** |
| `payer_presented` | The value was in what the payer saw and approved (covered by `subject_hash`) | The payer saw it |
| `injected` | Time or a conversion rate supplied to `evaluate()` | The host supplied it |

```
attestation {class, attested_by key_id, capturer (host|operator), raw_hash?}
```

- The host captures facts itself wherever it can reach the source directly; facts passed in by the operator are recorded as `capturer = operator`.
- `facts.verified_sources` (`02`) is separate: it says which sources may **clear** a check. Attestation says who vouches for the value. A fact needs both to clear.
- In `explicit_tap`, the cart, total and rail are `payer_presented` as well as captured, so the payer's own assertion backs them. The trust gap is in facts the payer did not see (item attributes, account facts) and in silent execution.
- Evidence packs (`06`) state, per fact, its attestation; a pack never claims more than its weakest relevant attestation supports. A value later proven false is the divergence category `fact_misreported`, pointing to the attesting party.

---

## 7. Evaluator releases

"The decision reproduces" means something only if anyone can run the exact evaluator that made it.

- `evaluator` version in every Determination is the **digest of a reproducible build**, not a label.
- Each release is published with its source and build recipe and signed by an `evaluator_release` key in the key log.
- The open verifier (`06`) runs the evaluator matching the digest; if the digest has no published release, it reports `evaluator_unpublished` and does not count the replay as proof.

---

## 8. Data keys

Personal data stays out of the ledger (invariant 21); the ledger holds **commitments**.

- Each payer has a **data key** in the profile store. Personal values are encrypted under it.
- A commitment is a hash of the value **plus a random per-commitment salt**, with the salt stored encrypted under the data key. Without the salt, a commitment cannot be brute-forced even when the value space is small (addresses, item names).
- **Crypto-shredding** (`06` §5) destroys the data key; salts become unreadable and commitments can never be opened, while every chain still verifies. A retention hold keeps the key in a `held` state with its legal basis recorded.

---

## 9. Tests

- A proposal from an unenrolled, expired, suspended or `claimed`-only caller is always refused.
- A proposal below `identity.caller.min_assurance` for its spoke's approval mode is refused.
- A replayed proposal (reused nonce or stale timestamp) is refused.
- A `SurfaceAssertion` with a missing, reused or expired challenge never authorises.
- No assertion from a revoked surface key is accepted after revocation.
- Recovery never widens any permitted set; any old surface or recovery key can cancel it within `identity.recovery_delay`.
- A recovery key never authorises a payment, an override or a wheel change.
- No payment executes on a wheel while a delayed recovery is pending.
- Rotating a caller's key never invalidates a spoke binding.
- The verifier flags every record signed after `compromised_since`.
- An operator-hosted deployment cannot rewrite a ledger entry without mismatching a chain head held by a witness or a payer surface.
- Every fact in a Determination has an attestation; a pack's claims never exceed it.
- A Determination whose evaluator digest has no published release is reported `evaluator_unpublished`.
- After shredding, no commitment can be opened and every chain still verifies.

---

## 10. Decisions (approved 9 Oct 2026)

| # | Decision |
|---|---|
| D1 | A principal is its enrolled keys; self-declared names are recorded as claimed and never trusted |
| D2 | Only the payer enrols surfaces and callers, on a Trusted Surface; first surface by bootstrap; recovery is delayed, cancellable and never widens; a recovery-only key may approve a new surface and nothing else, and allows recovery without delay; the wheel is suspended during a delayed recovery |
| D3 | Surface keys usable only after on-device user verification; every payer action is a challenge-bound `SurfaceAssertion` |
| D4 | Caller assurance (`key_bound` / `session_bound`) is a verified fact rules can act on; `claimed` never proposes |
| D5 | One key log per host; rotate, revoke and compromise never delete, they flag |
| D6 | Operator-hosted deployments declare it; chain heads anchored to witnesses and payer surfaces |
| D7 | Every fact carries an attestation; evidence never claims more than it supports; new divergence `fact_misreported` |
| D8 | Evaluator identified by reproducible build digest; unpublished digests do not count as proof |
| D9 | Salted commitments under a per-payer data key; shredding the key is erasure |
