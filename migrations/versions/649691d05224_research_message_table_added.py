"""research message table added

Revision ID: 649691d05224
Revises: 4667248c39b0
Create Date: 2026-10-29 17:48:32.122081

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '649691d05224'
down_revision: Union[str, Sequence[str], None] = '4667248c39b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_CK = "ck_research_jobs_status"  # <- adjust if your naming convention differs
NEW_STATUSES = "'planned', 'queued', 'running', 'completed', 'failed', 'cancelled'"
OLD_STATUSES = "'planned', 'running', 'completed', 'failed'"
 
 
def upgrade() -> None:
    # ── research_jobs: new columns ──────────────────────────────────────────
    op.add_column("research_jobs", sa.Column("title", sa.String(length=200), nullable=True))
    op.add_column("research_jobs", sa.Column("stage", sa.String(length=60), nullable=True))
    op.add_column("research_jobs", sa.Column("error_code", sa.String(length=40), nullable=True))
    op.add_column("research_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("research_jobs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "research_jobs",
        sa.Column(
            "parent_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_jobs.id", ondelete="SET NULL", name="fk_research_jobs_parent_job_id"),
            nullable=True,
        ),
    )
    op.create_index("ix_research_jobs_parent_job_id", "research_jobs", ["parent_job_id"])
 
    # ── status CHECK: add 'queued' and 'cancelled' (autogenerate can't detect this) ──
    op.drop_constraint(STATUS_CK, "research_jobs", type_="check")
    op.create_check_constraint(STATUS_CK, "research_jobs", f"status IN ({NEW_STATUSES})")
 
    # ── backfill so existing jobs look right in the sidebar ──
    op.execute("UPDATE research_jobs SET title = LEFT(query, 80) WHERE title IS NULL")
    op.execute(
        "UPDATE research_jobs SET completed_at = updated_at "
        "WHERE status IN ('completed', 'failed') AND completed_at IS NULL"
    )
    # sources stays JSONB; old rows hold ["https://..."] and are normalised by the service.
 
    # ── research_messages (follow-up chat) ──────────────────────────────────
    op.create_table(
        "research_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"),
                  primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("research_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("suggested_query", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_research_messages_role"),
    )
    op.create_index("ix_research_messages_job_created", "research_messages", ["job_id", "created_at"])
 
 
def downgrade() -> None:
    # ── research_messages ───────────────────────────────────────────────────
    op.drop_index("ix_research_messages_job_created", table_name="research_messages")
    op.drop_table("research_messages")
 
    # ── statuses the old schema doesn't know must be rewritten before the CHECK is restored ──
    op.execute(
        "UPDATE research_jobs SET status = 'failed', "
        "error = COALESCE(error, 'Reverted by migration downgrade.') "
        "WHERE status IN ('queued', 'cancelled')"
    )
    op.drop_constraint(STATUS_CK, "research_jobs", type_="check")
    op.create_check_constraint(STATUS_CK, "research_jobs", f"status IN ({OLD_STATUSES})")
 
    # ── sources: structured objects back to plain URL strings (what the old code expects) ──
    op.execute(
        """
        UPDATE research_jobs
        SET sources = COALESCE(
            (SELECT jsonb_agg(CASE WHEN jsonb_typeof(e) = 'object' THEN e -> 'url' ELSE e END)
             FROM jsonb_array_elements(sources) AS e),
            '[]'::jsonb)
        WHERE jsonb_typeof(sources) = 'array' AND sources <> '[]'::jsonb
        """
    )
 
    # ── research_jobs: drop new columns ─────────────────────────────────────
    op.drop_index("ix_research_jobs_parent_job_id", table_name="research_jobs")
    op.drop_constraint("fk_research_jobs_parent_job_id", "research_jobs", type_="foreignkey")
    op.drop_column("research_jobs", "parent_job_id")
    op.drop_column("research_jobs", "completed_at")
    op.drop_column("research_jobs", "started_at")
    op.drop_column("research_jobs", "error_code")
    op.drop_column("research_jobs", "stage")
    op.drop_column("research_jobs", "title")
