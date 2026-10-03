#!/usr/bin/env python3
"""
run_payout_eval.py -- Deterministic payout-decision eval harness.

Loads evals/payout_eval_set.json (17 scenarios: 5 clean approvals, 7
rejections, 3 edge cases, 2 adversarial) and runs each through the real
pipeline in src/payout.py:

    check_eligibility -> compute_payout_amount -> decide (via decide_idempotent)

Reported metrics:
  decision_accuracy    fraction of cases whose verdict matches expectation
                       (target 1.0 for the deterministic suite)
  citation_precision   micro share of a rejection's citations that justify it
                       (target >= 0.95)
  citation_recall      fraction of expected citations actually present
  rejection_without_citation_violations  must be 0 (hard invariant)

It also measures the mean end-to-end decision latency over 100 iterations.

Run: python3 scripts/run_payout_eval.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from payout import (  # noqa: E402
    KYCStatus,
    PAYOUT_IDEMPOTENCY,
    PayoutAccount,
    PayoutPolicy,
    PayoutRequest,
    check_eligibility,
    compute_payout_amount,
    decide_idempotent,
)
from rule_engine import ACCOUNT_SPECS, AccountState  # noqa: E402

EVAL_PATH = os.path.join(ROOT, "evals", "payout_eval_set.json")
OUT_PATH = os.path.join(ROOT, "evals", "payout_eval_results.json")


def _account(spec_key: str, payouts_taken: int, equity_delta: float,
             balance_delta: float) -> PayoutAccount:
    state = AccountState.new(spec_key)
    state.payouts_taken = payouts_taken
    equity = state.start_balance + equity_delta
    balance = state.start_balance + balance_delta
    return PayoutAccount.from_state(spec_key, state, current_equity=equity,
                                    current_balance=balance)


def _request(scenario: dict, override: dict | None = None) -> PayoutRequest:
    fields = {
        "request_id": scenario["request_id"],
        "account_key": scenario["spec_key"],
        "amount_usd": scenario["amount_usd"],
        "trading_days": scenario["trading_days"],
        "open_positions": scenario.get("open_positions", 0),
        "daily_profits": scenario.get("daily_profits", []),
        "current_equity": None,
        "payout_method": scenario.get("payout_method", "ach"),
        "profit_split_pct": scenario.get("profit_split_pct", 90.0),
    }
    if override:
        fields.update(override)
    return PayoutRequest(**fields)


def _kyc(scenario: dict) -> KYCStatus:
    status = scenario.get("kyc_status", "unverified")
    return KYCStatus(trader_id="eval", status=status,
                     method="eval-stub" if status == "verified" else None)


def run_case(scenario: dict, override: dict | None = None) -> dict:
    """Run one request and return {'decision', 'citations', 'reasons'}."""
    account = _account(scenario["spec_key"], scenario["payouts_taken"],
                       scenario["equity_delta"], scenario["balance_delta"])
    req = _request(scenario, override)
    decision = decide_idempotent(req, account, scenario.get("trades", []),
                                 _kyc(scenario), PayoutPolicy())
    return {
        "decision": decision.decision,
        "citations": list(decision.citations),
        "reasons": list(decision.reasons),
    }


def evaluate() -> dict:
    with open(EVAL_PATH) as f:
        suite = json.load(f)

    cases: list[dict] = []
    for scenario in suite["scenarios"]:
        # Reset the idempotency store per scenario so duplicates are explicit.
        PAYOUT_IDEMPOTENCY.clear()
        first = run_case(scenario)
        cases.append({
            "id": scenario["id"], "part": "primary",
            "category": scenario["category"],
            "expected": scenario["expected_decision"],
            "got": first["decision"],
            "expected_citations": scenario["expected_citations"],
            "got_citations": first["citations"],
            "correct": first["decision"] == scenario["expected_decision"],
        })

        if "second_request" in scenario:
            second = run_case(scenario, scenario["second_request"])
            reason_code_ok = any(
                scenario["second_expected_reason_code"] in r
                for r in second["reasons"])
            cases.append({
                "id": scenario["id"], "part": "second",
                "category": scenario["category"],
                "expected": scenario["second_expected_decision"],
                "got": second["decision"],
                "expected_citations": scenario["second_expected_citations"],
                "got_citations": second["citations"],
                "correct": (second["decision"]
                            == scenario["second_expected_decision"]
                            and reason_code_ok),
                "reason_code_ok": reason_code_ok,
            })

    total = len(cases)
    correct = sum(c["correct"] for c in cases)
    decision_accuracy = correct / total if total else 0.0

    # Citation precision / recall on every REJECTED case (no rejection may
    # carry a citation that does not justify it, and none may be uncited).
    rej = [c for c in cases if c["got"] == "REJECTED"]
    inter_total = 0
    got_total = 0
    exp_total = 0
    uncited_violations = 0
    per_case = []
    for c in rej:
        exp = set(c["expected_citations"])
        got = set(c["got_citations"])
        if not got:
            uncited_violations += 1
        inter = len(exp & got)
        inter_total += inter
        got_total += len(got)
        exp_total += len(exp)
        precision = (inter / len(got)) if got else 0.0
        recall = (inter / len(exp)) if exp else 1.0
        per_case.append({"id": c["id"], "part": c["part"],
                         "citations": sorted(got), "precision": precision,
                         "recall": recall})
    citation_precision = inter_total / got_total if got_total else 1.0
    citation_recall = inter_total / exp_total if exp_total else 1.0

    # Latency: 100 iterations of the pure decision pipeline with fresh ids.
    latencies_ms = []
    for i in range(100):
        scenario = suite["scenarios"][0]
        PAYOUT_IDEMPOTENCY.clear()
        account = _account(scenario["spec_key"], scenario["payouts_taken"],
                           scenario["equity_delta"], scenario["balance_delta"])
        req = _request(dict(scenario, request_id=f"bench-{i}"))
        t0 = time.perf_counter()
        elig = check_eligibility(account, [], _kyc(scenario), req)
        amount = compute_payout_amount(account, elig.current_profit, 90.0)
        decide_idempotent(req, account, [], _kyc(scenario), PayoutPolicy())
        latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    return {
        "n_scenarios": len(suite["scenarios"]),
        "n_cases": total,
        "decision_accuracy": round(decision_accuracy, 4),
        "citation_precision": round(citation_precision, 4),
        "citation_recall": round(citation_recall, 4),
        "rejection_without_citation_violations": uncited_violations,
        "n_rejection_cases": len(rej),
        "latency_ms_mean": round(statistics.mean(latencies_ms), 3),
        "latency_ms_p50": round(statistics.median(latencies_ms), 3),
        "latency_ms_min": round(min(latencies_ms), 3),
        "latency_ms_max": round(max(latencies_ms), 3),
        "cases": cases,
        "rejection_citation_detail": per_case,
    }


def main() -> None:
    res = evaluate()
    print("Payout decision eval (deterministic engine)")
    print(f"  scenarios                {res['n_scenarios']}")
    print(f"  cases (incl. adversarial) {res['n_cases']}")
    print(f"  decision_accuracy        {res['decision_accuracy']:.4f}")
    print(f"  citation_precision       {res['citation_precision']:.4f} "
          f"(>= 0.95 target)")
    print(f"  citation_recall          {res['citation_recall']:.4f}")
    print(f"  uncited rejections       "
          f"{res['rejection_without_citation_violations']}")
    print(f"  decision latency (100x)  mean {res['latency_ms_mean']:.3f} ms "
          f"| p50 {res['latency_ms_p50']:.3f} | max {res['latency_ms_max']:.3f}")

    failures = [c for c in res["cases"] if not c["correct"]]
    if failures:
        print("\n  mismatches:")
        for c in failures:
            print(f"    {c['id']} [{c['part']}] expected {c['expected']} "
                  f"got {c['got']}")

    with open(OUT_PATH, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\nwrote {OUT_PATH}")

    gates = [
        ("decision_accuracy == 1.0", res["decision_accuracy"] >= 1.0),
        ("citation_precision >= 0.95", res["citation_precision"] >= 0.95),
        ("no rejection without citation",
         res["rejection_without_citation_violations"] == 0),
    ]
    failed = [name for name, ok in gates if not ok]
    if failed:
        print("\nPAYOUT EVAL GATE FAILED:")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nAll payout eval gates passed.")


if __name__ == "__main__":
    main()