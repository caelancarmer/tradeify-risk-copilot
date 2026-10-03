"""
payout_models.py -- SQLAlchemy ORM models for the payout automation tables.

Schema is created ONLY through Alembic migrations (alembic/versions/), never
via ``Base.metadata.create_all()`` in production. The migration in
``alembic/versions/0001_payout_tables.py`` is the source of truth.

Tables:
  payout_requests   one row per submitted request (request_id is the
                    idempotency key; UNIQUE constraint enforces it in Postgres)
  payout_decisions  one row per decision (APPROVED/REJECTED/MANUAL_REVIEW)
                    with JSONB reasons[], citations[], decided_at, decided_by
  kyc_status        per-trader KYC state (provider stub)
  audit_log         append-only trail: entity_type, entity_id, action,
                    payload, actor, timestamp

The in-memory demo in ``src/api.py`` mirrors these rows exactly so the API can
run without Postgres; production flips STORAGE_BACKEND=postgres and uses these
models behind the same function signatures.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PayoutRequestRow(Base):
    __tablename__ = "payout_requests"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_payout_requests_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trader_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount_requested: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    # Fingerprint lets the DB detect the "same request_id, different payload"
    # adversarial case without re-reading every column.
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class PayoutDecisionRow(Base):
    __tablename__ = "payout_decisions"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_payout_decisions_request_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # APPROVED|REJECTED|MANUAL_REVIEW
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    citations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False, default="engine")


class KYCStatusRow(Base):
    __tablename__ = "kyc_status"

    trader_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unverified")
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


# Convenience list for migrations and tests.
ALL_TABLES = ("payout_requests", "payout_decisions", "kyc_status", "audit_log")