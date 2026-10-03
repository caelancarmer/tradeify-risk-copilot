# Tradeify Risk Copilot

[![tests](https://github.com/caelancarmer/tradeify-risk-copilot/actions/workflows/test.yml/badge.svg)](https://github.com/caelancarmer/tradeify-risk-copilot/actions)

Agentic risk monitor for Tradeify futures prop-firm accounts. It watches trader
accounts against the real Tradeify rulebook, explains breaches with citations,
and refuses to guess when the rulebook does not cover a question.

## Architecture

```
                    ┌─────────────────────────────┐
                    │      TRADER OPS AGENT       │
                    │ classify -> retrieve ->     │
                    │ grade -> rule_check ->      │
                    │ generate -> verify_citations│
                    └──────────────┬──────────────┘
               ┌───────────────────┼───────────────────┐
               ▼                   ▼                   ▼
     ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
     │ RULE ENGINE     │ │ RAG RETRIEVER   │ │ MODEL ROUTER    │
     │ (pure Python,   │ │ BM25 + TF-IDF   │ │ classify: qwen  │
     │  deterministic) │ │ vector, RRF     │ │ explain: qwen   │
     │ DLL / trailing  │ │ fusion, rerank  │ │ reason: kimi-k3 │
     │ DD / consistency│ │ gate, citation  │ │ /glm-5.3 (escal)│
     └─────────────────┘ └─────────────────┘ └─────────────────┘
               │                   │
               └─────────┬─────────┘
                         ▼
              ┌─────────────────────┐
              │ Discord bot / FastAPI│
              │ background worker    │
              └─────────────────────┘
```

Core principle: **the LLM never decides rule logic.** Rules live in
`src/rule_engine.py` as pure functions. The LLM only explains the engine's
output, and every claim is programmatically checked against retrieved chunks.

## Measured results (not claims)

| Layer | Result |
|---|---|
| Rule engine | **29/29 unit tests pass** (`tests/test_rule_engine.py`) |
| Microscalping gate | **29/29 checks pass**, funded-only (>50% trades AND >50% profit held >10s) (`tests/test_microscalping.py`) |
| Retrieval (30 trader Qs) | hybrid **hit@1 0.867, MRR 0.925** vs BM25-only 0.833/0.908 vs vector-only 0.833/0.903 (`src/eval.py`) |
| Demo scenario | -$1,300 day on Growth $50K funded -> correctly reported as **SOFT breach: session paused, account NOT failed**, with `[chunk_dll]` citation, precision 1.0 |
| Refusal probe | "capital gains tax in Indonesia" -> **refused** (not in rulebook) |
| Pre-check (19 scenarios) | **19/19** status + reason code, latency p50 **0.103 ms** (`scripts/run_precheck_eval.py`) |
| Buffer alerts (10 scenarios) | **10/10**, max sent latency **0.043 ms** (`scripts/run_buffer_eval.py`) |
| Rule registry | **37 entries**, 0 invalid citations (`src/rule_registry.py`) |
| Explainer citations | citation_precision **1.000** (>= 0.95 target) |

Corpus: 9 rule-atomic chunks from the official Tradeify rule tables
(tradeify.co) and verified review sources. Small corpus, honest numbers;
the harness (`evals/eval_set.json`) is built to grow.

## Key design decisions

1. **Rules are code, not prompts.** DLL soft-vs-hard, EOD trailing mechanics
   (trail on close only, lock at start+DD+$100), progressive Lightning
   consistency (20/25/30) are encoded and unit-tested. Hallucination in this
   layer is impossible by construction.
2. **Hybrid retrieval beats vector-only** on regulatory text (see table).
   Chunking is by rule, not by character count.
3. **Model routing beats biggest-model-everywhere.** classify/explain tiers
   (Qwen 3.x / DeepSeek-V4.1-Flash, Apache-2.0/MIT) handle ~80% of traffic;
   frontier tier (Kimi-K3/GLM-5.3) only for ambiguous, compliance-critical
   reasoning. License notes in `src/model_router.py`.
4. **Post-training is for behavior, not knowledge** (`src/finetune_qlora.py`):
   QLoRA + ORPO teaches citation discipline and refusal; the RAG corpus keeps
   the knowledge. Needs a GPU host.
5. **Found and fixed via live verification (2026-10-03):** `/evaluate` and
   `/sync` were unreachable over HTTP because the account registry started
   empty with no way to register. Added `POST /accounts`; the full flow
   (register -> evaluate -> sync -> ask) now returns 200 with correct rule
   output (e.g. trailing floor $145,000 for Growth 150K).

## Payout Automation

Deterministic first, language second: `src/payout.py` decides, the LLM only
explains. The API and worker share one pure pipeline.

```
POST /payout/request
      │  check_eligibility (funded, KYC, method, flat, days, profit,
      │                     hard/soft breach, consistency, bounds)
      ▼  compute_payout_amount  (profit x split%, capped by withdrawable)
decide ─┬─ REJECTED      (reasons + citations)     -> notify trader
        ├─ APPROVED      (amount < $500)           -> 'enqueue payment'
        └─ MANUAL_REVIEW (1st payout or >= $500)   -> admin queue
      │
      ▼  decision + citations + audit_log; POST /payout/{id} is idempotent
```

Eval (`scripts/run_payout_eval.py`, 18 cases across 17 scenarios): **18/18**
cases pass (decision_accuracy 1.000 on the eval set — deterministic by
construction, not a claim about live accuracy), citation_precision **1.000**,
citation_recall **1.000**, 0 uncited rejections. Latency, split honestly:
decision path (check_eligibility -> compute -> decide, no LLM) **0.08 ms**
mean / 200 runs; explanation path (mock LLM) **1.3 ms** mean / 20 runs —
a real LLM call would add 100-2000+ ms and is never on the money path.
Schema via Alembic (`alembic upgrade head`), never `create_all` in production.

Trade-offs: (1) **$500 auto-approve threshold** — clean payouts >= $500 still
manual; fraud risk > delay. (2) **KYC first payout always manual**
(`FIRST_PAYOUT_MANUAL=True`), auto afterwards. (3) **LLM never touches
numbers** — explainer is blocked from any number not already in the decision.
(4) **No payment gateway** — stops at "enqueue payment" (Stripe/Wise roadmap).
Soft DLL breaches warn, not block (soft != failed), matching the rule engine.

## Payout Risk Gate

Answers one question: **"can this trader pay out today, and if not, why?"**
Deterministic first, language second.

### A. Rule Registry (`src/rule_registry.py`)

DB-backed catalogue in the `rule_registry` table (Alembic `0002_rule_registry`)
with the brief's exact columns: `rule_id`, `account_type`, `phase`, `rule_name`,
`threshold`, `breach_type`, `citation_chunk_id`. `load_rules()` reads from
`RULE_REGISTRY_DB_URL`/`DATABASE_URL`, falling back to a seeded in-memory SQLite
DB so demo/CI exercise the same load-from-DB path. `validate_registry()` gates
>= 20 entries and every citation resolving in the retrieval corpus;
`validate_against_account_specs()` guards against drift from
`rule_engine.ACCOUNT_SPECS` (Growth 35, Select eval 40, Lightning 20/25/30,
Growth 150K drawdown 5000).

### B. Read-only pre-check (`POST /payout/precheck`)

Input `{trader_id, account_key, amount_usd}` (+ optional snapshot fields).
Calls the SAME functions as the money path (`check_eligibility`,
`compute_payout_amount`, `decide`) and returns
`{status: ELIGIBLE | NOT_ELIGIBLE | MANUAL_REVIEW, reasons[], citations[],
buffer_remaining_usd, suggested_action}`. It writes nothing to the payout
tables, never touches the idempotency store, and never enqueues the worker;
only a separate `PRECHECK_LOG` grows for the Ops panel.

### C. Buffer monitor (`worker.monitor_buffers`)

30-second job over active accounts:
`buffer = current_equity - dd_floor`, `pct = buffer / trailing_drawdown`.
`< 30%` -> WARNING, `< 10%` -> CRITICAL (boundaries 30%/10% stay one level
lower). Sends a Discord DM when `DISCORD_BOT_TOKEN` + a recipient id are
configured, otherwise writes to the alerts store and logs (never crashes).
Max 1 alert per account per hour.

### D. Explainer (`src/explainer.py`)

`explain_payout_status(trader_id, status, reasons)` -> `Explanation`.
The LLM rephrases only: every number is computed by deterministic Python and
handed over as a fact, and the output is replaced by a deterministic template
if it introduces any number outside that fact set or omits a valid citation.
Citations are verified with `agent.verify_citations`.

### E. Ops Console panels

`/ops` now also shows **Pre-Checks Today** (ELIGIBLE / NOT_ELIGIBLE /
MANUAL_REVIEW counts from `PRECHECK_LOG`) and **Buffer Alerts Sent** (count,
account, severity, channel), both HTMX-polling every 10 s. The three original
panels are unchanged.

### Answers to the brief's questions

1. **Pre-check latency p50 = 0.103 ms** (well under 50 ms; `run_precheck_eval.py`
   over 200 iterations).
2. **Explainer citation_precision = 1.000** (>= 0.95).
3. **No** — pre-check is read-only. `PAYOUT_REQUESTS` / `PAYOUT_DECISIONS` and
   `PAYOUT_IDEMPOTENCY` are untouched (`idempotency_store_delta == 0` in the
   eval; asserted in `tests/test_precheck.py`).
4. **Screenshot**: a headless environment cannot capture one, but `GET /ops`
   renders both new panels and `/ops/partials/prechecks` +
   `/ops/partials/buffer_alerts` return 200 with live counts
   (`tests/test_ops_panels.py`).

No `KNOWN_ISSUE` entries: no bug exceeded the 3-iteration kill criterion.

## Ops Console (Pilar 6)

A single-page operational view that unifies Risk Copilot + Payout Automation.

- **Access**: `/ops`
- **View 5 panels** (Accounts at Risk / Payout Queue / Recent Decisions /
  Pre-Checks Today / Buffer Alerts Sent) with auto-refresh every 10 seconds.
- **Dark mode by default**, responsive layout.
- **Read-only except approve/reject buttons** for manual review requests.
- **Single admin-key auth** via `X-Admin-Key` header (already used by the payout API).
- **No dependencies** on frontend frameworks: uses Jinja2 + HTMX (CDN).

### What it shows

1. **Accounts at Risk**
   - Buffer drawdown = current_equity - dd_floor (proxy start_balance if no live equity)
   - Risk flag if buffer < 30% of trailing_drawdown
   - Concise consistency status (family + limit)
   - Pagination & sorting (risk first)

2. **Payout Queue**
   - Grouped by status (PENDING / MANUAL_REVIEW / APPROVED / REJECTED)
   - Key fields: request_id, account_key, amount, trader_id, KYC status, trading days, decision time
   - Auto-updates on admin approval/rejection

3. **Recent Decisions**
   - Last 10 decisions (timestamp descending)
   - Truncated: decision, amount, first reason, first citation
   - Manual Review entries have approve/reject buttons (requires admin-key)

### Trade-offs (documented)

- **Polling 10s, not websocket**: lightweight, no extra connections.
- **No real-time charts**: roadmap item (trend lines, risk scores).
- **Single-key auth**: not role-based; fits existing payout admin model.
- **No CSV export**: roadmap item (trader reports, compliance snapshots).

### Code / location

- `src/dashboard.py` – context builder (`build_dashboard_context`)
- `templates/ops.html` – HTML shell with 3 HTMX panels
- `static/ops.css` – dark mode styling
- `static/ops.js` – tiny UI glue (admin-key, HTMX headers, confirmations)
- `src/api.py` – added GET `/ops`, partials, static mount
- `tests/test_dashboard.py` – 8–10 unit tests + end‑to‑end UI flow
- Updated `requirements.txt` (`jinja2>=3.1`)

### Run it

```
# All tests
python3 tests/test_dashboard.py

# Ops Console UI (background worker required for live accounts)
cd tradeify-work
pip install -r requirements.txt
uvicorn src.api:app --host 0.0.0.0 --port 8000
open http://localhost:8000/ops
```

GitHub Actions (`.github/workflows/test.yml`) now runs the dashboard tests.

---

## Run it (no GPU, no keys needed)

```
python3 tests/test_rule_engine.py       # 29 rule tests
python3 tests/test_payout.py            # 46 payout tests
python3 tests/test_rule_registry.py     # 17 registry tests
python3 tests/test_precheck.py          # 19 pre-check tests (read-only proof)
python3 tests/test_explainer.py         # 17 explainer tests
python3 tests/test_buffer_alerts.py     # 25 buffer-alert tests
python3 tests/test_ops_panels.py        # 20 ops-panel tests
python3 scripts/run_payout_eval.py      # payout eval -> evals/payout_eval_results.json
python3 scripts/run_precheck_eval.py    # pre-check + explainer -> evals/precheck_eval_results.json
python3 scripts/run_buffer_eval.py      # buffer alerts -> evals/buffer_alert_eval_results.json
python3 src/rule_registry.py            # registry validation gate
python3 src/eval.py                     # retrieval benchmark -> evals/retrieval_results.json
python3 demo.py                         # end-to-end scenario (offline mock LLM)
```

Production: `docker compose up` (postgres+pgvector, redis, langfuse, api, worker).
Schema: `POSTGRES_URL=... alembic upgrade head` (migrations only; 0001 payout
tables, 0002 rule_registry).

## What the full version still needs

- GPU for `finetune_qlora.py` + local inference. **No GPU? Use the Colab path:**
  open `colab_finetune.ipynb` in Google Colab (free T4), Runtime -> Run all.
  It generates 270 preference pairs on the spot, QLoRA+ORPO fine-tunes
  **Qwen2.5-7B-Instruct** (7B is a deliberate trade-off: 14B does not fit a
  free T4 reliably), runs a behavioral eval (citation present? refusal exact?),
  and saves the adapter to `/content/qwen7b-copilot-orpo` for download.
  Expected wall time 2-4 hours; keep the tab open.
- `OPENROUTER_API_KEY` for the frontier reasoning tier
- `DISCORD_BOT_TOKEN` for the Discord surface
- Tradovate/Rithmic API wiring in `src/worker.py::fetch_broker_snapshot`

## Repo map

```
src/rule_engine.py    deterministic Tradeify rules + 22 account specs
src/ingest.py         rulebook corpus -> data/rulebook_chunks.json (9 chunks)
src/retrieval.py      BM25 + TF-IDF + RRF fusion, relevance grading
src/model_router.py   tiered routing (classify/explain/reason)
src/agent.py          agentic loop: classify->retrieve->grade->rule_check->generate->verify
src/api.py            FastAPI: /ask /evaluate /sync /health + payout endpoints
src/payout.py         deterministic payout engine (eligibility/amount/decide)
src/payout_models.py  SQLAlchemy models: requests/decisions/kyc/audit_log
src/rule_registry.py  DB-backed registry of 37 verified rules + citation gate
src/microscalping.py  funded-only microscalping gate (>50% trades & profit >10s)
src/precheck.py       read-only payout pre-check (calls the payout engine + gate)
src/explainer.py      LLM rephrase-only explanation with verified citations
src/alert_store.py    buffer-alert log shared by worker + dashboard
src/bot.py            Discord: /ask /risk /accounts
src/worker.py         durable background sync + process_payout_request + buffer monitor
src/eval.py           retrieval eval harness (hit@k, MRR)
src/finetune_qlora.py QLoRA+ORPO post-training (GPU host only)
alembic/              payout + rule_registry migrations (0001, 0002)
evals/eval_set.json   30 trader questions + refusal probes
evals/payout_eval_set.json  17 payout scenarios (5 approve/7 reject/3 edge/2 adv)
evals/precheck_eval.json    19 pre-check scenarios (6 eligible/8 reject/3 manual/2 adv)
evals/buffer_alert_eval.json 10 buffer-alert scenarios (6 threshold/4 anti-spam)
scripts/run_payout_eval.py  payout eval -> decision_accuracy, citation_precision
scripts/run_precheck_eval.py  pre-check + explainer eval
scripts/run_buffer_eval.py    buffer-alert eval
tests/                rule engine + payout + registry + pre-check + microscalping + buffer tests
demo.py               offline end-to-end demo
```

## Ops Console

`GET /ops` — single-page ops view unifying both products. Header: health badges
(Postgres / Redis / model router) + X-Admin-Key input. Three panels, each
auto-refreshing every 10s via HTMX polling (`hx-trigger="every 10s"`):

- **Accounts at Risk** — accounts with drawdown buffer < 30% of trailing,
  with buffer $/%, consistency status.
- **Payout Queue** — requests grouped by status
  (MANUAL_REVIEW / PENDING / APPROVED / REJECTED).
- **Recent Decisions** — last 10 decisions with reason + citation; Approve/Reject
  buttons for MANUAL_REVIEW (POST with X-Admin-Key header).

Run: `uvicorn src.api:app --reload`, open http://localhost:8000/ops.
Measured: /ops renders in ~25 ms with 100 accounts + 50 payouts; partial
polling averages ~3 ms/request over 300 requests with zero errors.

Trade-offs: (1) 10s polling, not websocket — simpler, enough for ops.
(2) No real-time charts — lists first, charts are roadmap. (3) Single-key auth,
not role-based — fine for internal v1. (4) No CSV export — roadmap, not blocker.
