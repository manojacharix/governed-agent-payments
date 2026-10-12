"""Phase 7 demo (make identity): who is acting, proven by keys the payer enrolled."""

from datetime import timedelta
from pathlib import Path

import psycopg

from gap_core.hashing import hash_value
from gap_flow.flow import Rejected
from gap_flow.testing import LINES, make_world
from gap_identity.devices import Device, KeyAgent, bearer_proof, webauthn_response
from gap_identity.identity import Identity, IdentityError
from gap_identity.webauthn import SoftwareAuthenticator
from gap_ledger.ledger import Ledger
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

ROOT = Path(__file__).resolve().parents[1]
pg = TempCluster()
try:
    pg.create_template("tl", LEDGER_SQL)
    pg.create_template("tp", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lc = psycopg.connect(pg.fresh_db("tl", "d7l"), autocommit=True)
    lc.execute("SET ROLE ledger_writer")
    pc = psycopg.connect(pg.fresh_db("tp", "d7p"), autocommit=True)
    pc.execute("SET ROLE profile_writer")
    w = make_world(Ledger(lc), pc)
    i = w.flow.identity
    print("1. The payer's phone is enrolled; an agent connected by OAuth 2.1 + PKCE and a scheduler with its own key were approved on it")

    w.merchant.add_cart("cart-1", LINES)
    print(f"2. A stranger claims to be an agent:        {w.flow.propose('whl_1', w.request(), bearer_proof('made-up-token', w.clock))}")
    p = w.propose(w.request(), nonce="n-1")
    print(f"3. The real agent proposes:                 {p.status} (caller proven as {p.request.caller.assurance})")
    print(f"4. Someone replays that exact request:      {w.propose(w.request(intent='i-2'), nonce='n-1')}")
    w.merchant.add_cart("cart-2", LINES)
    q = w.propose(w.request("cart-2", "i-3"))
    a = w.respond(p, "authorise")
    w.flow.approve(p.proposal_id, a)
    try:
        w.flow.approve(q.proposal_id, a)
    except Exception as e:
        print(f"5. Yesterday's approval reused on another proposal: refused ({str(e).split(':')[1].strip()})")

    yk = SoftwareAuthenticator("key_backup", rp_id=i.rp_id, origin=i.origin)
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=Identity.key_subject(yk.key_id, yk.public_jwk, "recovery_key"))
    i.enrol_key("pyr_1", yk.key_id, yk.public_jwk, w.device.respond(ch), role="recovery_key")
    try:
        w.flow.approve(q.proposal_id, webauthn_response(yk, w.flow.challenge(q.proposal_id, "authorise")))
    except Exception:
        print("6. The backup key tries to approve a payment: refused (recovery only)")

    i.set_recovery_questions("pyr_1", ["first pet's real name", "street we grew up on", "a favourite teacher"],
                             w.device.respond(i.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"recovery_questions": "pyr_1"}))))
    new = Device("key_new_phone")
    ch = i.challenge(payer_ref="pyr_1", action="recover", subject_hash=Identity.recovery_subject(new.key_id, new.public_jwk))
    r = i.start_recovery("pyr_1", new_key_id=new.key_id, new_jwk=new.public_jwk, new_key_user_verification="inherence",
                         answers=["First pet's real name", "street we  grew up on", "A FAVOURITE TEACHER"], recovery_key_assertion=webauthn_response(yk, ch))
    st = i.keylog.state()
    print(f"7. Phone lost: backup key + answers + biometric on the new phone -> recovery {r.status}; old phone {st[w.device.key_id].status}, new phone {st[new.key_id].status}")
finally:
    pg.stop()
