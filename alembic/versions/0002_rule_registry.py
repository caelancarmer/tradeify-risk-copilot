"""0002 rule registry

Creates the ``rule_registry`` table read by ``src/rule_registry.py``: the
DB-backed catalogue of verified Tradeify rules and their citation chunks.

Revision ID: 0002_rule_registry
Revises: 0001_payout_tables
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0002_rule_registry"
down_revision = "0001_payout_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_registry",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.String(length=128), nullable=False),
        sa.Column("account_type", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("rule_name", sa.String(length=160), nullable=False),
        sa.Column("threshold", sa.String(length=256), nullable=False,
                  server_default=""),
        sa.Column("breach_type", sa.String(length=32), nullable=False,
                  server_default="INFO"),
        sa.Column("citation_chunk_id", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("rule_id", name="uq_rule_registry_rule_id"),
    )
    op.create_index("ix_rule_registry_rule_id", "rule_registry", ["rule_id"])
    op.create_index("ix_rule_registry_account_type", "rule_registry",
                    ["account_type"])


def downgrade() -> None:
    op.drop_index("ix_rule_registry_account_type", table_name="rule_registry")
    op.drop_index("ix_rule_registry_rule_id", table_name="rule_registry")
    op.drop_table("rule_registry")
