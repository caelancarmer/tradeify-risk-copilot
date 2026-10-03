"""Unit tests for explainer.py (deliverable D).

Verifies the hard rule: the LLM only rephrases; it can never introduce a number
or a citation that the deterministic layer did not provide.

Run: python3 tests/test_explainer.py
"""

import os
import sys

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from explainer import Explanation, chunks_for_reasons, explain_payout_status

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


class InventedNumberClient:
    """Malicious/broken LLM that fabricates a number and no valid citation."""

    def generate(self, system, user):
        return "The trader has a buffer of 99999 dollars and can pay out. [chunk_bogus]"


class UncitedClient:
    def generate(self, system, user):
        return "Everything looks fine, no citation here."


def main():
    print("\n=== explainer tests ===")
    reasons = [
        "[PAYOUT_CONSISTENCY_BREACH] Best day $900 is 81.8% of total profit "
        "$1,100, over the 35% cap.",
        "[PAYOUT_MIN_TRADING_DAYS] 2 trading day(s) completed; at least 5 are "
        "required.",
    ]
    chunks = chunks_for_reasons(reasons)
    check("maps consistency reason to chunk_consistency",
          "chunk_consistency" in chunks, str(chunks))
    check("maps min-days reason to chunk_payouts",
          "chunk_payouts" in chunks, str(chunks))

    exp = explain_payout_status("trader_x", "NOT_ELIGIBLE", reasons)
    check("returns Explanation model", isinstance(exp, Explanation))
    check("has citations", bool(exp.citations), str(exp.citations))
    check("citation_precision == 1.0", exp.citation_precision == 1.0,
          str(exp.citation_precision))
    check("numbers unchanged", exp.numbers_unchanged is True)
    check("has suggested action", bool(exp.suggested_action))
    check("suggested action mentions consistency",
          "consistency" in exp.suggested_action.lower(),
          exp.suggested_action)

    eligible = explain_payout_status(
        "trader_y", "ELIGIBLE", ["[PAYOUT_APPROVED] Funded growth is eligible: "
                                 "$200.00 requested."])
    check("eligible explanation cites a chunk", bool(eligible.citations))
    check("eligible action is to submit",
          "submit" in eligible.suggested_action.lower(),
          eligible.suggested_action)

    # Adversarial: LLM invents a number -> deterministic fallback.
    bad = explain_payout_status("trader_z", "NOT_ELIGIBLE", reasons,
                                client=InventedNumberClient())
    check("invented number triggers fallback",
          bad.deterministic_fallback is True)
    check("invented number not present in fallback",
          "99999" not in bad.explanation, bad.explanation)
    check("fallback numbers still unchanged", bad.numbers_unchanged is True)
    check("fallback keeps citations", bool(bad.citations))

    # Adversarial: LLM cites nothing -> fallback.
    uncited = explain_payout_status("trader_w", "MANUAL_REVIEW",
                                    ["[PAYOUT_FIRST_PAYOUT_MANUAL] First "
                                     "payout requires manual review."],
                                    client=UncitedClient())
    check("uncited output triggers fallback",
          uncited.deterministic_fallback is True)
    check("uncited fallback has citations", bool(uncited.citations))
    check("manual review action mentions operator",
          "operator" in uncited.suggested_action.lower(),
          uncited.suggested_action)

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
