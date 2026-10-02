"""
make_finetune_data.py -- Generate synthetic ORPO preference pairs from the rulebook.

Each pair teaches BEHAVIOR, not knowledge:
  chosen   = short grounded answer WITH correct citation  [chunk_xxx]
  rejected = one of: (a) no citation, (b) WRONG citation, (c) rambling no citation

The summaries below are hand-written from the chunk texts (no LLM needed),
so every pair is factually grounded by construction. This is starter data:
replace/augment with frontier-model-generated pairs when an API key exists.

Run: python3 scripts/make_finetune_data.py  ->  evals/finetune_pairs.jsonl
"""

import json
import os
import random

SUMMARIES = {
    "chunk_dll": (
        "Daily Loss Limit",
        "daily loss limit",
        "Growth accounts have a daily loss limit ($1,250 on $50K, $2,500 on $100K, "
        "$3,750 on $150K) that pauses trading for the rest of the session when hit "
        "but never fails the account; Select evaluations, Select Flex funded, and "
        "Lightning $25K have no daily loss limit.",
    ),
    "chunk_trailing_dd": (
        "End-of-Day Trailing Max Drawdown",
        "trailing drawdown",
        "Every account uses an end-of-day trailing drawdown that only moves up with "
        "the highest session close, locks permanently at $100 above the starting "
        "balance once cleared, and fails the account permanently if intraday equity "
        "touches the floor.",
    ),
    "chunk_consistency": (
        "Consistency Rule",
        "consistency rule",
        "Select evaluations cap the best day at 40% of total profit, Growth funded "
        "accounts at 35%, and Lightning uses a progressive 20%/25%/30% rule across "
        "payouts; breaking it only delays payout eligibility and never fails the account.",
    ),
    "chunk_profit_target": (
        "Profit Targets",
        "profit target",
        "Growth evaluation targets are $3,000/$6,000/$9,000 and Select evaluation "
        "targets are $2,500/$6,000/$9,000 by account size; Growth evaluations can be "
        "passed in a single day, and funded accounts have no profit target.",
    ),
    "chunk_microscalp": (
        "Microscalping Rule",
        "microscalping rule",
        "Over 50% of trades and over 50% of profit must come from positions held "
        "longer than 10 seconds; otherwise evaluation activation and payouts stay "
        "blocked until both ratios clear.",
    ),
    "chunk_session_rules": (
        "Session Rules",
        "session rules",
        "All positions must be flat by 4:59 PM ET (12:59 PM ET on early-close "
        "holidays); no overnight or weekend holds, no hedging even across accounts, "
        "a maximum of 5 funded accounts, at least one trade per week, and news "
        "trading is allowed.",
    ),
    "chunk_account_families": (
        "Account Families",
        "account types",
        "Growth is an evaluation with a soft daily loss limit and no consistency "
        "rule (35% once funded); Select evaluation has no DLL but a 40% consistency "
        "rule, with no consistency rule once funded; Lightning is instant funding "
        "with a progressive 20%/25%/30% consistency rule.",
    ),
    "chunk_payouts": (
        "Payouts and Profit Split",
        "payouts",
        "Traders keep 90% of profits (100% of the first $15,000 on eligible plans) "
        "with an advertised 60-minute payout guarantee, reaching Elite Live capital "
        "after 5 payouts.",
    ),
    "chunk_drawdown_recovery": (
        "Drawdown Recovery",
        "drawdown recovery",
        "Because the trailing floor only moves on end-of-day balances, intraday "
        "swings never raise it; protecting the session close is what protects the "
        "account, and once the floor locks the original capital cannot be trailed into.",
    ),
}

QUESTION_TEMPLATES = [
    "What is the {title} at Tradeify?",
    "Explain the {title}.",
    "How does the {short} work at Tradeify?",
    "Summarize the {title} for me.",
    "What should a trader know about the {short}?",
    "How does Tradeify handle the {short}?",
    "Tell me about the {title}.",
    "What are the key points of the {title}?",
    "A new trader is confused about the {short}. Explain it clearly.",
    "Give me the essentials on the {title}.",
]

RAMBLE_PREFIX = (
    "Well, this is a really interesting question and one that a lot of traders "
    "ask me about all the time, and I think it's important to look at it from "
    "several angles before giving a definitive answer. "
)

SYSTEM = ("You are the Tradeify Risk Copilot. Answer ONLY from the context. "
          "Cite every factual claim like [chunk_dll]. If the context is "
          "insufficient, reply EXACTLY: "
          "\"I don't have that in the Tradeify rulebook I can access.\"")


def build_pairs(seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    ids = list(SUMMARIES)
    pairs: list[dict] = []
    for cid in ids:
        title, short, summary = SUMMARIES[cid]
        context = f"[{cid}] {title}: {summary}"
        for tmpl in QUESTION_TEMPLATES:
            q = tmpl.format(title=title, short=short)
            chosen = f"{summary} [{cid}]"
            wrong_cid = rng.choice([i for i in ids if i != cid])
            rejected_variants = [
                summary,                                        # (a) no citation
                f"{summary} [{wrong_cid}]",                     # (b) wrong citation
                RAMBLE_PREFIX + summary,                        # (c) rambling, no citation
            ]
            for rej in rejected_variants:
                pairs.append({"system": SYSTEM, "context": context,
                              "question": q, "chosen": chosen, "rejected": rej})
    rng.shuffle(pairs)
    return pairs


def main() -> None:
    pairs = build_pairs()
    out = os.path.join(os.path.dirname(__file__), "..", "evals", "finetune_pairs.jsonl")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        for p in pairs:
            f.write(json.dumps(p) + "\n")
    print(f"wrote {len(pairs)} pairs -> {out}")
    print("sample chosen :", pairs[0]["chosen"][:120], "...")
    print("sample rejected:", pairs[0]["rejected"][:120], "...")


if __name__ == "__main__":
    main()
