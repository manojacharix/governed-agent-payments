"""The codec round-trips every evaluation input exactly, so stored decisions can be replayed byte for byte."""

from dataclasses import replace

import pytest
from hypothesis import given, settings, strategies as st

from gap_core.authority import Caller
from gap_core.canonical import canonical
from gap_core.codec import CodecError, decode, encode
from gap_core.evaluate import PriorIntent, RecentProposal, Usage, evaluate
from gap_core.hashing import hash_value
from gap_core.testing import AT, PARAMS, repeat_spoke, rule, scenario, xts


def test_scenario_round_trips_and_replays_identically():
    inp = scenario(prior_intents={"x": PriorIntent("sha256:" + "1" * 64, "det_0")},
                   recent=(RecentProposal("prp_0", "i0", "mrc_shop", "sha256:" + "2" * 64, AT),))
    back = decode(encode(inp))
    assert back == inp
    assert canonical(evaluate(back).to_record()) == canonical(evaluate(inp).to_record())


def test_silent_scenario_with_rules_round_trips():
    inp = scenario(spokes={"spk_repeat": repeat_spoke()}, spoke_id="spk_repeat", caller_id="clr_scheduler",
                   caller=Caller("clr_scheduler", "key_bound"),
                   params=replace(PARAMS, silent_preconditions=({"fact": "notice.delivered", "op": "eq", "value": True},)))
    inp = replace(inp, wheel=replace(inp.wheel, rules=(
        rule("a", "amount", per_transaction_max=xts(100000)),
        rule("t", "time", exclude=[{"days": ["sun"], "from": "23:00", "to": "07:00", "tz": "Asia/Kolkata"}]),
        rule("c", "conditions", when={"not": {"fact": "x", "op": "exists"}}, effect="clarify"),
    )))
    assert decode(encode(inp)) == inp


def test_encoding_is_canonical_and_hashable():
    a, b = scenario(), scenario()
    assert hash_value(encode(a)) == hash_value(encode(b))
    assert hash_value(encode(a)) != hash_value(encode(replace(a, at=AT.replace(minute=31))))


def test_none_and_maps_with_tuple_keys_survive():
    inp = scenario(caller=None, usage={("whl_test", "2026-10"): Usage(1, {"XTS": 5})})
    back = decode(encode(inp))
    assert back.caller is None and back.usage == {("whl_test", "2026-10"): Usage(1, {"XTS": 5})}


@pytest.mark.parametrize("bad", [1.5, object(), {"$t": "Nope"}])
def test_refuses_what_it_cannot_represent(bad):
    with pytest.raises(CodecError):
        decode(bad) if isinstance(bad, dict) else encode(bad)


def test_tampered_encoding_fails_validation_on_decode():
    t = encode(scenario())
    t["proposal"]["body"]["agent_confidence"] = 5000  # out of PerMille range
    with pytest.raises(ValueError):
        decode(t)


@settings(max_examples=60)
@given(st.integers(0, 3600), st.integers(0, 1000))
def test_round_trip_property(seconds, confidence):
    from datetime import timedelta
    from gap_core.testing import body

    inp = scenario(body=body(agent_confidence=confidence), at=AT + timedelta(seconds=seconds))
    assert decode(encode(inp)) == inp
