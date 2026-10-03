"""
eval.py -- Eval harness for the Risk Copilot (the "harness" from the DeepSeek plan).

Two suites, both runnable offline with the deterministic MockLLM:
  Retrieval metrics: hit@k, MRR over 30 trader questions.
  LLM behavior metrics (the ones that matter in the interview):
    - answer_hit:        fraction of questions whose answer cites an expected chunk
    - citation_precision: micro-average share of citations that are real retrieved chunks
    - grounded_rate:      fraction of answers that are non-refused AND precision==1.0
    - refusal_accuracy:   fraction of out-of-rulebook probes correctly refused
    - consistency@temp0:  fraction of repeated runs with byte-identical answers

With a real LLM backend (default_clients(mock=False)), the same harness
measures the real model. The mock run below establishes the harness itself.

Run: python3 src/eval.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from retrieval import HybridRetriever, load_corpus


def run_retrieval_eval(retriever: HybridRetriever, questions: list[dict],
                       methods=("bm25", "vector", "hybrid"), k: int = 5) -> dict:
    results: dict = {}
    for method in methods:
        hits1 = hits5 = 0
        rr_sum = 0.0
        per_q = []
        for item in questions:
            ranked = [h["id"] for h in retriever.retrieve(item["q"], k=max(k, 10), method=method)]
            expected = set(item["expected_chunks"])
            rank = next((i + 1 for i, cid in enumerate(ranked) if cid in expected), None)
            if rank is not None:
                rr_sum += 1.0 / rank
                if rank == 1:
                    hits1 += 1
                if rank <= k:
                    hits5 += 1
            per_q.append({"q": item["q"][:60], "rank": rank})
        n = len(questions)
        results[method] = {
            "hit@1": round(hits1 / n, 3),
            f"hit@{k}": round(hits5 / n, 3),
            "mrr": round(rr_sum / n, 3),
            "n": n,
        }
    return results


def run_llm_eval(retriever: HybridRetriever, questions: list[dict],
                 refusal_probes: list[str], n_consistency_runs: int = 3) -> dict:
    """End-to-end agent eval with the offline mock LLM (deterministic)."""
    from agent import run_agent, default_clients
    clients = default_clients(mock=True)

    hits = 0
    precisions: list[float] = []
    grounded = 0
    per_q = []
    for item in questions:
        res = run_agent(item["q"], retriever, clients)
        expected = set(item["expected_chunks"])
        cited_ok = bool(expected & set(res.citations))
        hits += cited_ok
        precisions.append(res.citation_precision)
        grounded += (not res.refused and res.citation_precision == 1.0)
        per_q.append({"q": item["q"][:60], "cited_expected": cited_ok,
                      "precision": res.citation_precision, "refused": res.refused})
    n = len(questions)

    refused = 0
    for probe in refusal_probes:
        res = run_agent(probe, retriever, clients)
        refused += res.refused

    consistent = 0
    sample = questions[:5]
    for item in sample:
        answers = {run_agent(item["q"], retriever, clients).answer
                   for _ in range(n_consistency_runs)}
        consistent += (len(answers) == 1)

    return {
        "answer_hit": round(hits / n, 3),
        "citation_precision_micro": round(sum(precisions) / n, 3),
        "grounded_rate": round(grounded / n, 3),
        "refusal_accuracy": round(refused / len(refusal_probes), 3),
        "consistency@temp0": round(consistent / len(sample), 3),
        "n_questions": n, "n_probes": len(refusal_probes),
        "note": "mock-LLM run: validates the harness; re-run with mock=False for real models",
    }


def main() -> None:
    base = os.path.dirname(__file__)
    with open(os.path.join(base, "..", "evals", "eval_set.json")) as f:
        eval_set = json.load(f)
    retriever = HybridRetriever(load_corpus())

    res = run_retrieval_eval(retriever, eval_set["questions"])
    print("Retrieval eval (30 trader questions, 9-chunk corpus)")
    print(f"{'method':10s} {'hit@1':>7s} {'hit@5':>7s} {'mrr':>7s}")
    for m, r in res.items():
        print(f"{m:10s} {r['hit@1']:7.3f} {r['hit@5']:7.3f} {r['mrr']:7.3f}")

    print("\nLLM behavior eval (offline mock, deterministic)")
    llm = run_llm_eval(retriever, eval_set["questions"], eval_set["refusal_probes"])
    for k in ("answer_hit", "citation_precision_micro", "grounded_rate",
              "refusal_accuracy", "consistency@temp0"):
        print(f"  {k:26s} {llm[k]:.3f}")
    print(f"  ({llm['n_questions']} questions, {llm['n_probes']} refusal probes)")

    out = os.path.join(base, "..", "evals", "retrieval_results.json")
    with open(out, "w") as f:
        json.dump({"retrieval": res, "llm_behavior": llm}, f, indent=2)
    print(f"\nwrote {out}")

    # Regression gates: CI fails loudly if quality drops.
    # Thresholds sit below the verified 2026-10-03 numbers
    # (hybrid 0.867/0.925, citation_precision 0.967, refusal 1.0)
    # with enough margin for legitimate variance.
    gates = [
        ("hybrid hit@1 >= 0.80", res["hybrid"]["hit@1"] >= 0.80),
        ("hybrid MRR >= 0.85", res["hybrid"]["mrr"] >= 0.85),
        ("citation_precision >= 0.90", llm["citation_precision_micro"] >= 0.90),
        ("refusal_accuracy >= 0.95", llm["refusal_accuracy"] >= 0.95),
        ("consistency@temp0 >= 0.95", llm["consistency@temp0"] >= 0.95),
    ]
    failed = [name for name, ok in gates if not ok]
    if failed:
        print("\nREGRESSION GATE FAILED:")
        for name in failed:
            print(f"  - {name}")
        raise SystemExit(1)
    print("\nAll regression gates passed.")


if __name__ == "__main__":
    main()
