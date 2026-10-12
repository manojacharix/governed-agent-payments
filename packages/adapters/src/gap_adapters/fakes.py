"""Two fake merchants with deliberately different shapes (R3). No real merchant is integrated here.

fake-a: pending-then-confirm placement (the payer completes payment on the rail), itemised fees,
        honours idempotency keys, no cancel, sees only the last few orders.
fake-b: immediate placement, one flat fee, ignores idempotency keys (a blind retry duplicates the order),
        supports cancel, complete history.

Both take a script of behaviours for place(): "ok", "timeout_after_create", "timeout_before_create",
"decline", "overcharge:<minor units>", "error:<CODE>" (raised before creating), "error_after_create:<CODE>",
"processing" (an unknown status), "substitute" (delivers a different item than approved).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from gap_core.clock import Clock
from gap_core.hashing import hash_value
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee

from gap_core.proposal import content_hash

from .interface import AdapterInfo, CartView, Coverage, MerchantError, MerchantTimeout, OrderView, PlaceResult, RefundView


@dataclass
class _Cart:
    lines: tuple[CartLine, ...]
    fees: tuple[Fee, ...]
    attributes: dict


class FakeMerchant:
    def __init__(self, shape: str, clock: Clock, *, script: list[str] | None = None, history_last_n: int | None = None,
                 currency: str = "INR") -> None:
        if shape not in ("a", "b"):
            raise ValueError("shape is 'a' or 'b'")
        self.shape, self.clock, self.currency = shape, clock, currency
        self.script = list(script or [])
        self.history_last_n = history_last_n if shape == "a" and history_last_n is None else history_last_n
        if shape == "a" and history_last_n is None:
            self.history_last_n = 5
        self.carts: dict[str, _Cart] = {}
        self.orders: dict[str, OrderView] = {}
        self.refunds: dict[str, RefundView] = {}
        self.place_calls = 0

    # --- test controls ------------------------------------------------------------

    def add_cart(self, cart_ref: str, lines, fees=(), attributes=None) -> None:
        if self.shape == "b":  # fake-b has one flat fee whatever the cart
            fees = (Fee("platform", Money(2500, self.currency)),)
        self.carts[cart_ref] = _Cart(tuple(lines), tuple(fees), attributes or {})

    def set_price(self, cart_ref: str, item_id: str, unit_price: Money) -> None:
        c = self.carts[cart_ref]
        c.lines = tuple(replace(l, unit_price=unit_price) if l.item_id == item_id else l for l in c.lines)

    def confirm_payment(self, merchant_order_ref: str) -> None:
        """fake-a: the payer completed payment on the rail."""
        o = self.orders[merchant_order_ref]
        self.orders[merchant_order_ref] = replace(o, status="PLACED", charged=o.charged or o.total)

    def orders_for_cart(self, cart_ref: str) -> list[OrderView]:
        return [o for o in self.orders.values() if o.cart_ref == cart_ref]

    # --- the adapter interface ------------------------------------------------------

    def describe(self) -> AdapterInfo:
        if self.shape == "a":
            return AdapterInfo(
                "fake-a", "1", ("in:upi_intent", "in:merchant_wallet", "in:cash_on_delivery"), False, True,
                Coverage(False, "last_n", self.history_last_n),
                {"PENDING_PAYMENT": "awaiting_payer", "PLACED": "success", "FAILED": "failure", "PROCESSING": "unknown"},
            )
        cov = Coverage(True) if self.history_last_n is None else Coverage(False, "last_n", self.history_last_n)
        return AdapterInfo("fake-b", "1", ("in:upi_intent", "in:merchant_wallet"), True, False, cov,
                           {"ok": "success", "error": "failure", "processing": "unknown"})

    def get_cart(self, cart_ref: str) -> CartView:
        c = self.carts[cart_ref]
        total = sum(l.unit_price.amount_minor * l.quantity for l in c.lines) + sum(f.amount.amount_minor for f in c.fees)
        h = hash_value({"lines": [l.to_json() for l in c.lines], "fees": [{"kind": f.kind, "amount": f.amount.to_json()} for f in c.fees]})
        return CartView(c.lines, c.fees, Money(total, self.currency), h)

    def get_item_attributes(self, item_ids: list[str]) -> dict:
        return {}

    def _create(self, cart_ref: str, key: str, status: str, charged_extra: int = 0, substitute: bool = False) -> OrderView:
        view = self.get_cart(cart_ref)
        ref = f"{self.shape}-order-{len(self.orders) + 1}"
        charged = None if status in ("PENDING_PAYMENT", "processing", "PROCESSING") else Money(view.total.amount_minor + charged_extra, self.currency)
        lines = view.lines
        if substitute:  # same price, a different item: the cart the payer approved is not what was ordered
            lines = (replace(lines[0], item_id=lines[0].item_id + "_substitute"),) + lines[1:]
        o = OrderView(ref, cart_ref, status, view.total, charged, self.clock.now(), key if self.shape == "a" else None,
                      content_hash(lines, view.fees))
        self.orders[ref] = o
        return o

    def place(self, cart_ref: str, rail_id: str, idempotency_key: str, destination_ref: str) -> PlaceResult:
        self.place_calls += 1
        if rail_id not in self.describe().rails_offered:
            raise MerchantError("RAIL_NOT_OFFERED")
        if self.shape == "a":  # honours idempotency: the same key returns the same order
            for o in self.orders.values():
                if o.idempotency_key == idempotency_key:
                    return PlaceResult(o.status, o.merchant_order_ref)
        step = self.script.pop(0) if self.script else "ok"
        ok_status = ("PENDING_PAYMENT" if rail_id == "in:upi_intent" else "PLACED") if self.shape == "a" else "ok"
        if step == "timeout_before_create":
            raise MerchantTimeout()
        if step.startswith("error:"):
            raise MerchantError(step.split(":", 1)[1])
        if step == "decline":
            return PlaceResult("FAILED" if self.shape == "a" else "error")
        if step == "processing":
            o = self._create(cart_ref, idempotency_key, "PROCESSING" if self.shape == "a" else "processing")
            return PlaceResult(o.status, o.merchant_order_ref)
        extra = int(step.split(":", 1)[1]) if step.startswith("overcharge:") else 0
        o = self._create(cart_ref, idempotency_key, ok_status, extra, substitute=step == "substitute")
        if step == "timeout_after_create":
            raise MerchantTimeout()
        if step.startswith("error_after_create:"):
            raise MerchantError(step.split(":", 1)[1])
        handoff = f"rail-redirect:{o.merchant_order_ref}" if o.status == "PENDING_PAYMENT" else None
        return PlaceResult(o.status, o.merchant_order_ref, handoff)

    def get_order(self, merchant_order_ref: str) -> OrderView:
        return self.orders[merchant_order_ref]

    def list_orders(self, since: datetime) -> tuple[list[OrderView], Coverage]:
        orders = sorted((o for o in self.orders.values() if o.placed_at >= since), key=lambda o: o.placed_at)
        cov = self.describe().history_coverage
        if cov.limit_code == "last_n":
            orders = orders[-cov.detail:]
        return orders, cov

    def refund(self, merchant_order_ref: str, amount: Money, *, completes: bool = True) -> RefundView:
        """Test control: the merchant starts a refund; it completes now or stalls."""
        r = RefundView(f"{self.shape}-refund-{len(self.refunds) + 1}", merchant_order_ref, amount,
                       "completed" if completes else "initiated", self.clock.now())
        self.refunds[r.refund_ref] = r
        return r

    def complete_refund(self, refund_ref: str) -> None:
        self.refunds[refund_ref] = replace(self.refunds[refund_ref], status="completed")

    def list_refunds(self, since: datetime) -> list[RefundView]:
        return sorted((r for r in self.refunds.values() if r.at >= since), key=lambda r: r.at)

    def cancel(self, merchant_order_ref: str) -> str:
        if self.shape == "a":
            raise MerchantError("CANCEL_NOT_SUPPORTED")
        o = self.orders[merchant_order_ref]
        self.orders[merchant_order_ref] = replace(o, status="cancelled")
        return "cancelled"
