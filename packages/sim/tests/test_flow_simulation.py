"""The full Phase 6 flow under synthetic load."""

import pytest

from gap_sim.flow_sim import run


@pytest.mark.parametrize("seed", [1, 2])
def test_flow_workload_keeps_every_invariant(ledger, profile_conn, seed):
    rep = run(ledger.conn, profile_conn, seed=seed, payers=6, events=300, minutes_per_event=120, bundles=8)
    assert rep.ok, rep.violations[:10]
    assert rep.statuses["executed"] > 20 and rep.actions["authorise"] > 20
    assert rep.bundles["payer:verified"] > 0 and rep.bundles["merchant:verified"] > 0
