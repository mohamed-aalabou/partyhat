import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agents.task_tools import (
    CompleteTaskInput,
    NextTask,
    _normalize_next_tasks,
    complete_task_and_create_next,
)
from agents.db.crud import pending_task_sort_key


def test_normalize_next_tasks_defaults_parent_and_sequence():
    current_task_id = str(uuid.uuid4())
    explicit_parent = str(uuid.uuid4())
    pipeline_run_id = str(uuid.uuid4())
    current_task = SimpleNamespace(
        id=uuid.UUID(current_task_id),
        pipeline_run_id=uuid.UUID(pipeline_run_id),
        task_type="coding.generate_contracts",
        assigned_to="coding",
        artifact_revision=0,
        context={
            "artifact_revision": 0,
            "plan_summary": {"project_name": "PartyToken"},
            "input_artifacts": {"coding": [], "testing": [], "deployment": []},
        },
    )
    next_tasks = [
        NextTask(
            assigned_to="testing",
            task_type="testing.generate_tests",
            description="Generate Foundry tests.",
        ),
        NextTask(
            assigned_to="deployment",
            task_type="deployment.execute_deploy",
            description="Deploy with the prepared script.",
            parent_task_id=explicit_parent,
            sequence_index=7,
        ),
    ]

    from agents import task_tools

    task_tools._get_artifact_snapshot = lambda project_id, user_id: {
        "coding": [],
        "testing": [],
        "deployment": [],
    }
    task_tools._get_next_retry_attempt_sync = (
        lambda pipeline_run_id, retry_budget_key: 0
    )
    task_tools._get_plan_summary = lambda project_id, user_id: {"project_name": "PartyToken"}
    task_tools._update_revision_pointer = lambda project_id, user_id, revision: None

    normalized = _normalize_next_tasks(
        current_task,
        next_tasks,
        project_id="project-id",
        user_id="user-id",
        task_status="completed",
        result_summary="Generated contracts.",
    )

    assert normalized[0]["parent_task_id"] == current_task_id
    assert normalized[0]["sequence_index"] == 0
    assert normalized[0]["artifact_revision"] == 1
    assert normalized[0]["retry_budget_key"] == "testing.generate"
    assert normalized[0]["retry_attempt"] == 0
    assert normalized[0]["context"]["plan_summary"]["project_name"] == "PartyToken"
    assert normalized[1]["parent_task_id"] == explicit_parent
    assert normalized[1]["sequence_index"] == 7
    assert normalized[1]["retry_budget_key"] == "deployment.execute"
    assert normalized[1]["retry_attempt"] == 0


def test_complete_task_rejects_non_terminal_empty_completion(monkeypatch):
    current_task_id = uuid.uuid4()
    monkeypatch.setattr(
        "agents.task_tools._db_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "agents.task_tools._get_context",
        lambda: ("project-id", "user-id", "pipeline-run-id", str(current_task_id)),
    )
    monkeypatch.setattr(
        "agents.task_tools._get_current_task_sync",
        lambda pipeline_run_id, pipeline_task_id=None: SimpleNamespace(
            id=current_task_id,
            task_type="testing.generate_tests",
            assigned_to="testing",
        ),
    )

    result = complete_task_and_create_next.func(
        CompleteTaskInput(
            task_status="completed",
            result_summary="Generated tests.",
            next_tasks=[],
        )
    )

    assert "error" in result
    assert "terminal deployment" in result["error"]


def test_complete_task_passes_failed_status_and_normalized_subtasks(monkeypatch):
    current_task_id = uuid.uuid4()
    pipeline_run_id = str(uuid.uuid4())
    captured = {}

    monkeypatch.setattr(
        "agents.task_tools._db_available",
        lambda: True,
    )
    monkeypatch.setattr(
        "agents.task_tools._get_context",
        lambda: ("project-id", "user-id", pipeline_run_id, str(current_task_id)),
    )
    monkeypatch.setattr(
        "agents.task_tools._get_current_task_sync",
        lambda pipeline_run_id, pipeline_task_id=None: SimpleNamespace(
            id=current_task_id,
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            task_type="deployment.execute_deploy",
            assigned_to="deployment",
            artifact_revision=1,
            context={
                "artifact_revision": 1,
                "plan_summary": {"project_name": "PartyToken"},
                "input_artifacts": {"coding": [], "testing": [], "deployment": []},
            },
        ),
    )
    monkeypatch.setattr(
        "agents.task_tools._get_artifact_snapshot",
        lambda project_id, user_id: {"coding": [], "testing": [], "deployment": []},
    )
    monkeypatch.setattr(
        "agents.task_tools._get_plan_summary",
        lambda project_id, user_id: {"project_name": "PartyToken"},
    )
    monkeypatch.setattr(
        "agents.task_tools._update_revision_pointer",
        lambda project_id, user_id, revision: None,
    )
    monkeypatch.setattr(
        "agents.task_tools._get_next_retry_attempt_sync",
        lambda pipeline_run_id, retry_budget_key: 2,
    )

    def fake_complete_and_create_sync(**kwargs):
        captured.update(kwargs)
        return [
            {
                "id": str(uuid.uuid4()),
                "assigned_to": "coding",
                "task_type": "coding.generate_contracts",
                "description": "Fix the deployment failure.",
                "parent_task_id": kwargs["next_tasks"][0]["parent_task_id"],
                "sequence_index": kwargs["next_tasks"][0]["sequence_index"],
            }
        ]

    monkeypatch.setattr(
        "agents.task_tools._complete_and_create_sync",
        fake_complete_and_create_sync,
    )

    result = complete_task_and_create_next.func(
        CompleteTaskInput(
            task_status="failed",
            result_summary="Forge deploy failed on Fuji.",
            next_tasks=[
                NextTask(
                    assigned_to="coding",
                    task_type="coding.generate_contracts",
                    description="Fix the deployment failure.",
                )
            ],
        )
    )

    assert result["task_status"] == "failed"
    assert captured["task_status"] == "failed"
    assert captured["next_tasks"][0]["parent_task_id"] == str(current_task_id)
    assert captured["next_tasks"][0]["sequence_index"] == 0
    assert captured["next_tasks"][0]["artifact_revision"] == 1
    assert captured["next_tasks"][0]["retry_budget_key"] == "coding"
    assert captured["next_tasks"][0]["retry_attempt"] == 2
    assert captured["next_tasks"][0]["context"]["failure_context"]["task_id"] == str(current_task_id)


def test_pending_task_sort_key_prefers_fifo_then_sequence_index():
    base_time = datetime(2026, 4, 3, tzinfo=timezone.utc)
    later_time = base_time + timedelta(seconds=1)
    second = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=base_time,
        sequence_index=1,
    )
    first = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=base_time,
        sequence_index=0,
    )
    last = SimpleNamespace(
        id=uuid.uuid4(),
        created_at=later_time,
        sequence_index=0,
    )

    ordered = sorted([second, last, first], key=pending_task_sort_key)

    assert ordered == [first, second, last]
