"""make simulate: a seeded synthetic run of the full flow on a throwaway Postgres, with a summary.

Usage: uv run python tools/simulate.py [seed] [payers] [events]
"""

import sys
import time
from pathlib import Path

import psycopg

from gap_ledger.pgtemp import LEDGER_SQL, TempCluster
from gap_sim.flow_sim import run

seed, payers, events = (int(a) for a in (sys.argv[1:] + ["42", "40", "3000"][len(sys.argv) - 1:]))
ROOT = Path(__file__).resolve().parents[1]
pg = TempCluster()
try:
    pg.create_template("tmpl_ledger", LEDGER_SQL)
    pg.create_template("tmpl_profile", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    lc = psycopg.connect(pg.fresh_db("tmpl_ledger", "sim"), autocommit=True)
    lc.execute("SET ROLE ledger_writer")
    pc = psycopg.connect(pg.fresh_db("tmpl_profile", "simp"), autocommit=True)
    pc.execute("SET ROLE profile_writer")
    t0 = time.perf_counter()
    rep = run(lc, pc, seed=seed, payers=payers, events=events, minutes_per_event=max(1, 60 * 1440 // events), bundles=40)
    dt = time.perf_counter() - t0
    print(f"seed {seed}: {payers} payers, {rep.proposals} proposals over about 60 days, in {dt:.0f}s; {rep.records} signed records, integrity verified")
    print("proposal outcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.statuses.items())))
    print("payer actions:     " + ", ".join(f"{k} {v}" for k, v in sorted(rep.actions.items())))
    print("findings:          " + ", ".join(f"{k} {v}" for k, v in sorted(rep.findings.items())))
    print("evidence bundles:  " + ", ".join(f"{k} {v}" for k, v in sorted(rep.bundles.items())))
    print(f"violations: {len(rep.violations)}")
    for v in rep.violations[:20]:
        print("  " + v)
    sys.exit(0 if rep.ok else 1)
finally:
    pg.stop()
