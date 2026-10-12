"""Readable views of a bundle: labelled as renderings, escaped, deterministic, and faithful to the verifier."""

import copy

import pytest

from gap_evidence.render import render_html, render_text, timeline


def bundle():
    rec = lambda seq, **r: {"chain": {"seq": seq}, **r}
    return {
        "bundle_id": "bnd_x", "proposal_id": "prp_x", "wheel_id": "whl_1", "requester_role": "payer", "basis": "default",
        "divergence": {"primary": "merchant_divergence", "contributing": []},
        "records": [
            rec(2, record_type="determination", stage="proposal", outcome="refuse", approval_mode="explicit_tap", evaluated_at="t2",
                failing_clauses=[{"rule_id": "payer.no_alcohol", "dimension": "category", "author": "payer", "overridable": True}]),
            rec(1, record_type="proposal", received_at="t1", spoke_id="spk_g", intent_id="i", merchant_ref="<script>alert(1)</script>",
                total={"amount_minor": 19000, "currency": "INR"}),
        ],
    }


REPORT = {"bundle_signature": "ok", "key_log": "ok", "signatures": "ok", "chain": "ok", "keys": "ok", "replay": "reproduced",
          "presented_hash": "matches", "ok": True, "attestations": [{"fact": "x.y", "class": "source_signed", "verified": True}]}


def test_every_view_says_it_is_a_rendering_and_names_the_bundle():
    assert "a rendering of bundle bnd_x; the bundle is the evidence" in render_text(bundle(), REPORT)
    assert "This page is a rendering of bundle" in render_html(bundle(), REPORT)


def test_bundle_content_is_escaped():
    html = render_html(bundle(), REPORT)
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_timeline_follows_the_chain_and_names_the_rule_author():
    t = timeline(bundle())
    assert [e["seq"] for e in t] == [1, 2]
    assert "190.00 INR" in t[0]["what"]
    assert "payer.no_alcohol (category, written by the payer, the payer may override it)" in t[1]["what"]


def test_rendering_is_deterministic_and_does_not_mutate():
    b = bundle()
    before = copy.deepcopy(b)
    assert render_html(b, REPORT) == render_html(b, REPORT) and render_text(b, REPORT) == render_text(b, REPORT)
    assert b == before


@pytest.mark.parametrize("field,value", [("replay", "differs"), ("signatures", "failed")])
def test_a_failing_verifier_shows_as_failing(field, value):
    html = render_html(bundle(), {**REPORT, field: value, "ok": False})
    assert f"class='bad'>{value}<" in html and 'class="bad">False<' in html
