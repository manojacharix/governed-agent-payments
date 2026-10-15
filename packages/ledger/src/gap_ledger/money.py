"""Money in flight (03, 19 §6): holds, captures, releases, overcaptures and refunds as signed postings.

Balances are never stored; they are derived by replaying postings. A spoke and all its ancestors
(and the hub) are posted together in one transaction, under the wheel chain's lock, so two
authorisations on one wheel can never both spend the same room. Only definitive signals from a
merchant, rail, bank or reconciliation move money; an agent never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from gap_core.authority import Window
from gap_core.clock import Clock
from gap_core.evaluate import Usage
from gap_core.ids import new_id
from gap_core.money import Money
from gap_core.signatures import Signer
from gap_core.timefmt import format_ts, parse_ts

from .ledger import Ledger, LedgerError

SIGNAL_SOURCES = ("merchant", "rail", "bank", "reconciliation")  # never "agent" (invariant 16)


class HoldRefused(LedgerError):
    """There is not enough room, or an account is already overspent (03 §3)."""


class SignalRefused(LedgerError):
    """Only definitive signals from merchants, rails, banks or reconciliation may move money."""


@dataclass(frozen=True)
class Account:
    """One account set to post to: an owner (spoke, ancestor or wheel), its window, and its limit if any."""

    owner: str
    window: Window
    limit: Money | None


@dataclass(frozen=True)
class Signal:
    source: str
    ref: str

    def __post_init__(self) -> None:
        if self.source not in SIGNAL_SOURCES:
            raise SignalRefused(f"signals from {self.source!r} never move money; only {SIGNAL_SOURCES}")


@dataclass
class AccountState:
    held: int = 0
    spent: int = 0
    holds_placed: int = 0


@dataclass
class PaymentState:
    accounts: dict = field(default_factory=dict)  # {owner: (currency, window_id)}
    held: int = 0       # per account (the same amount is held on every account of the payment)
    captured: int = 0   # capture + overcapture
    refunded: int = 0
    closed: bool = False


@dataclass
class Entry:
    """One payment on one owner: what it holds and spends there, and when its hold was placed."""

    currency: str
    held: int = 0
    spent: int = 0
    hold_at: datetime | None = None


@dataclass
class State:
    accounts: dict = field(default_factory=dict)  # {(owner, currency, window_id): AccountState}
    payments: dict = field(default_factory=dict)  # {payment_ref: PaymentState}
    entries: dict = field(default_factory=dict)   # {owner: {payment_ref: Entry}}

    def usage(self) -> dict:
        """Held plus spent per (owner, window_id) of the windows postings were made in (19 §6). For a limit's own
        calendar window this is exact; for any other window (a rolling one, or a rule's window that differs from the
        cap's) use usage_in(), which counts by when each hold was placed."""
        out: dict = {}
        for (owner, currency, window_id), a in self.accounts.items():
            u = out.get((owner, window_id), Usage(0, {}))
            amounts = dict(u.amounts)
            amounts[currency] = amounts.get(currency, 0) + a.held + a.spent
            out[(owner, window_id)] = Usage(u.count + a.holds_placed, amounts)
        return out

    def usage_in(self, owner: str, window: Window, at: datetime) -> Usage:
        """Held plus spent, and the number of holds, for every payment on `owner` whose hold falls in the instance of
        `window` that `at` is in. A payment stays in the window its hold was placed in, whatever happens to it later."""
        count, amounts = 0, {}
        for e in self.entries.get(owner, {}).values():
            if e.hold_at is not None and window.contains(e.hold_at, at):
                count += 1
                amounts[e.currency] = amounts.get(e.currency, 0) + e.held + e.spent
        return Usage(count, amounts)


def replay(ledger: Ledger, wheel_id: str) -> State:
    """Derive every balance from the postings on the wheel's chain."""
    st = State()
    for rec in ledger.records(wheel_id, "posting"):
        aset, kind, amt, pref = rec["account_set"], rec["kind"], rec["amount"]["amount_minor"], rec["payment_ref"]
        key = (aset["owner"], aset["currency"], aset["window_id"])
        a = st.accounts.setdefault(key, AccountState())
        p = st.payments.setdefault(pref, PaymentState())
        e = st.entries.setdefault(aset["owner"], {}).setdefault(pref, Entry(aset["currency"]))
        e.held += amt if kind == "hold" else -amt if kind in ("capture", "release") else 0
        e.spent += amt if kind in ("capture", "overcapture") else -amt if kind == "refund" else 0
        first_account = not p.accounts or aset["owner"] == next(iter(p.accounts))
        if kind == "hold":
            e.hold_at = parse_ts(rec["posted_at"])
            a.held += amt
            a.holds_placed += 1
            p.accounts[aset["owner"]] = (aset["currency"], aset["window_id"])
            if first_account:
                p.held += amt
        elif kind == "capture":
            a.held -= amt
            a.spent += amt
            if first_account:
                p.held -= amt
                p.captured += amt
        elif kind == "release":
            a.held -= amt
            if first_account:
                p.held -= amt
        elif kind == "overcapture":
            a.spent += amt
            if first_account:
                p.captured += amt
        elif kind == "refund":
            a.spent -= amt
            if first_account:
                p.refunded += amt
        if first_account and kind in ("capture", "release") and p.held == 0:
            p.closed = True
    return st


class MoneyInFlight:
    def __init__(self, ledger: Ledger, signer: Signer, clock: Clock, rng) -> None:
        self.ledger, self.signer, self.clock, self.rng = ledger, signer, clock, rng

    def _post(self, wheel_id: str, group: str, owner: str, currency: str, window_id: str, kind: str, amount: int,
              payment_ref: str, signal: Signal | None) -> dict:
        pid = new_id("pst", clock=self.clock, rng=self.rng)
        body = {
            "record_type": "posting", "posting_id": pid, "txn_group_id": group,
            "account_set": {"owner": owner, "currency": currency, "window_id": window_id},
            "kind": kind, "amount": Money(amount, currency).to_json(), "payment_ref": payment_ref,
            "posted_at": format_ts(self.clock.now()),
        }
        if signal is not None:
            body["signal_ref"] = f"{signal.source}:{signal.ref}"
        return self.ledger.append(wheel_id, body, self.signer, record_id=pid)

    def hold(self, wheel_id: str, payment_ref: str, amount: Money, accounts: list[Account]) -> list[dict]:
        """Place a hold on every account (spoke, ancestors, hub) at once, or on none (03 §3 step 1)."""
        if amount.amount_minor <= 0:
            raise HoldRefused("a hold is for a positive amount")
        conn = self.ledger.conn
        with conn.transaction():
            self.ledger.lock_chain(wheel_id)  # one payment at a time per wheel
            st = replay(self.ledger, wheel_id)
            if payment_ref in st.payments:
                raise HoldRefused(f"payment {payment_ref} already has a hold")
            at = self.clock.now()
            for acc in accounts:
                if acc.limit is None:
                    continue
                if acc.limit.currency != amount.currency:
                    raise HoldRefused(f"{acc.owner}: limit is in {acc.limit.currency}, payment in {amount.currency}")
                available = acc.limit.amount_minor - st.usage_in(acc.owner, acc.window, at).amounts.get(amount.currency, 0)
                if available < 0:
                    raise HoldRefused(f"{acc.owner}: overspent; no new holds until available ≥ 0")
                if amount.amount_minor > available:
                    raise HoldRefused(f"{acc.owner}: needs {amount.amount_minor}, available {available}")
            group = new_id("txg", clock=self.clock, rng=self.rng)
            return [self._post(wheel_id, group, acc.owner, amount.currency, acc.window.window_id(at), "hold",
                               amount.amount_minor, payment_ref, None) for acc in accounts]

    def _open_payment(self, wheel_id: str, payment_ref: str) -> PaymentState:
        p = replay(self.ledger, wheel_id).payments.get(payment_ref)
        if p is None:
            raise LedgerError(f"no hold for payment {payment_ref}: no capture without a hold")
        return p

    def capture(self, wheel_id: str, payment_ref: str, actual: Money, signal: Signal) -> dict:
        """Definitive success at the actual amount (03 §3 steps 2 and 4). Returns {'overcaptured': int}."""
        conn = self.ledger.conn
        with conn.transaction():
            self.ledger.lock_chain(wheel_id)
            p = self._open_payment(wheel_id, payment_ref)
            if p.closed or p.held == 0:
                raise LedgerError(f"payment {payment_ref} has nothing held")
            group = new_id("txg", clock=self.clock, rng=self.rng)
            take = min(actual.amount_minor, p.held)
            for owner, (currency, window_id) in p.accounts.items():
                if currency != actual.currency:
                    raise LedgerError("capture currency differs from the hold")
                self._post(wheel_id, group, owner, currency, window_id, "capture", take, payment_ref, signal)
                if actual.amount_minor < p.held:
                    self._post(wheel_id, group, owner, currency, window_id, "release", p.held - take, payment_ref, signal)
                elif actual.amount_minor > p.held:
                    self._post(wheel_id, group, owner, currency, window_id, "overcapture", actual.amount_minor - p.held, payment_ref, signal)
            return {"overcaptured": max(0, actual.amount_minor - p.held)}

    def release(self, wheel_id: str, payment_ref: str, signal: Signal) -> None:
        """Definitive failure, or suspension before execution: release everything still held."""
        conn = self.ledger.conn
        with conn.transaction():
            self.ledger.lock_chain(wheel_id)
            p = self._open_payment(wheel_id, payment_ref)
            if p.held == 0:
                return
            group = new_id("txg", clock=self.clock, rng=self.rng)
            for owner, (currency, window_id) in p.accounts.items():
                self._post(wheel_id, group, owner, currency, window_id, "release", p.held, payment_ref, signal)

    def refund(self, wheel_id: str, payment_ref: str, amount: Money, signal: Signal) -> None:
        """Completed refunds only, to the original window only, never more than was captured (03 §3 step 5)."""
        conn = self.ledger.conn
        with conn.transaction():
            self.ledger.lock_chain(wheel_id)
            p = self._open_payment(wheel_id, payment_ref)
            if amount.amount_minor <= 0 or amount.amount_minor > p.captured - p.refunded:
                raise LedgerError("refund exceeds what was captured and not yet refunded")
            group = new_id("txg", clock=self.clock, rng=self.rng)
            for owner, (currency, window_id) in p.accounts.items():
                self._post(wheel_id, group, owner, currency, window_id, "refund", amount.amount_minor, payment_ref, signal)
