"""Phase 4: the ledger is append-only, chained in the database, and tampering is always detected."""

import threading

import psycopg
import pytest

from gap_core.chain import record_hash
from gap_keys.local import LocalSigner
from gap_ledger.ledger import Ledger

SIGNER = LocalSigner.generate("key_host")
KEYS = {SIGNER.key_id: SIGNER.public_jwk}


def _fill(ledger, chain="whl_a", n=4):
    return [ledger.append(chain, {"record_type": "test", "n": i, "posted_at": "2026-10-12T09:00:00.000Z"}, SIGNER, record_id=f"r{i}") for i in range(n)]


def test_append_read_and_verify(ledger):
    recs = _fill(ledger)
    assert ledger.records("whl_a") == recs
    assert ledger.head("whl_a") == recs[-1]
    report = ledger.verify_integrity(KEYS)
    assert report.ok and report.records_checked == 4


# --- the database refuses edits ---------------------------------------------------------

def test_writer_role_cannot_update_or_delete(ledger):
    _fill(ledger)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ledger.conn.execute("UPDATE ledger_records SET record_type = 'x'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ledger.conn.execute("DELETE FROM ledger_records")


@pytest.mark.parametrize("sql", ["UPDATE ledger_records SET record_type = 'x'", "DELETE FROM ledger_records", "TRUNCATE ledger_records"])
def test_even_the_owner_is_stopped_by_triggers(ledger, admin, sql):
    _fill(ledger)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        admin.execute(sql)


def test_the_database_rejects_a_record_that_does_not_extend_the_chain(ledger):
    recs = _fill(ledger, n=2)
    body = dict(recs[1])
    sig = body.pop("signature")
    # replay record 1 as if it were record 2: wrong seq for the head
    with pytest.raises(psycopg.errors.CheckViolation):
        ledger.conn.execute(
            "INSERT INTO ledger_records (chain_id, seq, record_type, record_id, body, record_hash, previous_hash, signature)"
            " VALUES ('whl_a', 2, 'test', 'x', %s, %s, %s, %s)",
            (psycopg.types.json.Jsonb(body), "sha256:" + "a" * 64, body["chain"]["previous_hash"], psycopg.types.json.Jsonb(sig)),
        )


# --- an attacker with superuser access: every edit is detected ----------------------------------

def _attack(admin, *statements):
    admin.execute("ALTER TABLE ledger_records DISABLE TRIGGER USER")
    for s in statements:
        admin.execute(s)
    admin.execute("ALTER TABLE ledger_records ENABLE TRIGGER USER")


@pytest.mark.parametrize("name, statements", [
    ("row mutation", ["UPDATE ledger_records SET body = jsonb_set(body, '{n}', '99') WHERE seq = 2"]),
    ("timestamp shift", ["UPDATE ledger_records SET body = jsonb_set(body, '{posted_at}', '\"2026-10-12T09:00:01.000Z\"') WHERE seq = 1"]),
    ("deletion", ["DELETE FROM ledger_records WHERE seq = 1"]),
    ("reorder", ["UPDATE ledger_records SET seq = 100 WHERE seq = 1", "UPDATE ledger_records SET seq = 1 WHERE seq = 2",
                 "UPDATE ledger_records SET seq = 2 WHERE seq = 100"]),
    ("signature swap", ["UPDATE ledger_records a SET signature = b.signature FROM ledger_records b WHERE a.seq = 1 AND b.seq = 2"]),
    ("recomputed hash column", ["UPDATE ledger_records SET record_hash = 'sha256:' || repeat('b', 64) WHERE seq = 3"]),
])
def test_tampering_is_detected(ledger, admin, name, statements):
    _fill(ledger)
    _attack(admin, *statements)
    report = ledger.verify_integrity(KEYS)
    assert not report.ok, name


def test_concurrent_appends_to_one_chain_stay_in_order(ledger_dsn):
    errors = []

    def worker(i):
        try:
            with psycopg.connect(ledger_dsn, autocommit=True) as c:
                c.execute("SET ROLE ledger_writer")
                lg = Ledger(c)
                for j in range(5):
                    lg.append("whl_busy", {"record_type": "test", "w": i, "j": j}, SIGNER, record_id=f"{i}-{j}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with psycopg.connect(ledger_dsn, autocommit=True) as c:
        lg = Ledger(c)
        assert len(lg.records("whl_busy")) == 40
        assert lg.verify_integrity(KEYS).ok


def test_continued_chain_verifies_across_a_handover(ledger):
    old = _fill(ledger, chain="whl_old", n=2)
    handover = ledger.append("whl_old", {"record_type": "handover", "to_host": "host-b"}, SIGNER, record_id="h")
    new_host = LocalSigner.generate("key_host_b")
    ledger.append("whl_new", {"record_type": "test", "n": 0}, new_host, record_id="n0",
                  continues={"chain_id": "whl_old", "host_ref": "host-a", "handover_hash": record_hash(handover)})
    assert ledger.verify_integrity({**KEYS, new_host.key_id: new_host.public_jwk}).ok
