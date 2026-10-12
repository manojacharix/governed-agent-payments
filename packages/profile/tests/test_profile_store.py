"""Phase 4: the profile store. Personal data is encrypted; shredding is erasure; the ledger still verifies."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from gap_core.clock import FixedClock
from gap_core.commitment import commit, opens
from gap_keys.local import LocalSigner
from gap_profile.store import LocalKek, ProfileStore, RetentionHeld, Shredded

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ADDRESS = {"line1": "12 Example Road", "city": "Bengaluru", "pin": "560001"}


@pytest.fixture
def store(profile_conn):
    return ProfileStore(profile_conn, LocalKek.generate(), FixedClock(T0))


def test_round_trip_and_commitment(store):
    ref, c = store.put("pyr_1", "destination", ADDRESS)
    assert ref.startswith("prof://pyr_1/destination/prf_")
    assert store.get(ref) == ADDRESS
    value, salt = store.opening(ref)
    assert opens(c, value, salt)
    assert not opens(c, {**ADDRESS, "pin": "560002"}, salt)


def test_ciphertext_does_not_contain_the_plaintext(store, profile_conn):
    store.put("pyr_1", "destination", ADDRESS)
    raw = b"".join(bytes(r[0]) for r in profile_conn.execute("SELECT ciphertext FROM profile_records").fetchall())
    assert b"Example Road" not in raw and b"560001" not in raw


def test_commitments_cannot_be_brute_forced_without_the_salt(store):
    _, c = store.put("pyr_1", "pin", "560001")
    # an attacker who guesses the value but lacks the salt cannot confirm it
    assert c != commit("560001", b"\x00" * 32)


def test_shredding_makes_everything_unreadable_and_the_ledger_still_verifies(store, ledger):
    ref, c = store.put("pyr_1", "destination", ADDRESS)
    signer = LocalSigner.generate("key_host")
    ledger.append("whl_1", {"record_type": "proposal", "destination_commitment": c}, signer, record_id="p1")
    store.shred("pyr_1")
    with pytest.raises(Shredded):
        store.get(ref)
    with pytest.raises(Shredded):
        store.opening(ref)
    assert ledger.verify_integrity({signer.key_id: signer.public_jwk}).ok


def test_retention_hold_delays_shredding_until_its_date(profile_conn):
    clock = FixedClock(T0)
    s = ProfileStore(profile_conn, LocalKek.generate(), clock)
    ref, _ = s.put("pyr_1", "destination", ADDRESS)
    s.place_hold("pyr_1", basis="open dispute fnd_1", until=T0 + timedelta(days=90))
    with pytest.raises(RetentionHeld):
        s.shred("pyr_1")
    assert s.get(ref) == ADDRESS  # still available for evidence
    clock.advance(timedelta(days=91))
    s.shred("pyr_1")
    with pytest.raises(Shredded):
        s.get(ref)


def test_a_new_key_after_shredding_starts_clean(store):
    old_ref, _ = store.put("pyr_1", "note", "old")
    store.shred("pyr_1")
    new_ref, _ = store.put("pyr_1", "note", "new")
    assert store.get(new_ref) == "new"
    with pytest.raises(Shredded):
        store.get(old_ref)


def test_profile_records_are_insert_only(store, profile_conn):
    store.put("pyr_1", "note", "x")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        profile_conn.execute("UPDATE profile_records SET kind = 'y'")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        profile_conn.execute("DELETE FROM profile_records")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        profile_conn.execute("DELETE FROM data_keys")


def test_a_wrong_kek_cannot_read(profile_conn):
    a = ProfileStore(profile_conn, LocalKek.generate("kek-a"), FixedClock(T0))
    ref, _ = a.put("pyr_1", "note", "secret")
    b = ProfileStore(profile_conn, LocalKek.generate("kek-a"), FixedClock(T0))
    with pytest.raises(Exception):
        b.get(ref)
