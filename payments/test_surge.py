"""Surge — concurrent buyers full flow.

Negative-only. Exit 0 = no anomaly. Exit 1 = a failure mode triggered.

Each buyer runs full flow:
    create_cart -> add_item* -> start_checkout -> mock_payment_success.

Stock is dimensioned so that math says ALL flows must succeed. Any failure
therefore reveals a race, lost write, double-decrement, idempotency leak,
or FSM violation under contention.
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


def buyer_workflow(cart: Cart, buyer: dict, results: list, idx: int) -> None:
    try:
        cart_id = cart.create_cart()
        for item in buyer["purchases"]:
            cart.add_item(cart_id, item["sku"], item["quantity"], str(uuid.uuid4()))
        order = cart.start_checkout(cart_id, str(uuid.uuid4()))
        cart.mock_payment_success(order["order_id"], f"tx_{uuid.uuid4()}", str(uuid.uuid4()))
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
        for item in buyer["purchases"]:
            sku = item["sku"]
            qty = item["quantity"]
            if sku not in expected:
                print(f"FAIL: buyer {buyer['name']} references unknown sku '{sku}'")
                return 1
            expected[sku] -= qty
            if expected[sku] < 0:
                print(
                    f"FAIL: test data inconsistent — total demand for '{sku}' exceeds initial stock"
                )
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
        print(f"FAIL: {len(failed)} buyer flow(s) raised despite sufficient stock:")
        for name, err in failed:
            print(f"  {name}: {err}")

    actual = cart.get_stock_total()
    diffs = {
        sku: (expected[sku], actual[sku])
        for sku in expected
        if expected[sku] != actual[sku]
    }
    if diffs:
        print(f"FAIL: stock mismatch after {len(buyers)}-buyer surge:")
        for sku, (exp, act) in sorted(diffs.items()):
            print(f"  {sku}: expected {exp}, actual {act} (drift {act - exp:+d})")

    avail = cart.get_stock_available()
    leaked_holds = {sku: avail[sku] for sku in avail if avail[sku] != actual[sku]}
    if leaked_holds:
        print("FAIL: reservations leaked after all checkouts (available != stock):")
        for sku, a in sorted(leaked_holds.items()):
            print(f"  {sku}: stock={actual[sku]} available={a}")

    return 1 if (failed or diffs or leaked_holds) else 0


if __name__ == "__main__":
    sys.exit(main())
