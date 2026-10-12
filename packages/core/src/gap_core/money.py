"""Money as integer minor units plus an ISO 4217 code (19 §1.2). No floats, no currency mixing."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MINOR = 2**53 - 1
_CURRENCY = re.compile(r"[A-Z]{3}")


class CurrencyMismatch(ValueError):
    """Two amounts in different currencies were combined or compared."""


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if type(self.amount_minor) is not int:  # rejects bool, float, str
            raise TypeError(f"amount_minor must be int, got {type(self.amount_minor).__name__}")
        if not -MAX_MINOR <= self.amount_minor <= MAX_MINOR:
            raise ValueError("amount_minor outside ±(2^53 - 1)")
        if not isinstance(self.currency, str) or not _CURRENCY.fullmatch(self.currency):
            raise ValueError(f"currency must be a 3-letter ISO 4217 code, got {self.currency!r}")

    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise TypeError("can only combine Money with Money")
        if other.currency != self.currency:
            raise CurrencyMismatch(f"{self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor >= other.amount_minor

    def to_json(self) -> dict:
        return {"amount_minor": self.amount_minor, "currency": self.currency}

    @classmethod
    def from_json(cls, data: dict) -> Money:
        if not isinstance(data, dict) or set(data) != {"amount_minor", "currency"}:
            raise ValueError("Money must have exactly amount_minor and currency")
        return cls(data["amount_minor"], data["currency"])
