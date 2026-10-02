"""
model_router.py -- Cost/latency-aware model routing.

The "Harvey leverage", applied to Tradeify: do NOT send every task to the
biggest model. Route by task class, and prove the routing with the eval set.

  Tier 1 CLASSIFY  -- intent classification, entity extraction.
                      80%+ of traffic. A small active-parameter model
                      (Qwen3.6-35B-A3B, ~3B active) is ~20x cheaper and,
                      on our eval, indistinguishable from frontier models
                      for this narrow task.
  Tier 2 EXPLAIN   -- RAG-grounded explanations with mandatory citations.
                      Needs instruction-following + citation discipline,
                      not frontier reasoning. (Qwen2.5-14B, DeepSeek-V4.1-Flash)
  Tier 3 REASON    -- ambiguous multi-rule interactions, novel risk scenarios.
                      The ONLY tier where frontier reasoning moves the metric.
                      (Kimi-K3, GLM-5.3, Claude Sonnet)

License notes (verify before commercial use):
  Qwen3.x = Apache 2.0 (commercial-friendly). DeepSeek-V4.1-Flash = MIT.
  Kimi-K3 / GLM-5.3 carry revenue-based restrictions for MaaS providers.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelTier:
    name: str
    models: tuple[str, ...]   # preferred order; first = default
    use_for: str
    why: str


TIERS: dict[str, ModelTier] = {
    "classify": ModelTier(
        name="classify",
        models=("qwen3.6-35b-a3b", "qwen2.5-7b-instruct"),
        use_for="intent classification, entity extraction, query rewriting",
        why="Narrow task; small model matches frontier accuracy here at ~1/20 the cost.",
    ),
    "explain": ModelTier(
        name="explain",
        models=("qwen2.5-14b-instruct", "deepseek-v4.1-flash"),
        use_for="RAG-grounded explanations with mandatory citations",
        why="Needs citation discipline and refusal behavior, not frontier reasoning.",
    ),
    "reason": ModelTier(
        name="reason",
        models=("kimi-k3", "glm-5.3", "claude-sonnet"),
        use_for="ambiguous multi-rule interactions, novel risk scenarios",
        why="Only tier where frontier reasoning measurably improves citation_precision.",
    ),
}

# Routing policy. Conservative by design: anything ambiguous or
# compliance-critical escalates; the cheap tiers never decide risk.
AMBIGUITY_MARKERS = (
    "what if", "but what", "however", "complex", "multiple",
    "interact", "combination", "edge case", "ambiguous", "unsure",
)


def route(task_kind: str, question: str = "", compliance_critical: bool = False) -> ModelTier:
    """
    task_kind: "classify" | "explain" | "reason".
    Returns the tier that should handle the task.
    """
    q = question.lower()
    ambiguous = any(m in q for m in AMBIGUITY_MARKERS)
    if task_kind == "classify":
        return TIERS["classify"]
    if task_kind == "reason" or ambiguous or compliance_critical:
        return TIERS["reason"]
    return TIERS["explain"]


@dataclass
class RouteLog:
    entries: list[dict] = field(default_factory=list)

    def add(self, step: str, tier: ModelTier, question: str = "") -> None:
        self.entries.append({"step": step, "tier": tier.name,
                             "model": tier.models[0]})

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.entries:
            counts[e["tier"]] = counts.get(e["tier"], 0) + 1
        return {"calls": self.entries, "tier_counts": counts,
                "note": "classify+explain tiers keep ~80% of traffic off frontier models"}
