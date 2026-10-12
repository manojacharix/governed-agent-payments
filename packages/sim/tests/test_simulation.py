"""Synthetic workloads through the whole stack; and proof that the checks catch real bugs."""

import pytest

import gap_sim.simulate as sim
from gap_core.evaluate import Determination


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_synthetic_workload_keeps_every_invariant(ledger, seed):
    report = sim.run(ledger.conn, seed=seed, payers=8, events=400, minutes_per_event=240)
    assert report.ok, report.violations[:10]
    assert report.outcomes["allow"] > 50 and report.outcomes["refuse"] > 0 and report.outcomes["no_authority"] > 0
    assert report.settlements["higher"] >= 0 and report.records > 400
    # no adversarial input was ever allowed
    assert not any(k.endswith(":allow") and not k.startswith("unverified_category") for k in report.adversarial)


def test_the_harness_catches_an_evaluator_that_allows_everything(ledger, monkeypatch):
    real = sim.evaluate

    def broken(inp):
        res = real(inp)
        if isinstance(res, Determination) and res.outcome != "allow":
            from dataclasses import replace
            return replace(res, outcome="allow", failing_clauses=(), clarify_fields=())
        return res

    monkeypatch.setattr(sim, "evaluate", broken)
    report = sim.run(ledger.conn, seed=7, payers=6, events=300, minutes_per_event=240)
    assert not report.ok
    assert any("allowed despite" in v for v in report.violations)


def test_the_harness_catches_tampering(ledger, admin):
    sim_ = sim.Simulation(ledger.conn, seed=9, payers=4)
    sim_.run(150, 240)
    admin.execute("ALTER TABLE ledger_records DISABLE TRIGGER USER")
    admin.execute("UPDATE ledger_records SET body = jsonb_set(body, '{amount,amount_minor}', '1') WHERE ctid = (SELECT ctid FROM ledger_records WHERE record_type = 'posting' LIMIT 1)")
    admin.execute("ALTER TABLE ledger_records ENABLE TRIGGER USER")
    sim_.report.violations.clear()
    sim_._check()
    assert any(v.startswith("integrity") for v in sim_.report.violations)
    assert any("shadow ledger and real ledger differ" in v for v in sim_.report.violations)
