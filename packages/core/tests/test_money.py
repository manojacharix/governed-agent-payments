import pytest
from hypothesis import given, strategies as st

from gap_core.money import MAX_MINOR, CurrencyMismatch, Money


def test_built_from_integer_minor_units():
    m = Money(120000, "XTS")
    assert m.amount_minor == 120000
    assert m.currency == "XTS"


@pytest.mark.parametrize("bad", [12.5, 1.0, "100", None, True])
def test_rejects_anything_but_int(bad):
    with pytest.raises(TypeError):
        Money(bad, "XTS")


@pytest.mark.parametrize("code", ["xts", "XT", "XTSX", "", "X1S", 840])
def test_rejects_bad_currency_codes(code):
    with pytest.raises(ValueError):
        Money(1, code)


def test_rejects_amounts_beyond_safe_integer_range():
    Money(MAX_MINOR, "XTS")
    Money(-MAX_MINOR, "XTS")
    with pytest.raises(ValueError):
        Money(MAX_MINOR + 1, "XTS")
    with pytest.raises(ValueError):
        Money(-MAX_MINOR - 1, "XTS")


def test_no_float_constructor_exists():
    assert not hasattr(Money, "from_float")


def test_add_and_subtract_same_currency():
    assert Money(100, "XTS") + Money(50, "XTS") == Money(150, "XTS")
    assert Money(100, "XTS") - Money(150, "XTS") == Money(-50, "XTS")


def test_mixing_currencies_is_refused():
    with pytest.raises(CurrencyMismatch):
        Money(1, "XTS") + Money(1, "XXX")
    with pytest.raises(CurrencyMismatch):
        Money(1, "XTS") - Money(1, "XXX")
    with pytest.raises(CurrencyMismatch):
        Money(1, "XTS") < Money(2, "XXX")


def test_ordering_within_a_currency():
    assert Money(1, "XTS") < Money(2, "XTS")
    assert Money(2, "XTS") >= Money(2, "XTS")


def test_equality_never_crosses_currencies():
    assert Money(1, "XTS") != Money(1, "XXX")


def test_immutable():
    m = Money(1, "XTS")
    with pytest.raises(AttributeError):
        m.amount_minor = 2  # type: ignore[misc]


def test_json_round_trip():
    m = Money(-42, "XTS")
    assert m.to_json() == {"amount_minor": -42, "currency": "XTS"}
    assert Money.from_json(m.to_json()) == m


@pytest.mark.parametrize(
    "bad",
    [
        {"amount_minor": 1},
        {"amount_minor": 1, "currency": "XTS", "extra": 1},
        {"amount_minor": 1.5, "currency": "XTS"},
    ],
)
def test_from_json_is_closed_and_strict(bad):
    with pytest.raises((TypeError, ValueError)):
        Money.from_json(bad)


def test_overflow_on_arithmetic_is_refused():
    with pytest.raises(ValueError):
        Money(MAX_MINOR, "XTS") + Money(1, "XTS")


@given(st.integers(-(10**12), 10**12), st.integers(-(10**12), 10**12))
def test_addition_is_exact(a, b):
    assert (Money(a, "XTS") + Money(b, "XTS")).amount_minor == a + b
