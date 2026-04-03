import uuid
from typing import AsyncIterator

from agents.context import (
    clear_project_context,
    set_pipeline_run_id,
    set_pipeline_task_id,
    set_project_context,
)
from agents.agent_registry import stream_chat_with_intent
from agents.db import async_session_factory
from agents.db.crud import (
    create_pipeline_task,
    get_next_pending_task,
    get_pipeline_task,
    get_pipeline_run_tasks,
    set_task_in_progress,
)
from agents.memory_manager import MemoryManager
from agents.pipeline_cancel import is_pipeline_cancelled, clear_cancellation

MAX_ITERATIONS = 10  # Just a hard cap to prevent infinite agent loops

VALID_AGENTS = {"coding", "testing", "deployment", "audit"}
TERMINAL_DEPLOY_TASK_TYPES = {
    "deployment.execute_deploy",
    "deployment.retry_deploy",
}
EXECUTION_RESULT_HISTORY = {
    "testing.run_tests": ("testing", "last_test_results"),
    "deployment.execute_deploy": ("deployment", "last_deploy_results"),
    "deployment.retry_deploy": ("deployment", "last_deploy_results"),
}
INITIAL_TASK = {
    "assigned_to": "coding",
    "task_type": "coding.generate_contracts",
    "description": "Generate Solidity contracts from the approved plan.",
}

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


def _get_tagged_history_entry(
    project_id: str,
    user_id: str,
    agent_name: str,
    history_key: str,
    pipeline_run_id: str,
    task_id: str,
):
    """Return the latest tagged stage result for a specific pipeline task."""
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    state = mm.get_agent_state(agent_name)
    history = state.get(history_key, [])
    for entry in reversed(history):
        if (
            entry.get("pipeline_run_id") == pipeline_run_id
            and entry.get("pipeline_task_id") == task_id
        ):
            return entry
    return None


def _validate_execution_result(
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    task,
) -> tuple[bool, str | None, dict | None]:
    """Ensure execution tasks have a tagged result whose exit_code matches task status."""
    history_spec = EXECUTION_RESULT_HISTORY.get(task.task_type)
    if not history_spec:
        return True, None, None

    agent_name, history_key = history_spec
    entry = _get_tagged_history_entry(
        project_id=project_id,
        user_id=user_id,
        agent_name=agent_name,
        history_key=history_key,
        pipeline_run_id=pipeline_run_id,
        task_id=str(task.id),
    )
    if entry is None:
        return (
            False,
            (
                f"Task '{task.task_type}' finished without a tagged execution "
                "result for this pipeline task."
            ),
            None,
        )

    exit_code = entry.get("exit_code")
    if task.status == "completed" and exit_code != 0:
        return (
            False,
            (
                f"Task '{task.task_type}' was marked completed but the recorded "
                f"execution exit_code was {exit_code}."
            ),
            entry,
        )
    if task.status == "failed" and exit_code == 0:
        return (
            False,
            (
                f"Task '{task.task_type}' was marked failed but the recorded "
                "execution exit_code was 0."
            ),
            entry,
        )
    return True, None, entry


def _run_has_successful_terminal_deploy(
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    tasks: list,
) -> bool:
    """True when a terminal deployment task for this run has exit_code 0."""
    terminal_task_ids = {
        str(task.id)
        for task in tasks
        if task.task_type in TERMINAL_DEPLOY_TASK_TYPES
    }
    if not terminal_task_ids:
        return False

    mm = MemoryManager(user_id=user_id, project_id=project_id)
    state = mm.get_agent_state("deployment")
    history = state.get("last_deploy_results", [])
    return any(
        entry.get("pipeline_run_id") == pipeline_run_id
        and entry.get("pipeline_task_id") in terminal_task_ids
        and entry.get("exit_code") == 0
        for entry in history
    )


def _derive_pipeline_status(
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    tasks: list,
) -> tuple[str, str | None]:
    """Summarize pipeline status from task state plus tagged deploy results."""
    if not tasks:
        return "pending", None
    if any(task.status == "in_progress" for task in tasks):
        return "running", None
    if any(task.status == "pending" for task in tasks):
        return "queued", None
    if _run_has_successful_terminal_deploy(project_id, user_id, pipeline_run_id, tasks):
        return "completed", None

    failed_tasks = [task for task in tasks if task.status == "failed"]
    if failed_tasks:
        latest_failed = failed_tasks[-1]
        return (
            "failed",
            latest_failed.result_summary
            or f"Task '{latest_failed.task_type}' failed without a successful deployment.",
        )

    return (
        "failed",
        "Pipeline exhausted all tasks without a successful deployment execution.",
    )


async def run_autonomous_pipeline(
    project_id: str,
    user_id: str,
    max_iterations: int = MAX_ITERATIONS,
) -> AsyncIterator[dict]:
    """
    Run the autonomous post-approval pipeline.

    Yields structured event dicts for SSE streaming to the frontend:
        {"type": "pipeline_start",   "pipeline_run_id": "..."}
        {"type": "stage_start",      "stage": "coding", "task_id": "...", "description": "..."}
        {"type": "agent_message",    "stage": "coding", "content": "..."}
        {"type": "tool_call",        "stage": "coding", "tool": "save_code_artifact", "args": "..."}
        {"type": "stage_complete",   "stage": "coding", "task_id": "..."}
        {"type": "pipeline_complete","pipeline_run_id": "...", "tasks_completed": 3}
        {"type": "pipeline_cancelled","pipeline_run_id": "...", "stage": "..."}
        {"type": "pipeline_error",   "stage": "testing", "error": "..."}
    """

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
                assigned_to=INITIAL_TASK["assigned_to"],
                created_by="orchestrator",
                task_type=INITIAL_TASK["task_type"],
                description=INITIAL_TASK["description"],
                sequence_index=0,
            )
    except Exception as e:
        yield {
            "type": "pipeline_error",
            "stage": "init",
            "error": f"Could not seed first task: {e}",
        }
        _update_plan_status(project_id, user_id, "failed")
        clear_project_context()
        return

    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        if is_pipeline_cancelled(pipeline_run_id):
            yield {
                "type": "pipeline_cancelled",
                "pipeline_run_id": pipeline_run_id,
                "iteration": iteration,
            }
            _update_plan_status(project_id, user_id, "ready")
            clear_cancellation(pipeline_run_id)
            break

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
            _update_plan_status(project_id, user_id, "failed")
            break

        if task is None:
            async with async_session_factory() as session:
                tasks = await get_pipeline_run_tasks(session, uuid.UUID(pipeline_run_id))
            if _run_has_successful_terminal_deploy(
                project_id, user_id, pipeline_run_id, tasks
            ):
                yield {
                    "type": "pipeline_complete",
                    "pipeline_run_id": pipeline_run_id,
                    "tasks_completed": len(
                        [task for task in tasks if task.status == "completed"]
                    ),
                }
                _update_plan_status(project_id, user_id, "deployed")
            else:
                status, failure_reason = _derive_pipeline_status(
                    project_id, user_id, pipeline_run_id, tasks
                )
                yield {
                    "type": "pipeline_error",
                    "stage": "deployment",
                    "pipeline_run_id": pipeline_run_id,
                    "error": failure_reason
                    or "Pipeline ended without a successful deployment execution.",
                    "status": status,
                }
                _update_plan_status(project_id, user_id, "failed")
            break

        if task.assigned_to not in VALID_AGENTS:
            yield {
                "type": "pipeline_error",
                "stage": task.assigned_to,
                "error": f"Invalid agent assignment: '{task.assigned_to}'",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        stage = task.assigned_to
        task_id = str(task.id)
        set_pipeline_task_id(task_id)

        try:
            async with async_session_factory() as session:
                await set_task_in_progress(session, task.id)
        except Exception as e:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "error": f"Could not mark task in_progress: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        plan_status = _AGENT_TO_PLAN_STATUS.get(stage, "generating")
        _update_plan_status(project_id, user_id, plan_status)

        yield {
            "type": "stage_start",
            "stage": stage,
            "task_id": task_id,
            "task_type": task.task_type,
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
                if is_pipeline_cancelled(pipeline_run_id):
                    yield {
                        "type": "pipeline_cancelled",
                        "pipeline_run_id": pipeline_run_id,
                        "stage": stage,
                        "iteration": iteration,
                    }
                    _update_plan_status(project_id, user_id, "ready")
                    clear_cancellation(pipeline_run_id)
                    clear_project_context()
                    return

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
        finally:
            set_pipeline_task_id(None)

        try:
            async with async_session_factory() as session:
                updated_task = await get_pipeline_task(session, uuid.UUID(task_id))
        except Exception as e:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": f"Could not reload task after agent execution: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        if updated_task is None:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": "Pipeline task disappeared before completion could be verified.",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        if updated_task.status not in {"completed", "failed"}:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": (
                    "Agent finished without resolving its pipeline task via "
                    "complete_task_and_create_next()."
                ),
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        valid_result, validation_error, execution_result = _validate_execution_result(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            task=updated_task,
        )
        if not valid_result:
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": validation_error,
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        yield {
            "type": "stage_complete",
            "stage": stage,
            "task_id": task_id,
            "task_type": updated_task.task_type,
            "task_status": updated_task.status,
            "iteration": iteration,
            "result_exit_code": (
                execution_result.get("exit_code") if execution_result else None
            ),
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
    user_id: str,
    pipeline_run_id: str,
) -> dict:
    """
    Return the full task history for a pipeline run.
    Used by GET /pipeline/status for frontend display.
    """
    try:
        async with async_session_factory() as session:
            tasks = await get_pipeline_run_tasks(session, uuid.UUID(pipeline_run_id))

        status, failure_reason = _derive_pipeline_status(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            tasks=tasks,
        )

        return {
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
            "status": status,
            "failure_reason": failure_reason,
            "total_tasks": len(tasks),
            "tasks": [
                {
                    "id": str(t.id),
                    "assigned_to": t.assigned_to,
                    "created_by": t.created_by,
                    "task_type": t.task_type,
                    "description": t.description,
                    "parent_task_id": (
                        str(t.parent_task_id) if t.parent_task_id else None
                    ),
                    "sequence_index": t.sequence_index,
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
