"""make demo: the whole story on a throwaway Postgres, with readable evidence pages written to out/."""

from pathlib import Path

import psycopg

from gap_api.story import run_story
from gap_ledger.ledger import Ledger
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

ROOT = Path(__file__).resolve().parents[1]
pg = TempCluster()
try:
    pg.create_template("tl", LEDGER_SQL)
    pg.create_template("tp", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lc = psycopg.connect(pg.fresh_db("tl", "demo_l"), autocommit=True)
    lc.execute("SET ROLE ledger_writer")
    pc = psycopg.connect(pg.fresh_db("tp", "demo_p"), autocommit=True)
    pc.execute("SET ROLE profile_writer")
    out = run_story(Ledger(lc), pc)
    print(out["steps"])
    print(out["payer_text"])
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out/evidence_payer.html").write_text(out["payer_html"])
    (ROOT / "out/evidence_merchant.html").write_text(out["merchant_html"])
    print("Readable pages: out/evidence_payer.html, out/evidence_merchant.html (renderings; the bundles are the evidence)")
finally:
    pg.stop()
