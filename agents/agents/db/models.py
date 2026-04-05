"""SQLAlchemy models for users, projects, and pipeline tasks (Neon Postgres)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, JSON, Index
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
    screenshot_base64: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    user: Mapped["User"] = relationship("User", back_populates="projects")

    # Relationships to agent tables
    plans: Mapped[list["Plan"]] = relationship(
        "Plan",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Plan.created_at.desc()",
    )
    reasoning_notes: Mapped[list["ReasoningNote"]] = relationship(
        "ReasoningNote",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    agent_log_entries: Mapped[list["AgentLogEntry"]] = relationship(
        "AgentLogEntry",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    test_runs: Mapped[list["TestRun"]] = relationship(
        "TestRun",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    deployments: Mapped[list["Deployment"]] = relationship(
        "Deployment",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Message.created_at.asc()",
    )
    pipeline_tasks: Mapped[list["PipelineTask"]] = relationship(
        "PipelineTask",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PipelineTask.created_at.desc()",
    )
    pipeline_runs: Mapped[list["PipelineRun"]] = relationship(
        "PipelineRun",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PipelineRun.created_at.desc()",
    )
    pipeline_human_gates: Mapped[list["PipelineHumanGate"]] = relationship(
        "PipelineHumanGate",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PipelineHumanGate.created_at.desc()",
    )
    pipeline_evaluations: Mapped[list["PipelineEvaluation"]] = relationship(
        "PipelineEvaluation",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="PipelineEvaluation.created_at.desc()",
    )


class Plan(Base):
    """
    Full smart contract plan JSON per project.
    Replaces the bloated current_plan blob stored directly in the Letta block.
    So Letta will only store: plan_id (UUID) + status (string).
    """

    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "draft" | "ready" | "generating" | "testing" | "deployed"
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    # The full SmartContractPlan as JSON (model.model_dump())
    plan_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="plans")


class ReasoningNote(Base):
    """
    Planning agent's WHY notes i.e the episodic memory layer.
    Replaces the reasoning_notes list in the Letta user block.
    Letta will store nothing for these and agents fetch via get_reasoning_notes().
    """

    __tablename__ = "reasoning_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="reasoning_notes"
    )


class AgentLogEntry(Base):
    """
    Full audit trail; replaces the Letta global_agent_log block entirely.
    Proper columns so logs are queryable by agent, action, time.
    Letta will store nothing for this.
    """

    __tablename__ = "agent_log"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    # Lightweight summary only no full code dumps
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="agent_log_entries"
    )


class TestRun(Base):
    """
    Testing agent results per project.
    Letta will only store: last_test_status ("passed" | "failed" | "error").
    """

    __tablename__ = "test_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "passed" | "failed" | "error"
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Number of tests run / passed
    tests_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tests_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The full Foundry output stored here, not in Letta
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    pipeline_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stdout_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="test_runs")


class Deployment(Base):
    """
    Deployment records per project.
    Letta will only store: deployed_address + tx_hash (small strings).
    """

    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Avalanche Fuji
    network: Mapped[str] = mapped_column(Text, nullable=False, default="avalanche_fuji")
    contract_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    deployed_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    snowtrace_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # "success" | "failed"
    status: Mapped[str] = mapped_column(Text, nullable=False, default="success")
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    pipeline_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stdout_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="deployments")


class Message(Base):
    """
    Persistent chat messages per project + session.
    Sender is constrained in code to: "user" | "agent".
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    sender: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    project: Mapped["Project"] = relationship("Project", back_populates="messages")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_project_created", "project_id", "created_at"),
        Index("ix_pipeline_runs_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="created",
        comment=(
            "created | running | waiting_for_approval | cancellation_requested | "
            "cancelled | completed | failed"
        ),
    )
    current_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    deployment_target: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    terminal_deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    failure_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship("Project", back_populates="pipeline_runs")


class PipelineHumanGate(Base):
    __tablename__ = "pipeline_human_gates"
    __table_args__ = (
        Index("ix_pipeline_human_gates_run_created", "pipeline_run_id", "created_at"),
        Index("ix_pipeline_human_gates_run_status", "pipeline_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    evaluation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    gate_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        comment="pending | approved | rejected | overridden",
    )
    requested_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    resolved_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    requested_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="pipeline_human_gates"
    )


class PipelineEvaluation(Base):
    __tablename__ = "pipeline_evaluations"
    __table_args__ = (
        Index(
            "ix_pipeline_evaluations_run_stage_created",
            "pipeline_run_id",
            "stage",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pipeline_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_type: Mapped[str] = mapped_column(Text, nullable=False)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="passed",
        comment="passed | failed | advisory",
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifact_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trace_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="pipeline_evaluations"
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
    __table_args__ = (
        Index(
            "ix_pipeline_tasks_dispatch_status_created",
            "pipeline_run_id",
            "status",
            "created_at",
            "sequence_index",
            "id",
        ),
        Index(
            "ix_pipeline_tasks_dispatch_revision",
            "pipeline_run_id",
            "status",
            "artifact_revision",
            "created_at",
            "sequence_index",
            "id",
        ),
    )

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
    task_type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="unknown",
        comment="Canonical task type, usually <agent>.<action>",
    )
    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Natural language instruction the agent receives",
    )
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pipeline_tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Optional parent task for hierarchical subtasks",
    )
    sequence_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Sibling ordering index for predictable FIFO dispatch",
    )
    artifact_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Monotonic code artifact revision propagated through the pipeline.",
    )
    depends_on_task_ids: Mapped[list[str] | None] = mapped_column(
        JSON,
        nullable=True,
        comment="Optional list of prerequisite pipeline task UUID strings.",
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="pending",
        comment="pending | in_progress | waiting_for_approval | completed | failed | cancelled",
    )
    retry_budget_key: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        index=True,
        comment="Retry policy key for this task.",
    )
    retry_attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Monotonic retry attempt for the retry policy key on this run.",
    )
    failure_class: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Canonical failure classification for retries and gates.",
    )
    gate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment="Optional human gate associated with this task.",
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
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="pipeline_tasks"
    )
    
