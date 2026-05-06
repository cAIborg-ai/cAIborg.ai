"""T-Bank Acquiring API — minimum surface, stdlib only.

Algorithm verified against canonical Go SDK (github.com/nikita-vanyasin/tinkoff,
client.go::generateToken). Identical semantics:

  Token = SHA256( concat(values_sorted_by_key) ) as lowercase hex
  where values map = {request top-level scalar fields} + {"TerminalKey": ..., "Password": ...}
  Nested objects (DATA, Receipt, Shops) are NOT included.

Production endpoint:  https://securepay.tinkoff.ru/v2
Sandbox: same URL with a DEMO terminal key (no certificates, no separate URL).

This module is pure: no DB, no global state. cart.py owns persistence and FSM;
api.py wires HTTP transport.
"""
import hashlib
import json
import os
import urllib.error
import urllib.request

TBANK_BASE_URL_DEFAULT = "https://securepay.tinkoff.ru/v2"

# Status values per official spec / Go SDK reference. Terminal states for our FSM:
STATUS_SUCCESS_TERMINAL = {"CONFIRMED", "AUTHORIZED"}  # AUTHORIZED if two-step PayType=T
STATUS_FAILURE_TERMINAL = {
    "REJECTED",
    "AUTH_FAIL",
    "CANCELED",
    "DEADLINE_EXPIRED",
    "REVERSED",
    "REFUNDED",
    "PARTIAL_REFUNDED",
}
STATUS_INTERMEDIATE = {
    "NEW",
    "FORM_SHOWED",
    "AUTHORIZING",
    "PREAUTHORIZING",
    "3DS_CHECKING",
    "3DS_CHECKED",
    "REVERSING",
    "PARTIAL_REVERSED",
    "CONFIRMING",
    "REFUNDING",
    "ASYNC_REFUNDING",
}


def generate_token(values: dict, password: str) -> str:
    """Canonical T-Bank Token: SHA256(concat(values_sorted_by_key)) as hex.

    `values` should already contain TerminalKey but NOT Password (we add Password here).
    Empty-string values are included; missing keys are excluded. Bools serialize to
    "true"/"false", ints to decimal strings — caller is responsible for that coercion
    before calling this function.
    """
    payload = dict(values)
    payload["Password"] = password
    keys = sorted(payload.keys())
    concat = "".join(payload[k] for k in keys)
    return hashlib.sha256(concat.encode("utf-8")).hexdigest()


def _scalar_values_for_token(d: dict) -> dict:
    """Filter dict to top-level scalar fields suitable for token computation.

    Excludes: nested dicts/lists (DATA, Receipt, Shops, Items), the Token field itself,
    and None-valued fields. Bools -> "true"/"false". Ints -> str. Strings pass through.
    Empty strings are kept.
    """
    out = {}
    for k, v in d.items():
        if k == "Token":
            continue
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[k] = str(v)
        elif isinstance(v, str):
            out[k] = v
        # dicts/lists: skip (nested objects not signed per spec)
    return out


def sign_request(body: dict, terminal_key: str, password: str) -> dict:
    """Returns a copy of `body` with TerminalKey + Token populated.

    Body should be the request body without auth fields; we add them.
    """
    out = dict(body)
    out["TerminalKey"] = terminal_key
    values = _scalar_values_for_token(out)
    out["Token"] = generate_token(values, password)
    return out


def verify_notification(notification: dict, password: str) -> bool:
    """Returns True if Token in notification matches recomputed token, else False.

    Per design fail-proof #5: invalid signature -> silently drop (not 401).
    Caller treats False as "drop, do nothing".
    """
    received_token = notification.get("Token")
    if not received_token:
        return False
    values = _scalar_values_for_token(notification)
    expected = generate_token(values, password)
    # constant-time compare
    if len(received_token) != len(expected):
        return False
    diff = 0
    for a, b in zip(received_token.lower(), expected.lower()):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def build_init_body(
    *,
    order_id: str,
    amount_kopecks: int,
    description: str = "",
    notification_url: str = "",
    success_url: str = "",
    fail_url: str = "",
    customer_key: str = "",
    pay_type: str = "O",  # one-step capture by default; "T" for two-step
) -> dict:
    """Build Init body per docs. Caller wraps with sign_request to add TerminalKey/Token."""
    body: dict = {
        "Amount": int(amount_kopecks),
        "OrderId": order_id,
        "PayType": pay_type,
    }
    if description:
        body["Description"] = description[:140]
    if notification_url:
        body["NotificationURL"] = notification_url
    if success_url:
        body["SuccessURL"] = success_url
    if fail_url:
        body["FailURL"] = fail_url
    if customer_key:
        body["CustomerKey"] = customer_key
    return body


def call_init(
    *,
    terminal_key: str,
    password: str,
    body: dict,
    base_url: str = None,
    timeout: float = 15.0,
) -> dict:
    """POST /Init. Returns parsed JSON response. Raises on transport / non-2xx errors."""
    base_url = base_url or os.environ.get("TBANK_BASE_URL", TBANK_BASE_URL_DEFAULT)
    signed = sign_request(body, terminal_key, password)
    req = urllib.request.Request(
        f"{base_url}/Init",
        method="POST",
        data=json.dumps(signed).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
