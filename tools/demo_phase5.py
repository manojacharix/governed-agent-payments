"""Phase 5 demo (make execute): ambiguous merchant responses never become a second order."""

import random
from datetime import datetime, timezone

import psycopg

from gap_adapters.execute import Authorisation, Executor
from gap_adapters.fakes import FakeMerchant
from gap_core.authority import RailProfile
from gap_core.clock import FixedClock
from gap_core.evaluate import evaluate
from gap_core.testing import body, scenario
from gap_keys.local import LocalSigner
from gap_ledger.ledger import Ledger
from gap_ledger.pgtemp import LEDGER_SQL, TempCluster

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
WALLET = RailProfile("in:merchant_wallet", "none", "not_bound", ("explicit_tap",),
                     retry_policy={"safe_errors": ["RATE_LIMITED"], "max_retries": 2, "window_s": 600})
CASES = [
    ("a", ["timeout_after_create", "ok"], "merchant a (remembers idempotency keys, sees only its last 5 orders): created the order, then timed out"),
    ("b", ["timeout_after_create", "ok"], "merchant b (ignores idempotency keys, sees everything): created the order, then timed out"),
    ("b", ["error:RATE_LIMITED", "ok"], "merchant b: rate-limited before creating anything"),
    ("a", ["error:RATE_LIMITED", "ok"], "merchant a: rate-limited, but its history is incomplete"),
    ("b", ["error:INTERNAL", "ok"], "merchant b: an internal error the payment-method profile does not mark safe"),
]

pg = TempCluster()
try:
    pg.create_template("tmpl_ledger", LEDGER_SQL)
    conn = psycopg.connect(pg.fresh_db("tmpl_ledger", "demo5"), autocommit=True)
    conn.execute("SET ROLE ledger_writer")
    signer = LocalSigner.generate("key_host")
    ex = Executor(Ledger(conn), signer, FixedClock(T0), random.Random(5))
    for i, (shape, script, title) in enumerate(CASES, 1):
        b = body("INR", rail_id=WALLET.rail_id)
        first = evaluate(scenario("INR", body=b, rails={WALLET.rail_id: WALLET}))
        det = evaluate(scenario("INR", body=b, rails={WALLET.rail_id: WALLET}, stage="authorisation",
                                authorised_presented_hash=first.presented_hash, determination_id=f"det_{i}"))
        m = FakeMerchant(shape, FixedClock(T0), script=script)
        m.add_cart("cart-1", b.lines, b.fees)
        res = ex.execute("whl_demo", det, "cart-1", "dst_home", WALLET, m, Authorisation("payer_approval", no_auth_rail_ok=True))
        print(f"\n{i}. {title}")
        print(f"   outcome {res.outcome}; place() called {m.place_calls}x; orders at the merchant: {len(m.orders_for_cart('cart-1'))}")
        for a in res.attempts:
            print(f"   attempt {a['attempt_number']}: {a['response_status']} -> {a['outcome']}")
finally:
    pg.stop()
