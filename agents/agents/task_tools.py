"""
Two tools added to every agent (except planning) for the autonomous pipeline:

    1. get_my_current_task:read what work has been assigned to this agent
    2. complete_task_and_create_next: mark current task done, push next task(s)

These tools read/write to the Neon pipeline_tasks table via async CRUD,
bridged into sync context for LangChain @tool compatibility.
"""

import asyncio
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field


# LangChain tools are sync and our DB CRUD is async. We use a dedicated thread
# pool so asyncio.run() always gets a fresh event loop, avoiding conflicts with FastAPI's running loop.

_db_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline-task")


def _run_async(coro):
    """Run an async coroutine from sync tool context safely."""
    future = _db_executor.submit(asyncio.run, coro)
    return future.result()


def _db_available() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _get_context():
    """Read pipeline_run_id and project_id from contextvars."""
    from agents.context import get_project_context, get_pipeline_run_id

    project_id, user_id = get_project_context()
    pipeline_run_id = get_pipeline_run_id()
    return project_id, user_id, pipeline_run_id


async def _get_current_task_async(pipeline_run_id: str):
    """Fetch the in-progress task for this pipeline run."""
    from agents.db import async_session_factory
    from agents.db.crud import get_current_in_progress_task

    async with async_session_factory() as session:
        return await get_current_in_progress_task(session, uuid.UUID(pipeline_run_id))


async def _complete_and_create_async(
    pipeline_run_id: str,
    project_id: str,
    task_id: str,
    result_summary: str,
    next_tasks: list[dict],
    created_by: str,
):
    """Complete current task and create next tasks in a single session."""
    from agents.db import async_session_factory
    from agents.db.crud import complete_task, create_pipeline_task

    async with async_session_factory() as session:
        await complete_task(session, uuid.UUID(task_id), result_summary)

        created = []
        for t in next_tasks:
            new_task = await create_pipeline_task(
                session,
                pipeline_run_id=uuid.UUID(pipeline_run_id),
                project_id=uuid.UUID(project_id),
                assigned_to=t["assigned_to"],
                created_by=created_by,
                description=t["description"],
                context=t.get("context"),
            )
            created.append(
                {
                    "id": str(new_task.id),
                    "assigned_to": new_task.assigned_to,
                    "description": new_task.description,
                }
            )
        return created


class NextTask(BaseModel):
    """A task to be created for another agent."""

    assigned_to: str = Field(
        ...,
        description=(
            "Which agent should handle this task. "
            "Must be one of: coding, testing, deployment, audit"
        ),
    )
    description: str = Field(
        ...,
        description=(
            "Clear, natural language instruction for the assigned agent. "
            "Include what needs to be done and why."
        ),
    )
    context: Optional[dict] = Field(
        default=None,
        description=(
            "Optional structured data the next agent needs: error output, "
            "file paths, test results, etc."
        ),
    )


class CompleteTaskInput(BaseModel):
    """Input for completing the current task and creating follow-up tasks."""

    result_summary: str = Field(
        ...,
        description=(
            "Brief summary of what you accomplished on this task. "
            "Include key outcomes: files generated, tests passed/failed, "
            "contracts deployed, errors encountered."
        ),
    )
    next_tasks: List[NextTask] = Field(
        default_factory=list,
        description=(
            "Tasks to create for other agents. Leave empty ONLY when the "
            "contract has been successfully deployed — that signals the "
            "pipeline is complete. Otherwise, always create at least one "
            "next task to keep the pipeline moving."
        ),
    )


@tool
def get_my_current_task() -> dict:
    """
    Retrieve the task assigned to you for this pipeline run.

    Call this at the START of your work to understand:
    - What you've been asked to do (description)
    - Any context from the previous agent (error output, file paths, etc.)
    - Who created the task (which agent assigned this to you)

    Returns the task details or an error if no task is found.
    """
    if not _db_available():
        return {"error": "DATABASE_URL not configured — pipeline tasks require Neon"}

    project_id, user_id, pipeline_run_id = _get_context()

    if not pipeline_run_id:
        return {
            "error": (
                "No active pipeline run. This tool is only available "
                "during autonomous pipeline execution."
            )
        }

    try:
        task = _run_async(_get_current_task_async(pipeline_run_id))
        if task is None:
            return {"error": "No in-progress task found for this pipeline run."}

        result = {
            "task_id": str(task.id),
            "assigned_to": task.assigned_to,
            "created_by": task.created_by,
            "description": task.description,
            "status": task.status,
        }
        if task.context:
            result["context"] = task.context
        return result
    except Exception as e:
        return {"error": f"Could not retrieve task: {str(e)}"}


@tool
def complete_task_and_create_next(input: CompleteTaskInput) -> dict:
    """
    Mark your current task as completed and create the next task(s).

    Call this when you have FINISHED your work. You MUST call this before
    your turn ends, or the pipeline will stall.

    How to decide what next tasks to create:

    CODING AGENT:
    - After generating code successfully → create task for "testing"
    - If you cannot generate code (plan issue) → create task for "testing"
      with error context so it can be diagnosed

    TESTING AGENT:
    - If all tests pass → create task for "deployment"
    - If tests fail due to contract bugs → create task for "coding" with
      the error output and file paths in context
    - If tests fail due to test-only issues → fix tests yourself, re-run,
      then create the appropriate next task

    DEPLOYMENT AGENT:
    - If deployment succeeds → create NO next tasks (empty list). This
      signals the pipeline is complete.
    - If deployment fails due to contract issues → create task for "coding"
      with the error in context
    - If deployment fails due to config issues → create task for "deployment"
      to retry with adjusted parameters

    AUDIT AGENT:
    - After completing audit → create task for the appropriate agent based
      on findings, or no tasks if everything is clean

    Args:
        input: CompleteTaskInput with result_summary and next_tasks list.
    """
    if not _db_available():
        return {"error": "DATABASE_URL not configured; pipeline tasks require Neon"}

    project_id, user_id, pipeline_run_id = _get_context()

    if not pipeline_run_id:
        return {
            "error": (
                "No active pipeline run. This tool is only available "
                "during autonomous pipeline execution."
            )
        }

    if not project_id:
        return {"error": "No project_id in context."}

    try:
        task = _run_async(_get_current_task_async(pipeline_run_id))
        if task is None:
            return {"error": "No in-progress task found to complete."}

        valid_agents = {"coding", "testing", "deployment", "audit"}
        for nt in input.next_tasks:
            if nt.assigned_to not in valid_agents:
                return {
                    "error": (
                        f"Invalid assigned_to '{nt.assigned_to}'. "
                        f"Must be one of: {', '.join(sorted(valid_agents))}"
                    )
                }

        created = _run_async(
            _complete_and_create_async(
                pipeline_run_id=pipeline_run_id,
                project_id=project_id,
                task_id=str(task.id),
                result_summary=input.result_summary,
                next_tasks=[nt.model_dump() for nt in input.next_tasks],
                created_by=task.assigned_to,  # the current agent is the creator
            )
        )

        result = {
            "success": True,
            "completed_task_id": str(task.id),
            "result_summary": input.result_summary,
            "next_tasks_created": len(created),
        }
        if created:
            result["next_tasks"] = created
        else:
            result["pipeline_signal"] = "no_more_tasks"

        return result
    except Exception as e:
        return {"error": f"Could not complete task: {str(e)}"}


TASK_TOOLS = [
    get_my_current_task,
    complete_task_and_create_next,
]
