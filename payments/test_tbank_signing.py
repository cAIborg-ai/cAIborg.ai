"""T-Bank Token signing — round-trip and known-vector negative tests.

Negative-only. Exit 0 = no anomaly. Exit 1 = signing or verification regressed.

Algorithm: SHA256(concat(values_sorted_by_key)) lowercase hex, where values
includes Password and TerminalKey, excludes nested objects (DATA, Receipt, Shops),
excludes the Token field itself. Bools serialized as "true"/"false", ints as decimal.
"""
import hashlib
import sys

import tbank


def main() -> int:
    failures: list[str] = []

    # 1) Known-vector check derived directly from the algorithm definition.
    # Values: {Amount=1000, OrderId=21090, Password=secret, TerminalKey=TestTerminal}
    # Sorted keys: Amount, OrderId, Password, TerminalKey
    # Concat: "1000" + "21090" + "secret" + "TestTerminal"
    expected = hashlib.sha256(b"100021090secretTestTerminal").hexdigest()
    body = {"Amount": 1000, "OrderId": "21090"}
    signed = tbank.sign_request(body, "TestTerminal", "secret")
    if signed["Token"] != expected:
        failures.append(f"sign_request token mismatch: got {signed['Token']} expected {expected}")
    if signed["TerminalKey"] != "TestTerminal":
        failures.append(f"sign_request did not stamp TerminalKey: {signed}")

    # 2) Nested objects MUST be excluded from token computation.
    body_with_nested = {
        "Amount": 1000,
        "OrderId": "21090",
        "DATA": {"foo": "bar"},
        "Receipt": {"Items": [{"Name": "X", "Price": 1000}]},
    }
    signed_nested = tbank.sign_request(body_with_nested, "TestTerminal", "secret")
    if signed_nested["Token"] != expected:
        failures.append(
            f"nested-object exclusion broken: token differs from baseline. got {signed_nested['Token']}"
        )

    # 3) Notification verification round-trip: sign a fake notification, verify with same password,
    #    must succeed. Tweak any field, verification must fail.
    notif = {
        "TerminalKey": "TestTerminal",
        "OrderId": "ord-123",
        "Success": True,
        "Status": "CONFIRMED",
        "PaymentId": 999000111,
        "ErrorCode": "0",
        "Amount": 200000,
        "Pan": "430000******0777",
        "ExpDate": "1130",
    }
    notif_signed = tbank.sign_request(notif, "TestTerminal", "secret")
    if not tbank.verify_notification(notif_signed, "secret"):
        failures.append(f"verify_notification rejected our own signed payload: {notif_signed}")

    # 4) Wrong password -> reject.
    if tbank.verify_notification(notif_signed, "wrong-password"):
        failures.append("verify_notification accepted wrong password")

    # 5) Tampered body field -> reject.
    tampered = dict(notif_signed)
    tampered["Amount"] = 999999  # attacker raises amount
    if tbank.verify_notification(tampered, "secret"):
        failures.append("verify_notification accepted tampered Amount")

    # 6) Missing Token -> reject.
    no_token = {k: v for k, v in notif_signed.items() if k != "Token"}
    if tbank.verify_notification(no_token, "secret"):
        failures.append("verify_notification accepted payload with no Token")

    # 7) Bool serialization: Success=true and Success=false must yield different tokens.
    a = tbank.sign_request({"OrderId": "x", "Success": True}, "T", "p")
    b = tbank.sign_request({"OrderId": "x", "Success": False}, "T", "p")
    if a["Token"] == b["Token"]:
        failures.append("Success=True and Success=False produced identical tokens")

    if failures:
        print("FAIL: tbank signing:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
