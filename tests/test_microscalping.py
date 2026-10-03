"""Unit + integration tests for the funded microscalping gate (deliverable F).

Covers the silent Tradeify rule: on a FUNDED account, MORE THAN 50% of trades
must be held >10s AND MORE THAN 50% of gross profit must come from holds >10s.
At or below either threshold blocks a payout. The rule does NOT apply during an
evaluation, and empty/zero-profit histories must never FAIL.

Run: python3 tests/test_microscalping.py
"""

import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient

import api
from microscalping import (
    MICROSCALP_CITATION,
    PROFIT_RATIO_CODE,
    TRADE_RATIO_CODE,
    check_microscalping,
)
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


def trade(hold_seconds, pnl):
    return {"hold_seconds": hold_seconds, "pnl": pnl}


def main():
    print("\n=== microscalping tests ===")

    # --- direct unit tests on check_microscalping() ------------------------
    sixty_sixty = ([trade(30, 10)] * 6) + ([trade(5, 10)] * 4)
    r = check_microscalping(sixty_sixty)
    check("60% trades / 60% profit -> PASS", r.status == "PASS", r.status)
    check("PASS ratios are 0.6 / 0.6",
          r.trade_ratio == 0.6 and r.profit_ratio == 0.6, r.model_dump())
    check("PASS cites the microscalping chunk",
          r.citation == MICROSCALP_CITATION, r.citation)

    forty_trades = ([trade(30, 20)] * 4) + ([trade(5, 5)] * 6)
    r = check_microscalping(forty_trades)
    check("40% trades -> FAIL", r.status == "FAIL", r.status)
    check("40% trades flags the trade-ratio code",
          r.reason_codes == [TRADE_RATIO_CODE], str(r.reason_codes))
    check("trade-ratio reason carries concrete numbers",
          "40.0%" in r.reason and "4/10" in r.reason, r.reason)

    forty_profit = ([trade(30, 4)] * 6) + ([trade(5, 9)] * 4)
    r = check_microscalping(forty_profit)
    check("60% trades but 40% profit -> FAIL", r.status == "FAIL", r.status)
    check("40% profit flags the profit-ratio code",
          r.reason_codes == [PROFIT_RATIO_CODE], str(r.reason_codes))

    boundary = ([trade(30, 10)] * 5) + ([trade(5, 10)] * 5)
    r = check_microscalping(boundary)
    check("exactly 50% -> FAIL (rule is MORE than 50%)",
          r.status == "FAIL" and set(r.reason_codes) ==
          {TRADE_RATIO_CODE, PROFIT_RATIO_CODE}, str(r.model_dump()))

    just_over = ([trade(30, 1)] * 51) + ([trade(5, 1)] * 49)
    r = check_microscalping(just_over)
    check("51% -> PASS", r.status == "PASS", r.status)

    r = check_microscalping([])
    check("0 trades -> NEED_MORE_DATA (not FAIL)",
          r.status == "NEED_MORE_DATA", r.status)

    zero_profit = [trade(30, 0.0)] * 3
    r = check_microscalping(zero_profit)
    check("non-positive gross profit -> NEED_MORE_DATA",
          r.status == "NEED_MORE_DATA" and r.profit_ratio is None,
          str(r.model_dump()))

    r = check_microscalping(forty_trades, phase="eval")
    check("evaluation account -> SKIP", r.status == "SKIP", r.status)
    check("SKIP reason explains the rule is funded-only",
          "funded" in r.reason, r.reason)

    exactly_10s = [trade(10.0, 10.0)] * 10
    r = check_microscalping(exactly_10s)
    check("a 10.0s hold is NOT >10s", r.n_long_holds == 0, str(r.n_long_holds))

    # --- end-to-end through POST /payout/precheck --------------------------
    api.ACCOUNTS.clear()
    api.PAYOUT_REQUESTS.clear()
    api.PAYOUT_DECISIONS.clear()
    api.PRECHECK_LOG.clear()
    client = TestClient(api.app)

    from rule_engine import AccountState
    funded = AccountState.new("growth_funded_50k")
    funded.payouts_taken = 1
    api.ACCOUNTS["ms_funded"] = funded
    api.ACCOUNTS["ms_eval"] = AccountState.new("growth_eval_50k")

    base = {
        "trader_id": "trader_ms",
        "account_key": "ms_funded",
        "amount_usd": 200,
        "current_equity": 50600,
        "current_balance": 50600,
        "kyc_verified": True,
        "trading_days": 10,
        "daily_profits": [300, 300, 300],
    }

    idem_before = dict(PAYOUT_IDEMPOTENCY)
    resp = client.post("/payout/precheck",
                       json={**base, "trades": forty_trades})
    check("precheck returns 200 for microscalping case",
          resp.status_code == 200, str(resp.status_code))
    body = resp.json()
    check("funded microscalping FAIL -> NOT_ELIGIBLE",
          body["status"] == "NOT_ELIGIBLE", body["status"])
    check("microscalping reason code present",
          TRADE_RATIO_CODE in body["reason_codes"], str(body["reason_codes"]))
    check("microscalping citation present",
          MICROSCALP_CITATION in body["citations"], str(body["citations"]))
    check("contradictory approval reason removed",
          "PAYOUT_APPROVED" not in body["reason_codes"],
          str(body["reason_codes"]))
    check("microscalping detail surfaced",
          body["microscalping"]["status"] == "FAIL",
          str(body["microscalping"]))

    resp = client.post("/payout/precheck",
                       json={**base, "trades": sixty_sixty})
    good = resp.json()
    check("funded microscalping PASS does not block ELIGIBLE",
          good["status"] == "ELIGIBLE", good["status"])
    check("PASS case carries no microscalping reason code",
          TRADE_RATIO_CODE not in good["reason_codes"]
          and PROFIT_RATIO_CODE not in good["reason_codes"],
          str(good["reason_codes"]))

    resp = client.post("/payout/precheck",
                       json={**base, "account_key": "ms_eval",
                             "trades": forty_trades})
    ev = resp.json()
    check("evaluation account skips microscalping",
          ev["microscalping"]["status"] == "SKIP",
          str(ev["microscalping"]))
    check("evaluation is not blocked by microscalping codes",
          TRADE_RATIO_CODE not in ev["reason_codes"]
          and PROFIT_RATIO_CODE not in ev["reason_codes"],
          str(ev["reason_codes"]))

    # Read-only guarantee: the new check must not write anything.
    check("no payout request written", api.PAYOUT_REQUESTS == {})
    check("no payout decision written", api.PAYOUT_DECISIONS == {})
    check("idempotency store untouched", dict(PAYOUT_IDEMPOTENCY) == idem_before)
    check("precheck log grew by three", len(api.PRECHECK_LOG) == 3,
          str(len(api.PRECHECK_LOG)))

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
