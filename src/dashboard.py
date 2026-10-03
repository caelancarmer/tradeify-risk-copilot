"""
dashboard.py -- Unified Ops Console context builder.

Implements build_dashboard_context() that compiles the operational view for
templates/ops.html:

1. accounts_at_risk: for each account in ACCOUNTS (api module), compute buffer
   drawdown = current_equity - dd_floor (proxy start_balance if no live equity).
   Flag risk if buffer < 30% of trailing_drawdown (account spec). Include a
   concise consistency status.

2. payout_queue: group PAYOUT_REQUESTS (api) by status (PENDING/MANUAL_REVIEW/
   APPROVED/REJECTED) for the admin console.

3. recent_decisions: top 10 PAYOUT_DECISIONS (api) by timestamp, with trimmed
   data (time, account, decision, first reason, first citation).

4. health: three sub-checks with 2s timeouts and graceful degradation:
   - postgres: connect via POSTGRES_URL env (try to query pg_catalog)
   - redis: connect via REDIS_URL env (ping)
   - model_router: call model_router.status() if present, else mark 'mock'

All I/O is wrapped in try/except; the dashboard never crashes because infra
failed.

Usage: called from api.py GET /ops endpoint.
"""

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(__file__))

from rule_engine import ACCOUNT_SPECS, AccountState

try:  # shared alert store written by worker.monitor_buffers
    import alert_store
    _DEFAULT_BUFFER_ALERTS = alert_store.BUFFER_ALERTS
except Exception:  # pragma: no cover - dashboard must render without the worker
    _DEFAULT_BUFFER_ALERTS = []
try:
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

class DashboardContextBuilder:
    """Builds the Ops Console context from in-memory stores and health checks.

    State is injected (not imported from api) to avoid a circular import:
    api.py imports this module for the /ops routes.
    """

    def __init__(self, accounts=None, payout_requests=None,
                 payout_decisions=None, precheck_log=None,
                 buffer_alerts=None):
        self.accounts = accounts if accounts is not None else {}
        self.payout_requests = payout_requests if payout_requests is not None else {}
        self.payout_decisions = (payout_decisions
                                 if payout_decisions is not None else {})
        self.precheck_log = precheck_log if precheck_log is not None else []
        self.buffer_alerts = buffer_alerts

    @staticmethod
    def _as_dict(obj):
        """Accept Pydantic models or plain dicts from the api stores."""
        if obj is None:
            return {}
        if isinstance(obj, dict):
            return obj
        dump = getattr(obj, "model_dump", None)
        if callable(dump):
            return dump()
        return {}

    def build_dashboard_context(self) -> Dict[str, Any]:
        """Return the full dashboard context dict for rendering."""
        try:
            accounts_at_risk = self._build_accounts_at_risk()
        except Exception as e:
            accounts_at_risk = self._error_panel(f"accounts_at_risk: {e}")

        try:
            payout_queue = self._build_payout_queue()
        except Exception as e:
            payout_queue = self._error_panel(f"payout_queue: {e}")

        try:
            recent_decisions = self._build_recent_decisions()
        except Exception as e:
            recent_decisions = self._error_panel(f"recent_decisions: {e}")

        try:
            health = self._build_health()
        except Exception as e:
            health = self._error_panel(f"health: {e}")

        try:
            prechecks_today = self._build_prechecks_today()
        except Exception as e:
            prechecks_today = self._error_panel(f"prechecks_today: {e}")

        try:
            buffer_alerts = self._build_buffer_alerts()
        except Exception as e:
            buffer_alerts = self._error_panel(f"buffer_alerts: {e}")

        return {
            "accounts_at_risk": accounts_at_risk,
            "payout_queue": payout_queue,
            "recent_decisions": recent_decisions,
            "health": health,
            "prechecks_today": prechecks_today,
            "buffer_alerts": buffer_alerts,
            "refresh_timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # -- New panels (deliverable E); the panels above are unchanged. --------

    def _build_prechecks_today(self) -> Dict[str, Any]:
        """Count today's pre-checks by verdict (from the read-only log)."""
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        rows = [r for r in self.precheck_log
                if str(r.get("timestamp", "")).startswith(today)]
        counts = {"ELIGIBLE": 0, "NOT_ELIGIBLE": 0, "MANUAL_REVIEW": 0}
        for r in rows:
            status = r.get("status")
            if status in counts:
                counts[status] += 1
        return {
            "date": today,
            "total": len(rows),
            "counts": counts,
            "recent": list(reversed(rows[-20:])),
        }

    def _build_buffer_alerts(self) -> Dict[str, Any]:
        """Count buffer alerts sent today, by account and severity."""
        alerts = (self.buffer_alerts if self.buffer_alerts is not None
                  else _DEFAULT_BUFFER_ALERTS)
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        rows = [a for a in alerts
                if str(a.get("detected_at", "")).startswith(today)]
        sent = [a for a in rows if a.get("sent")]
        by_severity: Dict[str, int] = {}
        for a in sent:
            sev = a.get("severity", "UNKNOWN")
            by_severity[sev] = by_severity.get(sev, 0) + 1
        return {
            "date": today,
            "sent_count": len(sent),
            "suppressed_count": sum(1 for a in rows if a.get("suppressed")),
            "by_severity": by_severity,
            "alerts": list(reversed(sent[-20:])),
        }

    def _build_accounts_at_risk(self) -> List[Dict[str, Any]]:
        """Compute risk status for each account."""
        result = []
        for account_key, state in self.accounts.items():
            try:
                spec = ACCOUNT_SPECS[state.spec_key]
                # Use start_balance as proxy for current_equity if not present
                current_equity = getattr(state, 'current_equity', state.start_balance)
                dd_floor = state.dd_floor
                buffer = current_equity - dd_floor
                buffer_pct = (buffer / spec.trailing_drawdown * 100.0) if spec.trailing_drawdown > 0 else 0.0

                # Risk if buffer < 30% of trailing_drawdown
                is_at_risk = buffer_pct < 30.0

                # Get consistency status (simplified)
                consistency_status = self._get_consistency_status(state, spec)

                result.append({
                    "account_key": account_key,
                    "spec_key": state.spec_key,
                    "family": spec.family,
                    "stage": spec.stage,
                    "start_balance": state.start_balance,
                    "dd_floor": dd_floor,
                    "current_equity": current_equity,
                    "buffer_dollars": round(buffer, 2),
                    "buffer_percent": round(buffer_pct, 2),
                    "is_at_risk": is_at_risk,
                    "consistency_status": consistency_status,
                    "dd_locked": state.dd_locked,
                    "payouts_taken": state.payouts_taken,
                })
            except Exception as e:
                result.append({
                    "account_key": account_key,
                    "error": f"failed to compute risk: {e}",
                    "is_error": True,
                })
        # Sort by risk (at-risk first) then account key
        result.sort(key=lambda x: (not x.get("is_at_risk", False), x.get("account_key", "")))
        return result

    def _get_consistency_status(self, state: AccountState, spec) -> str:
        """Generate a concise consistency status string."""
        if spec.consistency is None and spec.consistency_progressive is None:
            return "none"
        limit = spec.consistency
        if spec.consistency_progressive is not None:
            idx = min(state.payouts_taken, len(spec.consistency_progressive) - 1)
            limit = spec.consistency_progressive[idx]
        return f"{limit:.0f}%" if limit is not None else "none"

    def _build_payout_queue(self) -> Dict[str, List[Dict[str, Any]]]:
        """Group payout requests by their decision status."""
        grouped = {
            "PENDING": [],
            "MANUAL_REVIEW": [],
            "APPROVED": [],
            "REJECTED": [],
        }
        for request_id, data in self.payout_requests.items():
            decision = self.payout_decisions.get(request_id, {})
            status = decision.get("decision", "PENDING")
            if status not in grouped:
                status = "PENDING"

            req = self._as_dict(data.get("request"))
            account = self._as_dict(data.get("account"))
            entry = {
                "request_id": request_id,
                "account_key": req.get("account_key", ""),
                "amount_usd": req.get("amount_usd", 0.0),
                "trader_id": req.get("trader_id", ""),
                "status": status,
                "kyc_verified": req.get("kyc_verified", False),
                "trading_days": req.get("trading_days", 0),
                "decision_time": decision.get("decided_at", ""),
                "decided_by": decision.get("decided_by", "engine"),
            }
            grouped[status].append(entry)
        return grouped

    def _build_recent_decisions(self) -> List[Dict[str, Any]]:
        """Extract the last 10 decisions with trimmed data."""
        decisions = []
        # Sort by timestamp descending
        sorted_items = sorted(
            self.payout_decisions.items(),
            key=lambda x: x[1].get("decided_at", ""),
            reverse=True
        )
        for request_id, decision in sorted_items[:10]:
            req_data = self.payout_requests.get(request_id, {}) or {}
            account = self._as_dict(req_data.get("account"))
            req = self._as_dict(req_data.get("request"))
            decisions.append({
                "request_id": request_id,
                "decided_at": decision.get("decided_at", ""),
                "account_key": req.get("account_key", ""),
                "decision": decision.get("decision", ""),
                "amount_usd": decision.get("amount_usd", 0.0),
                "first_reason": (decision.get("reasons", [""])[0] if decision.get("reasons") else ""),
                "first_citation": (decision.get("citations", [""])[0] if decision.get("citations") else ""),
                "decided_by": decision.get("decided_by", "engine"),
                "note": decision.get("note", ""),
            })
        return decisions

    def _build_health(self) -> Dict[str, Any]:
        """Check external services health with 2s timeouts."""
        postgres_ok, postgres_msg = self._check_postgres()
        redis_ok, redis_msg = self._check_redis()
        router_ok, router_msg = self._check_model_router()

        return {
            "postgres": {
                "status": "ok" if postgres_ok else "unreachable",
                "message": postgres_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "redis": {
                "status": "ok" if redis_ok else "unreachable",
                "message": redis_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "model_router": {
                "status": "ok" if router_ok else "mock",
                "message": router_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            "overall": "ok" if all([postgres_ok, redis_ok, router_ok]) else "degraded",
        }

    def _check_postgres(self) -> tuple[bool, str]:
        """Attempt a lightweight PostgreSQL connection via POSTGRES_URL."""
        if not POSTGRES_AVAILABLE:
            return False, "psycopg2 not installed"
        url = os.environ.get("POSTGRES_URL")
        if not url:
            return False, "POSTGRES_URL not set"
        try:
            import urllib.parse
            from psycopg2 import connect
            # Parse connection string (simplified)
            if url.startswith("postgresql://"):
                # Simple timeout implementation
                import socket
                import urllib.parse
                parsed = urllib.parse.urlparse(url)
                # Try to create connection with timeout
                conn = connect(
                    host=parsed.hostname,
                    port=parsed.port or 5432,
                    user=parsed.username,
                    password=parsed.password,
                    database=parsed.path.lstrip("/"),
                    connect_timeout=2,
                    application_name="dashboard_health_check"
                )
                conn.close()
                return True, "connected"
            else:
                return False, "unsupported URL format"
        except Exception as e:
            return False, f"connection failed: {str(e)[:100]}"

    def _check_redis(self) -> tuple[bool, str]:
        """Attempt a lightweight Redis PING via REDIS_URL."""
        if not REDIS_AVAILABLE:
            return False, "redis not installed"
        url = os.environ.get("REDIS_URL")
        if not url:
            return False, "REDIS_URL not set"
        try:
            # Simple Redis URL parsing
            import urllib.parse
            parsed = urllib.parse.urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 6379
            password = parsed.password

            # Create connection with timeout
            r = redis.Redis(
                host=host,
                port=port,
                password=password,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=False,
            )
            r.ping()
            return True, "ping ok"
        except Exception as e:
            return False, f"ping failed: {str(e)[:100]}"

    def _check_model_router(self) -> tuple[bool, str]:
        """Call model_router.status() if present, else mark 'mock'."""
        try:
            from model_router import status as router_status
            result = router_status()
            return True, "ok"
        except ImportError:
            return False, "not installed"
        except Exception as e:
            return False, f"error: {str(e)[:100]}"

    def _error_panel(self, message: str) -> Dict[str, Any]:
        """Return an error panel that can be displayed in the UI."""
        return {
            "error": True,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# Global instance
builder = DashboardContextBuilder()
build_dashboard_context = builder.build_dashboard_context

# Convenience export
__all__ = ["build_dashboard_context"]

def build_dashboard_context(accounts=None, payout_requests=None,
                            payout_decisions=None, precheck_log=None,
                            buffer_alerts=None):
    """Module-level convenience: build context from injected stores."""
    return DashboardContextBuilder(
        accounts=accounts,
        payout_requests=payout_requests,
        payout_decisions=payout_decisions,
        precheck_log=precheck_log,
        buffer_alerts=buffer_alerts,
    ).build_dashboard_context()
