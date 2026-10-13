"""Durable state: a host restart loses nothing, and several host processes share one set of state safely.

Each "process" here is a full Host with its own connections to the same ledger, profile and state databases,
sharing only what real processes would share: the database, the host signing key (in a KMS), the key-encryption
key (in a KMS), the clock, and the outside world (merchant, bank, the payer's device).
"""

import random
import secrets
import threading
from collections import Counter
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from gap_api.host import Host
from gap_api.http import create_app
from gap_core.money import Money
from gap_core.proposal import CartLine
from gap_core.timefmt import format_ts
from gap_flow.flow import Flow
from gap_flow.testing import ID_PARAMS, LINES, SCALE, make_world
from gap_identity.identity import Identity
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_profile.store import ProfileStore
from gap_state.store import PgStore


def connect(dsn, role):
    c = psycopg.connect(dsn, autocommit=True)
    c.execute(f"SET ROLE {role}")
    return c


class Cluster:
    def __init__(self, ledger_dsn, profile_dsn, state_dsn):
        self.dsns = ledger_dsn, profile_dsn, state_dsn
        self.conns = []
        lc, pc, sc = self._conns()
        self.w = make_world(Ledger(lc), pc, store=PgStore(sc))
        self.hosts = [Host(self.w.flow)]

    def _conns(self):
        out = [connect(d, r) for d, r in zip(self.dsns, ("ledger_writer", "profile_writer", "state_writer"))]
        self.conns += out
        return out

    def process(self, seed: int) -> Host:
        """Start another host process over the same databases (or restart one)."""
        w = self.w
        lc, pc, sc = self._conns()
        ledger, store = Ledger(lc), PgStore(sc)
        keylog = KeyLog(ledger, "host", w.host, w.clock)  # the key log already exists: no bootstrap
        identity = Identity(ledger, keylog, w.clock, scale=SCALE, params=ID_PARAMS, host_ref="host", secrets_conn=pc, store=store)
        flow = Flow(ledger, ProfileStore(pc, w.flow.profile.kek, w.clock), keylog, identity, w.clock, random.Random(seed),
                    evaluator_digest=w.flow.evaluator_digest, store=store)
        flow.register(w.flow.deployments["whl_1"])  # the same configuration: recorded once, not again
        host = Host(flow)
        self.hosts.append(host)
        return host

    def close(self):
        for c in self.conns:
            c.close()


@pytest.fixture
def c(ledger_dsn, profile_dsn, state_dsn):
    cl = Cluster(ledger_dsn, profile_dsn, state_dsn)
    cl.w.merchant.add_cart("cart-1", LINES)
    yield cl
    cl.close()


def H(w):
    return {"Authorization": f"Bearer {w.agent_token}", "X-GAP-Nonce": secrets.token_urlsafe(8), "X-GAP-Created-At": format_ts(w.clock.now())}


def P(cart="cart-1", intent=None):
    return {"spoke_id": "spk_grocery", "intent_id": intent or secrets.token_hex(4), "merchant_ref": "mrc_shop", "adapter_id": "fake-b",
            "cart_ref": cart, "destination_ref": "dst_home", "agent_confidence": 950}


def api(host):
    return TestClient(create_app(host), raise_server_exceptions=False)


def test_a_restart_loses_nothing(c):
    w, before = c.w, api(c.hosts[0])
    pid = before.post("/v1/wheels/whl_1/proposals", json=P(intent="weekly"), headers=H(w)).json()["proposal_id"]
    ch = before.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    after = api(c.process(seed=2))  # the first process is gone; a new one starts on the same databases
    # the token, the pending proposal and the challenge all survived
    assert after.get(f"/v1/wheels/whl_1/proposals/{pid}", headers=H(w)).json()["status"] == "awaiting_approval"
    done = after.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch)}).json()
    assert done["status"] == "executed" and len(w.merchant.orders) == 1
    # the repeat check survived too: the same intent returns the original, a different cart under it conflicts
    assert after.post("/v1/wheels/whl_1/proposals", json=P(intent="weekly"), headers=H(w)).json()["proposal_id"] == pid
    # and registering the same configuration again recorded nothing new
    versions = [r for r in w.flow.ledger.records("whl_1") if r["record_type"] in ("wheel_version", "spoke_version")]
    assert len(versions) == 3


def test_a_challenge_is_single_use_across_processes(c):
    w, a, b = c.w, api(c.hosts[0]), api(c.process(seed=2))
    pid = a.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    ch = a.post(f"/v1/proposals/{pid}/challenges", json={"action": "decline"}).json()
    assertion = w.device.respond(ch)
    assert a.post(f"/v1/proposals/{pid}/decline", json={"assertion": assertion}).status_code == 200
    assert b.post(f"/v1/proposals/{pid}/decline", json={"assertion": assertion}).status_code == 403


def test_a_nonce_is_single_use_across_processes(c):
    w, a, b = c.w, api(c.hosts[0]), api(c.process(seed=2))
    h = H(w)
    assert a.post("/v1/wheels/whl_1/proposals", json=P(), headers=h).status_code == 201
    assert b.post("/v1/wheels/whl_1/proposals", json=P(), headers=h).json()["code"] == "sys.nonce_reused"


def test_refresh_tokens_rotate_across_processes(c):
    w = c.w
    idn_a, idn_b = c.hosts[0].identity, c.process(seed=2).identity
    client = idn_a.register_client("x", "https://x.example/cb")
    e = idn_a.request_caller("pyr_1", client_id=client)
    import base64, hashlib
    pkce = base64.urlsafe_b64encode(hashlib.sha256(b"v" * 50).digest()).rstrip(b"=").decode()
    code = idn_a.approve_caller(e["enrolment_id"], w.device.respond(idn_a.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])),
                                code_challenge=pkce)["code"]
    tok = idn_b.token(code, "v" * 50, client)  # code issued by one process, redeemed at another
    new = idn_a.refresh_token(tok["refresh_token"], client)
    from gap_identity.identity import IdentityError
    with pytest.raises(IdentityError):
        idn_b.refresh_token(tok["refresh_token"], client)  # rotated: the old one is dead everywhere
    assert idn_b.refresh_token(new["refresh_token"], client)["token_type"] == "Bearer"


def test_a_suspension_holds_across_processes(c):
    w, a, b = c.w, c.hosts[0], c.process(seed=2)
    a.identity.report_compromise("key_phone", w.clock.now())
    assert b.identity.wheel_suspended("pyr_1")
    out = api(b).post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()
    assert out["determination"]["outcome"] != "allow"


def _race(calls):
    outs = []
    ts = [threading.Thread(target=lambda f=f: outs.append(f())) for f in calls]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return outs


def test_two_processes_approving_one_proposal_execute_it_once(c):
    w = c.w
    a, b = api(c.hosts[0]), api(c.process(seed=2))
    pid = a.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    chs = [a.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json() for _ in range(6)]
    outs = _race([lambda ch=ch, x=(a if i % 2 else b): x.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch)})
                  for i, ch in enumerate(chs)])
    codes = Counter(o.status_code for o in outs)
    assert codes[200] == 1 and codes[200] + codes[403] + codes[409] == 6 and codes[500] == 0
    assert len(w.merchant.orders) == 1


def test_processes_racing_for_one_cap_never_overspend_it(c):
    w = c.w
    hosts = [api(c.hosts[0]), api(c.process(seed=2)), api(c.process(seed=3))]
    pids = []
    for i in range(12):
        w.merchant.add_cart(f"r{i}", (CartLine("item_x", 1, Money(97500, "INR")),))  # 1,000 rupees with the fee; the cap holds 4
        pids.append(hosts[0].post("/v1/wheels/whl_1/proposals", json=P(cart=f"r{i}"), headers=H(w)).json()["proposal_id"])
    chs = [hosts[0].post(f"/v1/proposals/{p}/challenges", json={"action": "authorise"}).json() for p in pids]
    outs = _race([lambda p=p, ch=ch, x=hosts[i % 3]: x.post(f"/v1/proposals/{p}/approve", json={"assertion": w.device.respond(ch)})
                  for i, (p, ch) in enumerate(zip(pids, chs))])
    assert all(o.status_code == 200 for o in outs), [o.text[:80] for o in outs if o.status_code != 200]
    st = Counter(o.json()["status"] for o in outs)
    assert st["executed"] == 4 and st["refused"] == 8 and len(w.merchant.orders) == 4


def test_two_schedulers_ticking_at_once_run_a_repeat_once(c):
    w = c.w
    a, b = c.hosts[0], c.process(seed=2)
    w.merchant.add_cart("rep", LINES)
    p = w.propose(w.request("rep", intent="si-1", spoke="spk_repeat"))
    assert p.status == "notice"
    w.flow.notice_delivered(p.proposal_id)
    w.clock.advance(timedelta(hours=1))
    _race([a.tick, b.tick, a.tick, b.tick])
    assert w.merchant.place_calls == 1
    assert c.hosts[0].flow.proposals[p.proposal_id].status == "executed"


def test_a_crash_at_the_point_of_no_return_never_allows_a_second_approval(c, monkeypatch):
    w = c.w
    a = api(c.hosts[0])
    pid = a.post("/v1/wheels/whl_1/proposals", json=P(), headers=H(w)).json()["proposal_id"]
    ch = a.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()

    def dies(*args, **kw):
        raise SystemExit("the process died mid-call to the merchant")
    monkeypatch.setattr(c.hosts[0].flow.executor, "execute", dies)
    with pytest.raises(SystemExit):
        c.hosts[0].payer_act(pid, "approve", w.device.respond(ch))
    survivor = api(c.process(seed=2))
    assert survivor.get(f"/v1/wheels/whl_1/proposals/{pid}", headers=H(w)).json()["status"] == "executing"
    ch2 = survivor.post(f"/v1/proposals/{pid}/challenges", json={"action": "authorise"}).json()
    assert survivor.post(f"/v1/proposals/{pid}/approve", json={"assertion": w.device.respond(ch2)}).status_code == 403
    c.hosts[-1].tick(sweep=True)
    assert w.merchant.place_calls == 0  # nobody re-ran it; reconciliation owns it from here


# --- security review (2026-10-13): a rolled-back unit must not undo what has to stick ----------------------

def _session_code(idn, w, verifier="v" * 50):
    import base64, hashlib
    client = idn.register_client("x", "https://x.example/cb")
    e = idn.request_caller("pyr_1", client_id=client)
    pkce = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = idn.approve_caller(e["enrolment_id"], w.device.respond(idn.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])),
                              code_challenge=pkce)["code"]
    return client, code


def test_wrong_recovery_answers_count_even_though_the_attempt_fails(c):
    from gap_identity.devices import Device
    from gap_identity.identity import IdentityError
    w, idn = c.w, c.hosts[0].identity
    from gap_core.hashing import hash_value
    idn.set_recovery_questions("pyr_1", ["alpha one", "beta two", "gamma three"],
                               w.device.respond(idn.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"recovery_questions": "pyr_1"}))))
    new = Device("key_new")
    for _ in range(5):
        with pytest.raises(IdentityError, match="wrong_answers"):
            idn.start_recovery("pyr_1", new_key_id=new.key_id, new_jwk=new.public_jwk, new_key_user_verification="inherence",
                               answers=["x x x", "y y y", "z z z"], external_verified=True)
    other = c.process(seed=2).identity  # the count is shared and durable
    with pytest.raises(IdentityError, match="locked"):
        other.start_recovery("pyr_1", new_key_id=new.key_id, new_jwk=new.public_jwk, new_key_user_verification="inherence",
                             answers=["alpha one", "beta two", "gamma three"], external_verified=True)


def test_a_code_is_spent_by_a_failed_redemption(c):
    from gap_identity.identity import IdentityError
    w, idn = c.w, c.hosts[0].identity
    client, code = _session_code(idn, w)
    with pytest.raises(IdentityError):
        idn.token(code, "wrong" * 10, client)  # wrong PKCE verifier
    with pytest.raises(IdentityError):
        idn.token(code, "v" * 50, client)  # the right one no longer helps: the code was single use


def test_a_refresh_token_is_spent_by_a_failed_refresh(c):
    from gap_identity.identity import IdentityError
    w, idn = c.w, c.hosts[0].identity
    client, code = _session_code(idn, w)
    tok = idn.token(code, "v" * 50, client)
    with pytest.raises(IdentityError):
        idn.refresh_token(tok["refresh_token"], "cli_someone_else")
    with pytest.raises(IdentityError):
        idn.refresh_token(tok["refresh_token"], client)
