# Cart & Payment Processing — Fail-Proof Design Principles

**Scope**: Universe Vitamins corporate-domain webshop. Cart UI in browser, payment backend on `root@65.21.53.168`, T-Bank Acquiring API for card processing.

**Goal**: System where wire-level failures (network drops, browser refresh, double-clicks, T-Bank webhook re-delivery, server restarts) **cannot produce wrong outcomes** — no double charges, no lost orders, no inventory desync, no missed notifications.

---

## Stack

- **Backend**: Python (stdlib + `requests` for T-Bank, `flask` or `fastapi` for HTTP)
- **DB**: SQLite, WAL mode, `synchronous=NORMAL`, `foreign_keys=ON`
- **Backup**: Litestream → S3-compatible bucket (continuous streaming + PITR)
- **Worker**: cron-driven Python script for outbox draining + reservation TTL cleanup + reconciliation
- **Secrets**: env vars on server, never in git
- **Logging**: stdout → systemd-journal, with correlation IDs

No Redis. No Postgres. No Celery. No queue brokers. No microservices. No Kubernetes. No Docker mandatory (optional for reproducibility).

The simpler the stack, the fewer failure modes.

---

## 12 Must-Adhere Principles

### 1. Idempotency keys on every state mutation

Every cart-changing request from the browser carries a client-generated UUID in `Idempotency-Key` header. Server stores `(key, response)` in `idempotency` table. Same key seen twice → return stored response, do not re-process.

```sql
CREATE TABLE idempotency (
    key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
```

Applies to: add-to-cart, remove-from-cart, change-quantity, checkout-init, payment-confirm.

**Why**: browser retries on flaky 4G. Without this, every retry = double mutation.

### 2. Explicit state machine, persisted, no implicit states

Order has exactly these states, transitions one direction only:

```
cart_open
  → cart_validated
    → inventory_reserved
      → payment_authorized
        → payment_captured
          → order_fulfilled
            → order_delivered
[any state] → cancelled
```

Each transition is a row insert in `order_events` table (event sourcing — see #4). The current state is the latest event for that order.

**Why**: implicit state = bugs. Explicit state = enforceable invariants. State diagram is the spec; code is the implementation.

### 3. Inventory reservation with TTL, never decrement at "add to cart"

When user enters checkout (state `cart_validated → inventory_reserved`), insert reservation row with `expires_at = now + 15min`. Stock available = total stock − active (non-expired, non-converted) reservations.

Payment success → reservation converts to permanent decrement.
Payment failure or timeout → reservation expires, stock auto-released by cleanup worker.

```sql
CREATE TABLE inventory_reservations (
    id TEXT PRIMARY KEY,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_id TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    converted INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_reservations_active ON inventory_reservations(sku, expires_at) WHERE converted=0 AND cancelled=0;
```

**Why**: decrementing at add-to-cart kills stock for abandoned carts. TTL reservation = correct semantics.

### 4. Event log: every state change is an immutable append-only event

```sql
CREATE TABLE order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX idx_events_order ON order_events(order_id, id);
```

Current state of any order = replay events for that order. Never UPDATE, never DELETE — only INSERT.

**Why**: full audit trail, time-travel debugging, recovery from corrupted derived state. Reconciliation against T-Bank becomes mechanical.

### 5. Webhook handlers: signature-verified, idempotent by `payment_id`, always 2xx

T-Bank webhooks can be replayed or duplicated. Handler MUST:

1. Verify HMAC signature against terminal password. Invalid signature → drop silently (not 401 — don't leak which keys are wrong).
2. Parse `payment_id` from payload.
3. Check if `payment_id` already processed (table `processed_webhooks`). If yes → return 200 OK without action.
4. Process the event in a transaction.
5. Insert `payment_id` into `processed_webhooks`.
6. Return **200 OK always when signature valid**, even on internal errors. T-Bank retries 4xx/5xx — internal exceptions = retry storms.
7. Log internal errors to stderr for human review.

**Why**: T-Bank delivers at-least-once. Without dedupe + signature-verify + always-200, you get either double-processing OR retry storms.

### 6. Outbox pattern for side effects (email, SMS, Telegram, fulfillment)

Order created → in same DB transaction, write event to `outbox` table. Separate cron worker (every 30s) reads pending outbox rows, dispatches side effects, marks rows as `sent`.

```sql
CREATE TABLE outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_at INTEGER,
    retries INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL
);
```

Worker is idempotent per row (check `sent_at IS NULL`). Crashes are safe — events stay in outbox.

**Why**: synchronous email send inside checkout = SMTP timeout kills checkout. Outbox decouples critical path from notifications. Notifications can be slow, retried, or rerouted; orders never block.

### 7. Reconciliation job, daily, never optional

Cron job at 03:00 every day: pull T-Bank ledger for last 48h, compare against local `orders` table. Discrepancies (charge in T-Bank without local order, or vice versa) → write to `reconciliation_alerts`, escalate via Telegram bot.

**Why**: webhooks can fail silently (your server crashed during the 5-minute window T-Bank delivered). Reconciliation catches what webhooks missed. Without this, missed payments are invisible until customer complains.

### 8. Money is `Decimal`, never `float`

Python `decimal.Decimal` for all amounts. Store as **kopecks integer** in SQLite (`amount_kopecks INTEGER`). Render to user as ₽X.XX in UI layer only.

**Why**: `0.1 + 0.2 != 0.3` in float. Floats lose money. Integer kopecks is exact.

### 9. T-Bank credentials never in git, never in code

Server env vars: `TBANK_TERMINAL_KEY`, `TBANK_PASSWORD`, `TBANK_NOTIFICATION_URL`. Loaded via `os.environ`. Local dev uses sandbox credentials (no token/cert needed). Production uses live credentials, set via systemd service unit `Environment=` directives or `.env` file outside git.

`.gitignore` has `.env`, `*.db`, `secrets/`.

**Why**: leaked T-Bank credentials = direct financial liability. Treat them like AWS keys.

### 10. SQLite hardening checklist

Mandatory PRAGMAs at connection open:

```python
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=5000")
```

`WAL` = concurrent reads while writes happen. `NORMAL` sync = durable across crashes (only loses ~1 transaction on power loss, never corrupts). `foreign_keys=ON` (off by default!) = referential integrity. `busy_timeout` = wait 5s for write lock instead of immediate failure.

Backup: Litestream → S3/B2 for continuous streaming. Recovery = `litestream restore`.

**Why**: SQLite default settings are conservative for legacy compat. WAL+NORMAL is the modern production config.

### 11. All HTTP responses to T-Bank: HMAC-signed, all responses from T-Bank: HMAC-verified

Use `hashlib.sha256` per T-Bank docs. Sign on send, verify on receive. Never accept any T-Bank-claimed payload without verifying signature.

**Why**: without signature checks, attacker hits webhook URL with `{status: "CONFIRMED"}` and you ship product without payment.

### 12. Test in sandbox until 100% green, then flip credentials

T-Bank sandbox needs no token, no certificate, no real money ([T-Bank docs](https://developer.tbank.ru/docs/intro/integration-steps)). Build full happy + error paths in sandbox:

- Successful card payment
- 3DS challenge
- Card declined
- Cancellation
- Refund (full + partial)
- Webhook delivery for each above
- Webhook re-delivery (replay same payload)
- Network failure mid-payment

Only flip env vars to production credentials after every scenario passes smoke test.

**Why**: production debugging costs real money + reputation. Sandbox covers 95% of integration bugs free.

---

## Anti-Patterns That GUARANTEE Failure

1. ❌ Cart in browser localStorage only (no server mirror) → user clears storage, loses cart
2. ❌ Inventory decrement at add-to-cart → abandoned carts kill stock
3. ❌ Synchronous email/SMS in checkout response path → SMTP slow → checkout timeout
4. ❌ T-Bank API call without idempotency key → network retry → double charge
5. ❌ Webhook handler returns 500 on internal error → T-Bank retry storm
6. ❌ Webhook signature not verified → spoofable success
7. ❌ Float for money → cumulative rounding loss
8. ❌ No reconciliation → missed payments invisible
9. ❌ Single-step state transitions (cart → fulfilled in one DB write) → no recovery on crash mid-way
10. ❌ Credentials in code or `.env` committed to git → eventual leak
11. ❌ SQLite without WAL → write blocks all reads → checkout slow
12. ❌ No backup strategy → DB corruption = whole business down

---

## Minimum Schema (starter)

```sql
-- Customers (created on first checkout)
CREATE TABLE customers (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    phone TEXT,
    created_at INTEGER NOT NULL
);

-- Products (synced from products.en.json or admin UI)
CREATE TABLE products (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    price_kopecks INTEGER NOT NULL,
    stock INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- Carts (server-side mirror, persistent)
CREATE TABLE carts (
    id TEXT PRIMARY KEY,
    customer_id TEXT,
    state TEXT NOT NULL DEFAULT 'cart_open',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);

CREATE TABLE cart_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cart_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    price_kopecks INTEGER NOT NULL,
    FOREIGN KEY(cart_id) REFERENCES carts(id),
    FOREIGN KEY(sku) REFERENCES products(sku),
    UNIQUE(cart_id, sku)
);

-- Orders (created from cart on checkout)
CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    cart_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    total_kopecks INTEGER NOT NULL,
    state TEXT NOT NULL,
    tbank_payment_id TEXT,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(cart_id) REFERENCES carts(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id)
);

-- Idempotency (covered above)
-- Inventory reservations (covered above)
-- Order events (covered above)
-- Outbox (covered above)
-- Processed webhooks (covered above)
CREATE TABLE processed_webhooks (
    payment_id TEXT PRIMARY KEY,
    processed_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

-- Reconciliation alerts
CREATE TABLE reconciliation_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    severity TEXT NOT NULL,
    description TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
```

---

## Sources

- [T-Bank Acquiring API root](https://developer.tbank.ru/eacq/api/)
- [T-Bank integration steps + sandbox info](https://developer.tbank.ru/docs/intro/integration-steps)
- [Microsoft Azure — Event Sourcing pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
- [DZone — Idempotency in event-driven systems](https://dzone.com/articles/idempotency-and-reliability-in-event-driven-systems)
- [event-driven.io — Outbox pattern, projections, read models](https://event-driven.io/en/projections_and_read_models_in_event_driven_architecture/)
- [CockroachLabs — Idempotency and ordering](https://www.cockroachlabs.com/blog/idempotency-and-ordering-in-event-driven-systems/)
- [System Design Handbook — E-commerce design](https://www.systemdesignhandbook.com/guides/design-e-commerce-system-design/)
- [SQLite WAL mode docs](https://www.sqlite.org/wal.html)
- [Litestream — SQLite continuous backup](https://litestream.io/)
