# Build Plan

**Status:** written 9 Oct 2026. Builds the infrastructure in `../specs/` and nothing else: no product, no merchant integration, no app. Where this plan and a spec disagree, the spec wins.

**Progress:** Phases 1 to 9 done (publication gate reached; publishing is Manoj's call), with a synthetic-workload harness (`make simulate`) that each later phase extends. **Next action:** Phase 5 (merchant adapters, rails, execution).

---

## 1. Principles

- **TDD throughout;** every phase ends with a runnable demo.
- **The specs' tests are the acceptance tests.** Each phase lists the spec sections whose "Tests" it must pass.
- **Purity is enforced, not hoped for.** `evaluate()` has no I/O, clock, database or keys; an import-graph test fails the build otherwise.
- **No policy value outside configuration.** Thresholds and limits come from Tier 0 configs and jurisdiction mappings; a test fails the build if code defines one.
- **No personal data in the ledger.** A schema test rejects free-text columns in ledger tables.
- **Merchants are fakes.** Two fake merchants with deliberately different shapes prove the adapter interface; no real merchant is integrated in this repository.

## 2. Repository layout (target)

```
governed-agent-payments/
├── specs/  jurisdictions/  research/  plan/
├── packages/
│   ├── core/        PURE: types, Money, canonical JSON, hashing, evaluate()
│   ├── keys/        signer interface; local signer (dev, tests) and KMS signer
│   ├── ledger/      Postgres: chained records, postings, key log, verify_integrity()
│   ├── profile/     Postgres: encrypted personal data, commitments, shredding
│   ├── mapping/     loads jurisdiction mappings and Tier 0 configs
│   ├── adapters/    merchant adapter interface, fake-a, fake-b; rail profiles
│   ├── recon/       source adapters, sweeps, findings
│   ├── identity/    enrolment, challenges, assertions, callers, recovery
│   ├── evidence/    bundle builder + open verifier (standalone)
│   ├── api/         HTTP API
│   └── mcp/         governance MCP server
├── mappings/in/v0.1.0.yaml   mappings/uk/v0.1.0.yaml
├── tier0/examples/           personal operator, B2B procurement operator
└── tools/reference_surface/  a software Trusted Surface for tests and demos
```

**Stack:** Python with `uv`, `pydantic`, `pytest`, `hypothesis`; `cryptography` (ES256); an RFC 8785 canonicaliser; `psycopg` and Postgres in Docker; `argon2-cffi`; a WebAuthn verification library.

## 3. Phases

**Phase 1: model spec and primitives.** `19` first. Then `Money`, canonical JSON (with the thousand-permutation test), hashing, ES256 sign and verify, typed IDs, injected clock, the hash-chain primitive.
*Demo:* sign a record, mutate one byte, verification fails.

**Phase 2: authority and the evaluator** (`01`, `02`). Wheel, spoke and rule types for all 14 dimensions; the chain intersection; caps and budgets as inputs; approval mode, strictest wins; facts with attestation; clarify; duplicate guard; `intent_conflict`; every failing clause with author and overridable; overrides. Must pass `01` §9 and `02` §6 as property tests.
*Demo:* `make evaluate` on allowed, near-cap, refused (k clauses) and conflicting proposals.

**Phase 3: jurisdiction mappings** (`../jurisdictions/`). Mapping schema from `19`; `mappings/in` (`status: interpretation`) and `mappings/uk` (`status: reference`); authentication-strength scales; rail profiles with `amount_binding`. Same proposal evaluated under both mappings.
*Demo:* one proposal, two jurisdictions, two determinations.

**Phase 4: ledger, money in flight, key log, profile store** (`03`, `07` §2 and §8). Insert-only Postgres; per-wheel chains; postings and derived balances; one hold per wheel at a time; key log with rotation, revocation and compromise; per-payer data keys, salted commitments, shredding. Must pass `03` §4 under concurrency.
*Demo:* tamper with a row, `verify_integrity()` fails; shred a payer, integrity still passes.

**Phase 5: adapters, rails, execution** (`04`). Merchant adapter interface; `fake-a` (pending-then-confirm, itemised fees) and `fake-b` (immediate, flat fee); rail profiles; `ExecutionAttempt`; prove no order before retry; never switch rail; `pending_outcome`. Must pass `04` §5, including the order-then-timeout fake.
*Demo:* a timeout that created an order never produces a second order.

**Phase 6: proposal flow, reconciliation, evidence** (`02` stage 2, `05`, `06`). Proposal lifecycle and resolutions; stage-2 re-fetch and hash compare; standing instructions with slot IDs and worst-case holds; reconciliation sweeps and every finding kind; open findings forcing a tap; evidence bundle, open verifier, role-scoped disclosure, primary and contributing divergence. Approvals come from the reference surface. Must pass `05` §6 and `06` §6.
*Demo:* drive every resolution and finding, then verify the bundle with the standalone verifier.

> **Checkpoint.** After Phase 6 the layer decides, holds, executes, reconciles and proves. Everything after makes it usable by others.

**Phase 7: identity** (`07`). Challenges; surface enrolment (reference surface, attestation hook for real platforms); `SurfaceAssertion` verification; caller enrolment with OAuth 2.1 + PKCE and dynamic client registration, DPoP for `key_bound`; proposal proofs and replay window; recovery-only keys; recovery with delay, suspension and cancel. Must pass `07` §9.
*Demo:* enrol a caller, approve with the reference surface, lose the surface, recover with the recovery key.

**Phase 8: interfaces.** HTTP API over Phases 2 to 7; governance MCP server (`propose_order`, `get_determination`, `list_proposals`, `proceed_to_pay` as a hand-off, `request_change`, read-only spoke views). Second operator example (B2B procurement) running through the same API.
*Demo:* an MCP client proposes against `fake-b`; the reference surface approves.

> **Publication gate** (when Manoj decides to publish): after Phase 8. The LinkedIn embargo on this topic holds until Manoj releases it.

**Phase 9: evidence rendering and demo.** Readable timeline generated from a bundle and labelled a rendering; worked example; `make demo` with golden files.

## 4. Not in this repository

Any product or app; real merchant integrations; device-specific surfaces (they implement the surface contract in their own repositories); AP2 and ACP adapters (after the core, `04` D3); a separately operated wheel host; external anchor witnesses; multi-currency beyond carrying the currency field.

## 5. Open decisions

| Decision | Needed by |
|---|---|
| Hosting provider and region | First deployment (development uses local Docker) |
| India `evidence.retention` | Phase 3 value; Phase 4 shred timing |
| Ledger history when a wheel moves hosts (`19` §15) | Before Phase 6 |
| Item category taxonomy and attribute vocabulary source (`19` §15) | Phase 2 |
