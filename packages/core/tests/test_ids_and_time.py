import random
from datetime import datetime, timedelta, timezone

import pytest

from gap_core.clock import FixedClock
from gap_core.ids import PREFIXES, IdError, new_id, parse_id
from gap_core.timefmt import TimeError, format_ts, parse_ts

T0 = datetime(2026, 10, 9, 13, 5, 0, 123000, tzinfo=timezone.utc)


def test_timestamp_has_exact_millisecond_precision_and_z():
    assert format_ts(T0) == "2026-10-09T13:05:00.123Z"
    assert format_ts(T0.replace(microsecond=0)) == "2026-10-09T13:05:00.000Z"


def test_timestamp_truncates_sub_millisecond():
    assert format_ts(T0.replace(microsecond=123999)) == "2026-10-09T13:05:00.123Z"


def test_timestamp_round_trip():
    assert parse_ts(format_ts(T0)) == T0


def test_non_utc_input_is_converted():
    ist = timezone(timedelta(hours=5, minutes=30))
    assert format_ts(T0.astimezone(ist)) == "2026-10-09T13:05:00.123Z"


def test_naive_datetime_is_refused():
    with pytest.raises(TimeError):
        format_ts(datetime(2026, 10, 9, 13, 5))


@pytest.mark.parametrize(
    "bad",
    ["2026-10-09T13:05:00Z", "2026-10-09T13:05:00.1Z", "2026-10-09T13:05:00.123+05:30", "2026-10-09 13:05:00.123Z", "2026-13-09T13:05:00.123Z"],
)
def test_parse_is_strict(bad):
    with pytest.raises(TimeError):
        parse_ts(bad)


def test_fixed_clock_is_injected_time():
    clock = FixedClock(T0)
    assert clock.now() == T0
    clock.advance(timedelta(seconds=5))
    assert clock.now() == T0 + timedelta(seconds=5)


def test_fixed_clock_refuses_naive_or_backwards():
    with pytest.raises(TimeError):
        FixedClock(datetime(2026, 1, 1))
    clock = FixedClock(T0)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))


def test_id_format():
    clock = FixedClock(T0)
    i = new_id("spk", clock=clock, rng=random.Random(1))
    assert i.startswith("spk_")
    body = i[4:]
    assert len(body) == 26
    assert body == body.lower()
    assert parse_id(i) == ("spk", body)


def test_ids_sort_by_creation_time():
    clock = FixedClock(T0)
    rng = random.Random(2)
    a = new_id("det", clock=clock, rng=rng)
    clock.advance(timedelta(milliseconds=1))
    b = new_id("det", clock=clock, rng=rng)
    assert a < b


def test_ids_are_unique_within_the_same_millisecond():
    clock = FixedClock(T0)
    rng = random.Random(3)
    ids = {new_id("pst", clock=clock, rng=rng) for _ in range(1000)}
    assert len(ids) == 1000


def test_unknown_prefix_is_refused():
    with pytest.raises(IdError):
        new_id("xyz", clock=FixedClock(T0), rng=random.Random(1))


@pytest.mark.parametrize("bad", ["spk_short", "SPK_01j9xk3m2q8v7c4r5t6w8y0z1a", "xyz_01j9xk3m2q8v7c4r5t6w8y0z1a", "spk_01j9xk3m2q8v7c4r5t6w8y0z1u", "spk01j9xk3m2q8v7c4r5t6w8y0z1a"])
def test_parse_rejects_malformed_ids(bad):
    with pytest.raises(IdError):
        parse_id(bad)


def test_prefixes_match_the_model_spec():
    assert PREFIXES == {
        "whl", "spk", "rul", "evt", "rcp", "prp", "det", "res", "pst", "txg", "exa", "swp", "fnd",
        "bnd", "rls", "key", "chl", "asr", "clr", "enr", "rcv", "pyr", "opr", "mrc", "dst", "prf", "ntc",
    }
