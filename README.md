# Governed Agent Payments

When software spends a person's money at a merchant, record what they authorised, enforce it before money moves, and make every decision provable to a party that does not trust the operator.

This repository is **the infrastructure layer only**. It contains no product, no merchant integration and no app. Products built on the layer live in their own repositories.

**Status (12 Oct 2026):** seven specs reviewed; `07` approved. Scope: merchant commerce (an agent paying a merchant for an order). Phases 1 to 6 built (primitives; the evaluator; jurisdiction mappings and Tier 0 configs; the Postgres ledger, money in flight, key log and profile store; merchant adapters and safe execution; the proposal flow, reconciliation and evidence with an open verifier). Phase 7 (identity) built: challenge-bound approvals, enrolled agents with proofs, recovery and compromise handling. Phase 8 built: the HTTP API and the governance MCP server over one host service, with the procurement operator running through the same API. This is the publication gate (publishing stays Manoj's decision; the LinkedIn embargo holds). Phase 9 (readable evidence page and `make demo`) next. Build plan: `plan/build_plan.md`; model spec: `specs/19_model_spec.md`.

---

## Map

```
governed-agent-payments/
├── specs/            the infrastructure: no merchant, device, product, amount or country values
│   ├── README.md     four layers, parties, scope, 28 invariants
│   ├── 01_authority.md … 07_identity_and_keys.md
│   ├── 19_model_spec.md        exact fields, canonical form, chains, storage (code is written against it)
│   └── policy_parameters.md   every configurable value (no defaults baked in)
├── plan/             build_plan.md: phases 1 to 9, infrastructure only
├── jurisdictions/    layer 3: country law, rails and schemes, read as mapping data (India, UK)
└── research/         18 silence and approval rules (India), 21 NPCI UAP, 23 AP2 / ACP / UK consent review
```

Document numbers are permanent identifiers: "`07`" means `specs/07_identity_and_keys.md` wherever it is cited.

## Reading order

1. `specs/README.md`, then `01` to `07`, then `policy_parameters.md`.
2. `jurisdictions/india.md`.
3. `research/` as needed.

## Rules for this repository

- **No product, merchant, device, amount, threshold or country value in `specs/`.** Country values go in `jurisdictions/`; example values go in Tier 0 example configs.
- **No merchant- or app-specific code or data.** Merchant adapters here are generic interfaces and test fakes only.
- **`.env` holds secrets and is git-ignored.**

## Development

Requires [uv](https://docs.astral.sh/uv/) and PostgreSQL binaries (`initdb`, `pg_ctl`; e.g. `brew install postgresql@17`). Tests start a throwaway cluster in a temp folder; set `GAP_TEST_PG_DSN` to use an existing server instead.

```
uv sync            # install the workspace
make test          # all tests
make demo-phase1   # sign a chain, tamper with it, see verification fail
make evaluate      # the evaluator on six situations
make mappings      # one evaluator, India / UK mappings and two operators swapped underneath
make ledger        # Postgres ledger: payments, an overcharge, erasure, and a database admin's edit caught
make execute       # ambiguous merchant responses (timeouts, errors) never become a second order
make evidence      # one payment from request to proof; the open verifier catches an edited bundle
make identity      # strangers, replays and reused approvals refused; backup key cannot pay; recovery to a new phone
make interfaces    # an agent over MCP proposes, the payer's device approves over HTTP
make simulate      # synthetic run of the full flow: 40 payers, 3,000 proposals over 60 days, invariants and evidence checked (SEED=, PAYERS=, EVENTS=)
```

| Package | Contents |
|---|---|
| `packages/core` (`gap_core`) | Pure: Money, canonical JSON (RFC 8785), hashing, IDs, timestamps, injected clock, ES256 verification, hash chains, and `evaluate()` with the 14 rule dimensions. No I/O, no clock reads (enforced by `test_purity.py`) |
| `packages/keys` (`gap_keys`) | Signers: a local software signer for development and tests |
| `packages/ledger` (`gap_ledger`) | Postgres ledger with database-enforced chains, money in flight (holds, captures, releases, overcaptures, refunds), key log, `verify_integrity()` |
| `packages/profile` (`gap_profile`) | Postgres profile store: personal data under per-payer AES-256-GCM keys, salted commitments, retention holds, shredding |
| `packages/adapters` (`gap_adapters`) | Merchant adapter interface, two fake merchants of different shapes (contract-tested), and the executor: guards, prove-before-retry, signed execution attempts |
| `packages/recon` (`gap_recon`) | Reconciliation sweeps: settle on consistent evidence, all nine finding kinds, bank feed, refunds, payer acceptance |
| `packages/flow` (`gap_flow`) | The proposal flow: proven callers, host-captured carts, stored replayable inputs, challenge-bound approve / override / decline, expiry, silent repeats after notice, stage 2, hold, execute, reconcile |
| `packages/evidence` (`gap_evidence`) | Evidence bundles with role-scoped disclosure and divergence, release log, and the open verifier |
| `packages/identity` (`gap_identity`) | Challenges, surface assertions (ES256 or WebAuthn) with strength from the jurisdiction's scale, approval-gated enrolment, OAuth 2.1 + PKCE and signed proofs for agents, replay protection, recovery questions, recovery, compromise and suspension |
| `packages/api` (`gap_api`) | One host service; the HTTP API (FastAPI) and the governance MCP server (MCP 2.x) over it. Agents propose, read their own proposals and spokes, request changes and hand off; only the payer's device approves |
| `packages/sim` (`gap_sim`) | Seeded synthetic workloads through the whole stack: personal and procurement payers on the shipped configs, messy settlements, adversarial inputs; a shadow ledger and invariant checks |
| `packages/mapping` (`gap_mapping`) | Loads jurisdiction mappings (`mappings/`) and Tier 0 configs (`tier0/`) strictly into core types |

| Configuration | Contents |
|---|---|
| `mappings/in/v0.1.0.yaml` | India, `status: interpretation`: UPI intent, merchant wallets, cash on delivery, UPI Reserve Pay, UAP placeholder; INR only; silent ceiling unset; 3-year retention |
| `mappings/uk/v0.1.0.yaml` | UK, `status: reference`: Faster Payments, cards, Open Banking VRP; GBP only |
| `tier0/examples/` | Two operators on the same core: a personal ordering service and a B2B procurement policy (matched PO, approved supplier, no silence), plus a UK personal example |

Vocabulary terms such as `std:mcc:7995` are referenced by code only. The ISO 18245 and GS1 code lists themselves are not shipped until their redistribution licences are confirmed.
