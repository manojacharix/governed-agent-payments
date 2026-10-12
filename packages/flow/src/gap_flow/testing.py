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

from gap_identity.devices import Device, KeyAgent, bearer_proof
from gap_identity.identity import Identity

from .flow import Deployment, Flow, ProposalRequest, request_digest

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
MONTH = Window("calendar_month", "Etc/UTC")
UPI = RailProfile("xx:upi", "human_per_payment", "bound_at_rail", ("explicit_tap",))
WALLET = RailProfile("xx:wallet", "none", "not_bound", ("explicit_tap", "silent_with_notice"))
LINES = (CartLine("item_milk", 2, Money(6000, "INR")), CartLine("item_bread", 1, Money(4500, "INR")))
CAT = Fact("merchant.categories", TermSet(frozenset({"std:mcc:5411"}), "std:mcc@1", "m@1"), "merchant_adapter", True, "capturer_signed")
SCALE = (type("L", (), {"level": "none", "requires": ()})(), type("L", (), {"level": "possession", "requires": ("possession",)})(),
         type("L", (), {"level": "two_factor", "requires": ("possession", {"any_of": ["knowledge", "inherence"]})})())
ID_PARAMS = {
    **{f"auth.strength.{k}": "two_factor" for k in ("create", "widen", "resume", "override", "no_auth_rail", "enrol_surface", "enrol_caller", "recovery")},
    "identity.recovery_delay": 86400, "identity.surface.min_assurance": "software", "identity.caller.max_validity": 90 * 86400,
    "identity.proof_max_age": 300,
}


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
    device: Device
    clock: FixedClock
    bank: BankFeed
    host: LocalSigner
    agent_token: str
    scheduler: KeyAgent

    def request(self, cart="cart-1", intent="i-1", spoke="spk_grocery", **kw) -> ProposalRequest:
        # the caller is filled in by the flow from the proof; the placeholder is never trusted
        return ProposalRequest(spoke, Caller("unproven", "session_bound"), intent, "mrc_shop", "fake-b", cart, "dst_home",
                               kw.pop("confidence", 950), facts=(CAT,), **kw)

    def propose(self, req: ProposalRequest, **proof_kw):
        if req.spoke_id == "spk_repeat":
            proof = self.scheduler.proof(req.intent_id, request_digest(req), **proof_kw)
        else:
            proof = bearer_proof(self.agent_token, self.clock, **proof_kw)
        return self.flow.propose("whl_1", req, proof)

    def respond(self, p, action, overridden=None) -> dict:
        return self.device.respond(self.flow.challenge(p.proposal_id, action, overridden))

    def approve(self, p):
        return self.flow.approve(p.proposal_id, self.respond(p, "authorise"))

    def decline(self, p):
        return self.flow.decline(p.proposal_id, self.respond(p, "decline"))

    def override(self, p, clauses):
        return self.flow.override(p.proposal_id, clauses, self.respond(p, "override", clauses))

    def pay_bank(self, result) -> None:
        o = self.merchant.get_order(result.merchant_order_ref)
        self.bank.debit(f"bank-{o.merchant_order_ref}", o.merchant_order_ref, o.charged, self.clock.now())


def make_world(ledger, profile_conn, *, evaluator_digest: str = "sha256:" + "e" * 64) -> World:
    import base64
    import hashlib

    clock = FixedClock(T0)
    host = LocalSigner.generate("key_host")
    keylog = KeyLog(ledger, "host", host, clock)
    keylog.bootstrap()
    identity = Identity(ledger, keylog, clock, scale=SCALE, params=ID_PARAMS, host_ref="host", secrets_conn=profile_conn)
    device = Device("key_phone")
    identity.enrol_first_surface("pyr_1", device.key_id, device.public_jwk)
    # an agent (OAuth, session-bound) and the scheduler (key-bound), each enrolled only on the payer's approval
    client = identity.register_client("an agent", "https://agent.example/cb")
    e = identity.request_caller("pyr_1", client_id=client)
    verifier = "v" * 48
    pkce = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    agent = identity.approve_caller(e["enrolment_id"], device.respond(identity.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])),
                                    code_challenge=pkce)
    token = identity.token(agent["code"], verifier, client)["access_token"]
    scheduler = KeyAgent("key_scheduler", clock)
    e2 = identity.request_caller("pyr_1", public_jwk=scheduler.signer.public_jwk)
    scheduler.caller_id = identity.approve_caller(e2["enrolment_id"], device.respond(
        identity.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e2["subject_hash"])))["caller_id"]
    flow = Flow(ledger, ProfileStore(profile_conn, LocalKek.generate(), clock), keylog, identity, clock, random.Random(1), evaluator_digest=evaluator_digest)
    m = XXMerchant("b", clock)
    ins = StandingInstruction("mrc_shop", "fake-b", (CartLineTemplate("item_milk", 2), CartLineTemplate("item_bread", 1)), "dst_home", "xx:wallet", 1000, inr(19000))
    spokes = {
        "spk_grocery": Spoke("spk_grocery", "whl_1", "explicit_tap", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=(agent["caller_id"],),
                             caps=(Limit(inr(400000), MONTH),), rules=(rule("max_lines", "items", max_lines=3),)),
        "spk_repeat": Spoke("spk_repeat", "whl_1", "silent_with_notice", T0 - timedelta(days=1), T0 + timedelta(days=365), callers=(scheduler.caller_id,),
                            instruction=ins, caps=(Limit(inr(100000), MONTH),)),
    }
    params = replace(PARAMS, silent_max_amount=inr(200000), silent_preconditions=({"fact": "notice.delivered", "op": "eq", "value": True},))
    bank = BankFeed()
    flow.register(Deployment(Wheel("whl_1", "pyr_1", "xx", budgets=(Limit(inr(1500000), MONTH),)), spokes,
                             {"params": params, "rails": {"xx:upi": UPI, "xx:wallet": WALLET}, "tier0_rules": (), "jurisdiction_rules": (),
                              "jurisdiction": "xx"}, {"fake-b": m}, bank))
    return World(flow, m, device, clock, bank, host, token, scheduler)
