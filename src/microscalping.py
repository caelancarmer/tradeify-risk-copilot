"""
microscalping.py -- Silent Tradeify microscalping gate for FUNDED accounts.

The rule (verified from help.tradeify.co and several independent sources):
    On a FUNDED account, BOTH of these must hold for a payout:
      * MORE THAN 50% of trades were held longer than 10 seconds, AND
      * MORE THAN 50% of gross profit came from trades held longer than 10s.
    Falling at or under either threshold blocks the payout. The rule does NOT
    apply during an evaluation -- only once the account is funded.

Why this is a separate module:
    ``rule_engine.check_microscalp`` already *reports* the ratios, but as an
    advisory INFO finding that never blocks. This module is the explicit,
    funded-only gate the pre-check consults. It is pure: no I/O, no LLM.

The ``trades`` shape is the one already used by ``payout.check_eligibility``
and must not change: ``[{"hold_seconds": float, "pnl": float}, ...]``.

Statuses:
    PASS            both ratios are strictly greater than 50%.
    FAIL            at least one ratio is <= 50% -> payout must be blocked.
    NEED_MORE_DATA  nothing to judge (no trades, or no positive gross profit).
    SKIP            not a funded account; the rule does not apply.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

MicroscalpingStatus = Literal["PASS", "FAIL", "NEED_MORE_DATA", "SKIP"]

MICROSCALP_MIN_SECONDS = 10.0
MICROSCALP_MIN_RATIO = 0.50
MICROSCALP_CITATION = "chunk_microscalp"

TRADE_RATIO_CODE = "PAYOUT_MICROSCALPING_TRADE_RATIO"
PROFIT_RATIO_CODE = "PAYOUT_MICROSCALPING_PROFIT_RATIO"


class MicroscalpingResult(BaseModel):
    """Outcome of the funded-only microscalping gate."""

    status: MicroscalpingStatus
    trade_ratio: Optional[float] = None
    profit_ratio: Optional[float] = None
    n_trades: int = 0
    n_long_holds: int = 0
    reason: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    citation: str = MICROSCALP_CITATION

    def to_dict(self) -> dict:
        return self.model_dump()


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100.0:.1f}%"


def check_microscalping(trades: list[dict],
                        phase: str = "funded") -> MicroscalpingResult:
    """
    Evaluate the funded microscalping rule.

    ``phase`` is the account stage (``ACCOUNT_SPECS[spec_key].stage``): the
    rule is only enforced when it is ``"funded"``. The single-argument call
    ``check_microscalping(trades)`` defaults to funded, matching the brief.

    Empty trade history or a non-positive gross profit is never a FAIL; it is
    NEED_MORE_DATA so a fresh funded account is not falsely blocked.
    """
    trades = trades or []
    if phase != "funded":
        return MicroscalpingResult(
            status="SKIP",
            reason=(f"microscalping rule does not apply to {phase!r} accounts; "
                    "it is enforced only on funded accounts."),
            citation=MICROSCALP_CITATION)

    n_trades = len(trades)
    if n_trades == 0:
        return MicroscalpingResult(
            status="NEED_MORE_DATA", n_trades=0, n_long_holds=0,
            reason="no trades yet; microscalping ratios are undefined.",
            citation=MICROSCALP_CITATION)

    long_trades = [
        t for t in trades
        if float(t["hold_seconds"]) > MICROSCALP_MIN_SECONDS
    ]
    n_long_holds = len(long_trades)
    trade_ratio = n_long_holds / n_trades

    gross_profit = sum(
        float(t["pnl"]) for t in trades if float(t["pnl"]) > 0.0)
    if gross_profit <= 0.0:
        return MicroscalpingResult(
            status="NEED_MORE_DATA", trade_ratio=round(trade_ratio, 4),
            profit_ratio=None, n_trades=n_trades, n_long_holds=n_long_holds,
            reason=(f"gross profit is ${gross_profit:,.2f} (<= 0); the profit "
                    "ratio is undefined, so microscalping cannot be judged."),
            citation=MICROSCALP_CITATION)

    long_profit = sum(float(t["pnl"]) for t in long_trades)
    profit_ratio = long_profit / gross_profit

    failures: list[str] = []
    codes: list[str] = []
    if trade_ratio <= MICROSCALP_MIN_RATIO:
        codes.append(TRADE_RATIO_CODE)
        failures.append(
            f"[{TRADE_RATIO_CODE}] only {_pct(trade_ratio)} of trades "
            f"({n_long_holds}/{n_trades}) were held longer than "
            f"{MICROSCALP_MIN_SECONDS:.0f}s; Tradeify requires more than "
            f"{MICROSCALP_MIN_RATIO:.0%}.")
    if profit_ratio <= MICROSCALP_MIN_RATIO:
        codes.append(PROFIT_RATIO_CODE)
        failures.append(
            f"[{PROFIT_RATIO_CODE}] only {_pct(profit_ratio)} of gross profit "
            f"(${long_profit:,.2f} of ${gross_profit:,.2f}) came from trades "
            f"held longer than {MICROSCALP_MIN_SECONDS:.0f}s; Tradeify "
            f"requires more than {MICROSCALP_MIN_RATIO:.0%}.")

    if failures:
        return MicroscalpingResult(
            status="FAIL", trade_ratio=round(trade_ratio, 4),
            profit_ratio=round(profit_ratio, 4), n_trades=n_trades,
            n_long_holds=n_long_holds, reason=" ".join(failures),
            reason_codes=codes, citation=MICROSCALP_CITATION)

    return MicroscalpingResult(
        status="PASS", trade_ratio=round(trade_ratio, 4),
        profit_ratio=round(profit_ratio, 4), n_trades=n_trades,
        n_long_holds=n_long_holds,
        reason=(f"{_pct(trade_ratio)} of trades ({n_long_holds}/{n_trades}) "
                f"and {_pct(profit_ratio)} of gross profit came from holds "
                f"longer than {MICROSCALP_MIN_SECONDS:.0f}s; both exceed "
                f"{MICROSCALP_MIN_RATIO:.0%}."),
        citation=MICROSCALP_CITATION)


__all__ = [
    "MICROSCALP_CITATION",
    "MICROSCALP_MIN_RATIO",
    "MICROSCALP_MIN_SECONDS",
    "PROFIT_RATIO_CODE",
    "TRADE_RATIO_CODE",
    "MicroscalpingResult",
    "check_microscalping",
]
