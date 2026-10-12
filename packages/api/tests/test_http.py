"""Phase 8: the HTTP API. An agent connects, the payer approves it, it proposes, the payer pays."""

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from gap_api.host import Host, read_digest
from gap_api.http import create_app
from gap_core.timefmt import format_ts
from gap_flow.testing import LINES, make_world
from gap_identity.devices import KeyAgent


@pytest.fixture
def w(ledger, profile_conn):
    return make_world(ledger, profile_conn)


@pytest.fixture
def api(w):
    return TestClient(create_app(Host(w.flow)))


def bearer(w, token=None, nonce=None):
    import secrets
    return {"Authorization": f"Bearer {token or w.agent_token}", "X-GAP-Nonce": nonce or secrets.token_urlsafe(8),
            "X-GAP-Created-At": format_ts(w.clock.now())}


def payload(cart="cart-1", intent="i-1", **kw):
    return {"spoke_id": "spk_grocery", "intent_id": intent, "merchant_ref": "mrc_shop", "adapter_id": "fake-b",
            "cart_ref": cart, "destination_ref": "dst_home", "agent_confidence": 950, **kw}


def test_an_agent_connects_only_with_the_payers_approval(w, api):
    client = api.post("/v1/oauth/register", json={"client_name": "Some Assistant", "redirect_uri": "https://a.example/cb"}).json()["client_id"]
    enr = api.post("/v1/wheels/whl_1/callers", json={"client_id": client})
    assert enr.status_code == 202 and enr.json()["status"] == "pending"
    eid = enr.json()["enrolment_id"]
    verifier = "z" * 50
    pkce = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ch = api.post(f"/v1/enrolments/{eid}/challenge").json()
    code = api.post(f"/v1/enrolments/{eid}/approve", json={"assertion": w.device.respond(ch), "code_challenge": pkce}).json()["code"]
    tok = api.post("/v1/oauth/token", json={"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": client})
    assert tok.status_code == 200 and tok.json()["token_type"] == "Bearer"
    assert api.post("/v1/oauth/token", json={"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
                                               "client_id": client}).status_code == 400  # codes work once


def test_propose_then_the_payer_approves_on_their_device(w, api):
    w.merchant.add_cart("cart-1", LINES)
    r = api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w))
    assert r.status_code == 201 and r.json()["status"] == "awaiting_approval"
    pid = r.json()["proposal_id"]
    hand = api.post(f"/v1/wheels/whl_1/proposals/{pid}/proceed-to-pay", headers=bearer(w)).json()
    assert hand == {"proposal_id": pid, "status": "awaiting_payer", "handoff": f"gap://approve/{pid}"}
    ch = api.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    done = api.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch)})
    assert done.status_code == 200 and done.json()["status"] == "executed" and done.json()["execution"]["outcome"] == "success"


def test_requests_without_valid_proof_are_401(w, api):
    w.merchant.add_cart("cart-1", LINES)
    assert api.post("/v1/wheels/whl_1/proposals", json=payload()).status_code == 401
    assert api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w, token="made-up")).json()["code"] == "sys.proof_invalid"
    h = bearer(w, nonce="same")
    assert api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=h).status_code == 201
    assert api.post("/v1/wheels/whl_1/proposals", json=payload(intent="i-2"), headers=h).json()["code"] == "sys.nonce_reused"


def test_unmodelled_fields_are_refused(w, api):
    w.merchant.add_cart("cart-1", LINES)
    r = api.post("/v1/wheels/whl_1/proposals", json=payload(tip_the_driver=500), headers=bearer(w))
    assert r.status_code == 422 and r.json()["code"] == "unmodelled_attribute"


def test_intent_conflict_is_409_and_a_repeat_returns_the_original(w, api):
    w.merchant.add_cart("cart-1", LINES)
    w.merchant.add_cart("cart-2", LINES[:1])
    first = api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w)).json()
    again = api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w)).json()
    assert again["repeat"] and again["proposal_id"] == first["proposal_id"]
    assert api.post("/v1/wheels/whl_1/proposals", json=payload("cart-2"), headers=bearer(w)).status_code == 409


def test_an_agent_sees_only_its_own_proposals_and_spokes(w, api):
    w.merchant.add_cart("cart-1", LINES)
    pid = api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w)).json()["proposal_id"]
    spokes = api.get("/v1/wheels/whl_1/spokes", headers=bearer(w)).json()
    assert [s["spoke_id"] for s in spokes] == ["spk_grocery"]  # not the scheduler's repeat spoke
    assert spokes[0]["caps"][0]["remaining_minor"] == 400000
    # the scheduler (a different caller) cannot see the agent's proposal
    path = f"/v1/wheels/whl_1/proposals/{pid}"
    proof = w.scheduler.proof(f"read:GET:{path}", read_digest("GET", path))
    r = api.get(path, headers={"X-GAP-Proof": json.dumps(proof)})
    assert r.status_code == 404
    assert api.get(path, headers=bearer(w)).json()["proposal_id"] == pid


def test_a_change_request_changes_nothing(w, api):
    r = api.post("/v1/wheels/whl_1/change-requests", json={"spoke_id": "spk_grocery", "change": "raise the cap to 6,000"}, headers=bearer(w))
    assert r.status_code == 202 and r.json()["status"] == "requested"
    assert w.flow.deployments["whl_1"].spokes["spk_grocery"].caps[0].limit.amount_minor == 400000
    ev = [e for e in w.flow.ledger.records("whl_1", "wheel_event")]
    assert ev[0]["event_type"] == "change_requested" and ev[0]["initiated_by"] == "caller"
    assert api.post("/v1/wheels/whl_1/change-requests", json={"spoke_id": "spk_repeat", "change": "x"}, headers=bearer(w)).status_code == 404


def test_the_payer_endpoints_need_the_device(w, api):
    w.merchant.add_cart("cart-1", LINES)
    pid = api.post("/v1/wheels/whl_1/proposals", json=payload(), headers=bearer(w)).json()["proposal_id"]
    ch = api.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    forged = {**w.device.respond(ch), "key_id": "key_someone_else"}
    assert api.post(f"/v1/proposals/{pid}/approve", json={"assertion": forged}).status_code == 403
