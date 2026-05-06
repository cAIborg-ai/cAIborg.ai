"""HTTP API surface for the cart.

Single consumer right now: browser "Add to Cart" button on product pages.
Thin JSON-over-HTTP wrapper around cart.py — all fail-resistance lives in cart.py.

Endpoints:
  POST /api/cart/add   body: {cart_id, sku, quantity, idempotency_key}
                       -> 200 {cart_id, items, total_items, total_kopecks}
                       -> 400 {error} on missing fields / bad JSON
                       -> 409 {error} on unknown sku / cart-state violation

  GET  /api/cart?cart_id=...
                       -> 200 {cart_id, items, total_items, total_kopecks}
                       -> 400 {error} if cart_id missing

  POST /api/cart/checkout body: {cart_id, idempotency_key}
                          -> 200 {order_id, total_kopecks, expires_at}
                          -> 409 {error}

CORS: open while in dev; lock to corp domain in production via env CART_ALLOWED_ORIGIN.
"""
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import tbank
from cart import Cart, StateError


def make_handler(
    cart: Cart,
    allowed_origin: str = "*",
    tbank_terminal_key: str = "",
    tbank_password: str = "",
    tbank_init_fn=None,  # (terminal_key, password, body) -> response_dict; default: real HTTP
    notification_url: str = "",
    success_url: str = "",
    fail_url: str = "",
):
    if tbank_init_fn is None:
        tbank_init_fn = lambda tk, pw, body: tbank.call_init(terminal_key=tk, password=pw, body=body)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", "0"))
            if n == 0:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode())
            except Exception:
                return None

        def do_OPTIONS(self):
            self._json(204, {})

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._read_json()
            if body is None:
                return self._json(400, {"error": "invalid JSON"})

            if path == "/api/cart/add":
                cart_id = body.get("cart_id") or str(uuid.uuid4())
                sku = body.get("sku")
                qty = body.get("quantity")
                key = body.get("idempotency_key")
                if not sku or qty is None or not key:
                    return self._json(400, {"error": "sku, quantity, idempotency_key required"})
                try:
                    cart.ensure_cart(cart_id)
                    cart.add_item(cart_id, sku, int(qty), key)
                except StateError as e:
                    return self._json(409, {"error": str(e)})
                except (TypeError, ValueError) as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, cart.get_cart_summary(cart_id))

            if path == "/api/cart/checkout":
                cart_id = body.get("cart_id")
                key = body.get("idempotency_key")
                if not cart_id or not key:
                    return self._json(400, {"error": "cart_id, idempotency_key required"})
                try:
                    order = cart.start_checkout(cart_id, key)
                except StateError as e:
                    return self._json(409, {"error": str(e)})
                # If T-Bank credentials configured, call Init and return PaymentURL.
                # Otherwise return order info only (caller can pay later or dev runs without T-Bank).
                if tbank_terminal_key and tbank_password:
                    init_body = tbank.build_init_body(
                        order_id=order["order_id"],
                        amount_kopecks=order["total_kopecks"],
                        description=f"Universe Vitamins order {order['order_id'][:8]}",
                        notification_url=notification_url,
                        success_url=success_url,
                        fail_url=fail_url,
                    )
                    try:
                        tbank_resp = tbank_init_fn(tbank_terminal_key, tbank_password, init_body)
                    except Exception as e:
                        return self._json(502, {"error": f"tbank Init failed: {type(e).__name__}: {e}"})
                    if not tbank_resp.get("Success"):
                        return self._json(502, {"error": "tbank Init returned Success=false", "tbank": tbank_resp})
                    cart.record_payment_init(
                        order["order_id"],
                        str(tbank_resp.get("PaymentId", "")),
                        f"init-{order['order_id']}",
                    )
                    return self._json(200, {
                        "order_id": order["order_id"],
                        "total_kopecks": order["total_kopecks"],
                        "payment_url": tbank_resp.get("PaymentURL"),
                        "tbank_payment_id": tbank_resp.get("PaymentId"),
                    })
                return self._json(200, order)

            if path == "/api/tbank/webhook":
                # Per design #5: silent drop on invalid signature; always 200 when valid.
                if not isinstance(body, dict) or "Token" not in body:
                    # Body shape wrong. Drop silently to mirror T-Bank tolerance for retries.
                    return self._json(200, {"ok": True})
                if not tbank_password:
                    # No credentials configured -> drop silently. No state mutation.
                    return self._json(200, {"ok": True})
                if not tbank.verify_notification(body, tbank_password):
                    # Bad signature: log to stderr, return 200 OK (no leak about which key wrong).
                    print(f"tbank webhook: signature mismatch order={body.get('OrderId')} payment={body.get('PaymentId')}", file=sys.stderr)
                    return self._json(200, {"ok": True})
                try:
                    cart.apply_webhook(body)
                except StateError as e:
                    # FSM violation. Log; still return 200 so T-Bank does not retry storm.
                    print(f"tbank webhook: state error: {e}", file=sys.stderr)
                # T-Bank expects literal "OK" body for successful ack:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.end_headers()
                self.wfile.write(b"OK")
                return

            return self._json(404, {"error": "not found"})

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/cart":
                qs = parse_qs(u.query)
                cart_id = (qs.get("cart_id") or [None])[0]
                if not cart_id:
                    return self._json(400, {"error": "cart_id required"})
                return self._json(200, cart.get_cart_summary(cart_id))
            if u.path.startswith("/api/order/"):
                order_id = u.path[len("/api/order/"):]
                state = cart.get_order_state(order_id)
                if state is None:
                    return self._json(404, {"error": "order not found"})
                return self._json(200, {"order_id": order_id, "state": state})
            return self._json(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            return  # quiet

    return Handler


def serve(host: str, port: int, cart: Cart, allowed_origin: str = "*", **handler_kwargs) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(cart, allowed_origin, **handler_kwargs))
    server.serve_forever()


def main() -> None:
    db_path = os.environ.get("CART_DB", "/tmp/cart_dev.db")
    port = int(os.environ.get("PORT", "8080"))
    origin = os.environ.get("CART_ALLOWED_ORIGIN", "*")
    inv_file = os.path.join(os.path.dirname(__file__), "mock", "inventory.json")
    with open(inv_file) as f:
        inv = json.load(f)
    c = Cart(db_path)
    c.load_inventory(inv)
    print(f"cart api listening on :{port}, db={db_path}, origin={origin}", file=sys.stderr)
    serve(
        "0.0.0.0", port, c, origin,
        tbank_terminal_key=os.environ.get("TBANK_TERMINAL_KEY", ""),
        tbank_password=os.environ.get("TBANK_PASSWORD", ""),
        notification_url=os.environ.get("TBANK_NOTIFICATION_URL", ""),
        success_url=os.environ.get("TBANK_SUCCESS_URL", ""),
        fail_url=os.environ.get("TBANK_FAIL_URL", ""),
    )


if __name__ == "__main__":
    main()
