"""Contract tests (R3): every merchant adapter passes the same suite, whatever its shape."""

from datetime import datetime, timedelta, timezone

import pytest

from gap_adapters.fakes import FakeMerchant
from gap_adapters.interface import OUTCOMES, AdapterInfo, CartView, MerchantError, MerchantTimeout, OrderView
from gap_core.clock import FixedClock
from gap_core.hashing import is_hash
from gap_core.money import Money
from gap_core.proposal import CartLine, Fee

T0 = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
LINES = (CartLine("item_milk", 2, Money(6000, "INR")), CartLine("item_bread", 1, Money(4500, "INR")))


@pytest.fixture(params=["a", "b"])
def merchant(request):
    m = FakeMerchant(request.param, FixedClock(T0))
    m.add_cart("cart-1", LINES, (Fee("delivery", Money(3000, "INR")),))
    return m


def test_describe(merchant):
    info = merchant.describe()
    assert isinstance(info, AdapterInfo) and info.rails_offered
    assert all(o in OUTCOMES for o in info.outcomes.values())
    assert set(info.outcomes.values()) >= {"success", "failure", "unknown"}
    assert all(":" in r for r in info.rails_offered)


def test_cart_total_adds_up_and_hash_is_stable(merchant):
    v = merchant.get_cart("cart-1")
    assert isinstance(v, CartView)
    assert v.total.amount_minor == sum(l.unit_price.amount_minor * l.quantity for l in v.lines) + sum(f.amount.amount_minor for f in v.fees)
    assert is_hash(v.merchant_cart_hash) and v.merchant_cart_hash == merchant.get_cart("cart-1").merchant_cart_hash


def test_price_change_changes_the_cart_hash(merchant):
    before = merchant.get_cart("cart-1").merchant_cart_hash
    merchant.set_price("cart-1", "item_milk", Money(6100, "INR"))
    assert merchant.get_cart("cart-1").merchant_cart_hash != before


def test_place_then_read_back(merchant):
    rail = merchant.describe().rails_offered[0]
    res = merchant.place("cart-1", rail, "key-1", "dst_home")
    assert merchant.describe().outcomes[res.status] in ("success", "awaiting_payer")
    order = merchant.get_order(res.merchant_order_ref)
    assert isinstance(order, OrderView) and order.cart_ref == "cart-1"
    orders, cov = merchant.list_orders(T0 - timedelta(minutes=1))
    assert order.merchant_order_ref in {o.merchant_order_ref for o in orders}


def test_unknown_rail_is_an_error(merchant):
    with pytest.raises(MerchantError):
        merchant.place("cart-1", "zz:nowhere", "key-1", "dst_home")


@pytest.mark.parametrize("step, exc", [("timeout_before_create", MerchantTimeout), ("error:RATE_LIMITED", MerchantError)])
def test_scripted_failures_raise(merchant, step, exc):
    merchant.script = [step]
    with pytest.raises(exc):
        merchant.place("cart-1", merchant.describe().rails_offered[0], "key-1", "dst_home")
    assert merchant.orders == {}


def test_timeout_after_create_leaves_an_order(merchant):
    merchant.script = ["timeout_after_create"]
    with pytest.raises(MerchantTimeout):
        merchant.place("cart-1", merchant.describe().rails_offered[0], "key-1", "dst_home")
    assert len(merchant.orders) == 1


def test_the_two_shapes_really_differ():
    a, b = FakeMerchant("a", FixedClock(T0)), FakeMerchant("b", FixedClock(T0))
    ia, ib = a.describe(), b.describe()
    assert ia.honours_idempotency and not ib.honours_idempotency
    assert not ia.history_coverage.complete and ib.history_coverage.complete
    assert not ia.supports_cancel and ib.supports_cancel
    assert set(ia.outcomes) != set(ib.outcomes)
