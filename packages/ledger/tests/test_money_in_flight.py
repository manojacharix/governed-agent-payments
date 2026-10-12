"""Phase 4: 03 §4 tests. Holds, captures, releases, overcaptures and refunds, derived balances, concurrency."""

import random
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from gap_core.authority import Window
from gap_core.clock import FixedClock
from gap_core.evaluate import evaluate
from gap_core.money import Money
from gap_core.testing import SPOKE, WHEEL, scenario
from gap_keys.local import LocalSigner
from gap_ledger.ledger import Ledger, LedgerError
from gap_ledger.money import Account, HoldRefused, MoneyInFlight, Signal, SignalRefused, replay

SIGNER = LocalSigner.generate("key_host")
MONTH = Window("calendar_month", "Etc/UTC")
T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
BANK = Signal("bank", "stmt-1")


def inr(x):
    return Money(x, "INR")


def accounts(spoke_cap=10000, hub=50000):
    return [Account("spk_parent", MONTH, None), Account(SPOKE, MONTH, inr(spoke_cap)), Account(WHEEL, MONTH, inr(hub))]


@pytest.fixture
def mif(ledger):
    return MoneyInFlight(ledger, SIGNER, FixedClock(T0), random.Random(4))


def bal(ledger, owner, window="2026-10"):
    a = replay(ledger, WHEEL).accounts.get((owner, "INR", window))
    return (a.held, a.spent) if a else (0, 0)


def test_hold_posts_spoke_ancestors_and_hub_together(mif, ledger):
    recs = mif.hold(WHEEL, "det_1", inr(3000), accounts())
    assert len(recs) == 3 and len({r["txn_group_id"] for r in recs}) == 1
    assert bal(ledger, SPOKE) == (3000, 0) and bal(ledger, "spk_parent") == (3000, 0) and bal(ledger, WHEEL) == (3000, 0)


def test_exact_capture(mif, ledger):
    mif.hold(WHEEL, "det_1", inr(3000), accounts())
    assert mif.capture(WHEEL, "det_1", inr(3000), BANK) == {"overcaptured": 0}
    assert bal(ledger, SPOKE) == (0, 3000)


def test_lower_capture_releases_the_rest(mif, ledger):
    mif.hold(WHEEL, "det_1", inr(3000), accounts())
    mif.capture(WHEEL, "det_1", inr(2500), BANK)
    assert bal(ledger, SPOKE) == (0, 2500) and bal(ledger, WHEEL) == (0, 2500)


def test_overcapture_blocks_new_holds_until_back_in_range(mif, ledger):
    mif.hold(WHEEL, "det_1", inr(9000), accounts())
    assert mif.capture(WHEEL, "det_1", inr(10500), BANK) == {"overcaptured": 1500}
    assert bal(ledger, SPOKE) == (0, 10500)  # available is now -500
    with pytest.raises(HoldRefused, match="overspent"):
        mif.hold(WHEEL, "det_2", inr(1), accounts())


def test_release_on_failure(mif, ledger):
    mif.hold(WHEEL, "det_1", inr(3000), accounts())
    mif.release(WHEEL, "det_1", Signal("rail", "declined"))
    assert bal(ledger, SPOKE) == (0, 0)


def test_no_capture_without_a_hold(mif):
    with pytest.raises(LedgerError):
        mif.capture(WHEEL, "det_missing", inr(1), BANK)


def test_one_hold_per_payment(mif):
    mif.hold(WHEEL, "det_1", inr(100), accounts())
    with pytest.raises(HoldRefused):
        mif.hold(WHEEL, "det_1", inr(100), accounts())


def test_hold_beyond_any_limit_is_refused(mif, ledger):
    with pytest.raises(HoldRefused):
        mif.hold(WHEEL, "det_1", inr(10001), accounts())
    assert replay(ledger, WHEEL).accounts == {}  # nothing posted on any account


def test_agent_messages_never_move_money():
    with pytest.raises(SignalRefused):
        Signal("agent", "I paid, trust me")


def test_refund_goes_to_the_original_window_only(ledger):
    clock = FixedClock(T0)
    m = MoneyInFlight(ledger, SIGNER, clock, random.Random(5))
    m.hold(WHEEL, "det_1", inr(4000), accounts())
    m.capture(WHEEL, "det_1", inr(4000), BANK)
    clock.advance(timedelta(days=30))  # now in November
    m.refund(WHEEL, "det_1", inr(1500), Signal("merchant", "refund-77"))
    assert bal(ledger, SPOKE, "2026-10") == (0, 2500)
    assert bal(ledger, SPOKE, "2026-11") == (0, 0)  # a refund never increases a later window's room


def test_refund_never_exceeds_what_was_captured(mif):
    mif.hold(WHEEL, "det_1", inr(4000), accounts())
    mif.capture(WHEEL, "det_1", inr(3000), BANK)
    mif.refund(WHEEL, "det_1", inr(2000), BANK)
    with pytest.raises(LedgerError):
        mif.refund(WHEEL, "det_1", inr(1001), BANK)


def test_late_capture_posts_to_the_hold_window(ledger):
    clock = FixedClock(datetime(2026, 10, 31, 23, 50, tzinfo=timezone.utc))
    m = MoneyInFlight(ledger, SIGNER, clock, random.Random(6))
    m.hold(WHEEL, "det_1", inr(2000), accounts())
    clock.advance(timedelta(hours=1))  # settles on 1 November
    m.capture(WHEEL, "det_1", inr(2000), BANK)
    assert bal(ledger, SPOKE, "2026-10") == (0, 2000)
    assert bal(ledger, SPOKE, "2026-11") == (0, 0)


def test_replay_reproduces_every_balance_and_the_chain_verifies(mif, ledger):
    mif.hold(WHEEL, "det_1", inr(1000), accounts())
    mif.capture(WHEEL, "det_1", inr(900), BANK)
    mif.hold(WHEEL, "det_2", inr(2000), accounts())
    assert replay(ledger, WHEEL).accounts == replay(ledger, WHEEL).accounts
    assert ledger.verify_integrity({SIGNER.key_id: SIGNER.public_jwk}).ok


def test_concurrent_holds_never_exceed_a_limit(ledger_dsn):
    """03 §4: the balance equation holds under concurrent authorisations."""
    results = []

    def worker(i):
        with psycopg.connect(ledger_dsn, autocommit=True) as c:
            c.execute("SET ROLE ledger_writer")
            m = MoneyInFlight(Ledger(c), SIGNER, FixedClock(T0), random.Random(100 + i))
            try:
                m.hold(WHEEL, f"det_{i}", inr(2000), accounts(spoke_cap=10000))
                results.append("held")
            except HoldRefused:
                results.append("refused")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("held") == 5 and results.count("refused") == 7
    with psycopg.connect(ledger_dsn, autocommit=True) as c:
        held, spent = (replay(Ledger(c), WHEEL).accounts[(SPOKE, "INR", "2026-10")].held, 0)
        assert held == 10000


def test_ledger_usage_feeds_the_evaluator(mif, ledger):
    """End to end: a hold placed in the ledger reduces the room evaluate() sees."""
    inp = scenario("INR")
    st = replay(ledger, WHEEL)
    assert evaluate(replace(inp, usage={**inp.usage, **st.usage()})).outcome == "allow"
    mif.hold(WHEEL, "det_big", inr(390000), [Account(SPOKE, MONTH, inr(400000)), Account(WHEEL, MONTH, inr(1500000))])
    usage = {**inp.usage, **replay(ledger, WHEEL).usage()}
    det = evaluate(replace(inp, usage=usage))
    assert det.outcome == "refuse" and "sys.cap_exceeded" in {c.rule_id for c in det.failing_clauses}
