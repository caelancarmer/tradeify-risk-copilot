"""
precheck.py -- Read-only payout eligibility pre-check.

Answers one question: "can this trader pay out today, and if not, why?" It
CALLS the deterministic engine in ``payout.py`` -- ``check_eligibility``,
``compute_payout_amount`` and ``decide`` -- and never re-implements any rule.
It is a pure read: it does not write payout rows, does not touch the
idempotency store, and does not enqueue a worker job. The API layer may append
an audit row to its separate pre-check log, but that is the only side effect.

Status mapping (engine decision -> pre-check status):
    APPROVED       -> ELIGIBLE
    REJECTED       -> NOT_ELIGIBLE
    MANUAL_REVIEW  -> MANUAL_REVIEW

On top of the engine decision, the pre-check applies the funded-only
microscalping gate (``src/microscalping.py``). The rule is silent on the
dashboard, so a FAIL here is reported as NOT_ELIGIBLE with the concrete
ratios; evaluation accounts are SKIPped because the rule does not apply there.

All numbers (buffer, percentages, withdrawable, payable) are computed here in
deterministic Python; the explainer only receives them as facts.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Literal, Optional

from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))

from payout import (  # noqa: E402
    KYCStatus,
    PayoutAccount,
    PayoutPolicy,
    PayoutRequest,
    check_eligibility,
    compute_payout_amount,
    decide,
)
from microscalping import (  # noqa: E402
    PROFIT_RATIO_CODE,
    TRADE_RATIO_CODE,
    check_microscalping,
)
from rule_engine import ACCOUNT_SPECS  # noqa: E402

PrecheckStatus = Literal["ELIGIBLE", "NOT_ELIGIBLE", "MANUAL_REVIEW"]

_DECISION_TO_STATUS = {
    "APPROVED": "ELIGIBLE",
    "REJECTED": "NOT_ELIGIBLE",
    "MANUAL_REVIEW": "MANUAL_REVIEW",
}

_REASON_CODE_RE = re.compile(r"\[([A-Z0-9_]+)\]")


class PrecheckResult(BaseModel):
    trader_id: str
    account_key: str
    spec_key: str
    status: PrecheckStatus
    decision: str                       # underlying engine decision
    reasons: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    buffer_remaining_usd: float = 0.0
    buffer_pct: float = 0.0
    trailing_drawdown: float = 0.0
    amount_usd: float = 0.0
    withdrawable_usd: float = 0.0
    payable_usd: float = 0.0
    suggested_action: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    microscalping: Optional[dict] = None
    read_only: bool = True
    latency_ms: float = 0.0

    def to_response(self) -> dict:
        """The brief's response shape (extras included for the Ops console)."""
        return self.model_dump()


# ---------------------------------------------------------------------------
# Deterministic suggested actions (no LLM)
# ---------------------------------------------------------------------------

_CODE_ACTIONS = {
    "PAYOUT_STAGE_NOT_FUNDED":
        "Payouts are only available on funded accounts. Pass the evaluation "
        "and activate funding first.",
    "PAYOUT_KYC_UNVERIFIED":
        "Complete KYC identity verification, then re-run the pre-check.",
    "PAYOUT_METHOD_INVALID":
        "Register a valid payout method (ach, wire, wise, stripe or crypto).",
    "PAYOUT_HARD_BREACH":
        "The account has an unresolved hard breach (e.g. trailing drawdown). "
        "Contact support; a failed account is not payout-eligible.",
    "PAYOUT_SOFT_BREACH_WARNING":
        "A soft breach (e.g. daily loss limit) paused today's session. It does "
        "not fail the account; resume trading when the session allows.",
    "PAYOUT_OPEN_POSITIONS":
        "Close all open positions before requesting a payout.",
    "PAYOUT_MIN_TRADING_DAYS":
        "Keep trading until the minimum number of trading days is reached.",
    "PAYOUT_BELOW_MIN_PROFIT":
        "Current profit is below the minimum required to request a payout. "
        "Keep trading until the floor is cleared.",
    "PAYOUT_EXCEEDS_WITHDRAWABLE":
        "Reduce the requested amount so it stays within the withdrawable "
        "balance (equity minus the reserve floor).",
    "PAYOUT_BELOW_MIN":
        "Requested amount is below the per-request minimum. Increase it to at "
        "least the minimum payout.",
    "PAYOUT_ABOVE_MAX":
        "Requested amount is above the per-request maximum. Split it into "
        "smaller requests.",
    "PAYOUT_CONSISTENCY_BREACH":
        "Add smaller profitable trading days until the best-day share drops "
        "under the consistency cap, then re-run the pre-check.",
    "PAYOUT_ZERO_PAYABLE":
        "The trader share after the profit split is zero; nothing is payable.",
    "PAYOUT_EXCEEDS_EARNED_SHARE":
        "Requested amount exceeds the trader's earned share. Reduce it to the "
        "payable amount.",
    "PRECHECK_DUPLICATE_REQUEST":
        "This request_id was already used. Start a new request_id; the "
        "pre-check is read-only and will not re-decide a duplicate.",
    TRADE_RATIO_CODE:
        "Hold more than 50% of trades longer than 10 seconds. The funded "
        "microscalping rule blocks payouts while this ratio is at or below "
        "50%; the dashboard does not surface it.",
    PROFIT_RATIO_CODE:
        "Earn more than 50% of gross profit from trades held longer than 10 "
        "seconds. The funded microscalping rule blocks payouts while this "
        "ratio is at or below 50%; the dashboard does not surface it.",
}

_APPROVED_ACTION = (
    "All checks passed. Submit the payout request via POST /payout/request.")


def reason_codes(reasons: list[str]) -> list[str]:
    """Extract [CODE] prefixes from engine reasons, preserving order."""
    codes: list[str] = []
    for reason in reasons:
        codes.extend(_REASON_CODE_RE.findall(reason))
    return codes


def suggested_action(status: str, codes: list[str]) -> str:
    if status == "ELIGIBLE":
        return _APPROVED_ACTION
    if status == "MANUAL_REVIEW":
        if "PAYOUT_FIRST_PAYOUT_MANUAL" in codes:
            return ("First payout: route to an operator for manual review "
                    "before releasing funds.")
        return ("Amount is at/above the auto-approve threshold: route to an "
                "operator for fraud review.")
    for code in codes:
        if code in _CODE_ACTIONS:
            return _CODE_ACTIONS[code]
    return "Resolve the blocking reason(s) above, then re-run the pre-check."


def _buffer_figures(account: PayoutAccount) -> tuple[float, float, float]:
    spec = ACCOUNT_SPECS[account.state.spec_key]
    buffer = account.current_equity - account.state.dd_floor
    pct = (buffer / spec.trailing_drawdown * 100.0
           if spec.trailing_drawdown > 0 else 0.0)
    return round(buffer, 2), round(pct, 2), float(spec.trailing_drawdown)


# ---------------------------------------------------------------------------
# The pre-check (pure, read-only)
# ---------------------------------------------------------------------------

def precheck_payout(*, trader_id: str, account: PayoutAccount,
                    trades: Optional[list[dict]] = None,
                    kyc: Optional[KYCStatus] = None,
                    request: PayoutRequest,
                    policy: Optional[PayoutPolicy] = None,
                    existing_request_ids: Optional[set[str]] = None,
                    ) -> PrecheckResult:
    """
    Run the deterministic eligibility + amount + policy pipeline and return a
    read-only verdict. Never writes to the payout stores or idempotency store.
    """
    import time
    t0 = time.perf_counter()
    trades = trades or []
    policy = policy or PayoutPolicy()
    kyc = kyc or KYCStatus(trader_id=trader_id, status="unverified")
    buffer_usd, buffer_pct, dd = _buffer_figures(account)

    # Duplicate request guard: read-only lookup against already-used ids.
    if existing_request_ids and request.request_id in existing_request_ids:
        reason = (f"[PRECHECK_DUPLICATE_REQUEST] request_id "
                  f"{request.request_id!r} was already used; the pre-check is "
                  f"read-only and refuses to re-evaluate a duplicate.")
        codes = reason_codes([reason])
        return PrecheckResult(
            trader_id=trader_id, account_key=account.account_key,
            spec_key=account.state.spec_key, status="NOT_ELIGIBLE",
            decision="REJECTED", reasons=[reason],
            citations=["chunk_payouts"], buffer_remaining_usd=buffer_usd,
            buffer_pct=buffer_pct, trailing_drawdown=dd,
            amount_usd=request.amount_usd, reason_codes=codes,
            suggested_action=suggested_action("NOT_ELIGIBLE", codes),
            latency_ms=round((time.perf_counter() - t0) * 1000.0, 3))

    eligibility = check_eligibility(account, trades, kyc, request)
    amount = compute_payout_amount(
        account, eligibility.current_profit, request.profit_split_pct)
    decision = decide(eligibility, amount, policy)

    # Funded-only microscalping gate. Kept OUT of payout.check_eligibility
    # (protected decision logic); precheck calls it separately and merges the
    # verdict here. A FAIL is a payout block even when the engine approved.
    spec = ACCOUNT_SPECS[account.state.spec_key]
    micro = check_microscalping(trades, phase=spec.stage)

    status = _DECISION_TO_STATUS[decision.decision]
    decision_label = decision.decision
    reasons = list(decision.reasons)
    citations = list(decision.citations)
    if micro.status == "FAIL":
        # The engine may have approved; the microscalping block supersedes it,
        # so drop the now-contradictory approval reason.
        reasons = [r for r in reasons if "PAYOUT_APPROVED" not in r]
        reasons.append(micro.reason)
        citations = list(dict.fromkeys(citations + [micro.citation]))
        status = "NOT_ELIGIBLE"
        decision_label = "REJECTED"
    codes = reason_codes(reasons)
    return PrecheckResult(
        trader_id=trader_id, account_key=account.account_key,
        spec_key=account.state.spec_key, status=status,
        decision=decision_label, reasons=reasons, citations=citations,
        buffer_remaining_usd=buffer_usd, buffer_pct=buffer_pct,
        trailing_drawdown=dd, amount_usd=request.amount_usd,
        withdrawable_usd=round(eligibility.withdrawable, 2),
        payable_usd=round(amount.payable_usd, 2),
        suggested_action=suggested_action(status, codes), reason_codes=codes,
        microscalping=micro.to_dict(),
        latency_ms=round((time.perf_counter() - t0) * 1000.0, 3))


__all__ = ["PrecheckResult", "precheck_payout", "reason_codes",
           "suggested_action"]
