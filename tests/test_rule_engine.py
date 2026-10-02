"""Unit tests for rule_engine.py. Run: python tests/test_rule_engine.py"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")
from rule_engine import (
    ACCOUNT_SPECS, AccountState, check_daily_loss_limit, update_dd_floor,
    check_trailing_drawdown, check_consistency, check_profit_target,
    check_microscalp, check_flat_time, evaluate_all, summarize,
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

print("== daily loss limit ==")
s = ACCOUNT_SPECS["growth_eval_50k"]
f = check_daily_loss_limit(s, -1250)
check("growth DLL hit at exactly -1250 -> BREACHED/SOFT", f.status == "BREACHED" and f.severity == "SOFT_BREACH", f.to_dict())
f = check_daily_loss_limit(s, -500)
check("growth DLL room -500 -> OK, remaining 750", f.status == "OK" and f.value["remaining"] == 750, f.to_dict())
s = ACCOUNT_SPECS["select_eval_50k"]
f = check_daily_loss_limit(s, -5000)
check("select eval has NO DLL -> OK even at -5000", f.status == "OK" and f.value["limit"] is None, f.to_dict())
s = ACCOUNT_SPECS["select_daily_50k"]
f = check_daily_loss_limit(s, -1000)
check("select daily 50k DLL=1000 hit -> BREACHED", f.status == "BREACHED", f.to_dict())

print("== trailing drawdown: trail-up, lock, hard breach ==")
st = AccountState.new("growth_funded_50k")
check("initial floor = 50000-2000 = 48000", st.dd_floor == 48000, st.dd_floor)
update_dd_floor(st, 51000)   # EOD 51k -> floor 49000
check("floor trails up to 49000 after EOD 51000", st.dd_floor == 49000, st.dd_floor)
update_dd_floor(st, 50500)   # lower EOD -> floor must NOT go down
check("floor never trails down", st.dd_floor == 49000, st.dd_floor)
f = check_trailing_drawdown(st, 49500)
check("equity 49500 above floor -> OK, distance 500", f.status == "OK" and f.value["distance"] == 500, f.to_dict())
f = check_trailing_drawdown(st, 49000)
check("equity == floor -> HARD breach", f.status == "BREACHED" and f.severity == "HARD_BREACH", f.to_dict())
st2 = AccountState.new("growth_funded_50k")
floor, locked = update_dd_floor(st2, 52100)  # 52100 >= 50000+2000+100 -> lock
check("lock triggers at start+DD+100", locked and floor == 50100 and st2.dd_locked, (floor, locked))
floor2, _ = update_dd_floor(st2, 60000)
check("locked floor never moves again", floor2 == 50100, floor2)

print("== consistency ==")
s = ACCOUNT_SPECS["growth_funded_50k"]
f = check_consistency(s, [2000, 500, 300])
check("71.4% best-day > 35% cap -> BREACHED but INFO (no fail)", f.status == "BREACHED" and f.severity == "INFO", f.to_dict())
f = check_consistency(s, [600, 600, 600])
check("33.3% within 35% cap -> OK", f.status == "OK", f.to_dict())
s = ACCOUNT_SPECS["select_eval_50k"]
f = check_consistency(s, [2000, 500, 300])
check("select eval 40% cap: 71.4% -> BREACHED", f.status == "BREACHED" and f.value["limit_pct"] == 40.0, f.to_dict())
s = ACCOUNT_SPECS["lightning_50k"]
for payouts, exp in [(0, 20.0), (1, 25.0), (2, 30.0), (9, 30.0)]:
    f = check_consistency(s, [100, 100, 100], payouts_taken=payouts)
    check(f"lightning progressive payouts={payouts} -> {exp}%", f.value["limit_pct"] == exp, f.value)
s = ACCOUNT_SPECS["select_flex_50k"]
f = check_consistency(s, [5000])
check("select flex has NO consistency -> OK", f.status == "OK" and f.value["limit_pct"] is None, f.to_dict())

print("== profit target ==")
s = ACCOUNT_SPECS["growth_eval_50k"]
f = check_profit_target(s, 53000)
check("growth eval 50k at 53000 -> target HIT", "HIT" in f.message, f.message)
f = check_profit_target(s, 51500)
check("at 51500 -> 1500 remaining", f.value["remaining"] == 1500, f.value)
s = ACCOUNT_SPECS["growth_funded_50k"]
f = check_profit_target(s, 60000)
check("funded has no target -> OK", f.status == "OK", f.message)

print("== microscalp ==")
good = [{"hold_seconds": 30, "pnl": 100}, {"hold_seconds": 45, "pnl": 120},
        {"hold_seconds": 5, "pnl": 20}]
f = check_microscalp(good)
check("2/3 long trades, 91.7% long profit -> OK", f.status == "OK", f.to_dict())
bad = [{"hold_seconds": 5, "pnl": 100}, {"hold_seconds": 6, "pnl": 120},
       {"hold_seconds": 30, "pnl": 20}]
f = check_microscalp(bad)
check("scalper profile -> WARNING", f.status == "WARNING", f.to_dict())

print("== flat-by rule ==")
ET = ZoneInfo("America/New_York")
f = check_flat_time(datetime(2026, 10, 5, 17, 30, tzinfo=ET))  # Monday
check("Mon 17:30 ET -> BREACHED", f.status == "BREACHED", f.message)
f = check_flat_time(datetime(2026, 10, 5, 10, 0, tzinfo=ET))
check("Mon 10:00 ET -> OK", f.status == "OK", f.message)
f = check_flat_time(datetime(2026, 10, 3, 18, 0, tzinfo=ET))  # Saturday
check("Sat 18:00 ET -> OK (weekend, no session)", f.status == "OK", f.message)

print("== evaluate_all + summarize ==")
st = AccountState.new("growth_funded_100k")
update_dd_floor(st, 102000)  # floor 98500
findings = evaluate_all(st, day_pnl=-3000, current_equity=99000,
                        daily_profits=[800, 700, 600], trades=good,
                        current_balance=102000)
rep = summarize(findings)
check("DLL -3000 vs 2500 -> session paused, not failed",
      rep["session_paused"] and not rep["account_failed"], rep)
st = AccountState.new("growth_funded_100k")
findings = evaluate_all(st, day_pnl=-100, current_equity=96500,  # floor 96500
                        daily_profits=[800, 700, 600], trades=good,
                        current_balance=99000)
rep = summarize(findings)
check("equity at floor 96500 -> account_failed", rep["account_failed"], rep)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
