"""Phase 7 acceptance tests: 07 §9."""

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gap_core.canonical import canonical
from gap_core.clock import FixedClock
from gap_core.hashing import hash_value
from gap_identity.devices import Device, KeyAgent, bearer_proof, webauthn_response
from gap_identity.identity import Identity, IdentityError
from gap_identity.webauthn import SoftwareAuthenticator
from gap_keys.local import LocalSigner
from gap_ledger.keylog import KeyLog
from gap_mapping.load import load_mapping, load_tier0

ROOT = Path(__file__).resolve().parents[3]
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
SUBJECT = "sha256:" + "a" * 64
ANSWERS = ["The blue bicycle", "Grandma's mango pickle", "Platform nine"]


@pytest.fixture
def idn(ledger, profile_conn):
    india = load_mapping(ROOT / "mappings/in/v0.1.0.yaml")
    params = dict(load_tier0(ROOT / "tier0/examples/personal_in.yaml", india).raw)
    params["identity.surface.min_assurance"] = "software"  # the reference device has no platform attestation
    clock = FixedClock(T0)
    keylog = KeyLog(ledger, "host", LocalSigner.generate("key_host"), clock)
    keylog.bootstrap()
    i = Identity(ledger, keylog, clock, scale=india.auth_levels, params=params, host_ref="host", secrets_conn=profile_conn)
    phone = Device("key_phone")
    i.enrol_first_surface("pyr_1", phone.key_id, phone.public_jwk)
    return i, phone, clock


def approve(i, device, action="authorise", subject=SUBJECT, payer="pyr_1", **kw):
    ch = i.challenge(payer_ref=payer, action=action, subject_hash=subject)
    return i.verify_assertion(device.respond(ch), payer_ref=payer, action=action, subject_hash=subject, **kw)


# --- challenges and assertions ------------------------------------------------------------------

def test_a_challenge_bound_assertion_verifies_with_derived_strength(idn):
    i, phone, _ = idn
    info = approve(i, phone, required="auth.strength.override")
    assert info["strength"] == "two_factor"  # possession + inherence on the India scale


def test_a_challenge_can_be_used_once(idn):
    i, phone, _ = idn
    ch = i.challenge(payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    a = phone.respond(ch)
    i.verify_assertion(a, payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    with pytest.raises(IdentityError, match="challenge_reused"):
        i.verify_assertion(a, payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)


def test_expired_missing_or_mismatched_challenges_never_authorise(idn):
    i, phone, clock = idn
    ch = i.challenge(payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    with pytest.raises(IdentityError, match="challenge_mismatch"):
        i.verify_assertion(phone.respond(ch), payer_ref="pyr_1", action="authorise", subject_hash="sha256:" + "b" * 64)
    with pytest.raises(IdentityError, match="no_challenge"):
        i.verify_assertion({**phone.respond(ch), "challenge_id": "chl_made_up"}, payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    ch2 = i.challenge(payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    clock.advance(timedelta(minutes=6))
    with pytest.raises(IdentityError, match="challenge_expired"):
        i.verify_assertion(phone.respond(ch2), payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)


def test_strength_comes_from_the_scale_not_the_device(idn):
    i, phone, _ = idn
    weak = Device("key_weak", user_verification="none")
    i.enrol_key("pyr_1", weak.key_id, weak.public_jwk, approve_enrol(i, phone, weak.key_id, weak.public_jwk), user_verification="none")
    with pytest.raises(IdentityError, match="strength_too_low"):
        approve(i, weak, required="auth.strength.override")


def approve_enrol(i, device, key_id, jwk, role="surface"):
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=Identity.key_subject(key_id, jwk, role))
    return device.respond(ch)


def test_a_stranger_or_another_payer_cannot_assert(idn):
    i, phone, _ = idn
    stranger = Device("key_stranger")
    with pytest.raises(IdentityError, match="key_not_allowed"):
        approve(i, stranger)
    other = Device("key_other")
    i.enrol_first_surface("pyr_2", other.key_id, other.public_jwk)
    ch = i.challenge(payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    with pytest.raises(IdentityError, match="key_not_allowed"):
        i.verify_assertion(other.respond(ch), payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)


def test_a_revoked_surface_key_is_refused(idn):
    i, phone, _ = idn
    i.keylog.revoke(phone.key_id)
    with pytest.raises(IdentityError, match="key_not_active"):
        approve(i, phone)


def test_first_surface_only_once(idn):
    i, _, _ = idn
    with pytest.raises(IdentityError, match="already_enrolled"):
        i.enrol_first_surface("pyr_1", "key_x", Device("key_x").public_jwk)


# --- recovery keys --------------------------------------------------------------------------------

def test_a_recovery_key_can_never_authorise_a_payment(idn):
    i, phone, _ = idn
    yk = SoftwareAuthenticator("key_yubikey", rp_id=i.rp_id, origin=i.origin)
    i.enrol_key("pyr_1", yk.key_id, yk.public_jwk, approve_enrol(i, phone, yk.key_id, yk.public_jwk, "recovery_key"), role="recovery_key")
    ch = i.challenge(payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)
    with pytest.raises(IdentityError, match="key_not_allowed"):
        i.verify_assertion(webauthn_response(yk, ch), payer_ref="pyr_1", action="authorise", subject_hash=SUBJECT)


def test_webauthn_rejects_a_wrong_origin_or_tampered_signature(idn):
    i, phone, _ = idn
    phish = SoftwareAuthenticator("key_yk2", rp_id=i.rp_id, origin="https://evil.example")
    i.enrol_key("pyr_1", phish.key_id, phish.public_jwk, approve_enrol(i, phone, phish.key_id, phish.public_jwk, "recovery_key"), role="recovery_key")
    ch = i.challenge(payer_ref="pyr_1", action="recover", subject_hash=SUBJECT)
    with pytest.raises(IdentityError, match="bad_signature"):
        i.verify_assertion(webauthn_response(phish, ch), payer_ref="pyr_1", action="recover", subject_hash=SUBJECT, roles=("recovery_key",))


# --- callers ---------------------------------------------------------------------------------------

def test_key_bound_caller_enrols_only_with_the_payers_approval_and_signs_proposals(idn):
    i, phone, clock = idn
    agent = KeyAgent("key_agent", clock)
    e = i.request_caller("pyr_1", public_jwk=agent.signer.public_jwk)
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    agent.caller_id = i.approve_caller(e["enrolment_id"], phone.respond(ch))["caller_id"]
    c = i.authenticate_proposal(agent.proof("intent-1", "sha256:" + "d" * 64), payer_ref="pyr_1", intent_id="intent-1", digest="sha256:" + "d" * 64)
    assert c.assurance == "key_bound" and c.caller_id == agent.caller_id


def _session_caller(i, phone, clock):
    client = i.register_client("Totally Official Assistant", "https://client.example/cb")
    e = i.request_caller("pyr_1", client_id=client)
    verifier = "v" * 50
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    out = i.approve_caller(e["enrolment_id"], phone.respond(ch), code_challenge=challenge)
    return client, verifier, out


def test_session_caller_gets_tokens_only_with_pkce(idn):
    i, phone, clock = idn
    client, verifier, out = _session_caller(i, phone, clock)
    with pytest.raises(IdentityError, match="invalid_grant"):
        i.token(out["code"], "wrong-verifier", client)
    client2, verifier2, out2 = _session_caller(i, phone, clock)
    tok = i.token(out2["code"], verifier2, client2)
    with pytest.raises(IdentityError, match="invalid_grant"):
        i.token(out2["code"], verifier2, client2)  # codes are single use
    c = i.authenticate_proposal(bearer_proof(tok["access_token"], clock), payer_ref="pyr_1", intent_id="x", digest="d")
    assert c.assurance == "session_bound"


def test_refresh_tokens_rotate(idn):
    i, phone, clock = idn
    client, verifier, out = _session_caller(i, phone, clock)
    tok = i.token(out["code"], verifier, client)
    new = i.refresh_token(tok["refresh_token"], client)
    with pytest.raises(IdentityError):
        i.refresh_token(tok["refresh_token"], client)
    assert new["access_token"] != tok["access_token"]


def test_unenrolled_expired_suspended_or_pending_callers_are_refused(idn):
    i, phone, clock = idn
    stranger = KeyAgent("key_stranger_agent", clock)
    stranger.caller_id = "clr_made_up"
    with pytest.raises(IdentityError, match="sys.caller_not_bound"):
        i.authenticate_proposal(stranger.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d")
    agent = KeyAgent("key_agent", clock)
    e = i.request_caller("pyr_1", public_jwk=agent.signer.public_jwk)  # pending: not yet approved
    agent.caller_id = "clr_pending"
    with pytest.raises(IdentityError, match="sys.caller_not_bound"):
        i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d")
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    agent.caller_id = i.approve_caller(e["enrolment_id"], phone.respond(ch))["caller_id"]
    i.suspend_caller(agent.caller_id)
    with pytest.raises(IdentityError, match="sys.caller_not_bound"):
        i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d")
    i.callers[agent.caller_id].status = "active"
    clock.advance(timedelta(seconds=i.params["identity.caller.max_validity"] + 1))
    with pytest.raises(IdentityError, match="sys.caller_not_bound"):
        i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d")


def test_replayed_or_stale_or_mismatched_proofs_are_refused(idn):
    i, phone, clock = idn
    agent = KeyAgent("key_agent", clock)
    e = i.request_caller("pyr_1", public_jwk=agent.signer.public_jwk)
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    agent.caller_id = i.approve_caller(e["enrolment_id"], phone.respond(ch))["caller_id"]
    p = agent.proof("i", "d", nonce="n-1")
    i.authenticate_proposal(p, payer_ref="pyr_1", intent_id="i", digest="d")
    with pytest.raises(IdentityError, match="sys.nonce_reused"):
        i.authenticate_proposal(p, payer_ref="pyr_1", intent_id="i", digest="d")
    old = agent.proof("i", "d", created_at=clock.now() - timedelta(seconds=301))
    with pytest.raises(IdentityError, match="sys.proof_stale"):
        i.authenticate_proposal(old, payer_ref="pyr_1", intent_id="i", digest="d")
    with pytest.raises(IdentityError, match="sys.proof_invalid"):
        i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="other-cart")


def test_rotating_a_caller_key_keeps_the_caller_id(idn):
    i, phone, clock = idn
    agent = KeyAgent("key_agent", clock)
    e = i.request_caller("pyr_1", public_jwk=agent.signer.public_jwk)
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    agent.caller_id = i.approve_caller(e["enrolment_id"], phone.respond(ch))["caller_id"]
    new = LocalSigner.generate("key_agent_v2")
    i.rotate_caller_key(agent.caller_id, new.key_id, new.public_jwk, agent.signer.sign(canonical({"rotate_to": new.key_id, "public_key": new.public_jwk})))
    agent.signer = new
    assert i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d").caller_id == agent.caller_id


# --- recovery --------------------------------------------------------------------------------------

def _questions(i, phone):
    ch = i.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"recovery_questions": "pyr_1"}))
    i.set_recovery_questions("pyr_1", ANSWERS, phone.respond(ch))


def test_recovery_with_the_backup_key_is_immediate_and_replaces_every_old_device(idn):
    i, phone, _ = idn
    yk = SoftwareAuthenticator("key_yk", rp_id=i.rp_id, origin=i.origin)
    i.enrol_key("pyr_1", yk.key_id, yk.public_jwk, approve_enrol(i, phone, yk.key_id, yk.public_jwk, "recovery_key"), role="recovery_key")
    _questions(i, phone)
    new_phone = Device("key_new_phone")
    ch = i.challenge(payer_ref="pyr_1", action="recover", subject_hash=Identity.recovery_subject(new_phone.key_id, new_phone.public_jwk))
    r = i.start_recovery("pyr_1", new_key_id=new_phone.key_id, new_jwk=new_phone.public_jwk, new_key_user_verification="inherence",
                         answers=[" the BLUE  bicycle", "grandma's mango pickle", "PLATFORM NINE"], recovery_key_assertion=webauthn_response(yk, ch))
    assert r.status == "completed"
    st = i.keylog.state()
    assert st[phone.key_id].status == "revoked" and st[new_phone.key_id].status == "active"
    assert st[yk.key_id].principal_role == "recovery_key"  # recovery never widens: only surface keys changed
    approve(i, new_phone)


def test_delayed_recovery_suspends_payments_and_any_old_device_can_cancel(idn):
    i, phone, clock = idn
    _questions(i, phone)
    new_phone = Device("key_new_phone")
    r = i.start_recovery("pyr_1", new_key_id=new_phone.key_id, new_jwk=new_phone.public_jwk, new_key_user_verification="inherence",
                         answers=ANSWERS, external_verified=True)
    assert r.status == "pending" and i.recovery_pending("pyr_1")
    ch = i.challenge(payer_ref="pyr_1", action="recover", subject_hash=hash_value({"cancel": r.recovery_id}))
    i.cancel_recovery(r.recovery_id, phone.respond(ch))
    clock.advance(timedelta(days=2))
    i.tick()
    assert r.status == "cancelled" and i.keylog.state()[phone.key_id].status == "active"


def test_delayed_recovery_completes_after_the_delay(idn):
    i, phone, clock = idn
    _questions(i, phone)
    new_phone = Device("key_new_phone")
    r = i.start_recovery("pyr_1", new_key_id=new_phone.key_id, new_jwk=new_phone.public_jwk, new_key_user_verification="inherence",
                         answers=ANSWERS, external_verified=True)
    clock.advance(timedelta(seconds=i.params["identity.recovery_delay"] - 1))
    i.tick()
    assert r.status == "pending"
    clock.advance(timedelta(seconds=2))
    i.tick()
    assert r.status == "completed" and not i.recovery_pending("pyr_1")


def test_wrong_answers_lock_recovery_after_five_tries(idn):
    i, phone, clock = idn
    _questions(i, phone)
    for _ in range(5):
        assert not i.check_answers("pyr_1", ["no", "nope", "nah"])
    with pytest.raises(IdentityError, match="locked"):
        i.check_answers("pyr_1", ANSWERS)
    assert ("pyr_1", "recovery locked after five wrong answers") in i.notices
    clock.advance(timedelta(hours=25))
    assert i.check_answers("pyr_1", ANSWERS)


def test_recovery_needs_more_than_the_answers(idn):
    i, phone, _ = idn
    _questions(i, phone)
    new_phone = Device("key_new")
    with pytest.raises(IdentityError, match="insufficient_factors"):
        i.start_recovery("pyr_1", new_key_id=new_phone.key_id, new_jwk=new_phone.public_jwk, new_key_user_verification="inherence", answers=ANSWERS)
    with pytest.raises(IdentityError, match="no_user_verification"):
        i.start_recovery("pyr_1", new_key_id=new_phone.key_id, new_jwk=new_phone.public_jwk, new_key_user_verification="none",
                         answers=ANSWERS, external_verified=True)


def test_answers_are_stored_only_as_argon2_hashes(idn, profile_conn):
    i, phone, _ = idn
    _questions(i, phone)
    stored = " ".join(r[0] for r in profile_conn.execute("SELECT argon2_hash FROM recovery_secrets").fetchall())
    assert "bicycle" not in stored.lower() and stored.count("$argon2id$") == 3


# --- compromise -----------------------------------------------------------------------------------

def test_a_compromised_surface_suspends_and_only_another_surface_resumes(idn):
    i, phone, clock = idn
    tablet = Device("key_tablet")
    i.enrol_key("pyr_1", tablet.key_id, tablet.public_jwk, approve_enrol(i, phone, tablet.key_id, tablet.public_jwk), user_verification="inherence")
    assert i.report_compromise(phone.key_id, clock.now()) == "wheel_suspended"
    assert i.wheel_suspended("pyr_1")
    with pytest.raises(IdentityError, match="key_not_active"):
        approve(i, phone, action="wheel_event", subject=hash_value({"resume": "pyr_1"}))
    approve_resume = i.challenge(payer_ref="pyr_1", action="wheel_event", subject_hash=hash_value({"resume": "pyr_1"}))
    i.resume("pyr_1", tablet.respond(approve_resume))
    assert not i.wheel_suspended("pyr_1")


def test_a_compromised_caller_key_suspends_that_caller(idn):
    i, phone, clock = idn
    agent = KeyAgent("key_agent", clock)
    e = i.request_caller("pyr_1", public_jwk=agent.signer.public_jwk)
    ch = i.challenge(payer_ref="pyr_1", action="enrol", subject_hash=e["subject_hash"])
    agent.caller_id = i.approve_caller(e["enrolment_id"], phone.respond(ch))["caller_id"]
    assert i.report_compromise(i.callers[agent.caller_id].key_id, clock.now()) == "caller_suspended"
    with pytest.raises(IdentityError, match="sys.caller_not_bound"):
        i.authenticate_proposal(agent.proof("i", "d"), payer_ref="pyr_1", intent_id="i", digest="d")


def test_every_identity_event_is_a_signed_record(idn):
    i, phone, _ = idn
    approve(i, phone)
    kinds = {r["record_type"] for r in i.ledger.records(i.chain_id)}
    assert {"challenge", "surface_assertion"} <= kinds
    assert i.ledger.verify_integrity(i.keylog.public_keys()).ok
