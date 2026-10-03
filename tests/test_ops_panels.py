"""Unit tests for the Ops Console panels added in deliverable E.

Verifies the two new panels ('Pre-Checks Today', 'Buffer Alerts Sent') render
via both the context builder and the HTMX partial endpoints, and that the
existing panels/partials are untouched.

Run: python3 tests/test_ops_panels.py
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import alert_store
import api
import dashboard

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
    print("\n=== ops panels tests ===")
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    precheck_log = [
        {"timestamp": f"{today}T10:00:00+00:00", "trader_id": "t1",
         "account_key": "a1", "status": "ELIGIBLE", "amount_usd": 200,
         "buffer_remaining_usd": 2600, "buffer_pct": 130.0},
        {"timestamp": f"{today}T10:01:00+00:00", "trader_id": "t2",
         "account_key": "a2", "status": "NOT_ELIGIBLE", "amount_usd": 900,
         "buffer_remaining_usd": 300, "buffer_pct": 15.0},
        {"timestamp": f"{today}T10:02:00+00:00", "trader_id": "t3",
         "account_key": "a3", "status": "MANUAL_REVIEW", "amount_usd": 500,
         "buffer_remaining_usd": 1000, "buffer_pct": 50.0},
        {"timestamp": "2020-01-01T00:00:00+00:00", "trader_id": "old",
         "account_key": "a0", "status": "ELIGIBLE", "amount_usd": 1,
         "buffer_remaining_usd": 1, "buffer_pct": 1.0},
    ]
    buffer_alerts = [
        {"account_key": "a2", "trader_id": "t2", "severity": "CRITICAL",
         "buffer_usd": 180, "buffer_pct": 9.0, "message": "m",
         "channel": "log", "sent": True, "suppressed": False,
         "detected_at": f"{today}T10:05:00+00:00",
         "sent_at": f"{today}T10:05:00+00:00", "latency_ms": 0.1},
        {"account_key": "a3", "trader_id": "t3", "severity": "WARNING",
         "buffer_usd": 580, "buffer_pct": 29.0, "message": "m",
         "channel": "log", "sent": True, "suppressed": False,
         "detected_at": f"{today}T10:06:00+00:00",
         "sent_at": f"{today}T10:06:00+00:00", "latency_ms": 0.1},
        {"account_key": "a3", "trader_id": "t3", "severity": "CRITICAL",
         "buffer_usd": 100, "buffer_pct": 5.0, "message": "m",
         "channel": "suppressed", "sent": False, "suppressed": True,
         "detected_at": f"{today}T10:30:00+00:00",
         "sent_at": f"{today}T10:30:00+00:00", "latency_ms": 0.0},
    ]

    ctx = dashboard.build_dashboard_context(
        accounts={}, payout_requests={}, payout_decisions={},
        precheck_log=precheck_log, buffer_alerts=buffer_alerts)

    pc = ctx["prechecks_today"]
    check("prechecks today total excludes yesterday", pc["total"] == 3,
          str(pc["total"]))
    check("eligible count", pc["counts"]["ELIGIBLE"] == 1, str(pc["counts"]))
    check("not_eligible count", pc["counts"]["NOT_ELIGIBLE"] == 1,
          str(pc["counts"]))
    check("manual_review count", pc["counts"]["MANUAL_REVIEW"] == 1,
          str(pc["counts"]))

    ba = ctx["buffer_alerts"]
    check("buffer alerts sent count", ba["sent_count"] == 2,
          str(ba["sent_count"]))
    check("suppressed count", ba["suppressed_count"] == 1,
          str(ba["suppressed_count"]))
    check("severity breakdown", ba["by_severity"] == {"CRITICAL": 1,
                                                      "WARNING": 1},
          str(ba["by_severity"]))

    # HTTP endpoints.
    client = TestClient(api.app)
    api.PRECHECK_LOG.clear()
    api.PRECHECK_LOG.extend(precheck_log)
    alert_store.clear()
    alert_store.BUFFER_ALERTS.extend(buffer_alerts)

    resp = client.get("/ops")
    check("GET /ops 200", resp.status_code == 200, str(resp.status_code))
    check("/ops has Pre-Checks Today panel",
          "Pre-Checks Today" in resp.text)
    check("/ops has Buffer Alerts Sent panel",
          "Buffer Alerts Sent" in resp.text)
    check("/ops still has old panels",
          "Accounts at Risk" in resp.text and "Payout Queue" in resp.text
          and "Recent Decisions" in resp.text)

    resp = client.get("/ops/partials/prechecks")
    check("GET prechecks partial 200", resp.status_code == 200)
    check("prechecks partial shows counts",
          "ELIGIBLE" in resp.text and "NOT_ELIGIBLE" in resp.text)
    check("prechecks partial has 10s polling markup on page",
          "every 10s" in client.get("/ops").text)

    resp = client.get("/ops/partials/buffer_alerts")
    check("GET buffer_alerts partial 200", resp.status_code == 200)
    check("buffer partial shows CRITICAL", "CRITICAL" in resp.text)
    check("buffer partial notes suppression",
          "suppressed" in resp.text.lower())

    # Old partials remain intact.
    for path in ("/ops/partials/accounts", "/ops/partials/queue",
                 "/ops/partials/decisions"):
        r = client.get(path)
        check(f"old partial {path} 200", r.status_code == 200)

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
