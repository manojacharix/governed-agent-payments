# Governed Agent Payments

When software spends a person's money at a merchant, record what they authorised, enforce it before money moves, and make every decision provable to a party that does not trust the operator.

This repository is **the infrastructure layer only**. It contains no product, no merchant integration and no app. Products built on the layer live in their own repositories.

**Status (12 Oct 2026):** seven specs reviewed; `07` approved. Scope: merchant commerce (an agent paying a merchant for an order). Phase 1 (primitives) built 12 Oct; Phase 2 (the evaluator) next. Build plan: `plan/build_plan.md`; model spec: `specs/19_model_spec.md`.

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

Requires [uv](https://docs.astral.sh/uv/).

```
uv sync            # install the workspace
make test          # all tests
make demo-phase1   # sign a chain, tamper with it, see verification fail
```

| Package | Contents |
|---|---|
| `packages/core` (`gap_core`) | Pure: Money, canonical JSON (RFC 8785), hashing, IDs, timestamps, injected clock, ES256 verification, hash chains. No I/O, no clock reads (enforced by `test_purity.py`) |
| `packages/keys` (`gap_keys`) | Signers: a local software signer for development and tests |
