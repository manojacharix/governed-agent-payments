"""Proposals (02, 19 §4) and the hashes that bind them: proposal, cart and presented hashes."""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import hash_value
from .money import Money

FEE_KINDS = ("delivery", "platform", "packaging", "tax", "other")


@dataclass(frozen=True)
class CartLine:
    item_id: str
    quantity: int
    unit_price: Money
    variant_id: str | None = None
    category: str | None = None

    def __post_init__(self) -> None:
        if type(self.quantity) is not int or self.quantity < 1:
            raise ValueError("quantity must be a positive int")

    def to_json(self) -> dict:
        d = {"item_id": self.item_id, "quantity": self.quantity, "unit_price": self.unit_price.to_json()}
        if self.variant_id is not None:
            d["variant_id"] = self.variant_id
        if self.category is not None:
            d["category"] = self.category
        return d


@dataclass(frozen=True)
class Fee:
    kind: str
    amount: Money

    def __post_init__(self) -> None:
        if self.kind not in FEE_KINDS:
            raise ValueError(f"unknown fee kind {self.kind!r}")


@dataclass(frozen=True)
class Extra:
    kind: str
    amount: Money | None = None


@dataclass(frozen=True)
class ProposalBody:
    merchant_ref: str
    adapter_id: str
    merchant_cart_ref: str
    lines: tuple[CartLine, ...]
    total: Money
    destination_ref: str
    agent_confidence: int  # PerMille
    fees: tuple[Fee, ...] = ()
    extras: tuple[Extra, ...] = ()
    rail_id: str | None = None
    substitution_allowed: bool | None = None

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("a proposal has at least one line")
        if type(self.agent_confidence) is not int or not 0 <= self.agent_confidence <= 1000:
            raise ValueError("agent_confidence is a PerMille")
        computed = Money(0, self.total.currency)
        for line in self.lines:
            computed = computed + Money(line.unit_price.amount_minor * line.quantity, line.unit_price.currency)
        for fee in self.fees:
            computed = computed + fee.amount
        for extra in self.extras:
            if extra.amount is not None:
                computed = computed + extra.amount
        if computed != self.total:
            raise ValueError(f"total {self.total} does not equal lines + fees + extras {computed}")

    def _lines_json(self) -> list:
        return [l.to_json() for l in sorted(self.lines, key=lambda l: (l.item_id, l.variant_id or ""))]

    def _fees_json(self) -> list:
        return [{"kind": f.kind, "amount": f.amount.to_json()} for f in sorted(self.fees, key=lambda f: (f.kind, f.amount.amount_minor))]

    def _extras_json(self) -> list:
        out = []
        for e in sorted(self.extras, key=lambda e: (e.kind, e.amount.amount_minor if e.amount else 0)):
            d = {"kind": e.kind}
            if e.amount is not None:
                d["amount"] = e.amount.to_json()
            out.append(d)
        return out

    def to_json(self) -> dict:
        d = {
            "merchant_ref": self.merchant_ref, "adapter_id": self.adapter_id, "merchant_cart_ref": self.merchant_cart_ref,
            "lines": self._lines_json(), "fees": self._fees_json(), "extras": self._extras_json(),
            "total": self.total.to_json(), "destination_ref": self.destination_ref, "agent_confidence": self.agent_confidence,
        }
        if self.rail_id is not None:
            d["rail_id"] = self.rail_id
        if self.substitution_allowed is not None:
            d["substitution"] = {"allowed": self.substitution_allowed}
        return d


def proposal_hash(body: ProposalBody) -> str:
    """Hash of the normalised body (19 §4.1). Same intent + different hash = intent_conflict."""
    return hash_value(body.to_json())


def cart_hash(body: ProposalBody) -> str:
    """What the duplicate guard compares (02 §3): merchant, lines and fees."""
    return hash_value({"merchant_ref": body.merchant_ref, "lines": body._lines_json(), "fees": body._fees_json()})


def content_hash(lines, fees) -> str:
    """What was in the cart, independent of who computes it: the merchant can compute it too (item_mismatch, 05)."""
    ls = sorted((l.to_json() for l in lines), key=lambda d: (d["item_id"], d.get("variant_id", "")))
    fs = sorted(({"kind": f.kind, "amount": f.amount.to_json()} for f in fees), key=lambda d: (d["kind"], d["amount"]["amount_minor"]))
    return hash_value({"lines": [{k: v for k, v in l.items() if k != "category"} for l in ls], "fees": fs})


def presented_hash(body: ProposalBody, rail_id: str) -> str:
    """What the payer approves (02 §1, invariant 10): lines, fees, extras, total, rail, destination."""
    return hash_value(
        {
            "lines": body._lines_json(), "fees": body._fees_json(), "extras": body._extras_json(),
            "total": body.total.to_json(), "rail_id": rail_id, "destination_ref": body.destination_ref,
        }
    )


def payer_subject(proposal_id: str, presented: str, overridden=None) -> str:
    """What the payer's device signs for a proposal (07 §3): this proposal, exactly as presented, and for an override
    the clauses set aside. Binding the proposal id means a signature can never be moved to another proposal that
    happens to show the same cart."""
    body = {"proposal_id": proposal_id, "presented_hash": presented}
    if overridden is not None:
        body["overridden"] = sorted(overridden)
    return hash_value(body)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    wheel_id: str
    spoke_id: str
    caller_id: str
    intent_id: str
    body: ProposalBody
