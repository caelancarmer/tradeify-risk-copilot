"""
ingest.py -- Build the retrieval corpus from the Tradeify rulebook.

Chunking strategy (intentional, not LangChain defaults):
  - chunk BY RULE, not by character count: each regulatory/operational rule
    is one atomic chunk, so a retrieved chunk is always a complete statement.
  - every chunk carries metadata: rule_id (links to rule_engine findings),
    account families it applies to, source URL, and chunk type.
  - multilingual note: corpus is English (source language of the rulebook);
    the embedding model should be multilingual (BGE-M3 / e5) for ID queries.

Run: python src/ingest.py  ->  writes data/rulebook_chunks.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

CHUNKS = [
    {
        "id": "chunk_dll",
        "rule_id": "dll",
        "title": "Daily Loss Limit (DLL)",
        "text": (
            "Growth evaluation and Growth funded accounts carry a daily loss limit: "
            "$1,250 on $50K, $2,500 on $100K, $3,750 on $150K. Select Daily funded "
            "accounts carry a lower DLL: $1,000 on $50K, $1,250 on $100K, $1,750 on "
            "$150K. Select evaluation, Select Flex funded, and Lightning $25K have "
            "no daily loss limit. On Growth the DLL is a SOFT breach: hitting it "
            "pauses trading for the rest of the session but does NOT fail the account."
        ),
        "families": ["growth", "select"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "rule",
    },
    {
        "id": "chunk_trailing_dd",
        "rule_id": "trailing_dd",
        "title": "End-of-Day Trailing Max Drawdown",
        "text": (
            "Every Tradeify account uses an end-of-day trailing max drawdown. The "
            "floor starts at starting balance minus the drawdown amount ($2,000 on "
            "$50K Growth, $3,500 on $100K Growth, $5,000 on $150K Growth; $1,000-$6,000 "
            "on Lightning by size; $2,000-$4,500 on Select). The floor trails UP with "
            "the highest end-of-day balance only, never intraday. It locks permanently "
            "at $100 above the starting balance once an end-of-day balance clears "
            "starting balance + drawdown + $100 (or at the first payout, whichever "
            "comes first). If intraday equity touches the floor, it is a HARD breach "
            "and the account fails permanently."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "rule",
    },
    {
        "id": "chunk_consistency",
        "rule_id": "consistency",
        "title": "Consistency Rule",
        "text": (
            "The consistency rule caps how much of total profit may come from the "
            "single best day. Select evaluations use 40% (forcing at least 3 profitable "
            "days). Growth funded accounts use 35%. Lightning uses a progressive rule: "
            "20% for the first payout, 25% for the second, 30% for all subsequent "
            "payouts. Growth evaluations and all funded Select accounts (Daily and "
            "Flex) have no consistency rule. Breaking the consistency threshold NEVER "
            "fails the account; it only delays payout eligibility until smaller "
            "profitable days bring the ratio back under the cap."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "rule",
    },
    {
        "id": "chunk_profit_target",
        "rule_id": "profit_target",
        "title": "Profit Targets (evaluation stage)",
        "text": (
            "Growth evaluation profit targets: $3,000 on $50K, $6,000 on $100K, $9,000 "
            "on $150K. Select evaluation targets: $2,500 on $50K, $6,000 on $100K, "
            "$9,000 on $150K. Growth evaluations can be passed in as little as one "
            "trading day (no consistency rule during Growth eval). Funded accounts "
            "have no profit target."
        ),
        "families": ["growth", "select"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "rule",
    },
    {
        "id": "chunk_microscalp",
        "rule_id": "microscalp",
        "title": "Microscalping Rule",
        "text": (
            "Tradeify's microscalping rule requires BOTH: over 50% of trades held "
            "longer than 10 seconds, AND over 50% of profit coming from trades held "
            "longer than 10 seconds. Falling under either threshold does not fail the "
            "account, but the trader cannot activate a passed evaluation or request a "
            "payout until both ratios clear with longer-held trades."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://damnpropfirms.com/futures-prop-firms/tradeify/",
        "type": "rule",
    },
    {
        "id": "chunk_session_rules",
        "rule_id": "session",
        "title": "Session Rules: flat-by time, hedging, activity",
        "text": (
            "All positions must be closed by 4:59 PM ET on weekdays (12:59 PM ET on "
            "holidays with an early close). No overnight or weekend positions are "
            "allowed. Hedging is strictly prohibited, including opposing positions "
            "across accounts. Minis and micros cannot be traded simultaneously on the "
            "same account. At least one trade per week is required to avoid inactivity "
            "warnings. News trading is allowed. A maximum of 5 funded accounts may be "
            "held simultaneously."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://blog.traderspost.io/article/tradeify-review",
        "type": "rule",
    },
    {
        "id": "chunk_account_families",
        "rule_id": "families",
        "title": "Account Families: Growth vs Select vs Lightning",
        "text": (
            "Tradeify offers three account families. Growth: evaluation with no "
            "consistency rule and a soft daily loss limit; once funded it carries a "
            "35% consistency rule. Select: evaluation with no daily loss limit and a "
            "40% consistency rule; after passing, the trader picks a funded path "
            "(Flex with no DLL, or Daily with a DLL) and there is no consistency "
            "rule once funded. Lightning: instant funding with no evaluation and a "
            "progressive 20%/25%/30% consistency rule across payouts. All families "
            "use end-of-day trailing drawdown and one-time pricing (no subscriptions "
            "under Tradeify 3.0)."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "overview",
    },
    {
        "id": "chunk_payouts",
        "rule_id": "payouts",
        "title": "Payouts and Profit Split",
        "text": (
            "Tradeify advertises a 90/10 profit split in the trader's favor, with 100% "
            "of the first $15,000 kept by the trader on eligible plans. Payouts are "
            "advertised with a 60-minute guarantee on supported paths. A trader becomes "
            "eligible for Elite Live capital after 5 payouts. The trailing drawdown "
            "floor locks at the earlier of the first payout or the EOD balance "
            "clearing starting balance + drawdown + $100."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://phidiaspropfirm.com/education/lucid-trading-vs-tradeify",
        "type": "overview",
    },
    {
        "id": "chunk_drawdown_recovery",
        "rule_id": "recovery",
        "title": "Drawdown Recovery Guidance",
        "text": (
            "Because the trailing drawdown only moves on end-of-day balances, intraday "
            "equity swings do not raise the floor; only the session close matters for "
            "trailing. This makes end-of-day risk management the critical discipline: "
            "protecting the close protects the floor. After the floor locks ($100 above "
            "starting balance), the account can never trail into a loss of the "
            "original capital on a drawdown basis."
        ),
        "families": ["growth", "select", "lightning"],
        "source": "https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders",
        "type": "guidance",
    },
]


def build_corpus() -> list[dict]:
    """Validate chunk schema, then return the corpus."""
    for c in CHUNKS:
        assert set(c) >= {"id", "rule_id", "title", "text", "families", "source", "type"}, c["id"]
        assert len(c["text"]) > 50, c["id"]
    ids = [c["id"] for c in CHUNKS]
    assert len(ids) == len(set(ids)), "duplicate chunk ids"
    return CHUNKS


def main() -> None:
    corpus = build_corpus()
    out = os.path.join(os.path.dirname(__file__), "..", "data", "rulebook_chunks.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(corpus, f, indent=2)
    print(f"wrote {len(corpus)} chunks -> {out}")
    for c in corpus:
        print(f"  {c['id']:24s} [{c['rule_id']:14s}] {c['title']}")


if __name__ == "__main__":
    main()
