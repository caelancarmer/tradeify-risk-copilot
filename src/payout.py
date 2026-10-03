"""
payout.py -- Deterministic payout decision engine (pure Python, no LLM).

Design principle (same as rule_engine.py): NEVER let the LLM decide payout
eligibility. Payout logic is encoded here as pure functions returning Pydantic
models; the LLM may only explain the engine's output, with citations to
retrieved documentation chunks (see ``explain_payout_decision`` at the bottom).

Pipeline (all deterministic):
    check_eligibility(account, trades, kyc, request) -> EligibilityResult
    compute_payout_amount(account, profit, profit_split_pct) -> PayoutAmount
    decide(eligibility, amount, policy) -> Decision

Invariants enforced here:
  1. Every REJECTED decision carries at least one citation to a specific
     rulebook chunk. No rejection without a citation.
  2. The same request_id + same fingerprint returns the same Decision forever
     (in-memory store; production swaps it for Postgres).
  3. The same request_id + a DIFFERENT fingerprint is flagged as an
     idempotency conflict and REJECTED -- it is never silently re-decided.

Sources (see rule_engine.py header):
  [S1] https://tradeify.co/post/prop-firm-drawdown-recovery-plan-funded-traders
  [S3] https://blog.traderspost.io/article/tradeify-review
  Payout mechanics: data/rulebook_chunks.json::chunk_payouts

Fields marked VERIFY are plausible but not confirmed by an official source;
the engine surfaces them as configurable parameters, never as facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from rule_engine import (
    ACCOUNT_SPECS,
    AccountState,
    check_consistency,
    evaluate_all,
    summarize,
)

# ---------------------------------------------------------------------------
# Configurable thresholds. Anything not confirmed by an official source is
# marked VERIFY, following the rule_engine.py convention.
# ---------------------------------------------------------------------------

# VERIFY: no official Tradeify page confirms a minimum number of trading days
# before a payout. 5 is a common prop-firm baseline and is exposed as a knob.
MIN_PAYOUT_TRADING_DAYS = 5

# VERIFY: per-request and minimum-profit bounds are policy choices, not
# confirmed official numbers. They are deliberately conservative.
MIN_PAYOUT_PROFIT_USD = 100
MIN_PAYOUT_USD = 100
MAX_PAYOUT_USD = 10_000

# POLICY CHOICE (documented in README): payouts below this auto-approve;
# at/above it the decision is MANUAL_REVIEW because fraud risk > delay.
AUTO_APPROVE_THRESHOLD_USD = 500.0        # VERIFY: policy threshold

# POLICY CHOICE: first payout of a trader is always manual. Automatic only
# afterwards. Exposed as a policy flag so an operator can override it.
FIRST_PAYOUT_MANUAL = True

# POLICY CHOICE: a soft breach (e.g. DLL hit) pauses a session, it does NOT
# fail the account, so by default it warns instead of blocking a payout.
SOFT_BREACH_IS_BLOCKING = False

# Citation chunk ids used by this module (ids resolve in
# data/rulebook_chunks.json and are verifiable by retrieval.py).
CITE_PAYOUTS = "chunk_payouts"
CITE_TRAILING_DD = "chunk_trailing_dd"
CITE_CONSISTENCY = "chunk_consistency"
CITE_FAMILIES = "chunk_account_families"
CITE_DLL = "chunk_dll"

# KYC + payout-method integration stubs. Real providers replace these; the
# engines below only need the resolved status, not the provider call.
VALID_PAYOUT_METHODS: frozenset[str] = frozenset(
    {"ach", "wire", "wise", "stripe", "crypto"})
KYC_STUB_DEFAULT = "unverified"           # explicit stub, override per request

CITATION_RE = re.compile(r"\[(chunk_[a-z_]+)\]")
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

PayoutStatus = Literal["APPROVED", "REJECTED", "MANUAL_REVIEW"]


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class KYCStatus(BaseModel):
    """KYC integration stub: the provider resolves this before payout runs."""
    trader_id: str = "trader"
    status: Literal["verified", "pending", "rejected", "unverified"] = KYC_STUB_DEFAULT
    verified_at: Optional[str] = None
    method: Optional[str] = None

    @property
    def is_verified(self) -> bool:
        return self.status == "verified"


class PayoutRequest(BaseModel):
    """A trader's payout request against one account."""
    request_id: str
    account_key: str
    trader_id: str = "trader"
    amount_usd: float
    kyc_verified: bool = False
    trading_days: int = 0
    open_positions: int = 0
    daily_profits: list[float] = Field(default_factory=list)
    current_equity: Optional[float] = None
    payout_method: str = "ach"
    profit_split_pct: float = 90.0       # chunk_payouts: 90/10 trader-favor


class PayoutAccount(BaseModel):
    """Account state + the live snapshot the rule engine needs."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_key: str
    state: AccountState
    current_equity: float
    day_pnl: float = 0.0
    current_balance: Optional[float] = None

    @classmethod
    def from_state(cls, account_key: str, state: AccountState,
                   current_equity: float, day_pnl: float = 0.0,
                   current_balance: Optional[float] = None) -> "PayoutAccount":
        return cls(account_key=account_key, state=state,
                   current_equity=current_equity, day_pnl=day_pnl,
                   current_balance=current_balance)


class PayoutPolicy(BaseModel):
    """Deterministic policy knobs (no LLM involved)."""
    auto_approve_threshold_usd: float = AUTO_APPROVE_THRESHOLD_USD
    first_payout_manual: bool = FIRST_PAYOUT_MANUAL
    min_trading_days: int = MIN_PAYOUT_TRADING_DAYS
    min_payout_usd: float = MIN_PAYOUT_USD
    max_payout_usd: float = MAX_PAYOUT_USD
    min_payout_profit_usd: float = MIN_PAYOUT_PROFIT_USD
    soft_breach_blocking: bool = SOFT_BREACH_IS_BLOCKING


# ---------------------------------------------------------------------------
# Pydantic output models
# ---------------------------------------------------------------------------

class CheckResult(BaseModel):
    rule_id: str
    rule: str
    status: Literal["OK", "WARN", "FAIL"]
    severity: str = "INFO"
    message: str
    citation: str
    value: dict = Field(default_factory=dict)


class EligibilityResult(BaseModel):
    request_id: str
    account_key: str
    eligible: bool
    reasons: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    requested_amount: float = 0.0
    withdrawable: float = 0.0
    current_profit: float = 0.0
    first_payout: bool = False
    pending_soft_breach: bool = False
    family: str = ""
    spec_key: str = ""


class PayoutAmount(BaseModel):
    gross_profit_usd: float
    profit_split_pct: float
    trader_share_usd: float
    withdrawable_usd: float
    payable_usd: float
    currency: str = "USD"
    note: str = ""


class Decision(BaseModel):
    request_id: str
    decision: PayoutStatus
    amount_usd: float
    reasons: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    decided_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat())
    decided_by: str = "engine"
    note: str = ""
    eligibility: Optional[EligibilityResult] = None
    checks: list[CheckResult] = Field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.decision == "APPROVED"

    def to_dict(self) -> dict:
        return self.model_dump()


class PayoutExplanation(BaseModel):
    request_id: str
    explanation: str
    citations: list[str] = Field(default_factory=list)
    citation_precision: float = 0.0
    tier: str = "explain"
    model: str = ""
    numbers_unchanged: bool = True
    deterministic_fallback: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for i in items:
        seen.setdefault(i, None)
    return list(seen)


def _withdrawable(state: AccountState, current_equity: float) -> float:
    """
    Maximum amount this account may pay out right now.

        withdrawable = max(0, equity - max(dd_floor, start_balance))

    A payout must leave the account (a) at or above its starting balance, so
    the trader never withdraws starting capital, and (b) strictly above the
    trailing drawdown floor, so the withdrawal cannot push the account into a
    hard trailing-DD breach. The stricter (larger) constraint is the reserve.
    """
    reserve_floor = max(state.dd_floor, state.start_balance)
    return max(0.0, current_equity - reserve_floor)


def request_fingerprint(request: PayoutRequest) -> str:
    """Stable fingerprint of the economically meaningful request fields."""
    payload = {
        "account_key": request.account_key,
        "trader_id": request.trader_id,
        "amount_usd": round(float(request.amount_usd), 6),
        "trading_days": int(request.trading_days),
        "open_positions": int(request.open_positions),
        "daily_profits": [round(float(p), 6) for p in request.daily_profits],
        "current_equity": (None if request.current_equity is None
                           else round(float(request.current_equity), 6)),
        "payout_method": request.payout_method,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Rule 1: eligibility (pure, deterministic)
# ---------------------------------------------------------------------------

def check_eligibility(account: PayoutAccount, trades: list[dict],
                      kyc: KYCStatus, request: PayoutRequest) -> EligibilityResult:
    """
    Run every deterministic eligibility rule in a fixed order. Each failed
    check contributes a code-prefixed reason and the rulebook chunk that
    justifies it. ``eligible`` is True only when no check has status FAIL.

    ``trades`` uses rule_engine.py's shape: [{"hold_seconds", "pnl"}, ...].
    """
    trades = trades or []
    spec = ACCOUNT_SPECS[account.state.spec_key]
    equity = account.current_equity
    balance = (account.current_balance if account.current_balance is not None
               else equity)
    checks: list[CheckResult] = []
    reasons: list[str] = []

    def fail(rule_id: str, rule: str, message: str, citation: str,
             value: dict | None = None) -> None:
        checks.append(CheckResult(rule_id=rule_id, rule=rule, status="FAIL",
                                  message=message, citation=citation,
                                  value=value or {}))

    def warn(rule_id: str, rule: str, message: str, citation: str,
             value: dict | None = None) -> None:
        checks.append(CheckResult(rule_id=rule_id, rule=rule, status="WARN",
                                  message=message, citation=citation,
                                  value=value or {}))

    def ok(rule_id: str, rule: str, message: str, citation: str,
           value: dict | None = None) -> None:
        checks.append(CheckResult(rule_id=rule_id, rule=rule, status="OK",
                                  message=message, citation=citation,
                                  value=value or {}))

    # 1. Only funded accounts can pay out; evaluation accounts cannot. [S1]
    if spec.stage != "funded":
        fail("stage", "Funded Account Required",
             f"[PAYOUT_STAGE_NOT_FUNDED] {spec.key} is an evaluation account; "
             f"payouts are only available once the account is funded.",
             CITE_FAMILIES, {"stage": spec.stage})
    else:
        ok("stage", "Funded Account Required",
           f"{spec.key} is a funded {spec.family} account.", CITE_FAMILIES,
           {"stage": spec.stage})

    # 2. KYC must be verified (explicit provider stub; override per request).
    if not kyc.is_verified:
        fail("kyc", "KYC Verification",
             f"[PAYOUT_KYC_UNVERIFIED] KYC identity verification is "
             f"{kyc.status!r}; payouts cannot be released until verification "
             f"passes.", CITE_PAYOUTS, {"kyc_status": kyc.status})
    else:
        ok("kyc", "KYC Verification",
           f"KYC verified via {kyc.method or 'stub provider'}.", CITE_PAYOUTS,
           {"kyc_status": kyc.status})

    # 3. Payout method must be registered and valid (explicit stub).
    method = (request.payout_method or "").strip().lower()
    if method not in VALID_PAYOUT_METHODS:
        fail("payout_method", "Payout Method",
             f"[PAYOUT_METHOD_INVALID] Payout method {request.payout_method!r} "
             f"is not registered; valid methods: "
             f"{sorted(VALID_PAYOUT_METHODS)}.", CITE_PAYOUTS,
             {"method": request.payout_method})
    else:
        ok("payout_method", "Payout Method",
           f"Payout method {method!r} is registered.", CITE_PAYOUTS,
           {"method": method})

    # 4. No hard breach may be active (trailing-DD failure etc.).
    findings = evaluate_all(
        account.state, day_pnl=account.day_pnl, current_equity=equity,
        daily_profits=request.daily_profits, trades=trades,
        current_balance=balance)
    rep = summarize(findings)
    # The flat-by-4:59PM finding is a session WALL-CLOCK rule about holding
    # positions past the deadline; it depends on when the request happens to
    # be evaluated. Open positions are enforced independently (rule 6) and are
    # a deterministic input, so flat_time is always excluded from the hard
    # breaches that block a payout. This makes payout decisions reproducible
    # at any hour and keeps the engine pure.
    hard = [f for f in rep["hard"] if f["rule_id"] != "flat_time"]
    if hard:
        primary = hard[0]
        fail("hard_breach", "No Active Hard Breach",
             f"[PAYOUT_HARD_BREACH] Account has an unresolved hard breach "
             f"({primary.get('rule_id', 'unknown')}: "
             f"{primary.get('message', 'failed')}); a failed account is not "
             f"payout-eligible.", primary.get("citation", CITE_TRAILING_DD),
             {"hard_breaches": [f["rule_id"] for f in hard]})
    else:
        ok("hard_breach", "No Active Hard Breach",
           "No unresolved hard breach detected.", CITE_TRAILING_DD, {})

    # 5. Pending soft breach: advisory by default (soft != failed). The
    #    ``soft_breach_blocking`` policy knob can promote it to blocking.
    soft = rep["soft"]
    if soft:
        s0 = soft[0]
        warn("pending_violation", "No Pending Violation",
             f"[PAYOUT_SOFT_BREACH_WARNING] Soft breach active "
             f"({s0.get('rule_id')}: {s0.get('message')}); advisory only "
             f"(pauses the session, does not fail the account).",
             s0.get("citation", CITE_DLL),
             {"soft_breaches": [f["rule_id"] for f in soft]})
    else:
        ok("pending_violation", "No Pending Violation",
           "No pending soft or hard violation.", CITE_DLL, {})

    # 6. Positions must be flat to pay out.
    if request.open_positions != 0:
        fail("open_positions", "Flat Positions",
             f"[PAYOUT_OPEN_POSITIONS] {request.open_positions} open "
             f"position(s); all positions must be flat before a payout is "
             f"released.", CITE_PAYOUTS, {"open_positions": request.open_positions})
    else:
        ok("open_positions", "Flat Positions", "No open positions.",
           CITE_PAYOUTS, {})

    # 7. Minimum trading days.
    if request.trading_days < MIN_PAYOUT_TRADING_DAYS:
        fail("min_trading_days", "Minimum Trading Days",
             f"[PAYOUT_MIN_TRADING_DAYS] {request.trading_days} trading day(s) "
             f"completed; at least {MIN_PAYOUT_TRADING_DAYS} are required "
             f"(threshold VERIFY: not confirmed by an official source).",
             CITE_PAYOUTS, {"trading_days": request.trading_days,
                            "required": MIN_PAYOUT_TRADING_DAYS})
    else:
        ok("min_trading_days", "Minimum Trading Days",
           f"{request.trading_days} trading days completed (>= "
           f"{MIN_PAYOUT_TRADING_DAYS} required).", CITE_PAYOUTS,
           {"trading_days": request.trading_days})

    # 8. Profitability floor + withdrawable cap.
    current_profit = balance - account.state.start_balance
    withdrawable = _withdrawable(account.state, equity)
    if current_profit < MIN_PAYOUT_PROFIT_USD:
        fail("min_profit", "Minimum Profit",
             f"[PAYOUT_BELOW_MIN_PROFIT] Current profit "
             f"${current_profit:,.2f} is below the ${MIN_PAYOUT_PROFIT_USD:,} "
             f"minimum required to request a payout (threshold VERIFY).",
             CITE_PAYOUTS, {"current_profit": current_profit})
    elif request.amount_usd > withdrawable:
        fail("withdrawable", "Withdrawable Balance",
             f"[PAYOUT_EXCEEDS_WITHDRAWABLE] Requested "
             f"${request.amount_usd:,.2f} exceeds withdrawable "
             f"${withdrawable:,.2f} (equity ${equity:,.2f} minus reserve floor "
             f"${max(account.state.dd_floor, account.state.start_balance):,.2f}).",
             CITE_TRAILING_DD, {"requested": request.amount_usd,
                                "withdrawable": withdrawable})
    else:
        ok("withdrawable", "Withdrawable Balance",
           f"Withdrawable ${withdrawable:,.2f} covers the requested "
           f"${request.amount_usd:,.2f}.", CITE_TRAILING_DD,
           {"withdrawable": withdrawable, "current_profit": current_profit})

    # 9. Per-request payout bounds.
    if request.amount_usd < MIN_PAYOUT_USD:
        fail("min_amount", "Minimum Payout Amount",
             f"[PAYOUT_BELOW_MIN] Requested ${request.amount_usd:,.2f} is "
             f"below the ${MIN_PAYOUT_USD:,} per-request minimum (threshold "
             f"VERIFY).", CITE_PAYOUTS, {"amount": request.amount_usd})
    elif request.amount_usd > MAX_PAYOUT_USD:
        fail("max_amount", "Maximum Payout Amount",
             f"[PAYOUT_ABOVE_MAX] Requested ${request.amount_usd:,.2f} "
             f"exceeds the ${MAX_PAYOUT_USD:,} per-request maximum (threshold "
             f"VERIFY).", CITE_PAYOUTS, {"amount": request.amount_usd})
    else:
        ok("max_amount", "Payout Amount Bounds",
           f"${request.amount_usd:,.2f} is within "
           f"[${MIN_PAYOUT_USD:,}, ${MAX_PAYOUT_USD:,}].", CITE_PAYOUTS,
           {"amount": request.amount_usd})

    # 10. Consistency (Growth 35% / Select 40% / Lightning 20-25-30).
    cons = check_consistency(spec, request.daily_profits,
                            account.state.payouts_taken)
    if cons.status == "BREACHED":
        fail("consistency", "Consistency Rule",
             f"[PAYOUT_CONSISTENCY_BREACH] {cons.message} "
             f"(progressive limits apply, e.g. Lightning 20/25/30).",
             CITE_CONSISTENCY, cons.value)
    else:
        ok("consistency", "Consistency Rule",
           f"[PAYOUT_CONSISTENCY_OK] {cons.message}", CITE_CONSISTENCY,
           cons.value)

    # Compose the reason list from failed + warning checks (ordered).
    for c in checks:
        if c.status in ("FAIL", "WARN"):
            reasons.append(f"[{c.rule_id.upper()}] {c.message}")

    eligible = not any(c.status == "FAIL" for c in checks)
    citations = _dedupe([c.citation for c in checks
                         if c.status in ("FAIL", "WARN")])
    if not eligible and not citations:
        # Hard guarantee: no rejection without a citation.
        citations = [CITE_PAYOUTS]
    if eligible:
        # Supporting citations for an approval.
        citations = _dedupe([CITE_PAYOUTS, CITE_CONSISTENCY] + citations)

    return EligibilityResult(
        request_id=request.request_id, account_key=account.account_key,
        eligible=eligible, reasons=reasons, citations=citations,
        checks=checks, requested_amount=request.amount_usd,
        withdrawable=withdrawable, current_profit=current_profit,
        first_payout=account.state.payouts_taken == 0,
        pending_soft_breach=bool(soft), family=spec.family,
        spec_key=spec.key)


# ---------------------------------------------------------------------------
# Rule 2: amount (pure, deterministic)
# ---------------------------------------------------------------------------

def compute_payout_amount(account: PayoutAccount, profit: float,
                          profit_split_pct: float) -> PayoutAmount:
    """
    Trader payout from realized profit under the advertised profit split,
    capped by what is actually withdrawable without breaching the reserve
    floor. Pure arithmetic -- no rounding surprises: cents-level rounding to
    2 decimals is the only transformation.
    """
    gross = float(profit)
    split = float(profit_split_pct)
    trader_share = max(0.0, gross) * (split / 100.0)
    withdrawable = _withdrawable(account.state, account.current_equity)
    payable = round(min(trader_share, withdrawable), 2)
    note = ("payable = min(profit * split%, withdrawable)" if payable ==
            round(trader_share, 2) else
            "capped by withdrawable balance (reserve floor protection)")
    return PayoutAmount(
        gross_profit_usd=round(gross, 2), profit_split_pct=split,
        trader_share_usd=round(trader_share, 2),
        withdrawable_usd=round(withdrawable, 2), payable_usd=payable,
        note=note)


# ---------------------------------------------------------------------------
# Rule 3: decision (pure, deterministic)
# ---------------------------------------------------------------------------

def decide(eligibility: EligibilityResult, amount: PayoutAmount,
           policy: PayoutPolicy) -> Decision:
    """
    Combine eligibility + amount + policy into a final Decision.

    Precedence:
      1. ineligible            -> REJECTED (reasons + citations from checks)
      2. soft breach blocking  -> REJECTED
      3. zero earned share     -> REJECTED
      4. request > earned share-> REJECTED
      5. first payout          -> MANUAL_REVIEW
      6. amount >= threshold   -> MANUAL_REVIEW (fraud risk)
      7. otherwise             -> APPROVED (ready to enqueue payment)
    """
    checks = list(eligibility.checks)

    def rejected(reason: str, citation: str) -> Decision:
        return Decision(
            request_id=eligibility.request_id, decision="REJECTED",
            amount_usd=0.0,
            reasons=_dedupe(list(eligibility.reasons) + [reason]),
            citations=_dedupe(list(eligibility.citations) + [citation]),
            note="rejected: notify trader with reasons and citations",
            eligibility=eligibility, checks=checks)

    if not eligibility.eligible:
        reasons = eligibility.reasons or [
            "[PAYOUT_INELIGIBLE] Account is not eligible for a payout."]
        citations = eligibility.citations or [CITE_PAYOUTS]
        return Decision(
            request_id=eligibility.request_id, decision="REJECTED",
            amount_usd=0.0, reasons=_dedupe(reasons),
            citations=_dedupe(citations),
            note="rejected: notify trader with reasons and citations",
            eligibility=eligibility, checks=checks)

    if policy.soft_breach_blocking and eligibility.pending_soft_breach:
        return rejected(
            "[PAYOUT_SOFT_BREACH_BLOCK] Policy is configured to block on soft "
            "breaches; payout withheld until the session recovers.", CITE_DLL)

    if amount.payable_usd <= 0:
        return rejected(
            "[PAYOUT_ZERO_PAYABLE] Trader share after the profit split is "
            "$0.00; nothing is payable.", CITE_PAYOUTS)

    epsilon = 0.01
    if eligibility.requested_amount > amount.payable_usd + epsilon:
        return rejected(
            f"[PAYOUT_EXCEEDS_EARNED_SHARE] Requested "
            f"${eligibility.requested_amount:,.2f} exceeds the trader's "
            f"earned share ${amount.payable_usd:,.2f} "
            f"(profit ${amount.gross_profit_usd:,.2f} x "
            f"{amount.profit_split_pct:.0f}% capped by withdrawable "
            f"${amount.withdrawable_usd:,.2f}).", CITE_PAYOUTS)

    if eligibility.first_payout and policy.first_payout_manual:
        return Decision(
            request_id=eligibility.request_id, decision="MANUAL_REVIEW",
            amount_usd=eligibility.requested_amount,
            reasons=_dedupe(list(eligibility.reasons) + [
                "[PAYOUT_FIRST_PAYOUT_MANUAL] This is the trader's first "
                "payout; first payouts always require manual review "
                "(policy FIRST_PAYOUT_MANUAL=True)."]),
            citations=_dedupe(list(eligibility.citations) + [CITE_PAYOUTS]),
            note="manual review: first payout", eligibility=eligibility,
            checks=checks)

    if eligibility.requested_amount >= policy.auto_approve_threshold_usd:
        return Decision(
            request_id=eligibility.request_id, decision="MANUAL_REVIEW",
            amount_usd=eligibility.requested_amount,
            reasons=_dedupe(list(eligibility.reasons) + [
                f"[PAYOUT_AMOUNT_MANUAL_REVIEW] Requested "
                f"${eligibility.requested_amount:,.2f} is at/above the "
                f"${policy.auto_approve_threshold_usd:,.0f} auto-approve "
                f"threshold; flagged for fraud review (amount, not "
                f"eligibility, is the concern)."]),
            citations=_dedupe(list(eligibility.citations) + [CITE_PAYOUTS]),
            note="manual review: amount >= auto-approve threshold (fraud risk)",
            eligibility=eligibility, checks=checks)

    return Decision(
        request_id=eligibility.request_id, decision="APPROVED",
        amount_usd=eligibility.requested_amount,
        reasons=_dedupe(list(eligibility.reasons) + [
            f"[PAYOUT_APPROVED] Funded {eligibility.family} "
            f"{eligibility.spec_key} is eligible: "
            f"${eligibility.requested_amount:,.2f} requested, "
            f"${eligibility.withdrawable:,.2f} withdrawable, profit "
            f"${eligibility.current_profit:,.2f}."]),
        citations=_dedupe(list(eligibility.citations) + [CITE_PAYOUTS]),
        note="approved: ready to enqueue payment", eligibility=eligibility,
        checks=checks)


# ---------------------------------------------------------------------------
# Idempotency store (in-memory for the demo; Postgres in production)
# ---------------------------------------------------------------------------
#
# Production replaces these two dicts with a UNIQUE(request_id) column on
# payout_requests plus a payout_decisions row written in the same transaction.
# The semantics are identical: first write wins; a replayed request returns the
# stored decision; a conflicting payload is flagged, never re-decided.

PAYOUT_IDEMPOTENCY: dict[str, dict] = {}


def get_stored_decision(request_id: str) -> Optional[Decision]:
    entry = PAYOUT_IDEMPOTENCY.get(request_id)
    if not entry:
        return None
    return entry["decision"]


def _store_decision(request_id: str, fingerprint: str,
                    decision: Decision) -> Decision:
    PAYOUT_IDEMPOTENCY[request_id] = {
        "fingerprint": fingerprint,
        "decision": decision,
    }
    return decision


def decide_idempotent(request: PayoutRequest, account: PayoutAccount,
                      trades: list[dict], kyc: KYCStatus,
                      policy: PayoutPolicy | None = None) -> Decision:
    """
    Full deterministic pipeline with idempotency protection.

    - request_id never seen  -> evaluate, store, return the decision.
    - request_id seen, same fingerprint -> return the stored decision verbatim.
    - request_id seen, different fingerprint -> REJECTED conflict decision
      (citation chunk_payouts); the original stored decision is untouched.
    """
    policy = policy or PayoutPolicy()
    fingerprint = request_fingerprint(request)
    existing = PAYOUT_IDEMPOTENCY.get(request.request_id)
    if existing is not None:
        if existing["fingerprint"] == fingerprint:
            return existing["decision"]
        conflict = Decision(
            request_id=request.request_id, decision="REJECTED",
            amount_usd=0.0,
            reasons=[
                "[PAYOUT_IDEMPOTENCY_CONFLICT] request_id "
                f"{request.request_id!r} was already used with a different "
                "payload; refusing to re-decide. Original decision is "
                "unchanged.",
            ],
            citations=[CITE_PAYOUTS],
            note="rejected: idempotency conflict", decided_by="engine",
            eligibility=None)
        return conflict

    eligibility = check_eligibility(account, trades, kyc, request)
    profit = eligibility.current_profit
    amount = compute_payout_amount(account, profit, request.profit_split_pct)
    decision = decide(eligibility, amount, policy)
    return _store_decision(request.request_id, fingerprint, decision)


# ---------------------------------------------------------------------------
# LLM explanation (language only; numbers are frozen)
# ---------------------------------------------------------------------------

def _allowed_numbers(decision: Decision) -> set[str]:
    """Normalized numeric tokens the explanation is allowed to reuse."""
    text = " ".join(list(decision.reasons) + list(decision.citations))
    text += f" {decision.amount_usd}"
    out: set[str] = set()
    for raw in _NUMBER_RE.findall(text):
        out.add(raw.replace(",", ""))
    return out


def _numbers_unchanged(explanation: str, decision: Decision) -> bool:
    allowed = _allowed_numbers(decision)
    for raw in _NUMBER_RE.findall(explanation):
        token = raw.replace(",", "")
        if token not in allowed:
            return False
    return True


def _fallback_explanation(decision: Decision) -> str:
    """Deterministic template used when the LLM changes a number/omits citations."""
    label = {"APPROVED": "approved", "REJECTED": "rejected",
             "MANUAL_REVIEW": "flagged for manual review"}[decision.decision]
    body = "\n".join(f"- {r}" for r in decision.reasons)
    src = " ".join(f"[{c}]" for c in decision.citations)
    return (f"Payout request {decision.request_id} was {label}. "
            f"Amount: ${decision.amount_usd:,.2f}. {decision.note}.\n{body}\n"
            f"Sources: {src}")


def explain_payout_decision(decision: Decision, *, client=None,
                            retrieved_ids: list[str] | None = None
                            ) -> PayoutExplanation:
    """
    Explain a deterministic Decision in natural language.

    The LLM receives ONLY the already-computed reasons, citations and amount;
    it may arrange wording but never compute or alter a number. The output is
    run through agent.py's ``verify_citations`` and a numeric-change guard. If
    either fails, a deterministic template is returned instead. This function
    is the ONLY place payout.py may touch an LLM; the decision itself is never
    influenced by it.
    """
    from agent import MockLLMClient, SYSTEM_PROMPT, verify_citations
    from model_router import route

    tier = route("explain", decision.request_id,
                 compliance_critical=bool(decision.reasons))
    if client is None:
        client = MockLLMClient()
    allowed_ids = (list(retrieved_ids) if retrieved_ids is not None
                   else list(decision.citations) or [CITE_PAYOUTS])

    base_citation = decision.citations[0] if decision.citations else CITE_PAYOUTS
    findings = [{"rule": "Payout Decision", "status": decision.decision,
                 "severity": "INFO", "message": r, "citation": base_citation}
                for r in decision.reasons]
    user = (
        f"QUESTION: Explain payout decision {decision.request_id}.\n"
        f"INSTRUCTION: Rephrase the FINDINGS below in plain language. Use "
        f"ONLY the numbers already present. Cite chunks like [chunk_payouts]. "
        f"Do not invent or recompute any amount. Decision: {decision.decision}; "
        f"amount_usd: {decision.amount_usd}.\n\n"
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

    numbers_ok = _numbers_unchanged(raw, decision)
    fallback = False
    if not numbers_ok or not cited:
        raw = _fallback_explanation(decision)
        cited, precision = verify_citations(raw, allowed_ids)
        numbers_ok = _numbers_unchanged(raw, decision)
        fallback = True
        if not cited:
            cited, precision = [], 0.0

    return PayoutExplanation(
        request_id=decision.request_id, explanation=raw, citations=cited,
        citation_precision=round(precision, 3), tier=tier.name,
        model=tier.models[0], numbers_unchanged=numbers_ok,
        deterministic_fallback=fallback)