"""
worker.py -- Durable background sync (the BullMQ-equivalent in the DeepSeek plan).

Every SYNC_INTERVAL seconds, for each tracked account:
  1. fetch the latest broker snapshot (positions, PnL, equity)
  2. advance the EOD trailing-drawdown floor on session close
  3. run the deterministic rule engine
  4. emit alerts on NEW findings (hard breach / soft breach / warning)

It also exposes process_payout_request(request_id): the deterministic payout
pipeline used by the API (eligibility -> amount -> decide -> persist + audit).

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from rule_engine import AccountState, evaluate_all, summarize, update_dd_floor, ET
from payout import (
    AUTO_APPROVE_THRESHOLD_USD,
    Decision,
    PayoutPolicy,
    decide_idempotent,
)

# Payout policy used by the worker pipeline. Mirrors payout.PayoutPolicy, but
# the worker additionally re-asserts the fraud threshold for defence in depth.
PAYOUT_POLICY = PayoutPolicy()

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


def process_payout_request(request_id: str) -> Decision | None:
    """
    Deterministic payout pipeline for one stored request.

    Flow: load request data -> check_eligibility -> compute_payout_amount ->
    decide -> persist decision + citations + audit entry.

    Amount policy applied on top of ``decide``:
      - APPROVED and amount < $500  -> ready to 'enqueue payment' (noted in
        the decision; no payment-gateway integration is attempted).
      - APPROVED and amount >= $500 -> escalated to MANUAL_REVIEW (fraud risk).
      - REJECTED -> reasons + citations attached for trader notification.

    The in-memory stores live in ``api``; imported lazily so this module can
    also run standalone (as a durable worker) without a circular import.
    """
    import api

    entry = api.PAYOUT_REQUESTS.get(request_id)
    if entry is None:
        return None

    # A stored decision is immutable: replaying process_payout_request is
    # idempotent and never re-decides. The replay is still audited.
    if request_id in api.PAYOUT_DECISIONS:
        cached = Decision(**api.PAYOUT_DECISIONS[request_id])
        api._audit("payout_decision", request_id, "IDEMPOTENT_REPLAY",
                   payload={"decision": cached.decision,
                            "amount_usd": cached.amount_usd,
                            "input": entry["request"].model_dump()},
                   actor="system")
        return cached

    decision = decide_idempotent(
        entry["request"], entry["account"], entry.get("trades", []),
        entry["kyc"], PAYOUT_POLICY)

    # Defence in depth: re-assert the fraud threshold even if a custom policy
    # let a >= threshold amount through as APPROVED.
    if (decision.decision == "APPROVED"
            and decision.amount_usd >= AUTO_APPROVE_THRESHOLD_USD):
        decision = decision.model_copy(update={
            "decision": "MANUAL_REVIEW",
            "note": "manual review: amount >= auto-approve threshold "
                    "(fraud risk re-check in worker)",
            "reasons": decision.reasons + [
                f"[PAYOUT_WORKER_ESCALATION] Worker escalated "
                f"${decision.amount_usd:,.2f} >= "
                f"${AUTO_APPROVE_THRESHOLD_USD:,.0f} to manual review."],
        })
    elif decision.decision == "APPROVED":
        decision = decision.model_copy(update={
            "note": "approved: ready to enqueue payment "
                    "(no payment gateway; Stripe/Wise on the roadmap)"})

    record = decision.model_dump()
    api.PAYOUT_DECISIONS[request_id] = record
    # Audit payload carries timestamp (audit row), the input request, the
    # output amount/decision, and every evaluated rule check.
    api._audit("payout_decision", request_id, decision.decision,
               payload={"amount_usd": decision.amount_usd,
                        "reasons": decision.reasons,
                        "citations": decision.citations,
                        "note": decision.note,
                        "rules_evaluated": [c.model_dump()
                                            for c in decision.checks],
                        "input": entry["request"].model_dump()},
               actor=decision.decided_by)
    return decision


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
