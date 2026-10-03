# Ashby Application — Agentic Coder @ Tradeify
# Draft untuk M Wisnu R — sesuaikan sebelum submit

---

## Short intro (field "Tell us about yourself" / cover letter)

I'm a futures trader who builds the tools I wish my prop firm had.

I trade NQ futures and have taken payouts from prop firms, so I know the
exact moment trust breaks: a trader passes evaluation, requests a payout,
and gets rejected with no explanation. Tradeify's own 1-star Trustpilot
reviews repeat this complaint — "my payout was rejected and nobody told me
why." I built the fix before applying for this role.

**Tradeify Risk Copilot** (github.com/caelancarmer/tradeify-risk-copilot)
is a Payout Risk Gate: it answers "can this trader payout today, and if
not, why?" in ~3ms, with reasons and citations to the actual rulebook. It
catches silent payout killers like microscalping (funded accounts need >50%
of trades AND >50% of profit held >10s — a rule that never shows as a
dashboard violation), warns traders before their drawdown buffer goes
critical, and gives ops one console for every risk decision.

No LLM touches the money path. Every payout decision is deterministic
Python; the LLM only explains the result, and every claim carries a
citation. 241 tests green, pre-check eval 19/19, microscalping 29/29,
buffer alerts 10/10, citation precision 1.000, CI passing on GitHub Actions.

What I bring to this role:
- Shipped agentic systems end to end: hybrid retrieval (BM25 + TF-IDF +
  RRF, benchmarked hit@1 0.867 — no LangChain defaults), a custom agent
  loop with citation verification and explicit refusal, background workers,
  Discord/API/HTMX interfaces.
- Eval-driven development: every claim in the repo is backed by a numbered
  eval set, not adjectives.
- Domain knowledge: I trade the products your customers trade. I know what
  a trailing drawdown feels like at 2am.
- High-output habits: this repo went from idea to 241 green tests in days,
  not sprints — including a same-day pivot when I realized the original
  roadmap was a roadmap, not a product.

I'd rather show you a working system than tell you I'm a fast learner.
The repo is public; the evals are reproducible; the architecture doc
explains every trade-off (including why I didn't use LangGraph or React,
and when I would).

---

## One-line hook (jika ada field singkat)

Futures trader who built a payout risk gate that answers "can I payout
today, and why not?" in 3ms — with citations, 241 tests green.

---

## Links untuk dilampirkan

- Repo: https://github.com/caelancarmer/tradeify-risk-copilot
- Architecture (animated): https://htmlpreview.github.io/?https://github.com/caelancarmer/tradeify-risk-copilot/blob/main/docs/archify-architecture.html
- Pre-check workflow (animated): https://htmlpreview.github.io/?https://github.com/caelancarmer/tradeify-risk-copilot/blob/main/docs/archify-precheck-workflow.html
- Case flows + design decisions: https://github.com/caelancarmer/tradeify-risk-copilot/blob/main/docs/architecture.md
