"""
worker.py -- Durable background sync (the BullMQ-equivalent in the DeepSeek plan).

Every SYNC_INTERVAL seconds, for each tracked account:
  1. fetch the latest broker snapshot (positions, PnL, equity)
  2. advance the EOD trailing-drawdown floor on session close
  3. run the deterministic rule engine
  4. emit alerts on NEW findings (hard breach / soft breach / warning)

Broker integration is a stub: implement fetch_broker_snapshot() against
Tradovate or Rithmic WebSocket/REST. Alert sinks: stdout (demo), Discord
webhook, Slack webhook (set ALERT_WEBHOOK_URL).

Run: python3 src/worker.py
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from rule_engine import AccountState, evaluate_all, summarize, update_dd_floor, ET

SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "30"))
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "worker_state.json")

TRACKED: dict[str, AccountState] = {
    "demo_growth_50k": AccountState.new("growth_funded_50k"),
}
SEEN: dict[str, set] = {k: set() for k in TRACKED}


def fetch_broker_snapshot(account_key: str) -> dict:
    """STUB. Replace with Tradovate/Rithmic API polling.
    Returns: day_pnl, current_equity, daily_profits, trades, current_balance,
             eod_balance (only meaningful at session close)."""
    st = TRACKED[account_key]
    return {"day_pnl": 0.0, "current_equity": st.dd_floor + 1500,
            "daily_profits": [], "trades": [],
            "current_balance": st.start_balance, "eod_balance": None}


def send_alert(text: str) -> None:
    print(f"[ALERT {datetime.now(ET):%H:%M:%S}] {text}", flush=True)
    if ALERT_WEBHOOK_URL:
        try:
            urllib.request.urlopen(urllib.request.Request(
                ALERT_WEBHOOK_URL,
                data=json.dumps({"text": text}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
        except Exception as e:  # alerts must never crash the worker
            print(f"webhook failed: {e}", flush=True)


def sync_once() -> None:
    for key, state in TRACKED.items():
        snap = fetch_broker_snapshot(key)
        if snap.get("eod_balance") is not None:
            floor, locked = update_dd_floor(state, snap["eod_balance"])
            print(f"[sync] {key}: EOD {snap['eod_balance']:.0f} -> floor {floor:.0f}"
                  f"{' LOCKED' if locked else ''}", flush=True)
        rep = summarize(evaluate_all(
            state, day_pnl=snap["day_pnl"], current_equity=snap["current_equity"],
            daily_profits=snap["daily_profits"], trades=snap["trades"],
            current_balance=snap["current_balance"]))
        for f in rep["all"]:
            fid = f["rule_id"] + ":" + f["status"]
            if f["status"] != "OK" and fid not in SEEN[key]:
                SEEN[key].add(fid)
                send_alert(f"{key}: {f['rule']} {f['status']} "
                           f"({f['severity']}) -- {f['message']}")
        if rep["account_failed"]:
            send_alert(f"{key}: ACCOUNT FAILED (hard breach). Stopping alerts.")
            SEEN[key].add("account_failed")


def main() -> None:
    print(f"worker started, interval={SYNC_INTERVAL}s, accounts={list(TRACKED)}", flush=True)
    while True:
        try:
            sync_once()
        except Exception as e:  # durable: never die on a bad poll
            print(f"[worker error] {e}", flush=True)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
