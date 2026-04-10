import asyncio
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

from agents.db import async_session_factory
from agents.db.crud import (
    acquire_pipeline_run_lease,
    create_pipeline_run_events,
    get_pipeline_run,
    get_pipeline_run_poll_state,
    list_pipeline_run_events,
    refresh_pipeline_run_lease,
    release_pipeline_run_lease,
    reset_in_progress_tasks_for_run,
    update_pipeline_run,
)
from agents.pipeline_orchestrator import run_autonomous_pipeline

LEASE_HEARTBEAT_INTERVAL_S = 5.0
LEASE_STALE_AFTER_SECONDS = 900
EVENT_PAGE_SIZE = 200
TERMINAL_RUN_STATUSES = {"cancelled", "completed", "failed"}
EVENT_BUFFER_MAX = 16
EVENT_BUFFER_WINDOW_S = 0.25

_background_pipeline_tasks: set[asyncio.Task[Any]] = set()


def build_pipeline_control_response(
    project_id: str,
    pipeline_run_id: str,
    status: str,
) -> dict[str, Any]:
    return {
        "pipeline_run_id": pipeline_run_id,
        "status": status,
        "events_url": (
            f"/pipeline/events?project_id={project_id}&pipeline_run_id={pipeline_run_id}"
        ),
        "status_url": (
            f"/pipeline/status?project_id={project_id}&pipeline_run_id={pipeline_run_id}"
        ),
    }


def _track_background_task(task: asyncio.Task[Any]) -> None:
    _background_pipeline_tasks.add(task)
    task.add_done_callback(_background_pipeline_tasks.discard)


async def _persist_pipeline_events(
    project_id: str,
    pipeline_run_id: str,
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not events:
        return []
    async with async_session_factory() as session:
        rows = await create_pipeline_run_events(
            session,
            project_id=uuid.UUID(project_id),
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            events=events,
        )

    persisted: list[dict[str, Any]] = []
    for row, event in zip(rows, events, strict=False):
        payload = dict(event)
        payload["seq"] = row.seq
        persisted.append(payload)
    return persisted


async def list_serialized_pipeline_run_events(
    pipeline_run_id: str,
    *,
    after_seq: int = 0,
    limit: int = EVENT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    async with async_session_factory() as session:
        rows = await list_pipeline_run_events(
            session,
            uuid.UUID(pipeline_run_id),
            after_seq=after_seq,
            limit=limit,
        )

    events: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        payload["seq"] = row.seq
        events.append(payload)
    return events


async def get_pipeline_run_record(pipeline_run_id: str):
    async with async_session_factory() as session:
        return await get_pipeline_run(session, uuid.UUID(pipeline_run_id))


async def get_pipeline_run_poll_record(pipeline_run_id: str):
    async with async_session_factory() as session:
        return await get_pipeline_run_poll_state(session, uuid.UUID(pipeline_run_id))


async def _lease_heartbeat_loop(
    pipeline_run_id: str,
    runner_token: str,
) -> None:
    while True:
        await asyncio.sleep(LEASE_HEARTBEAT_INTERVAL_S)
        async with async_session_factory() as session:
            refreshed = await refresh_pipeline_run_lease(
                session,
                uuid.UUID(pipeline_run_id),
                runner_token,
            )
        if not refreshed:
            return


async def _mark_pipeline_failed(
    pipeline_run_id: str,
    error: str,
) -> None:
    async with async_session_factory() as session:
        run = await get_pipeline_run(session, uuid.UUID(pipeline_run_id))
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return
        await update_pipeline_run(
            session,
            uuid.UUID(pipeline_run_id),
            status="failed",
            completed_at=datetime.now(timezone.utc),
            failure_class="unknown",
            failure_reason=error,
        )


async def _run_pipeline_background(
    *,
    project_id: str,
    user_id: str,
    pipeline_run_id: str | None,
    runner_token: str,
    start_future: asyncio.Future[dict[str, Any]],
    lease_preacquired: bool,
) -> None:
    current_run_id = pipeline_run_id
    lease_acquired = lease_preacquired
    heartbeat_task: asyncio.Task[Any] | None = None

    if lease_preacquired and current_run_id is not None:
        heartbeat_task = asyncio.create_task(
            _lease_heartbeat_loop(current_run_id, runner_token)
        )

    event_buffer: list[dict[str, Any]] = []
    last_event_flush = asyncio.get_running_loop().time()

    async def _flush_event_buffer() -> list[dict[str, Any]]:
        nonlocal event_buffer, last_event_flush
        if current_run_id is None or not event_buffer:
            return []
        buffered = event_buffer
        event_buffer = []
        persisted = await _persist_pipeline_events(
            project_id,
            current_run_id,
            buffered,
        )
        last_event_flush = asyncio.get_running_loop().time()
        return persisted

    try:
        async for event in run_autonomous_pipeline(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
        ):
            event_run_id = event.get("pipeline_run_id")
            if isinstance(event_run_id, str) and event_run_id:
                current_run_id = event_run_id

            if current_run_id is None:
                raise RuntimeError(
                    "Detached pipeline runner did not receive a pipeline_run_id."
                )

            if not lease_acquired:
                async with async_session_factory() as session:
                    acquired = await acquire_pipeline_run_lease(
                        session,
                        uuid.UUID(current_run_id),
                        runner_token,
                        replace_stale_after_seconds=LEASE_STALE_AFTER_SECONDS,
                    )
                if acquired is None:
                    raise RuntimeError(
                        f"Could not acquire pipeline run lease for {current_run_id}."
                    )
                lease_acquired = True
                heartbeat_task = asyncio.create_task(
                    _lease_heartbeat_loop(current_run_id, runner_token)
                )

            event_buffer.append(event)
            should_flush = (
                not start_future.done()
                or len(event_buffer) >= EVENT_BUFFER_MAX
                or (asyncio.get_running_loop().time() - last_event_flush)
                >= EVENT_BUFFER_WINDOW_S
                or event.get("type") in {"stage_start", "stage_end", "pipeline_error"}
            )
            persisted_events: list[dict[str, Any]] = []
            if should_flush:
                persisted_events = await _flush_event_buffer()
            if not start_future.done():
                start_future.set_result(
                    {
                        "pipeline_run_id": current_run_id,
                        "started": True,
                        "event": persisted_events[0] if persisted_events else None,
                    }
                )

        trailing_events = await _flush_event_buffer()

        if not start_future.done():
            if current_run_id is None:
                start_future.set_exception(
                    RuntimeError("Detached pipeline runner exited before start.")
                )
            else:
                start_future.set_result(
                    {
                        "pipeline_run_id": current_run_id,
                        "started": True,
                        "event": trailing_events[0] if trailing_events else None,
                    }
                )
    except Exception as exc:
        if current_run_id is not None:
            error_message = f"Detached pipeline runner failed: {exc}"
            await _mark_pipeline_failed(current_run_id, error_message)
            with suppress(Exception):
                persisted_error = await _persist_pipeline_events(
                    project_id,
                    current_run_id,
                    [
                        {
                            "type": "pipeline_error",
                            "pipeline_run_id": current_run_id,
                            "error": error_message,
                        }
                    ],
                )
                if not start_future.done():
                    start_future.set_result(
                        {
                            "pipeline_run_id": current_run_id,
                            "started": True,
                            "event": persisted_error[0] if persisted_error else None,
                        }
                    )
        if not start_future.done():
            start_future.set_exception(exc)
        raise
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if lease_acquired and current_run_id is not None:
            with suppress(Exception):
                async with async_session_factory() as session:
                    await release_pipeline_run_lease(
                        session,
                        uuid.UUID(current_run_id),
                        runner_token,
                    )


async def spawn_detached_pipeline_runner(
    *,
    project_id: str,
    user_id: str,
    pipeline_run_id: str | None = None,
    reset_in_progress: bool = False,
) -> dict[str, Any]:
    runner_token = str(uuid.uuid4())
    lease_preacquired = False

    if pipeline_run_id is not None:
        async with async_session_factory() as session:
            acquired = await acquire_pipeline_run_lease(
                session,
                uuid.UUID(pipeline_run_id),
                runner_token,
                replace_stale_after_seconds=LEASE_STALE_AFTER_SECONDS,
            )
            if acquired is None:
                return {
                    "pipeline_run_id": pipeline_run_id,
                    "started": False,
                    "event": None,
                }
            if reset_in_progress:
                await reset_in_progress_tasks_for_run(
                    session,
                    uuid.UUID(pipeline_run_id),
                )
        lease_preacquired = True

    loop = asyncio.get_running_loop()
    start_future: asyncio.Future[dict[str, Any]] = loop.create_future()
    task = asyncio.create_task(
        _run_pipeline_background(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            runner_token=runner_token,
            start_future=start_future,
            lease_preacquired=lease_preacquired,
        )
    )
    _track_background_task(task)
    return await start_future
