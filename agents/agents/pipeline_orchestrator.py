import uuid
from typing import AsyncIterator

from agents.context import (
    set_project_context,
    set_pipeline_run_id,
    clear_project_context,
)
from agents.agent_registry import stream_chat_with_intent
from agents.db import async_session_factory
from agents.db.crud import (
    create_pipeline_task,
    get_next_pending_task,
    set_task_in_progress,
    get_pipeline_task_count,
    get_pipeline_run_tasks,
)
from agents.memory_manager import MemoryManager

MAX_ITERATIONS = 10  # Just a hard cap to prevent infinite agent loops

VALID_AGENTS = {"coding", "testing", "deployment", "audit"}

_AGENT_TO_PLAN_STATUS = {
    "coding": "generating",
    "testing": "testing",
    "deployment": "deploying",
    "audit": "testing",  # audit doesn't have its own status, keep as testing
}


def _update_plan_status(project_id: str, user_id: str, status: str) -> None:
    """Update the plan status in Letta memory (best-effort, non-blocking)."""
    try:
        mm = MemoryManager(user_id=user_id, project_id=project_id)
        plan = mm.get_plan()
        if plan:
            plan["status"] = status
            mm.save_plan(plan)
    except Exception as e:
        print(
            f"[Orchestrator] Warning: could not update plan status to '{status}': {e}"
        )


async def run_autonomous_pipeline(
    project_id: str,
    user_id: str,
    max_iterations: int = MAX_ITERATIONS,
) -> AsyncIterator[dict]:
    pipeline_run_id = str(uuid.uuid4())

    yield {
        "type": "pipeline_start",
        "pipeline_run_id": pipeline_run_id,
        "project_id": project_id,
    }

    set_project_context(project_id, user_id)
    set_pipeline_run_id(pipeline_run_id)

    try:
        async with async_session_factory() as session:
            await create_pipeline_task(
                session,
                pipeline_run_id=uuid.UUID(pipeline_run_id),
                project_id=uuid.UUID(project_id),
                assigned_to="coding",
                created_by="orchestrator",
                description="Generate Solidity contracts from the approved plan.",
            )
    except Exception as e:
        yield {
            "type": "pipeline_error",
            "stage": "init",
            "error": f"Could not seed first task: {e}",
        }
        clear_project_context()
        return

    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        set_project_context(project_id, user_id)
        set_pipeline_run_id(pipeline_run_id)

        try:
            async with async_session_factory() as session:
                task = await get_next_pending_task(session, uuid.UUID(pipeline_run_id))
        except Exception as e:
            yield {
                "type": "pipeline_error",
                "stage": "dispatch",
                "error": f"DB error reading tasks: {e}",
            }
            break

        if task is None:
            yield {
                "type": "pipeline_complete",
                "pipeline_run_id": pipeline_run_id,
                "tasks_completed": iteration - 1,
            }
            _update_plan_status(project_id, user_id, "deployed")
            break

        if task.assigned_to not in VALID_AGENTS:
            yield {
                "type": "pipeline_error",
                "stage": task.assigned_to,
                "error": f"Invalid agent assignment: '{task.assigned_to}'",
            }
            break

        stage = task.assigned_to
        task_id = str(task.id)

        try:
            async with async_session_factory() as session:
                await set_task_in_progress(session, task.id)
        except Exception as e:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "error": f"Could not mark task in_progress: {e}",
            }
            break

        plan_status = _AGENT_TO_PLAN_STATUS.get(stage, "generating")
        _update_plan_status(project_id, user_id, plan_status)

        yield {
            "type": "stage_start",
            "stage": stage,
            "task_id": task_id,
            "description": task.description,
            "iteration": iteration,
        }

        try:
            async for event in stream_chat_with_intent(
                intent=stage,
                session_id=f"pipeline-{pipeline_run_id}-{iteration}",
                user_message=task.description,
                project_id=project_id,
            ):
                if event.get("type") == "step":
                    step_event = {"type": "agent_message", "stage": stage}
                    if event.get("content"):
                        step_event["content"] = event["content"]
                    if event.get("tool_calls"):
                        for tc in event["tool_calls"]:
                            yield {
                                "type": "tool_call",
                                "stage": stage,
                                "tool": tc.get("name", ""),
                                "args": tc.get("args", ""),
                            }
                    if event.get("content"):
                        yield step_event

                elif event.get("type") == "done":
                    pass

        except Exception as e:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": f"Agent '{stage}' raised an exception: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        yield {
            "type": "stage_complete",
            "stage": stage,
            "task_id": task_id,
            "iteration": iteration,
        }

    else:
        yield {
            "type": "pipeline_error",
            "stage": "orchestrator",
            "error": (
                f"Pipeline hit the maximum iteration cap ({max_iterations}). "
                f"The agents may be stuck in a loop. Please review the task "
                f"history and intervene manually."
            ),
        }
        _update_plan_status(project_id, user_id, "failed")

    clear_project_context()


async def get_pipeline_status(
    project_id: str,
    pipeline_run_id: str,
) -> dict:
    """
    Return the full task history for a pipeline run.
    Used by GET /pipeline/status for frontend display.
    """
    try:
        async with async_session_factory() as session:
            tasks = await get_pipeline_run_tasks(session, uuid.UUID(pipeline_run_id))

        return {
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
            "total_tasks": len(tasks),
            "tasks": [
                {
                    "id": str(t.id),
                    "assigned_to": t.assigned_to,
                    "created_by": t.created_by,
                    "description": t.description,
                    "status": t.status,
                    "result_summary": t.result_summary,
                    "context": t.context,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "completed_at": (
                        t.completed_at.isoformat() if t.completed_at else None
                    ),
                }
                for t in tasks
            ],
        }
    except Exception as e:
        return {"error": f"Could not retrieve pipeline status: {e}"}
