# Licensing

This repository is **open core**: two licenses, split by path.

## Apache License 2.0 (open source)

| Path | What it is |
|---|---|
| `specs/` | The infrastructure specification: invariants, mechanisms, model spec, policy parameters |
| `packages/core/` (`gap-core`) | Types, Money, canonical JSON, hashing, signatures, hash chains and `evaluate()` |
| `packages/verify/` (`gap-verify`) | The open verifier for evidence bundles |

Each of these folders carries its own `LICENSE` (Apache-2.0 text) and `NOTICE`. Every Python file in them starts with `SPDX-License-Identifier: Apache-2.0`. Commercial use, modification and redistribution are allowed under the Apache-2.0 terms, including its patent grant.

**Why these parts.** The layer's promise is that every decision is provable to a party that does not trust the operator. The parties who check that proof (merchants, payment providers, banks, auditors, regulators) do so in the course of business. The specification, the decision function the verifier re-runs and the verifier itself are therefore open to them without a license negotiation.

**Boundary.** `gap-core` and `gap-verify` import no other `gap_*` package, so installing them never pulls in noncommercial code. `packages/verify/tests/test_license_boundary.py` fails if that changes.

## PolyForm Noncommercial License 1.0.0 (source available)

Everything else, under [`LICENSE.md`](LICENSE.md) at the repository root: the ledger, profile store, working state, proposal flow, identity, HTTP API and MCP server, merchant adapters, reconciliation, signers, mapping loader, simulator, evidence bundle builder, jurisdiction mappings, Tier 0 configs, research, plans and tools.

You may read, run and modify it for personal use, research, study and other noncommercial purposes. Commercial use needs written permission: get in touch through [manojachari.com](https://www.manojachari.com).

## History

Up to and including `v0.1.0`, the whole repository was under PolyForm Noncommercial 1.0.0. The Apache-2.0 grant above applies from the commit that added this file onward; earlier copies keep the license they were published under.

Copyright 2026 Manoj Achari.
