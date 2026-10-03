# Tradeify Risk Copilot

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
| Retrieval (30 trader Qs) | hybrid **hit@1 0.867, MRR 0.925** vs BM25-only 0.833/0.908 vs vector-only 0.833/0.903 (`src/eval.py`) |
| Demo scenario | -$1,300 day on Growth $50K funded -> correctly reported as **SOFT breach: session paused, account NOT failed**, with `[chunk_dll]` citation, precision 1.0 |
| Refusal probe | "capital gains tax in Indonesia" -> **refused** (not in rulebook) |

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

## Run it (no GPU, no keys needed)

```
python3 tests/test_rule_engine.py   # 29 rule tests
python3 src/eval.py                 # retrieval benchmark -> evals/retrieval_results.json
python3 demo.py                     # end-to-end scenario (offline mock LLM)
```

Production: `docker compose up` (postgres+pgvector, redis, langfuse, api, worker).

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
src/api.py            FastAPI: /ask /evaluate /sync /health
src/bot.py            Discord: /ask /risk /accounts
src/worker.py         durable background sync + alerting
src/eval.py           retrieval eval harness (hit@k, MRR)
src/finetune_qlora.py QLoRA+ORPO post-training (GPU host only)
evals/eval_set.json   30 trader questions + refusal probes
tests/                rule engine unit tests
demo.py               offline end-to-end demo
```
