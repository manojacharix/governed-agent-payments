"""Shared fixtures: one throwaway Postgres per test session, a fresh database per test (from templates)."""

import itertools
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).parent
_counter = itertools.count()


@pytest.fixture(scope="session")
def pg():
    from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

    cluster = TempCluster()
    cluster.create_template("tmpl_ledger", LEDGER_SQL)
    cluster.create_template("tmpl_profile", sorted((ROOT / "packages/profile/src/gap_profile/sql").glob("*.sql")))
    yield cluster
    cluster.stop()


@pytest.fixture
def ledger_dsn(pg):
    return pg.fresh_db("tmpl_ledger", f"ledger_{next(_counter)}")


@pytest.fixture
def ledger(ledger_dsn):
    """A Ledger connected as the least-privileged writer role."""
    from gap_ledger.ledger import Ledger

    conn = psycopg.connect(ledger_dsn, autocommit=True)
    conn.execute("SET ROLE ledger_writer")
    yield Ledger(conn)
    conn.close()


@pytest.fixture
def admin(ledger_dsn):
    """A superuser connection to the same database: plays the attacker with database access."""
    conn = psycopg.connect(ledger_dsn, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def profile_conn(pg):
    dsn = pg.fresh_db("tmpl_profile", f"profile_{next(_counter)}")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET ROLE profile_writer")
    yield conn
    conn.close()
