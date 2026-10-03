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
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import alert_store
from rule_engine import (
    ACCOUNT_SPECS,
    AccountState,
    evaluate_all,
    summarize,
    update_dd_floor,
    ET,
)
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


# ---------------------------------------------------------------------------
# Buffer monitor (deliverable C)
# ---------------------------------------------------------------------------
#
# buffer      = current_equity - dd_floor
# buffer_pct  = buffer / trailing_drawdown * 100
#   < 10%  -> CRITICAL
#   < 30%  -> WARNING
#   else   -> OK
#
# Alerts go to the trader's Discord DM when DISCORD_BOT_TOKEN + a recipient id
# are configured; otherwise they are written to the alerts log (never crash).
# Rate limit: at most one alert per account per hour.

BUFFER_WARNING_PCT = 30.0
BUFFER_CRITICAL_PCT = 10.0
BUFFER_ALERT_INTERVAL_SECONDS = int(
    os.environ.get("BUFFER_ALERT_INTERVAL_SECONDS", "3600"))

# account_key -> trader_id, and trader_id -> Discord user id.
TRADER_IDS: dict[str, str] = {}
TRADER_DISCORD_IDS: dict[str, str] = {}
# account_key -> unix timestamp of the last SENT buffer alert.
LAST_BUFFER_ALERT_AT: dict[str, float] = {}


def buffer_pct(buffer_usd: float, trailing_drawdown: float) -> float:
    if not trailing_drawdown or trailing_drawdown <= 0:
        return 0.0
    return buffer_usd / trailing_drawdown * 100.0


def classify_buffer(buffer_usd: float, trailing_drawdown: float) -> str:
    """OK | WARNING | CRITICAL from the drawdown-buffer percentage."""
    pct = buffer_pct(buffer_usd, trailing_drawdown)
    if pct < BUFFER_CRITICAL_PCT:
        return "CRITICAL"
    if pct < BUFFER_WARNING_PCT:
        return "WARNING"
    return "OK"


def _discord_recipient(trader_id: str) -> str:
    return TRADER_DISCORD_IDS.get(trader_id) or os.environ.get(
        "DISCORD_DEFAULT_USER_ID", "")


def send_discord_dm(trader_id: str, text: str) -> tuple[bool, str]:
    """
    Send a direct message to the trader via the Discord REST API.

    Returns (sent, channel). When Discord is not configured (no bot token or no
    recipient id) this returns (False, reason) and the caller logs instead; it
    never raises.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    recipient = _discord_recipient(trader_id)
    if not token or not recipient:
        return False, "discord not configured"
    try:
        open_dm = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/channels",
            data=json.dumps({"recipient_id": recipient}).encode(),
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(open_dm, timeout=5) as resp:
            channel_id = json.loads(resp.read().decode())["id"]
        post = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            data=json.dumps({"content": text}).encode(),
            headers={"Authorization": f"Bot {token}",
                     "Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(post, timeout=5)
        return True, "discord_dm"
    except Exception as e:  # alerts must never crash the worker
        return False, f"discord error: {e}"


def track_account(account_key: str, spec_key: str,
                  trader_id: str | None = None) -> AccountState:
    """Register an active account for buffer monitoring."""
    state = AccountState.new(spec_key)
    TRACKED[account_key] = state
    SEEN.setdefault(account_key, set())
    if trader_id:
        TRADER_IDS[account_key] = trader_id
    return state


def monitor_account(account_key: str, state: AccountState,
                    current_equity: float, *, trader_id: str | None = None,
                    now: float | None = None, force: bool = False) -> dict:
    """
    Evaluate one account's drawdown buffer and alert if it crossed a threshold.

    ``now`` is a unix timestamp (defaults to wall clock). ``force`` bypasses the
    hourly rate limit (used by tests/manual runs). Returns a result dict with
    severity / alerted / suppressed / latency_ms.
    """
    now_dt = (datetime.fromtimestamp(now, timezone.utc)
              if now is not None else datetime.now(timezone.utc))
    now_ts = now if now is not None else now_dt.timestamp()
    spec = ACCOUNT_SPECS[state.spec_key]
    buffer_usd = current_equity - state.dd_floor
    pct = buffer_pct(buffer_usd, spec.trailing_drawdown)
    severity = classify_buffer(buffer_usd, spec.trailing_drawdown)
    trader = trader_id or TRADER_IDS.get(account_key, account_key)

    base = {
        "account_key": account_key, "trader_id": trader,
        "severity": severity, "buffer_usd": round(buffer_usd, 2),
        "buffer_pct": round(pct, 2),
    }

    if severity == "OK":
        base.update({"alerted": False, "suppressed": False, "channel": None,
                     "latency_ms": 0.0})
        return base

    last = LAST_BUFFER_ALERT_AT.get(account_key)
    if (not force and last is not None
            and (now_ts - last) < BUFFER_ALERT_INTERVAL_SECONDS):
        message = (f"Suppressed duplicate {severity} buffer alert for "
                   f"{account_key}; last alert sent < 1 hour ago.")
        alert_store.record_alert(
            account_key=account_key, trader_id=trader, severity=severity,
            buffer_usd=buffer_usd, buffer_pct=pct, message=message,
            channel="suppressed", sent=False, suppressed=True,
            detected_at=now_dt, sent_at=now_dt)
        base.update({"alerted": False, "suppressed": True,
                     "channel": "suppressed", "latency_ms": 0.0,
                     "message": message})
        return base

    text = (f"[{severity}] {account_key}: drawdown buffer is "
            f"${buffer_usd:,.2f} ({pct:.1f}% of the "
            f"${spec.trailing_drawdown:,} trailing drawdown). "
            f"Protect the buffer before it hits the trailing floor.")
    t0 = time.perf_counter()
    sent, channel = send_discord_dm(trader, text)
    if not sent:
        print(f"[BUFFER {severity}] {text} ({channel})", flush=True)
        channel = "log"
    sent_at = datetime.now(timezone.utc)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    alert_store.record_alert(
        account_key=account_key, trader_id=trader, severity=severity,
        buffer_usd=buffer_usd, buffer_pct=pct, message=text, channel=channel,
        sent=True, suppressed=False, detected_at=now_dt, sent_at=sent_at,
        latency_ms=latency_ms)
    LAST_BUFFER_ALERT_AT[account_key] = now_ts
    base.update({"alerted": True, "suppressed": False, "channel": channel,
                 "latency_ms": round(latency_ms, 3), "message": text})
    return base


def monitor_buffers(*, now: float | None = None,
                    equities: dict[str, float] | None = None,
                    force: bool = False) -> list[dict]:
    """
    Poll every active account's drawdown buffer. This is the 30-second job
    invoked by ``main``; ``equities`` lets tests inject snapshots.
    """
    results = []
    for key, state in list(TRACKED.items()):
        equity = (equities or {}).get(key)
        if equity is None:
            equity = fetch_broker_snapshot(key)["current_equity"]
        results.append(monitor_account(key, state, equity, now=now, force=force))
    return results


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
        try:
            monitor_buffers()
        except Exception as e:  # buffer monitor must never kill the worker
            print(f"[buffer monitor error] {e}", flush=True)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
