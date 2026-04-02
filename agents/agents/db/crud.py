"""CRUD for users, projects, and all agent memory tables."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession


from agents.db.models import (
    Project,
    User,
    Plan,
    ReasoningNote,
    AgentLogEntry,
    TestRun,
    Deployment,
    Message,
)


async def create_user(
    session: AsyncSession,
    wallet: str | None = None,
    user_id: uuid.UUID | None = None,
) -> User:
    """Create a user. If user_id is provided, use it; otherwise generate."""
    user = User(
        id=user_id or uuid.uuid4(),
        wallet=wallet,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Fetch user by id."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def get_user_by_wallet(session: AsyncSession, wallet: str) -> User | None:
    """Fetch user by wallet address."""
    result = await session.execute(select(User).where(User.wallet == wallet))
    return result.scalar_one_or_none()


async def create_project(
    session: AsyncSession,
    user_id: uuid.UUID,
    name: str | None = None,
    screenshot_base64: str | None = None,
    project_id: uuid.UUID | None = None,
) -> Project:
    """Create a project for the given user."""
    project = Project(
        id=project_id or uuid.uuid4(),
        user_id=user_id,
        name=name,
        screenshot_base64=screenshot_base64,
    )
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return project


async def get_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Project | None:
    """Fetch project by id. If user_id is provided, ensure project belongs to user."""
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        return None
    if user_id is not None and project.user_id != user_id:
        return None
    return project


async def list_projects_by_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[Project]:
    """List all projects for a user."""
    result = await session.execute(
        select(Project)
        .where(Project.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    return list(result.scalars().all())


async def update_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    name: str | None = None,
    screenshot_base64: str | None = None,
    set_name: bool = False,
    set_screenshot_base64: bool = False,
) -> Project | None:
    """Update project fields by id; only provided fields are modified."""
    project = await get_project(session, project_id)
    if project is None:
        return None
    if set_name:
        project.name = name
    if set_screenshot_base64:
        project.screenshot_base64 = screenshot_base64
    await session.commit()
    await session.refresh(project)
    return project


# Plans
async def upsert_plan(
    session: AsyncSession,
    project_id: uuid.UUID,
    plan_data: dict,
    status: str = "draft",
) -> Plan:
    """
    Save or update the current plan for a project.
    There is only ever ONE active plan per project so this overwrites it.
    Returns the saved Plan row.
    """
    result = await session.execute(
        select(Plan)
        .where(Plan.project_id == project_id)
        .order_by(Plan.created_at.desc())
        .limit(1)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.plan_data = plan_data
        existing.status = status
        existing.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(existing)
        return existing

    plan = Plan(
        project_id=project_id,
        plan_data=plan_data,
        status=status,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    return plan


async def get_current_plan(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Plan | None:
    """Get the most recent plan for a project."""
    result = await session.execute(
        select(Plan)
        .where(Plan.project_id == project_id)
        .order_by(Plan.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_plan_status(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str,
) -> Plan | None:
    """Update the status of the current plan."""
    plan = await get_current_plan(session, project_id)
    if plan:
        plan.status = status
        plan.updated_at = datetime.now(timezone.utc)
        await session.commit()
        await session.refresh(plan)
    return plan


# The reasoning
async def add_reasoning_note(
    session: AsyncSession,
    project_id: uuid.UUID,
    note: str,
) -> ReasoningNote:
    """Append a reasoning note for this project."""
    row = ReasoningNote(project_id=project_id, note=note)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_reasoning_notes(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int = 20,
) -> list[ReasoningNote]:
    """Get the most recent reasoning notes for a project."""
    result = await session.execute(
        select(ReasoningNote)
        .where(ReasoningNote.project_id == project_id)
        .order_by(ReasoningNote.created_at.desc())
        .limit(limit)
    )
    # Returning in chronological order (oldest first)
    return list(reversed(result.scalars().all()))


# Agent Log
async def append_agent_log(
    session: AsyncSession,
    project_id: uuid.UUID,
    agent: str,
    action: str,
    user_id: str | None = None,
    summary: str | None = None,
    why: str | None = None,
    error: str | None = None,
) -> AgentLogEntry:
    """
    Append a lean log entry. No full payloads, summary only.
    This replaces the Letta global log block.
    """
    entry = AgentLogEntry(
        project_id=project_id,
        user_id=user_id,
        agent=agent,
        action=action,
        summary=summary,
        why=why,
        error=error,
    )
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def get_agent_log(
    session: AsyncSession,
    project_id: uuid.UUID,
    agent: str | None = None,
    limit: int = 30,
) -> list[AgentLogEntry]:
    """
    Get recent log entries for a project, optionally filtered by agent name.
    Capped at 30 by default so that agents only need recent context.
    """
    query = (
        select(AgentLogEntry)
        .where(AgentLogEntry.project_id == project_id)
        .order_by(AgentLogEntry.created_at.desc())
        .limit(limit)
    )
    if agent:
        query = query.where(AgentLogEntry.agent == agent)
    result = await session.execute(query)
    return list(reversed(result.scalars().all()))


# Test runs
async def save_test_run(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str,
    tests_run: int | None = None,
    tests_passed: int | None = None,
    output: str | None = None,
) -> TestRun:
    """Save a test run result for a project."""
    run = TestRun(
        project_id=project_id,
        status=status,
        tests_run=tests_run,
        tests_passed=tests_passed,
        output=output,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_last_test_run(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> TestRun | None:
    """Get the most recent test run for a project."""
    result = await session.execute(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .order_by(TestRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# The deployments
async def save_deployment(
    session: AsyncSession,
    project_id: uuid.UUID,
    status: str,
    contract_name: str | None = None,
    deployed_address: str | None = None,
    tx_hash: str | None = None,
    snowtrace_url: str | None = None,
    network: str = "avalanche_fuji",
) -> Deployment:
    """Record a deployment result for a project."""
    dep = Deployment(
        project_id=project_id,
        network=network,
        contract_name=contract_name,
        deployed_address=deployed_address,
        tx_hash=tx_hash,
        snowtrace_url=snowtrace_url,
        status=status,
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)
    return dep


async def get_last_deployment(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> Deployment | None:
    """Get the most recent deployment for a project."""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_deployments(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> list[Deployment]:
    """Get all deployments for a project, newest first."""
    result = await session.execute(
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(Deployment.created_at.desc())
    )
    return list(result.scalars().all())


# Messages
async def append_message(
    session: AsyncSession,
    project_id: uuid.UUID,
    session_id: str,
    sender: str,
    content: str,
) -> Message:
    """
    Persist one chat message for a project + session.
    Sender is expected to be: "user" | "agent" (enforced by callers).
    """
    row = Message(
        project_id=project_id,
        session_id=session_id,
        sender=sender,
        content=content,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_messages(
    session: AsyncSession,
    project_id: uuid.UUID,
    session_id: str | None = None,
    limit: int = 200,
) -> list[Message]:
    """List messages for a project, chronological; optionally filter by session."""
    query = (
        select(Message)
        .where(Message.project_id == project_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    if session_id:
        query = query.where(Message.session_id == session_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def create_pipeline_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    project_id: uuid.UUID,
    assigned_to: str,
    created_by: str,
    description: str,
    context: dict | None = None,
) -> "PipelineTask":
    """Push a new task onto the pipeline task stack."""
    from agents.db.models import PipelineTask

    task = PipelineTask(
        pipeline_run_id=pipeline_run_id,
        project_id=project_id,
        assigned_to=assigned_to,
        created_by=created_by,
        description=description,
        status="pending",
        context=context,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def get_next_pending_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> "PipelineTask | None":
    """Get the most recently created pending task (LIFO stack behavior)."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask)
        .where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.status == "pending",
        )
        .order_by(desc(PipelineTask.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_task_in_progress(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> "PipelineTask | None":
    """Mark a task as in_progress."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = "in_progress"
    await session.commit()
    await session.refresh(task)
    return task


async def get_pipeline_run_tasks(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> "list[PipelineTask]":
    """Get all tasks for a pipeline run, ordered by creation time."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask)
        .where(PipelineTask.pipeline_run_id == pipeline_run_id)
        .order_by(PipelineTask.created_at.asc())
    )
    return list(result.scalars().all())


async def get_latest_pipeline_run_id(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> uuid.UUID | None:
    """Get the most recent pipeline_run_id for a project."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask.pipeline_run_id)
        .where(PipelineTask.project_id == project_id)
        .order_by(desc(PipelineTask.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()
