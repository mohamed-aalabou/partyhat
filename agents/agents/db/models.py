"""SQLAlchemy models for users, projects, and pipeline tasks (Neon Postgres)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Text, JSON
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    wallet: Mapped[str | None] = mapped_column(Text, unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    projects: Mapped[list["Project"]] = relationship(
        "Project",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship("User", back_populates="projects")
    pipeline_tasks: Mapped[list["PipelineTask"]] = relationship(
        "PipelineTask",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PipelineTask.created_at.desc()",
    )


class PipelineTask(Base):
    """
    Shared task stack for the autonomous pipeline.

    Agents push tasks onto this stack for other agents to pick up.
    The orchestrator reads the most recently created pending task
    and dispatches it to the assigned agent. This is a LIFO stack,
    not a priority queue.

    Each pipeline run groups tasks by pipeline_run_id so the frontend
    can display the full history of a single run.
    """

    __tablename__ = "pipeline_tasks"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    assigned_to: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Agent name: coding | testing | deployment | audit",
    )
    created_by: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Who created: orchestrator | coding | testing | deployment | audit",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Natural language instruction the agent receives",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        comment="pending | in_progress | completed | failed",
    )
    context: Mapped[dict | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Error output, file paths, or any data the next agent needs",
    )
    result_summary: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Brief summary of what the agent did when completing the task",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="pipeline_tasks"
    )
