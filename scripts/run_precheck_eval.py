#!/usr/bin/env python3
"""
run_precheck_eval.py -- Eval harness for the read-only payout pre-check.

Loads evals/precheck_eval.json (19 scenarios) and runs each through
``src/precheck.precheck_payout`` -- which calls the SAME deterministic engine as
POST /payout/request (check_eligibility -> compute_payout_amount -> decide)
without writing anything.

Metrics / gates:
  precheck_accuracy     status match + expected reason code present (target 1.0)
  latency_ms_p50        pre-check p50 over 200 iterations (target < 50 ms)
  explainer_precision   mean citation_precision of explain_payout_status
                        (target >= 0.95)
  idempotency_store_delta  must be 0 (pre-check never writes the engine store)

Run: python3 scripts/run_precheck_eval.py
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
    PAYOUT_IDEMPOTENCY,
    KYCStatus,
    PayoutAccount,
    PayoutRequest,
)
from precheck import precheck_payout  # noqa: E402
from rule_engine import AccountState  # noqa: E402

EVAL_PATH = os.path.join(ROOT, "evals", "precheck_eval.json")
OUT_PATH = os.path.join(ROOT, "evals", "precheck_eval_results.json")

try:
    from explainer import explain_payout_status  # noqa: E402
    HAVE_EXPLAINER = True
except Exception:  # pragma: no cover - explainer is optional for this gate
    HAVE_EXPLAINER = False


def _account(scenario: dict) -> PayoutAccount:
    state = AccountState.new(scenario["spec_key"])
    state.payouts_taken = scenario["payouts_taken"]
    equity = state.start_balance + scenario["equity_delta"]
    balance = state.start_balance + scenario["balance_delta"]
    return PayoutAccount.from_state(
        scenario["spec_key"], state, current_equity=equity,
        current_balance=balance)


def _kyc(scenario: dict) -> KYCStatus:
    status = scenario.get("kyc_status", "unverified")
    return KYCStatus(trader_id=scenario.get("trader_id", "eval"),
                     status=status,
                     method="eval-stub" if status == "verified" else None)


def _request(scenario: dict, request_id: str | None = None) -> PayoutRequest:
    return PayoutRequest(
        request_id=request_id or scenario["request_id"],
        account_key=scenario["spec_key"],
        trader_id=scenario.get("trader_id", "eval"),
        amount_usd=scenario["amount_usd"],
        trading_days=scenario["trading_days"],
        open_positions=scenario.get("open_positions", 0),
        daily_profits=scenario.get("daily_profits", []),
        current_equity=None,
        payout_method=scenario.get("payout_method", "ach"),
        profit_split_pct=scenario.get("profit_split_pct", 90.0),
    )


def run_case(scenario: dict, request_id: str | None = None):
    return precheck_payout(
        trader_id=scenario.get("trader_id", "eval"),
        account=_account(scenario),
        trades=scenario.get("trades", []),
        kyc=_kyc(scenario),
        request=_request(scenario, request_id),
        existing_request_ids=set(scenario.get("duplicate_request_ids", [])),
    )


def evaluate() -> dict:
    with open(EVAL_PATH) as f:
        suite = json.load(f)

    idem_before = len(PAYOUT_IDEMPOTENCY)
    cases = []
    for scenario in suite["scenarios"]:
        result = run_case(scenario)
        expected = scenario["expected_status"]
        codes = set(result.reason_codes)
        missing_codes = [c for c in scenario.get("expected_reason_codes", [])
                         if c not in codes]
        forbidden_present = [c for c in
                             scenario.get("expected_absent_reason_codes", [])
                             if c in codes]
        status_ok = result.status == expected
        codes_ok = not missing_codes and not forbidden_present
        cases.append({
            "id": scenario["id"], "category": scenario["category"],
            "expected": expected, "got": result.status,
            "expected_codes": scenario.get("expected_reason_codes", []),
            "got_codes": result.reason_codes,
            "missing_codes": missing_codes,
            "forbidden_codes_present": forbidden_present,
            "status_ok": status_ok, "codes_ok": codes_ok,
            "correct": status_ok and codes_ok,
            "buffer_remaining_usd": result.buffer_remaining_usd,
            "suggested_action": result.suggested_action,
            "reasons": result.reasons, "citations": result.citations,
        })

    n = len(cases)
    correct = sum(c["correct"] for c in cases)
    accuracy = correct / n if n else 0.0

    # Latency: 200 iterations of a fresh eligible request.
    base = next(s for s in suite["scenarios"]
                if s["expected_status"] == "ELIGIBLE")
    latencies = []
    for i in range(200):
        t0 = time.perf_counter()
        run_case(base, request_id=f"bench-precheck-{i}")
        latencies.append((time.perf_counter() - t0) * 1000.0)

    # Explainer citation precision over every case.
    explainer_precision = None
    explainer_detail = []
    if HAVE_EXPLAINER:
        precisions = []
        for scenario, case in zip(suite["scenarios"], cases):
            explanation = explain_payout_status(
                scenario.get("trader_id", "eval"), case["got"],
                case["reasons"])
            precisions.append(explanation.citation_precision)
            explainer_detail.append({
                "id": case["id"],
                "citation_precision": explanation.citation_precision,
                "citations": explanation.citations,
                "fallback": explanation.deterministic_fallback,
            })
        explainer_precision = (sum(precisions) / len(precisions)
                               if precisions else 0.0)

    idem_after = len(PAYOUT_IDEMPOTENCY)

    return {
        "n_scenarios": n,
        "precheck_accuracy": round(accuracy, 4),
        "n_correct": correct,
        "latency_ms_p50": round(statistics.median(latencies), 3),
        "latency_ms_mean": round(statistics.mean(latencies), 3),
        "latency_ms_max": round(max(latencies), 3),
        "explainer_citation_precision": (
            round(explainer_precision, 4)
            if explainer_precision is not None else None),
        "idempotency_store_delta": idem_after - idem_before,
        "cases": cases,
        "explainer_detail": explainer_detail,
    }


def main() -> None:
    res = evaluate()
    print("Payout pre-check eval (read-only)")
    print(f"  scenarios                 {res['n_scenarios']}")
    print(f"  precheck_accuracy         {res['precheck_accuracy']:.4f} "
          f"({res['n_correct']}/{res['n_scenarios']})")
    print(f"  latency p50 / mean / max  {res['latency_ms_p50']:.3f} / "
          f"{res['latency_ms_mean']:.3f} / {res['latency_ms_max']:.3f} ms "
          f"(p50 target < 50 ms)")
    if res["explainer_citation_precision"] is not None:
        print(f"  explainer citation_prec   "
              f"{res['explainer_citation_precision']:.4f} (>= 0.95 target)")
    print(f"  idempotency store delta   {res['idempotency_store_delta']} "
          f"(must be 0)")

    failures = [c for c in res["cases"] if not c["correct"]]
    if failures:
        print("\n  mismatches:")
        for c in failures:
            print(f"    {c['id']}: expected {c['expected']} got {c['got']} "
                  f"missing_codes={c['missing_codes']} "
                  f"forbidden_codes_present={c['forbidden_codes_present']}")

    with open(OUT_PATH, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\nwrote {OUT_PATH}")

    gates = [
        ("precheck_accuracy == 1.0", res["precheck_accuracy"] >= 1.0),
        ("latency p50 < 50 ms", res["latency_ms_p50"] < 50.0),
        ("idempotency store untouched", res["idempotency_store_delta"] == 0),
    ]
    if res["explainer_citation_precision"] is not None:
        gates.append(("explainer citation_precision >= 0.95",
                      res["explainer_citation_precision"] >= 0.95))
    failed = [name for name, ok in gates if not ok]
    if failed:
        print("\nPRECHECK EVAL GATE FAILED:")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nAll pre-check eval gates passed.")


if __name__ == "__main__":
    main()
