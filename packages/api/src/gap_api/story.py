"""The worked example behind `make demo`: one payment, told end to end, every claim checked.

An agent connected over MCP proposes a grocery order; the payer approves on their device over HTTP; the
merchant charges more than approved; the bank shows it; reconciliation opens a finding; the payer and the
merchant each get an evidence bundle; the open verifier checks both; someone edits the payer's bundle to
hide the overcharge and the verifier catches it. Deterministic up to random IDs and keys (normalise()).
"""

from __future__ import annotations

import asyncio
import copy
import json
import random
import re
from datetime import timedelta

from fastapi.testclient import TestClient
from mcp import Client

from gap_evidence.bundle import EvidenceBuilder, evaluator_digest
from gap_evidence.render import render_html, render_text
from gap_evidence.verify import verify_bundle
from gap_flow.testing import LINES, make_world
from gap_identity.devices import bearer_proof

from .host import Host
from .http import create_app
from .mcp_server import build_server

_ID = re.compile(r"\b(prp|det|res|pst|txg|exa|swp|fnd|bnd|rls|key|chl|asr|clr|enr|rcv|evt|ntc)_[0-9a-z]{26}\b")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def normalise(text: str) -> str:
    """Replace random IDs and hashes with stable placeholders, numbered by first appearance."""
    seen: dict[str, str] = {}

    def sub(m):
        v = m.group(0)
        if v not in seen:
            prefix = v.split("_")[0] if "_" in v and not v.startswith("sha256") else "sha256"
            n = sum(1 for k in seen.values() if k.startswith(prefix + "#")) + 1
            seen[v] = f"{prefix}#{n}"
        return seen[v]

    return _HASH.sub(sub, _ID.sub(sub, text))


async def _call(server, tool, **args):
    async with Client(server) as c:
        r = await c.call_tool(tool, args)
        if r.structured_content is not None:
            return r.structured_content.get("result", r.structured_content)
        return json.loads(r.content[0].text)


def run_story(ledger, profile_conn) -> dict:
    w = make_world(ledger, profile_conn, evaluator_digest=evaluator_digest())
    host = Host(w.flow)
    mcp = build_server(host, "whl_1", lambda i, d: bearer_proof(w.agent_token, w.clock))
    http = TestClient(create_app(host))
    lines = []
    say = lines.append

    w.merchant.add_cart("cart-1", LINES)
    w.merchant.script = ["overcharge:700"]
    out = asyncio.run(_call(mcp, "propose_order", spoke_id="spk_grocery", merchant_ref="mrc_shop", adapter_id="fake-b",
                            cart_ref="cart-1", destination_ref="dst_home", intent_id="weekly-groceries", agent_confidence=950))
    pid = out["proposal_id"]
    say(f"1. The agent proposes over MCP: {out['determination']['outcome']}, {out['status'].replace('_', ' ')} "
        f"(rail {out['determination']['rail_id']}, amount {out['determination']['amount_binding'].replace('_', ' ')})")
    w.clock.advance(timedelta(seconds=40))
    hand = asyncio.run(_call(mcp, "proceed_to_pay", proposal_id=pid))
    say(f"2. The agent hands off to the payer ({hand['status'].replace('_', ' ')}); it has no way to approve")
    w.clock.advance(timedelta(minutes=2))
    ch = http.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    done = http.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch)}).json()
    say(f"3. The payer approves on their device: {done['status']}, merchant order {done['execution']['merchant_order_ref']}")
    w.clock.advance(timedelta(minutes=50))
    w.pay_bank(w.flow.proposals[pid].result)
    w.clock.advance(timedelta(minutes=10))
    w.flow.tick(sweep=True)
    opened = [f for f in w.flow.recon.open_findings("whl_1")]
    say(f"4. The bank shows the debit; reconciliation opens: {', '.join(f['kind'] for f in opened)} "
        f"({opened[0]['detail']['excess'] / 100:.2f} INR more than approved)")

    builder = EvidenceBuilder(w.flow.ledger, w.flow.keylog, w.flow.profile, w.clock, random.Random(7))
    payer = builder.build("whl_1", pid, role="payer", requester_ref="pyr_1")
    merchant = builder.build("whl_1", pid, role="merchant", requester_ref="mrc_shop")
    rp = verify_bundle(payer, w.host.public_jwk, evaluator=evaluator_digest())
    rm = verify_bundle(merchant, w.host.public_jwk, evaluator=evaluator_digest())
    say(f"5. The payer's bundle: {len(payer['records'])} records; verifier passes: {rp['ok']}; replay: {rp['replay']}; "
        f"divergence: {payer['divergence']['primary'].replace('_', ' ')}")
    say(f"6. The merchant's bundle: {[r['record_type'] for r in merchant['records']]} only; verifier passes: {rm['ok']}")
    forged = copy.deepcopy(payer)
    next(r for r in forged["records"] if r["record_type"] == "posting" and r["kind"] == "overcapture")["amount"]["amount_minor"] = 0
    rf = verify_bundle(forged, w.host.public_jwk, evaluator=evaluator_digest())
    say(f"7. Someone edits the payer's bundle to hide the overcharge: verifier passes: {rf['ok']}")
    releases = w.flow.ledger.records("releases:host", "release")
    say(f"8. Every disclosure is logged: {[(r['requester_role'], r['basis']) for r in releases]}")
    return {"steps": "\n".join(lines) + "\n", "payer_text": render_text(payer, rp), "payer_html": render_html(payer, rp),
            "merchant_html": render_html(merchant, rm)}
