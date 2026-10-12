import base64

import pytest

from gap_core.chain import ChainError, append, record_hash, verify_chain
from gap_core.hashing import ZERO_HASH
from gap_core.signatures import SignatureError, verify
from gap_keys.local import LocalSigner


@pytest.fixture
def signer():
    return LocalSigner.generate("key_01j9xk3m2q8v7c4r5t6w8y0z1a")


@pytest.fixture
def keys(signer):
    return {signer.key_id: signer.public_jwk}


# --- signatures -------------------------------------------------------------

def test_sign_and_verify(signer):
    sig = signer.sign(b"hello")
    verify(signer.public_jwk, b"hello", sig)


def test_signature_is_64_byte_p1363(signer):
    raw = base64.urlsafe_b64decode(signer.sign(b"x") + "==")
    assert len(raw) == 64


def test_one_changed_byte_fails(signer):
    sig = signer.sign(b"hello")
    with pytest.raises(SignatureError):
        verify(signer.public_jwk, b"hellp", sig)


def test_wrong_key_fails(signer):
    other = LocalSigner.generate("key_01j9xk3m2q8v7c4r5t6w8y0z1b")
    with pytest.raises(SignatureError):
        verify(other.public_jwk, b"hello", signer.sign(b"hello"))


def test_high_s_signatures_are_refused(signer):
    """ECDSA allows (r, n - s) as a second valid signature. Records hash their signature, so only low-s is accepted."""
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    raw = base64.urlsafe_b64decode(signer.sign(b"m") + "==")
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    assert s <= n // 2
    flipped = base64.urlsafe_b64encode(r.to_bytes(32, "big") + (n - s).to_bytes(32, "big")).rstrip(b"=").decode()
    with pytest.raises(SignatureError):
        verify(signer.public_jwk, b"m", flipped)


def test_malformed_signature_is_refused(signer):
    with pytest.raises(SignatureError):
        verify(signer.public_jwk, b"m", "not-base64!")
    with pytest.raises(SignatureError):
        verify(signer.public_jwk, b"m", base64.urlsafe_b64encode(b"\x00" * 63).decode())


def test_public_jwk_shape(signer):
    jwk = signer.public_jwk
    assert set(jwk) == {"kty", "crv", "x", "y"}
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"


# --- chains -----------------------------------------------------------------

def _build(signer, n=4, chain_id="whl_01j9xk3m2q8v7c4r5t6w8y0z1a"):
    records, prev = [], None
    for i in range(n):
        rec = append({"record_type": "test", "n": i}, chain_id=chain_id, previous=prev, signer=signer)
        records.append(rec)
        prev = rec
    return records


def test_genesis_and_links(signer):
    recs = _build(signer)
    assert recs[0]["chain"] == {"chain_id": "whl_01j9xk3m2q8v7c4r5t6w8y0z1a", "seq": 0, "previous_hash": ZERO_HASH}
    for a, b in zip(recs, recs[1:]):
        assert b["chain"]["seq"] == a["chain"]["seq"] + 1
        assert b["chain"]["previous_hash"] == record_hash(a)


def test_signature_covers_the_chain_link(signer, keys):
    recs = _build(signer)
    assert recs[1]["signature"]["key_id"] == signer.key_id
    assert recs[1]["signature"]["alg"] == "ES256"
    verify_chain(recs, keys)


def test_valid_chain_verifies(signer, keys):
    verify_chain(_build(signer), keys)


def test_mutated_body_fails(signer, keys):
    recs = _build(signer)
    recs[2]["n"] = 99
    with pytest.raises(ChainError):
        verify_chain(recs, keys)


def test_reorder_fails(signer, keys):
    recs = _build(signer)
    recs[1], recs[2] = recs[2], recs[1]
    with pytest.raises(ChainError):
        verify_chain(recs, keys)


def test_deletion_fails(signer, keys):
    recs = _build(signer)
    del recs[1]
    with pytest.raises(ChainError):
        verify_chain(recs, keys)


def test_signature_swap_fails(signer, keys):
    recs = _build(signer)
    recs[1]["signature"], recs[2]["signature"] = recs[2]["signature"], recs[1]["signature"]
    with pytest.raises(ChainError):
        verify_chain(recs, keys)


def test_unknown_key_fails(signer):
    with pytest.raises(ChainError):
        verify_chain(_build(signer), {})


def test_genesis_must_use_zero_hash(signer, keys):
    recs = _build(signer, n=1)
    recs[0]["chain"]["previous_hash"] = "sha256:" + "1" * 64
    with pytest.raises(ChainError):
        verify_chain(recs, keys)


def test_record_may_not_preset_chain_or_signature(signer):
    with pytest.raises(ChainError):
        append({"chain": {}}, chain_id="whl_x", previous=None, signer=signer)
    with pytest.raises(ChainError):
        append({"signature": {}}, chain_id="whl_x", previous=None, signer=signer)


def test_previous_must_belong_to_the_same_chain(signer):
    first = append({"n": 0}, chain_id="whl_a", previous=None, signer=signer)
    with pytest.raises(ChainError):
        append({"n": 1}, chain_id="whl_b", previous=first, signer=signer)


def test_continuation_across_a_handover(signer, keys):
    """19 §1.4 and §10.1: a new chain may start from another chain's handover record."""
    old = _build(signer, n=2, chain_id="whl_old")
    handover = append({"record_type": "handover"}, chain_id="whl_old", previous=old[-1], signer=signer)
    old.append(handover)
    new_host = LocalSigner.generate("key_01j9xk3m2q8v7c4r5t6w8y0z1c")
    first = append(
        {"record_type": "test", "n": 0},
        chain_id="whl_new",
        previous=None,
        signer=new_host,
        continues={"chain_id": "whl_old", "host_ref": "host-a", "handover_hash": record_hash(handover)},
    )
    assert first["chain"]["previous_hash"] == record_hash(handover)
    all_keys = {**keys, new_host.key_id: new_host.public_jwk}
    verify_chain(old, all_keys)
    verify_chain([first], all_keys, previous_chain=old)
    with pytest.raises(ChainError):
        verify_chain([first], all_keys)  # a continuation needs the chain it continues
