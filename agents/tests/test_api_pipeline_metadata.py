import asyncio

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
                "exit_code": 1,
                "stdout": "large stdout",
                "trace_id": "trace-123",
            }
        ]

    def list_test_runs(self, limit: int = 20) -> list[dict]:
        return [
            {
                "pipeline_run_id": "run-123",
                "pipeline_task_id": "task-789",
                "exit_code": 0,
                "stderr": "large stderr",
                "trace_id": "trace-456",
            }
        ]


async def _noop_ensure_project_context(project_id, user_id, session):
    return None


def test_pipeline_status_returns_new_metadata(monkeypatch):
    captured = {}

    async def fake_get_pipeline_status(project_id, user_id, pipeline_run_id):
        captured.update(
            {
                "project_id": project_id,
                "user_id": user_id,
                "pipeline_run_id": pipeline_run_id,
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
    }
    assert result["status"] == "failed"
    assert result["failure_reason"] == "Forge deploy failed."
    assert result["run"]["id"] == "run-123"
    assert result["tasks"][0]["task_type"] == "deployment.execute_deploy"
    assert result["tasks"][0]["parent_task_id"] == "task-0"
    assert result["tasks"][0]["sequence_index"] == 0


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
    assert "stderr" not in response.last_test_results[0]
