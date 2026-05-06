"""Idempotency — same Idempotency-Key replayed must not double-mutate.

Emulates network retries: client sends add_item with key K, gets timeout, retries
with same K. Server must apply the mutation exactly once and return identical
response on retry.

Negative-only. Exit 0 = no anomaly. Exit 1 = double-mutation or response drift.
"""
import json
import sys
import tempfile
import uuid
from pathlib import Path

from cart import Cart

HERE = Path(__file__).parent
MOCK = HERE / "mock"
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_idem_")) / "db.sqlite"


def main() -> int:
    inventory = json.loads((MOCK / "inventory.json").read_text())
    cart = Cart(str(DB_PATH))
    cart.load_inventory(inventory)

    failures: list[str] = []

    # Case 1: add_item retried 5x with same key. quantity should stay = original.
    cart_id = cart.create_cart()
    key = str(uuid.uuid4())
    responses = [cart.add_item(cart_id, "omega", 3, key) for _ in range(5)]
    items = cart.get_cart_items(cart_id)
    if items.get("omega") != 3:
        failures.append(
            f"add_item double-mutated: 5x retry of qty=3 produced cart qty={items.get('omega')}"
        )
    if not all(r == responses[0] for r in responses):
        failures.append(f"add_item returned drifting responses on retry: {responses}")

    # Case 2: distinct keys with same payload SHOULD accumulate (not idempotent across keys).
    cart2 = cart.create_cart()
    cart.add_item(cart2, "vitamin-c", 1, str(uuid.uuid4()))
    cart.add_item(cart2, "vitamin-c", 1, str(uuid.uuid4()))
    items2 = cart.get_cart_items(cart2)
    if items2.get("vitamin-c") != 2:
        failures.append(
            f"distinct keys must NOT dedupe; expected vitamin-c=2 got {items2.get('vitamin-c')}"
        )

    # Case 3: start_checkout retried with same key returns same order_id, never spawns 2 orders.
    cart3 = cart.create_cart()
    cart.add_item(cart3, "zinc-complex", 2, str(uuid.uuid4()))
    chk_key = str(uuid.uuid4())
    o1 = cart.start_checkout(cart3, chk_key)
    o2 = cart.start_checkout(cart3, chk_key)
    if o1["order_id"] != o2["order_id"]:
        failures.append(
            f"start_checkout retry produced different orders: {o1['order_id']} vs {o2['order_id']}"
        )
    if o1 != o2:
        failures.append(f"start_checkout retry response drifted: {o1} vs {o2}")

    # Reservation must not have been doubled (otherwise stock available would be off).
    avail = cart.get_stock_available()
    expected_held_zinc = 2  # one reservation, qty=2
    actual_held_zinc = inventory["zinc-complex"]["stock"] - avail["zinc-complex"]
    if actual_held_zinc != expected_held_zinc:
        failures.append(
            f"start_checkout retry doubled reservation: held={actual_held_zinc}, expected={expected_held_zinc}"
        )

    # Case 4: mock_payment_success retried with same key returns same response, stock decremented exactly once.
    pay_key = str(uuid.uuid4())
    payment_id = f"tx_{uuid.uuid4()}"
    p1 = cart.mock_payment_success(o1["order_id"], payment_id, pay_key)
    p2 = cart.mock_payment_success(o1["order_id"], payment_id, pay_key)
    if p1 != p2:
        failures.append(f"mock_payment_success retry response drifted: {p1} vs {p2}")
    stock_after = cart.get_stock_total()
    expected_stock = inventory["zinc-complex"]["stock"] - 2
    if stock_after["zinc-complex"] != expected_stock:
        failures.append(
            f"mock_payment_success retry caused double decrement: zinc-complex stock={stock_after['zinc-complex']}, expected {expected_stock}"
        )

    if failures:
        print("FAIL: idempotency violations:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
