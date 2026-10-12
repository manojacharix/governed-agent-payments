"""The merchant adapter interface (19 §7.3). Every merchant integration implements it; the core never
sees a merchant's own API. Contract tests run the same suite against every adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from gap_core.money import Money
from gap_core.proposal import CartLine, Fee

OUTCOMES = ("success", "failure", "unknown", "awaiting_payer")


@dataclass(frozen=True)
class Coverage:
    """How much order history the adapter can see (05 §1). Incomplete history cannot prove an order is absent."""

    complete: bool
    limit_code: str | None = None  # last_n | time_bounded | partial_fields
    detail: int | None = None


@dataclass(frozen=True)
class AdapterInfo:
    adapter_id: str
    version: str
    rails_offered: tuple[str, ...]
    supports_cancel: bool
    honours_idempotency: bool  # does place() with the same key return the same order?
    history_coverage: Coverage
    outcomes: dict  # {merchant status: success | failure | unknown | awaiting_payer}


@dataclass(frozen=True)
class CartView:
    lines: tuple[CartLine, ...]
    fees: tuple[Fee, ...]
    total: Money
    merchant_cart_hash: str


@dataclass(frozen=True)
class PlaceResult:
    status: str
    merchant_order_ref: str | None = None
    handoff: str | None = None  # e.g. a rail redirect the payer must complete


@dataclass(frozen=True)
class OrderView:
    merchant_order_ref: str
    cart_ref: str
    status: str
    total: Money
    charged: Money | None
    placed_at: datetime
    idempotency_key: str | None = None  # only if the merchant records it
    content_hash: str | None = None     # gap_core.proposal.content_hash of what was actually ordered


@dataclass(frozen=True)
class RefundView:
    refund_ref: str
    merchant_order_ref: str
    amount: Money
    status: str  # initiated | completed | failed
    at: datetime


class MerchantTimeout(Exception):
    """The call did not return. The order may or may not exist."""


class MerchantError(Exception):
    """The merchant returned an error code. The order may still exist (an error is not proof)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MerchantAdapter(Protocol):
    def describe(self) -> AdapterInfo: ...
    def get_cart(self, cart_ref: str) -> CartView: ...
    def get_item_attributes(self, item_ids: list[str]) -> dict: ...
    def place(self, cart_ref: str, rail_id: str, idempotency_key: str, destination_ref: str) -> PlaceResult: ...
    def get_order(self, merchant_order_ref: str) -> OrderView: ...
    def list_orders(self, since: datetime) -> tuple[list[OrderView], Coverage]: ...
    def cancel(self, merchant_order_ref: str) -> str: ...
    def list_refunds(self, since: datetime) -> list[RefundView]: ...
