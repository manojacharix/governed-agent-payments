# Governed Agent Payments

When software spends a person's money at a merchant, record what they authorised, enforce it before money moves, and make every decision provable to a party that does not trust the operator.

This repository is **the infrastructure layer only**. It contains no product, no merchant integration and no app. Products built on the layer live in their own repositories.

**Status (12 Oct 2026):** seven specs reviewed; `07` approved. Scope: merchant commerce (an agent paying a merchant for an order). Phases 1 to 4 built 12 Oct (primitives; the evaluator; jurisdiction mappings and Tier 0 configs; the Postgres ledger, money in flight, key log and profile store). Phase 5 (merchant adapters, rails, execution) next. Build plan: `plan/build_plan.md`; model spec: `specs/19_model_spec.md`.

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
make simulate      # synthetic run: 60 payers, 5,000 proposals over 75 days, invariants checked (SEED=, PAYERS=, EVENTS=)
```

| Package | Contents |
|---|---|
| `packages/core` (`gap_core`) | Pure: Money, canonical JSON (RFC 8785), hashing, IDs, timestamps, injected clock, ES256 verification, hash chains, and `evaluate()` with the 14 rule dimensions. No I/O, no clock reads (enforced by `test_purity.py`) |
| `packages/keys` (`gap_keys`) | Signers: a local software signer for development and tests |
| `packages/ledger` (`gap_ledger`) | Postgres ledger with database-enforced chains, money in flight (holds, captures, releases, overcaptures, refunds), key log, `verify_integrity()` |
| `packages/profile` (`gap_profile`) | Postgres profile store: personal data under per-payer AES-256-GCM keys, salted commitments, retention holds, shredding |
| `packages/sim` (`gap_sim`) | Seeded synthetic workloads through the whole stack: personal and procurement payers on the shipped configs, messy settlements, adversarial inputs; a shadow ledger and invariant checks |
| `packages/mapping` (`gap_mapping`) | Loads jurisdiction mappings (`mappings/`) and Tier 0 configs (`tier0/`) strictly into core types |

| Configuration | Contents |
|---|---|
| `mappings/in/v0.1.0.yaml` | India, `status: interpretation`: UPI intent, merchant wallets, cash on delivery, UPI Reserve Pay, UAP placeholder; INR only; silent ceiling unset; 3-year retention |
| `mappings/uk/v0.1.0.yaml` | UK, `status: reference`: Faster Payments, cards, Open Banking VRP; GBP only |
| `tier0/examples/` | Two operators on the same core: a personal ordering service and a B2B procurement policy (matched PO, approved supplier, no silence), plus a UK personal example |

Vocabulary terms such as `std:mcc:7995` are referenced by code only. The ISO 18245 and GS1 code lists themselves are not shipped until their redistribution licences are confirmed.
