"""Surge — concurrent buyers, multi-flow.

Negative-only. Exit 0 = no anomaly. Exit 1 = a failure mode triggered.

Each buyer JSON declares a `flow`:
  full          add -> checkout -> pay        (default)
  abandon_add   add only, never checkout       (cart_items written, no reservation, no decrement)
  stale_cart    full flow x2 with separate cart_ids back-to-back (simulates user buying again
                with stale localStorage cart_id from a previously-completed order)
  two_devices   full flow x2 in parallel under different cart_ids (same nominal person, two devices)

Math (per-buyer multiplier on `stock` impact):
  full=1   abandon_add=0   stale_cart=2   two_devices=2

Stock is dimensioned so that math says ALL flows must succeed. Any failure therefore
reveals a race, lost write, double-decrement, idempotency leak, FSM violation under
contention, or improper hold semantics across the new flows.
"""
import json
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from cart import Cart

HERE = Path(__file__).parent
MOCK = HERE / "mock"
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_surge_")) / "db.sqlite"

FLOW_MULTIPLIER = {"full": 1, "abandon_add": 0, "stale_cart": 2, "two_devices": 2}


def run_full(cart: Cart, purchases: list) -> None:
    cart_id = cart.create_cart()
    for item in purchases:
        cart.add_item(cart_id, item["sku"], item["quantity"], str(uuid.uuid4()))
    order = cart.start_checkout(cart_id, str(uuid.uuid4()))
    cart.mock_payment_success(order["order_id"], f"tx_{uuid.uuid4()}", str(uuid.uuid4()))


def run_abandon_add(cart: Cart, purchases: list) -> None:
    cart_id = cart.create_cart()
    for item in purchases:
        cart.add_item(cart_id, item["sku"], item["quantity"], str(uuid.uuid4()))
    # never call start_checkout: items sit in cart_items, no reservation, no order, no stock impact


def run_stale_cart(cart: Cart, purchases: list) -> None:
    # User completed a purchase, browser still holds completed cart_id, then they shop again.
    # Each pass uses its own fresh cart_id (which is what the JS would generate after seeing
    # the completed order). Two full flows back-to-back.
    run_full(cart, purchases)
    run_full(cart, purchases)


def run_two_devices(cart: Cart, purchases: list) -> None:
    # Same person checking out from desktop and phone concurrently.
    threads = [threading.Thread(target=run_full, args=(cart, purchases)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


FLOWS = {
    "full": run_full,
    "abandon_add": run_abandon_add,
    "stale_cart": run_stale_cart,
    "two_devices": run_two_devices,
}


def buyer_workflow(cart: Cart, buyer: dict, results: list, idx: int) -> None:
    flow_name = buyer.get("flow", "full")
    fn = FLOWS.get(flow_name)
    if fn is None:
        results[idx] = ("fail", f"unknown flow '{flow_name}'")
        return
    try:
        fn(cart, buyer["purchases"])
        results[idx] = ("ok", None)
    except Exception as e:
        results[idx] = ("fail", f"{type(e).__name__}: {e}")


def main() -> int:
    inventory = json.loads((MOCK / "inventory.json").read_text())
    cart = Cart(str(DB_PATH))
    cart.load_inventory(inventory)

    buyer_files = sorted((MOCK / "buyers").glob("*.json"))
    if not buyer_files:
        print("FAIL: no buyer files in mock/buyers/")
        return 1
    buyers = [json.loads(f.read_text()) for f in buyer_files]

    expected = {sku: data["stock"] for sku, data in inventory.items()}
    for buyer in buyers:
        flow = buyer.get("flow", "full")
        if flow not in FLOW_MULTIPLIER:
            print(f"FAIL: buyer {buyer['name']} declares unknown flow '{flow}'")
            return 1
        m = FLOW_MULTIPLIER[flow]
        for item in buyer["purchases"]:
            sku = item["sku"]
            qty = item["quantity"]
            if sku not in expected:
                print(f"FAIL: buyer {buyer['name']} references unknown sku '{sku}'")
                return 1
            expected[sku] -= qty * m
            if expected[sku] < 0:
                print(f"FAIL: test data inconsistent — total demand for '{sku}' exceeds initial stock")
                return 1

    results = [None] * len(buyers)
    threads = [
        threading.Thread(target=buyer_workflow, args=(cart, buyer, results, i))
        for i, buyer in enumerate(buyers)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    failed = [(buyers[i]["name"], err) for i, (status, err) in enumerate(results) if status != "ok"]
    if failed:
        print(f"FAIL: {len(failed)} buyer flow(s) raised:")
        for name, err in failed:
            print(f"  {name}: {err}")

    actual = cart.get_stock_total()
    diffs = {sku: (expected[sku], actual[sku]) for sku in expected if expected[sku] != actual[sku]}
    if diffs:
        print(f"FAIL: stock mismatch after {len(buyers)}-buyer surge:")
        for sku, (exp, act) in sorted(diffs.items()):
            print(f"  {sku}: expected {exp}, actual {act} (drift {act - exp:+d})")

    avail = cart.get_stock_available()
    leaked = {sku: avail[sku] for sku in avail if avail[sku] != actual[sku]}
    if leaked:
        print("FAIL: reservations leaked after all checkouts (available != stock):")
        for sku, a in sorted(leaked.items()):
            print(f"  {sku}: stock={actual[sku]} available={a}")

    # abandon_add semantics: cart_items must exist for those buyers but never appear in
    # any reservation or order. Verify by checking no order exists for an empty/abandoned cart.
    # (Indirect: if abandon_add silently triggered checkout, expected math would be off and
    # we'd already have caught it.)

    return 1 if (failed or diffs or leaked) else 0


if __name__ == "__main__":
    sys.exit(main())
