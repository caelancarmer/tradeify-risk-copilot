"""Unit tests for dashboard.py (Pilar 6 Unified Ops Console).

Tests the context builder for the unified ops console:
  - accounts_at_risk computation with mock ACCOUNTS
  - payout_queue grouping by status
  - recent_decisions limit (<= 10)
  - health checks with mocked infra (postgres, redis, model_router)
  - HTTP endpoints return 200 and expected content
  - Approve/reject UI flow with X-Admin-Key header
  - Rejection without key -> 401

Run: python3 tests/test_dashboard.py
"""

import sys
import os
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, "src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from rule_engine import AccountState

# Import the module to patch its globals
import api
import dashboard

PASS, FAIL = 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")
def setup_mock_accounts():
    """Create mock ACCOUNTS data for testing."""
    # Clear existing accounts
    api.ACCOUNTS.clear()
    
    # Create a test account with known state
    state = AccountState(
        spec_key="growth_funded_50k",
        start_balance=50000.0,
        dd_floor=48000.0,  # 50000 - 2000 trailing drawdown
        dd_locked=False,
        payouts_taken=1,
        session_trading_paused=False
    )
    api.ACCOUNTS["test_account_1"] = state
    
    # Create an at-risk account (buffer < 30% of trailing drawdown)
    state2 = AccountState(
        spec_key="growth_funded_100k", 
        start_balance=100000.0,
        dd_floor=99500.0,  # buffer 500 < 30% of 3500 trailing -> at risk
        dd_locked=False,
        payouts_taken=0,
        session_trading_paused=False
    )
    api.ACCOUNTS["test_account_2"] = state2
    
    # Create a healthy account (buffer > 30% of trailing drawdown)
    state3 = AccountState(
        spec_key="select_flex_50k",
        start_balance=50000.0,
        dd_floor=48000.0,  # 50000 - 2000 trailing drawdown
        dd_locked=False,
        payouts_taken=2,
        session_trading_paused=False
    )
    api.ACCOUNTS["test_account_3"] = state3
def setup_mock_payout_requests():
    """Create mock PAYOUT_REQUESTS and PAYOUT_DECISIONS."""
    # Clear existing data
    api.PAYOUT_REQUESTS.clear()
    api.PAYOUT_DECISIONS.clear()
    
    # Create a few mock payout requests
    request_data = {
        "request_id": "req_001",
        "account_key": "test_account_1",
        "request": {
            "account_key": "test_account_1",
            "amount_usd": 1500.0,
            "trader_id": "trader_a",
            "kyc_verified": True,
            "trading_days": 6,
        }
    }
    api.PAYOUT_REQUESTS["req_001"] = request_data
    
    # Add decision
    decision = {
        "request_id": "req_001",
        "decision": "MANUAL_REVIEW",
        "amount_usd": 1500.0,
        "reasons": ["[PAYOUT_FIRST_PAYOUT_MANUAL] First payout requires manual review"],
        "citations": ["[chunk_payouts]"],
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by": "engine",
        "note": "manual review: first payout",
        "checks": []
    }
    api.PAYOUT_DECISIONS["req_001"] = decision
    
    # Create an approved request
    request_data2 = {
        "request_id": "req_002",
        "account_key": "test_account_2",
        "request": {
            "account_key": "test_account_2",
            "amount_usd": 800.0,
            "trader_id": "trader_b",
            "kyc_verified": True,
            "trading_days": 10,
        }
    }
    api.PAYOUT_REQUESTS["req_002"] = request_data2
    
    decision2 = {
        "request_id": "req_002",
        "decision": "APPROVED",
        "amount_usd": 800.0,
        "reasons": ["[PAYOUT_APPROVED] Account is eligible for payout"],
        "citations": ["[chunk_payouts]"],
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by": "engine",
        "note": "approved: ready to enqueue payment",
        "checks": []
    }
    api.PAYOUT_DECISIONS["req_002"] = decision2
    
    # Create a rejected request
    request_data3 = {
        "request_id": "req_003",
        "account_key": "test_account_3",
        "request": {
            "account_key": "test_account_3",
            "amount_usd": 2000.0,
            "trader_id": "trader_c",
            "kyc_verified": True,
            "trading_days": 8,
        }
    }
    api.PAYOUT_REQUESTS["req_003"] = request_data3
    
    decision3 = {
        "request_id": "req_003",
        "decision": "REJECTED",
        "amount_usd": 0.0,
        "reasons": ["[PAYOUT_CONSISTENCY_BREACH] Consistency rule violated"],
        "citations": ["[chunk_consistency]"],
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "decided_by": "engine",
        "note": "rejected: notify trader with reasons and citations",
        "checks": []
    }
    api.PAYOUT_DECISIONS["req_003"] = decision3
    
    # Create a pending request (no decision yet)
    request_data4 = {
        "request_id": "req_004",
        "account_key": "test_account_1",
        "request": {
            "account_key": "test_account_1",
            "amount_usd": 300.0,
            "trader_id": "trader_d",
            "kyc_verified": True,
            "trading_days": 3,
        }
    }
    api.PAYOUT_REQUESTS["req_004"] = request_data4
# Mock health check functions
def mock_check_postgres():
    return False, "POSTGRES_URL not set"

def mock_check_redis():
    return False, "REDIS_URL not set"

def mock_check_router():
    return False, "not installed"
def setup_mock_health():
    """Mock health check functions."""
    dashboard.DashboardContextBuilder._check_postgres = mock_check_postgres
    dashboard.DashboardContextBuilder._check_redis = mock_check_redis
    dashboard.DashboardContextBuilder._check_model_router = mock_check_router
def create_test_client():
    """Test client against the real api.app (all /ops + payout routes)."""
    return TestClient(api.app)
def run_tests():
    """Run all dashboard tests."""
    print("\n=== Dashboard Context Builder Tests ===")
    
    # Test 1: accounts_at_risk computation
    print("\n1. Testing accounts_at_risk computation...")
    setup_mock_accounts()
    context = dashboard.build_dashboard_context(api.ACCOUNTS, api.PAYOUT_REQUESTS, api.PAYOUT_DECISIONS)
    accounts = context.get("accounts_at_risk", [])
    
    # Check we have 3 accounts
    check("accounts_at_risk returns 3 accounts", len(accounts) == 3, f"got {len(accounts)}")
    
    # Check account details
    test_account = next((a for a in accounts if a["account_key"] == "test_account_1"), None)
    check("test_account_1 exists", test_account is not None, "")
    if test_account:
        check("test_account_1 is not at_risk", not test_account.get("is_at_risk", False), "")
        check("test_account_1 has correct buffer", test_account.get("buffer_dollars", 0) == 2000, "")
    
    test_account2 = next((a for a in accounts if a["account_key"] == "test_account_2"), None)
    check("test_account_2 exists", test_account2 is not None, "")
    if test_account2:
        check("test_account_2 is at_risk", test_account2.get("is_at_risk", False), "")
        check("test_account_2 has correct buffer", test_account2.get("buffer_dollars", 0) == 500, "")
    
    # Test 2: payout_queue grouping
    print("\n2. Testing payout_queue grouping...")
    setup_mock_payout_requests()
    context = dashboard.build_dashboard_context(api.ACCOUNTS, api.PAYOUT_REQUESTS, api.PAYOUT_DECISIONS)
    queue = context.get("payout_queue", {})
    
    check("payout_queue has status keys", "PENDING" in queue and "MANUAL_REVIEW" in queue and "APPROVED" in queue and "REJECTED" in queue, "")
    
    # Check counts
    check("MANUAL_REVIEW count is 1", len(queue.get("MANUAL_REVIEW", [])) == 1, "")
    check("APPROVED count is 1", len(queue.get("APPROVED", [])) == 1, "")
    check("REJECTED count is 1", len(queue.get("REJECTED", [])) == 1, "")
    check("PENDING count is 1", len(queue.get("PENDING", [])) == 1, "")
    
    # Check content
    manual_review = queue.get("MANUAL_REVIEW", [])[0]
    check("MANUAL_REVIEW request has correct ID", manual_review.get("request_id") == "req_001", "")
    check("MANUAL_REVIEW has correct status", manual_review.get("status") == "MANUAL_REVIEW", "")
    
    # Test 3: recent_decisions limit
    print("\n3. Testing recent_decisions limit...")
    decisions = context.get("recent_decisions", [])
    check("recent_decisions <= 10 items", len(decisions) <= 10, f"got {len(decisions)}")
    
    # Check that we have 3 decisions (the ones we created)
    check("recent_decisions has 3 items", len(decisions) == 3, f"got {len(decisions)}")
    
    # Check decision structure
    if decisions:
        decision = decisions[0]
        check("decision has request_id", "request_id" in decision, "")
        check("decision has decided_at", "decided_at" in decision, "")
        check("decision has decision field", "decision" in decision, "")
        check("decision has amount_usd", "amount_usd" in decision, "")
    
    # Test 4: health checks
    print("\n4. Testing health checks...")
    setup_mock_health()
    health = context.get("health", {})
    
    check("health has postgres section", "postgres" in health, "")
    check("health has redis section", "redis" in health, "")
    check("health has model_router section", "model_router" in health, "")
    
    check("postgres status is not ok", health.get("postgres", {}).get("status") != "ok", "")
    check("redis status is not ok", health.get("redis", {}).get("status") != "ok", "")
    check("router status is mock", health.get("model_router", {}).get("status") == "mock", "")
    
    check("overall status is degraded", health.get("overall") == "degraded", "")
    
    # Test 5: HTTP endpoints
    print("\n5. Testing HTTP endpoints...")
    client = create_test_client()
    
    # Test main ops page
    response = client.get("/ops")
    check("GET /ops returns 200", response.status_code == 200, f"got {response.status_code}")
    check("GET /ops contains 'Accounts at Risk'", "Accounts at Risk" in response.text, "")
    
    # Test partials
    response = client.get("/ops/partials/accounts")
    check("GET /ops/partials/accounts returns 200", response.status_code == 200, f"got {response.status_code}")
    
    response = client.get("/ops/partials/queue")
    check("GET /ops/partials/queue returns 200", response.status_code == 200, f"got {response.status_code}")
    
    response = client.get("/ops/partials/decisions")
    check("GET /ops/partials/decisions returns 200", response.status_code == 200, f"got {response.status_code}")
    
    # Test 6: Approve/reject UI flow
    print("\n6. Testing approve/reject UI flow...")
    # First ensure we have a request to approve
    setup_mock_payout_requests()
    
    # Test approve with correct key
    response = client.post("/payout/req_001/approve", headers={"X-Admin-Key": api.ADMIN_API_KEY})
    check("POST /payout/{id}/approve with key returns 200", response.status_code == 200, f"got {response.status_code}")
    
    # Check that decision was updated
    updated_decision = api.PAYOUT_DECISIONS.get("req_001")
    check("decision updated to APPROVED", updated_decision and updated_decision.get("decision") == "APPROVED", "")
    
    # Test reject with correct key
    response = client.post("/payout/req_002/reject", headers={"X-Admin-Key": api.ADMIN_API_KEY})
    check("POST /payout/{id}/reject with key returns 200", response.status_code == 200, f"got {response.status_code}")
    
    # Check that decision was updated
    updated_decision = api.PAYOUT_DECISIONS.get("req_002")
    check("decision updated to REJECTED", updated_decision and updated_decision.get("decision") == "REJECTED", "")
    
    # Test 7: Rejection without key -> 401
    print("\n7. Testing authentication...")
    response = client.post("/payout/req_003/approve", headers={})
    check("POST without X-Admin-Key returns 401", response.status_code == 401, f"got {response.status_code}")
    
    response = client.post("/payout/req_003/reject", headers={})
    check("POST without X-Admin-Key returns 401", response.status_code == 401, f"got {response.status_code}")
    
    # Test 8: Error handling
    print("\n8. Testing error handling...")
    # Create a scenario that triggers error in context builder
    original_accounts = api.ACCOUNTS.copy()
    api.ACCOUNTS.clear()
    
    try:
        error_context = dashboard.build_dashboard_context(api.ACCOUNTS, api.PAYOUT_REQUESTS, api.PAYOUT_DECISIONS)
        error_accounts = error_context.get("accounts_at_risk", [])
        
        # Should handle the error gracefully
        check("error panel returned for empty ACCOUNTS", isinstance(error_accounts, list), "")
    finally:
        api.ACCOUNTS.clear()
        api.ACCOUNTS.update(original_accounts)
    
    print(f"\n=== Test Summary ===")
    print(f"PASS: {PASS}")
    print(f"FAIL: {FAIL}")
    
    # Restore original accounts
    api.ACCOUNTS.clear()
    
    if FAIL == 0:
        print("All tests passed!")
        return True
    else:
        print(f"{FAIL} test(s) failed")
        return False
if __name__ == "__main__":
    # Import and patch the api module
    import importlib
    importlib.reload(api)
    importlib.reload(dashboard)
    
    # Set the admin key for tests
    api.ADMIN_API_KEY = "test-admin-key"
    
    success = run_tests()
    sys.exit(0 if success else 1)