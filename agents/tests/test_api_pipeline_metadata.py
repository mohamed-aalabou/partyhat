import asyncio
import json
import uuid
from types import SimpleNamespace

import api


class FakeMemoryManager:
    def __init__(self, user_id: str, project_id: str | None):
        self.user_id = user_id
        self.project_id = project_id

    def list_deployments(self, limit: int = 20) -> list[dict]:
        return [
            {
                "pipeline_run_id": "run-123",
                "pipeline_task_id": "task-456",
                "plan_contract_id": "pc_partytoken",
                "exit_code": 1,
                "stdout": "large stdout",
                "trace_id": "trace-123",
            }
        ]

    def list_test_runs(self, limit: int = 20, include_output: bool = True) -> list[dict]:
        return [
            {
                "pipeline_run_id": "run-123",
                "pipeline_task_id": "task-789",
                "exit_code": 0,
                "output": "summary" if include_output else None,
                "stderr": "large stderr",
                "trace_id": "trace-456",
            }
        ]


async def _noop_ensure_project_context(project_id, user_id, session):
    return None


def _decode_stream_chunks(chunks):
    return "".join(
        chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in chunks
    )


def _parse_sse_events(body):
    events = []
    for chunk in body.split("\n\n"):
        if not chunk.strip() or chunk.startswith(":"):
            continue
        event_type = None
        payload = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if event_type is not None and payload is not None:
            events.append((event_type, payload))
    return events


def _make_state_stream_sources(snapshots):
    state = {"snapshot_index": 0, "version_index": 0}

    def fake_get_project_state_snapshot(*, user_id, project_id):
        assert user_id == "user-123"
        assert project_id == "project-123"
        idx = min(state["snapshot_index"], len(snapshots) - 1)
        state["snapshot_index"] += 1
        return snapshots[idx]

    def fake_get_project_state_versions(*, user_id, project_id, allow_recompute=True):
        assert user_id == "user-123"
        assert project_id == "project-123"
        assert allow_recompute is False
        idx = min(state["version_index"] + 1, len(snapshots) - 1)
        state["version_index"] += 1
        return snapshots[idx]["versions"]

    return state, fake_get_project_state_snapshot, fake_get_project_state_versions


def test_pipeline_status_returns_new_metadata(monkeypatch):
    captured = {}

    async def fake_get_pipeline_status(
        project_id,
        user_id,
        pipeline_run_id,
        **kwargs,
    ):
        captured.update(
            {
                "project_id": project_id,
                "user_id": user_id,
                "pipeline_run_id": pipeline_run_id,
                **kwargs,
            }
        )
        return {
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
            "status": "failed",
            "failure_reason": "Forge deploy failed.",
            "run": {"id": pipeline_run_id, "status": "failed"},
            "total_tasks": 1,
            "tasks": [
                {
                    "id": "task-1",
                    "assigned_to": "deployment",
                    "created_by": "deployment",
                    "task_type": "deployment.execute_deploy",
                    "description": "Deploy the contract.",
                    "parent_task_id": "task-0",
                    "sequence_index": 0,
                    "status": "failed",
                    "result_summary": "Forge deploy failed.",
                    "context": None,
                    "created_at": None,
                    "completed_at": None,
                }
            ],
            "gates": [],
            "evaluations": [],
        }

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_pipeline_status", fake_get_pipeline_status)

    result = asyncio.run(
        api.pipeline_status(
            project_id="project-123",
            pipeline_run_id="run-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert captured == {
        "project_id": "project-123",
        "user_id": "user-123",
        "pipeline_run_id": "run-123",
        "include_tasks": True,
        "include_gates": True,
        "include_evaluations": True,
    }
    assert result["status"] == "failed"
    assert result["failure_reason"] == "Forge deploy failed."
    assert result["run"]["id"] == "run-123"
    assert result["tasks"][0]["task_type"] == "deployment.execute_deploy"
    assert result["tasks"][0]["parent_task_id"] == "task-0"
    assert result["tasks"][0]["sequence_index"] == 0


def test_pipeline_status_summary_only_disables_detail_collections(monkeypatch):
    captured = {}

    async def fake_get_pipeline_status(project_id, user_id, pipeline_run_id, **kwargs):
        captured.update(kwargs)
        return {
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
            "status": "running",
            "failure_reason": None,
            "run": {"id": pipeline_run_id, "status": "running"},
            "total_tasks": 2,
            "tasks": [],
            "gates": [],
            "evaluations": [],
        }

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_pipeline_status", fake_get_pipeline_status)

    result = asyncio.run(
        api.pipeline_status(
            project_id="project-123",
            pipeline_run_id="run-123",
            summary_only=True,
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert captured == {
        "include_tasks": False,
        "include_gates": False,
        "include_evaluations": False,
    }
    assert result["total_tasks"] == 2


def test_run_pipeline_returns_detached_control_response(monkeypatch):
    class ReadyMemoryManager:
        def __init__(self, user_id: str, project_id: str | None):
            self.user_id = user_id
            self.project_id = project_id

        def get_plan(self) -> dict:
            return {"status": "ready"}

    async def fake_spawn_detached_pipeline_runner(*, project_id, user_id, **kwargs):
        assert project_id == "project-123"
        assert user_id == "user-123"
        return {
            "pipeline_run_id": "run-123",
            "started": True,
            "event": {"type": "pipeline_start", "seq": 1},
        }

    async def fake_get_pipeline_run_record(pipeline_run_id: str):
        assert pipeline_run_id == "run-123"
        return SimpleNamespace(status="running")

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "MemoryManager", ReadyMemoryManager)
    monkeypatch.setattr(
        api,
        "spawn_detached_pipeline_runner",
        fake_spawn_detached_pipeline_runner,
    )
    monkeypatch.setattr(api, "get_pipeline_run_record", fake_get_pipeline_run_record)

    result = asyncio.run(
        api.run_pipeline(
            request=api.PipelineRunRequest(
                project_id="project-123",
                user_id="user-123",
            ),
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert result == {
        "pipeline_run_id": "run-123",
        "status": "running",
        "events_url": "/pipeline/events?project_id=project-123&pipeline_run_id=run-123",
        "status_url": "/pipeline/status?project_id=project-123&pipeline_run_id=run-123",
    }


def test_resume_pipeline_rejects_pending_gate(monkeypatch):
    project_id = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())

    async def fake_get_pipeline_run(session, run_uuid):
        assert str(run_uuid) == pipeline_run_id
        return SimpleNamespace(project_id=uuid.UUID(project_id), status="waiting_for_approval")

    async def fake_list_pipeline_human_gates(session, run_uuid):
        assert str(run_uuid) == pipeline_run_id
        return [SimpleNamespace(status="pending")]

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "db_get_pipeline_run", fake_get_pipeline_run)
    monkeypatch.setattr(api, "list_pipeline_human_gates", fake_list_pipeline_human_gates)

    try:
        asyncio.run(
            api.resume_pipeline(
                request=api.PipelineResumeRequest(
                    project_id=project_id,
                    pipeline_run_id=pipeline_run_id,
                ),
                ctx=api.RequestContext(project_id=project_id, user_id="user-123"),
                session=None,
            )
        )
    except api.HTTPException as exc:
        assert exc.status_code == 409
        assert "waiting for approval" in str(exc.detail)
    else:
        raise AssertionError("Expected HTTPException for pending gate")


def test_pipeline_events_replays_backlog(monkeypatch):
    project_id = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())
    serialized_events = [
        {
            "seq": 1,
            "type": "pipeline_start",
            "pipeline_run_id": pipeline_run_id,
        },
        {
            "seq": 2,
            "type": "pipeline_complete",
            "pipeline_run_id": pipeline_run_id,
        },
    ]
    events_calls = 0

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def fake_get_pipeline_run(session, run_uuid):
        assert str(run_uuid) == pipeline_run_id
        return SimpleNamespace(project_id=uuid.UUID(project_id), status="running")

    async def fake_list_serialized_pipeline_run_events(run_id: str, *, after_seq: int = 0):
        nonlocal events_calls
        events_calls += 1
        if events_calls == 1:
            assert run_id == pipeline_run_id
            assert after_seq == 0
            return list(serialized_events)
        return []

    async def fake_get_pipeline_run_poll_record(run_id: str):
        assert run_id == pipeline_run_id
        return SimpleNamespace(status="completed", next_event_seq=3)

    async def scenario():
        response = await api.pipeline_events(
            request=FakeRequest(),
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            after_seq=0,
            ctx=api.RequestContext(project_id=project_id, user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return "".join(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "db_get_pipeline_run", fake_get_pipeline_run)
    monkeypatch.setattr(
        api,
        "list_serialized_pipeline_run_events",
        fake_list_serialized_pipeline_run_events,
    )
    monkeypatch.setattr(
        api,
        "get_pipeline_run_poll_record",
        fake_get_pipeline_run_poll_record,
    )

    body = asyncio.run(scenario())

    assert "id: 1" in body
    assert "event: pipeline_start" in body
    assert "id: 2" in body
    assert "event: pipeline_complete" in body


def test_state_stream_emits_initial_snapshot_for_empty_project(monkeypatch):
    snapshot = {
        "plan": {"plan": None, "status": None, "version": "plan-v1"},
        "code": {"artifacts": [], "version": "code-v1"},
        "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
        "versions": {
            "plan": "plan-v1",
            "code": "code-v1",
            "deployment": "deployment-v1",
        },
    }
    state = {"calls": 0}

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return state["calls"] >= 1

    def fake_get_project_state_snapshot(*, user_id, project_id):
        assert user_id == "user-123"
        assert project_id == "project-123"
        state["calls"] += 1
        return snapshot

    async def fake_sleep(_seconds):
        return None

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())

    assert "event: state_snapshot" in body
    assert '"project_id": "project-123"' in body
    assert '"plan": null' in body
    assert '"artifacts": []' in body
    assert '"last_deploy_results": []' in body


def test_state_stream_emits_plan_update_once_when_plan_changes(monkeypatch):
    snapshots = [
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {
                "plan": {"project_name": "PartyToken", "status": "draft"},
                "status": "draft",
                "version": "plan-v2",
            },
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v2",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {
                "plan": {"project_name": "PartyToken", "status": "draft"},
                "status": "draft",
                "version": "plan-v2",
            },
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v2",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
    ]
    state, fake_get_project_state_snapshot, fake_get_project_state_versions = (
        _make_state_stream_sources(snapshots)
    )

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return state["version_index"] >= len(snapshots) - 1

    async def fake_sleep(_seconds):
        return None

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api, "get_project_state_versions", fake_get_project_state_versions)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())
    events = _parse_sse_events(body)
    plan_event = next(payload for event_type, payload in events if event_type == "plan_updated")

    assert body.count("event: plan_updated") == 1
    assert plan_event["plan"]["plan"]["project_name"] == "PartyToken"
    assert plan_event["code"] == {"artifacts": [], "version": "code-v1"}
    assert plan_event["deployment"] == {
        "last_deploy_results": [],
        "version": "deployment-v1",
    }


def test_state_stream_emits_code_update_once_when_artifacts_change(monkeypatch):
    snapshots = [
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {
                "artifacts": [{"path": "contracts/PartyToken.sol"}],
                "version": "code-v2",
            },
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v2",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {
                "artifacts": [{"path": "contracts/PartyToken.sol"}],
                "version": "code-v2",
            },
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v2",
                "deployment": "deployment-v1",
            },
        },
    ]
    state, fake_get_project_state_snapshot, fake_get_project_state_versions = (
        _make_state_stream_sources(snapshots)
    )

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return state["version_index"] >= len(snapshots) - 1

    async def fake_sleep(_seconds):
        return None

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api, "get_project_state_versions", fake_get_project_state_versions)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())
    events = _parse_sse_events(body)
    code_event = next(payload for event_type, payload in events if event_type == "code_updated")

    assert body.count("event: code_updated") == 1
    assert code_event["plan"] == {"plan": None, "status": None, "version": "plan-v1"}
    assert code_event["code"] == {
        "artifacts": [{"path": "contracts/PartyToken.sol"}],
        "version": "code-v2",
    }
    assert code_event["deployment"] == {
        "last_deploy_results": [],
        "version": "deployment-v1",
    }


def test_state_stream_emits_deployment_update_once_when_deployments_change(monkeypatch):
    snapshots = [
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {
                "last_deploy_results": [{"tx_hash": "0x123", "status": "success"}],
                "version": "deployment-v2",
            },
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v2",
            },
        },
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {
                "last_deploy_results": [{"tx_hash": "0x123", "status": "success"}],
                "version": "deployment-v2",
            },
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v2",
            },
        },
    ]
    state, fake_get_project_state_snapshot, fake_get_project_state_versions = (
        _make_state_stream_sources(snapshots)
    )

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return state["version_index"] >= len(snapshots) - 1

    async def fake_sleep(_seconds):
        return None

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api, "get_project_state_versions", fake_get_project_state_versions)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())
    events = _parse_sse_events(body)
    deployment_event = next(
        payload for event_type, payload in events if event_type == "deployment_updated"
    )

    assert body.count("event: deployment_updated") == 1
    assert deployment_event["plan"] == {
        "plan": None,
        "status": None,
        "version": "plan-v1",
    }
    assert deployment_event["code"] == {"artifacts": [], "version": "code-v1"}
    assert deployment_event["deployment"] == {
        "last_deploy_results": [{"tx_hash": "0x123", "status": "success"}],
        "version": "deployment-v2",
    }


def test_state_stream_emits_full_snapshot_payload_for_each_changed_resource(monkeypatch):
    snapshots = [
        {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        },
        {
            "plan": {
                "plan": {"project_name": "PartyToken", "status": "ready"},
                "status": "ready",
                "version": "plan-v2",
            },
            "code": {
                "artifacts": [{"path": "contracts/PartyToken.sol"}],
                "version": "code-v2",
            },
            "deployment": {
                "last_deploy_results": [{"tx_hash": "0xabc", "status": "success"}],
                "version": "deployment-v2",
            },
            "versions": {
                "plan": "plan-v2",
                "code": "code-v2",
                "deployment": "deployment-v2",
            },
        },
        {
            "plan": {
                "plan": {"project_name": "PartyToken", "status": "ready"},
                "status": "ready",
                "version": "plan-v2",
            },
            "code": {
                "artifacts": [{"path": "contracts/PartyToken.sol"}],
                "version": "code-v2",
            },
            "deployment": {
                "last_deploy_results": [{"tx_hash": "0xabc", "status": "success"}],
                "version": "deployment-v2",
            },
            "versions": {
                "plan": "plan-v2",
                "code": "code-v2",
                "deployment": "deployment-v2",
            },
        },
    ]
    state, fake_get_project_state_snapshot, fake_get_project_state_versions = (
        _make_state_stream_sources(snapshots)
    )

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return state["version_index"] >= len(snapshots) - 1

    async def fake_sleep(_seconds):
        return None

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api, "get_project_state_versions", fake_get_project_state_versions)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())
    events = _parse_sse_events(body)
    update_events = [
        (event_type, payload)
        for event_type, payload in events
        if event_type in {"plan_updated", "code_updated", "deployment_updated"}
    ]

    assert [event_type for event_type, _ in update_events] == [
        "plan_updated",
        "code_updated",
        "deployment_updated",
    ]
    assert all(
        payload["plan"] == snapshots[1]["plan"]
        and payload["code"] == snapshots[1]["code"]
        and payload["deployment"] == snapshots[1]["deployment"]
        for _, payload in update_events
    )
    assert len({payload["emitted_at"] for _, payload in update_events}) == 1


def test_state_stream_stops_cleanly_on_disconnect_and_validates_context(monkeypatch):
    calls = {"ensure": [], "snapshots": 0}

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return calls["snapshots"] >= 1

    async def fake_ensure_project_context(project_id, user_id, session):
        calls["ensure"].append((project_id, user_id, session))
        return None

    def fake_get_project_state_snapshot(*, user_id, project_id):
        calls["snapshots"] += 1
        assert user_id == "user-123"
        assert project_id == "project-123"
        return {
            "plan": {"plan": None, "status": None, "version": "plan-v1"},
            "code": {"artifacts": [], "version": "code-v1"},
            "deployment": {"last_deploy_results": [], "version": "deployment-v1"},
            "versions": {
                "plan": "plan-v1",
                "code": "code-v1",
                "deployment": "deployment-v1",
            },
        }

    async def fake_sleep(_seconds):
        raise AssertionError("sleep should not be reached after disconnect")

    async def scenario():
        response = await api.state_stream(
            request=FakeRequest(),
            project_id="default",
            user_id="default",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return _decode_stream_chunks(chunks)

    monkeypatch.setattr(api, "ensure_project_context", fake_ensure_project_context)
    monkeypatch.setattr(api, "get_project_state_snapshot", fake_get_project_state_snapshot)
    monkeypatch.setattr(api.asyncio, "sleep", fake_sleep)

    body = asyncio.run(scenario())

    assert calls["ensure"] == [("project-123", "user-123", None)]
    assert calls["snapshots"] == 1
    assert body.count("event: state_snapshot") == 1


def test_get_current_deployment_includes_pipeline_tags(monkeypatch):
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "MemoryManager", FakeMemoryManager)

    response = asyncio.run(
        api.get_current_deployment(
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert response.last_deploy_results[0]["pipeline_run_id"] == "run-123"
    assert response.last_deploy_results[0]["pipeline_task_id"] == "task-456"
    assert response.last_deploy_results[0]["plan_contract_id"] == "pc_partytoken"
    assert response.last_deploy_results[0]["trace_id"] == "trace-123"
    assert "stdout" not in response.last_deploy_results[0]


def test_get_current_test_results_includes_pipeline_tags(monkeypatch):
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "MemoryManager", FakeMemoryManager)

    response = asyncio.run(
        api.get_current_test_results(
            project_id="project-123",
            user_id="user-123",
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert response.last_test_results[0]["pipeline_run_id"] == "run-123"
    assert response.last_test_results[0]["pipeline_task_id"] == "task-789"
    assert response.last_test_results[0]["trace_id"] == "trace-456"
    assert "output" not in response.last_test_results[0]
    assert "stderr" not in response.last_test_results[0]


def test_get_current_test_results_can_include_output(monkeypatch):
    monkeypatch.setattr(api, "ensure_project_context", _noop_ensure_project_context)
    monkeypatch.setattr(api, "MemoryManager", FakeMemoryManager)

    response = asyncio.run(
        api.get_current_test_results(
            project_id="project-123",
            user_id="user-123",
            include_output=True,
            ctx=api.RequestContext(project_id="project-123", user_id="user-123"),
            session=None,
        )
    )

    assert response.last_test_results[0]["output"] == "summary"
