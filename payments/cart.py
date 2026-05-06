"""Cart + order core.

Adheres to fail-proof-design.md from line 1:
- explicit FSM with persisted state, transitions enforced
- idempotency keys on every state mutation
- inventory reservations with TTL (no decrement at add-to-cart)
- immutable append-only event log per order
- outbox table for downstream side effects
- Decimal money stored as integer kopecks
- SQLite WAL + synchronous=NORMAL + foreign_keys=ON + busy_timeout

Clock is injectable so time-based fail modes (TTL expiry) are tested without
real waits.

Webhook handlers, signature verification, processed_webhooks table, and
reconciliation are intentionally NOT in this file: their consumer (T-Bank
sandbox integration) does not yet exist. Per the type-driven rule, they will
be added when that consumer arrives.
"""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager


# Order FSM. None == before creation.
VALID_TRANSITIONS = {
    None: {"cart_validated"},
    "cart_validated": {"inventory_reserved", "cancelled"},
    "inventory_reserved": {"payment_authorized", "cancelled"},
    "payment_authorized": {"payment_captured", "cancelled"},
    "payment_captured": {"order_fulfilled", "cancelled"},
    "order_fulfilled": {"order_delivered", "cancelled"},
    "order_delivered": set(),
    "cancelled": set(),
}

RESERVATION_TTL_SECONDS = 15 * 60  # 15 min, per design #3


class StateError(Exception):
    """Raised on invalid FSM transition or invalid input that breaks invariants."""


class Cart:
    def __init__(self, db_path: str, now=None):
        self.db_path = db_path
        self._now = now or (lambda: int(time.time()))
        with self._conn() as db:
            self._init_schema(db)

    def _init_schema(self, db) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                sku TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                price_kopecks INTEGER NOT NULL CHECK(price_kopecks >= 0),
                stock INTEGER NOT NULL CHECK(stock >= 0),
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS carts (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cart_items (
                cart_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                price_kopecks INTEGER NOT NULL,
                PRIMARY KEY(cart_id, sku),
                FOREIGN KEY(cart_id) REFERENCES carts(id),
                FOREIGN KEY(sku) REFERENCES products(sku)
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                cart_id TEXT NOT NULL,
                total_kopecks INTEGER NOT NULL CHECK(total_kopecks >= 0),
                state TEXT NOT NULL,
                tbank_payment_id TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(cart_id) REFERENCES carts(id)
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventory_reservations (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity > 0),
                expires_at INTEGER NOT NULL,
                converted INTEGER NOT NULL DEFAULT 0,
                cancelled INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(order_id) REFERENCES orders(id),
                FOREIGN KEY(sku) REFERENCES products(sku)
            );
            CREATE INDEX IF NOT EXISTS idx_reservations_active
                ON inventory_reservations(sku) WHERE converted=0 AND cancelled=0;
            CREATE TABLE IF NOT EXISTS order_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id, id);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                sent_at INTEGER,
                retries INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            );
            """
        )

    @contextmanager
    def _conn(self):
        # autocommit; explicit BEGIN IMMEDIATE for write txns serializes them.
        db = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            db.close()

    # ---------- helpers ----------
    def _idem_get(self, db, key):
        row = db.execute("SELECT response_json FROM idempotency WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _idem_put(self, db, key, response, now):
        db.execute(
            "INSERT INTO idempotency (key, response_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(response), now),
        )

    def _enforce(self, current, target):
        if target not in VALID_TRANSITIONS.get(current, set()):
            raise StateError(f"invalid transition: {current} -> {target}")

    def _append_event(self, db, order_id, event_type, payload, now):
        db.execute(
            "INSERT INTO order_events (order_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (order_id, event_type, json.dumps(payload), now),
        )

    def _enqueue_outbox(self, db, event_type, payload, now):
        db.execute(
            "INSERT INTO outbox (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (event_type, json.dumps(payload), now),
        )

    def _available(self, db, sku, now):
        row = db.execute("SELECT stock FROM products WHERE sku = ? AND active = 1", (sku,)).fetchone()
        if row is None:
            return 0
        held = db.execute(
            """SELECT COALESCE(SUM(quantity), 0) FROM inventory_reservations
               WHERE sku = ? AND converted=0 AND cancelled=0 AND expires_at > ?""",
            (sku, now),
        ).fetchone()[0]
        return row[0] - held

    # ---------- inventory bootstrap ----------
    def load_inventory(self, items: dict) -> None:
        """items: {sku: {price_kopecks, stock}}."""
        now = self._now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                for sku, data in items.items():
                    db.execute(
                        "INSERT OR REPLACE INTO products (sku, name, price_kopecks, stock, active) VALUES (?, ?, ?, ?, 1)",
                        (sku, sku, int(data["price_kopecks"]), int(data["stock"])),
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get_stock_total(self) -> dict:
        with self._conn() as db:
            return dict(db.execute("SELECT sku, stock FROM products").fetchall())

    def get_stock_available(self) -> dict:
        now = self._now()
        with self._conn() as db:
            stock = dict(db.execute("SELECT sku, stock FROM products").fetchall())
            holds = db.execute(
                """SELECT sku, COALESCE(SUM(quantity), 0) FROM inventory_reservations
                   WHERE converted=0 AND cancelled=0 AND expires_at > ?
                   GROUP BY sku""",
                (now,),
            ).fetchall()
            for sku, qty in holds:
                stock[sku] = stock[sku] - qty
            return stock

    def get_cart_items(self, cart_id: str) -> dict:
        with self._conn() as db:
            rows = db.execute(
                "SELECT sku, quantity FROM cart_items WHERE cart_id = ?", (cart_id,)
            ).fetchall()
            return dict(rows)

    def get_order_state(self, order_id: str):
        with self._conn() as db:
            row = db.execute("SELECT state FROM orders WHERE id = ?", (order_id,)).fetchone()
            return row[0] if row else None

    def get_order_events(self, order_id: str) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT event_type, payload_json, created_at FROM order_events WHERE order_id = ? ORDER BY id",
                (order_id,),
            ).fetchall()
            return [(t, json.loads(p), c) for t, p, c in rows]

    def get_outbox_pending(self) -> list:
        with self._conn() as db:
            rows = db.execute(
                "SELECT id, event_type, payload_json FROM outbox WHERE sent_at IS NULL ORDER BY id"
            ).fetchall()
            return [(rid, et, json.loads(pj)) for rid, et, pj in rows]

    # ---------- cart operations ----------
    def create_cart(self) -> str:
        cart_id = str(uuid.uuid4())
        now = self._now()
        with self._conn() as db:
            db.execute(
                "INSERT INTO carts (id, created_at, updated_at) VALUES (?, ?, ?)",
                (cart_id, now, now),
            )
        return cart_id

    def ensure_cart(self, cart_id: str) -> None:
        """Idempotent cart row. Used when a client supplies its own cart_id (browser localStorage)."""
        now = self._now()
        with self._conn() as db:
            db.execute(
                "INSERT OR IGNORE INTO carts (id, created_at, updated_at) VALUES (?, ?, ?)",
                (cart_id, now, now),
            )

    def get_cart_summary(self, cart_id: str) -> dict:
        with self._conn() as db:
            rows = db.execute(
                "SELECT sku, quantity, price_kopecks FROM cart_items WHERE cart_id = ?",
                (cart_id,),
            ).fetchall()
        items = [{"sku": s, "quantity": q, "price_kopecks": p} for s, q, p in rows]
        total_kopecks = sum(i["quantity"] * i["price_kopecks"] for i in items)
        total_items = sum(i["quantity"] for i in items)
        return {
            "cart_id": cart_id,
            "items": items,
            "total_kopecks": total_kopecks,
            "total_items": total_items,
        }

    def add_item(self, cart_id: str, sku: str, quantity: int, idempotency_key: str) -> dict:
        if quantity <= 0:
            raise StateError(f"quantity must be > 0, got {quantity}")
        now = self._now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cached = self._idem_get(db, idempotency_key)
                if cached is not None:
                    db.execute("ROLLBACK")
                    return cached
                if db.execute("SELECT 1 FROM carts WHERE id = ?", (cart_id,)).fetchone() is None:
                    raise StateError(f"cart {cart_id} does not exist")
                prod = db.execute(
                    "SELECT price_kopecks FROM products WHERE sku = ? AND active = 1", (sku,)
                ).fetchone()
                if prod is None:
                    raise StateError(f"unknown sku '{sku}'")
                price = prod[0]
                # Disallow add after an order exists for this cart (cart frozen at checkout).
                ord_row = db.execute(
                    "SELECT state FROM orders WHERE cart_id = ? ORDER BY created_at DESC LIMIT 1",
                    (cart_id,),
                ).fetchone()
                if ord_row is not None and ord_row[0] != "cancelled":
                    raise StateError(
                        f"cannot add to cart {cart_id}: active order exists in state {ord_row[0]}"
                    )
                db.execute(
                    """INSERT INTO cart_items (cart_id, sku, quantity, price_kopecks)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(cart_id, sku) DO UPDATE SET quantity = quantity + excluded.quantity""",
                    (cart_id, sku, quantity, price),
                )
                db.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (now, cart_id))
                response = {"status": "ok", "cart_id": cart_id, "sku": sku, "quantity_added": quantity}
                self._idem_put(db, idempotency_key, response, now)
                db.execute("COMMIT")
                return response
            except Exception:
                db.execute("ROLLBACK")
                raise

    def start_checkout(self, cart_id: str, idempotency_key: str) -> dict:
        """cart_open -> cart_validated -> inventory_reserved (single tx).

        Creates order, reserves inventory with TTL, appends events, enqueues outbox row.
        """
        now = self._now()
        expires_at = now + RESERVATION_TTL_SECONDS
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cached = self._idem_get(db, idempotency_key)
                if cached is not None:
                    db.execute("ROLLBACK")
                    return cached
                if db.execute("SELECT 1 FROM carts WHERE id = ?", (cart_id,)).fetchone() is None:
                    raise StateError(f"cart {cart_id} does not exist")
                # Block double-checkout unless prior order was cancelled.
                ord_row = db.execute(
                    "SELECT state FROM orders WHERE cart_id = ? ORDER BY created_at DESC LIMIT 1",
                    (cart_id,),
                ).fetchone()
                if ord_row is not None and ord_row[0] != "cancelled":
                    raise StateError(
                        f"cart {cart_id} already has active order in state {ord_row[0]}"
                    )
                items = db.execute(
                    "SELECT sku, quantity, price_kopecks FROM cart_items WHERE cart_id = ?",
                    (cart_id,),
                ).fetchall()
                if not items:
                    raise StateError(f"cart {cart_id} is empty")
                for sku, qty, _ in items:
                    avail = self._available(db, sku, now)
                    if avail < qty:
                        raise StateError(
                            f"insufficient stock for {sku}: requested {qty}, available {avail}"
                        )
                order_id = str(uuid.uuid4())
                total = sum(qty * price for _, qty, price in items)
                self._enforce(None, "cart_validated")
                db.execute(
                    "INSERT INTO orders (id, cart_id, total_kopecks, state, created_at) VALUES (?, ?, ?, ?, ?)",
                    (order_id, cart_id, total, "cart_validated", now),
                )
                self._append_event(
                    db, order_id, "cart_validated",
                    {"cart_id": cart_id, "total_kopecks": total, "items": [{"sku": s, "qty": q, "price_kopecks": p} for s, q, p in items]},
                    now,
                )
                reservation_ids = []
                for sku, qty, _ in items:
                    rid = str(uuid.uuid4())
                    db.execute(
                        "INSERT INTO inventory_reservations (id, order_id, sku, quantity, expires_at) VALUES (?, ?, ?, ?, ?)",
                        (rid, order_id, sku, qty, expires_at),
                    )
                    reservation_ids.append(rid)
                self._enforce("cart_validated", "inventory_reserved")
                db.execute("UPDATE orders SET state = 'inventory_reserved' WHERE id = ?", (order_id,))
                self._append_event(
                    db, order_id, "inventory_reserved",
                    {"expires_at": expires_at, "reservation_ids": reservation_ids},
                    now,
                )
                self._enqueue_outbox(
                    db, "order_created",
                    {"order_id": order_id, "cart_id": cart_id, "total_kopecks": total, "expires_at": expires_at},
                    now,
                )
                response = {
                    "order_id": order_id,
                    "total_kopecks": total,
                    "expires_at": expires_at,
                    "reservation_ids": reservation_ids,
                }
                self._idem_put(db, idempotency_key, response, now)
                db.execute("COMMIT")
                return response
            except Exception:
                db.execute("ROLLBACK")
                raise

    def mock_payment_success(self, order_id: str, payment_id: str, idempotency_key: str) -> dict:
        """Simulates T-Bank webhook arrival for a successful payment.

        inventory_reserved -> payment_authorized -> payment_captured.
        Converts reservations: decrements `products.stock` permanently, marks reservations converted.
        """
        now = self._now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cached = self._idem_get(db, idempotency_key)
                if cached is not None:
                    db.execute("ROLLBACK")
                    return cached
                row = db.execute("SELECT state FROM orders WHERE id = ?", (order_id,)).fetchone()
                if row is None:
                    raise StateError(f"order {order_id} does not exist")
                self._enforce(row[0], "payment_authorized")
                expired = db.execute(
                    """SELECT id FROM inventory_reservations
                       WHERE order_id = ? AND converted=0 AND cancelled=0 AND expires_at <= ?""",
                    (order_id, now),
                ).fetchall()
                if expired:
                    raise StateError(
                        f"order {order_id} has expired reservations; cannot capture payment"
                    )
                reservations = db.execute(
                    """SELECT id, sku, quantity FROM inventory_reservations
                       WHERE order_id = ? AND converted=0 AND cancelled=0""",
                    (order_id,),
                ).fetchall()
                if not reservations:
                    raise StateError(f"order {order_id} has no active reservations")
                for rid, sku, qty in reservations:
                    cur = db.execute("SELECT stock FROM products WHERE sku = ?", (sku,)).fetchone()[0]
                    if cur < qty:
                        raise StateError(
                            f"corrupt: stock for {sku}={cur} but reservation={qty}"
                        )
                    db.execute("UPDATE products SET stock = stock - ? WHERE sku = ?", (qty, sku))
                    db.execute("UPDATE inventory_reservations SET converted = 1 WHERE id = ?", (rid,))
                db.execute(
                    "UPDATE orders SET state = 'payment_authorized', tbank_payment_id = ? WHERE id = ?",
                    (payment_id, order_id),
                )
                self._append_event(db, order_id, "payment_authorized", {"payment_id": payment_id}, now)
                self._enforce("payment_authorized", "payment_captured")
                db.execute("UPDATE orders SET state = 'payment_captured' WHERE id = ?", (order_id,))
                self._append_event(db, order_id, "payment_captured", {"payment_id": payment_id}, now)
                self._enqueue_outbox(
                    db, "payment_captured",
                    {"order_id": order_id, "payment_id": payment_id},
                    now,
                )
                response = {"order_id": order_id, "state": "payment_captured", "payment_id": payment_id}
                self._idem_put(db, idempotency_key, response, now)
                db.execute("COMMIT")
                return response
            except Exception:
                db.execute("ROLLBACK")
                raise

    def expire_reservations(self) -> int:
        """Cron-style cleanup. Cancels expired reservations and any orders still in inventory_reserved.

        Returns count of cancelled orders.
        """
        now = self._now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                expired_orders = [
                    r[0]
                    for r in db.execute(
                        """SELECT DISTINCT order_id FROM inventory_reservations
                           WHERE converted=0 AND cancelled=0 AND expires_at <= ?""",
                        (now,),
                    ).fetchall()
                ]
                db.execute(
                    """UPDATE inventory_reservations SET cancelled = 1
                       WHERE converted=0 AND cancelled=0 AND expires_at <= ?""",
                    (now,),
                )
                cancelled_count = 0
                for oid in expired_orders:
                    state_row = db.execute("SELECT state FROM orders WHERE id = ?", (oid,)).fetchone()
                    if state_row and state_row[0] == "inventory_reserved":
                        self._enforce(state_row[0], "cancelled")
                        db.execute("UPDATE orders SET state = 'cancelled' WHERE id = ?", (oid,))
                        self._append_event(
                            db, oid, "cancelled", {"reason": "reservation_expired"}, now
                        )
                        self._enqueue_outbox(
                            db, "order_cancelled",
                            {"order_id": oid, "reason": "reservation_expired"},
                            now,
                        )
                        cancelled_count += 1
                db.execute("COMMIT")
                return cancelled_count
            except Exception:
                db.execute("ROLLBACK")
                raise
