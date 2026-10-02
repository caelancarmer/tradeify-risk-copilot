"""
rule_engine.py -- Deterministic Tradeify rule engine (pure Python, no LLM).

Design principle (from the DeepSeek plan): NEVER let the LLM decide rule
logic. Rules are encoded here as pure functions. The LLM only explains the
engine's output, with citations to retrieved documentation chunks.

Sources (verified 2026-10-02):
  [S1] https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders
       (official Tradeify blog: full rule tables for Growth/Select/Lightning)
  [S2] https://damnpropfirms.com/futures-prop-firms/tradeify/
       (EOD trailing mechanics, microscalp rule, soft DLL on Growth)
  [S3] https://blog.traderspost.io/article/tradeify-review
       (flat-by-4:59PM ET, no hedging, max 5 funded accounts, 1 trade/week)

Fields marked VERIFY are plausible but not confirmed by an official source;
the engine surfaces them as configurable parameters, never as facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Account catalogue. Numbers are USD. `dll=None` means the account family has
# no daily loss limit. `consistency` is the max share of total profit that may
# come from the single best day, in percent; `None` = no consistency rule.
# `consistency_progressive` overrides `consistency` by payout count (Lightning).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountSpec:
    key: str
    family: str            # growth | select | lightning
    stage: str             # eval | funded
    size: int
    daily_loss_limit: Optional[int]   # None = no DLL
    dll_severity: str = "soft"        # soft = session paused, NOT failed
    trailing_drawdown: int = 0
    consistency: Optional[float] = None
    consistency_progressive: Optional[tuple] = None  # e.g. (20, 25, 30)
    profit_target: Optional[int] = None
    source: str = "[S1]"

ACCOUNT_SPECS: dict[str, AccountSpec] = {}

def _reg(key, family, stage, size, dll, dd, cons=None, cons_prog=None,
         target=None, dll_sev="soft"):
    ACCOUNT_SPECS[key] = AccountSpec(
        key=key, family=family, stage=stage, size=size,
        daily_loss_limit=dll, dll_severity=dll_sev,
        trailing_drawdown=dd, consistency=cons,
        consistency_progressive=cons_prog, profit_target=target)

# --- Growth evaluation (no consistency, soft DLL, 1-day pass possible) ---
_reg("growth_eval_50k",  "growth", "eval", 50000, 1250, 2000, target=3000)
_reg("growth_eval_100k", "growth", "eval", 100000, 2500, 3500, target=6000)
_reg("growth_eval_150k", "growth", "eval", 150000, 3750, 5000, target=9000)
# --- Select evaluation (no DLL, 40% consistency) ---
_reg("select_eval_50k",  "select", "eval", 50000, None, 2000, cons=40.0, target=2500)
_reg("select_eval_100k", "select", "eval", 100000, None, 3000, cons=40.0, target=6000)
_reg("select_eval_150k", "select", "eval", 150000, None, 4500, cons=40.0, target=9000)
# --- Growth funded (35% consistency) ---
_reg("growth_funded_50k",  "growth", "funded", 50000, 1250, 2000, cons=35.0)
_reg("growth_funded_100k", "growth", "funded", 100000, 2500, 3500, cons=35.0)
_reg("growth_funded_150k", "growth", "funded", 150000, 3750, 5000, cons=35.0)
# --- Select Flex funded (5-day path: no DLL, no consistency) ---
_reg("select_flex_50k",  "select", "funded", 50000, None, 2000)
_reg("select_flex_100k", "select", "funded", 100000, None, 3000)
_reg("select_flex_150k", "select", "funded", 150000, None, 4500)
# --- Select Daily funded (lower DLL than Growth, no consistency) ---
# VERIFY: DLL severity on Select Daily is not confirmed official; default soft.
_reg("select_daily_50k",  "select", "funded", 50000, 1000, 2000)
_reg("select_daily_100k", "select", "funded", 100000, 1250, 3000)
_reg("select_daily_150k", "select", "funded", 150000, 1750, 4500)
# --- Lightning (instant funding, progressive consistency 20/25/30) ---
_reg("lightning_25k",  "lightning", "funded", 25000, None, 1000, cons_prog=(20.0, 25.0, 30.0))
_reg("lightning_50k",  "lightning", "funded", 50000, 1250, 2000, cons_prog=(20.0, 25.0, 30.0))
_reg("lightning_100k", "lightning", "funded", 100000, 2500, 4000, cons_prog=(20.0, 25.0, 30.0))
_reg("lightning_150k", "lightning", "funded", 150000, 3750, 6000, cons_prog=(20.0, 25.0, 30.0))

# Microscalp rule: >50% of trades held longer than N seconds AND >50% of
# profit from trades held longer than N seconds. [S2] says 10s; one review
# site says 20s -> parameterised, default 10, flagged VERIFY.
MICROSCALP_MIN_SECONDS = 10          # VERIFY: 10s [S2] vs 20s (one review)
MICROSCALP_MIN_RATIO = 0.50

# Session rules [S3]
FLAT_TIME_WEEKDAY = time(16, 59)     # all positions flat by 4:59 PM ET
FLAT_TIME_EARLY_CLOSE = time(12, 59) # 12:59 PM ET on early-close holidays
MAX_FUNDED_ACCOUNTS = 5
MIN_TRADES_PER_WEEK = 1

LOCK_BUFFER = 100  # DD locks at start_balance + 100 once EOD clears start+DD+100


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    rule_id: str
    rule: str
    status: str          # OK | WARNING | BREACHED
    severity: str        # INFO | SOFT_BREACH | HARD_BREACH
    message: str
    citation: str        # chunk id in the retrieval corpus
    value: dict = field(default_factory=dict)

    def to_dict(self):
        return {"rule_id": self.rule_id, "rule": self.rule, "status": self.status,
                "severity": self.severity, "message": self.message,
                "citation": self.citation, "value": self.value}


@dataclass
class AccountState:
    """Mutable per-account state tracked by the worker."""
    spec_key: str
    start_balance: float
    dd_floor: float            # current trailing drawdown floor (equity)
    dd_locked: bool = False
    payouts_taken: int = 0
    session_trading_paused: bool = False

    @classmethod
    def new(cls, spec_key: str) -> "AccountState":
        spec = ACCOUNT_SPECS[spec_key]
        return cls(spec_key=spec_key, start_balance=float(spec.size),
                   dd_floor=float(spec.size - spec.trailing_drawdown))


# ---------------------------------------------------------------------------
# Rule checks (pure functions)
# ---------------------------------------------------------------------------

def check_daily_loss_limit(spec: AccountSpec, day_pnl: float) -> Finding:
    """DLL: breaching pauses the session (soft), it does NOT fail the account."""
    if spec.daily_loss_limit is None:
        return Finding("dll", "Daily Loss Limit", "OK", "INFO",
                       f"{spec.key} has no daily loss limit.", "chunk_dll",
                       {"day_pnl": day_pnl, "limit": None})
    if day_pnl <= -spec.daily_loss_limit:
        return Finding("dll", "Daily Loss Limit", "BREACHED", "SOFT_BREACH",
                       f"Day PnL {day_pnl:+.0f} hit the -${spec.daily_loss_limit:,} "
                       f"daily loss limit. Trading is PAUSED for the rest of the "
                       f"session; the account is NOT failed.", "chunk_dll",
                       {"day_pnl": day_pnl, "limit": spec.daily_loss_limit,
                        "remaining": 0.0})
    return Finding("dll", "Daily Loss Limit", "OK", "INFO",
                   f"Day PnL {day_pnl:+.0f}; ${day_pnl + spec.daily_loss_limit:,.0f} "
                   f"of DLL room left.", "chunk_dll",
                   {"day_pnl": day_pnl, "limit": spec.daily_loss_limit,
                    "remaining": day_pnl + spec.daily_loss_limit})


def update_dd_floor(state: AccountState, eod_balance: float) -> tuple[float, bool]:
    """
    EOD trailing drawdown mechanics [S1][S2]:
      - floor trails UP with the highest end-of-day balance (never intraday)
      - floor LOCKS permanently at start_balance + LOCK_BUFFER once an EOD
        balance clears start_balance + trailing_drawdown + LOCK_BUFFER
        (or at first payout, whichever comes first)
    Returns (new_floor, just_locked).
    """
    spec = ACCOUNT_SPECS[state.spec_key]
    if state.dd_locked:
        return state.dd_floor, False
    candidate = eod_balance - spec.trailing_drawdown
    new_floor = max(state.dd_floor, candidate)
    lock_level = state.start_balance + spec.trailing_drawdown + LOCK_BUFFER
    if eod_balance >= lock_level:
        new_floor = state.start_balance + LOCK_BUFFER
        state.dd_locked = True
        state.dd_floor = new_floor
        return new_floor, True
    state.dd_floor = new_floor
    return new_floor, False


def check_trailing_drawdown(state: AccountState, current_equity: float) -> Finding:
    """Intraday equity at/below the floor = HARD breach, account failed. [S1][S2]"""
    spec = ACCOUNT_SPECS[state.spec_key]
    if current_equity <= state.dd_floor:
        return Finding("trailing_dd", "Max Trailing Drawdown (EOD)", "BREACHED",
                       "HARD_BREACH",
                       f"Equity ${current_equity:,.0f} hit the trailing floor "
                       f"${state.dd_floor:,.0f}. HARD breach: account failed "
                       f"permanently.", "chunk_trailing_dd",
                       {"equity": current_equity, "floor": state.dd_floor,
                        "distance": 0.0})
    return Finding("trailing_dd", "Max Trailing Drawdown (EOD)", "OK", "INFO",
                   f"Equity ${current_equity:,.0f} is "
                   f"${current_equity - state.dd_floor:,.0f} above the trailing "
                   f"floor ${state.dd_floor:,.0f}"
                   f"{' (LOCKED)' if state.dd_locked else ''}.", "chunk_trailing_dd",
                   {"equity": current_equity, "floor": state.dd_floor,
                    "distance": current_equity - state.dd_floor,
                    "locked": state.dd_locked})


def _consistency_limit(spec: AccountSpec, payouts_taken: int) -> Optional[float]:
    if spec.consistency_progressive is not None:
        idx = min(payouts_taken, len(spec.consistency_progressive) - 1)
        return spec.consistency_progressive[idx]
    return spec.consistency


def check_consistency(spec: AccountSpec, daily_profits: list[float],
                     payouts_taken: int = 0) -> Finding:
    """
    Consistency: best_day_profit / total_profit <= limit.
    Breaking it NEVER fails the account; it only delays payout eligibility
    until smaller profitable days bring the ratio back under the cap. [S1]
    """
    limit = _consistency_limit(spec, payouts_taken)
    if limit is None:
        return Finding("consistency", "Consistency Rule", "OK", "INFO",
                       f"{spec.key} has no consistency rule.", "chunk_consistency",
                       {"limit_pct": None})
    total = sum(p for p in daily_profits if p > 0)
    best = max(daily_profits) if daily_profits else 0.0
    ratio = (best / total * 100.0) if total > 0 else 0.0
    if total > 0 and ratio > limit:
        return Finding("consistency", "Consistency Rule", "BREACHED", "INFO",
                       f"Best day ${best:,.0f} is {ratio:.1f}% of total profit "
                       f"${total:,.0f}, over the {limit:.0f}% cap. Account NOT "
                       f"failed; payout eligibility delayed until the ratio "
                       f"drops back under {limit:.0f}%.", "chunk_consistency",
                       {"best_day": best, "total_profit": total,
                        "ratio_pct": round(ratio, 1), "limit_pct": limit})
    return Finding("consistency", "Consistency Rule", "OK", "INFO",
                   f"Best-day share {ratio:.1f}% is within the {limit:.0f}% cap.",
                   "chunk_consistency",
                   {"best_day": best, "total_profit": total,
                    "ratio_pct": round(ratio, 1), "limit_pct": limit})


def check_profit_target(spec: AccountSpec, current_balance: float) -> Finding:
    if spec.profit_target is None:
        return Finding("profit_target", "Profit Target", "OK", "INFO",
                       f"{spec.key} has no profit target (funded stage).",
                       "chunk_profit_target", {})
    pnl = current_balance - spec.size
    if pnl >= spec.profit_target:
        return Finding("profit_target", "Profit Target", "OK", "INFO",
                       f"Target HIT: +${pnl:,.0f} vs ${spec.profit_target:,} target.",
                       "chunk_profit_target",
                       {"pnl": pnl, "target": spec.profit_target, "remaining": 0.0})
    return Finding("profit_target", "Profit Target", "OK", "INFO",
                   f"+${pnl:,.0f} / ${spec.profit_target:,} target "
                   f"(${spec.profit_target - pnl:,.0f} to go).", "chunk_profit_target",
                   {"pnl": pnl, "target": spec.profit_target,
                    "remaining": spec.profit_target - pnl})


def check_microscalp(trades: list[dict]) -> Finding:
    """
    trades: [{"hold_seconds": float, "pnl": float}, ...]
    Rule [S2]: >50% of trades held longer than MICROSCALP_MIN_SECONDS AND
    >50% of profit from trades held longer than MICROSCALP_MIN_SECONDS.
    """
    if not trades:
        return Finding("microscalp", "Microscalp Rule", "OK", "INFO",
                       "No trades to evaluate.", "chunk_microscalp", {})
    long_trades = [t for t in trades if t["hold_seconds"] > MICROSCALP_MIN_SECONDS]
    trade_ratio = len(long_trades) / len(trades)
    total_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    long_profit = sum(t["pnl"] for t in long_trades if t["pnl"] > 0)
    profit_ratio = (long_profit / total_profit) if total_profit > 0 else 1.0
    ok = trade_ratio > MICROSCALP_MIN_RATIO and profit_ratio > MICROSCALP_MIN_RATIO
    return Finding(
        "microscalp", "Microscalp Rule",
        "OK" if ok else "WARNING", "INFO",
        f"{trade_ratio:.0%} of trades and {profit_ratio:.0%} of profit from holds "
        f">{MICROSCALP_MIN_SECONDS}s (need >{MICROSCALP_MIN_RATIO:.0%} on both). "
        + ("Compliant." if ok else "Below threshold: activation/payout blocked until "
           "both ratios clear with longer holds. [S2]"),
        "chunk_microscalp",
        {"trade_ratio": round(trade_ratio, 3), "profit_ratio": round(profit_ratio, 3),
         "min_seconds": MICROSCALP_MIN_SECONDS})


def check_flat_time(now: Optional[datetime] = None, early_close: bool = False) -> Finding:
    """All positions must be flat by 4:59 PM ET (12:59 PM ET early close). [S3]"""
    now = now or datetime.now(ET)
    cutoff = FLAT_TIME_EARLY_CLOSE if early_close else FLAT_TIME_WEEKDAY
    is_weekday = now.weekday() < 5
    past_cutoff = now.timetz() >= cutoff
    if is_weekday and past_cutoff:
        return Finding("flat_time", "Flat-by Rule", "BREACHED", "HARD_BREACH",
                       f"{now.strftime('%H:%M %Z')}: past the "
                       f"{cutoff.strftime('%-I:%M %p')} ET flat deadline. Positions "
                       f"must be closed; overnight/weekend holds are not allowed.",
                       "chunk_session_rules", {})
    return Finding("flat_time", "Flat-by Rule", "OK", "INFO",
                   f"{now.strftime('%H:%M %Z')}: before the "
                   f"{cutoff.strftime('%-I:%M %p')} ET flat deadline.", "chunk_session_rules", {})


def evaluate_all(state: AccountState, *, day_pnl: float, current_equity: float,
                 daily_profits: list[float], trades: list[dict],
                 current_balance: float,
                 now: Optional[datetime] = None) -> list[Finding]:
    """Run every deterministic check; returns findings in fixed order."""
    spec = ACCOUNT_SPECS[state.spec_key]
    return [
        check_daily_loss_limit(spec, day_pnl),
        check_trailing_drawdown(state, current_equity),
        check_consistency(spec, daily_profits, state.payouts_taken),
        check_profit_target(spec, current_balance),
        check_microscalp(trades),
        check_flat_time(now),
    ]


def summarize(findings: list[Finding]) -> dict:
    hard = [f for f in findings if f.severity == "HARD_BREACH"]
    soft = [f for f in findings if f.severity == "SOFT_BREACH"]
    breached = [f for f in findings if f.status == "BREACHED"]
    warnings = [f for f in findings if f.status == "WARNING"]
    return {
        "account_failed": bool(hard),
        "session_paused": bool(soft),
        "n_breached": len(breached),
        "n_warnings": len(warnings),
        "hard": [f.to_dict() for f in hard],
        "soft": [f.to_dict() for f in soft],
        "all": [f.to_dict() for f in findings],
    }
