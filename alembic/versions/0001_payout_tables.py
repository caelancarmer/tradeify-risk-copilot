"""0001 payout tables

Creates the four payout-automation tables:
  payout_requests, payout_decisions, kyc_status, audit_log.

Revision ID: 0001_payout_tables
Revises:
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_payout_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payout_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("trader_id", sa.String(length=128), nullable=False),
        sa.Column("amount_requested", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default="PENDING"),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("request_id", name="uq_payout_requests_request_id"),
    )
    op.create_index("ix_payout_requests_account_id", "payout_requests",
                    ["account_id"])
    op.create_index("ix_payout_requests_trader_id", "payout_requests",
                    ["trader_id"])

    op.create_table(
        "payout_decisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False,
                  server_default="0"),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("decided_by", sa.String(length=64), nullable=False,
                  server_default="engine"),
        sa.UniqueConstraint("request_id", name="uq_payout_decisions_request_id"),
    )
    op.create_index("ix_payout_decisions_request_id", "payout_decisions",
                    ["request_id"])

    op.create_table(
        "kyc_status",
        sa.Column("trader_id", sa.String(length=128), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False,
                  server_default="unverified"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("method", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False,
                  server_default="system"),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_entity_id", table_name="audit_log")
    op.drop_index("ix_audit_log_entity_type", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("kyc_status")
    op.drop_index("ix_payout_decisions_request_id", table_name="payout_decisions")
    op.drop_table("payout_decisions")
    op.drop_index("ix_payout_requests_trader_id", table_name="payout_requests")
    op.drop_index("ix_payout_requests_account_id", table_name="payout_requests")
    op.drop_table("payout_requests")