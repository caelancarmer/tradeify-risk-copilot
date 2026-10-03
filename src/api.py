"""
api.py -- FastAPI service: the glue layer between LLM, DB, broker, and product.

Endpoints:
  GET  /health
  POST /ask                       {question, account_key?} -> copilot answer
  POST /accounts                  {account_key, spec_key} -> register account
  POST /evaluate                  {account_key, ...snapshot} -> rule findings
  POST /sync                      {account_key, eod_balance} -> advance DD floor

Payout automation (Option 1):
  POST /payout/request            trader submits a payout request
  GET  /payout/queue              admin: list MANUAL_REVIEW requests
  GET  /payout/{request_id}       status of one request
  POST /payout/{request_id}/approve   admin approve (X-Admin-Key)
  POST /payout/{request_id}/reject    admin reject  (X-Admin-Key)

In-memory stores below are the DEMO backend. Production replaces them with the
SQLAlchemy models in payout_models.py via Alembic migrations -- the API
signatures stay identical.

Run: uvicorn src.api:app --host 0.0.0.0 --port 8000
"""

import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from agent import run_agent, default_clients
from payout import (
    CITE_PAYOUTS,
    Decision,
    KYCStatus,
    PayoutAccount,
    PayoutRequest,
    request_fingerprint,
)
from retrieval import HybridRetriever, load_corpus
from rule_engine import AccountState, evaluate_all, summarize, update_dd_floor

app = FastAPI(title="Tradeify Risk Copilot")

retriever = HybridRetriever(load_corpus())
clients = default_clients(mock=os.environ.get("MOCK_LLM", "1") == "1")
ACCOUNTS: dict[str, AccountState] = {}

# ---------------------------------------------------------------------------
# In-memory payout stores (demo backend; Postgres in production)
# ---------------------------------------------------------------------------
PAYOUT_REQUESTS: dict[str, dict] = {}
PAYOUT_DECISIONS: dict[str, dict] = {}
AUDIT_LOG: list[dict] = []

# ADMIN_API_KEY: the default 'dev-admin-key' is for the demo ONLY. Production
# MUST set a strong ADMIN_API_KEY env var; the default is deliberately obvious.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "dev-admin-key")


def _audit(entity_type: str, entity_id: str, action: str, payload: dict,
           actor: str = "system") -> dict:
    entry = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action": action,
        "payload": payload,
        "actor": actor,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    AUDIT_LOG.append(entry)
    return entry


def _require_admin(x_admin_key: Optional[str]) -> None:
    # DEMO default only; see ADMIN_API_KEY comment above.
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(401, "invalid or missing X-Admin-Key")


class AskIn(BaseModel):
    question: str
    account_key: Optional[str] = None


class EvalIn(BaseModel):
    account_key: str
    day_pnl: float = 0.0
    current_equity: float = 0.0
    daily_profits: list[float] = []
    trades: list[dict] = []
    current_balance: float = 0.0


class SyncIn(BaseModel):
    account_key: str
    eod_balance: float


class AccountIn(BaseModel):
    account_key: str
    spec_key: str


class PayoutRequestIn(BaseModel):
    request_id: str
    account_key: str
    amount_usd: float
    current_equity: float
    kyc_verified: bool = False
    trading_days: int = 0
    open_positions: int = 0
    daily_profits: list[float] = []
    trader_id: str = "trader"
    payout_method: str = "ach"
    profit_split_pct: float = 90.0
    trades: list[dict] = []


@app.post("/accounts")
def register_account(body: AccountIn):
    from rule_engine import ACCOUNT_SPECS
    if body.spec_key not in ACCOUNT_SPECS:
        raise HTTPException(
            400, f"unknown spec_key; valid: {sorted(ACCOUNT_SPECS)}")
    ACCOUNTS[body.account_key] = AccountState.new(body.spec_key)
    return {"account_key": body.account_key, "spec_key": body.spec_key}


@app.get("/health")
def health():
    return {"ok": True, "mock_llm": os.environ.get("MOCK_LLM", "1") == "1",
            "accounts": list(ACCOUNTS),
            "payout_backend": "in-memory (demo; Postgres via alembic in prod)"}


@app.post("/ask")
def ask(body: AskIn):
    state = ACCOUNTS.get(body.account_key) if body.account_key else None
    res = run_agent(body.question, retriever, clients, account_state=state)
    return {"answer": res.answer, "citations": res.citations,
            "citation_precision": res.citation_precision,
            "refused": res.refused, "route_log": res.route_log}


@app.post("/evaluate")
def evaluate(body: EvalIn):
    state = ACCOUNTS.get(body.account_key)
    if not state:
        raise HTTPException(404, "unknown account_key")
    rep = summarize(evaluate_all(
        state, day_pnl=body.day_pnl, current_equity=body.current_equity,
        daily_profits=body.daily_profits, trades=body.trades,
        current_balance=body.current_balance))
    return rep


@app.post("/sync")
def sync(body: SyncIn):
    state = ACCOUNTS.get(body.account_key)
    if not state:
        raise HTTPException(404, "unknown account_key")
    floor, locked = update_dd_floor(state, body.eod_balance)
    return {"dd_floor": floor, "locked": locked}


# ---------------------------------------------------------------------------
# Payout automation endpoints
# ---------------------------------------------------------------------------

@app.post("/payout/request")
def payout_request(body: PayoutRequestIn):
    """Trader submits a payout request; the deterministic engine decides."""
    state = ACCOUNTS.get(body.account_key)
    if not state:
        raise HTTPException(404, f"unknown account_key {body.account_key!r}; "
                                 f"register it via POST /accounts first")
    account = PayoutAccount.from_state(
        body.account_key, state, current_equity=body.current_equity,
        current_balance=body.current_equity)
    req = PayoutRequest(
        request_id=body.request_id, account_key=body.account_key,
        trader_id=body.trader_id, amount_usd=body.amount_usd,
        kyc_verified=body.kyc_verified, trading_days=body.trading_days,
        open_positions=body.open_positions, daily_profits=body.daily_profits,
        current_equity=body.current_equity, payout_method=body.payout_method,
        profit_split_pct=body.profit_split_pct)
    kyc = KYCStatus(
        trader_id=body.trader_id,
        status="verified" if body.kyc_verified else "unverified",
        method="api-stub" if body.kyc_verified else None,
        verified_at=(datetime.now(timezone.utc).isoformat()
                     if body.kyc_verified else None))

    # Idempotency at the API boundary: first write wins for the request record.
    # A replayed request_id with a DIFFERENT payload is an adversarial
    # idempotency conflict; it is rejected with a citation and the original
    # record is left untouched. Identical replays fall through to the stored
    # decision (also via the worker, which returns cached decisions).
    existing = PAYOUT_REQUESTS.get(req.request_id)
    incoming_fp = request_fingerprint(req)
    if existing is not None:
        if existing.get("fingerprint") != incoming_fp:
            conflict = Decision(
                request_id=req.request_id, decision="REJECTED",
                amount_usd=0.0,
                reasons=[
                    "[PAYOUT_IDEMPOTENCY_CONFLICT] request_id "
                    f"{req.request_id!r} was already submitted with a "
                    "different payload; refusing to re-decide. Original "
                    "request and decision are unchanged.",
                ],
                citations=[CITE_PAYOUTS],
                note="rejected: idempotency conflict", decided_by="engine")
            record = conflict.model_dump()
            # Do NOT overwrite the original decision.
            _audit("payout_decision", req.request_id, "REJECTED",
                   payload={"reason": "idempotency_conflict",
                            "incoming": req.model_dump()}, actor="system")
            return {
                "request_id": conflict.request_id,
                "decision": conflict.decision, "amount_usd": 0.0,
                "reasons": conflict.reasons, "citations": conflict.citations,
                "note": conflict.note, "decided_at": conflict.decided_at,
                "decided_by": conflict.decided_by,
            }
    else:
        PAYOUT_REQUESTS[req.request_id] = {
            "request": req, "account": account, "kyc": kyc,
            "trades": body.trades, "fingerprint": incoming_fp,
        }
        _audit("payout_request", req.request_id, "SUBMITTED",
               req.model_dump(), actor=body.trader_id)

    # Delegate the deterministic pipeline to the worker function.
    from worker import process_payout_request
    decision = process_payout_request(req.request_id)
    if decision is None:
        raise HTTPException(500, "payout processing failed")
    return {
        "request_id": decision.request_id,
        "decision": decision.decision,
        "amount_usd": decision.amount_usd,
        "reasons": decision.reasons,
        "citations": decision.citations,
        "note": decision.note,
        "decided_at": decision.decided_at,
        "decided_by": decision.decided_by,
    }


@app.get("/payout/queue")
def payout_queue(x_admin_key: Optional[str] = Header(default=None)):
    """Admin: list requests whose decision is MANUAL_REVIEW."""
    _require_admin(x_admin_key)
    items = [
        {"request_id": rid, **d}
        for rid, d in PAYOUT_DECISIONS.items()
        if d.get("decision") == "MANUAL_REVIEW"
    ]
    return {"count": len(items), "queue": items}


@app.get("/payout/{request_id}")
def payout_status(request_id: str):
    req = PAYOUT_REQUESTS.get(request_id)
    decision = PAYOUT_DECISIONS.get(request_id)
    if req is None and decision is None:
        raise HTTPException(404, "unknown request_id")
    return {"request_id": request_id,
            "request": req["request"].model_dump() if req else None,
            "decision": decision}


@app.post("/payout/{request_id}/approve")
def payout_approve(request_id: str,
                   x_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_admin_key)
    decision = PAYOUT_DECISIONS.get(request_id)
    if decision is None:
        raise HTTPException(404, "unknown request_id")
    decision = dict(decision)
    decision["decision"] = "APPROVED"
    decision["decided_by"] = "admin"
    decision["note"] = (decision.get("note", "") +
                        " | admin-approved").strip(" |")
    decision["decided_at"] = datetime.now(timezone.utc).isoformat()
    PAYOUT_DECISIONS[request_id] = decision
    _audit("payout_decision", request_id, "APPROVED",
           {"decided_by": "admin"}, actor="admin")
    return {"request_id": request_id, "decision": decision["decision"],
            "decided_by": decision["decided_by"]}


@app.post("/payout/{request_id}/reject")
def payout_reject(request_id: str,
                  x_admin_key: Optional[str] = Header(default=None)):
    _require_admin(x_admin_key)
    decision = PAYOUT_DECISIONS.get(request_id)
    if decision is None:
        raise HTTPException(404, "unknown request_id")
    decision = dict(decision)
    decision["decision"] = "REJECTED"
    decision["amount_usd"] = 0.0
    decision["decided_by"] = "admin"
    decision["note"] = (decision.get("note", "") +
                        " | admin-rejected").strip(" |")
    decision["decided_at"] = datetime.now(timezone.utc).isoformat()
    PAYOUT_DECISIONS[request_id] = decision
    _audit("payout_decision", request_id, "REJECTED",
           {"decided_by": "admin"}, actor="admin")
    return {"request_id": request_id, "decision": decision["decision"],
            "decided_by": decision["decided_by"]}