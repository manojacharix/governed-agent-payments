"""Phase 8 demo (make interfaces): an AI agent over MCP, the payer's device over HTTP, one host underneath."""

import asyncio
import json
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from mcp import Client

from gap_api.host import Host
from gap_api.http import create_app
from gap_api.mcp_server import build_server
from gap_flow.testing import LINES, make_world
from gap_identity.devices import bearer_proof
from gap_ledger.ledger import Ledger
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

ROOT = Path(__file__).resolve().parents[1]


async def call(server, tool, **args):
    async with Client(server) as c:
        if tool == "__list__":
            return [t.name for t in (await c.list_tools()).tools]
        r = await c.call_tool(tool, args)
        return r.structured_content.get("result", r.structured_content) if r.structured_content else json.loads(r.content[0].text)


pg = TempCluster()
try:
    pg.create_template("tl", LEDGER_SQL)
    pg.create_template("tp", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lc = psycopg.connect(pg.fresh_db("tl", "d8l"), autocommit=True)
    lc.execute("SET ROLE ledger_writer")
    pc = psycopg.connect(pg.fresh_db("tp", "d8p"), autocommit=True)
    pc.execute("SET ROLE profile_writer")
    w = make_world(Ledger(lc), pc)
    host = Host(w.flow)
    mcp = build_server(host, "whl_1", lambda i, d: bearer_proof(w.agent_token, w.clock))
    http = TestClient(create_app(host))

    print(f"1. The agent's MCP tools: {', '.join(asyncio.run(call(mcp, '__list__')))} (no tool can approve)")
    w.merchant.add_cart("cart-1", LINES)
    out = asyncio.run(call(mcp, "propose_order", spoke_id="spk_grocery", merchant_ref="mrc_shop", adapter_id="fake-b",
                           cart_ref="cart-1", destination_ref="dst_home", intent_id="weekly-shop", agent_confidence=950))
    print(f"2. Agent proposes over MCP -> {out['determination']['outcome']}, {out['status']}")
    print(f"3. Agent hands off: {asyncio.run(call(mcp, 'proceed_to_pay', proposal_id=out['proposal_id']))['handoff']}")
    ch = http.post(f"/v1/proposals/{out['proposal_id']}/challenges", json={"action": "authorise"}).json()
    done = http.post(f"/v1/proposals/{out['proposal_id']}/approve", json={"assertion": w.device.respond(ch)}).json()
    print(f"4. Payer approves on their device over HTTP -> {done['status']}, merchant order {done['execution']['merchant_order_ref']}")
    print(f"5. Agent reads back over MCP -> {asyncio.run(call(mcp, 'get_determination', proposal_id=out['proposal_id']))['status']}")
    spokes = asyncio.run(call(mcp, "list_spokes"))["spokes"]
    print(f"6. Agent's view of its spokes: {[(s['spoke_id'], s['caps'][0]['remaining_minor'] // 100) for s in spokes]} rupees left (nothing about other spokes)")
    r = asyncio.run(call(mcp, "request_change", spoke_id="spk_grocery", change="raise the grocery cap to 6,000 rupees"))
    print(f"7. Agent asks for a bigger cap -> {r['status']}: {r['note']}")
finally:
    pg.stop()
