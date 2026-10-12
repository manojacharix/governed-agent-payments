"""The HTTP API (build plan Phase 8). A thin layer over Host: no policy lives here (R1).

Agents authenticate every request: `Authorization: Bearer <token>` with `X-GAP-Nonce` and `X-GAP-Created-At`
(session-bound), or `X-GAP-Proof: <json>` signed with the caller's key (key-bound). Proposals carry the proof
in the body for key-bound callers. The payer's device authenticates by answering challenges.
"""

from __future__ import annotations

import json

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .host import Host, HostError


def _proof(authorization: str | None, nonce: str | None, created_at: str | None, x_proof: str | None) -> dict:
    if x_proof:
        try:
            return json.loads(x_proof)
        except ValueError:
            raise HTTPException(401, {"code": "sys.proof_invalid"}) from None
    if authorization and authorization.startswith("Bearer "):
        return {"format": "bearer_session", "token": authorization[7:], "nonce": nonce or "", "created_at": created_at or ""}
    raise HTTPException(401, {"code": "sys.proof_invalid", "detail": "no credentials"})


def create_app(host: Host) -> FastAPI:
    app = FastAPI(title="Governed Agent Payments", version="0.1.0")

    @app.exception_handler(HostError)
    async def _host_error(_req: Request, e: HostError):
        return JSONResponse({"code": e.code, **({"detail": e.detail} if e.detail else {})}, status_code=e.status)

    def caller(req: Request, wheel_id: str, authorization, nonce, created_at, x_proof):
        return host.caller_for_read(wheel_id, _proof(authorization, nonce, created_at, x_proof), req.method, req.url.path)

    # --- OAuth and enrolment ---------------------------------------------------------------------

    @app.post("/v1/oauth/register")
    def register(body: dict = Body(...)):
        return host.register_client(body["client_name"], body["redirect_uri"])

    @app.post("/v1/oauth/token")
    def token(body: dict = Body(...)):
        return host.token(body)

    @app.post("/v1/wheels/{wheel_id}/callers", status_code=202)
    def request_caller(wheel_id: str, body: dict = Body(...)):
        return host.request_caller(wheel_id, client_id=body.get("client_id"), public_jwk=body.get("public_jwk"))

    @app.post("/v1/enrolments/{enrolment_id}/challenge")
    def enrolment_challenge(enrolment_id: str):
        return host.enrolment_challenge(enrolment_id)

    @app.post("/v1/enrolments/{enrolment_id}/approve")
    def approve_enrolment(enrolment_id: str, body: dict = Body(...)):
        return host.approve_enrolment(enrolment_id, body["assertion"], body.get("code_challenge"))

    # --- agents ----------------------------------------------------------------------------------

    @app.post("/v1/wheels/{wheel_id}/proposals", status_code=201)
    def propose(wheel_id: str, body: dict = Body(...), authorization: str | None = Header(None),
                x_gap_nonce: str | None = Header(None), x_gap_created_at: str | None = Header(None)):
        proof = body.pop("proof", None) or _proof(authorization, x_gap_nonce, x_gap_created_at, None)
        return host.propose(wheel_id, body, proof)

    @app.get("/v1/wheels/{wheel_id}/proposals")
    def list_proposals(wheel_id: str, request: Request, authorization: str | None = Header(None), x_gap_nonce: str | None = Header(None),
                       x_gap_created_at: str | None = Header(None), x_gap_proof: str | None = Header(None)):
        return host.list(wheel_id, caller(request, wheel_id, authorization, x_gap_nonce, x_gap_created_at, x_gap_proof))

    @app.get("/v1/wheels/{wheel_id}/proposals/{proposal_id}")
    def get_proposal(wheel_id: str, proposal_id: str, request: Request, authorization: str | None = Header(None),
                     x_gap_nonce: str | None = Header(None), x_gap_created_at: str | None = Header(None), x_gap_proof: str | None = Header(None)):
        return host.get(proposal_id, caller(request, wheel_id, authorization, x_gap_nonce, x_gap_created_at, x_gap_proof))

    @app.get("/v1/wheels/{wheel_id}/spokes")
    def spokes(wheel_id: str, request: Request, authorization: str | None = Header(None), x_gap_nonce: str | None = Header(None),
               x_gap_created_at: str | None = Header(None), x_gap_proof: str | None = Header(None)):
        return host.spokes(wheel_id, caller(request, wheel_id, authorization, x_gap_nonce, x_gap_created_at, x_gap_proof))

    @app.post("/v1/wheels/{wheel_id}/change-requests", status_code=202)
    def change(wheel_id: str, request: Request, body: dict = Body(...), authorization: str | None = Header(None),
               x_gap_nonce: str | None = Header(None), x_gap_created_at: str | None = Header(None), x_gap_proof: str | None = Header(None)):
        c = caller(request, wheel_id, authorization, x_gap_nonce, x_gap_created_at, x_gap_proof)
        return host.request_change(wheel_id, c, body["spoke_id"], body["change"])

    @app.post("/v1/wheels/{wheel_id}/proposals/{proposal_id}/proceed-to-pay")
    def proceed(wheel_id: str, proposal_id: str, request: Request, authorization: str | None = Header(None),
                x_gap_nonce: str | None = Header(None), x_gap_created_at: str | None = Header(None), x_gap_proof: str | None = Header(None)):
        return host.proceed_to_pay(proposal_id, caller(request, wheel_id, authorization, x_gap_nonce, x_gap_created_at, x_gap_proof))

    # --- the payer's device -------------------------------------------------------------------------

    @app.post("/v1/proposals/{proposal_id}/challenges")
    def challenge(proposal_id: str, body: dict = Body(...)):
        return host.challenge(proposal_id, body["action"], body.get("overridden"))

    @app.post("/v1/proposals/{proposal_id}/{action}")
    def payer_act(proposal_id: str, action: str, body: dict = Body(...)):
        if action not in ("approve", "decline", "override"):
            raise HTTPException(404)
        return host.payer_act(proposal_id, action, body["assertion"], body.get("clauses"))

    return app
