"""Phase 8: an agent over MCP, and the second operator (B2B procurement) through the same interfaces."""

import asyncio
import random
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gap_adapters.fakes import FakeMerchant
from gap_api.host import Host
from gap_api.http import create_app
from gap_api.mcp_server import TOOLS, build_server
from gap_core.authority import Limit, Spoke, Wheel, Window
from gap_core.facts import Fact
from gap_core.money import Money
from gap_core.proposal import CartLine
from gap_flow.flow import Deployment, request_digest
from gap_flow.testing import LINES, T0, make_world
from gap_identity.devices import Device, KeyAgent, bearer_proof
from gap_mapping.load import evaluation_context, load_mapping, load_tier0
from mcp import Client

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def w(ledger, profile_conn):
    return make_world(ledger, profile_conn)


def mcp_call(server, tool, **args):
    async def go():
        async with Client(server) as c:
            if tool == "__list__":
                return [t.name for t in (await c.list_tools()).tools]
            res = await c.call_tool(tool, args)
            if res.structured_content is not None:
                out = res.structured_content
                return out.get("result", out) if isinstance(out, dict) else out
            import json
            return json.loads(res.content[0].text)
    return asyncio.run(go())


def test_the_mcp_server_has_no_way_to_approve(w):
    server = build_server(Host(w.flow), "whl_1", lambda i, d: bearer_proof(w.agent_token, w.clock))
    names = mcp_call(server, "__list__")
    assert set(names) == set(TOOLS)
    assert not any(n in names for n in ("approve", "approve_proposal", "override", "decline", "authorise"))


def test_an_agent_proposes_over_mcp_and_hands_off_to_the_payer(w):
    w.merchant.add_cart("cart-1", LINES)
    host = Host(w.flow)
    server = build_server(host, "whl_1", lambda i, d: bearer_proof(w.agent_token, w.clock))
    out = mcp_call(server, "propose_order", spoke_id="spk_grocery", merchant_ref="mrc_shop", adapter_id="fake-b", cart_ref="cart-1",
                   destination_ref="dst_home", intent_id="mcp-1", agent_confidence=950)
    assert out["status"] == "awaiting_approval" and out["determination"]["outcome"] == "allow"
    hand = mcp_call(server, "proceed_to_pay", proposal_id=out["proposal_id"])
    assert hand["handoff"].startswith("gap://approve/")
    spokes = mcp_call(server, "list_spokes")["spokes"]
    assert [s["spoke_id"] for s in spokes] == ["spk_grocery"]
    assert [p["proposal_id"] for p in mcp_call(server, "list_proposals")["proposals"]] == [out["proposal_id"]]
    # the payer approves on the device; the agent then reads the result
    pid = out["proposal_id"]
    w.flow.approve(pid, w.device.respond(w.flow.challenge(pid, "authorise")))
    got = mcp_call(server, "get_determination", proposal_id=pid)
    assert got["status"] == "executed"


def test_mcp_with_a_revoked_agent_is_refused(w):
    w.merchant.add_cart("cart-1", LINES)
    server = build_server(Host(w.flow), "whl_1", lambda i, d: bearer_proof("not-a-real-token", w.clock))
    out = mcp_call(server, "propose_order", spoke_id="spk_grocery", merchant_ref="mrc_shop", adapter_id="fake-b", cart_ref="cart-1",
                   destination_ref="dst_home", intent_id="mcp-1", agent_confidence=950)
    assert out == {"error": "sys.proof_invalid"}


# --- the second operator: B2B procurement, same core, same API ------------------------------------------

class ERP:
    """A host-side fact source: the company's ERP says whether a cart matches an approved purchase order."""

    def __init__(self):
        self.approved_carts = set()

    def __call__(self, req):
        ok = req.cart_ref in self.approved_carts
        return [Fact("ext.procurement.po_matched", ok, "erp", True, "source_signed"),
                Fact("ext.procurement.supplier_approved", ok, "erp", True, "source_signed")]


def b2b(w):
    india = load_mapping(ROOT / "mappings/in/v0.1.0.yaml")
    ctx = {**evaluation_context(india, load_tier0(ROOT / "tier0/examples/b2b_procurement_in.yaml", india)), "mapping_ref": india.ref}
    idn = w.flow.identity
    device = Device("key_corp_phone")
    idn.enrol_first_surface("pyr_corp", device.key_id, device.public_jwk)
    buyer = KeyAgent("key_buyer", w.clock)
    e = idn.request_caller("pyr_corp", public_jwk=buyer.signer.public_jwk)
    buyer.caller_id = idn.approve_caller(e["enrolment_id"], device.respond(idn.challenge(payer_ref="pyr_corp", action="enrol", subject_hash=e["subject_hash"])))["caller_id"]
    month = Window("calendar_month", "Asia/Kolkata")
    a, z = T0 - timedelta(days=1), T0 + timedelta(days=365)
    spokes = {"corp_company": Spoke("corp_company", "whl_corp", "explicit_tap", a, z, caps=(Limit(Money(50_000_000, "INR"), month),)),
              "corp_team": Spoke("corp_team", "whl_corp", "explicit_tap", a, z, parent_id="corp_company", caps=(Limit(Money(15_000_000, "INR"), month),)),
              "corp_agent": Spoke("corp_agent", "whl_corp", "explicit_tap", a, z, parent_id="corp_team", callers=(buyer.caller_id,),
                                  caps=(Limit(Money(4_000_000, "INR"), month),))}
    merchant = FakeMerchant("b", w.clock)
    erp = ERP()
    w.flow.register(Deployment(Wheel("whl_corp", "pyr_corp", "in", budgets=(Limit(Money(200_000_000, "INR"), month),)), spokes, ctx,
                               {"fake-b": merchant}, None, fact_sources=(erp,)))
    return device, buyer, merchant, erp


def test_the_procurement_operator_runs_through_the_same_api(w):
    device, buyer, merchant, erp = b2b(w)
    api = TestClient(create_app(Host(w.flow)))
    merchant.add_cart("po-cart", (CartLine("item_paper", 10, Money(45_000, "INR")),))
    merchant.add_cart("rogue-cart", (CartLine("item_paper", 10, Money(45_000, "INR")),))
    erp.approved_carts.add("po-cart")

    def propose(cart, intent):
        body = {"spoke_id": "corp_agent", "intent_id": intent, "merchant_ref": "mrc_stationer", "adapter_id": "fake-b", "cart_ref": cart,
                "destination_ref": "dst_office", "agent_confidence": 990,
                "facts": [{"name": "ext.procurement.po_matched", "value": True}]}  # the agent claims a PO; only the ERP counts
        from gap_core.authority import Caller
        from gap_flow.flow import ProposalRequest
        digest = request_digest(ProposalRequest("corp_agent", Caller("x", "key_bound"), intent, "mrc_stationer", "fake-b", cart, "dst_office", 990))
        return api.post("/v1/wheels/whl_corp/proposals", json={**body, "proof": buyer.proof(intent, digest)}).json()

    ok = propose("po-cart", "po-1")
    assert ok["determination"]["outcome"] == "allow" and ok["determination"]["rail_id"] == "in:upi_intent"
    rogue = propose("rogue-cart", "po-2")
    failing = {c["rule_id"]: c for c in rogue["determination"]["failing_clauses"]}
    assert {"op.po_matched", "op.approved_supplier"} <= set(failing)
    assert not failing["op.po_matched"]["overridable"]  # an operator clause: the payer cannot override it
