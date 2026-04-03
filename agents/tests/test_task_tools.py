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

    normalized = _normalize_next_tasks(current_task_id, next_tasks)

    assert normalized[0]["parent_task_id"] == current_task_id
    assert normalized[0]["sequence_index"] == 0
    assert normalized[1]["parent_task_id"] == explicit_parent
    assert normalized[1]["sequence_index"] == 7


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
    captured = {}

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
            task_type="deployment.execute_deploy",
            assigned_to="deployment",
        ),
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
