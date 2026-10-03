"""
explainer.py -- LLM explanation layer for payout verdicts (deliverable D).

HARD RULE (enforced here, not just documented):
    The LLM only *rephrases* the deterministic engine output. It never computes
    a number. Every number that appears in the prompt comes from Python --
    either the engine's ``reasons`` (which already contain the computed buffer,
    percentages, thresholds and amounts) or an explicit ``facts`` dict supplied
    by the caller. After generation, the output is checked so that no numeric
    token appears that was not in the allowed fact set; a violation falls back
    to a deterministic template.

Citations are programmatically verified with ``agent.verify_citations`` and
``citation_precision`` is reported, so the eval gate (>= 0.95) is measured, not
asserted.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))

from precheck import reason_codes, suggested_action  # noqa: E402

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_CHUNK_RE = re.compile(r"\[(chunk_[a-z_]+)\]")

# Reason code -> rulebook chunk that justifies it. Keeps citations honest even
# when the caller passes only the brief's (trader_id, status, reasons) triple.
_CODE_TO_CHUNK: dict[str, str] = {
    "PAYOUT_STAGE_NOT_FUNDED": "chunk_account_families",
    "PAYOUT_KYC_UNVERIFIED": "chunk_payouts",
    "PAYOUT_METHOD_INVALID": "chunk_payouts",
    "PAYOUT_HARD_BREACH": "chunk_trailing_dd",
    "PAYOUT_SOFT_BREACH_WARNING": "chunk_dll",
    "PAYOUT_OPEN_POSITIONS": "chunk_payouts",
    "PAYOUT_MIN_TRADING_DAYS": "chunk_payouts",
    "PAYOUT_BELOW_MIN_PROFIT": "chunk_payouts",
    "PAYOUT_EXCEEDS_WITHDRAWABLE": "chunk_trailing_dd",
    "PAYOUT_BELOW_MIN": "chunk_payouts",
    "PAYOUT_ABOVE_MAX": "chunk_payouts",
    "PAYOUT_CONSISTENCY_BREACH": "chunk_consistency",
    "PAYOUT_CONSISTENCY_OK": "chunk_consistency",
    "PAYOUT_ZERO_PAYABLE": "chunk_payouts",
    "PAYOUT_EXCEEDS_EARNED_SHARE": "chunk_payouts",
    "PAYOUT_FIRST_PAYOUT_MANUAL": "chunk_payouts",
    "PAYOUT_AMOUNT_MANUAL_REVIEW": "chunk_payouts",
    "PAYOUT_WORKER_ESCALATION": "chunk_payouts",
    "PAYOUT_APPROVED": "chunk_payouts",
    "PRECHECK_DUPLICATE_REQUEST": "chunk_payouts",
}

_STATUS_ALIASES = {
    "ELIGIBLE": "APPROVED",
    "NOT_ELIGIBLE": "REJECTED",
    "APPROVED": "APPROVED",
    "REJECTED": "REJECTED",
    "MANUAL_REVIEW": "MANUAL_REVIEW",
}


class Explanation(BaseModel):
    trader_id: str
    status: str
    explanation: str
    citations: list[str] = Field(default_factory=list)
    citation_precision: float = 0.0
    suggested_action: str = ""
    facts: dict = Field(default_factory=dict)
    tier: str = "explain"
    model: str = ""
    numbers_unchanged: bool = True
    deterministic_fallback: bool = False


def chunks_for_reasons(reasons: list[str]) -> list[str]:
    """Resolve the citation chunks for a set of engine reasons."""
    out: list[str] = []
    for reason in reasons:
        out.extend(_CHUNK_RE.findall(reason))
        for code in reason_codes([reason]):
            chunk = _CODE_TO_CHUNK.get(code)
            if chunk:
                out.append(chunk)
    if not out:
        out = ["chunk_payouts"]
    seen: dict[str, None] = {}
    for c in out:
        seen.setdefault(c, None)
    return list(seen)


def _numbers(text: str) -> set[str]:
    return {raw.replace(",", "") for raw in _NUMBER_RE.findall(text)}


def _allowed_numbers(reasons: list[str], facts: dict) -> set[str]:
    text = " ".join(reasons)
    text += " " + json.dumps(facts, default=str)
    return _numbers(text)


def _fallback_explanation(trader_id: str, status: str, reasons: list[str],
                          citations: list[str], action: str) -> str:
    label = {"APPROVED": "approved", "REJECTED": "not eligible",
             "MANUAL_REVIEW": "flagged for manual review"}.get(
                 status, status.lower())
    body = "\n".join(f"- {r}" for r in reasons) or "- No blocking findings."
    src = " ".join(f"[{c}]" for c in citations)
    return (f"Payout pre-check for trader {trader_id} was {label}.\n{body}\n"
            f"Next step: {action}\nSources: {src}")


def explain_payout_status(trader_id: str, status: str, reasons: list[str],
                          *, facts: Optional[dict] = None,
                          citations: Optional[list[str]] = None,
                          client=None,
                          retrieved_ids: Optional[list[str]] = None,
                          ) -> Explanation:
    """
    Explain a deterministic verdict in plain language with citations.

    ``status`` may be the pre-check status (ELIGIBLE/NOT_ELIGIBLE) or the raw
    engine decision (APPROVED/REJECTED/MANUAL_REVIEW). ``reasons`` are the
    engine's reasons, which already contain every computed number. ``facts`` is
    an optional dict of additional deterministic numbers to hand the LLM.
    """
    from agent import MockLLMClient, SYSTEM_PROMPT, verify_citations
    from model_router import route

    facts = dict(facts or {})
    facts.setdefault("status", status)
    reasons = list(reasons or [])
    engine_status = _STATUS_ALIASES.get(status, status)
    codes = reason_codes(reasons)
    action = suggested_action(
        {"ELIGIBLE": "ELIGIBLE", "NOT_ELIGIBLE": "NOT_ELIGIBLE",
         "APPROVED": "ELIGIBLE", "REJECTED": "NOT_ELIGIBLE",
         "MANUAL_REVIEW": "MANUAL_REVIEW"}.get(status, status), codes)

    allowed_ids = list(citations) if citations else chunks_for_reasons(reasons)
    if retrieved_ids is not None:
        # Only cite chunks that are actually in the retrieval corpus.
        allowed_ids = [c for c in allowed_ids if c in retrieved_ids] or \
            list(retrieved_ids)[:1]

    tier = route("explain", trader_id, compliance_critical=bool(reasons))
    if client is None:
        client = MockLLMClient()

    # Each finding carries the citation that justifies it. The LLM renders
    # these; it is never asked to derive a number or pick a chunk on its own.
    findings = []
    for reason in reasons:
        chunk = next((c for c in chunks_for_reasons([reason])
                      if c in allowed_ids), allowed_ids[0])
        findings.append({
            "rule": "Payout pre-check", "status": engine_status,
            "severity": "INFO", "message": reason, "citation": chunk,
        })
    if not findings:
        findings = [{
            "rule": "Payout pre-check", "status": engine_status,
            "severity": "INFO",
            "message": "No blocking findings; the deterministic engine "
                       "approved this payout.",
            "citation": allowed_ids[0],
        }]

    user = (
        f"QUESTION: Explain the payout pre-check verdict for trader "
        f"{trader_id} in plain language.\n"
        f"INSTRUCTION: Rephrase the FINDINGS below. Use ONLY the numbers "
        f"already present in the FINDINGS or FACTS. Cite chunks like "
        f"[chunk_payouts]. Never invent or recompute a number. Also state the "
        f"next step.\n\n"
        f"== FACTS ==\n{json.dumps(facts, default=str)}\n\n"
        f"== CHUNKS ==\n" +
        "\n".join(f"[{c}] rulebook chunk: {c}" for c in allowed_ids) + "\n\n"
        f"== FINDINGS ==\n{json.dumps({'all': findings})}\n"
    )

    raw = client.generate(SYSTEM_PROMPT, user)
    cited, precision = verify_citations(raw, allowed_ids)
    if precision < 1.0:
        raw = client.generate(
            SYSTEM_PROMPT, user + "\n\nFix: every claim must cite one of "
            + ", ".join(f"[{i}]" for i in allowed_ids) + ".")
        cited, precision = verify_citations(raw, allowed_ids)

    numbers_ok = _numbers(raw) <= _allowed_numbers(reasons, facts)
    fallback = False
    if not numbers_ok or not cited:
        raw = _fallback_explanation(trader_id, engine_status, reasons,
                                    allowed_ids, action)
        cited, precision = verify_citations(raw, allowed_ids)
        numbers_ok = _numbers(raw) <= _allowed_numbers(reasons, facts)
        fallback = True

    return Explanation(
        trader_id=trader_id, status=status, explanation=raw,
        citations=cited, citation_precision=round(precision, 3),
        suggested_action=action, facts=facts, tier=tier.name,
        model=tier.models[0], numbers_unchanged=numbers_ok,
        deterministic_fallback=fallback)


__all__ = ["Explanation", "explain_payout_status", "chunks_for_reasons"]
