"""Reservation TTL — emulates "user added, came back later".

Clock is INJECTED. No real waits.

Scenarios:
  A) Buyer A reserves 1 unit (only stock=1). After +10 min (within 15-min TTL),
     reservation still held; buyer B cannot claim.
  B) After +90 min, expire_reservations() runs. Order auto-cancelled, stock
     released. Buyer B then succeeds.
  C) Buyer A returns at +90 min and tries mock_payment_success on the original
     order: must fail (expired reservation), no stock decrement.

Negative-only. Exit 0 = no anomaly. Exit 1 = TTL semantics broken.
"""
import sys
import tempfile
import uuid
from pathlib import Path

from cart import Cart

HERE = Path(__file__).parent
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_ttl_")) / "db.sqlite"


class FakeClock:
    def __init__(self, t0: int):
        self.t = t0

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += seconds


def main() -> int:
    clock = FakeClock(1_700_000_000)
    cart = Cart(str(DB_PATH), now=clock)
    # Single unit of one SKU so contention is unambiguous.
    cart.load_inventory({"omega": {"price_kopecks": 200000, "stock": 1}})

    failures: list[str] = []

    # Buyer A reserves the only unit.
    cart_a = cart.create_cart()
    cart.add_item(cart_a, "omega", 1, str(uuid.uuid4()))
    order_a = cart.start_checkout(cart_a, str(uuid.uuid4()))

    if cart.get_stock_available()["omega"] != 0:
        failures.append(
            f"after A reserved, available should be 0 got {cart.get_stock_available()['omega']}"
        )

    # === Scenario A: +10 min — reservation must still hold ===
    clock.advance(10 * 60)
    cart_b1 = cart.create_cart()
    cart.add_item(cart_b1, "omega", 1, str(uuid.uuid4()))
    try:
        cart.start_checkout(cart_b1, str(uuid.uuid4()))
        failures.append(
            "at +10min buyer B succeeded — reservation should still hold stock"
        )
    except Exception:
        pass  # expected: insufficient stock

    if cart.get_stock_available()["omega"] != 0:
        failures.append(
            f"at +10min available should still be 0 got {cart.get_stock_available()['omega']}"
        )

    # === Scenario B: +90 min — reservation must have expired ===
    clock.advance(80 * 60)  # total = +90 min from t0
    if cart.get_stock_available()["omega"] != 1:
        failures.append(
            f"at +90min available should auto-reflect 1 (expired hold), got {cart.get_stock_available()['omega']}"
        )

    cancelled_count = cart.expire_reservations()
    if cancelled_count != 1:
        failures.append(
            f"expire_reservations() should cancel exactly 1 order at +90min, got {cancelled_count}"
        )

    state_a = cart.get_order_state(order_a["order_id"])
    if state_a != "cancelled":
        failures.append(f"order A should be cancelled after expiry, got state={state_a}")

    # === Scenario C: A returns and tries to pay on expired order ===
    try:
        cart.mock_payment_success(order_a["order_id"], "tx_late", str(uuid.uuid4()))
        failures.append(
            "buyer A should NOT be able to capture payment on cancelled order"
        )
    except Exception:
        pass  # expected: invalid transition

    # Buyer B can now succeed.
    cart_b2 = cart.create_cart()
    cart.add_item(cart_b2, "omega", 1, str(uuid.uuid4()))
    try:
        order_b = cart.start_checkout(cart_b2, str(uuid.uuid4()))
        cart.mock_payment_success(order_b["order_id"], "tx_b", str(uuid.uuid4()))
    except Exception as e:
        failures.append(f"buyer B should succeed after expiry but raised: {e}")

    final = cart.get_stock_total()
    if final["omega"] != 0:
        failures.append(
            f"after B's successful payment, stock should be 0 got {final['omega']}"
        )

    # Event log integrity: order A must have a cancelled event.
    events_a = cart.get_order_events(order_a["order_id"])
    if not any(e[0] == "cancelled" for e in events_a):
        failures.append(
            f"order A event log missing 'cancelled' event: {[e[0] for e in events_a]}"
        )

    if failures:
        print("FAIL: reservation TTL violations:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
