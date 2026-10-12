"""A small ready-made deployment for tests and demos: one payer, a grocery spoke and a weekly repeat,
a fake merchant, a bank feed and a reference surface. Example values only (currency INR, test rails xx:*)."""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from gap_adapters.fakes import FakeMerchant
from gap_core.authority import Caller, CartLineTemplate, Limit, RailProfile, Spoke, StandingInstruction, Wheel, Window
from gap_core.clock import FixedClock
from gap_core.facts import Fact, TermSet
from gap_core.money import Money
from gap_core.proposal import CartLine
from gap_core.testing import PARAMS, rule
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_profile.store import LocalKek, ProfileStore
from gap_recon.recon import BankFeed

from .flow import Deployment, Flow, ProposalRequest
from .surface import ReferenceSurface

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
MONTH = Window("calendar_month", "Etc/UTC")
UPI = RailProfile("xx:upi", "human_per_payment", "bound_at_rail", ("explicit_tap",))
WALLET = RailProfile("xx:wallet", "none", "not_bound", ("explicit_tap", "silent_with_notice"))
LINES = (CartLine("item_milk", 2, Money(6000, "INR")), CartLine("item_bread", 1, Money(4500, "INR")))
CAT = Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "std:mcc@1", "m@1"), "merchant_adapter", True, "capturer_signed")
AGENT = Caller("clr_agent", "session_bound")
SCHED = Caller("clr_sched", "key_bound")


def inr(x: int) -> Money:
    return Money(x, "INR")


class XXMerchant(FakeMerchant):
    """fake-b, offering the test jurisdiction's rails."""

    def describe(self):
        return replace(super().describe(), rails_offered=("xx:upi", "xx:wallet"))


@dataclass
class World:
    flow: Flow
    merchant: XXMerchant
    surface: ReferenceSurface
    clock: FixedClock
    bank: BankFeed
    host: LocalSigner

    def request(self, cart="cart-1", intent="i-1", caller=AGENT, spoke="spk_grocery", **kw) -> ProposalRequest:
        return ProposalRequest(spoke, caller, intent, "mrc_shop", "fake-b", cart, "dst_home", kw.pop("confidence", 950), facts=(CAT,), **kw)

    def pay_bank(self, result) -> None:
        o = self.merchant.get_order(result.merchant_order_ref)
        self.bank.debit(f"bank-{o.merchant_order_ref}", o.merchant_order_ref, o.charged, self.clock.now())

    def approve(self, p):
        return self.flow.approve(p.proposal_id, self.surface.sign("authorise", p.proposal_id, p.determination.presented_hash))


def make_world(ledger, profile_conn, *, evaluator_digest: str = "sha256:" + "e" * 64) -> World:
    clock = FixedClock(T0)
    host = LocalSigner.generate("key_host")
    keylog = KeyLog(ledger, "host", host, clock)
    keylog.bootstrap()
    flow = Flow(ledger, ProfileStore(profile_conn, LocalKek.generate(), clock), keylog, clock, random.Random(1), evaluator_digest=evaluator_digest)
    m = XXMerchant("b", clock)
    ins = StandingInstruction("mrc_shop", "fake-b", (CartLineTemplate("item_milk", 2), CartLineTemplate("item_bread", 1)), "dst_home", "xx:wallet", 1000, inr(19000))
    spokes = {
        "spk_grocery": Spoke("spk_grocery", "whl_1", "explicit_tap", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=("clr_agent",),
                             caps=(Limit(inr(400000), MONTH),), rules=(rule("max_lines", "items", max_lines=3),)),
        "spk_repeat": Spoke("spk_repeat", "whl_1", "silent_with_notice", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=("clr_sched",),
                            instruction=ins, caps=(Limit(inr(100000), MONTH),)),
    }
    params = replace(PARAMS, silent_max_amount=inr(200000), silent_preconditions=({"fact": "notice.delivered", "op": "eq", "value": True},))
    bank = BankFeed()
    flow.register(Deployment(Wheel("whl_1", "pyr_1", "xx", budgets=(Limit(inr(1500000), MONTH),)), spokes,
                             {"params": params, "rails": {"xx:upi": UPI, "xx:wallet": WALLET}, "tier0_rules": (), "jurisdiction_rules": (),
                              "jurisdiction": "xx"}, {"fake-b": m}, bank))
    surface = ReferenceSurface("key_surface", "pyr_1", clock)
    flow.enrol_surface("whl_1", surface)
    return World(flow, m, surface, clock, bank, host)
