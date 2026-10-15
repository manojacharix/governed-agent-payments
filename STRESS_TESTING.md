# Stress testing overview

How this layer is tested beyond its unit tests, what it is checked against, how to run it, and where it stands. The detailed record of the October 2026 campaign (every breakage found and how it was fixed) is in [`plan/stress_campaign.md`](plan/stress_campaign.md).

## Two kinds of stress

| | Extreme inputs | Extreme synthetic workloads |
|---|---|---|
| Question it answers | Can anything an agent, a payer's device or a merchant sends crash the layer, slip past a check, or leave half-written state? | Over weeks of messy, hostile activity, does the layer ever break one of its promises? |
| Where | `packages/api/tests/test_extremes.py` (96 cases), plus input cases in the MCP, identity and canonical tests | `packages/sim/src/gap_sim/flow_sim.py` with the profiles in `PROFILES`, run by `tools/stress.py` |
| How it runs | Every `make test` and every CI run | `make stress` on demand (about 4 minutes, parallel, each run on its own throwaway Postgres) |

## Extreme inputs: what is thrown at the layer

- **Agent requests over HTTP and MCP:** missing, null, wrong-type and empty fields; 1 MB strings; NUL bytes, newlines, unpaired surrogates, right-to-left and emoji text; floats, booleans and strings where integers belong; numbers far beyond 2^53; facts that are malformed, repeated, 5,000 levels deep, 10 KB each or 33 at a time; fields the model does not know; bodies over 64 KB.
- **Credentials:** empty, oversized and wrong-shaped bearer tokens and signed proofs; reused and oversized nonces; creation times that are garbage, in 1970, an hour ahead or in the year 9999.
- **The payer's device:** unknown actions, assertions that are strings, lists or empty, malformed override lists.
- **Enrolment and OAuth:** unregistered clients, keys that are not keys or not on the curve, oversized names, and redirect addresses that only look like loopback (`localhost.evil.example`, `localhost@evil.example`, `127.0.0.1.attacker.net`) or are `javascript:`, `data:` or remote plain http.
- **The merchant, which sits outside the trust boundary:** carts that do not exist, adapters that do not exist, carts mixing currencies, totals beyond 2^53, NUL bytes in item IDs, and carts that vanish between being shown to the payer and the payer's tap.
- **Boundaries:** a cart exactly at the cap (allowed) and one paisa over (refused); agent claims that try to overwrite facts only the host may supply.
- **Concurrency:** four simultaneous approvals of one proposal (it executes exactly once) and twelve simultaneous approvals against a cap with room for four (exactly four execute).

**The rule every case checks:** a bad input gets a clean 4xx with nothing recorded, bad merchant data ends in a refusal rather than a crash, and concurrent requests cannot interleave.

## Extreme workloads: the profiles

Each run has 30 payers (personal and business procurement, on the shipped India configurations) and 1,500 events over about 45 simulated days. The events include agents proposing, payers approving, declining, overriding or ignoring, silent weekly repeats, merchants and a bank misbehaving, refunds, device compromises, recoveries and intruders.

| Profile | What it pushes to the edge |
|---|---|
| `default` | The baseline mix |
| `hostile_merchants` | 80% of merchant calls time out, fail, overcharge or substitute |
| `chaotic_bank` | 70% of debits arrive late, 40% with the wrong amount, many orders unconfirmed, many refunds stall |
| `identity_storm` | Device compromises and recoveries 25 times more often; 30% of proposals come from an intruder |
| `month_boundary` | Starts minutes before midnight on 31 October in Kolkata, with caps tight enough that the monthly reset matters |
| `burst` | A proposal about every minute, so duplicate windows, notices and expiries overlap |
| `whales` | Carts and caps near the 2^53 minor-unit ceiling |
| `pennies` | Caps of a few rupees, so almost nothing fits |
| `unresponsive_payer` | The payer almost never answers, and notices are rarely delivered |
| `all_unverified` | Every merchant category claim is unverified |

## The invariants every run is checked against

1. Ledger integrity: every record's signature, hash chain and key validity verify.
2. An enrolled agent is never rejected, and an unenrolled one is never accepted.
3. Nothing executes while the payer is suspended (compromised device) or recovering.
4. A proposal blocked by an operator or jurisdiction rule never executes; the payer can override only rules they wrote.
5. No money moves without an approval: every posting traces to an approved, overridden or silently proceeded determination.
6. Money is held only when there is room: after every hold, held plus spent stays within the cap or budget for that account and month.
7. No balance goes negative, and no payment is refunded beyond what was captured.
8. Money still held always has an open reconciliation finding, or an attempt waiting on the payer, to explain it.
9. Every merchant order traces back to a proposal, and no cart is ordered twice.
10. Orders placed directly at the merchant are labelled external, and nothing else is.
11. Evidence bundles for a sample of payments, for both the payer and the merchant, pass the open verifier, and each decision replays byte for byte.

## Running it

```
make test                                            # includes all extreme-input cases
make stress                                          # all ten profiles, two seeds each
uv run python tools/stress.py 4 1500 burst whales    # chosen profiles, four seeds, 1,500 events
uv run python tools/simulate.py 42 40 3000           # one long default run with a full summary
```

`tools/stress.py` exits non-zero if any run crashes or breaks an invariant, and prints the first violations of each failing run. Runs are seeded, so the same profile and seed always reproduce the same run.

## Where it stands (2026-10-12)

- **Ten breakages were found and fixed,** plus one more in a fix, caught by a security review. The most serious was concurrent requests corrupting each other's database transactions, now prevented by serialising each host process. Details are in `plan/stress_campaign.md`.
- **Final sweep on the merged code:** all ten profiles with two seeds each (20 runs, about 29,000 proposals), plus four seeds each of `month_boundary`, `identity_storm` and `burst`. Zero crashes, zero violations.
- **Durable state (2026-10-13):** the same ten profiles × two seeds on Postgres working state: zero violations, results identical to memory. Restart, cross-process race, concurrent-scheduler and crash tests pass (including a crash in each window between committing an approval and recording the merchant's answer, with recovery after a grace period), and mutation checks confirm they fail if the per-payer lock, the version check or the lock's survival across a checkpoint is removed.
- **575 tests pass in memory mode; the flow, API, identity and evidence suites (182 tests) pass again on Postgres state in CI.**

## Audit fixes (2026-10-15)

The 14 October audit reproduced loopholes that the invariants above did not catch, because the simulator never tried them. All are closed, each with a regression test (`packages/flow/tests/test_audit_2026_10_14.py`, plus three in `packages/evidence/tests/test_evidence.py`):

1. The cited merchant is now the one the cart is at (reported by the adapter); a request citing another is refused, so an allowlist cannot be bypassed through another adapter or marketplace seller.
2. A silent repeat drifts (and falls back to a tap) if it goes through any adapter other than the instruction's.
3. Rolling windows accumulate: usage counts each payment by when its hold was placed.
4. Velocity and aggregate rules whose window differs from the cap's are counted exactly, instead of always refusing.
5. A payment holds against the cap and budget in its own currency.
6. A raw signature's strength comes from the user verification recorded with the key at enrolment, never from an unsigned claim.
7. Agent-supplied extras must be non-negative and in the cart's currency.
8. Evidence bundles carry the device's signature and challenge, and the open verifier checks the payer's consent from them (`payer_signatures`): every payer approval in a payer or regulator bundle must point to a disclosed assertion signed by a surface key in force, over exactly what was approved (and, for an override, the clauses overridden); silence is accepted only on a silent-mode decision; passkey relying parties can be pinned.

After the fixes: 608 tests pass in memory mode, 215 again on Postgres state; all ten profiles × two seeds: zero crashes, zero violations, every sampled bundle's payer signatures verify.

## What it does not cover

- **Real separate machines.** Since 2026-10-13 working state can live in Postgres (`gap_state`), and tests run several host processes against one database: restarts, approvals racing across processes, schedulers ticking at once, a crash at the point of no return. `tools/stress.py --durable` runs every profile on Postgres state (all clean, identical results to memory). Those "processes" share one machine and one Postgres; network partitions and database failover are not simulated.
- **Rate limiting.** Per-caller limits belong at the deployment edge (a gateway), not in the layer.
- **Real merchants and banks.** The fakes are synthetic. Real adapters will fail in ways these profiles only approximate.
- **Performance and load.** These runs test correctness under stress, not throughput or latency targets.
