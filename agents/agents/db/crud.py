"""CRUD for users, projects, and all agent memory tables."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, desc, func, or_, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from agents.db import run_with_retry
from agents.pipeline_status import build_pipeline_status_payload

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
    PipelineRunSnapshot,
    PipelineRunEvent,
    NotificationOutbox,
    ProjectRuntimeState,
    TelegramLinkToken,
    TelegramUserLink,
)


def pending_task_sort_key(task) -> tuple:
    """In-memory mirror of the FIFO dispatch ordering used by pending task queries."""
    return (task.created_at, task.sequence_index, task.id)


async def _get_user_by_id_once(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


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
    return await run_with_retry(
        session,
        lambda active_session: _get_user_by_id_once(active_session, user_id),
    )


async def _get_user_by_wallet_once(session: AsyncSession, wallet: str) -> User | None:
    result = await session.execute(select(User).where(User.wallet == wallet))
    return result.scalar_one_or_none()


async def get_user_by_wallet(session: AsyncSession, wallet: str) -> User | None:
    """Fetch user by wallet address."""
    return await run_with_retry(
        session,
        lambda active_session: _get_user_by_wallet_once(active_session, wallet),
    )


async def get_telegram_user_link(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> TelegramUserLink | None:
    result = await session.execute(
        select(TelegramUserLink).where(TelegramUserLink.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def upsert_telegram_user_link(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    chat_id: int,
    chat_type: str,
    telegram_user_id: int | None = None,
    chat_username: str | None = None,
    first_name: str | None = None,
    enabled: bool = True,
) -> TelegramUserLink:
    existing_for_user = await get_telegram_user_link(session, user_id)
    result = await session.execute(
        select(TelegramUserLink).where(TelegramUserLink.chat_id == int(chat_id))
    )
    existing_for_chat = result.scalar_one_or_none()
    if existing_for_chat is not None and existing_for_chat.user_id != user_id:
        await session.delete(existing_for_chat)

    now = datetime.now(timezone.utc)
    if existing_for_user is None:
        row = TelegramUserLink(
            user_id=user_id,
            chat_id=int(chat_id),
            chat_type=chat_type,
            telegram_user_id=telegram_user_id,
            chat_username=chat_username,
            first_name=first_name,
            enabled=enabled,
            linked_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row = existing_for_user
        row.chat_id = int(chat_id)
        row.chat_type = chat_type
        row.telegram_user_id = telegram_user_id
        row.chat_username = chat_username
        row.first_name = first_name
        row.enabled = enabled
        row.linked_at = now
        row.updated_at = now

    await session.commit()
    await session.refresh(row)
    return row


async def set_telegram_user_link_enabled(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    enabled: bool,
) -> TelegramUserLink | None:
    row = await get_telegram_user_link(session, user_id)
    if row is None:
        return None
    row.enabled = enabled
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_telegram_user_link(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> bool:
    row = await get_telegram_user_link(session, user_id)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def delete_unused_telegram_link_tokens(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    result = await session.execute(
        select(TelegramLinkToken).where(
            TelegramLinkToken.user_id == user_id,
            TelegramLinkToken.used_at.is_(None),
        )
    )
    rows = list(result.scalars().all())
    for row in rows:
        await session.delete(row)
    await session.commit()
    return len(rows)


async def create_telegram_link_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    token_hash: str,
    expires_at: datetime,
) -> TelegramLinkToken:
    row = TelegramLinkToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def consume_telegram_link_token(
    session: AsyncSession,
    token_hash: str,
) -> TelegramLinkToken | None:
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(TelegramLinkToken)
        .where(TelegramLinkToken.token_hash == token_hash)
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    if row.used_at is not None or row.expires_at <= now:
        return None
    row.used_at = now
    await session.commit()
    await session.refresh(row)
    return row


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


async def _get_project_once(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Project | None:
    result = await session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        return None
    if user_id is not None and project.user_id != user_id:
        return None
    return project


async def _project_access_exists_once(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    result = await session.execute(
        select(Project.id).where(
            Project.id == project_id,
            Project.user_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


async def project_access_exists(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
) -> bool:
    return await run_with_retry(
        session,
        lambda active_session: _project_access_exists_once(
            active_session,
            project_id,
            user_id,
        ),
    )


async def get_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
) -> Project | None:
    """Fetch project by id. If user_id is provided, ensure project belongs to user."""
    return await run_with_retry(
        session,
        lambda active_session: _get_project_once(
            active_session,
            project_id,
            user_id=user_id,
        ),
    )


async def _list_projects_by_user_once(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    include_screenshot: bool = False,
) -> list[Project]:
    query = (
        select(Project)
        .where(Project.user_id == user_id)
        .order_by(Project.created_at.desc())
    )
    if not include_screenshot:
        query = query.options(
            load_only(
                Project.id,
                Project.user_id,
                Project.name,
                Project.created_at,
            )
        )
    result = await session.execute(query)
    return list(result.scalars().all())


async def list_projects_by_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    include_screenshot: bool = False,
) -> list[Project]:
    """List all projects for a user."""
    return await run_with_retry(
        session,
        lambda active_session: _list_projects_by_user_once(
            active_session,
            user_id,
            include_screenshot=include_screenshot,
        ),
    )


async def _update_project_once(
    session: AsyncSession,
    project_id: uuid.UUID,
    name: str | None = None,
    screenshot_base64: str | None = None,
    set_name: bool = False,
    set_screenshot_base64: bool = False,
) -> Project | None:
    project = await _get_project_once(session, project_id)
    if project is None:
        return None
    if set_name:
        project.name = name
    if set_screenshot_base64:
        project.screenshot_base64 = screenshot_base64
    await session.commit()
    await session.refresh(project)
    return project


async def update_project(
    session: AsyncSession,
    project_id: uuid.UUID,
    name: str | None = None,
    screenshot_base64: str | None = None,
    set_name: bool = False,
    set_screenshot_base64: bool = False,
) -> Project | None:
    """Update project fields by id; only provided fields are modified."""
    return await run_with_retry(
        session,
        lambda active_session: _update_project_once(
            active_session,
            project_id,
            name=name,
            screenshot_base64=screenshot_base64,
            set_name=set_name,
            set_screenshot_base64=set_screenshot_base64,
        ),
    )


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
    *,
    include_output: bool = True,
) -> list[TestRun]:
    query = (
        select(TestRun)
        .where(TestRun.project_id == project_id)
        .order_by(TestRun.created_at.desc())
        .limit(limit)
    )
    if not include_output:
        query = query.options(
            load_only(
                TestRun.id,
                TestRun.project_id,
                TestRun.status,
                TestRun.tests_run,
                TestRun.tests_passed,
                TestRun.pipeline_run_id,
                TestRun.pipeline_task_id,
                TestRun.artifact_revision,
                TestRun.stdout_path,
                TestRun.stderr_path,
                TestRun.exit_code,
                TestRun.trace_id,
                TestRun.created_at,
            )
        )
    result = await session.execute(query)
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
    plan_contract_id: str | None = None,
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
    deployed_contracts: list[dict] | None = None,
    executed_calls: list[dict] | None = None,
) -> Deployment:
    """Record a deployment result for a project."""
    dep = Deployment(
        project_id=project_id,
        network=network,
        contract_name=contract_name,
        plan_contract_id=plan_contract_id,
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
        deployed_contracts=deployed_contracts,
        executed_calls=executed_calls,
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


async def get_notification_outbox_by_dedupe_key(
    session: AsyncSession,
    dedupe_key: str,
) -> NotificationOutbox | None:
    result = await session.execute(
        select(NotificationOutbox).where(NotificationOutbox.dedupe_key == dedupe_key)
    )
    return result.scalar_one_or_none()


async def enqueue_notification_outbox(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    channel: str,
    event_type: str,
    payload_json: dict,
    dedupe_key: str,
) -> NotificationOutbox:
    existing = await get_notification_outbox_by_dedupe_key(session, dedupe_key)
    if existing is not None:
        return existing

    row = NotificationOutbox(
        user_id=user_id,
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        channel=channel,
        event_type=event_type,
        payload_json=payload_json,
        dedupe_key=dedupe_key,
        status="pending",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def claim_next_notification_outbox(
    session: AsyncSession,
    *,
    channel: str,
    stale_after_seconds: int = 300,
) -> NotificationOutbox | None:
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=stale_after_seconds)
    result = await session.execute(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.channel == channel,
            or_(
                NotificationOutbox.status == "pending",
                (
                    (NotificationOutbox.status == "claimed")
                    & (NotificationOutbox.claimed_at.is_not(None))
                    & (NotificationOutbox.claimed_at < stale_cutoff)
                ),
            ),
        )
        .order_by(NotificationOutbox.created_at.asc(), NotificationOutbox.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        await session.rollback()
        return None

    row.status = "claimed"
    row.claimed_at = now
    row.attempts = int(row.attempts or 0) + 1
    row.updated_at = now
    await session.commit()
    await session.refresh(row)
    return row


async def mark_notification_outbox_sent(
    session: AsyncSession,
    notification_id: uuid.UUID,
) -> NotificationOutbox | None:
    result = await session.execute(
        select(NotificationOutbox).where(NotificationOutbox.id == notification_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    row.status = "sent"
    row.sent_at = now
    row.claimed_at = None
    row.last_error = None
    row.updated_at = now
    await session.commit()
    await session.refresh(row)
    return row


async def mark_notification_outbox_pending(
    session: AsyncSession,
    notification_id: uuid.UUID,
    *,
    last_error: str | None = None,
) -> NotificationOutbox | None:
    result = await session.execute(
        select(NotificationOutbox).where(NotificationOutbox.id == notification_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "pending"
    row.claimed_at = None
    row.last_error = last_error
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row


async def mark_notification_outbox_failed(
    session: AsyncSession,
    notification_id: uuid.UUID,
    *,
    last_error: str | None = None,
) -> NotificationOutbox | None:
    result = await session.execute(
        select(NotificationOutbox).where(NotificationOutbox.id == notification_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    row.status = "failed"
    row.claimed_at = None
    row.last_error = last_error
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(row)
    return row


async def finalize_pipeline_run_and_enqueue_notification(
    session: AsyncSession,
    *,
    pipeline_run_id: uuid.UUID,
    status: str,
    completed_at: datetime,
    terminal_deployment_id: uuid.UUID | None = None,
    failure_class: str | None = None,
    failure_reason: str | None = None,
    notification_channel: str = "telegram",
    notification_event_type: str | None = None,
    notification_payload: dict | None = None,
    notification_dedupe_key: str | None = None,
) -> tuple[PipelineRun | None, NotificationOutbox | None]:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None:
        return None, None

    run.status = status
    run.completed_at = completed_at
    run.terminal_deployment_id = terminal_deployment_id
    run.failure_class = failure_class
    run.failure_reason = failure_reason
    run.updated_at = datetime.now(timezone.utc)

    outbox: NotificationOutbox | None = None
    if (
        run.user_id is not None
        and notification_event_type
        and notification_payload is not None
        and notification_dedupe_key
    ):
        link = await get_telegram_user_link(session, run.user_id)
        existing = await get_notification_outbox_by_dedupe_key(
            session, notification_dedupe_key
        )
        if link is not None and link.enabled and existing is None:
            outbox = NotificationOutbox(
                user_id=run.user_id,
                project_id=run.project_id,
                pipeline_run_id=run.id,
                channel=notification_channel,
                event_type=notification_event_type,
                payload_json=notification_payload,
                dedupe_key=notification_dedupe_key,
                status="pending",
            )
            session.add(outbox)
        elif existing is not None:
            outbox = existing

    await session.commit()
    await session.refresh(run)
    if outbox is not None:
        await session.refresh(outbox)
    await refresh_pipeline_run_snapshot(session, run.id)
    return run, outbox


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
    await refresh_pipeline_run_snapshot(session, run.id)
    return run


async def get_pipeline_run(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> PipelineRun | None:
    async def _get_pipeline_run_once(active_session: AsyncSession) -> PipelineRun | None:
        result = await active_session.execute(
            select(PipelineRun).where(PipelineRun.id == pipeline_run_id)
        )
        return result.scalar_one_or_none()

    return await run_with_retry(session, _get_pipeline_run_once)


async def acquire_pipeline_run_lease(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    runner_token: str,
    *,
    replace_stale_after_seconds: int = 900,
) -> PipelineRun | None:
    result = await session.execute(
        select(PipelineRun)
        .where(PipelineRun.id == pipeline_run_id)
        .with_for_update()
    )
    run = result.scalar_one_or_none()
    if run is None:
        return None

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=replace_stale_after_seconds)
    current_token = getattr(run, "runner_token", None)
    heartbeat = getattr(run, "runner_heartbeat_at", None)
    lease_is_fresh = current_token and heartbeat and heartbeat >= stale_cutoff

    if current_token and current_token != runner_token and lease_is_fresh:
        return None

    run.runner_token = runner_token
    run.runner_started_at = now
    run.runner_heartbeat_at = now
    run.updated_at = now
    await session.commit()
    await session.refresh(run)
    return run


async def refresh_pipeline_run_lease(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    runner_token: str,
) -> bool:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None or getattr(run, "runner_token", None) != runner_token:
        return False
    run.runner_heartbeat_at = datetime.now(timezone.utc)
    run.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return True


async def release_pipeline_run_lease(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    runner_token: str,
) -> bool:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None or getattr(run, "runner_token", None) != runner_token:
        return False
    run.runner_token = None
    run.runner_heartbeat_at = None
    run.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(run)
    return True


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
    await refresh_pipeline_run_snapshot(session, run.id)
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
    await refresh_pipeline_run_snapshot(session, run.id)
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
    await refresh_pipeline_run_snapshot(session, gate.pipeline_run_id)
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
    await refresh_pipeline_run_snapshot(session, gate.pipeline_run_id)
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
    await refresh_pipeline_run_snapshot(session, evaluation.pipeline_run_id)
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


async def get_pipeline_run_snapshot(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> PipelineRunSnapshot | None:
    result = await session.execute(
        select(PipelineRunSnapshot).where(
            PipelineRunSnapshot.pipeline_run_id == pipeline_run_id
        )
    )
    return result.scalar_one_or_none()


async def get_pipeline_run_poll_state(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
):
    result = await session.execute(
        select(
            PipelineRun.id,
            PipelineRun.status,
            PipelineRun.next_event_seq,
            PipelineRun.updated_at,
        ).where(PipelineRun.id == pipeline_run_id)
    )
    row = result.one_or_none()
    if row is None:
        return None
    return row


async def refresh_pipeline_run_snapshot(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> PipelineRunSnapshot | None:
    run = await get_pipeline_run(session, pipeline_run_id)
    if run is None:
        return None
    tasks = await get_pipeline_run_tasks(session, pipeline_run_id)
    gates = await list_pipeline_human_gates(session, pipeline_run_id)
    evaluations = await list_pipeline_evaluations(session, pipeline_run_id)
    payload = build_pipeline_status_payload(
        project_id=str(run.project_id),
        pipeline_run_id=str(pipeline_run_id),
        run=run,
        tasks=tasks,
        gates=gates,
        evaluations=evaluations,
    )
    snapshot = await get_pipeline_run_snapshot(session, pipeline_run_id)
    now = datetime.now(timezone.utc)
    if snapshot is None:
        snapshot = PipelineRunSnapshot(
            pipeline_run_id=pipeline_run_id,
            project_id=run.project_id,
            status=payload["status"],
            failure_reason=payload.get("failure_reason"),
            snapshot_json=payload,
            version=1,
            updated_at=now,
        )
        session.add(snapshot)
    else:
        snapshot.project_id = run.project_id
        snapshot.status = payload["status"]
        snapshot.failure_reason = payload.get("failure_reason")
        snapshot.snapshot_json = payload
        snapshot.version = int(snapshot.version or 0) + 1
        snapshot.updated_at = now
    await session.commit()
    await session.refresh(snapshot)
    return snapshot


async def get_project_runtime_state(
    session: AsyncSession,
    project_id: uuid.UUID,
    scope: str,
) -> ProjectRuntimeState | None:
    result = await session.execute(
        select(ProjectRuntimeState).where(
            ProjectRuntimeState.project_id == project_id,
            ProjectRuntimeState.scope == scope,
        )
    )
    return result.scalar_one_or_none()


async def list_project_runtime_states(
    session: AsyncSession,
    project_id: uuid.UUID,
    scopes: list[str] | None = None,
) -> list[ProjectRuntimeState]:
    query = select(ProjectRuntimeState).where(
        ProjectRuntimeState.project_id == project_id
    )
    if scopes:
        query = query.where(ProjectRuntimeState.scope.in_(scopes))
    query = query.order_by(ProjectRuntimeState.scope.asc())
    result = await session.execute(query)
    return list(result.scalars().all())


async def get_project_runtime_state_versions(
    session: AsyncSession,
    project_id: uuid.UUID,
    scopes: list[str] | None = None,
) -> dict[str, int]:
    query = select(ProjectRuntimeState.scope, ProjectRuntimeState.version).where(
        ProjectRuntimeState.project_id == project_id
    )
    if scopes:
        query = query.where(ProjectRuntimeState.scope.in_(scopes))
    result = await session.execute(query)
    return {str(scope): int(version or 0) for scope, version in result.all()}


async def upsert_project_runtime_state(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    scope: str,
    state_json: dict | None,
) -> ProjectRuntimeState:
    existing = await get_project_runtime_state(session, project_id, scope)
    payload = state_json or {}
    now = datetime.now(timezone.utc)
    if existing is not None:
        if existing.state_json == payload:
            return existing
        existing.state_json = payload
        existing.version = int(existing.version or 0) + 1
        existing.updated_at = now
        await session.commit()
        await session.refresh(existing)
        return existing

    row = ProjectRuntimeState(
        project_id=project_id,
        scope=scope,
        state_json=payload,
        version=1,
        updated_at=now,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


def _pipeline_task_uuid_from_event(event: dict) -> uuid.UUID | None:
    raw_task_id = event.get("task_id")
    if not isinstance(raw_task_id, str):
        return None
    try:
        return uuid.UUID(raw_task_id)
    except ValueError:
        return None


async def create_pipeline_run_event(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    event: dict,
) -> PipelineRunEvent:
    rows = await create_pipeline_run_events(
        session,
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        events=[event],
    )
    return rows[0]


async def create_pipeline_run_events(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    events: list[dict],
) -> list[PipelineRunEvent]:
    if not events:
        return []

    seq_result = await session.execute(
        text(
            """
            UPDATE pipeline_runs
            SET next_event_seq = COALESCE(next_event_seq, 1) + :count,
                updated_at = NOW()
            WHERE id = :pipeline_run_id
            RETURNING next_event_seq - :count AS start_seq;
            """
        ),
        {
            "pipeline_run_id": pipeline_run_id,
            "count": len(events),
        },
    )
    start_seq = seq_result.scalar_one_or_none()
    if start_seq is None:
        raise ValueError(f"Unknown pipeline_run_id {pipeline_run_id}")

    rows: list[PipelineRunEvent] = []
    for index, event in enumerate(events):
        rows.append(
            PipelineRunEvent(
                project_id=project_id,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=_pipeline_task_uuid_from_event(event),
                seq=int(start_seq) + index,
                event_type=str(event.get("type") or "unknown"),
                stage=event.get("stage"),
                payload=event,
            )
        )
    session.add_all(rows)
    await session.commit()
    return rows


async def list_pipeline_run_events(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
    *,
    after_seq: int = 0,
    limit: int = 200,
) -> list[PipelineRunEvent]:
    result = await session.execute(
        select(PipelineRunEvent)
        .where(
            PipelineRunEvent.pipeline_run_id == pipeline_run_id,
            PipelineRunEvent.seq > after_seq,
        )
        .order_by(PipelineRunEvent.seq.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_latest_pipeline_run_event_seq(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> int:
    result = await session.execute(
        select(PipelineRun.next_event_seq).where(
            PipelineRun.id == pipeline_run_id
        )
    )
    next_seq = result.scalar()
    if next_seq is None:
        return 0
    return max(0, int(next_seq) - 1)


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
    async def _list_messages_once(active_session: AsyncSession) -> list[Message]:
        query = (
            select(Message)
            .where(Message.project_id == project_id)
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        if session_id:
            query = query.where(Message.session_id == session_id)
        result = await active_session.execute(query)
        return list(result.scalars().all())

    return await run_with_retry(session, _list_messages_once)


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
        depends_on_task_ids=depends_on_task_ids or [],
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
    await refresh_pipeline_run_snapshot(session, task.pipeline_run_id)
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

    claim_result = await session.execute(
        text(
            """
            WITH runnable AS (
              SELECT pt.id
              FROM pipeline_tasks AS pt
              WHERE pt.pipeline_run_id = :pipeline_run_id
                AND pt.status = 'pending'
                AND NOT EXISTS (
                  SELECT 1
                  FROM jsonb_array_elements_text(
                    CASE
                      WHEN pt.depends_on_task_ids IS NULL THEN '[]'::jsonb
                      WHEN jsonb_typeof(pt.depends_on_task_ids::jsonb) = 'array'
                        THEN pt.depends_on_task_ids::jsonb
                      ELSE '[]'::jsonb
                    END
                  ) AS dep(dep_id)
                  LEFT JOIN pipeline_tasks AS completed
                    ON completed.pipeline_run_id = pt.pipeline_run_id
                   AND completed.id::text = dep.dep_id
                   AND completed.status = 'completed'
                  WHERE completed.id IS NULL
                )
              ORDER BY pt.created_at ASC, pt.sequence_index ASC, pt.id ASC
              FOR UPDATE SKIP LOCKED
              LIMIT 1
            )
            UPDATE pipeline_tasks AS pt
            SET status = 'in_progress',
                claimed_at = NOW()
            FROM runnable
            WHERE pt.id = runnable.id
            RETURNING pt.id;
            """
        ),
        {
            "pipeline_run_id": pipeline_run_id,
        },
    )
    claimed_id = claim_result.scalar_one_or_none()
    if claimed_id is None:
        await session.rollback()
        return None

    await session.commit()
    refresh_result = await session.execute(
        select(PipelineTask).where(PipelineTask.id == claimed_id)
    )
    task = refresh_result.scalar_one_or_none()
    if task is not None:
        await refresh_pipeline_run_snapshot(session, task.pipeline_run_id)
    return task


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
    await refresh_pipeline_run_snapshot(session, task.pipeline_run_id)
    return task


async def reset_in_progress_tasks_for_run(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> int:
    from agents.db.models import PipelineTask

    result = await session.execute(
        update(PipelineTask)
        .where(
            PipelineTask.pipeline_run_id == pipeline_run_id,
            PipelineTask.status == "in_progress",
        )
        .values(status="pending", claimed_at=None)
    )
    await session.commit()
    if result.rowcount:
        await refresh_pipeline_run_snapshot(session, pipeline_run_id)
    return int(result.rowcount or 0)


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
    await refresh_pipeline_run_snapshot(session, pipeline_run_id)
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
    await refresh_pipeline_run_snapshot(session, task.pipeline_run_id)
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
    if result.rowcount:
        await refresh_pipeline_run_snapshot(session, pipeline_run_id)
    return int(result.rowcount or 0)
