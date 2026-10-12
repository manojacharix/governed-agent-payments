"""The governance MCP server (build plan Phase 8, 16): how an AI agent uses the layer.

One server instance serves one enrolled agent on one wheel; `credential()` returns a fresh proof for each
call (a bearer session with a new nonce, or a key-bound signature). The tools let an agent propose, read
its own decisions and spokes, ask for a change, and hand a proposal to the payer. There is no tool to
approve, override or decline: agents never authorise (invariant 7).
"""

from __future__ import annotations

from typing import Callable

from mcp.server.mcpserver import MCPServer

from gap_flow.flow import request_digest

from .host import Host, HostError, parse_request, read_digest

TOOLS = ("propose_order", "get_determination", "list_proposals", "list_spokes", "request_change", "proceed_to_pay")


def build_server(host: Host, wheel_id: str, credential: Callable[[str, str], dict]) -> MCPServer:
    """credential(intent_id, digest) -> proof. For bearer sessions the arguments are ignored."""
    server = MCPServer("governed-agent-payments", instructions=(
        "Propose purchases for the payer and read the outcome. You cannot approve payments: when a proposal needs the "
        "payer, call proceed_to_pay and tell them to open it on their device. Every failing rule is reported with its author."))

    def reader(name: str):
        intent, path = f"read:MCP:{name}", name
        return host.caller_for_read(wheel_id, credential(intent, read_digest("MCP", path)), "MCP", path)

    def safe(fn):
        try:
            return fn()
        except HostError as e:
            return {"error": e.code, **({"detail": e.detail} if e.detail else {})}

    @server.tool()
    def propose_order(spoke_id: str, merchant_ref: str, adapter_id: str, cart_ref: str, destination_ref: str, intent_id: str,
                      agent_confidence: int, rail_id: str | None = None) -> dict:
        """Propose paying for a cart already built at the merchant. Returns the decision: allow (awaiting the payer's tap,
        or a silent repeat with notice), clarify, refuse (with every failing rule and who wrote it) or no_authority."""
        payload = {"spoke_id": spoke_id, "intent_id": intent_id, "merchant_ref": merchant_ref, "adapter_id": adapter_id,
                   "cart_ref": cart_ref, "destination_ref": destination_ref, "agent_confidence": agent_confidence}
        if rail_id:
            payload["rail_id"] = rail_id
        def go():
            req = parse_request(payload)  # validated before anything is hashed or signed
            return host.propose(wheel_id, payload, credential(intent_id, request_digest(req)))
        return safe(go)

    @server.tool()
    def get_determination(proposal_id: str) -> dict:
        """The current state of one of your proposals."""
        return safe(lambda: host.get(proposal_id, reader("get_determination")))

    @server.tool()
    def list_proposals() -> dict:
        """Your proposals on this wheel and their status."""
        return safe(lambda: {"proposals": host.list(wheel_id, reader("list_proposals"))})

    @server.tool()
    def list_spokes() -> dict:
        """The spokes you may cite, with their approval mode and the room left this window."""
        return safe(lambda: {"spokes": host.spokes(wheel_id, reader("list_spokes"))})

    @server.tool()
    def request_change(spoke_id: str, change: str) -> dict:
        """Ask the payer to change a spoke (for example a higher cap). Nothing changes until they approve it on their device."""
        return safe(lambda: host.request_change(wheel_id, reader("request_change"), spoke_id, change))

    @server.tool()
    def proceed_to_pay(proposal_id: str) -> dict:
        """Hand a proposal to the payer to approve on their device. This does not approve it."""
        return safe(lambda: host.proceed_to_pay(proposal_id, reader("proceed_to_pay")))

    return server
