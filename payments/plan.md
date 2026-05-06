# Payments — Build Plan

**Method**: type-driven + test-driven. Adheres to `fail-proof-design.md` from line 1 (FSM, reservations+TTL, idempotency, event log, outbox, Decimal money in kopecks). Tests are **negative-only** — they emulate failure modes via injected clocks and synthetic retries. No pytest. No real waits. Frontend wiring comes last.

**Steps** (each ends only when its negative tests stop detecting anomalies):

1. `mock/inventory.json` — 10 SKUs, `price_kopecks` + `stock` (single source of truth).
2. `mock/buyers/*.json` — N buyers, each declares purchases.
3. `cart.py` — full surface per design: FSM, idempotency table, inventory_reservations w/ TTL, order_events log, outbox, products/orders tables. Clock injectable.
4. `test_surge.py` — concurrent buyers full flow (add → start_checkout → mock_payment_success); fails reveal races, lost writes, double decrement.
5. `test_idempotency.py` — same `Idempotency-Key` twice = one mutation (emulates network retry).
6. `test_reservation_ttl.py` — buyer reserves; clock advanced 10min (still held, second buyer blocked) and 90min (expired, order cancelled, stock released, second buyer succeeds).
7. `test_state_machine.py` — invalid FSM transitions rejected; bad inputs rejected.
8. Lock cart. Move to T-Bank sandbox with same negative-only e2e methodology (webhook replay, bad HMAC, network drop).
9. Lock T-Bank. Frontend wiring + UX hygiene + content + domain transfer per `fail-proof-design.md`.
