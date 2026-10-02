"""
eval.py -- Eval harness for the retrieval layer (runs offline, no LLM needed).

Metrics:
  hit@k   -- fraction of questions where an expected chunk is in top-k
  mrr     -- mean reciprocal rank of the first expected chunk

LLM-side metrics (citation_precision, refusal_accuracy, consistency@temp0)
are defined in agent.py's eval hooks and require an LLM client; the harness
below covers everything measurable without one.

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
    out = os.path.join(base, "..", "evals", "retrieval_results.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=2)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
