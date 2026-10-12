"""Phase 4: the key log (07 §2, §9)."""

from datetime import datetime, timedelta, timezone

import pytest

from gap_core.clock import FixedClock
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_ledger.ledger import LedgerError

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def setup(ledger):
    clock = FixedClock(T0)
    host = LocalSigner.generate("key_host")
    log = KeyLog(ledger, "host-a", host, clock)
    log.bootstrap()
    return ledger, log, host, clock


def test_bootstrap_is_self_signed_and_verifies(setup):
    ledger, log, host, _ = setup
    st = log.state()
    assert st["key_host"].principal_role == "host" and st["key_host"].status == "active"
    assert ledger.verify_integrity(log.public_keys()).ok


def test_bootstrap_only_once(setup):
    with pytest.raises(LedgerError):
        setup[1].bootstrap()


def test_enrol_rotate_and_old_signatures_still_verify(setup):
    ledger, log, host, clock = setup
    old = LocalSigner.generate("key_caller_1")
    log.enrol(old.key_id, role="caller", principal_ref="clr_a", public_key=old.public_jwk, protection_class="software", enrolled_by="asr_1")
    ledger.append("whl_a", {"record_type": "test", "at": "2026-10-12T09:01:00.000Z"}, old, record_id="t1")
    new = LocalSigner.generate("key_caller_2")
    log.enrol(new.key_id, role="caller", principal_ref="clr_a", public_key=new.public_jwk, protection_class="software", enrolled_by="asr_2")
    log.retire(old.key_id)
    assert log.state()[old.key_id].status == "retired"
    assert ledger.verify_integrity(log.public_keys()).ok  # retired keys' past signatures stay valid
    assert log.audit() == []


def test_enrolling_an_existing_key_is_refused(setup):
    _, log, _, _ = setup
    k = LocalSigner.generate("key_x")
    log.enrol(k.key_id, role="surface", principal_ref="pyr_1", public_key=k.public_jwk, protection_class="hardware_attested", enrolled_by="enr_1")
    with pytest.raises(LedgerError):
        log.enrol(k.key_id, role="surface", principal_ref="pyr_1", public_key=k.public_jwk, protection_class="hardware_attested", enrolled_by="enr_2")


def test_invalid_public_key_is_refused(setup):
    _, log, _, _ = setup
    with pytest.raises(Exception):
        log.enrol("key_bad", role="caller", principal_ref="clr", public_key={"kty": "EC", "crv": "P-256", "x": "AA", "y": "AA"},
                  protection_class="software", enrolled_by="x")


def test_compromise_flags_later_signatures_but_deletes_nothing(setup):
    ledger, log, host, clock = setup
    k = LocalSigner.generate("key_surface")
    log.enrol(k.key_id, role="surface", principal_ref="pyr_1", public_key=k.public_jwk, protection_class="hardware_attested", enrolled_by="enr_1")
    ledger.append("whl_a", {"record_type": "approval", "at": "2026-10-12T10:00:00.000Z"}, k, record_id="a1")
    ledger.append("whl_a", {"record_type": "approval", "at": "2026-10-12T12:00:00.000Z"}, k, record_id="a2")
    clock.advance(timedelta(hours=5))
    log.compromise(k.key_id, since=datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc))
    assert log.audit() == [("whl_a", 1, "signed_after_compromise")]
    assert len(ledger.records("whl_a")) == 2  # nothing deleted
    assert ledger.verify_integrity(log.public_keys()).ok


def test_revoked_key_cannot_be_reactivated_and_later_use_is_flagged(setup):
    ledger, log, host, clock = setup
    k = LocalSigner.generate("key_c")
    log.enrol(k.key_id, role="caller", principal_ref="clr_c", public_key=k.public_jwk, protection_class="software", enrolled_by="x")
    clock.advance(timedelta(hours=1))
    log.revoke(k.key_id)
    with pytest.raises(LedgerError):
        log.retire(k.key_id)
    ledger.append("whl_b", {"record_type": "proposal", "at": "2026-10-12T12:00:00.000Z"}, k, record_id="p1")
    assert ("whl_b", 0, "signed_after_revocation") in log.audit()


def test_a_signature_by_an_unknown_key_is_flagged(setup):
    ledger, log, _, _ = setup
    stranger = LocalSigner.generate("key_stranger")
    ledger.append("whl_c", {"record_type": "test", "at": "2026-10-12T09:00:00.000Z"}, stranger, record_id="s")
    assert ("whl_c", 0, "unknown_key") in log.audit()
    assert not ledger.verify_integrity(log.public_keys()).ok
