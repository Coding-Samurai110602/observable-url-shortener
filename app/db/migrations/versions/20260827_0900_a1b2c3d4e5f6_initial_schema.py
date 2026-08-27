"""initial schema: urls and click_events

Revision ID: a1b2c3d4e5f6
Revises:
Create Date: 2026-08-27 09:00:00.000000+00:00

Creates the two core tables and their indexes per the data model:

* ``urls``        — short_code -> long_url mapping with an aggregate click_count.
* ``click_events`` — append-only per-redirect history for analytics.

Indexes are created explicitly (not just via unique constraints) so the read
hot path (lookup by short_code) and the stats query (recent clicks per code,
newest first) are both index-served.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "urls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("short_code", sa.String(length=10), nullable=False),
        sa.Column("long_url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("click_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_urls_short_code"), "urls", ["short_code"], unique=True
    )
    op.create_index(op.f("ix_urls_client_id"), "urls", ["client_id"], unique=False)

    op.create_table(
        "click_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("short_code", sa.String(length=10), nullable=False),
        sa.Column(
            "clicked_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("client_ip_hash", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_click_events_short_code"), "click_events", ["short_code"], unique=False
    )
    op.create_index(
        "ix_click_events_short_code_clicked_at",
        "click_events",
        ["short_code", "clicked_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_click_events_short_code_clicked_at", table_name="click_events")
    op.drop_index(op.f("ix_click_events_short_code"), table_name="click_events")
    op.drop_table("click_events")

    op.drop_index(op.f("ix_urls_client_id"), table_name="urls")
    op.drop_index(op.f("ix_urls_short_code"), table_name="urls")
    op.drop_table("urls")
