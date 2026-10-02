"""
demo.py -- End-to-end offline demo (no GPU, no API keys, no network).

Scenario: a trader on a $50K Growth funded account.
  Day 1: EOD balance $51,000  -> trailing floor moves 48,000 -> 49,000
  Day 2: day PnL -$1,300      -> DLL soft breach (session paused, NOT failed)
The copilot runs the deterministic rule check, retrieves citations, and
explains. Then: a trailing-drawdown question, then a refusal probe.

Run: python3 demo.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from agent import run_agent, default_clients
from retrieval import HybridRetriever, load_corpus
from rule_engine import AccountState, update_dd_floor, summarize, evaluate_all

retriever = HybridRetriever(load_corpus())
clients = default_clients(mock=True)

print("=" * 70)
print("SCENARIO: $50K Growth funded account over two sessions")
print("=" * 70)

state = AccountState.new("growth_funded_50k")
print(f"\n[broker sync] start balance ${state.start_balance:,.0f}, "
      f"initial DD floor ${state.dd_floor:,.0f}")

floor, locked = update_dd_floor(state, 51000)
print(f"[broker sync] Day 1 EOD $51,000 -> trailing floor now ${floor:,.0f} "
      f"(locked={locked})")

print("\n--- Q1: trader asks after a -$1,300 day ---")
inputs = {"day_pnl": -1300.0, "current_equity": 49700.0,
          "daily_profits": [400.0, 300.0, 300.0],
          "trades": [{"hold_seconds": 30, "pnl": 100}, {"hold_seconds": 45, "pnl": -1400}],
          "current_balance": 49700.0}
res = run_agent("I lost $1,300 today on my Growth 50k funded account. Is my account failed?",
                retriever, clients, account_state=state, account_inputs=inputs)
print(res.answer)
print(f"\ncitations={res.citations} precision={res.citation_precision} "
      f"refused={res.refused} rounds={res.retrieval_rounds}")

print("\n--- Q2: trailing drawdown mechanics ---")
res = run_agent("When does my trailing drawdown stop trailing and lock?",
                retriever, clients)
print(res.answer)
print(f"\ncitations={res.citations} precision={res.citation_precision}")

print("\n--- Q3: refusal probe (not in rulebook) ---")
res = run_agent("What is the capital gains tax rate in Indonesia?",
                retriever, clients)
print(res.answer)
print(f"\nrefused={res.refused}")

print("\n--- route log (model tiers touched) ---")
print(res.route_log)
print("\nDemo complete. All LLM calls above used the offline mock; swap")
print("default_clients(mock=False) on a GPU host for real open-weight models.")
