"""FSM — invalid transitions and invalid inputs MUST be rejected.

Negative-only. Exit 0 = no anomaly. Exit 1 = a forbidden transition or
invalid input was accepted.
"""
import json
import sys
import tempfile
import uuid
from pathlib import Path

from cart import Cart, StateError

HERE = Path(__file__).parent
MOCK = HERE / "mock"
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_fsm_")) / "db.sqlite"


def expect_error(label: str, fn) -> str | None:
    try:
        fn()
    except StateError:
        return None
    except Exception as e:
        return None if isinstance(e, StateError) else f"{label}: wrong exception type {type(e).__name__}: {e}"
    return f"{label}: should have raised StateError, did not"


def main() -> int:
    inventory = json.loads((MOCK / "inventory.json").read_text())
    cart = Cart(str(DB_PATH))
    cart.load_inventory(inventory)

    failures: list[str] = []

    # 1) add_item with non-existent cart
    e = expect_error("add to non-existent cart", lambda: cart.add_item("no-such-cart", "omega", 1, str(uuid.uuid4())))
    if e:
        failures.append(e)

    # 2) add_item with non-existent sku
    cart_id = cart.create_cart()
    e = expect_error("add unknown sku", lambda: cart.add_item(cart_id, "nope-sku", 1, str(uuid.uuid4())))
    if e:
        failures.append(e)

    # 3) add_item with non-positive quantity
    e = expect_error("add quantity=0", lambda: cart.add_item(cart_id, "omega", 0, str(uuid.uuid4())))
    if e:
        failures.append(e)
    e = expect_error("add quantity=-1", lambda: cart.add_item(cart_id, "omega", -1, str(uuid.uuid4())))
    if e:
        failures.append(e)

    # 4) start_checkout on empty cart
    empty = cart.create_cart()
    e = expect_error("checkout empty cart", lambda: cart.start_checkout(empty, str(uuid.uuid4())))
    if e:
        failures.append(e)

    # 5) start_checkout exceeding available stock
    huge = cart.create_cart()
    cart.add_item(huge, "omega", inventory["omega"]["stock"] + 1, str(uuid.uuid4()))
    e = expect_error("checkout exceeding stock", lambda: cart.start_checkout(huge, str(uuid.uuid4())))
    if e:
        failures.append(e)

    # 6) double-checkout while order active
    c2 = cart.create_cart()
    cart.add_item(c2, "vitamin-c", 1, str(uuid.uuid4()))
    cart.start_checkout(c2, str(uuid.uuid4()))
    e = expect_error(
        "double checkout same cart (different keys)",
        lambda: cart.start_checkout(c2, str(uuid.uuid4())),
    )
    if e:
        failures.append(e)

    # 7) add to cart after checkout started
    e = expect_error(
        "add after checkout",
        lambda: cart.add_item(c2, "vitamin-c", 1, str(uuid.uuid4())),
    )
    if e:
        failures.append(e)

    # 8) mock_payment_success on non-existent order
    e = expect_error(
        "pay non-existent order",
        lambda: cart.mock_payment_success("no-such-order", "tx", str(uuid.uuid4())),
    )
    if e:
        failures.append(e)

    # 9) mock_payment_success twice on same order with different keys (already captured -> invalid transition)
    c3 = cart.create_cart()
    cart.add_item(c3, "magnesium-pro", 1, str(uuid.uuid4()))
    o3 = cart.start_checkout(c3, str(uuid.uuid4()))
    cart.mock_payment_success(o3["order_id"], "tx_first", str(uuid.uuid4()))
    e = expect_error(
        "double-capture (different keys)",
        lambda: cart.mock_payment_success(o3["order_id"], "tx_second", str(uuid.uuid4())),
    )
    if e:
        failures.append(e)

    # 10) Event log integrity: order o3 must have all 4 expected events in order.
    events = [t for t, _, _ in cart.get_order_events(o3["order_id"])]
    expected = ["cart_validated", "inventory_reserved", "payment_authorized", "payment_captured"]
    if events != expected:
        failures.append(f"order event log wrong sequence: got {events}, expected {expected}")

    # 11) Outbox MUST contain entries for order_created + payment_captured (created in same tx as state change).
    outbox = cart.get_outbox_pending()
    types = {ev_type for _, ev_type, _ in outbox}
    for required in ("order_created", "payment_captured"):
        if required not in types:
            failures.append(f"outbox missing required event '{required}': {types}")

    if failures:
        print("FAIL: state-machine / invariant violations:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
