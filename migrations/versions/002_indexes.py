"""add_performance_indexes

Revision ID: 002_indexes
Revises: 001_initial
Create Date: 2025-01-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "002_indexes"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Full-text search index on message content (PostgreSQL only)
    op.execute("""
        CREATE INDEX ix_messages_content_fts
        ON messages USING gin(to_tsvector('english', coalesce(content, '')))
    """)

    # Index for soft-deleted messages cleanup job
    op.create_index(
        "ix_messages_deleted_at",
        "messages",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    # Index for unread message queries
    op.create_index(
        "ix_members_last_read",
        "conversation_members",
        ["conversation_id", "last_read_at"],
    )

    # Index for active push tokens
    op.create_index(
        "ix_push_tokens_user_active",
        "push_tokens",
        ["user_id", "is_active"],
        postgresql_where=sa.text("is_active = true"),
    )

    # Partial index for non-revoked refresh tokens
    op.create_index(
        "ix_refresh_tokens_active",
        "refresh_tokens",
        ["token", "expires_at"],
        postgresql_where=sa.text("revoked = false"),
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_content_fts")
    op.drop_index("ix_messages_deleted_at", table_name="messages")
    op.drop_index("ix_members_last_read", table_name="conversation_members")
    op.drop_index("ix_push_tokens_user_active", table_name="push_tokens")
    op.drop_index("ix_refresh_tokens_active", table_name="refresh_tokens")
