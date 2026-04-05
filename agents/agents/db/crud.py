"""CRUD for users, projects, and all agent memory tables."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, desc, func, update
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
    PipelineEvaluation,
    PipelineHumanGate,
    PipelineRun,
)


def pending_task_sort_key(task) -> tuple:
    """In-memory mirror of the FIFO dispatch ordering used by pending task queries."""
    return (task.created_at, task.sequence_index, task.id)


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
    pipeline_run_id: uuid.UUID | None = None,
    pipeline_task_id: uuid.UUID | None = None,
    artifact_revision: int = 0,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    exit_code: int | None = None,
    trace_id: str | None = None,
) -> TestRun:
    """Save a test run result for a project."""
    run = TestRun(
        project_id=project_id,
        status=status,
        tests_run=tests_run,
        tests_passed=tests_passed,
        output=output,
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        artifact_revision=artifact_revision,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        exit_code=exit_code,
        trace_id=trace_id,
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


async def list_test_runs(
    session: AsyncSession,
    project_id: uuid.UUID,
    limit: int = 20,
) -> list[TestRun]:
    result = await session.execute(
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .order_by(TestRun.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_test_run_for_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    pipeline_task_id: uuid.UUID,
) -> TestRun | None:
    result = await session.execute(
        select(TestRun)
        .where(
            TestRun.pipeline_run_id == pipeline_run_id,
            TestRun.pipeline_task_id == pipeline_task_id,
        )
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
    pipeline_run_id: uuid.UUID | None = None,
    pipeline_task_id: uuid.UUID | None = None,
    artifact_revision: int = 0,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    exit_code: int | None = None,
    trace_id: str | None = None,
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
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        artifact_revision=artifact_revision,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        exit_code=exit_code,
        trace_id=trace_id,
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
    limit: int | None = None,
) -> list[Deployment]:
    """Get all deployments for a project, newest first."""
    query = (
        select(Deployment)
        .where(Deployment.project_id == project_id)
        .order_by(Deployment.created_at.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_deployment_for_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    pipeline_task_id: uuid.UUID,
) -> Deployment | None:
    result = await session.execute(
        select(Deployment)
        .where(
            Deployment.pipeline_run_id == pipeline_run_id,
            Deployment.pipeline_task_id == pipeline_task_id,
        )
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_successful_terminal_deployment(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> Deployment | None:
    result = await session.execute(
        select(Deployment)
        .where(
            Deployment.pipeline_run_id == pipeline_run_id,
            Deployment.status == "success",
        )
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_pipeline_run(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None,
    plan_id: uuid.UUID | None = None,
    deployment_target: dict | None = None,
    trace_id: str | None = None,
    pipeline_run_id: uuid.UUID | None = None,
) -> PipelineRun:
    run = PipelineRun(
        id=pipeline_run_id or uuid.uuid4(),
        project_id=project_id,
        user_id=user_id,
        plan_id=plan_id,
        deployment_target=deployment_target,
        trace_id=trace_id,
        status="created",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def get_pipeline_run(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> PipelineRun | None:
    result = await session.execute(
        select(PipelineRun).where(PipelineRun.id == pipeline_run_id)
    )
    return result.scalar_one_or_none()


async def update_pipeline_run(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    **fields,
) -> PipelineRun | None:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None:
        return None
    for key, value in fields.items():
        setattr(run, key, value)
    run.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(run)
    return run


async def request_pipeline_cancellation(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    reason: str | None = None,
) -> PipelineRun | None:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None:
        return None
    if run.cancellation_requested_at is None:
        run.cancellation_requested_at = datetime.now(timezone.utc)
        run.cancellation_reason = reason
    if run.status not in {"completed", "failed", "cancelled"}:
        run.status = "cancellation_requested"
    run.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(run)
    return run


async def create_pipeline_human_gate(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    gate_type: str,
    pipeline_task_id: uuid.UUID | None = None,
    evaluation_id: uuid.UUID | None = None,
    requested_payload: dict | None = None,
    requested_reason: str | None = None,
    requested_by: str | None = None,
    trace_id: str | None = None,
) -> PipelineHumanGate:
    gate = PipelineHumanGate(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        evaluation_id=evaluation_id,
        gate_type=gate_type,
        requested_payload=requested_payload,
        requested_reason=requested_reason,
        requested_by=requested_by,
        trace_id=trace_id,
    )
    session.add(gate)
    await session.commit()
    await session.refresh(gate)
    return gate


async def get_pipeline_human_gate(
    session: AsyncSession,
    gate_id: uuid.UUID,
) -> PipelineHumanGate | None:
    result = await session.execute(
        select(PipelineHumanGate).where(PipelineHumanGate.id == gate_id)
    )
    return result.scalar_one_or_none()


async def list_pipeline_human_gates(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> list[PipelineHumanGate]:
    result = await session.execute(
        select(PipelineHumanGate)
        .where(PipelineHumanGate.pipeline_run_id == pipeline_run_id)
        .order_by(PipelineHumanGate.created_at.asc())
    )
    return list(result.scalars().all())


async def resolve_pipeline_human_gate(
    session: AsyncSession,
    gate_id: uuid.UUID,
    *,
    status: str,
    resolved_payload: dict | None = None,
    resolved_reason: str | None = None,
    resolved_by: str | None = None,
) -> PipelineHumanGate | None:
    gate = await get_pipeline_human_gate(session, gate_id)
    if gate is None:
        return None
    gate.status = status
    gate.resolved_payload = resolved_payload
    gate.resolved_reason = resolved_reason
    gate.resolved_by = resolved_by
    gate.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(gate)
    return gate


async def create_pipeline_evaluation(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    stage: str,
    evaluation_type: str,
    blocking: bool,
    status: str,
    summary: str,
    details_json: dict | None = None,
    artifact_revision: int = 0,
    pipeline_task_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> PipelineEvaluation:
    evaluation = PipelineEvaluation(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        stage=stage,
        evaluation_type=evaluation_type,
        blocking=blocking,
        status=status,
        summary=summary,
        details_json=details_json,
        artifact_revision=artifact_revision,
        trace_id=trace_id,
    )
    session.add(evaluation)
    await session.commit()
    await session.refresh(evaluation)
    return evaluation


async def list_pipeline_evaluations(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> list[PipelineEvaluation]:
    result = await session.execute(
        select(PipelineEvaluation)
        .where(PipelineEvaluation.pipeline_run_id == pipeline_run_id)
        .order_by(PipelineEvaluation.created_at.asc())
    )
    return list(result.scalars().all())


async def count_claimed_tasks_for_run(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> int:
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(func.count(PipelineTask.id)).where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.claimed_at.is_not(None),
        )
    )
    return int(result.scalar() or 0)


async def get_next_retry_attempt(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    retry_budget_key: str,
) -> int:
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(func.max(PipelineTask.retry_attempt)).where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.retry_budget_key == retry_budget_key,
        )
    )
    current = result.scalar()
    return int(current or -1) + 1


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
    task_type: str,
    description: str,
    context: dict | None = None,
    parent_task_id: uuid.UUID | None = None,
    sequence_index: int = 0,
    artifact_revision: int = 0,
    depends_on_task_ids: list[str] | None = None,
    retry_budget_key: str | None = None,
    retry_attempt: int = 0,
    failure_class: str | None = None,
    gate_id: uuid.UUID | None = None,
    status: str = "pending",
) -> "PipelineTask":
    """Push a new task onto the pipeline task stack."""
    from agents.db.models import PipelineTask

    task = PipelineTask(
        pipeline_run_id=pipeline_run_id,
        project_id=project_id,
        assigned_to=assigned_to,
        created_by=created_by,
        task_type=task_type,
        description=description,
        parent_task_id=parent_task_id,
        sequence_index=sequence_index,
        artifact_revision=artifact_revision,
        depends_on_task_ids=depends_on_task_ids,
        retry_budget_key=retry_budget_key,
        retry_attempt=retry_attempt,
        failure_class=failure_class,
        gate_id=gate_id,
        status=status,
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
    """Get the next pending task using FIFO ordering plus sibling sequence."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask)
        .where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.status == "pending",
        )
        .order_by(
            PipelineTask.created_at.asc(),
            PipelineTask.sequence_index.asc(),
            PipelineTask.id.asc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def claim_next_pending_task(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> "PipelineTask | None":
    """Atomically claim the next runnable pending task for a pipeline run."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask)
        .where(PipelineTask.pipeline_run_id == pipeline_run_id)
        .order_by(
            PipelineTask.created_at.asc(),
            PipelineTask.sequence_index.asc(),
            PipelineTask.id.asc(),
        )
    )
    tasks = list(result.scalars().all())
    completed_ids = {str(task.id) for task in tasks if task.status == "completed"}

    for task in tasks:
        if task.status != "pending":
            continue
        dependencies = task.depends_on_task_ids or []
        if any(dep not in completed_ids for dep in dependencies):
            continue

        claim_time = datetime.now(timezone.utc)
        claim_result = await session.execute(
            update(PipelineTask)
            .where(PipelineTask.id == task.id, PipelineTask.status == "pending")
            .values(status="in_progress", claimed_at=claim_time)
        )
        if claim_result.rowcount:
            await session.commit()
            refresh_result = await session.execute(
                select(PipelineTask).where(PipelineTask.id == task.id)
            )
            return refresh_result.scalar_one_or_none()
        await session.rollback()

    return None


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
    task.claimed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(task)
    return task


async def get_pipeline_task(
    session: AsyncSession,
    task_id: uuid.UUID,
) -> "PipelineTask | None":
    """Fetch one pipeline task by its ID."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == task_id)
    )
    return result.scalar_one_or_none()


async def get_pipeline_run_tasks(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> "list[PipelineTask]":
    """Get all tasks for a pipeline run, ordered by creation time."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask)
        .where(PipelineTask.pipeline_run_id == pipeline_run_id)
        .order_by(
            PipelineTask.created_at.asc(),
            PipelineTask.sequence_index.asc(),
            PipelineTask.id.asc(),
        )
    )
    return list(result.scalars().all())


async def complete_pipeline_task_and_create_next(
    session: AsyncSession,
    *,
    pipeline_run_id: uuid.UUID,
    project_id: uuid.UUID,
    task_id: uuid.UUID,
    task_status: str,
    result_summary: str,
    next_tasks: list[dict],
    created_by: str,
) -> tuple["PipelineTask | None", list["PipelineTask"]]:
    """Complete a task and create the next tasks in one async transaction."""
    from agents.db.models import PipelineTask

    result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return None, []

    task.status = task_status
    task.result_summary = result_summary
    task.completed_at = datetime.now(timezone.utc)

    created: list[PipelineTask] = []
    for idx, payload in enumerate(next_tasks):
        new_task = PipelineTask(
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=payload["assigned_to"],
            created_by=created_by,
            task_type=payload["task_type"],
            description=payload["description"],
            context=payload.get("context"),
            parent_task_id=uuid.UUID(payload["parent_task_id"])
            if payload.get("parent_task_id")
            else task_id,
            sequence_index=payload.get("sequence_index", idx),
            artifact_revision=payload.get("artifact_revision", task.artifact_revision),
            depends_on_task_ids=payload.get("depends_on_task_ids"),
            retry_budget_key=payload.get("retry_budget_key"),
            retry_attempt=payload.get("retry_attempt", 0),
            failure_class=payload.get("failure_class"),
            gate_id=uuid.UUID(payload["gate_id"])
            if payload.get("gate_id")
            else None,
            status=payload.get("status", "pending"),
        )
        session.add(new_task)
        created.append(new_task)

    await session.commit()
    await session.refresh(task)
    for new_task in created:
        await session.refresh(new_task)
    return task, created


async def get_latest_pipeline_run_id(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> uuid.UUID | None:
    """Get the most recent pipeline_run_id for a project."""
    result = await session.execute(
        select(PipelineRun.id)
        .where(PipelineRun.project_id == project_id)
        .order_by(desc(PipelineRun.created_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_pipeline_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    **fields,
):
    from agents.db.models import PipelineTask

    task = await get_pipeline_task(session, task_id)
    if task is None:
        return None
    for key, value in fields.items():
        setattr(task, key, value)
    await session.commit()
    await session.refresh(task)
    return task


async def cancel_pending_followup_tasks(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    parent_task_id: uuid.UUID,
) -> int:
    from agents.db.models import PipelineTask

    result = await session.execute(
        update(PipelineTask)
        .where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.parent_task_id == parent_task_id,
            PipelineTask.status == "pending",
        )
        .values(status="cancelled", completed_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return int(result.rowcount or 0)
