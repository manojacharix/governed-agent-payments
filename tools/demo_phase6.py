"""Phase 6 demo (make evidence): one payment from request to proof, checked by the open verifier."""

import copy
import random
from pathlib import Path

import psycopg

from gap_evidence.bundle import EvidenceBuilder, evaluator_digest
from gap_evidence.verify import verify_bundle
from gap_flow.testing import LINES, make_world
from gap_ledger.ledger import Ledger
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

ROOT = Path(__file__).resolve().parents[1]
pg = TempCluster()
try:
    pg.create_template("tl", LEDGER_SQL)
    pg.create_template("tp", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lc = psycopg.connect(pg.fresh_db("tl", "d6l"), autocommit=True)
    lc.execute("SET ROLE ledger_writer")
    pc = psycopg.connect(pg.fresh_db("tp", "d6p"), autocommit=True)
    pc.execute("SET ROLE profile_writer")
    w = make_world(Ledger(lc), pc, evaluator_digest=evaluator_digest())
    w.merchant.add_cart("cart-1", LINES)
    w.merchant.script = ["overcharge:700"]
    p = w.flow.propose("whl_1", w.request())
    print(f"1. Agent proposes 195 rupees of groceries -> {p.determination.outcome}, approval: {p.determination.approval_mode.replace("_", " ")}")
    out = w.approve(p)
    print(f"2. Payer approves on the reference surface -> executed, merchant order {out.result.merchant_order_ref}")
    w.pay_bank(out.result)
    w.flow.tick(sweep=True)
    for f in w.flow.recon.open_findings("whl_1"):
        print(f"3. Reconciliation: bank shows 7 rupees more than approved -> finding '{f['kind']}' ({f['status']})")
    b = EvidenceBuilder(w.flow.ledger, w.flow.keylog, w.flow.profile, w.clock, random.Random(1))
    payer = b.build("whl_1", p.proposal_id, role="payer", requester_ref="pyr_1")
    r = verify_bundle(payer, w.host.public_jwk, evaluator=evaluator_digest())
    print(f"4. Payer's evidence bundle: {len(payer['records'])} records; verifier ok={r['ok']}, replay={r['replay']}, "
          f"what was approved={r['presented_hash']}; divergence: {payer['divergence']['primary']}")
    merchant = b.build("whl_1", p.proposal_id, role="merchant", requester_ref="mrc_shop")
    r = verify_bundle(merchant, w.host.public_jwk, evaluator=evaluator_digest())
    print(f"5. Merchant's bundle: only {[x['record_type'] for x in merchant['records']]}, no inputs; verifier ok={r['ok']}")
    forged = copy.deepcopy(payer)
    next(x for x in forged["records"] if x["record_type"] == "posting")["amount"]["amount_minor"] = 19500
    r = verify_bundle(forged, w.host.public_jwk, evaluator=evaluator_digest())
    print(f"6. Someone edits the bundle to hide the overcharge: verifier ok={r['ok']}: {r['problems'][0]}")
finally:
    pg.stop()
