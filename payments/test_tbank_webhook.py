"""T-Bank webhook handler — fail-resistance under synthetic delivery.

Negative-only. Exit 0 = no anomaly. Exit 1 = a failure mode triggered.

Covers:
  - valid signature + CONFIRMED -> order captured, stock decremented exactly once
  - valid signature + REJECTED  -> order cancelled, reservations released, stock untouched
  - invalid signature           -> silently dropped, no state change, response is 200 OK
  - replay (same PaymentId+Status) -> idempotent, no double effect
  - intermediate status (AUTHORIZING) -> recorded only, no FSM change
  - missing Token / malformed body -> silently dropped, no state change
  - reservation expired before CONFIRMED arrives -> order cancelled, stock NOT decremented
"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import tbank
from api import make_handler
from cart import Cart

PORT = 18101
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_tbank_")) / "db.sqlite"
HERE = Path(__file__).parent

TERMINAL_KEY = "TestTerminal"
PASSWORD = "test-password"


class FakeClock:
    def __init__(self, t0: int):
        self.t = t0

    def __call__(self) -> int:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += seconds


def post_webhook(body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/tbank/webhook",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def make_signed_notification(order_id: str, payment_id: int, status: str, amount: int) -> dict:
    n = {
        "TerminalKey": TERMINAL_KEY,
        "OrderId": order_id,
        "Success": status == "CONFIRMED",
        "Status": status,
        "PaymentId": payment_id,
        "ErrorCode": "0" if status == "CONFIRMED" else "100",
        "Amount": amount,
        "Pan": "430000******0777",
        "ExpDate": "1130",
    }
    return tbank.sign_request(n, TERMINAL_KEY, PASSWORD)


def setup_order(cart: Cart, sku: str, qty: int) -> tuple[str, int]:
    cart_id = cart.create_cart()
    cart.add_item(cart_id, sku, qty, str(uuid.uuid4()))
    order = cart.start_checkout(cart_id, str(uuid.uuid4()))
    return order["order_id"], order["total_kopecks"]


def main() -> int:
    inv = json.loads((HERE / "mock" / "inventory.json").read_text())
    clock = FakeClock(1_700_000_000)
    cart = Cart(str(DB_PATH), now=clock)
    cart.load_inventory(inv)

    server = ThreadingHTTPServer(
        ("127.0.0.1", PORT),
        make_handler(cart, "*", tbank_terminal_key=TERMINAL_KEY, tbank_password=PASSWORD),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures: list[str] = []
    try:
        # 1) Valid CONFIRMED -> order captured, stock decremented.
        oid1, amt1 = setup_order(cart, "omega", 2)
        stock_before = cart.get_stock_total()["omega"]
        notif = make_signed_notification(oid1, 1001, "CONFIRMED", amt1)
        status, body = post_webhook(notif)
        if status != 200 or body != "OK":
            failures.append(f"CONFIRMED webhook should return 200 'OK'. got {status} {body!r}")
        if cart.get_order_state(oid1) != "payment_captured":
            failures.append(f"order {oid1} expected payment_captured got {cart.get_order_state(oid1)}")
        if cart.get_stock_total()["omega"] != stock_before - 2:
            failures.append(f"omega stock not decremented after CONFIRMED")

        # 2) Replay same notification -> idempotent.
        post_webhook(notif)
        if cart.get_stock_total()["omega"] != stock_before - 2:
            failures.append(f"replay caused double decrement")

        # 3) Invalid signature -> silent drop, 200 OK, no state change.
        oid2, amt2 = setup_order(cart, "vitamin-c", 1)
        before_state = cart.get_order_state(oid2)
        bad = make_signed_notification(oid2, 1002, "CONFIRMED", amt2)
        bad["Token"] = "0" * 64  # garbage signature
        status, body = post_webhook(bad)
        if status != 200:
            failures.append(f"bad-signature webhook: status should be 200 got {status}")
        if cart.get_order_state(oid2) != before_state:
            failures.append(f"bad-signature webhook mutated order state")

        # 4) Tampered Amount -> silent drop.
        oid3, amt3 = setup_order(cart, "zinc-complex", 3)
        tampered = make_signed_notification(oid3, 1003, "CONFIRMED", amt3 + 1000000)
        # signature was computed over the inflated amount; resign with WRONG password to make it look tampered.
        # Actually a correctly-signed inflated amount is valid (T-Bank wouldn't send that, but our verifier
        # accepts whatever T-Bank-signed payload arrives). True tamper test: sign correctly, then mutate AFTER signing.
        legit = make_signed_notification(oid3, 1003, "CONFIRMED", amt3)
        legit["Amount"] = amt3 + 1000000
        status, _ = post_webhook(legit)
        if cart.get_order_state(oid3) != "inventory_reserved":
            failures.append(f"tampered Amount accepted; order advanced to {cart.get_order_state(oid3)}")

        # 5) REJECTED -> order cancelled, reservations released, stock unchanged.
        oid4, amt4 = setup_order(cart, "magnesium-pro", 5)
        stock_b4 = cart.get_stock_total()["magnesium-pro"]
        avail_b4 = cart.get_stock_available()["magnesium-pro"]
        if avail_b4 != stock_b4 - 5:
            failures.append(f"pre-reject sanity: available should be stock-5, got stock={stock_b4} avail={avail_b4}")
        notif_rej = make_signed_notification(oid4, 1004, "REJECTED", amt4)
        post_webhook(notif_rej)
        if cart.get_order_state(oid4) != "cancelled":
            failures.append(f"REJECTED should cancel order; got {cart.get_order_state(oid4)}")
        if cart.get_stock_total()["magnesium-pro"] != stock_b4:
            failures.append(f"REJECTED must NOT decrement stock; before={stock_b4} after={cart.get_stock_total()['magnesium-pro']}")
        if cart.get_stock_available()["magnesium-pro"] != stock_b4:
            failures.append(f"REJECTED must release reservation; available={cart.get_stock_available()['magnesium-pro']} stock={stock_b4}")

        # 6) Replay REJECTED -> idempotent.
        post_webhook(notif_rej)
        if cart.get_order_state(oid4) != "cancelled":
            failures.append("REJECTED replay flipped state")

        # 7) Intermediate status (AUTHORIZING) -> no FSM change.
        oid5, amt5 = setup_order(cart, "collagen-bovine", 1)
        notif_int = make_signed_notification(oid5, 1005, "AUTHORIZING", amt5)
        post_webhook(notif_int)
        if cart.get_order_state(oid5) != "inventory_reserved":
            failures.append(f"AUTHORIZING should not change state; got {cart.get_order_state(oid5)}")
        # Subsequent CONFIRMED still works.
        notif_ok = make_signed_notification(oid5, 1005, "CONFIRMED", amt5)
        post_webhook(notif_ok)
        if cart.get_order_state(oid5) != "payment_captured":
            failures.append(f"after AUTHORIZING then CONFIRMED expected payment_captured got {cart.get_order_state(oid5)}")

        # 8) Malformed body (no Token) -> silent drop, 200.
        status, _ = post_webhook({"OrderId": "whatever", "Status": "CONFIRMED"})
        if status != 200:
            failures.append(f"malformed body should return 200, got {status}")

        # 9) CONFIRMED arriving AFTER reservation expired -> order cancelled, stock NOT decremented.
        oid6, amt6 = setup_order(cart, "bone-longevity", 4)
        stock_b6 = cart.get_stock_total()["bone-longevity"]
        clock.advance(20 * 60)  # past 15-min TTL
        notif_late = make_signed_notification(oid6, 1006, "CONFIRMED", amt6)
        post_webhook(notif_late)
        if cart.get_order_state(oid6) != "cancelled":
            failures.append(f"late CONFIRMED on expired reservation should cancel order; got {cart.get_order_state(oid6)}")
        if cart.get_stock_total()["bone-longevity"] != stock_b6:
            failures.append(f"late CONFIRMED must NOT decrement stock; before={stock_b6} after={cart.get_stock_total()['bone-longevity']}")

    finally:
        server.shutdown()

    if failures:
        print("FAIL: tbank webhook handler:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
