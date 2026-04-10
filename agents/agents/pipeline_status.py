from __future__ import annotations

from typing import Any

from agents.pipeline_context import duration_ms


def serialize_task(task) -> dict[str, Any]:
    queue_duration = duration_ms(task.created_at, task.claimed_at)
    execution_duration = duration_ms(task.claimed_at, task.completed_at)
    total_duration = duration_ms(task.created_at, task.completed_at)
    return {
        "id": str(task.id),
        "assigned_to": task.assigned_to,
        "created_by": task.created_by,
        "task_type": task.task_type,
        "description": task.description,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "sequence_index": task.sequence_index,
        "artifact_revision": task.artifact_revision,
        "depends_on_task_ids": task.depends_on_task_ids,
        "retry_budget_key": getattr(task, "retry_budget_key", None),
        "retry_attempt": getattr(task, "retry_attempt", 0),
        "failure_class": getattr(task, "failure_class", None),
        "gate_id": str(getattr(task, "gate_id", None))
        if getattr(task, "gate_id", None)
        else None,
        "status": task.status,
        "result_summary": task.result_summary,
        "context": task.context,
        "queued_at": task.created_at.isoformat() if task.created_at else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "claimed_at": task.claimed_at.isoformat() if task.claimed_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "queue_duration_ms": queue_duration,
        "execution_duration_ms": execution_duration,
        "total_duration_ms": total_duration,
    }


def serialize_run(run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "project_id": str(run.project_id),
        "user_id": str(run.user_id) if run.user_id else None,
        "plan_id": str(run.plan_id) if run.plan_id else None,
        "status": run.status,
        "current_stage": run.current_stage,
        "current_task_id": str(run.current_task_id) if run.current_task_id else None,
        "deployment_target": run.deployment_target,
        "cancellation_requested_at": (
            run.cancellation_requested_at.isoformat()
            if run.cancellation_requested_at
            else None
        ),
        "cancellation_reason": run.cancellation_reason,
        "terminal_deployment_id": (
            str(run.terminal_deployment_id) if run.terminal_deployment_id else None
        ),
        "failure_class": run.failure_class,
        "failure_reason": run.failure_reason,
        "trace_id": run.trace_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "paused_at": run.paused_at.isoformat() if run.paused_at else None,
        "runner_token": run.runner_token,
        "runner_started_at": (
            run.runner_started_at.isoformat() if run.runner_started_at else None
        ),
        "runner_heartbeat_at": (
            run.runner_heartbeat_at.isoformat() if run.runner_heartbeat_at else None
        ),
        "resumed_at": run.resumed_at.isoformat() if run.resumed_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


def serialize_gate(gate) -> dict[str, Any]:
    return {
        "id": str(gate.id),
        "pipeline_run_id": str(gate.pipeline_run_id),
        "pipeline_task_id": str(gate.pipeline_task_id)
        if gate.pipeline_task_id
        else None,
        "evaluation_id": str(gate.evaluation_id) if gate.evaluation_id else None,
        "gate_type": gate.gate_type,
        "status": gate.status,
        "requested_payload": gate.requested_payload,
        "resolved_payload": gate.resolved_payload,
        "requested_reason": gate.requested_reason,
        "resolved_reason": gate.resolved_reason,
        "requested_by": gate.requested_by,
        "resolved_by": gate.resolved_by,
        "trace_id": gate.trace_id,
        "created_at": gate.created_at.isoformat() if gate.created_at else None,
        "resolved_at": gate.resolved_at.isoformat() if gate.resolved_at else None,
    }


def serialize_evaluation(evaluation) -> dict[str, Any]:
    return {
        "id": str(evaluation.id),
        "pipeline_run_id": str(evaluation.pipeline_run_id),
        "pipeline_task_id": str(evaluation.pipeline_task_id)
        if evaluation.pipeline_task_id
        else None,
        "stage": evaluation.stage,
        "evaluation_type": evaluation.evaluation_type,
        "blocking": evaluation.blocking,
        "status": evaluation.status,
        "summary": evaluation.summary,
        "details_json": evaluation.details_json,
        "artifact_revision": evaluation.artifact_revision,
        "trace_id": evaluation.trace_id,
        "created_at": evaluation.created_at.isoformat()
        if evaluation.created_at
        else None,
    }


def derive_pipeline_status(run, tasks: list) -> tuple[str, str | None]:
    if run is not None:
        return run.status, run.failure_reason
    if not tasks:
        return "pending", None
    if any(task.status == "in_progress" for task in tasks):
        return "running", None
    if any(task.status == "waiting_for_approval" for task in tasks):
        return "waiting_for_approval", None
    if any(task.status == "pending" for task in tasks):
        return "queued", None
    failed_tasks = [task for task in tasks if task.status == "failed"]
    if failed_tasks:
        last_failed = failed_tasks[-1]
        return "failed", getattr(last_failed, "result_summary", None)
    cancelled_tasks = [task for task in tasks if task.status == "cancelled"]
    if cancelled_tasks:
        last_cancelled = cancelled_tasks[-1]
        return "cancelled", getattr(last_cancelled, "result_summary", None)
    return "failed", "Pipeline exhausted all tasks without a successful terminal deployment."


def build_pipeline_status_payload(
    *,
    project_id: str,
    pipeline_run_id: str,
    run,
    tasks: list,
    gates: list,
    evaluations: list,
) -> dict[str, Any]:
    status, failure_reason = derive_pipeline_status(run, tasks)
    return {
        "pipeline_run_id": pipeline_run_id,
        "project_id": project_id,
        "status": status,
        "failure_reason": failure_reason,
        "run": serialize_run(run) if run is not None else None,
        "total_tasks": len(tasks),
        "tasks": [serialize_task(task) for task in tasks],
        "gates": [serialize_gate(gate) for gate in gates],
        "evaluations": [serialize_evaluation(evaluation) for evaluation in evaluations],
    }


def project_pipeline_status_payload(
    payload: dict[str, Any],
    *,
    include_tasks: bool = True,
    include_gates: bool = True,
    include_evaluations: bool = True,
) -> dict[str, Any]:
    projected = dict(payload)
    projected["tasks"] = list(payload.get("tasks", [])) if include_tasks else []
    projected["gates"] = list(payload.get("gates", [])) if include_gates else []
    projected["evaluations"] = (
        list(payload.get("evaluations", [])) if include_evaluations else []
    )
    if "total_tasks" not in projected:
        projected["total_tasks"] = len(payload.get("tasks", []))
    return projected
