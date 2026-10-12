"""make simulate: a larger seeded synthetic run on a throwaway Postgres, with a summary.

Usage: uv run python tools/simulate.py [seed] [payers] [events]
"""

import sys
import time

import psycopg

from gap_ledger.pgtemp import LEDGER_SQL, TempCluster
from gap_sim.simulate import run

seed, payers, events = (int(a) for a in (sys.argv[1:] + ["42", "60", "5000"][len(sys.argv) - 1:]))
pg = TempCluster()
try:
    pg.create_template("tmpl_ledger", LEDGER_SQL)
    conn = psycopg.connect(pg.fresh_db("tmpl_ledger", "sim"), autocommit=True)
    conn.execute("SET ROLE ledger_writer")
    t0 = time.perf_counter()
    rep = run(conn, seed=seed, payers=payers, events=events, minutes_per_event=max(1, 75 * 1440 // events))
    dt = time.perf_counter() - t0
    print(f"seed {seed}: {payers} payers, {rep.events} proposals over about 75 days, in {dt:.1f}s ({rep.events / dt:.0f}/s)")
    print(f"ledger: {rep.records} signed records in {rep.chains} chains, integrity verified")
    print("decisions:   " + ", ".join(f"{k} {v}" for k, v in sorted(rep.outcomes.items())))
    print("settlements: " + ", ".join(f"{k} {v}" for k, v in sorted(rep.settlements.items())))
    print("adversarial inputs and what happened to them:")
    for k, v in sorted(rep.adversarial.items()):
        print(f"  {k:<36} {v}")
    print("most common reasons a proposal was not allowed:")
    for k, v in rep.clauses.most_common(10):
        print(f"  {k:<36} {v}")
    print(f"violations: {len(rep.violations)}")
    for v in rep.violations[:20]:
        print("  " + v)
    sys.exit(0 if rep.ok else 1)
finally:
    pg.stop()
