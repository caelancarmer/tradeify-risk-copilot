"""Unit tests for payout.py. Run: python3 tests/test_payout.py

Style mirrors tests/test_rule_engine.py: check()/PASS/FAIL counters and a
non-zero exit on failure.
"""

import sys
from datetime import datetime, timezone

sys.path.insert(0, "src")
from rule_engine import AccountState
from payout import (
    PAYOUT_IDEMPOTENCY,
    KYCStatus,
    PayoutAccount,
    PayoutPolicy,
    PayoutRequest,
    check_eligibility,
    compute_payout_amount,
    decide,
    decide_idempotent,
    explain_payout_decision,
    request_fingerprint,
)

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


def make_account(spec_key="growth_funded_50k", payouts_taken=1,
                 equity_delta=600.0, balance_delta=None, day_pnl=0.0):
    state = AccountState.new(spec_key)
    state.payouts_taken = payouts_taken
    bd = equity_delta if balance_delta is None else balance_delta
    return PayoutAccount.from_state(
        spec_key, state,
        current_equity=state.start_balance + equity_delta,
        day_pnl=day_pnl,
        current_balance=state.start_balance + bd)


def make_kyc(status="verified"):
    return KYCStatus(trader_id="t1", status=status,
                     method="stub" if status == "verified" else None,
                     verified_at=(datetime.now(timezone.utc).isoformat()
                                   if status == "verified" else None))


def make_request(request_id="r1", amount=200.0, trading_days=10,
                 open_positions=0, daily_profits=None, payout_method="ach",
                 spec_key="growth_funded_50k"):
    return PayoutRequest(
        request_id=request_id, account_key=spec_key, trader_id="t1",
        amount_usd=amount, kyc_verified=True, trading_days=trading_days,
        open_positions=open_positions,
        daily_profits=[300, 300, 300] if daily_profits is None else daily_profits,
        payout_method=payout_method)


def run(request, account, kyc, policy=None):
    PAYOUT_IDEMPOTENCY.clear()
    return decide_idempotent(request, account, [], kyc,
                             policy or PayoutPolicy())


def codes(decision):
    """Extract [PAYOUT_*] codes from a decision's reasons."""
    import re
    out = []
    for r in decision.reasons:
        out.extend(re.findall(r"\[(PAYOUT_[A-Z_]+)\]", r))
    return out


# ---------------------------------------------------------------------------
print("== approve happy path ==")
d = run(make_request(), make_account(), make_kyc())
check("clean Growth funded -> APPROVED", d.decision == "APPROVED", d.reasons)
check("approved amount preserved", d.amount_usd == 200.0, d.amount_usd)
check("approved carries citations",
      set(d.citations) >= {"chunk_payouts", "chunk_consistency"}, d.citations)
check("no rejection without citation holds trivially for approvals",
      d.decision != "APPROVED" or bool(d.citations), d.citations)

d = run(make_request(request_id="r2", amount=150.0, daily_profits=[1000, 50]),
        make_account("select_flex_100k"), make_kyc())
check("Select Flex (no consistency) -> APPROVED", d.decision == "APPROVED",
      d.reasons)

# ---------------------------------------------------------------------------
print("== rejection: funded-only rule ==")
d = run(make_request(spec_key="growth_eval_50k"),
        make_account("growth_eval_50k", payouts_taken=0), make_kyc())
check("evaluation account -> REJECTED", d.decision == "REJECTED", d.reasons)
check("stage rejection cites chunk_account_families",
      "chunk_account_families" in d.citations, d.citations)

print("== rejection: minimum trading days ==")
d = run(make_request(request_id="days", trading_days=3), make_account(),
        make_kyc())
check("3 trading days -> REJECTED", d.decision == "REJECTED", d.reasons)
check("min-days rejection cites chunk_payouts",
      "chunk_payouts" in d.citations, d.citations)

print("== rejection: consistency ==")
d = run(make_request(request_id="cons", daily_profits=[5000, 100]),
        make_account(), make_kyc())
check("best day 98% > 35% cap -> REJECTED", d.decision == "REJECTED",
      d.reasons)
check("consistency rejection cites chunk_consistency",
      "chunk_consistency" in d.citations, d.citations)

print("== rejection: open positions ==")
d = run(make_request(request_id="open", open_positions=2), make_account(),
        make_kyc())
check("2 open positions -> REJECTED", d.decision == "REJECTED", d.reasons)
check("open-position rejection cites chunk_payouts",
      "chunk_payouts" in d.citations, d.citations)

print("== rejection: KYC not verified ==")
d = run(make_request(request_id="kyc"), make_account(), make_kyc("pending"))
check("KYC pending -> REJECTED", d.decision == "REJECTED", d.reasons)
check("KYC rejection cites chunk_payouts",
      "chunk_payouts" in d.citations, d.citations)

print("== rejection: hard breach ==")
d = run(make_request(request_id="hard"),
        make_account(equity_delta=-2000.0, balance_delta=600.0), make_kyc())
check("equity at trailing floor -> REJECTED", d.decision == "REJECTED",
      d.reasons)
check("hard breach cites chunk_trailing_dd",
      "chunk_trailing_dd" in d.citations, d.citations)

print("== rejection: payout method invalid ==")
d = run(make_request(request_id="method", payout_method="paypal"),
        make_account(), make_kyc())
check("unregistered method -> REJECTED", d.decision == "REJECTED", d.reasons)
check("method rejection cites chunk_payouts",
      "chunk_payouts" in d.citations, d.citations)

print("== rejection: exceeds withdrawable ==")
d = run(make_request(request_id="wd", amount=5000.0), make_account(),
        make_kyc())
check("amount > withdrawable -> REJECTED", d.decision == "REJECTED",
      d.reasons)
check("withdrawable rejection cites chunk_trailing_dd",
      "chunk_trailing_dd" in d.citations, d.citations)

print("== rejection: below minimum profit ==")
d = run(make_request(request_id="minprofit", amount=100.0),
        make_account(equity_delta=50.0, balance_delta=50.0), make_kyc())
check("profit $50 < $100 floor -> REJECTED", d.decision == "REJECTED",
      d.reasons)
check("min-profit rejection cites chunk_payouts",
      "chunk_payouts" in d.citations, d.citations)

# ---------------------------------------------------------------------------
print("== policy: first payout manual ==")
d = run(make_request(request_id="first"), make_account(payouts_taken=0),
        make_kyc())
check("first payout -> MANUAL_REVIEW", d.decision == "MANUAL_REVIEW",
      d.reasons)
check("first-payout reason code present",
      "PAYOUT_FIRST_PAYOUT_MANUAL" in codes(d), codes(d))

print("== policy: >= $500 auto-approve threshold ==")
d = run(make_request(request_id="big", amount=500.0),
        make_account(equity_delta=1000.0, balance_delta=1000.0), make_kyc())
check("$500 clean -> MANUAL_REVIEW (fraud risk)",
      d.decision == "MANUAL_REVIEW", d.reasons)
check("threshold reason code present",
      "PAYOUT_AMOUNT_MANUAL_REVIEW" in codes(d), codes(d))

# ---------------------------------------------------------------------------
print("== pure components: check_eligibility + decide ==")
acct = make_account()
req = make_request(request_id="pure")
elig = check_eligibility(acct, [], make_kyc(), req)
check("check_eligibility returns Pydantic EligibilityResult",
      hasattr(elig, "model_dump") and elig.eligible is True, type(elig))
amt = compute_payout_amount(acct, elig.current_profit, 90.0)
dec = decide(elig, amt, PayoutPolicy())
check("decide() accepts EligibilityResult + PayoutAmount + PayoutPolicy",
      dec.decision == "APPROVED", dec.reasons)
elig_bad = check_eligibility(
    acct, [], make_kyc(), make_request(request_id="pure2", open_positions=1))
dec_bad = decide(elig_bad, amt, PayoutPolicy())
check("ineligible EligibilityResult -> REJECTED with citation",
      dec_bad.decision == "REJECTED" and bool(dec_bad.citations), dec_bad.reasons)

print("== amount computation ==")
acct = make_account(equity_delta=1000.0, balance_delta=1000.0)
amt = compute_payout_amount(acct, profit=1000.0, profit_split_pct=90.0)
check("90% split of $1000 = $900", amt.trader_share_usd == 900.0,
      amt.trader_share_usd)
check("payable capped by withdrawable $1000", amt.payable_usd == 900.0,
      amt.payable_usd)
amt = compute_payout_amount(acct, profit=100.0, profit_split_pct=90.0)
check("90% of $100 = $90, withdrawable allows it", amt.payable_usd == 90.0,
      amt.payable_usd)

# ---------------------------------------------------------------------------
print("== idempotency: same request twice ==")
PAYOUT_IDEMPOTENCY.clear()
req = make_request(request_id="idem")
acct = make_account()
kyc = make_kyc()
d1 = decide_idempotent(req, acct, [], kyc, PayoutPolicy())
d2 = decide_idempotent(req, acct, [], kyc, PayoutPolicy())
check("same request_id -> identical decision",
      d1.model_dump() == d2.model_dump(),
      (d1.decision, d2.decision))
check("same request_id -> identical reasons",
      d1.reasons == d2.reasons, (d1.reasons, d2.reasons))
check("fingerprint stable across calls",
      request_fingerprint(req) == request_fingerprint(req))

# ---------------------------------------------------------------------------
print("== adversarial: duplicate request_id, different amount ==")
PAYOUT_IDEMPOTENCY.clear()
acct = make_account(equity_delta=2000.0, balance_delta=2000.0)
req_a = make_request(request_id="dup", amount=200.0)
d1 = decide_idempotent(req_a, acct, [], make_kyc(), PayoutPolicy())
check("first submit -> APPROVED", d1.decision == "APPROVED", d1.reasons)
req_b = make_request(request_id="dup", amount=999.0)
d2 = decide_idempotent(req_b, acct, [], make_kyc(), PayoutPolicy())
check("replay with different amount -> REJECTED", d2.decision == "REJECTED",
      d2.reasons)
check("idempotency-conflict code present",
      "PAYOUT_IDEMPOTENCY_CONFLICT" in codes(d2), codes(d2))
check("conflict decision carries a citation", bool(d2.citations), d2.citations)
check("original decision unchanged after conflict",
      PAYOUT_IDEMPOTENCY["dup"]["decision"].decision == "APPROVED",
      PAYOUT_IDEMPOTENCY["dup"]["decision"].decision)

# ---------------------------------------------------------------------------
print("== rejection invariant: every rejection has a citation ==")
scenarios = [
    (make_request(request_id="s1", spec_key="growth_eval_50k"),
     make_account("growth_eval_50k", payouts_taken=0), make_kyc("verified")),
    (make_request(request_id="s2", trading_days=1), make_account(),
     make_kyc("verified")),
    (make_request(request_id="s3", daily_profits=[9000, 1]),
     make_account(), make_kyc("verified")),
    (make_request(request_id="s4", open_positions=5), make_account(),
     make_kyc("verified")),
    (make_request(request_id="s5"), make_account(), make_kyc("unverified")),
    (make_request(request_id="s6", amount=8000.0), make_account(),
     make_kyc("verified")),
]
all_cited = True
for req, acct, kyc in scenarios:
    d = run(req, acct, kyc)
    if d.decision == "REJECTED" and not d.citations:
        all_cited = False
check("all rejection scenarios carry >= 1 citation", all_cited)

# ---------------------------------------------------------------------------
print("== deterministic decision object is Pydantic, not dict ==")
d = run(make_request(request_id="pyd"), make_account(), make_kyc())
check("decision exposes model_dump (Pydantic)", hasattr(d, "model_dump"),
      type(d))
check("decision object has typed fields",
      isinstance(d.reasons, list) and isinstance(d.citations, list), type(d))

# ---------------------------------------------------------------------------
print("== LLM explanation: numbers frozen and citations verified ==")
ex = explain_payout_decision(d)
check("explanation returns citations with precision 1.0",
      ex.citation_precision == 1.0 and bool(ex.citations),
      (ex.citation_precision, ex.citations))
check("explanation does not change numbers", ex.numbers_unchanged, ex)


# ---------------------------------------------------------------------------
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)