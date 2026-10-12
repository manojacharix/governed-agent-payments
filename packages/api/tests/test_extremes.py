"""Extreme and hostile inputs at every entry point (stress campaign, 2026-10-12).

Each case was found by probing: before the fix it crashed the server (500), was silently coerced, or was
accepted when it should not be. The rule now: every bad input is a clean 4xx with nothing recorded, merchant
data that breaks the money model is refused, and concurrent requests cannot interleave.
"""

import json
import secrets
import threading
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from gap_api.host import Host
from gap_api.http import create_app
from gap_core.money import Money
from gap_core.proposal import CartLine
from gap_core.timefmt import format_ts
from gap_flow.testing import LINES, make_world


@pytest.fixture
def w(ledger, profile_conn):
    w = make_world(ledger, profile_conn)
    w.merchant.add_cart("cart-1", LINES)
    return w


@pytest.fixture
def api(w):
    return TestClient(create_app(Host(w.flow)), raise_server_exceptions=False)


def H(w, **kw):
    return {"Authorization": f"Bearer {w.agent_token}", "X-GAP-Nonce": secrets.token_urlsafe(8),
            "X-GAP-Created-At": format_ts(w.clock.now()), **kw}


def P(cart="cart-1", **kw):
    return {"spoke_id": "spk_grocery", "intent_id": secrets.token_hex(4), "merchant_ref": "mrc_shop", "adapter_id": "fake-b",
            "cart_ref": cart, "destination_ref": "dst_home", "agent_confidence": 950, **kw}


def records(w):
    return len(w.flow.ledger.records("whl_1"))


BAD_PROPOSALS = {
    "confidence float": {"agent_confidence": 950.9},
    "confidence bool": {"agent_confidence": True},
    "confidence string": {"agent_confidence": "950"},
    "confidence huge": {"agent_confidence": 10**30},
    "confidence negative": {"agent_confidence": -1},
    "confidence null": {"agent_confidence": None},
    "spoke null": {"spoke_id": None},
    "spoke int": {"spoke_id": 5},
    "spoke list": {"spoke_id": ["a"]},
    "spoke empty": {"spoke_id": ""},
    "intent 1MB": {"intent_id": "x" * 1_000_000},
    "intent 129 chars": {"intent_id": "x" * 129},
    "intent NUL": {"intent_id": "a\x00b"},
    "intent newline": {"intent_id": "a\nb"},
    "merchant NUL": {"merchant_ref": "m\x00"},
    "rail int": {"rail_id": 7},
    "facts not list": {"facts": "x"},
    "facts of strings": {"facts": ["x"]},
    "fact missing value": {"facts": [{"name": "a.b"}]},
    "fact float": {"facts": [{"name": "a.b", "value": 1.5}]},
    "fact null": {"facts": [{"name": "a.b", "value": None}]},
    "fact huge int": {"facts": [{"name": "a.b", "value": 10**40}]},
    "fact deep nesting": {"facts": [{"name": "a.b", "value": json.loads("[" * 5000 + "]" * 5000)}]},
    "fact 10KB": {"facts": [{"name": "a.b", "value": "x" * 10_000}]},
    "fact bad name": {"facts": [{"name": "Not A Name", "value": 1}]},
    "fact repeated": {"facts": [{"name": "a.b", "value": 1}, {"name": "a.b", "value": 2}]},
    "fact extra key": {"facts": [{"name": "a.b", "value": 1, "verified": True}]},
    "33 facts": {"facts": [{"name": f"x.f{i}", "value": i} for i in range(33)]},
    "unmodelled field": {"tip": 5},
}


@pytest.mark.parametrize("case", BAD_PROPOSALS)
def test_bad_proposals_are_clean_4xx_and_record_nothing(w, api, case):
    before = records(w)
    r = api.post("/v1/wheels/whl_1/proposals", json=P(**BAD_PROPOSALS[case]), headers=H(w))
    assert 400 <= r.status_code < 500, r.text[:200]
    assert records(w) == before


def test_a_lone_surrogate_is_refused(w, api):
    raw = json.dumps(P()).replace('"cart-1"', '"\\ud800"').encode()
    r = api.post("/v1/wheels/whl_1/proposals", content=raw, headers={**H(w), "content-type": "application/json"})
    assert r.status_code == 422


@pytest.mark.parametrize("headers", [{"X-GAP-Nonce": "n" * 100_000}, {"X-GAP-Nonce": "n" * 129}, {"X-GAP-Nonce": ""},
                                     {"X-GAP-Created-At": "yesterday"}, {"X-GAP-Created-At": "9999-12-31T23:59:59.999Z"},
                                     {"Authorization": "Bearer "}, {"Authorization": "Bearer " + "t" * 600}])
def test_bad_credentials_are_401(w, api, headers):
    assert api.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w, **headers)).status_code == 401


@pytest.mark.parametrize("proof", ["[1,2]", '"x"', "{}", '{"format": 5}', '{"format": "bearer_session", "token": ["a"]}', "not json"])
def test_malformed_key_proofs_are_401(w, api, proof):
    assert api.get("/v1/wheels/whl_1/spokes", headers={"X-GAP-Proof": proof}).status_code == 401


@pytest.mark.parametrize("proof", ["x", 5, [], {}])
def test_malformed_body_proofs_are_401(w, api, proof):
    assert api.post("/v1/wheels/whl_1/proposals", json=P(proof=proof)).status_code == 401


def test_oversized_bodies_are_413(w, api):
    assert api.post("/v1/wheels/whl_1/proposals", content=b"{" + b" " * 70_000 + b"}", headers={**H(w), "content-type": "application/json"}).status_code == 413


@pytest.mark.parametrize("path,body", [
    ("/v1/oauth/register", {}),
    ("/v1/oauth/register", {"client_name": "x" * 201, "redirect_uri": "https://a.example/cb"}),
    ("/v1/oauth/register", {"client_name": "a", "redirect_uri": "javascript:alert(1)"}),
    ("/v1/oauth/register", {"client_name": "a", "redirect_uri": "http://evil.example/cb"}),
    ("/v1/oauth/token", {"grant_type": "authorization_code"}),
    ("/v1/oauth/token", {"grant_type": "authorization_code", "code": 5, "code_verifier": [], "client_id": {}}),
    ("/v1/wheels/whl_1/callers", {}),
    ("/v1/wheels/whl_1/callers", {"client_id": "cli_never_registered"}),
    ("/v1/wheels/whl_1/callers", {"public_jwk": {"kty": "EC"}}),
    ("/v1/wheels/whl_1/callers", {"public_jwk": {"kty": "EC", "crv": "P-256", "x": "AAAA", "y": "AAAA"}}),
    ("/v1/wheels/whl_1/callers", {"public_jwk": "x"}),
])
def test_enrolment_and_oauth_inputs_are_validated(w, api, path, body):
    r = api.post(path, json=body)
    assert 400 <= r.status_code < 500, r.text[:200]


@pytest.mark.parametrize("uri", ["http://127.0.0.1:8765/cb", "http://localhost/cb", "http://[::1]:9000/cb", "https://a.example/cb"])
def test_loopback_and_https_redirects_are_allowed(api, uri):
    assert api.post("/v1/oauth/register", json={"client_name": "cli", "redirect_uri": uri}).status_code == 200


@pytest.mark.parametrize("uri", [
    "http://localhost.evil.example/cb", "http://localhost@evil.example/cb", "http://127.0.0.1.evil.example/cb",
    "http://127.0.0.1:80@evil.example/", "http://[::1].evil.example/", "https://user@a.example/cb", "https:///cb",
    "https://a.example/cb#frag", "http://evil.example/cb", "HTTP://localhost:abc/", "https://a.example/\\@evil.example",
    "https://a.example/ cb", "https://a.example/\ncb", "data:text/html,x", "javascript:alert(1)//https://a.example",
])
def test_lookalike_redirects_are_refused(api, uri):
    """Security review: prefix matching let localhost.evil.example and localhost@evil.example pass as loopback."""
    assert api.post("/v1/oauth/register", json={"client_name": "cli", "redirect_uri": uri}).status_code == 422


def test_payer_endpoints_validate_their_inputs(w, api):
    pid = api.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    for body in ({"action": "steal"}, {"action": "override", "overridden": "x"}, {"action": "override", "overridden": [1]}, {}):
        assert api.post(f"/v1/proposals/{pid}/challenges", json=body).status_code == 422
    for body in ({"assertion": "x"}, {"assertion": []}, {"assertion": {}}, {}, {"assertion": {"challenge_id": 5}}):
        assert 400 <= api.post(f"/v1/proposals/{pid}/approve", json=body).status_code < 500
    assert api.post(f"/v1/proposals/{pid}/override", json={"assertion": {}, "clauses": 5}).status_code == 422
    assert w.flow.proposals[pid].status == "awaiting_approval"  # none of that touched it


@pytest.mark.parametrize("body", [{}, {"spoke_id": "spk_grocery"}, {"spoke_id": "spk_grocery", "change": {"a": 1.5}},
                                  {"spoke_id": "spk_grocery", "change": "x" * 1001}, {"spoke_id": "spk_grocery", "change": "\x00"}])
def test_change_requests_are_validated(w, api, body):
    assert api.post("/v1/wheels/whl_1/change-requests", json=body, headers=H(w)).status_code == 422


# --- the merchant is outside the trust boundary ---------------------------------------------------------

@pytest.mark.parametrize("cart,lines,code", [
    ("nope", None, "cart_unavailable"),
    ("huge", (CartLine("item_x", 10**9, Money(10**8, "INR")),), "cart_invalid"),
    ("usd", (CartLine("item_x", 1, Money(100, "USD")),), "cart_invalid"),
    ("mixed", (CartLine("item_x", 1, Money(100, "INR")), CartLine("item_y", 1, Money(100, "USD"))), "cart_invalid"),
    ("nul", (CartLine("item\x00x", 1, Money(100, "INR")),), "cart_invalid"),
])
def test_bad_merchant_carts_are_refused_cleanly(w, api, cart, lines, code):
    if lines:
        w.merchant.add_cart(cart, lines)
    before = records(w)
    r = api.post("/v1/wheels/whl_1/proposals", json=P(cart=cart), headers=H(w))
    assert r.status_code == 422 and r.json()["code"] == code
    assert records(w) == before


def test_unknown_adapter_is_refused(w, api):
    r = api.post("/v1/wheels/whl_1/proposals", json=P(adapter_id="nope"), headers=H(w))
    assert r.status_code == 422 and r.json()["code"] == "unknown_adapter"


def test_a_cart_that_goes_bad_before_approval_is_refused_not_crashed(w, api):
    pid = api.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    del w.merchant.carts["cart-1"]  # the merchant lost the cart between presentation and the tap
    ch = api.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    r = api.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch)})
    assert r.status_code == 200 and r.json()["status"] == "refused"
    assert not w.merchant.orders
    res = [x for x in w.flow.ledger.records("whl_1", "resolution") if x["proposal_id"] == pid]
    assert res[-1]["state"] == "declined" and res[-1]["by"] == "system"


def test_one_bad_cart_does_not_stop_the_tick_for_everyone(w):
    from datetime import timedelta
    w.merchant.add_cart("rep-1", LINES)
    w.merchant.add_cart("rep-2", LINES)
    a = w.propose(w.request("rep-1", intent="si-1", spoke="spk_repeat"))
    w.clock.advance(timedelta(days=8))
    b = w.propose(w.request("rep-2", intent="si-2", spoke="spk_repeat"))
    assert a.status == b.status == "notice"
    for p in (a, b):
        w.flow.notice_delivered(p.proposal_id)
    del w.merchant.carts["rep-1"]
    w.clock.advance(timedelta(hours=1))
    w.flow.tick()
    assert a.status == "refused" and b.status == "executed"


# --- boundaries -----------------------------------------------------------------------------------------

def test_exactly_at_the_cap_is_allowed_and_one_paisa_over_is_not(w, api):
    w.merchant.add_cart("at", (CartLine("item_x", 1, Money(400000 - 2500, "INR")),))  # fake-b adds a 25 rupee platform fee
    w.merchant.add_cart("over", (CartLine("item_x", 1, Money(400000 - 2500 + 1, "INR")),))
    assert api.post("/v1/wheels/whl_1/proposals", json=P(cart="at"), headers=H(w)).json()["determination"]["outcome"] == "allow"
    over = api.post("/v1/wheels/whl_1/proposals", json=P(cart="over"), headers=H(w)).json()["determination"]
    assert over["outcome"] == "refuse" and [c["rule_id"] for c in over["failing_clauses"]] == ["sys.cap_exceeded"]


def test_agent_claims_never_replace_host_facts(w, api):
    """Host facts with the same name win; an agent cannot claim the spoke has no open findings or no recovery."""
    r = api.post("/v1/wheels/whl_1/proposals", json=P(facts=[{"name": "recovery.pending", "value": False},
                                                               {"name": "findings.open_on_spoke", "value": 0}]), headers=H(w))
    assert r.status_code == 201
    w.flow.identity.recovery_pending = lambda payer: True  # now the host knows better
    r = api.post("/v1/wheels/whl_1/proposals", json=P(facts=[{"name": "recovery.pending", "value": False}]), headers=H(w))
    assert r.json()["determination"]["outcome"] != "allow" or r.json()["determination"]["approval_mode"] == "explicit_tap"


# --- concurrency ----------------------------------------------------------------------------------------

def test_concurrent_approvals_of_one_proposal_execute_once(w, api):
    pid = api.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    chs = [api.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json() for _ in range(4)]
    outs = []
    threads = [threading.Thread(target=lambda c=c: outs.append(api.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(c)})))
               for c in chs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(o.status_code for o in outs) == [200, 403, 403, 403]
    assert len(w.merchant.orders) == 1


def test_concurrent_approvals_never_overspend_the_cap(w, api):
    pids = []
    for i in range(12):
        w.merchant.add_cart(f"r{i}", (CartLine("item_x", 1, Money(97500, "INR")),))  # 1,000 rupees with the fee; cap 4,000
        pids.append(api.post("/v1/wheels/whl_1/proposals", json=P(cart=f"r{i}"), headers=H(w)).json()["proposal_id"])
    chs = [api.post(f"/v1/proposals/{p}/challenges", json={"action": "authorise"}).json() for p in pids]
    outs = []
    threads = [threading.Thread(target=lambda p=p, c=c: outs.append(api.post(f"/v1/proposals/{p}/approve", json={"assertion": w.device.respond(c)})))
               for p, c in zip(pids, chs)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(o.status_code == 200 for o in outs)
    st = Counter(o.json()["status"] for o in outs)
    assert st["executed"] == 4 and st["refused"] == 8
    assert len(w.merchant.orders) == 4
