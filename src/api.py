"""
api.py -- FastAPI service: the glue layer between LLM, DB, broker, and product.

Endpoints:
  GET  /health
  POST /ask        {question, account_key?} -> copilot answer + citations
  POST /evaluate   {account_key, snapshot}  -> deterministic rule findings
  POST /sync       {account_key, eod_balance} -> advance trailing-DD floor

Run: uvicorn src.api:app --host 0.0.0.0 --port 8000
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

from agent import run_agent, default_clients
from retrieval import HybridRetriever, load_corpus
from rule_engine import AccountState, evaluate_all, summarize, update_dd_floor

app = FastAPI(title="Tradeify Risk Copilot")

retriever = HybridRetriever(load_corpus())
clients = default_clients(mock=os.environ.get("MOCK_LLM", "1") == "1")
ACCOUNTS: dict[str, AccountState] = {}


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


@app.get("/health")
def health():
    return {"ok": True, "mock_llm": os.environ.get("MOCK_LLM", "1") == "1",
            "accounts": list(ACCOUNTS)}


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
