import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agents import pipeline_orchestrator as orchestrator


@dataclass
class FakeTask:
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    project_id: uuid.UUID
    assigned_to: str
    created_by: str
    task_type: str
    description: str
    status: str = "pending"
    context: dict | None = None
    parent_task_id: uuid.UUID | None = None
    sequence_index: int = 0
    result_summary: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMemoryManager:
    agent_states = {
        "deployment": {"last_deploy_results": []},
        "testing": {"last_test_results": []},
    }

    def __init__(self, user_id: str, project_id: str):
        self.user_id = user_id
        self.project_id = project_id

    def get_agent_state(self, agent_name: str) -> dict:
        return self.agent_states.setdefault(agent_name, {})

    def get_plan(self) -> dict:
        return {"status": "ready"}

    def save_plan(self, plan: dict) -> None:
        self.saved_plan = plan


def _build_harness(monkeypatch, scenario: str):
    tasks: list[FakeTask] = []
    status_updates: list[str] = []
    created_counter = 0

    FakeMemoryManager.agent_states = {
        "deployment": {"last_deploy_results": []},
        "testing": {"last_test_results": []},
    }

    def add_task(
        *,
        pipeline_run_id: uuid.UUID,
        project_id: uuid.UUID,
        assigned_to: str,
        created_by: str,
        task_type: str,
        description: str,
        context: dict | None = None,
        parent_task_id: uuid.UUID | None = None,
        sequence_index: int = 0,
    ) -> FakeTask:
        nonlocal created_counter
        task = FakeTask(
            id=uuid.uuid4(),
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=assigned_to,
            created_by=created_by,
            task_type=task_type,
            description=description,
            context=context,
            parent_task_id=parent_task_id,
            sequence_index=sequence_index,
            created_at=datetime(2026, 4, 3, tzinfo=timezone.utc)
            + timedelta(seconds=created_counter),
        )
        created_counter += 1
        tasks.append(task)
        return task

    async def fake_create_pipeline_task(
        session,
        pipeline_run_id,
        project_id,
        assigned_to,
        created_by,
        task_type,
        description,
        context=None,
        parent_task_id=None,
        sequence_index=0,
    ):
        return add_task(
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=assigned_to,
            created_by=created_by,
            task_type=task_type,
            description=description,
            context=context,
            parent_task_id=parent_task_id,
            sequence_index=sequence_index,
        )

    async def fake_get_next_pending_task(session, pipeline_run_id):
        pending = [
            task for task in tasks if task.pipeline_run_id == pipeline_run_id and task.status == "pending"
        ]
        if not pending:
            return None
        return sorted(
            pending, key=lambda task: (task.created_at, task.sequence_index, task.id)
        )[0]

    async def fake_set_task_in_progress(session, task_id):
        for task in tasks:
            if task.id == task_id:
                task.status = "in_progress"
                return task
        return None

    async def fake_get_pipeline_task(session, task_id):
        for task in tasks:
            if task.id == task_id:
                return task
        return None

    async def fake_get_pipeline_run_tasks(session, pipeline_run_id):
        return sorted(
            [task for task in tasks if task.pipeline_run_id == pipeline_run_id],
            key=lambda task: (task.created_at, task.sequence_index, task.id),
        )

    def finish_task(task: FakeTask, status: str, summary: str) -> None:
        task.status = status
        task.result_summary = summary
        task.completed_at = task.created_at + timedelta(minutes=1)

    def append_result(agent_name: str, history_key: str, task: FakeTask, exit_code: int):
        FakeMemoryManager.agent_states.setdefault(agent_name, {}).setdefault(
            history_key, []
        ).append(
            {
                "pipeline_run_id": str(task.pipeline_run_id),
                "pipeline_task_id": str(task.id),
                "exit_code": exit_code,
            }
        )

    async def fake_stream_chat_with_intent(intent, session_id, user_message, project_id=None):
        current_task = next(task for task in tasks if task.status == "in_progress")
        yield {"type": "step", "content": current_task.task_type}

        if current_task.task_type == "coding.generate_contracts":
            finish_task(current_task, "completed", "Generated Solidity contracts.")
            add_task(
                pipeline_run_id=current_task.pipeline_run_id,
                project_id=current_task.project_id,
                assigned_to="testing",
                created_by="coding",
                task_type="testing.generate_tests",
                description="Generate Foundry tests for the generated contracts.",
                parent_task_id=current_task.id,
                sequence_index=0,
            )
        elif current_task.task_type == "testing.generate_tests":
            finish_task(current_task, "completed", "Generated Foundry tests.")
            add_task(
                pipeline_run_id=current_task.pipeline_run_id,
                project_id=current_task.project_id,
                assigned_to="testing",
                created_by="testing",
                task_type="testing.run_tests",
                description="Run the generated Foundry tests.",
                parent_task_id=current_task.id,
                sequence_index=0,
            )
        elif current_task.task_type == "testing.run_tests":
            append_result("testing", "last_test_results", current_task, 0)
            finish_task(current_task, "completed", "Foundry tests passed.")
            if scenario != "queue_drain_after_non_deploy":
                add_task(
                    pipeline_run_id=current_task.pipeline_run_id,
                    project_id=current_task.project_id,
                    assigned_to="deployment",
                    created_by="testing",
                    task_type="deployment.prepare_script",
                    description="Prepare the Foundry deployment script.",
                    parent_task_id=current_task.id,
                    sequence_index=0,
                )
        elif current_task.task_type == "deployment.prepare_script":
            finish_task(current_task, "completed", "Prepared deployment script.")
            add_task(
                pipeline_run_id=current_task.pipeline_run_id,
                project_id=current_task.project_id,
                assigned_to="deployment",
                created_by="deployment",
                task_type="deployment.execute_deploy",
                description="Execute the prepared deployment script on Fuji.",
                parent_task_id=current_task.id,
                sequence_index=0,
            )
        elif current_task.task_type == "deployment.execute_deploy":
            if scenario == "deploy_fail_no_followup":
                append_result("deployment", "last_deploy_results", current_task, 1)
                finish_task(current_task, "failed", "Forge deploy failed.")
            elif scenario == "deploy_fail_with_recovery":
                append_result("deployment", "last_deploy_results", current_task, 1)
                finish_task(current_task, "failed", "Initial deploy failed.")
                add_task(
                    pipeline_run_id=current_task.pipeline_run_id,
                    project_id=current_task.project_id,
                    assigned_to="deployment",
                    created_by="deployment",
                    task_type="deployment.retry_deploy",
                    description="Retry deployment with adjusted deployment parameters.",
                    parent_task_id=current_task.id,
                    sequence_index=0,
                )
            else:
                append_result("deployment", "last_deploy_results", current_task, 0)
                finish_task(current_task, "completed", "Deployment succeeded.")
        elif current_task.task_type == "deployment.retry_deploy":
            append_result("deployment", "last_deploy_results", current_task, 0)
            finish_task(current_task, "completed", "Retry deployment succeeded.")

        yield {"type": "done"}

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "create_pipeline_task", fake_create_pipeline_task)
    monkeypatch.setattr(orchestrator, "get_next_pending_task", fake_get_next_pending_task)
    monkeypatch.setattr(orchestrator, "set_task_in_progress", fake_set_task_in_progress)
    monkeypatch.setattr(orchestrator, "get_pipeline_task", fake_get_pipeline_task)
    monkeypatch.setattr(orchestrator, "get_pipeline_run_tasks", fake_get_pipeline_run_tasks)
    monkeypatch.setattr(orchestrator, "stream_chat_with_intent", fake_stream_chat_with_intent)
    monkeypatch.setattr(orchestrator, "MemoryManager", FakeMemoryManager)
    monkeypatch.setattr(orchestrator, "is_pipeline_cancelled", lambda pipeline_run_id: False)
    monkeypatch.setattr(orchestrator, "clear_cancellation", lambda pipeline_run_id: None)
    monkeypatch.setattr(
        orchestrator,
        "_update_plan_status",
        lambda project_id, user_id, status: status_updates.append(status),
    )

    return tasks, status_updates


def _run_pipeline(monkeypatch, scenario: str):
    tasks, status_updates = _build_harness(monkeypatch, scenario)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    async def collect():
        return [
            event
            async for event in orchestrator.run_autonomous_pipeline(
                project_id=project_id,
                user_id=user_id,
            )
        ]

    return asyncio.run(collect()), tasks, status_updates


def test_pipeline_fails_when_deploy_fails_without_followups(monkeypatch):
    events, tasks, status_updates = _run_pipeline(monkeypatch, "deploy_fail_no_followup")

    assert any(
        event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
        and event["task_status"] == "failed"
        for event in events
    )
    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "failed"


def test_pipeline_continues_after_failed_deploy_with_recovery_subtask(monkeypatch):
    events, tasks, status_updates = _run_pipeline(monkeypatch, "deploy_fail_with_recovery")

    failed_execute = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
    )
    retry_complete = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.retry_deploy"
    )
    retry_task = next(task for task in tasks if task.task_type == "deployment.retry_deploy")
    execute_task = next(task for task in tasks if task.task_type == "deployment.execute_deploy")

    assert failed_execute["task_status"] == "failed"
    assert retry_complete["task_status"] == "completed"
    assert retry_task.parent_task_id == execute_task.id
    assert any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "deployed"


def test_pipeline_completes_only_after_successful_tagged_terminal_deploy(monkeypatch):
    events, tasks, status_updates = _run_pipeline(monkeypatch, "success")

    deploy_complete = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
    )

    assert deploy_complete["result_exit_code"] == 0
    assert any(event["type"] == "pipeline_complete" for event in events)
    assert not any(event["type"] == "pipeline_error" for event in events)
    assert status_updates[-1] == "deployed"


def test_pipeline_fails_when_queue_drains_without_successful_deploy(monkeypatch):
    events, tasks, status_updates = _run_pipeline(monkeypatch, "queue_drain_after_non_deploy")

    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "failed"
    assert any(task.task_type == "testing.run_tests" for task in tasks)
