# Contributing

Thank you for looking. This repository is open core (see [`LICENSING.md`](LICENSING.md)), and how a contribution is accepted depends on which side of the line it touches.

## Apache-2.0 parts: pull requests welcome

`specs/`, `packages/core` (`gap-core`) and `packages/verify` (`gap-verify`).

- **Inbound = outbound.** A contribution to these folders is made under the Apache License 2.0, as its section 5 already provides, including the patent license in section 3. No contributor license agreement.
- **Sign off every commit** with the [Developer Certificate of Origin](https://developercertificate.org/): `git commit -s` adds the line `Signed-off-by: Your Name <you@example.com>`, certifying you have the right to submit the work under the project's license. The `dco` check fails a pull request with any unsigned commit; `git rebase --signoff main` fixes an existing branch.
- **Keep the boundary.** Apache-2.0 packages must not import any other `gap_*` package. `packages/verify/tests/test_license_boundary.py` enforces it, and new Python files there start with `# SPDX-License-Identifier: Apache-2.0`.

## PolyForm Noncommercial parts: issues only, for now

Everything else. Bug reports, questions and design discussion are welcome as issues. Outside pull requests are not accepted here yet, because this code is also offered under commercial licenses and a contribution under the noncommercial license could not be included in them. If that changes, it will be through a short contributor license agreement, announced here.

## Before you open a pull request

- `uv sync` then `make test` (PostgreSQL binaries required; see the README).
- One change per pull request, with a test that fails without it.
- No merchant, device, amount, threshold or country value in `specs/` (README, "Rules for this repository").
