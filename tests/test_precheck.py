"""Unit tests for precheck.py and POST /payout/precheck (deliverable B).

Hard requirements verified here:
  - the endpoint is READ-ONLY: PAYOUT_REQUESTS / PAYOUT_DECISIONS and the
    engine's PAYOUT_IDEMPOTENCY store are untouched; only PRECHECK_LOG grows
  - the response has the brief's fields
  - a duplicate request_id already present in the payout tables is refused
  - a negative amount is never ELIGIBLE

Run: python3 tests/test_precheck.py
"""

import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import api
from payout import PAYOUT_IDEMPOTENCY

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def main():
    print("\n=== precheck tests ===")
    api.ACCOUNTS.clear()
    api.PAYOUT_REQUESTS.clear()
    api.PAYOUT_DECISIONS.clear()
    api.PRECHECK_LOG.clear()
    client = TestClient(api.app)

    api.ACCOUNTS["pc_acct"] = __import__(
        "rule_engine").AccountState.new("growth_funded_50k")
    api.ACCOUNTS["pc_acct"].payouts_taken = 1

    base = {
        "trader_id": "trader_pc",
        "account_key": "pc_acct",
        "amount_usd": 200,
        "current_equity": 50600,
        "current_balance": 50600,
        "kyc_verified": True,
        "trading_days": 10,
        "daily_profits": [300, 300, 300],
    }

    idem_before = dict(PAYOUT_IDEMPOTENCY)
    resp = client.post("/payout/precheck", json=base)
    check("precheck returns 200", resp.status_code == 200,
          f"got {resp.status_code}")
    body = resp.json()
    for field in ("status", "reasons", "citations", "buffer_remaining_usd",
                  "suggested_action"):
        check(f"response has {field}", field in body)
    check("eligible status", body["status"] == "ELIGIBLE", str(body["status"]))
    check("buffer is equity - floor", body["buffer_remaining_usd"] == 2600.0,
          str(body["buffer_remaining_usd"]))
    check("suggested action present", bool(body["suggested_action"]))
    check("marked read_only", body["read_only"] is True)

    check("no payout request written", api.PAYOUT_REQUESTS == {})
    check("no payout decision written", api.PAYOUT_DECISIONS == {})
    check("idempotency store untouched", dict(PAYOUT_IDEMPOTENCY) == idem_before)
    check("precheck log grew", len(api.PRECHECK_LOG) == 1)

    # Unknown account -> 404.
    resp = client.post("/payout/precheck", json={**base, "account_key": "nope"})
    check("unknown account returns 404", resp.status_code == 404)

    # Negative amount is never eligible.
    resp = client.post("/payout/precheck", json={**base, "amount_usd": -50})
    check("negative amount NOT_ELIGIBLE",
          resp.json()["status"] == "NOT_ELIGIBLE", str(resp.json()["status"]))

    # Duplicate request_id already present in the payout tables -> refused.
    api.PAYOUT_REQUESTS["pc_used_id"] = {"request": None, "account": None}
    resp = client.post("/payout/precheck",
                       json={**base, "request_id": "pc_used_id"})
    dup = resp.json()
    check("duplicate request NOT_ELIGIBLE",
          dup["status"] == "NOT_ELIGIBLE", str(dup["status"]))
    check("duplicate cites PRECHECK_DUPLICATE_REQUEST",
          "PRECHECK_DUPLICATE_REQUEST" in dup["reason_codes"],
          str(dup["reason_codes"]))
    check("duplicate did not overwrite payout store",
          list(api.PAYOUT_REQUESTS) == ["pc_used_id"])

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
