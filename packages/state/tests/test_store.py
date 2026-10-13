"""PgStore: units of work, version checks, per-payer locks across processes, checkpoints, purge."""

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from gap_core.codec import decode, encode
from gap_state.store import Kind, MemoryStore, PgStore, StateConflict

KIND = Kind("thing", encode, decode, scope=lambda d: d["payer"], tag=lambda d: d.get("tag", ""), status=lambda d: d.get("status", ""),
            expires=lambda d: d.get("expires"))


def connect(dsn):
    c = psycopg.connect(dsn, autocommit=True)
    c.execute("SET ROLE state_writer")
    return c


@pytest.fixture
def two(state_dsn):
    """Two stores on one database: two host processes."""
    a, b = PgStore(connect(state_dsn)), PgStore(connect(state_dsn))
    yield a, b
    a.conn.close()
    b.conn.close()


def test_objects_round_trip_and_in_place_changes_are_saved(two):
    a, b = two
    ta, tb = a.table(KIND), b.table(KIND)
    with a.unit("pyr_1"):
        ta["x"] = {"payer": "pyr_1", "n": 1}
    with a.unit("pyr_1"):
        ta["x"]["n"] = 2  # mutate in place: the unit writes it back
    assert tb["x"]["n"] == 2 and "x" in tb and "y" not in tb


def test_a_failed_unit_saves_nothing(two):
    a, b = two
    ta = a.table(KIND)
    with pytest.raises(RuntimeError):
        with a.unit("pyr_1"):
            ta["x"] = {"payer": "pyr_1", "n": 1}
            raise RuntimeError("boom")
    assert b.table(KIND).get("x") is None


def test_a_lost_update_is_a_conflict_not_a_silent_overwrite(two):
    a, b = two
    ta, tb = a.table(KIND), b.table(KIND)
    ta["x"] = {"payer": "pyr_1", "n": 1}
    with pytest.raises(StateConflict):
        with a.unit():          # no lock: both read version 1
            ta["x"]["n"] = 2
            with b.unit():
                tb["x"]["n"] = 3
            # b committed version 2; a's write names version 1
    assert tb["x"]["n"] == 3


def test_two_processes_creating_the_same_object_conflict(two):
    a, b = two
    with pytest.raises(StateConflict):
        with a.unit():
            a.table(KIND)["x"] = {"payer": "pyr_1"}
            with b.unit():
                b.table(KIND)["x"] = {"payer": "pyr_1"}


def test_the_payer_lock_makes_a_second_process_wait_even_across_a_checkpoint(two):
    a, b = two
    order = []

    def second():
        with b.unit("pyr_1"):
            order.append("b got the lock")

    with a.unit("pyr_1"):
        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.3)
        a.checkpoint()  # commits, but the lock must still be held
        time.sleep(0.3)
        order.append("a done")
    t.join()
    assert order == ["a done", "b got the lock"]


def test_different_payers_do_not_wait_for_each_other(two):
    a, b = two
    done = []
    with a.unit("pyr_1"):
        with b.unit("pyr_2"):  # would hang if payers shared a lock
            done.append("b in")
    assert done == ["b in"]


def test_checkpoint_commits_what_came_before(two):
    a, b = two
    ta = a.table(KIND)
    with pytest.raises(RuntimeError):
        with a.unit("pyr_1"):
            ta["before"] = {"payer": "pyr_1"}
            a.checkpoint()
            ta["after"] = {"payer": "pyr_1"}
            raise RuntimeError("process died")
    tb = b.table(KIND)
    assert "before" in tb and "after" not in tb


def test_queries_see_this_units_own_changes(two):
    a, _ = two
    ta = a.table(KIND)
    ta["x"] = {"payer": "pyr_1", "tag": "w1", "status": "open"}
    with a.unit():
        ta["y"] = {"payer": "pyr_1", "tag": "w1", "status": "open"}
        ta["x"]["status"] = "closed"
        assert [d["status"] for d in ta.where(tag="w1")] == ["closed", "open"]
        assert len(ta.where(status=("open",))) == 1
    assert ta.scope_of("x") == "pyr_1" and ta.scope_of("nope") is None


def test_deletes_and_purge(two):
    a, b = two
    ta = a.table(KIND)
    past, future = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2030, 1, 1, tzinfo=timezone.utc)
    ta["old"] = {"payer": "p", "expires": past}
    ta["new"] = {"payer": "p", "expires": future}
    ta["gone"] = {"payer": "p"}
    assert ta.pop("gone")["payer"] == "p" and "gone" not in b.table(KIND)
    assert a.purge(datetime(2026, 6, 1, tzinfo=timezone.utc)) == 1
    assert sorted(b.table(KIND).keys()) == ["new"]


def test_the_state_role_cannot_touch_anything_else(state_dsn):
    c = connect(state_dsn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("CREATE TABLE sneaky (x int)")
    c.close()


def test_memory_store_offers_the_same_queries():
    t = MemoryStore().table(KIND)
    t["x"] = {"payer": "p", "tag": "w", "status": "open"}
    assert t.where(tag="w", status=("open",)) == [t["x"]] and t.scope_of("x") == "p"
