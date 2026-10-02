"""
agent.py -- Agentic RAG loop for the Tradeify Risk Copilot.

Node graph (maps 1:1 to a LangGraph StateGraph; implemented dependency-free
so the core logic is testable without the framework):

  classify_intent -> retrieve -> grade --(fail)--> rewrite_query -> retrieve
        |                          |
     (no retrieval              (pass)
      needed / refuse)              v
        |                     rule_check (if account state given)
        v                           v
      refuse                    generate -> verify_citations --(fail)--> generate (1 retry)
                                        |
                                     (pass)
                                        v
                                      answer

Hard guarantees (the anti-hallucination design):
  1. Rule LOGIC never comes from the LLM (see rule_engine.py).
  2. Every factual claim about a rule must cite a retrieved chunk id.
  3. If retrieval grading fails twice, the agent REFUSES instead of guessing.
  4. Citation verification is programmatic, not vibe-based.

LLM backends implement LLMClient.generate(system, user) -> str.
  - MockLLMClient  : deterministic, offline. Used for demo/tests.
  - OllamaClient   : local open-weight models (qwen2.5-14b, deepseek-v4.1-flash...).
  - OpenRouterClient: frontier models (kimi-k3, glm-5.3, claude-sonnet...).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from model_router import route, RouteLog, TIERS
from retrieval import HybridRetriever, load_corpus
from rule_engine import AccountState, evaluate_all, summarize

SYSTEM_PROMPT = """You are the Tradeify Risk Copilot, an assistant for futures prop-firm traders.
You answer ONLY from the CONTEXT chunks and the RULE ENGINE findings provided.
Rules:
1. Every factual claim about a Tradeify rule must cite its chunk id like [chunk_dll].
2. Numbers (limits, percentages, dollar amounts) come ONLY from rule engine findings or cited chunks. Never invent them.
3. If the context does not contain the answer, reply EXACTLY: "I don't have that in the Tradeify rulebook I can access." Do not guess.
4. Distinguish breach severity: SOFT breach = session paused, account NOT failed. HARD breach = account failed permanently. Consistency breach = payout delayed, account NOT failed.
5. Keep answers short and structured."""

REFUSAL = "I don't have that in the Tradeify rulebook I can access."

CITATION_RE = re.compile(r"\[(chunk_[a-z_]+)\]")


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

class LLMClient:
    def generate(self, system: str, user: str) -> str:
        raise NotImplementedError


class MockLLMClient(LLMClient):
    """Deterministic offline stand-in. Renders answers from findings + chunks."""

    def generate(self, system: str, user: str) -> str:
        if user.startswith("CLASSIFY:"):
            q = user[len("CLASSIFY:"):].lower()
            needs_rules = any(w in q for w in
                              ("account", "loss", "drawdown", "payout", "limit", "rule",
                               "fail", "breach", "target", "consistency", "scalp",
                               "position", "hedg", "overnight"))
            return json.dumps({"needs_rules": needs_rules, "needs_retrieval": True})
        if user.startswith("REWRITE:"):
            q = user[len("REWRITE:"):]
            return "Tradeify rule: " + q
        # GENERATE path: user contains FINDINGS + CHUNKS sections
        fdata = _extract_json(user)
        lines = []
        for f in fdata.get("all", []):
            if f["status"] != "OK":
                lines.append(f"- **{f['rule']}**: {f['status']} ({f['severity']}). "
                             f"{f['message']} [{f['citation']}]")
        if not lines:
            lines.append("- All deterministic rule checks passed. [chunk_account_families]")
        cited = sorted(set(CITATION_RE.findall("\n".join(lines))))
        body = "\n".join(lines)
        if cited:
            body += "\n\nSources: " + ", ".join(f"[{c}]" for c in cited)
        return body


def _extract_json(text: str) -> dict:
    """Extract the first balanced {...} object from text (robust to trailing prose)."""
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return {}
    return {}


class OllamaClient(LLMClient):
    """Local open-weight inference. Requires: ollama serve (model pulled)."""

    def __init__(self, model: str, base_url: str = "http://localhost:11434"):
        import httpx
        self._httpx = httpx
        self.model = model
        self.base_url = base_url

    def generate(self, system: str, user: str) -> str:
        r = self._httpx.post(f"{self.base_url}/api/chat", json={
            "model": self.model, "stream": False, "options": {"temperature": 0.0},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}, timeout=120)
        r.raise_for_status()
        return r.json()["message"]["content"]


class OpenRouterClient(LLMClient):
    """Frontier models via OpenRouter. Requires OPENROUTER_API_KEY."""

    def __init__(self, model: str):
        import httpx
        self._httpx = httpx
        self.model = model
        self.key = os.environ["OPENROUTER_API_KEY"]

    def generate(self, system: str, user: str) -> str:
        r = self._httpx.post("https://openrouter.ai/api/v1/chat/completions",
                             headers={"Authorization": f"Bearer {self.key}"},
                             json={"model": self.model, "temperature": 0.0,
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": user}]},
                             timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    answer: str
    citations: list[str]
    citation_precision: float
    findings: list[dict]
    refused: bool
    route_log: dict
    retrieval_rounds: int


def classify_intent(question: str, client: LLMClient, log: RouteLog) -> dict:
    tier = route("classify", question)
    log.add("classify_intent", tier, question)
    raw = client.generate(SYSTEM_PROMPT, "CLASSIFY:" + question)
    try:
        return json.loads(raw)
    except Exception:
        return {"needs_rules": True, "needs_retrieval": True}


def rewrite_query(question: str, client: LLMClient, log: RouteLog) -> str:
    tier = route("classify", question)
    log.add("rewrite_query", tier, question)
    return client.generate(SYSTEM_PROMPT, "REWRITE:" + question).strip()


def verify_citations(answer: str, retrieved_ids: list[str]) -> tuple[list[str], float]:
    cited = CITATION_RE.findall(answer)
    if not cited:
        return [], 0.0
    good = [c for c in cited if c in retrieved_ids]
    return sorted(set(good)), len(good) / len(cited)


def run_agent(question: str, retriever: HybridRetriever, clients: dict[str, LLMClient],
              account_state: AccountState | None = None,
              account_inputs: dict | None = None,
              max_retrieval_rounds: int = 2) -> AgentResult:
    """
    clients: {"classify": LLMClient, "explain": LLMClient, "reason": LLMClient}
    account_inputs (optional): {"day_pnl","current_equity","daily_profits",
                                "trades","current_balance"} for rule_check.
    """
    log = RouteLog()
    intent = classify_intent(question, clients["classify"], log)

    # ---- retrieve + grade (+ rewrite loop) ----
    # Grading is ALWAYS against the ORIGINAL question: the rewrite step may
    # inject matching terms ("Tradeify rule: ..."), which must not be allowed
    # to launder an irrelevant query into a passing grade (refusal guarantee).
    chunks: list[dict] = []
    rounds = 0
    graded_ok = False
    q = question
    for _ in range(max_retrieval_rounds):
        rounds += 1
        chunks = retriever.retrieve(q, k=5, method="hybrid")
        if retriever.grade(question, chunks):
            graded_ok = True
            break
        q = rewrite_query(question, clients["classify"], log)
    if not graded_ok:
        return AgentResult(REFUSAL, [], 0.0, [], True, log.summary(), rounds)
    retrieved_ids = [c["id"] for c in chunks]

    # ---- deterministic rule check (never from the LLM) ----
    findings: list[dict] = []
    if account_state is not None and account_inputs:
        raw = evaluate_all(account_state, **account_inputs)
        findings = [f.to_dict() for f in raw]

    # ---- generate (tier by ambiguity) ----
    ambiguous = any(m in question.lower() for m in ("what if", "but what", "however"))
    tier = route("reason" if ambiguous else "explain", question,
                 compliance_critical=bool(findings))
    log.add("generate", tier, question)
    ctx = "\n".join(f"[{c['id']}] {c['title']}: {c['text']}" for c in chunks)
    fin = json.dumps({"all": findings}, indent=1) if findings else "{}"
    user = (f"QUESTION: {question}\n"
            f"INSTRUCTION: Answer with citations like [chunk_dll].\n\n"
            f"== CHUNKS ==\n{ctx}\n\n== FINDINGS ==\n{fin}")
    answer = clients[tier.name].generate(SYSTEM_PROMPT, user)

    # ---- programmatic citation verification (+ 1 retry) ----
    cited, precision = verify_citations(answer, retrieved_ids)
    if precision < 1.0 and cited:
        log.add("generate_retry", tier, question)
        answer = clients[tier.name].generate(
            SYSTEM_PROMPT, user + "\n\nFix: every claim must cite one of "
            + ", ".join(f"[{i}]" for i in retrieved_ids) + ".")
        cited, precision = verify_citations(answer, retrieved_ids)
    if not cited and REFUSAL not in answer:
        # No verifiable citation and not a refusal -> refuse rather than guess.
        return AgentResult(REFUSAL, [], 0.0, findings, True, log.summary(), rounds)

    return AgentResult(answer, cited, round(precision, 3), findings, False,
                       log.summary(), rounds)


def default_clients(mock: bool = True) -> dict[str, LLMClient]:
    if mock:
        m = MockLLMClient()
        return {"classify": m, "explain": m, "reason": m}
    return {"classify": OllamaClient(TIERS["classify"].models[0]),
            "explain": OllamaClient(TIERS["explain"].models[0]),
            "reason": OpenRouterClient(TIERS["reason"].models[0])}
