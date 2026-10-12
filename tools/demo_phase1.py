"""Phase 1 demo: sign a small chain, verify it, change one byte, watch verification fail."""

from datetime import datetime, timezone
import random

from gap_core.chain import ChainError, append, verify_chain
from gap_core.clock import FixedClock
from gap_core.ids import new_id
from gap_core.money import Money
from gap_core.timefmt import format_ts
from gap_keys.local import LocalSigner

clock = FixedClock(datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc))
rng = random.Random(12)
signer = LocalSigner.generate(new_id("key", clock=clock, rng=rng))
wheel = new_id("whl", clock=clock, rng=rng)

records, prev = [], None
for kind, amount in [("hold", 120000), ("capture", 118000), ("release", 2000)]:
    prev = append(
        {"record_type": "posting", "kind": kind, "amount": Money(amount, "XTS").to_json(), "posted_at": format_ts(clock.now())},
        chain_id=wheel, previous=prev, signer=signer,
    )
    records.append(prev)

keys = {signer.key_id: signer.public_jwk}
verify_chain(records, keys)
print(f"chain {wheel}: {len(records)} records verify")

records[1]["amount"]["amount_minor"] += 1
try:
    verify_chain(records, keys)
except ChainError as e:
    print(f"after changing one amount by one minor unit: {e}")
