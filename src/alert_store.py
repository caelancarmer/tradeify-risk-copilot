"""
alert_store.py -- Append-only in-memory store for buffer alerts.

Shared by the worker (which writes) and the Ops console dashboard (which
reads), so neither has to import the other. Production swaps this for the
``buffer_alerts`` table; the record shape is the same.

A record is written for every threshold crossing that the monitor observes,
including suppressed ones (``suppressed=True``, ``sent=False``), so operators
can see anti-spam behaviour. Only ``sent=True`` rows count as alerts sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

BUFFER_ALERTS: list[dict] = []


def record_alert(*, account_key: str, trader_id: str, severity: str,
                 buffer_usd: float, buffer_pct: float, message: str,
                 channel: str, sent: bool, suppressed: bool = False,
                 detected_at: Optional[datetime] = None,
                 sent_at: Optional[datetime] = None,
                 latency_ms: float = 0.0) -> dict:
    detected_at = detected_at or datetime.now(timezone.utc)
    sent_at = sent_at or detected_at
    entry = {
        "account_key": account_key,
        "trader_id": trader_id,
        "severity": severity,
        "buffer_usd": round(buffer_usd, 2),
        "buffer_pct": round(buffer_pct, 2),
        "message": message,
        "channel": channel,
        "sent": bool(sent),
        "suppressed": bool(suppressed),
        "detected_at": detected_at.isoformat(),
        "sent_at": sent_at.isoformat(),
        "latency_ms": round(latency_ms, 3),
    }
    BUFFER_ALERTS.append(entry)
    return entry


def alerts_today(now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    return [a for a in BUFFER_ALERTS
            if a["detected_at"][:10] == now.date().isoformat()]


def alert_summary(now: Optional[datetime] = None) -> dict:
    """Counts for the 'Buffer Alerts Sent' panel (sent rows only)."""
    rows = alerts_today(now)
    sent = [a for a in rows if a["sent"]]
    by_severity: dict[str, int] = {}
    for a in sent:
        by_severity[a["severity"]] = by_severity.get(a["severity"], 0) + 1
    return {
        "sent_count": len(sent),
        "suppressed_count": sum(1 for a in rows if a["suppressed"]),
        "by_severity": by_severity,
        "alerts": list(reversed(sent[-20:])),
    }


def clear() -> None:
    BUFFER_ALERTS.clear()
