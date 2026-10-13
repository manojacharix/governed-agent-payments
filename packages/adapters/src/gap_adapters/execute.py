"""Execution on a rail (04 §2, §3): one authorised decision becomes at most one order.

Guards before any call: only an authorisation-stage `allow` executes; the rail is the one that was
approved (never switched); it must be live and offered by the merchant; a rail with no authentication
of its own needs the operator's no-auth-rail strength or a permitted standing instruction.

After an ambiguous result (timeout, error, unknown status) the executor first looks for the order in the
merchant's history. Found means success. Not found is proof of absence only when the history is
complete; only then, and only for errors the rail profile marks safe and within its limits, does it retry.
Otherwise the outcome is unknown: the hold stays and reconciliation takes over (invariant 20).
Every attempt is a signed ExecutionAttempt on the wheel's chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from gap_core.authority import RailProfile
from gap_core.clock import Clock
from gap_core.evaluate import Determination, can_override
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.money import Money
from gap_core.signatures import Signer
from gap_core.timefmt import format_ts
from gap_ledger.ledger import Ledger

from .interface import MerchantAdapter, MerchantError, MerchantTimeout, OrderView


class ExecutionRefused(RuntimeError):
    """A guard stopped execution before anything was sent to the merchant."""


@dataclass(frozen=True)
class Authorisation:
    """How the payment was authorised (02 §2): a payer approval at a strength, or a standing instruction."""

    kind: str  # payer_approval | standing_instruction
    no_auth_rail_ok: bool = False  # the approval met auth.strength.no_auth_rail (checked by the identity layer)
    overridden: frozenset = frozenset()  # payer-authored clauses overridden for this one payment (02 §2)


@dataclass
class ExecutionResult:
    outcome: str  # success | failure | unknown | awaiting_payer
    merchant_order_ref: str | None
    charged: Money | None
    attempts: list = field(default_factory=list)
    handoff: str | None = None
    needs_reconciliation: bool = False


def idempotency_key(determination_id: str) -> str:
    """One key per decision, the same on every attempt, so merchants that honour keys deduplicate retries."""
    return hash_value({"determination_id": determination_id})


class Executor:
    def __init__(self, ledger: Ledger, signer: Signer, clock: Clock, rng) -> None:
        self.ledger, self.signer, self.clock, self.rng = ledger, signer, clock, rng

    def _guard(self, det: Determination, rail: RailProfile, adapter: MerchantAdapter, auth: Authorisation) -> None:
        overridden_ok = det.outcome == "refuse" and bool(auth.overridden) and can_override(det, auth.overridden)
        if det.stage != "authorisation" or not (det.outcome == "allow" or overridden_ok):
            raise ExecutionRefused("only an authorisation-stage allow, or a refusal the payer overrode, executes")
        if rail.rail_id != det.rail_id:
            raise ExecutionRefused(f"rail {rail.rail_id} is not the approved rail {det.rail_id}; never switched without a new approval")
        if rail.status != "live":
            raise ExecutionRefused(f"rail {rail.rail_id} is {rail.status}")
        if rail.rail_id not in adapter.describe().rails_offered:
            raise ExecutionRefused(f"{adapter.describe().adapter_id} does not offer {rail.rail_id}")
        if rail.authentication == "none":
            silent_ok = auth.kind == "standing_instruction" and det.approval_mode == "silent_with_notice"
            if not (auth.no_auth_rail_ok or silent_ok):
                raise ExecutionRefused(f"{rail.rail_id} has no authentication: needs auth.strength.no_auth_rail or a standing instruction")

    def _record(self, wheel_id, det, rail, adapter_id, n, key, request_hash, status, outcome, order_ref, started, retry_of) -> dict:
        aid = new_id("exa", clock=self.clock, rng=self.rng)
        body = {
            "record_type": "execution_attempt", "attempt_id": aid, "determination_id": det.determination_id,
            "rail_id": rail.rail_id, "adapter_id": adapter_id, "idempotency_key": key, "attempt_number": n,
            "request_hash": request_hash, "response_status": status, "outcome": outcome,
            "started_at": format_ts(started), "ended_at": format_ts(self.clock.now()),
        }
        if order_ref:
            body["merchant_order_ref"] = order_ref
        if retry_of:
            body["retry_of"] = retry_of
        return self.ledger.append(wheel_id, body, self.signer, record_id=aid)

    def _find(self, adapter: MerchantAdapter, cart_ref: str, key: str, since) -> tuple[OrderView | None, bool]:
        """Look for the order in history. Returns (order, proven_absent)."""
        info = adapter.describe()
        orders, cov = adapter.list_orders(since)
        for o in orders:
            if (info.honours_idempotency and o.idempotency_key == key) or (not info.honours_idempotency and o.cart_ref == cart_ref):
                return o, False
        return None, cov.complete

    def execute(self, wheel_id: str, det: Determination, cart_ref: str, destination_ref: str, rail: RailProfile,
                adapter: MerchantAdapter, auth: Authorisation) -> ExecutionResult:
        self._guard(det, rail, adapter, auth)
        info = adapter.describe()
        key = idempotency_key(det.determination_id)
        request_hash = hash_value({"cart_ref": cart_ref, "rail_id": rail.rail_id, "destination_ref": destination_ref,
                                   "idempotency_key": key, "presented_hash": det.fetched_hash or ""})
        policy = rail.retry_policy or {}
        safe, max_retries, window = set(policy.get("safe_errors", ())), policy.get("max_retries", 0), policy.get("window_s", 0)
        first_start = self.clock.now()
        attempts, retry_of = [], None
        for n in range(1, max_retries + 2):
            started = self.clock.now()
            try:
                res = adapter.place(cart_ref, rail.rail_id, key, destination_ref)
            except (MerchantTimeout, MerchantError) as e:
                code = e.code if isinstance(e, MerchantError) else "TIMEOUT"
                found, absent = self._find(adapter, cart_ref, key, first_start - timedelta(seconds=1))
                if found is not None:  # the order exists after all: never place a second one
                    outcome = info.outcomes.get(found.status, "unknown")
                    attempts.append(self._record(wheel_id, det, rail, info.adapter_id, n, key, request_hash, f"{code}+FOUND_IN_HISTORY",
                                                 outcome, found.merchant_order_ref, started, retry_of))
                    return self._result(outcome, found, attempts, rail)
                retryable = absent and code in safe and n <= max_retries and (self.clock.now() - first_start).total_seconds() <= window
                outcome = "failure" if retryable else "unknown"
                rec = self._record(wheel_id, det, rail, info.adapter_id, n, key, request_hash, code, outcome, None, started, retry_of)
                attempts.append(rec)
                if retryable:
                    retry_of = rec["attempt_id"]
                    continue
                return ExecutionResult("unknown", None, None, attempts, needs_reconciliation=True)
            outcome = info.outcomes.get(res.status, "unknown")
            attempts.append(self._record(wheel_id, det, rail, info.adapter_id, n, key, request_hash, res.status, outcome,
                                         res.merchant_order_ref, started, retry_of))
            order = adapter.get_order(res.merchant_order_ref) if res.merchant_order_ref else None
            result = self._result(outcome, order, attempts, rail)
            result.handoff = res.handoff
            return result
        return ExecutionResult("unknown", None, None, attempts, needs_reconciliation=True)

    def recover(self, wheel_id: str, det: Determination, cart_ref: str, destination_ref: str, rail: RailProfile,
                adapter: MerchantAdapter, since) -> tuple[ExecutionResult, bool]:
        """After a crash between committing an approval and recording the merchant's answer: ask the merchant whether
        the order exists, by this decision's idempotency key (or cart), and record what is found. Never places an order.
        Returns (result, proven_absent): proven_absent means the merchant's complete history shows no such order."""
        info = adapter.describe()
        key = idempotency_key(det.determination_id)
        request_hash = hash_value({"cart_ref": cart_ref, "rail_id": rail.rail_id, "destination_ref": destination_ref,
                                   "idempotency_key": key, "presented_hash": det.fetched_hash or ""})
        started = self.clock.now()
        try:
            found, absent = self._find(adapter, cart_ref, key, since)
        except (MerchantTimeout, MerchantError):
            found, absent = None, False
        if found is not None:
            outcome = info.outcomes.get(found.status, "unknown")
            rec = self._record(wheel_id, det, rail, info.adapter_id, 1, key, request_hash, "RECOVERED+FOUND_IN_HISTORY", outcome,
                               found.merchant_order_ref, started, None)
            return self._result(outcome, found, [rec], rail), False
        status = "RECOVERED+NOT_FOUND" if absent else "RECOVERED+NOT_PROVEN"
        rec = self._record(wheel_id, det, rail, info.adapter_id, 1, key, request_hash, status, "failure" if absent else "unknown",
                           None, started, None)
        return ExecutionResult("failure" if absent else "unknown", None, None, [rec], needs_reconciliation=not absent), absent

    @staticmethod
    def _result(outcome: str, order: OrderView | None, attempts: list, rail: RailProfile) -> ExecutionResult:
        return ExecutionResult(
            outcome, order.merchant_order_ref if order else None, order.charged if order else None, attempts,
            needs_reconciliation=outcome != "success" or rail.amount_binding == "not_bound",
        )
