import random
import unicodedata

import pytest
from hypothesis import given, settings, strategies as st

from gap_core.canonical import MAX_DEPTH, CanonicalError, canonical
from gap_core.hashing import ZERO_HASH, hash_value, is_hash


def test_sorts_keys_and_drops_whitespace():
    assert canonical({"b": 2, "a": 1, "c": [3, {"z": 0, "y": "x"}]}) == b'{"a":1,"b":2,"c":[3,{"y":"x","z":0}]}'


def test_keys_sort_by_utf16_code_units():
    # RFC 8785 sorts by UTF-16 code units: the euro sign is 0x20AC, the emoji starts with surrogate 0xD83D,
    # so the euro sign sorts first (code-point order would agree here, but surrogates are the rule).
    data = {"\U0001f600": 1, "€": 2, "a": 3}
    assert canonical(data) == '{"a":3,"€":2,"\U0001f600":1}'.encode()


@pytest.mark.parametrize("bad", [1.0, 0.5, float("nan"), {"a": 1.5}, [1, 2.0]])
def test_rejects_floats_anywhere(bad):
    with pytest.raises(CanonicalError):
        canonical(bad)


def test_rejects_null_because_optional_keys_are_omitted():
    with pytest.raises(CanonicalError):
        canonical({"a": None})


@pytest.mark.parametrize("big", [2**53, -(2**53)])
def test_rejects_integers_outside_safe_range(big):
    with pytest.raises(CanonicalError):
        canonical({"n": big})


def test_rejects_non_string_keys():
    with pytest.raises(CanonicalError):
        canonical({1: "a"})


def test_rejects_unsupported_types():
    with pytest.raises(CanonicalError):
        canonical({"s": {1, 2}})


def test_strings_are_nfc_normalised():
    decomposed = "é"  # e + combining acute
    composed = unicodedata.normalize("NFC", decomposed)
    assert canonical({"k": decomposed}) == canonical({"k": composed})
    assert canonical({decomposed: 1}) == canonical({composed: 1})


def test_booleans_are_not_integers():
    assert canonical({"a": True}) == b'{"a":true}'
    assert canonical({"a": 1}) != canonical({"a": True})


def _random_tree(rng: random.Random, depth: int = 0):
    kind = rng.choice(["int", "str", "bool", "list", "dict"] if depth < 3 else ["int", "str", "bool"])
    if kind == "int":
        return rng.randint(-(2**53) + 1, 2**53 - 1)
    if kind == "str":
        return "".join(rng.choice("abcé€😀 \n\"\\") for _ in range(rng.randint(0, 6)))
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "list":
        return [_random_tree(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {f"k{rng.randint(0, 50)}{rng.choice('aé€')}": _random_tree(rng, depth + 1) for _ in range(rng.randint(0, 5))}


def _shuffled(value, rng: random.Random):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: _shuffled(v, rng) for k, v in items}
    if isinstance(value, list):
        return [_shuffled(v, rng) for v in value]
    return value


def test_thousand_permutations_give_identical_bytes():
    """From the build guide: the same logical record, built with keys in any order, must hash identically."""
    rng = random.Random(1994)
    for _ in range(50):
        tree = {"root": _random_tree(rng)}
        expected = canonical(tree)
        for _ in range(20):
            assert canonical(_shuffled(tree, rng)) == expected
    # 50 trees x 20 permutations = 1,000 permutations


_text = st.text(st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=8)
json_values = st.recursive(
    st.integers(-(2**53) + 1, 2**53 - 1) | st.booleans() | _text,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(_text, children, max_size=4),
    max_leaves=20,
)


@settings(max_examples=200)
@given(json_values)
def test_canonical_is_deterministic(value):
    assert canonical(value) == canonical(value)


def test_hash_format():
    h = hash_value({"a": 1})
    assert is_hash(h)
    assert h.startswith("sha256:") and len(h) == 7 + 64
    assert h == hash_value({"a": 1})
    assert h != hash_value({"a": 2})


def test_zero_hash_is_the_genesis_value():
    assert ZERO_HASH == "sha256:" + "0" * 64
    assert is_hash(ZERO_HASH)


@pytest.mark.parametrize("bad", ["sha256:abc", "sha1:" + "0" * 64, "sha256:" + "G" * 64, "sha256:" + "A" * 64])
def test_is_hash_rejects_bad_forms(bad):
    assert not is_hash(bad)


@pytest.mark.parametrize("value", ["a\x00b", {"k\x00": 1}, "\ud800", ["x", {"y": "\udfff"}]])
def test_nul_and_unpaired_surrogates_are_refused(value):
    with pytest.raises(CanonicalError):
        canonical(value)


def test_nesting_is_bounded():
    v = 1
    for _ in range(MAX_DEPTH):
        v = [v]
    canonical(v)  # exactly MAX_DEPTH deep is fine
    with pytest.raises(CanonicalError):
        canonical([v])
