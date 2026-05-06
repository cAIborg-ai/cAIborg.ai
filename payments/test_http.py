"""HTTP-layer fail-resistance.

Negative-only. Verifies that fail-modes proven in cart.py-level tests still hold
when invoked through the JSON-over-HTTP boundary:
  - Idempotency key replay = single mutation
  - Concurrent adds across many sockets = correct totals
  - Bad input rejected with 4xx, no mutation
  - Unknown sku rejected with 409, no mutation

Server runs in-process on a private port using a tempdir DB.
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

from api import make_handler
from cart import Cart

PORT = 18099
DB_PATH = Path(tempfile.mkdtemp(prefix="cart_http_")) / "db.sqlite"
HERE = Path(__file__).parent


def post(path: str, body: dict):
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get(path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> int:
    inv = json.loads((HERE / "mock" / "inventory.json").read_text())
    c = Cart(str(DB_PATH))
    c.load_inventory(inv)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), make_handler(c))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    failures: list[str] = []
    try:
        cart_id = str(uuid.uuid4())

        # 1) Idempotency: same key replayed 5x = single mutation, identical bodies.
        key = str(uuid.uuid4())
        responses = []
        for _ in range(5):
            s, r = post("/api/cart/add", {"cart_id": cart_id, "sku": "omega", "quantity": 1, "idempotency_key": key})
            responses.append((s, r))
        if not all(r == responses[0] for r in responses):
            failures.append(f"idempotency replay drifted across responses: {responses}")
        s, r = get(f"/api/cart?cart_id={cart_id}")
        if r["total_items"] != 1:
            failures.append(f"5x retry of qty=1 with same key produced cart total_items={r['total_items']} (expected 1)")

        # 2) Distinct keys still accumulate.
        s, r = post("/api/cart/add", {"cart_id": cart_id, "sku": "omega", "quantity": 1, "idempotency_key": str(uuid.uuid4())})
        if r["total_items"] != 2:
            failures.append(f"after fresh add total_items should be 2, got {r['total_items']}")

        # 3) Unknown SKU = 409, no mutation.
        s, r = post("/api/cart/add", {"cart_id": cart_id, "sku": "no-such-sku", "quantity": 1, "idempotency_key": str(uuid.uuid4())})
        if s != 409:
            failures.append(f"unknown sku should -> 409, got {s} {r}")
        s, r = get(f"/api/cart?cart_id={cart_id}")
        if r["total_items"] != 2:
            failures.append(f"after rejected add total_items must remain 2, got {r['total_items']}")

        # 4) Missing field = 400.
        s, r = post("/api/cart/add", {"cart_id": cart_id, "sku": "omega"})
        if s != 400:
            failures.append(f"missing fields should -> 400, got {s} {r}")

        # 5) Concurrent adds across many sockets — no lost writes.
        c2 = str(uuid.uuid4())
        N = 25
        threads = []
        results = [None] * N
        def add_one(idx):
            results[idx] = post("/api/cart/add", {
                "cart_id": c2, "sku": "vitamin-c", "quantity": 1, "idempotency_key": str(uuid.uuid4())
            })
        for i in range(N):
            t = threading.Thread(target=add_one, args=(i,))
            threads.append(t); t.start()
        for t in threads:
            t.join()
        bad = [(i, r) for i, r in enumerate(results) if r[0] != 200]
        if bad:
            failures.append(f"concurrent adds: {len(bad)} non-200 responses: {bad[:3]}")
        s, r = get(f"/api/cart?cart_id={c2}")
        if r["total_items"] != N:
            failures.append(f"concurrent N={N} adds produced total_items={r['total_items']} (expected {N})")

        # 6) HTTP checkout end-to-end.
        s, r = post("/api/cart/checkout", {"cart_id": c2, "idempotency_key": str(uuid.uuid4())})
        if s != 200 or "order_id" not in r:
            failures.append(f"checkout should succeed for valid cart, got {s} {r}")

    finally:
        server.shutdown()

    if failures:
        print("FAIL: HTTP layer:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
