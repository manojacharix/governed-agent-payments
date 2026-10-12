"""The host service: one place both interfaces (HTTP and MCP) call, so they cannot drift apart.

Agents (callers) can propose, read their own proposals and their own spokes' room, ask for a change,
and hand a proposal off to the payer. They can never approve (invariant 7). The payer's device answers
challenges. Every caller-facing read follows verify-don't-see (invariant 22): an agent sees only what it
proposed and the spokes bound to it, never another spoke's rules, caps or spending.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from gap_core.authority import Caller
from gap_core.evaluate import IntentConflict, Repeat
from gap_core.facts import Fact
from gap_core.hashing import hash_value
from gap_core.ids import new_id
from gap_core.timefmt import format_ts
from gap_flow.flow import Flow, FlowError, Pending, ProposalRequest, Rejected
from gap_identity.identity import IdentityError
from gap_ledger.money import replay

REQUEST_FIELDS = {"spoke_id", "intent_id", "merchant_ref", "adapter_id", "cart_ref", "destination_ref", "agent_confidence", "rail_id", "facts"}


class HostError(Exception):
    """A request the host refuses. `status` follows HTTP semantics; the MCP layer reports the message."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.status, self.code, self.detail = status, code, detail


def read_digest(method: str, path: str) -> str:
    return hash_value({"read": f"{method} {path}"})


class Host:
    def __init__(self, flow: Flow) -> None:
        self.flow, self.identity = flow, flow.identity

    # --- who is calling ------------------------------------------------------------------------

    def _payer(self, wheel_id: str) -> str:
        dep = self.flow.deployments.get(wheel_id)
        if dep is None:
            raise HostError(404, "unknown_wheel")
        return dep.wheel.payer_ref

    def caller_for_read(self, wheel_id: str, proof: dict, method: str, path: str) -> Caller:
        try:
            return self.identity.authenticate_proposal(proof, payer_ref=self._payer(wheel_id), intent_id=f"read:{method}:{path}",
                                                       digest=read_digest(method, path))
        except IdentityError as e:
            raise HostError(401, e.reason if e.reason.startswith("sys.") else "sys.proof_invalid") from None

    # --- views ---------------------------------------------------------------------------------

    @staticmethod
    def view(p: Pending) -> dict:
        d = p.determination
        out = {
            "proposal_id": p.proposal_id, "wheel_id": p.wheel_id, "spoke_id": p.request.spoke_id, "status": p.status,
            "intent_id": p.request.intent_id,
            "determination": {
                "determination_id": d.determination_id, "outcome": d.outcome, "approval_mode": d.approval_mode,
                "failing_clauses": [c.to_json() for c in d.failing_clauses], "clarify_fields": list(d.clarify_fields),
                **({"rail_id": d.rail_id, "amount_binding": d.amount_binding} if d.rail_id else {}),
                **({"presented_hash": d.presented_hash} if d.presented_hash else {}),
            },
        }
        if p.notice_until:
            out["notice_until"] = format_ts(p.notice_until)
        res = p.result
        if res is not None and hasattr(res, "outcome"):
            out["execution"] = {"outcome": res.outcome, **({"merchant_order_ref": res.merchant_order_ref} if res.merchant_order_ref else {})}
        return out

    # --- agent actions --------------------------------------------------------------------------

    def propose(self, wheel_id: str, payload: dict, proof: dict) -> dict:
        unknown = set(payload) - REQUEST_FIELDS
        if unknown:  # unmodelled attributes are refused (invariant 1)
            raise HostError(422, "unmodelled_attribute", ", ".join(sorted(unknown)))
        try:
            facts = tuple(Fact(f["name"], f["value"], f.get("source", "agent"), False, "capturer_signed") for f in payload.get("facts", []))
            req = ProposalRequest(payload["spoke_id"], Caller("unproven", "session_bound"), payload["intent_id"], payload["merchant_ref"],
                                  payload["adapter_id"], payload["cart_ref"], payload["destination_ref"], int(payload["agent_confidence"]),
                                  rail_id=payload.get("rail_id"), facts=facts)
        except (KeyError, TypeError, ValueError) as e:
            raise HostError(422, "bad_request", str(e)) from None
        self._payer(wheel_id)
        res = self.flow.propose(wheel_id, req, proof)
        if isinstance(res, Rejected):
            raise HostError(401, res.reason)
        if isinstance(res, IntentConflict):
            raise HostError(409, "sys.intent_conflict", "this intent_id was used for a different request")
        if isinstance(res, Repeat):
            original = next(p for p in self.flow.proposals.values() if p.determination.determination_id == res.original_determination_id)
            return {**self.view(original), "repeat": True}
        return self.view(res)

    def _own(self, proposal_id: str, caller: Caller) -> Pending:
        p = self.flow.proposals.get(proposal_id)
        if p is None or p.request.caller.caller_id != caller.caller_id:
            raise HostError(404, "unknown_proposal")  # someone else's proposal does not exist for this caller
        return p

    def get(self, proposal_id: str, caller: Caller) -> dict:
        return self.view(self._own(proposal_id, caller))

    def list(self, wheel_id: str, caller: Caller) -> list[dict]:
        return [self.view(p) for p in self.flow.proposals.values() if p.wheel_id == wheel_id and p.request.caller.caller_id == caller.caller_id]

    def spokes(self, wheel_id: str, caller: Caller, at: datetime | None = None) -> list[dict]:
        """Read-only: the spokes this caller may cite, with the room left this window. Nothing about any other spoke."""
        dep = self.flow.deployments[wheel_id]
        at = at or self.flow.clock.now()
        usage = replay(self.flow.ledger, wheel_id).usage()
        out = []
        for s in dep.spokes.values():
            if s.callers is None or caller.caller_id not in s.callers:
                continue
            caps = []
            for c in s.caps:
                used = usage.get((s.spoke_id, c.window.window_id(at)))
                spent = used.amounts.get(c.limit.currency, 0) if used else 0
                caps.append({"limit": c.limit.to_json(), "window": c.window.kind, "remaining_minor": max(0, c.limit.amount_minor - spent)})
            out.append({"spoke_id": s.spoke_id, "approval_mode": s.approval_mode, "status": s.status,
                        "valid_until": format_ts(s.valid_until), "caps": caps, "standing_instruction": s.instruction is not None})
        return out

    def request_change(self, wheel_id: str, caller: Caller, spoke_id: str, change: str) -> dict:
        """An agent may ask; nothing changes until the payer approves on a Trusted Surface (01 §5)."""
        if not any(s["spoke_id"] == spoke_id for s in self.spokes(wheel_id, caller)):
            raise HostError(404, "unknown_spoke")
        eid = new_id("evt", clock=self.flow.clock, rng=self.flow.rng)
        self.flow.ledger.append(wheel_id, {"record_type": "wheel_event", "event_id": eid, "event_type": "change_requested",
                                           "target": {"kind": "spoke", "id": spoke_id}, "initiated_by": "caller",
                                           "initiator_ref": caller.caller_id, "request_hash": hash_value(change),
                                           "occurred_at": format_ts(self.flow.clock.now())}, self.flow.signer, record_id=eid)
        return {"event_id": eid, "status": "requested", "note": "nothing changes until the payer approves it on their device"}

    def proceed_to_pay(self, proposal_id: str, caller: Caller) -> dict:
        """Hand-off only: the agent points the payer at the proposal; it cannot approve it."""
        p = self._own(proposal_id, caller)
        if p.status not in ("awaiting_approval", "notice"):
            raise HostError(409, "not_awaiting_payer", p.status)
        return {"proposal_id": proposal_id, "status": "awaiting_payer", "handoff": f"gap://approve/{proposal_id}"}

    # --- the payer's device ---------------------------------------------------------------------

    def challenge(self, proposal_id: str, action: str, overridden: list[str] | None = None) -> dict:
        if proposal_id not in self.flow.proposals:
            raise HostError(404, "unknown_proposal")
        ch = self.flow.challenge(proposal_id, action, overridden)
        return {k: ch[k] for k in ("challenge_id", "nonce", "action", "subject_hash", "expires_at")}

    def payer_act(self, proposal_id: str, action: str, assertion: dict, clauses: list[str] | None = None) -> dict:
        try:
            if action == "approve":
                res = self.flow.approve(proposal_id, assertion)
            elif action == "decline":
                res = self.flow.decline(proposal_id, assertion)
            elif action == "override":
                res = self.flow.override(proposal_id, clauses or [], assertion)
            else:
                raise HostError(400, "unknown_action")
        except FlowError as e:
            raise HostError(403, "assertion_refused", str(e)) from None
        return self.view(res)

    # --- enrolment (agents ask, the payer approves) ------------------------------------------------

    def register_client(self, client_name: str, redirect_uri: str) -> dict:
        return {"client_id": self.identity.register_client(client_name, redirect_uri)}

    def request_caller(self, wheel_id: str, *, client_id: str | None = None, public_jwk: dict | None = None) -> dict:
        try:
            e = self.identity.request_caller(self._payer(wheel_id), client_id=client_id, public_jwk=public_jwk)
        except IdentityError as err:
            raise HostError(422, err.reason) from None
        return {"enrolment_id": e["enrolment_id"], "subject_hash": e["subject_hash"], "status": "pending"}

    def enrolment_challenge(self, enrolment_id: str) -> dict:
        e = self.identity.enrolments.get(enrolment_id)
        if e is None:
            raise HostError(404, "unknown_enrolment")
        ch = self.identity.challenge(payer_ref=e["payer_ref"], action="enrol", subject_hash=e["subject_hash"])
        return {k: ch[k] for k in ("challenge_id", "nonce", "action", "subject_hash", "expires_at")}

    def approve_enrolment(self, enrolment_id: str, assertion: dict, code_challenge: str | None = None) -> dict:
        try:
            return self.identity.approve_caller(enrolment_id, assertion, code_challenge=code_challenge)
        except IdentityError as e:
            raise HostError(403, e.reason, str(e)) from None

    def token(self, form: dict) -> dict:
        try:
            if form.get("grant_type") == "authorization_code":
                return self.identity.token(form["code"], form["code_verifier"], form["client_id"])
            if form.get("grant_type") == "refresh_token":
                return self.identity.refresh_token(form["refresh_token"], form["client_id"])
        except (IdentityError, KeyError):
            raise HostError(400, "invalid_grant") from None
        raise HostError(400, "unsupported_grant_type")
