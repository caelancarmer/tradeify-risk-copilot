# Architecture

Tradeify Risk Copilot + Payout Risk Gate. One repo, one question answered:
"Can this trader payout today — and if not, why?"

## System Map

```mermaid
flowchart TB
    subgraph UI["Interface"]
        OPS["/ops — Ops Console<br/>(HTMX + Jinja2, polling 10s)"]
        DISCORD["Discord Bot<br/>(/ask /risk /ledger, DM alerts)"]
        API["FastAPI REST<br/>(/payout/* /accounts /ask /alerts)"]
    end

    subgraph DECIDE["Decision Layer — deterministic, no LLM"]
        PRE["precheck.py<br/>POST /payout/precheck<br/>read-only, 3ms"]
        PAY["payout.py<br/>check_eligibility → decide<br/>idempotent"]
        RE["rule_engine.py<br/>DLL, trailing DD, consistency<br/>29/29 test"]
        REG["rule_registry.py<br/>35 rules as data<br/>+ citation_chunk_id"]
    end

    subgraph INTEL["Intelligence Layer — LLM explains only"]
        AGENT["agent.py<br/>classify → retrieve → grade<br/>→ rule_check → generate → verify<br/>(custom loop, not LangGraph)"]
        RET["retrieval.py<br/>BM25 + TF-IDF + RRF<br/>hit@1 0.867"]
        EXP["explainer.py<br/>LLM paraphrases Python numbers<br/>citation_precision 1.000"]
        ROUTER["model_router.py<br/>3-tier: classify/explain/reason"]
    end

    subgraph DATA["Data"]
        SQL["payout_models.py<br/>SQLAlchemy + Alembic<br/>append-only ledger"]
        ALERT["alert_store.py<br/>buffer alerts, anti-spam 1/hr"]
    end

    subgraph BG["Background Worker"]
        W1["process_payout_request"]
        W2["monitor_buffers (30s poll)<br/>WARNING <30%, CRITICAL <10%"]
        W3["sync_once (broker snapshot)"]
    end

    subgraph QA["Quality Gates"]
        EVAL["5 eval sets<br/>precheck 15/15, buffer 10/10<br/>payout 18/18"]
        CI["GitHub Actions<br/>212 tests green"]
    end

    DISCORD --> API
    OPS --> API
    API --> PRE
    API --> PAY
    PRE --> RE
    PAY --> RE
    RE --> REG
    API --> AGENT
    AGENT --> RET
    AGENT --> EXP
    EXP --> ROUTER
    W1 --> PAY
    W2 --> RE
    W2 --> ALERT
    W2 --> DISCORD
    PAY --> SQL
    PRE -.->|"0 writes"| SQL
```

## Case Flows

### 1. Pre-check payout (read-only, 3ms)

```mermaid
flowchart LR
    A["1. Trader: POST /payout/precheck<br/>{account_key, amount_usd}"]
    B["2. precheck.py: call<br/>check_eligibility()"]
    C["3. rule_engine.py: check<br/>DLL, trailing DD, consistency"]
    D["4. payout.py: compute_payout_amount()<br/>(profit x split%, capped)"]
    E["5. payout.py: decide()<br/>ELIGIBLE / NOT_ELIGIBLE / MANUAL_REVIEW"]
    F["6. Response: status + reasons[]<br/>+ citations[] + buffer_remaining_usd<br/>(explainer.py tersedia sebagai modul terpisah<br/>untuk narasi bahasa natural)"]
    A --> B --> C --> D --> E --> F
```

### 2. Full payout request (idempotent)

```mermaid
flowchart LR
    A["1. Trader: POST /payout/request"]
    B["2. api.py: fingerprint check<br/>same request_id + different payload<br/>→ REJECTED (conflict)"]
    C["3. worker: process_payout_request()<br/>check_eligibility → decide"]
    D["4a. APPROVED (<$500)<br/>→ enqueue payment"]
    D2["4b. MANUAL_REVIEW<br/>(>=$500 or first payout)<br/>→ admin queue"]
    D3["4c. REJECTED<br/>→ reasons + citations"]
    E["5. Audit log: every decision<br/>recorded (who, when, why)"]
    F["6. Admin: POST /payout/{id}/approve<br/>or /reject via /ops"]
    A --> B --> C --> D
    A --> B --> C --> D2
    A --> B --> C --> D3
    D --> E
    D2 --> E
    D3 --> E
    D2 --> F
```

### 3. Buffer monitor (30s worker poll)

```mermaid
flowchart LR
    A["1. worker.monitor_buffers():<br/>poll all active accounts / 30s"]
    B["2. Compute: buffer = equity - dd_floor<br/>pct = buffer / trailing_drawdown"]
    C["3a. pct < 10% → CRITICAL"]
    C2["3b. pct < 30% → WARNING"]
    C3["3c. pct >= 30% → silent"]
    D["4. alert_store: alerted<br/>within 1h? → suppress"]
    E["5. Discord DM to trader:<br/>'Your buffer is $X, down $Y/2h'"]
    F["6. Logged to alert_store +<br/>'Buffer Alerts Sent' panel in /ops"]
    A --> B --> C
    A --> B --> C2
    A --> B --> C3
    C --> D --> E --> F
    C2 --> D
```

### 4. Trader asks a rule question (agentic RAG)

```mermaid
flowchart LR
    A["1. Trader: /ask 'what is consistency<br/>on Growth funded?' (Discord/API)"]
    B["2. agent.py: classify_intent"]
    C["3. retrieval.py: hybrid search<br/>BM25 + TF-IDF + RRF"]
    D["4. Grade results: pass?<br/>no → rewrite query → retry<br/>fail 2x → REFUSE"]
    E["5. rule_check: cross-check with<br/>rule_engine (if account given)"]
    F["6. generate: LLM answers<br/>+ verify_citations (programmatic)"]
    G["7. Answer + citation chunk_ids<br/>or explicit refusal"]
    A --> B --> C --> D --> E --> F --> G
```

### 5. Ops monitors risk (dashboard)

```mermaid
flowchart LR
    A["1. Ops opens /ops"]
    B["2. HTMX: hx-trigger='every 10s'<br/>polls 5 partial endpoints"]
    C["3a. Accounts at Risk<br/>(buffer < 30% trailing DD)"]
    C2["3b. Payout Queue<br/>(grouped by status)"]
    C3["3c. Pre-Checks Today<br/>(ELIGIBLE vs NOT_ELIGIBLE)"]
    C4["3d. Buffer Alerts Sent"]
    C5["3e. Health<br/>(Postgres/Redis/router)"]
    D["4. MANUAL_REVIEW → Approve/Reject<br/>buttons (X-Admin-Key)"]
    A --> B --> C
    A --> B --> C2
    A --> B --> C3
    A --> B --> C4
    A --> B --> C5
    C2 --> D
```

## Design Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| No LLM on money path | LLMs hallucinate numbers; a wrong payout amount is a lawsuit | "LLM decides everything" (junior approach) |
| Custom agent loop, not LangGraph | 6-node linear loop doesn't need graph orchestration; dependency-free = testable in CI without framework | LangGraph (heavier, same behavior for this shape) |
| Custom hybrid retrieval, not LangChain | BM25+TF-IDF+RRF benchmarked: hit@1 0.867 beats BM25-only and vector-only on our eval | LangChain defaults (JD explicitly warns against these) |
| HTMX not React | Internal tool: ship in hours, one developer, zero build step | Next.js (overkill for 5 panels) |
| Polling not WebSocket | 10s staleness is fine for ops; no connection state to manage | WebSocket (complexity without payoff here) |
| Append-only ledger | Money history must be immutable; corrections are new entries | UPDATE/DELETE on ledger (destroys audit trail) |
| Pre-check is read-only | A check must never mutate state; proven by test (0 writes) | Pre-check that creates a "pending" record (side effects) |

## Constraints Enforced in Code

1. `payout.py`, `retrieval.py`, `agent.py`, `rule_engine.py` are never modified
   by feature work — only called. Verified via `git diff` in CI reviews.
2. Every money decision carries `reasons[]` + `citations[]`. No uncited
   rejections (eval gate: 0 violations).
3. Idempotency on all transactional endpoints (same request_id = same result).
