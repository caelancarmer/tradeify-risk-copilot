"""Unit tests for the buffer monitor in worker.py (deliverable C).

Run: python3 tests/test_buffer_alerts.py
"""

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import alert_store
import worker
from rule_engine import ACCOUNT_SPECS, AccountState

PASS, FAIL = 0, 0
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def _equity_for(spec_key, pct):
    state = AccountState.new(spec_key)
    dd = ACCOUNT_SPECS[spec_key].trailing_drawdown
    return state, state.dd_floor + pct / 100.0 * dd


def main():
    print("\n=== buffer alert tests ===")
    alert_store.clear()
    worker.LAST_BUFFER_ALERT_AT.clear()

    # Classification boundaries.
    check("50% -> OK", worker.classify_buffer(1000, 2000) == "OK")
    check("30% -> OK (boundary)", worker.classify_buffer(600, 2000) == "OK")
    check("29.9% -> WARNING", worker.classify_buffer(598, 2000) == "WARNING")
    check("10% -> WARNING (boundary)",
          worker.classify_buffer(200, 2000) == "WARNING")
    check("9.9% -> CRITICAL", worker.classify_buffer(198, 2000) == "CRITICAL")
    check("0% -> CRITICAL", worker.classify_buffer(0, 2000) == "CRITICAL")
    check("negative buffer -> CRITICAL",
          worker.classify_buffer(-50, 2000) == "CRITICAL")
    check("buffer_pct computes correctly",
          worker.buffer_pct(500, 2000) == 25.0)

    # Alert is recorded and falls back to the log channel when Discord is off.
    state, equity = _equity_for("growth_funded_50k", 25)
    res = worker.monitor_account("acct1", state, equity, trader_id="t1",
                                 now=BASE.timestamp())
    check("warning alert fired", res["alerted"] is True, str(res))
    check("severity is WARNING", res["severity"] == "WARNING")
    check("discord-off falls back to log", res["channel"] == "log",
          str(res["channel"]))
    check("alert recorded", len(alert_store.BUFFER_ALERTS) == 1)
    check("record marked sent", alert_store.BUFFER_ALERTS[0]["sent"] is True)
    check("latency under 2s", res["latency_ms"] < 2000.0, str(res["latency_ms"]))

    # Rate limit: second alert within the hour is suppressed.
    state2, equity2 = _equity_for("growth_funded_50k", 5)
    res2 = worker.monitor_account("acct1", state2, equity2, now=BASE.timestamp() + 1800)
    check("second alert within hour suppressed",
          res2["suppressed"] is True and res2["alerted"] is False, str(res2))
    check("suppressed record not counted as sent",
          alert_store.BUFFER_ALERTS[-1]["sent"] is False)

    # After an hour it fires again.
    res3 = worker.monitor_account("acct1", state2, equity2,
                                  now=BASE.timestamp() + 3660)
    check("alert allowed after an hour", res3["alerted"] is True, str(res3))
    check("severity escalated to CRITICAL", res3["severity"] == "CRITICAL")

    # Per-account isolation.
    res4 = worker.monitor_account("acct_other", state2, equity2,
                                  now=BASE.timestamp() + 3700)
    check("different account still alerts", res4["alerted"] is True)

    # Healthy buffer never alerts.
    state3, equity3 = _equity_for("growth_funded_50k", 50)
    res5 = worker.monitor_account("acct_healthy", state3, equity3,
                                  now=BASE.timestamp())
    check("healthy buffer no alert", res5["alerted"] is False)
    check("healthy severity OK", res5["severity"] == "OK")

    # monitor_buffers polls TRACKED with injected equities.
    worker.TRACKED.clear()
    worker.SEEN.clear()
    worker.track_account("poll_acct", "growth_funded_50k", trader_id="t9")
    st, eq = _equity_for("growth_funded_50k", 8)
    results = worker.monitor_buffers(now=BASE.timestamp() + 10000,
                                     equities={"poll_acct": eq})
    check("monitor_buffers polls tracked account",
          len(results) == 1 and results[0]["account_key"] == "poll_acct")
    check("monitor_buffers detects CRITICAL",
          results[0]["severity"] == "CRITICAL", str(results[0]))

    # Summary counts only sent alerts.
    summary = alert_store.alert_summary(now=BASE)
    check("summary counts sent alerts", summary["sent_count"] >= 1,
          str(summary["sent_count"]))
    check("summary has severity breakdown", "CRITICAL" in summary["by_severity"],
          str(summary["by_severity"]))

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
