"""Reconciliation (05): did what was decided actually happen?

A sweep reads what the ledger says (decisions, execution attempts, holds and captures), what the merchant
says (orders, refunds) and what the bank says (debits, credits), then:
- settles held money only on definitive, consistent evidence (capture or release; 03 §3),
- opens a finding wherever the sources disagree or the evidence is incomplete,
- never closes a finding by timeout; only evidence or the payer's acceptance closes one.

Authority by question (05 §2): money moved -> bank (else the merchant's definitive signal);
what was ordered -> merchant; what was authorised -> the ledger. Every sweep is a signed record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from gap_adapters.interface import MerchantAdapter, OrderView
from gap_core.clock import Clock
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.money import Money
from gap_core.signatures import Signer
from gap_core.timefmt import format_ts, parse_ts
from gap_ledger.ledger import Ledger
from gap_ledger.money import MoneyInFlight, Signal, replay

FINDING_KINDS = ("matched", "pending_resolved", "overcapture", "missing_order", "item_mismatch",
                 "refund_stalled", "source_conflict", "coverage_gap", "external_order")
OPENS_TAP = ("overcapture", "missing_order")  # an open one forces explicit_tap on its spoke (05 §5)
EPOCH = datetime(2000, 1, 1).astimezone()


@dataclass(frozen=True)
class BankEntry:
    ref: str
    kind: str  # debit | credit
    merchant_order_ref: str  # for credits: the refund_ref
    amount: Money
    at: datetime


class BankFeed:
    """A fake bank statement source (class bank). Real banks would be source adapters with their own coverage."""

    source_id, source_class = "bank-feed", "bank"

    def __init__(self) -> None:
        self.entries: list[BankEntry] = []

    def debit(self, ref: str, merchant_order_ref: str, amount: Money, at: datetime) -> None:
        self.entries.append(BankEntry(ref, "debit", merchant_order_ref, amount, at))

    def credit(self, ref: str, refund_ref: str, amount: Money, at: datetime) -> None:
        self.entries.append(BankEntry(ref, "credit", refund_ref, amount, at))

    def find(self, kind: str, merchant_ref: str) -> BankEntry | None:
        return next((e for e in self.entries if e.kind == kind and e.merchant_order_ref == merchant_ref), None)


@dataclass
class _Payment:
    det_id: str
    spoke_id: str
    adapter_id: str
    idempotency_key: str
    cart_ref: str
    content_hash: str | None
    order_ref: str | None = None
    ambiguous: bool = False  # the execution ended unknown or awaiting the payer


@dataclass
class SweepResult:
    sweep: dict
    findings: list = field(default_factory=list)
    captured: int = 0
    released: int = 0
    refunded: int = 0


class Reconciler:
    def __init__(self, ledger: Ledger, signer: Signer, clock: Clock, rng, money: MoneyInFlight,
                 *, max_pending_age_s: int, refund_stall_s: int) -> None:
        self.ledger, self.signer, self.clock, self.rng, self.money = ledger, signer, clock, rng, money
        self.max_pending_age = timedelta(seconds=max_pending_age_s)
        self.refund_stall = timedelta(seconds=refund_stall_s)

    # --- reading the ledger -------------------------------------------------------------

    def _payments(self, wheel_id: str) -> dict[str, _Payment]:
        proposals = {r["proposal_id"]: r for r in self.ledger.records(wheel_id, "proposal")}
        dets = {r["determination_id"]: r for r in self.ledger.records(wheel_id, "determination")}
        out: dict[str, _Payment] = {}
        for a in self.ledger.records(wheel_id, "execution_attempt"):
            d = dets.get(a["determination_id"], {})
            prop = proposals.get(d.get("proposal_id"), {})
            p = out.setdefault(a["determination_id"], _Payment(
                a["determination_id"], d.get("spoke_id", ""), a["adapter_id"], a["idempotency_key"],
                prop.get("merchant_cart_ref", ""), prop.get("content_hash")))
            if a.get("merchant_order_ref"):
                p.order_ref = a["merchant_order_ref"]
            p.ambiguous = a["outcome"] in ("unknown", "awaiting_payer")
        return out

    def findings(self, wheel_id: str) -> dict[str, dict]:
        """Latest state of every finding on the wheel."""
        latest: dict[str, dict] = {}
        for r in self.ledger.records(wheel_id, "finding"):
            latest[r["finding_id"]] = r
        return latest

    def open_findings(self, wheel_id: str) -> list[dict]:
        return [f for f in self.findings(wheel_id).values() if f["status"] == "open"]

    def open_tap_findings_by_spoke(self, wheel_id: str) -> dict[str, int]:
        """Counts for the fact findings.open_on_spoke (05 §5)."""
        dets = {r["determination_id"]: r.get("spoke_id") for r in self.ledger.records(wheel_id, "determination")}
        out: dict[str, int] = {}
        for f in self.open_findings(wheel_id):
            if f["kind"] in OPENS_TAP and f.get("payment_ref") in dets:
                out[dets[f["payment_ref"]]] = out.get(dets[f["payment_ref"]], 0) + 1
        return out

    # --- writing findings -------------------------------------------------------------------

    def _finding(self, wheel_id, kind, *, payment_ref=None, order_ref=None, status="open", closed_by=None,
                 finding_id=None, sources=(), next_step=None, detail=None) -> dict:
        now = format_ts(self.clock.now())
        fid = finding_id or new_id("fnd", clock=self.clock, rng=self.rng)
        body = {"record_type": "finding", "finding_id": fid, "kind": kind, "status": status, "opened_at": now,
                "sources": list(sources)}
        if payment_ref:
            body["payment_ref"] = payment_ref
        if order_ref:
            body["merchant_order_ref"] = order_ref
        if status == "closed":
            body["closed_at"], body["closed_by"] = now, closed_by
        if next_step:
            body["suggested_next_step"] = next_step
        if detail:
            body["detail"] = detail
        return self.ledger.append(wheel_id, body, self.signer, record_id=fid)

    def accept(self, wheel_id: str, finding_id: str, assertion_id: str) -> dict:
        """The payer accepts an open finding (05 §5): the only way besides evidence to close one."""
        f = self.findings(wheel_id).get(finding_id)
        if f is None or f["status"] != "open":
            raise ValueError("no such open finding")
        rec = dict(kind=f["kind"], payment_ref=f.get("payment_ref"), order_ref=f.get("merchant_order_ref"),
                   status="closed", closed_by="payer_acceptance", finding_id=finding_id, sources=f["sources"],
                   detail={"assertion_id": assertion_id})
        return self._finding(wheel_id, **rec)

    # --- the sweep -----------------------------------------------------------------------

    def sweep(self, wheel_id: str, adapters: dict[str, MerchantAdapter], bank: BankFeed | None, *, trigger: str = "scheduled") -> SweepResult:
        now = self.clock.now()
        conn = self.ledger.conn
        with conn.transaction():
            self.ledger.lock_chain(wheel_id)
            started = now
            pays = self._payments(wheel_id)
            state = replay(self.ledger, wheel_id)
            existing = self.findings(wheel_id)
            open_by_subject = {(f["kind"], f.get("payment_ref") or f.get("merchant_order_ref")): fid
                               for fid, f in existing.items() if f["status"] == "open"}
            recorded = {(f["kind"], f.get("payment_ref") or f.get("merchant_order_ref")) for f in existing.values()}
            res = SweepResult({})
            observed, coverage = {"orders": [], "bank": [], "refunds": []}, {}
            lists = {}
            for aid, ad in adapters.items():
                orders, cov = ad.list_orders(EPOCH)
                lists[aid] = (orders, cov)
                coverage[aid] = {"complete": cov.complete, **({"limit_code": cov.limit_code} if cov.limit_code else {})}
                observed["orders"] += [[o.merchant_order_ref, o.status, (o.charged or o.total).amount_minor] for o in orders]
            if bank is not None:
                observed["bank"] = [[e.ref, e.kind, e.merchant_order_ref, e.amount.amount_minor] for e in bank.entries]
            src_m = lambda aid: {"source_id": aid, "class": "merchant", "observed_hash": hash_value(observed["orders"])}  # noqa: E731
            src_b = {"source_id": BankFeed.source_id, "class": "bank", "observed_hash": hash_value(observed["bank"])}

            def open_once(kind, subject, **kw):
                if (kind, subject) not in open_by_subject:
                    rec = self._finding(wheel_id, kind, **kw)
                    open_by_subject[(kind, subject)] = rec["finding_id"]
                    res.findings.append(rec)

            def close(kind, subject, **kw):
                fid = open_by_subject.pop((kind, subject), None)
                if fid:
                    res.findings.append(self._finding(wheel_id, kind, status="closed", closed_by="evidence", finding_id=fid, **kw))

            matched_orders: set = set()
            for det_id, p in pays.items():
                ad = adapters.get(p.adapter_id)
                if ad is None:
                    continue
                info = ad.describe()
                orders, cov = lists[p.adapter_id]
                order = self._locate(ad, info, orders, p)
                if p.order_ref and order is None and cov.complete:
                    open_once("missing_order", det_id, payment_ref=det_id, order_ref=p.order_ref, sources=[src_m(p.adapter_id)])
                if order is not None:
                    matched_orders.add(order.merchant_order_ref)
                pay_state = state.payments.get(det_id)
                held = pay_state.held if pay_state else 0
                if held <= 0:
                    continue
                if order is None:
                    if cov.complete:
                        self.money.release(wheel_id, det_id, Signal("reconciliation", "no-order"))
                        res.released += 1
                        close("coverage_gap", det_id, payment_ref=det_id)
                        res.findings.append(self._finding(wheel_id, "pending_resolved", payment_ref=det_id, status="closed",
                                                          closed_by="evidence", sources=[src_m(p.adapter_id)], detail={"result": "no_order"}))
                    else:
                        open_once("coverage_gap", det_id, payment_ref=det_id, sources=[src_m(p.adapter_id)],
                                  next_step="ask the merchant for the order by idempotency key", detail=coverage[p.adapter_id])
                    continue
                outcome = info.outcomes.get(order.status, "unknown")
                if outcome == "failure" or order.status in ("cancelled", "FAILED"):
                    self.money.release(wheel_id, det_id, Signal("merchant", order.merchant_order_ref))
                    res.released += 1
                    res.findings.append(self._finding(wheel_id, "pending_resolved", payment_ref=det_id, order_ref=order.merchant_order_ref,
                                                      status="closed", closed_by="evidence", sources=[src_m(p.adapter_id)], detail={"result": "failed"}))
                    continue
                if outcome != "success":
                    if now - order.placed_at > self.max_pending_age:
                        open_once("coverage_gap", det_id, payment_ref=det_id, order_ref=order.merchant_order_ref,
                                  sources=[src_m(p.adapter_id)], next_step="order still not settled after max_pending_age")
                    continue
                charged = order.charged or order.total
                amount, signal = charged, Signal("merchant", order.merchant_order_ref)
                if bank is not None:
                    debit = bank.find("debit", order.merchant_order_ref)
                    if debit is None:
                        if now - order.placed_at > self.max_pending_age:
                            open_once("source_conflict", det_id, payment_ref=det_id, order_ref=order.merchant_order_ref,
                                      sources=[src_m(p.adapter_id), src_b], detail={"merchant": "paid", "bank": "no debit"})
                        continue
                    if debit.amount != charged:
                        open_once("source_conflict", det_id, payment_ref=det_id, order_ref=order.merchant_order_ref, sources=[src_m(p.adapter_id), src_b],
                                  detail={"merchant_charged": charged.amount_minor, "bank_debited": debit.amount.amount_minor})
                        continue
                    amount, signal = debit.amount, Signal("bank", debit.ref)
                out = self.money.capture(wheel_id, det_id, amount, signal)
                res.captured += 1
                for k in ("coverage_gap", "source_conflict"):
                    close(k, det_id, payment_ref=det_id, order_ref=order.merchant_order_ref)
                srcs = [src_m(p.adapter_id)] + ([src_b] if bank is not None else [])
                if out["overcaptured"]:
                    open_once("overcapture", det_id, payment_ref=det_id, order_ref=order.merchant_order_ref, sources=srcs,
                              detail={"excess": out["overcaptured"]}, next_step="dispute the excess with the merchant")
                if order.content_hash and p.content_hash and order.content_hash != p.content_hash:
                    open_once("item_mismatch", det_id, payment_ref=det_id, order_ref=order.merchant_order_ref, sources=[src_m(p.adapter_id)])
                if not out["overcaptured"]:
                    res.findings.append(self._finding(wheel_id, "pending_resolved" if p.ambiguous else "matched", payment_ref=det_id,
                                                      order_ref=order.merchant_order_ref, status="closed", closed_by="evidence", sources=srcs))

            # refunds: completed ones are posted to the original window; stalled ones are opened
            posted = {r.get("signal_ref", "").split(":", 1)[-1] for r in self.ledger.records(wheel_id, "posting") if r["kind"] == "refund"}
            order_to_det = {}
            for det_id, p in pays.items():
                o = self._locate(adapters[p.adapter_id], adapters[p.adapter_id].describe(), lists[p.adapter_id][0], p) if p.adapter_id in adapters else None
                if o:
                    order_to_det[o.merchant_order_ref] = det_id
            for aid, ad in adapters.items():
                for rf in ad.list_refunds(EPOCH):
                    det_id = order_to_det.get(rf.merchant_order_ref)
                    observed["refunds"].append([rf.refund_ref, rf.status, rf.amount.amount_minor])
                    if det_id is None or rf.refund_ref in posted:
                        continue
                    credited = bank.find("credit", rf.refund_ref) if bank is not None else None
                    complete = rf.status == "completed" and (bank is None or credited is not None)
                    if complete:
                        p_state = state.payments.get(det_id)
                        if p_state and p_state.captured - p_state.refunded >= rf.amount.amount_minor:
                            self.money.refund(wheel_id, det_id, rf.amount, Signal("bank" if credited else "merchant", rf.refund_ref))
                            res.refunded += 1
                        close("refund_stalled", rf.refund_ref, order_ref=rf.merchant_order_ref, payment_ref=det_id)
                    elif now - rf.at > self.refund_stall:
                        open_once("refund_stalled", rf.refund_ref, payment_ref=det_id, order_ref=rf.merchant_order_ref,
                                  sources=[src_m(aid)] + ([src_b] if bank is not None else []), detail={"refund_ref": rf.refund_ref})

            # orders at the merchant that no decision placed: outside the layer, never an agent's (05 §5)
            for aid, (orders, _cov) in lists.items():
                for o in orders:
                    if o.merchant_order_ref not in matched_orders and ("external_order", o.merchant_order_ref) not in recorded:
                        res.findings.append(self._finding(wheel_id, "external_order", order_ref=o.merchant_order_ref, status="closed",
                                                          closed_by="evidence", sources=[src_m(aid)], detail={"attributed_to": "nobody"}))

            sid = new_id("swp", clock=self.clock, rng=self.rng)
            res.sweep = self.ledger.append(wheel_id, {
                "record_type": "sweep", "sweep_id": sid, "trigger": trigger, "started_at": format_ts(started),
                "ended_at": format_ts(self.clock.now()), "observed_hash": hash_value(observed), "coverage": coverage,
                "finding_ids": [f["finding_id"] for f in res.findings],
            }, self.signer, record_id=sid)
            return res

    @staticmethod
    def _locate(ad: MerchantAdapter, info, orders: list[OrderView], p: _Payment) -> OrderView | None:
        if p.order_ref:
            try:
                return ad.get_order(p.order_ref)
            except KeyError:
                return None
        for o in orders:
            if (info.honours_idempotency and o.idempotency_key == p.idempotency_key) or (not info.honours_idempotency and o.cart_ref == p.cart_ref):
                return o
        return None
