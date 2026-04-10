import asyncio
import uuid
from types import SimpleNamespace

from agents import pipeline_runtime as runtime


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_spawn_detached_pipeline_runner_persists_events_for_new_run(monkeypatch):
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())
    persisted_events = []
    acquired_runs = []
    released_runs = []
    seq = 0

    async def fake_run_autonomous_pipeline(project_id, user_id, pipeline_run_id=None):
        assert project_id == project_id_value
        assert user_id == user_id_value
        assert pipeline_run_id is None
        yield {
            "type": "pipeline_start",
            "pipeline_run_id": active_run_id,
            "project_id": project_id,
        }
        yield {
            "type": "stage_start",
            "stage": "coding",
            "task_id": "11111111-1111-1111-1111-111111111111",
        }
        yield {
            "type": "pipeline_complete",
            "pipeline_run_id": active_run_id,
            "tasks_completed": 1,
        }

    async def fake_acquire_pipeline_run_lease(
        session,
        pipeline_run_id,
        runner_token,
        *,
        replace_stale_after_seconds,
    ):
        acquired_runs.append((str(pipeline_run_id), runner_token, replace_stale_after_seconds))
        return SimpleNamespace(id=pipeline_run_id, runner_token=runner_token)

    async def fake_create_pipeline_run_event(session, *, project_id, pipeline_run_id, event):
        nonlocal seq
        seq += 1
        persisted_events.append(
            {
                "project_id": str(project_id),
                "pipeline_run_id": str(pipeline_run_id),
                "event": dict(event),
                "seq": seq,
            }
        )
        return SimpleNamespace(seq=seq, payload=event)

    async def fake_release_pipeline_run_lease(session, pipeline_run_id, runner_token):
        released_runs.append((str(pipeline_run_id), runner_token))
        return True

    async def scenario():
        runtime._background_pipeline_tasks.clear()
        result = await runtime.spawn_detached_pipeline_runner(
            project_id=project_id_value,
            user_id=user_id_value,
        )
        for _ in range(20):
            if len(persisted_events) == 3 and released_runs:
                break
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(runtime, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(runtime, "run_autonomous_pipeline", fake_run_autonomous_pipeline)
    monkeypatch.setattr(
        runtime,
        "acquire_pipeline_run_lease",
        fake_acquire_pipeline_run_lease,
    )
    monkeypatch.setattr(runtime, "create_pipeline_run_event", fake_create_pipeline_run_event)
    monkeypatch.setattr(
        runtime,
        "release_pipeline_run_lease",
        fake_release_pipeline_run_lease,
    )

    project_id_value = project_id
    user_id_value = user_id
    active_run_id = pipeline_run_id
    result = asyncio.run(scenario())

    assert result["pipeline_run_id"] == pipeline_run_id
    assert result["started"] is True
    assert [row["seq"] for row in persisted_events] == [1, 2, 3]
    assert persisted_events[0]["event"]["type"] == "pipeline_start"
    assert persisted_events[-1]["event"]["type"] == "pipeline_complete"
    assert acquired_runs[0][0] == pipeline_run_id
    assert released_runs[0][0] == pipeline_run_id


def test_spawn_detached_pipeline_runner_returns_not_started_when_lease_is_busy(monkeypatch):
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())
    generator_called = False

    async def fake_run_autonomous_pipeline(project_id, user_id, pipeline_run_id=None):
        nonlocal generator_called
        generator_called = True
        if False:
            yield {}

    async def fake_acquire_pipeline_run_lease(
        session,
        pipeline_run_id,
        runner_token,
        *,
        replace_stale_after_seconds,
    ):
        return None

    async def scenario():
        runtime._background_pipeline_tasks.clear()
        return await runtime.spawn_detached_pipeline_runner(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
        )

    monkeypatch.setattr(runtime, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(runtime, "run_autonomous_pipeline", fake_run_autonomous_pipeline)
    monkeypatch.setattr(
        runtime,
        "acquire_pipeline_run_lease",
        fake_acquire_pipeline_run_lease,
    )

    result = asyncio.run(scenario())

    assert result == {
        "pipeline_run_id": pipeline_run_id,
        "started": False,
        "event": None,
    }
    assert generator_called is False


def test_spawn_detached_pipeline_runner_resets_in_progress_tasks_for_resume(monkeypatch):
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())
    reset_calls = []

    async def fake_run_autonomous_pipeline(project_id, user_id, pipeline_run_id=None):
        yield {
            "type": "pipeline_resumed",
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
        }
        yield {
            "type": "pipeline_complete",
            "pipeline_run_id": pipeline_run_id,
        }

    async def fake_acquire_pipeline_run_lease(
        session,
        pipeline_run_id,
        runner_token,
        *,
        replace_stale_after_seconds,
    ):
        return SimpleNamespace(id=pipeline_run_id, runner_token=runner_token)

    async def fake_reset_in_progress_tasks_for_run(session, pipeline_run_id):
        reset_calls.append(str(pipeline_run_id))
        return 1

    async def fake_create_pipeline_run_event(session, *, project_id, pipeline_run_id, event):
        return SimpleNamespace(seq=1, payload=event)

    async def fake_release_pipeline_run_lease(session, pipeline_run_id, runner_token):
        return True

    async def scenario():
        runtime._background_pipeline_tasks.clear()
        result = await runtime.spawn_detached_pipeline_runner(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            reset_in_progress=True,
        )
        for _ in range(10):
            if reset_calls:
                break
            await asyncio.sleep(0)
        return result

    monkeypatch.setattr(runtime, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(runtime, "run_autonomous_pipeline", fake_run_autonomous_pipeline)
    monkeypatch.setattr(
        runtime,
        "acquire_pipeline_run_lease",
        fake_acquire_pipeline_run_lease,
    )
    monkeypatch.setattr(
        runtime,
        "reset_in_progress_tasks_for_run",
        fake_reset_in_progress_tasks_for_run,
    )
    monkeypatch.setattr(runtime, "create_pipeline_run_event", fake_create_pipeline_run_event)
    monkeypatch.setattr(
        runtime,
        "release_pipeline_run_lease",
        fake_release_pipeline_run_lease,
    )

    result = asyncio.run(scenario())

    assert result["pipeline_run_id"] == pipeline_run_id
    assert result["started"] is True
    assert reset_calls == [pipeline_run_id]
