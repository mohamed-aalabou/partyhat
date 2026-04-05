"""
Two tools added to every agent (except planning) for the autonomous pipeline:

    1. get_my_current_task:read what work has been assigned to this agent
    2. complete_task_and_create_next: mark current task done, push next task(s)

These tools use SYNCHRONOUS SQLAlchemy sessions to avoid event loop
conflicts. LangChain tools are sync functions called from within an
async context (FastAPI → agent.astream → tool). Using asyncio.run()
in a thread creates a new event loop that conflicts with asyncpg's
connection pool on the original loop. Sync psycopg2 sessions sidestep
this entirely.
"""

import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from agents.pipeline_specs import (
    TERMINAL_SUCCESS_TASK_TYPES,
    retry_budget_key_for_task,
)
from agents.pipeline_context import (
    default_expected_outputs,
    extract_plan_summary,
    standardize_task_context,
)


def _get_sync_url() -> str:
    """Convert DATABASE_URL to a sync-compatible URL for psycopg2."""
    url = os.getenv("DATABASE_URL", "")
    # Removing async driver prefixes
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    # Ensuring it starts with postgresql://
    if not url.startswith("postgresql://"):
        url = "postgresql://localhost/partyhat"
    return url


def _is_remote_ssl_host(url: str) -> bool:
    if not url or "localhost" in url or "127.0.0.1" in url:
        return False
    return "neon.tech" in url or ".aws.neon.tech" in url


_sync_url = _get_sync_url()
_sync_engine = (
    create_engine(
        _sync_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"sslmode": "require"} if _is_remote_ssl_host(_sync_url) else {},
    )
    if os.getenv("DATABASE_URL")
    else None
)

_SyncSession = sessionmaker(bind=_sync_engine) if _sync_engine else None


def _get_sync_session() -> Session | None:
    if _SyncSession is None:
        return None
    return _SyncSession()


def _db_available() -> bool:
    return _sync_engine is not None


def _get_context():
    """Read pipeline identifiers from contextvars."""
    from agents.context import (
        get_pipeline_run_id,
        get_pipeline_task_id,
        get_project_context,
    )

    project_id, user_id = get_project_context()
    pipeline_run_id = get_pipeline_run_id()
    pipeline_task_id = get_pipeline_task_id()
    return project_id, user_id, pipeline_run_id, pipeline_task_id


def _get_memory_manager(project_id: str | None, user_id: str | None):
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id or "default", project_id=project_id)


def _get_artifact_snapshot(project_id: str | None, user_id: str | None) -> dict:
    mm = _get_memory_manager(project_id, user_id)
    return {
        "coding": mm.get_agent_state("coding").get("artifacts", []),
        "testing": mm.get_agent_state("testing").get("artifacts", []),
        "deployment": mm.get_agent_state("deployment").get("artifacts", []),
    }


def _get_plan_summary(project_id: str | None, user_id: str | None) -> dict:
    mm = _get_memory_manager(project_id, user_id)
    planning = mm.get_agent_state("planning")
    summary = planning.get("plan_summary")
    if summary:
        return summary
    plan = mm.get_plan()
    return extract_plan_summary(plan)


def _next_artifact_revision(task, task_status: str) -> int:
    current = 0
    if getattr(task, "artifact_revision", None) is not None:
        current = int(task.artifact_revision)
    elif getattr(task, "context", None):
        current = int((task.context or {}).get("artifact_revision", 0) or 0)

    if task.task_type == "coding.generate_contracts" and task_status == "completed":
        return current + 1
    return current


def _update_revision_pointer(project_id: str | None, user_id: str | None, revision: int) -> None:
    mm = _get_memory_manager(project_id, user_id)
    mm.update_coding_state(latest_artifact_revision=revision)


def _get_current_task_sync(
    pipeline_run_id: str,
    pipeline_task_id: str | None = None,
):
    """Fetch the active task for this pipeline run (sync)."""
    from agents.db.models import PipelineTask

    session = _get_sync_session()
    if session is None:
        return None
    try:
        query = select(PipelineTask).where(
            PipelineTask.pipeline_run_id == uuid.UUID(pipeline_run_id)
        )
        if pipeline_task_id:
            query = query.where(PipelineTask.id == uuid.UUID(pipeline_task_id))
        else:
            query = query.where(PipelineTask.status == "in_progress")
        result = session.execute(query.limit(1))
        return result.scalar_one_or_none()
    finally:
        session.close()


def _complete_and_create_sync(
    pipeline_run_id: str,
    project_id: str,
    task_id: str,
    task_status: str,
    result_summary: str,
    next_tasks: list[dict],
    created_by: str,
):
    """Complete current task and create next tasks in a single session (sync)."""
    from agents.db.models import PipelineTask

    session = _get_sync_session()
    if session is None:
        return []
    try:
        task = session.execute(
            select(PipelineTask).where(PipelineTask.id == uuid.UUID(task_id))
        ).scalar_one_or_none()

        if task:
            task.status = task_status
            task.result_summary = result_summary
            task.completed_at = datetime.now(timezone.utc)

        created = []
        for idx, t in enumerate(next_tasks):
            new_task = PipelineTask(
                pipeline_run_id=uuid.UUID(pipeline_run_id),
                project_id=uuid.UUID(project_id),
                assigned_to=t["assigned_to"],
                created_by=created_by,
                task_type=t["task_type"],
                description=t["description"],
                parent_task_id=(
                    uuid.UUID(t["parent_task_id"])
                    if t.get("parent_task_id")
                    else uuid.UUID(task_id)
                ),
                sequence_index=t.get("sequence_index", idx),
                artifact_revision=t.get("artifact_revision", 0),
                depends_on_task_ids=t.get("depends_on_task_ids"),
                retry_budget_key=t.get("retry_budget_key"),
                retry_attempt=t.get("retry_attempt", 0),
                failure_class=t.get("failure_class"),
                gate_id=(
                    uuid.UUID(t["gate_id"])
                    if t.get("gate_id")
                    else None
                ),
                status=t.get("status", "pending"),
                context=t.get("context"),
            )
            session.add(new_task)
            session.flush()  # getting the ID
            created.append(
                {
                    "id": str(new_task.id),
                    "assigned_to": new_task.assigned_to,
                    "task_type": new_task.task_type,
                    "description": new_task.description,
                    "parent_task_id": (
                        str(new_task.parent_task_id) if new_task.parent_task_id else None
                    ),
                    "sequence_index": new_task.sequence_index,
                }
            )

        session.commit()
        return created
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    task_type: str = Field(
        ...,
        description=(
            "Canonical task type in <agent>.<action> format, such as "
            "testing.run_tests or deployment.execute_deploy."
        ),
    )
    context: Optional[dict] = Field(
        default=None,
        description=(
            "Optional structured data the next agent needs: error output, "
            "file paths, test results, etc."
        ),
    )
    parent_task_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional explicit parent task UUID. If omitted, the current task "
            "becomes the parent so this is recorded as a subtask."
        ),
    )
    sequence_index: Optional[int] = Field(
        default=None,
        description=(
            "Optional sibling order. Omit to use the order of next_tasks in "
            "this completion call."
        ),
    )
    depends_on_task_ids: Optional[List[str]] = Field(
        default=None,
        description=(
            "Optional prerequisite task IDs that must be completed before this "
            "task becomes runnable."
        ),
    )


class CompleteTaskInput(BaseModel):
    """Input for completing the current task and creating follow-up tasks."""

    task_status: Literal["completed", "failed"] = Field(
        ...,
        description=(
            "Whether the current task succeeded or failed. Failed tasks may "
            "still create remediation subtasks."
        ),
    )
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
            "current task is a successful terminal deployment task, or when "
            "the current task failed with no viable recovery path."
        ),
    )


def _is_valid_task_type(assigned_to: str, task_type: str) -> bool:
    return (
        "." in task_type
        and task_type.split(".", 1)[0] == assigned_to
        and bool(task_type.split(".", 1)[1].strip())
    )


def _normalize_next_tasks(
    current_task,
    next_tasks: List[NextTask],
    *,
    project_id: str | None,
    user_id: str | None,
    task_status: str,
    result_summary: str,
) -> list[dict]:
    """Apply default pipeline context before writing follow-up tasks."""
    normalized = []
    artifact_snapshot = _get_artifact_snapshot(project_id, user_id)
    plan_summary = _get_plan_summary(project_id, user_id)
    artifact_revision = _next_artifact_revision(current_task, task_status)
    upstream_task = {
        "task_id": str(current_task.id),
        "task_type": current_task.task_type,
        "assigned_to": current_task.assigned_to,
        "status": task_status,
        "result_summary": result_summary,
    }
    failure_context = (
        {
            "task_id": str(current_task.id),
            "task_type": current_task.task_type,
            "result_summary": result_summary,
        }
        if task_status == "failed"
        else None
    )

    next_attempts: dict[str, int] = {}
    for idx, task in enumerate(next_tasks):
        payload = task.model_dump()
        retry_budget_key = retry_budget_key_for_task(payload["task_type"])
        if retry_budget_key not in next_attempts:
            next_attempts[retry_budget_key] = _get_next_retry_attempt_sync(
                pipeline_run_id=str(current_task.pipeline_run_id),
                retry_budget_key=retry_budget_key,
            )
        payload["parent_task_id"] = payload.get("parent_task_id") or str(current_task.id)
        payload["sequence_index"] = (
            payload["sequence_index"] if payload.get("sequence_index") is not None else idx
        )
        payload["artifact_revision"] = artifact_revision
        payload["retry_budget_key"] = retry_budget_key
        payload["retry_attempt"] = next_attempts[retry_budget_key]
        payload["context"] = standardize_task_context(
            payload.get("context"),
            plan_summary=plan_summary,
            artifact_revision=artifact_revision,
            input_artifacts=artifact_snapshot,
            upstream_task=upstream_task,
            failure_context=failure_context,
            expected_outputs=payload.get("context", {}).get("expected_outputs")
            if isinstance(payload.get("context"), dict)
            else default_expected_outputs(payload["task_type"]),
        )
        normalized.append(payload)
        next_attempts[retry_budget_key] += 1

    _update_revision_pointer(project_id, user_id, artifact_revision)
    return normalized


def _get_next_retry_attempt_sync(
    *,
    pipeline_run_id: str,
    retry_budget_key: str,
) -> int:
    from agents.db.models import PipelineTask

    session = _get_sync_session()
    if session is None:
        return 0
    try:
        result = session.execute(
            select(PipelineTask.retry_attempt)
            .where(
                PipelineTask.pipeline_run_id == uuid.UUID(pipeline_run_id),
                PipelineTask.retry_budget_key == retry_budget_key,
            )
            .order_by(PipelineTask.retry_attempt.desc())
            .limit(1)
        ).scalar_one_or_none()
        return int(result or -1) + 1
    finally:
        session.close()


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
        return {"error": "DATABASE_URL not configured; pipeline tasks require Neon"}

    project_id, user_id, pipeline_run_id, pipeline_task_id = _get_context()

    if not pipeline_run_id:
        return {
            "error": (
                "No active pipeline run. This tool is only available "
                "during autonomous pipeline execution."
            )
        }

    try:
        task = _get_current_task_sync(pipeline_run_id, pipeline_task_id)
        if task is None:
            return {"error": "No in-progress task found for this pipeline run."}

        result = {
            "task_id": str(task.id),
            "assigned_to": task.assigned_to,
            "created_by": task.created_by,
            "task_type": task.task_type,
            "description": task.description,
            "status": task.status,
            "parent_task_id": (
                str(task.parent_task_id) if task.parent_task_id else None
            ),
            "sequence_index": task.sequence_index,
            "artifact_revision": getattr(task, "artifact_revision", 0),
            "depends_on_task_ids": getattr(task, "depends_on_task_ids", None),
            "retry_budget_key": getattr(task, "retry_budget_key", None),
            "retry_attempt": getattr(task, "retry_attempt", 0),
            "failure_class": getattr(task, "failure_class", None),
            "gate_id": str(getattr(task, "gate_id", None))
            if getattr(task, "gate_id", None)
            else None,
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
    - Complete "coding.generate_contracts" → create task for "testing" with
      task_type "testing.generate_tests"
    - If you cannot generate code (plan issue) → create task for "testing"
      with task_type "testing.generate_tests" and error context

    TESTING AGENT:
    - Complete "testing.generate_tests" → create task for "testing" with
      task_type "testing.run_tests"
    - If "testing.run_tests" passes → create task for "deployment" with
      task_type "deployment.prepare_script"
    - If tests fail due to contract bugs → create task for "coding" with
      task_type "coding.generate_contracts" and the error output in context
    - If tests fail due to test-only issues → fix tests yourself, re-run,
      then create the appropriate next task

    DEPLOYMENT AGENT:
    - Complete "deployment.prepare_script" → create task for "deployment"
      with task_type "deployment.execute_deploy"
    - If "deployment.execute_deploy" or "deployment.retry_deploy" succeeds →
      create NO next tasks (empty list). This signals the pipeline is complete.
    - If deployment fails due to contract issues → create task for "coding"
      with task_type "coding.generate_contracts" and the error in context
    - If deployment fails due to config issues → create task for "deployment"
      with task_type "deployment.retry_deploy" to retry with adjusted parameters

    AUDIT AGENT:
    - After completing audit → create task for the appropriate agent based
      on findings, or no tasks if everything is clean

    Args:
        input: CompleteTaskInput with result_summary and next_tasks list.
    """
    if not _db_available():
        return {"error": "DATABASE_URL not configured; pipeline tasks require Neon"}

    project_id, user_id, pipeline_run_id, pipeline_task_id = _get_context()

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
        task = _get_current_task_sync(pipeline_run_id, pipeline_task_id)
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
            if not _is_valid_task_type(nt.assigned_to, nt.task_type):
                return {
                    "error": (
                        f"Invalid task_type '{nt.task_type}' for assigned_to "
                        f"'{nt.assigned_to}'. Use <agent>.<action> with the "
                        "agent prefix matching assigned_to."
                    )
                }
            if nt.parent_task_id:
                try:
                    uuid.UUID(nt.parent_task_id)
                except ValueError:
                    return {
                        "error": (
                            f"Invalid parent_task_id '{nt.parent_task_id}'. "
                            "Expected a UUID string."
                        )
                    }
            if nt.depends_on_task_ids:
                for dep_id in nt.depends_on_task_ids:
                    try:
                        uuid.UUID(dep_id)
                    except ValueError:
                        return {
                            "error": (
                                f"Invalid dependency task id '{dep_id}'. "
                                "Expected UUID strings in depends_on_task_ids."
                            )
                        }

        if (
            input.task_status == "completed"
            and not input.next_tasks
            and task.task_type not in TERMINAL_SUCCESS_TASK_TYPES
        ):
            return {
                "error": (
                    "Only successful terminal deployment tasks may complete "
                    "without creating follow-up tasks."
                )
            }

        normalized_next_tasks = _normalize_next_tasks(
            task,
            input.next_tasks,
            project_id=project_id,
            user_id=user_id,
            task_status=input.task_status,
            result_summary=input.result_summary,
        )

        created = _complete_and_create_sync(
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            task_id=str(task.id),
            task_status=input.task_status,
            result_summary=input.result_summary,
            next_tasks=normalized_next_tasks,
            created_by=task.assigned_to,  # current agent is the creator
        )

        result = {
            "success": True,
            "completed_task_id": str(task.id),
            "task_status": input.task_status,
            "result_summary": input.result_summary,
            "next_tasks_created": len(created),
        }
        if created:
            result["next_tasks"] = created
        else:
            result["pipeline_signal"] = (
                "no_more_tasks"
                if input.task_status == "completed"
                else "terminal_failure"
            )

        return result
    except Exception as e:
        return {"error": f"Could not complete task: {str(e)}"}


TASK_TOOLS = [
    get_my_current_task,
    complete_task_and_create_next,
]
