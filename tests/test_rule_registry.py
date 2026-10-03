"""Unit tests for rule_registry.py (deliverable A).

Covers:
  - >= 20 verified entries
  - every citation_chunk_id resolves to a real retrieval-corpus chunk
  - load-from-database path (SQLite file) and in-memory fallback
  - filtering by account type / phase
  - no drift from rule_engine.ACCOUNT_SPECS thresholds

Run: python3 tests/test_rule_registry.py
"""

import os
import sys
import tempfile

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import rule_registry as rr

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def main():
    print("\n=== rule_registry tests ===")

    report = rr.validate_registry()
    check("registry is valid", report["valid"], str(report))
    check("at least 20 entries", report["count"] >= 20, f"got {report['count']}")
    check("no invalid citations", not report["invalid_citations"],
          str(report["invalid_citations"]))
    check("no duplicate rule_ids", not report["duplicate_rule_ids"],
          str(report["duplicate_rule_ids"]))
    check("no missing required fields", not report["missing_fields"],
          str(report["missing_fields"]))

    drift = rr.validate_against_account_specs()
    check("no drift vs ACCOUNT_SPECS", drift["valid"], str(drift["mismatches"]))

    # Every citation must be a real corpus chunk.
    corpus_ids = rr.corpus_chunk_ids()
    bad = [r["citation_chunk_id"] for r in rr.DEFAULT_RULES
           if r["citation_chunk_id"] not in corpus_ids]
    check("all default citations in corpus", not bad, str(bad))

    # load_rules() must exercise the DB path (in-memory SQLite fallback).
    loaded = rr.load_rules()
    check("load_rules returns all entries", len(loaded) == len(rr.DEFAULT_RULES),
          f"{len(loaded)} vs {len(rr.DEFAULT_RULES)}")
    check("load_rules returns dicts with citation", 
          all("citation_chunk_id" in r for r in loaded))

    # Explicit file-backed SQLite database round-trip.
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "registry.db")
        url = f"sqlite:///{db_path}"
        inserted = rr.seed_rules(rr.create_engine(url))
        check("seed inserts rows", inserted == len(rr.DEFAULT_RULES),
              f"inserted {inserted}")
        from_db = rr.load_rules(db_url=url)
        check("load_rules(db_url) reads from database",
              len(from_db) == len(rr.DEFAULT_RULES), f"got {len(from_db)}")
        # Re-seeding is idempotent.
        check("re-seed is idempotent",
              rr.seed_rules(rr.create_engine(url)) == 0)

    growth_funded = rr.rules_for("growth", phase="funded")
    check("rules_for filters by family+phase",
          growth_funded and all(
              r["account_type"] in ("growth", "all") and
              r["phase"] in ("funded", "all") for r in growth_funded))
    check("global rules included for any family",
          any(r["rule_id"] == "all_flat_by_time" for r in growth_funded))

    rule = rr.get_rule("growth_funded_consistency")
    check("get_rule finds growth consistency", rule is not None)
    check("growth consistency threshold is 35.0",
          rule and rule["threshold"] == "35.0", str(rule))
    check("lightning progressive threshold present",
          rr.get_rule("lightning_consistency_progressive")["threshold"]
          == "20 / 25 / 30 by payout count")

    print(f"\nPASS: {PASS}  FAIL: {FAIL}")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
