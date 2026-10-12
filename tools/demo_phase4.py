"""Phase 4 demo (make ledger): a real Postgres ledger, money in flight, a key log, erasure, and an attack."""

import random
from datetime import datetime, timezone
from pathlib import Path

import psycopg

from gap_core.authority import Window
from gap_core.clock import FixedClock
from gap_core.money import Money
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import Ledger
from gap_ledger.money import Account, HoldRefused, MoneyInFlight, Signal, replay
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster
from gap_profile.store import LocalKek, ProfileStore, Shredded

ROOT = Path(__file__).resolve().parents[1]
MONTH = Window("calendar_month", "Asia/Kolkata")
inr = lambda x: Money(x, "INR")  # noqa: E731

pg = TempCluster()
try:
    pg.create_template("tmpl_ledger", LEDGER_SQL)
    pg.create_template("tmpl_profile", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lconn = psycopg.connect(pg.fresh_db("tmpl_ledger", "demo_ledger"), autocommit=True)
    lconn.execute("SET ROLE ledger_writer")
    clock = FixedClock(datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))
    ledger, host = Ledger(lconn), LocalSigner.generate("key_host_demo")
    keylog = KeyLog(ledger, "host-demo", host, clock)
    keylog.bootstrap()
    print("1. Key log started with the host's self-signed key")

    mif = MoneyInFlight(ledger, host, clock, random.Random(1))
    accts = [Account("spk_grocery", MONTH, inr(400000)), Account("whl_demo", MONTH, inr(1500000))]
    mif.hold("whl_demo", "det_1", inr(19500), accts)
    mif.capture("whl_demo", "det_1", inr(19500), Signal("bank", "stmt-1"))
    mif.hold("whl_demo", "det_2", inr(370000), accts)
    out = mif.capture("whl_demo", "det_2", inr(390000), Signal("bank", "stmt-2"))
    a = replay(ledger, "whl_demo").accounts[("spk_grocery", "INR", "2026-10")]
    print(f"2. Two payments; the second was charged {out['overcaptured'] // 100} rupees more than approved")
    print(f"   grocery spoke: held {a.held // 100}, spent {a.spent // 100} of 4,000 rupees (overspent)")
    try:
        mif.hold("whl_demo", "det_3", inr(100), accts)
    except HoldRefused as e:
        print(f"3. New hold refused: {e}")

    pconn = psycopg.connect(pg.fresh_db("tmpl_profile", "demo_profile"), autocommit=True)
    pconn.execute("SET ROLE profile_writer")
    store = ProfileStore(pconn, LocalKek.generate(), clock)
    ref, commitment = store.put("pyr_demo", "destination", {"line1": "12 Example Road", "pin": "560001"})
    ledger.append("whl_demo", {"record_type": "proposal_note", "destination_commitment": commitment, "at": "2026-10-12T09:00:00.000Z"}, host, record_id="n1")
    store.shred("pyr_demo")
    try:
        store.get(ref)
    except Shredded:
        print("4. Payer erased: their address can no longer be read or opened")
    r = ledger.verify_integrity(keylog.public_keys())
    print(f"   ledger still verifies: {r.ok} ({r.records_checked} records in {r.chains_checked} chains)")

    admin = psycopg.connect(pg.dsn("demo_ledger"), autocommit=True)
    admin.execute("ALTER TABLE ledger_records DISABLE TRIGGER USER")
    admin.execute("UPDATE ledger_records SET body = jsonb_set(body, '{amount,amount_minor}', '1') WHERE record_type = 'posting' AND body->>'kind' = 'overcapture'")
    admin.execute("ALTER TABLE ledger_records ENABLE TRIGGER USER")
    r = ledger.verify_integrity(keylog.public_keys())
    print(f"5. A database admin quietly shrinks the overcharge to 0.01 rupees. Ledger verifies: {r.ok}")
    for chain, problem in r.problems:
        print(f"   {chain}: {problem}")
finally:
    pg.stop()
