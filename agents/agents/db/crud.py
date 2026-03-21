"""CRUD for users, projects, and pipeline tasks."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from agents.db.models import Project, User, PipelineTask


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
    project_id: uuid.UUID | None = None,
) -> Project:
    """Create a project for the given user."""
    project = Project(
        id=project_id or uuid.uuid4(),
        user_id=user_id,
        name=name,
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


async def create_pipeline_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    project_id: uuid.UUID,
    assigned_to: str,
    created_by: str,
    description: str,
    context: dict | None = None,
) -> PipelineTask:
    """Push a new task onto the pipeline task stack."""
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
) -> PipelineTask | None:
    """
    Get the most recently created pending task for this pipeline run.
    """
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


async def get_current_in_progress_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> PipelineTask | None:
    """Get the currently in-progress task for this pipeline run (should be at most one)."""
    result = await session.execute(
        select(PipelineTask)
        .where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.status == "in_progress",
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_task_in_progress(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> PipelineTask | None:
    """Mark a task as in_progress. Called by the orchestrator before dispatching."""
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


async def complete_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    result_summary: str | None = None,
) -> PipelineTask | None:
    """Mark a task as completed. Called by the agent tool when work is done."""
    result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = "completed"
    task.result_summary = result_summary
    task.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(task)
    return task


async def fail_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    result_summary: str | None = None,
) -> PipelineTask | None:
    """Mark a task as failed. Called when an agent cannot complete its work."""
    result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None
    task.status = "failed"
    task.result_summary = result_summary
    task.completed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(task)
    return task


async def get_pipeline_run_tasks(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> list[PipelineTask]:
    """Get all tasks for a pipeline run, ordered by creation time (oldest first)."""
    result = await session.execute(
        select(PipelineTask)
        .where(PipelineTask.pipeline_run_id == pipeline_run_id)
        .order_by(PipelineTask.created_at.asc())
    )
    return list(result.scalars().all())


async def get_pipeline_task_count(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> int:
    """Count total tasks in a pipeline run (for the iteration cap check)."""
    result = await session.execute(
        select(PipelineTask).where(PipelineTask.pipeline_run_id == pipeline_run_id)
    )
    return len(list(result.scalars().all()))


async def get_latest_pipeline_run_id(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> uuid.UUID | None:
    """Get the most recent pipeline_run_id for a project."""
    result = await session.execute(
        select(PipelineTask.pipeline_run_id)
        .where(PipelineTask.project_id == project_id)
        .order_by(desc(PipelineTask.created_at))
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row
