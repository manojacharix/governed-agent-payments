"""make stress: the full flow under extreme profiles x seeds, in parallel, each on its own throwaway Postgres.

Usage: uv run python tools/stress.py [--durable] [seeds_per_profile] [events] [profile ...]
--durable keeps working state (proposals, challenges, tokens) in Postgres instead of memory.
Exit 1 if any run crashes or breaks an invariant.
"""

import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def one(profile: str, seed: int, events: int, durable: bool = False) -> dict:
    import psycopg
    from gap_ledger.pgtemp import LEDGER_SQL, TempCluster
    from gap_sim.flow_sim import PROFILES, run
    pg = TempCluster()
    t0 = time.perf_counter()
    try:
        pg.create_template("tl", LEDGER_SQL)
        pg.create_template("tp", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
        lc = psycopg.connect(pg.fresh_db("tl", "s"), autocommit=True)
        lc.execute("SET ROLE ledger_writer")
        pc = psycopg.connect(pg.fresh_db("tp", "sp"), autocommit=True)
        pc.execute("SET ROLE profile_writer")
        store = None
        if durable:
            from gap_state.store import PgStore
            pg.create_template("ts", sorted((ROOT / "packages/state/src/gap_state/sql").glob("*.sql")))
            sc = psycopg.connect(pg.fresh_db("ts", "ss"), autocommit=True)
            sc.execute("SET ROLE state_writer")
            store = PgStore(sc)
        rep = run(lc, pc, seed=seed, payers=30, events=events, minutes_per_event=max(1, 45 * 1440 // events), bundles=20,
                  profile=PROFILES[profile], store=store)
        return {"profile": profile + (" (pg)" if durable else ""), "seed": seed, "secs": time.perf_counter() - t0, "proposals": rep.proposals, "records": rep.records,
                "statuses": dict(rep.statuses), "findings": dict(rep.findings), "bundles": dict(rep.bundles),
                "violations": rep.violations, "crash": None}
    except Exception as e:
        frames = [f for f in traceback.extract_tb(e.__traceback__) if "/packages/" in f.filename]
        where = [f"{f.filename.split('/packages/')[1]}:{f.lineno} {f.name}" for f in frames[-4:]]
        return {"profile": profile + (" (pg)" if durable else ""), "seed": seed, "secs": time.perf_counter() - t0, "crash": f"{type(e).__name__}: {e}"[:300], "where": where,
                "violations": []}
    finally:
        pg.stop()


if __name__ == "__main__":
    from gap_sim.flow_sim import PROFILES
    args = sys.argv[1:]
    durable = "--durable" in args
    args = [a for a in args if a != "--durable"]
    seeds = int(args[0]) if len(args) > 0 else 2
    events = int(args[1]) if len(args) > 1 else 1500
    profiles = args[2:] or list(PROFILES)
    jobs = [(p, 1000 + 17 * i, events, durable) for p in profiles for i in range(seeds)]
    bad = 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for fut in as_completed([ex.submit(one, *j) for j in jobs]):
            r = fut.result()
            if r["crash"]:
                bad += 1
                print(f"CRASH {r['profile']:<20} seed {r['seed']}: {r['crash']}\n      at {r['where']}")
                continue
            v = r["violations"]
            bad += bool(v)
            exe = r["statuses"].get("executed", 0)
            print(f"{'FAIL ' if v else 'ok   '}{r['profile']:<20} seed {r['seed']}: {r['proposals']:>5} proposals, {exe:>5} executed, "
                  f"{r['records']:>6} records, bundles {sum(n for k, n in r['bundles'].items() if 'verified' in k)} verified, "
                  f"{len(v)} violations, {r['secs']:.0f}s")
            for x in v[:8]:
                print(f"      {x}")
    print(f"\n{len(jobs)} runs, {bad} with crashes or violations")
    sys.exit(1 if bad else 0)
