#!/usr/bin/env python3
"""
run_buffer_eval.py -- Eval harness for the buffer monitor (deliverable C).

Loads evals/buffer_alert_eval.json (10 scenarios) and drives
``worker.monitor_account`` with deterministic timestamps and injected equity.

Metrics / gates:
  scenario_accuracy     all steps in every scenario correct (target 10/10)
  step_accuracy         per-step severity/alerted/suppressed match
  max_sent_latency_ms   must be < 2000 ms (alert sent < 2 s from detection)
  anti_spam_suppressed  count of correctly suppressed second alerts

Run: python3 scripts/run_buffer_eval.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import alert_store  # noqa: E402
import worker  # noqa: E402
from rule_engine import ACCOUNT_SPECS, AccountState  # noqa: E402

EVAL_PATH = os.path.join(ROOT, "evals", "buffer_alert_eval.json")
OUT_PATH = os.path.join(ROOT, "evals", "buffer_alert_eval_results.json")
BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def evaluate() -> dict:
    with open(EVAL_PATH) as f:
        suite = json.load(f)

    scenarios = []
    all_steps = 0
    correct_steps = 0
    sent_latencies: list[float] = []
    suppressed_ok = 0

    for scenario in suite["scenarios"]:
        alert_store.clear()
        worker.LAST_BUFFER_ALERT_AT.clear()
        spec = ACCOUNT_SPECS[scenario["spec_key"]]
        dd = spec.trailing_drawdown
        steps_out = []
        scenario_ok = True

        for step in scenario["steps"]:
            state = AccountState.new(scenario["spec_key"])
            buffer_usd = step["buffer_pct"] / 100.0 * dd
            equity = state.dd_floor + buffer_usd
            now_ts = BASE_TIME.timestamp() + step.get("advance_seconds", 0)
            result = worker.monitor_account(
                step["account_key"], state, equity,
                trader_id=f"trader_{step['account_key']}", now=now_ts)
            ok = (result["severity"] == step["expect_severity"]
                  and result["alerted"] == step["expect_alerted"]
                  and result["suppressed"] == step["expect_suppressed"])
            if result["alerted"]:
                sent_latencies.append(result["latency_ms"])
            if step["expect_suppressed"] and result["suppressed"]:
                suppressed_ok += 1
            all_steps += 1
            correct_steps += 1 if ok else 0
            scenario_ok = scenario_ok and ok
            steps_out.append({
                "account_key": step["account_key"],
                "buffer_pct": step["buffer_pct"],
                "expected_severity": step["expect_severity"],
                "got_severity": result["severity"],
                "expected_alerted": step["expect_alerted"],
                "got_alerted": result["alerted"],
                "expected_suppressed": step["expect_suppressed"],
                "got_suppressed": result["suppressed"],
                "channel": result["channel"],
                "latency_ms": result["latency_ms"],
                "correct": ok,
            })

        scenarios.append({
            "id": scenario["id"], "category": scenario["category"],
            "correct": scenario_ok, "steps": steps_out,
        })

    n_scenarios = len(scenarios)
    correct_scenarios = sum(1 for s in scenarios if s["correct"])
    max_latency = max(sent_latencies) if sent_latencies else 0.0

    return {
        "n_scenarios": n_scenarios,
        "scenario_accuracy": round(correct_scenarios / n_scenarios, 4)
        if n_scenarios else 0.0,
        "n_scenarios_correct": correct_scenarios,
        "n_steps": all_steps,
        "step_accuracy": round(correct_steps / all_steps, 4)
        if all_steps else 0.0,
        "max_sent_latency_ms": round(max_latency, 3),
        "mean_sent_latency_ms": round(
            statistics.mean(sent_latencies), 3) if sent_latencies else 0.0,
        "n_sent_alerts": len(sent_latencies),
        "anti_spam_suppressed_ok": suppressed_ok,
        "scenarios": scenarios,
    }


def main() -> None:
    res = evaluate()
    print("Buffer alert eval (deliverable C)")
    print(f"  scenarios              {res['n_scenarios']}")
    print(f"  scenario_accuracy      {res['scenario_accuracy']:.4f} "
          f"({res['n_scenarios_correct']}/{res['n_scenarios']})")
    print(f"  step_accuracy          {res['step_accuracy']:.4f} "
          f"({res['n_steps']} steps)")
    print(f"  sent alerts            {res['n_sent_alerts']}")
    print(f"  suppressed (anti-spam) {res['anti_spam_suppressed_ok']}")
    print(f"  max sent latency       {res['max_sent_latency_ms']:.3f} ms "
          f"(< 2000 ms target)")

    failures = [s for s in res["scenarios"] if not s["correct"]]
    if failures:
        print("\n  mismatches:")
        for s in failures:
            for st in s["steps"]:
                if not st["correct"]:
                    print(f"    {s['id']} {st['account_key']} "
                          f"pct={st['buffer_pct']} expected "
                          f"{st['expected_severity']}/alerted="
                          f"{st['expected_alerted']}/suppressed="
                          f"{st['expected_suppressed']} got "
                          f"{st['got_severity']}/alerted={st['got_alerted']}/"
                          f"suppressed={st['got_suppressed']}")

    with open(OUT_PATH, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\nwrote {OUT_PATH}")

    gates = [
        ("scenario_accuracy == 1.0", res["scenario_accuracy"] >= 1.0),
        ("n_scenarios == 10", res["n_scenarios"] == 10),
        ("sent latency < 2000 ms", res["max_sent_latency_ms"] < 2000.0),
    ]
    failed = [name for name, ok in gates if not ok]
    if failed:
        print("\nBUFFER EVAL GATE FAILED:")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nAll buffer alert eval gates passed.")


if __name__ == "__main__":
    main()
