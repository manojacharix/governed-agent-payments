"""make demo, as a test: the whole story runs and its output matches the checked-in snapshot.

To accept an intended change: UPDATE_GOLDEN=1 uv run pytest packages/api/tests/test_story.py
"""

import os
from pathlib import Path

from gap_api.story import normalise, run_story

GOLDEN = Path(__file__).parent / "golden"


def check(name, text):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(text)
    assert text == path.read_text(), f"{name} changed; rerun with UPDATE_GOLDEN=1 if intended"


def test_the_demo_story(ledger, profile_conn):
    out = run_story(ledger, profile_conn)
    assert "verifier passes: True" in out["steps"]
    assert "hide the overcharge: verifier passes: False" in out["steps"]
    check("demo_steps.txt", normalise(out["steps"]))
    check("evidence_payer.txt", normalise(out["payer_text"]))
    assert "The bundle is the evidence" in out["merchant_html"] and "Captured" not in out["merchant_html"]
