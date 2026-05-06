#!/usr/bin/env bash
# Runs all negative-only tests. Exit 0 = no anomalies anywhere. Exit 1 = at least one detected.
set -u
cd "$(dirname "$0")"

PY="${PY:-python3}"
TESTS=(test_surge.py test_idempotency.py test_reservation_ttl.py test_state_machine.py test_http.py test_tbank_signing.py test_tbank_webhook.py)
FAILED=0

for t in "${TESTS[@]}"; do
    out="$($PY "$t" 2>&1)"
    rc=$?
    if [ $rc -ne 0 ]; then
        echo "[$t] EXIT=$rc"
        echo "$out" | sed 's/^/    /'
        FAILED=1
    fi
done

exit $FAILED
