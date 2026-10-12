# Stress campaign, 2026-10-12

The whole infrastructure was tested two ways after Phase 9: every entry point was probed with extreme and hostile inputs, and the full flow was run under extreme synthetic profiles. Everything found is fixed and covered by tests.

## Breakages found and fixed

| # | Where | What broke | Fix |
|---|---|---|---|
| 1 | HTTP and host | About 25 malformed inputs crashed the server (500): missing fields, null or list IDs, NUL bytes, unpaired surrogates, floats and nulls in facts, deep nesting, oversized integers, a non-object proof or assertion | One strict validator in the host (`parse_request`, `text`); identity checks that proofs and assertions are objects of strings; a missing field is a 422 |
| 2 | Host | `agent_confidence` 950.9, `"950"` and `true` were silently coerced (`true` became 1) | Exactly an int from 0 to 1000; anything else is refused |
| 3 | Host and HTTP | No size limits: 1 MB intent IDs, 100 KB nonces, 10,000 facts accepted | IDs at most 128 characters, nonces 128, tokens 512, at most 32 facts of at most 4 KB each, request bodies at most 64 KB (413) |
| 4 | Flow | Merchant data that breaks the money model crashed the flow: an unknown cart or adapter, a cart mixing currencies, a total beyond 2^53, NUL in an item ID | The merchant is outside the trust boundary: these are clean refusals (`unknown_adapter`, `cart_unavailable`, `cart_invalid`), nothing recorded |
| 5 | Flow | A cart that vanished or went bad between presentation and the payer's tap crashed approval after the assertion was recorded; inside `tick()` one bad cart stopped every other payer's scheduled payments | Refused and recorded as declined by the system; the tick carries on for everyone else |
| 6 | Host (serious) | Concurrent requests corrupted each other's database transactions: the flow shares one connection and HTTP servers use a thread pool. Two simultaneous approvals both failed with 500 | Every call into the flow holds one lock per host process. Tested: four simultaneous approvals of one proposal execute it exactly once; twelve simultaneous approvals against a cap execute exactly the four that fit |
| 7 | Evaluator | A spoke's cap was skipped for a proposal in another currency. Not exploitable today (the wheel budget fails closed), but a wheel with budgets in two currencies would have bypassed spoke caps | A capped spoke has no room in a currency it has no cap for (`sys.cap_exceeded`) |
| 8 | Canonical model | NUL and unpaired surrogates were allowed in strings although Postgres jsonb cannot store NUL and UTF-8 cannot encode surrogates; nesting was unbounded (recursion crash) | Both refused in every string and key; nesting at most 32 deep |
| 9 | Identity | Enrolment accepted an unregistered OAuth client and a malformed or off-curve public key, so the payer could approve something that can never work; client registration accepted `javascript:` redirects and unbounded names | Client must be registered, key must parse as P-256; redirects must be https or loopback, names at most 200 characters |
| 9b | Identity (found by the security review of this campaign's own fix) | The new redirect check matched string prefixes, so `http://localhost.evil.example/cb`, `http://localhost@evil.example/` and `http://127.0.0.1.attacker.net/` passed as loopback | Redirect URIs are parsed: https with a host, or http to exactly 127.0.0.1, localhost or ::1; no userinfo, fragment, spaces, control characters or backslashes |
| 10 | MCP | The tool hashed the request before validating it, so a NUL byte raised inside the tool | Validated first, then hashed and signed |

Checked and found sound: agent facts never replace host facts of the same name (an agent cannot claim "no open findings" or "no recovery"); a cart exactly at the cap is allowed and one paisa over is refused; stale, future and malformed proof times are refused.

## Extreme profiles (tools/stress.py, `make stress`)

Each run: 30 payers, 1,500 events, its own throwaway Postgres, all invariants, ledger integrity and 20 evidence bundles through the open verifier.

| Profile | Pushes | Result |
|---|---|---|
| default | as Phase 7 | clean |
| hostile_merchants | 80% of merchant calls time out, fail, overcharge or substitute | clean |
| chaotic_bank | 70% of debits late, 40% with a wrong amount, 40% of orders unconfirmed, many stalled refunds | clean |
| identity_storm | device compromises and recoveries 25 times more often, 30% of proposals from an intruder | clean |
| month_boundary | starts minutes before midnight on 31 October in Kolkata, caps tight enough that the reset matters | clean |
| burst | a proposal about every minute | clean |
| whales | carts and caps near the 2^53 minor-unit ceiling | clean |
| pennies | caps of a few rupees | clean (nothing fits, nothing executes) |
| unresponsive_payer | the payer almost never answers; notices rarely delivered | clean |
| all_unverified | every merchant category claim unverified | clean (almost everything needs clarifying) |

Final sweep on the committed code: all ten profiles × two seeds (20 runs, about 29,000 proposals), plus four seeds each of month_boundary, identity_storm and burst: zero crashes, zero violations. The default 3,000-event simulation (`make simulate`) is also clean. A new invariant was added during the campaign: **a hold is placed only when the account has room** (held plus spent within the limit, per account and window), checked over every posting.

Two reported violations were in the simulator's own checker, not the layer: it ordered events by timestamp within one simulated minute, and it noticed a delayed recovery completing one event late. Both are fixed with an event sequence number and a re-check after each tick.

## Not covered (known limits)

- One host process. Proposals, challenges and tokens live in memory, so several processes behind a load balancer would need that state in Postgres first. The lock makes one process safe; it does not make several safe.
- No rate limiting per caller. That belongs at the deployment edge (gateway), not in the layer.
- The fake merchants and bank are synthetic; real adapters will fail in ways these profiles only approximate.
