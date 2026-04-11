import json
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from agents.context import (
    clear_project_context,
    set_pipeline_run_id,
    set_pipeline_task_id,
    set_project_context,
)
from agents.agent_registry import stream_chat_with_intent
from agents.coding_tools import (
    ensure_chainlink_contracts,
    load_code_artifact,
)
from agents.db import async_session_factory
from agents.db.crud import (
    cancel_pending_followup_tasks,
    claim_next_pending_task,
    complete_pipeline_task_and_create_next,
    count_claimed_tasks_for_run,
    create_pipeline_evaluation,
    create_pipeline_human_gate,
    create_pipeline_run,
    create_pipeline_task,
    finalize_pipeline_run_and_enqueue_notification,
    get_current_plan as get_current_plan_row,
    get_deployment_for_task,
    get_next_retry_attempt,
    get_pipeline_run,
    get_pipeline_run_snapshot,
    get_pipeline_run_tasks,
    get_pipeline_task,
    get_successful_terminal_deployment,
    get_test_run_for_task,
    list_pipeline_evaluations,
    list_pipeline_human_gates,
    refresh_pipeline_run_snapshot,
    update_pipeline_run,
    update_pipeline_task,
)
from agents.pipeline_status import (
    build_pipeline_status_payload,
    derive_pipeline_status,
    project_pipeline_status_payload,
    serialize_evaluation,
    serialize_gate,
    serialize_run,
    serialize_task,
)
from agents.deployment_manifest import MANIFEST_PATH
from agents.pipeline_evaluations import (
    evaluate_code_generation,
    evaluate_deployment_prepare,
    evaluate_generated_tests,
    load_saved_manifest,
)
from agents.deployment_tools import (
    generate_foundry_deploy_script_direct,
    record_deployment,
    render_manifest_constructor_argument,
    run_foundry_deploy,
    save_deploy_artifact,
)
from agents.memory_manager import MemoryManager
from agents.pipeline_cancel import is_pipeline_cancelled
from agents.pipeline_context import (
    compact_execution_summary,
    default_expected_outputs,
    duration_ms,
    extract_plan_summary,
    standardize_task_context,
)
from agents.pipeline_specs import (
    DIRECT_TASK_TYPES,
    EMERGENCY_TASK_FUSE,
    TERMINAL_SUCCESS_TASK_TYPES,
    VALID_AGENTS,
    default_deployment_target_payload,
    retry_budget_for_key,
    retry_budget_key_for_task,
    stage_name_for_task,
)
from agents.testing_tools import run_foundry_tests
from agents.telegram_service import (
    build_terminal_notification_payload,
    terminal_event_type_for_status,
    terminal_notification_dedupe_key,
)
from agents.tracing import current_trace_id, start_span
from schemas.coding_schema import CodeArtifact
from schemas.deployment_schema import (
    DeploymentRecord,
    DeploymentStatus,
    DeploymentTarget,
    FoundryDeployRequest,
    FoundryDeployScriptGenerationRequest,
)

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

_TEST_INFRA_KEYWORDS = (
    "missing or unavailable dependencies",
    "file import callback not supported",
    "could not find source",
    "source \"@",
    "remapping",
    "chainlink",
    "aggregatorv3interface",
)
_CONTRACT_FAILURE_KEYWORDS = (
    "contracts/",
    "assertion",
    "revert",
    "panic(",
    "compiler run failed",
)
_DEPLOY_RETRY_KEYWORDS = (
    "rpc",
    "private key",
    "timed out",
    "timeout",
    "connection",
    "network",
    "nonce",
    "insufficient funds",
    "invalid sender",
    "env var",
    "fuji_",
)


def _update_plan_status(project_id: str, user_id: str, status: str) -> None:
    """Update the plan status in Letta/Neon (best-effort, non-blocking)."""
    try:
        mm = MemoryManager(user_id=user_id, project_id=project_id)
        mm.update_plan_status(status)
    except Exception as e:
        print(
            f"[Orchestrator] Warning: could not update plan status to '{status}': {e}"
        )


def _artifact_snapshot(mm: MemoryManager) -> dict:
    return {
        "coding": mm.get_agent_state("coding").get("artifacts", []),
        "testing": mm.get_agent_state("testing").get("artifacts", []),
        "deployment": mm.get_agent_state("deployment").get("artifacts", []),
    }


def _current_artifact_revision(mm: MemoryManager) -> int:
    return int(mm.get_agent_state("coding").get("latest_artifact_revision", 0) or 0)


def _current_plan_summary(mm: MemoryManager, task_context: dict | None = None) -> dict:
    if task_context and task_context.get("plan_summary"):
        return task_context["plan_summary"]
    planning_state = mm.get_agent_state("planning")
    summary = planning_state.get("plan_summary")
    if summary and summary.get("plan_contracts") is not None:
        return planning_state["plan_summary"]
    return extract_plan_summary(mm.get_plan())


def _serialize_task(task) -> dict:
    return serialize_task(task)


def _serialize_run(run) -> dict:
    return serialize_run(run)


def _serialize_gate(gate) -> dict:
    return serialize_gate(gate)


def _serialize_evaluation(evaluation) -> dict:
    return serialize_evaluation(evaluation)


def _derive_pipeline_status(run, tasks: list) -> tuple[str, str | None]:
    return derive_pipeline_status(run, tasks)


def _build_upstream_task(task, task_status: str, result_summary: str) -> dict:
    return {
        "task_id": str(task.id),
        "task_type": task.task_type,
        "assigned_to": task.assigned_to,
        "status": task_status,
        "result_summary": result_summary,
    }


def _project_name_for_notification(project_id: str, user_id: str) -> str | None:
    try:
        mm = MemoryManager(user_id=user_id, project_id=project_id)
        plan = mm.get_plan()
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    if isinstance(plan.get("project_name"), str) and plan.get("project_name"):
        return plan["project_name"]
    summary = extract_plan_summary(plan)
    if isinstance(summary, dict):
        project_name = summary.get("project_name")
        if isinstance(project_name, str) and project_name:
            return project_name
    return None


async def _finalize_pipeline_terminal_status(
    *,
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    status: str,
    terminal_deployment=None,
    failure_class: str | None = None,
    failure_reason: str | None = None,
) -> None:
    project_name = _project_name_for_notification(project_id, user_id)
    async with async_session_factory() as session:
        run = await get_pipeline_run(session, uuid.UUID(pipeline_run_id))
        if run is None:
            return
        payload = build_terminal_notification_payload(
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            project_name=project_name,
            terminal_status=status,
            deployment_target=run.deployment_target or default_deployment_target_payload(),
            terminal_deployment=terminal_deployment,
            failure_reason=failure_reason if status == "failed" else None,
            cancelled_reason=failure_reason if status == "cancelled" else None,
        )
        await finalize_pipeline_run_and_enqueue_notification(
            session,
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            status=status,
            completed_at=datetime.now(timezone.utc),
            terminal_deployment_id=(
                terminal_deployment.id
                if status == "completed" and terminal_deployment is not None
                else None
            ),
            failure_class=failure_class,
            failure_reason=failure_reason,
            notification_event_type=terminal_event_type_for_status(status),
            notification_payload=payload,
            notification_dedupe_key=terminal_notification_dedupe_key(
                pipeline_run_id, status
            ),
        )


async def _next_task_payload(
    pipeline_run_id: str,
    task,
    *,
    assigned_to: str,
    task_type: str,
    description: str,
    context: dict | None,
    artifact_revision: int,
    task_status: str,
    result_summary: str,
    failure_class: str | None = None,
    status: str = "pending",
    gate_id: str | None = None,
) -> dict:
    failure_context = None
    if task_status == "failed":
        failure_context = {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "result_summary": result_summary,
        }
    retry_budget_key = retry_budget_key_for_task(task_type)
    async with async_session_factory() as session:
        retry_attempt = await get_next_retry_attempt(
            session,
            uuid.UUID(pipeline_run_id),
            retry_budget_key,
        )
    return {
        "assigned_to": assigned_to,
        "task_type": task_type,
        "description": description,
        "parent_task_id": str(task.id),
        "sequence_index": 0,
        "artifact_revision": artifact_revision,
        "retry_budget_key": retry_budget_key,
        "retry_attempt": retry_attempt,
        "failure_class": failure_class,
        "status": status,
        "gate_id": gate_id,
        "context": standardize_task_context(
            context,
            plan_summary=(task.context or {}).get("plan_summary", {}),
            artifact_revision=artifact_revision,
            input_artifacts=(task.context or {}).get("input_artifacts", {}),
            upstream_task=_build_upstream_task(task, task_status, result_summary),
            failure_context=failure_context,
            expected_outputs=default_expected_outputs(task_type),
        ),
    }


def _select_primary_contract(plan: dict | None, coding_artifacts: list[dict]) -> tuple[str | None, dict | None]:
    if isinstance(plan, dict):
        contracts = plan.get("contracts") or []
        if contracts:
            first = contracts[0]
            if isinstance(first, dict):
                return first.get("name"), first
    for artifact in coding_artifacts:
        names = artifact.get("contract_names") or []
        if names:
            return names[0], None
    return None, None


def _normalize_constructor_default(input_spec: dict) -> str | None:
    raw_value = input_spec.get("default_value")
    if not isinstance(raw_value, str):
        return None
    normalized = raw_value.strip()
    if not normalized:
        return None
    input_type = str(input_spec.get("type", "")).lower()
    if input_type == "address" and normalized.lower() in {
        "deployer",
        "broadcaster",
        "broadcast",
    }:
        return "deployer"
    return normalized


def _default_constructor_literal(input_spec: dict | str) -> str:
    if isinstance(input_spec, dict):
        explicit_default = _normalize_constructor_default(input_spec)
        if explicit_default:
            return explicit_default
        input_type = str(input_spec.get("type", ""))
    else:
        input_type = str(input_spec)

    lowered = input_type.lower()
    if lowered == "string":
        return '"PartyHat"'
    if lowered == "bool":
        return "false"
    if lowered == "address":
        return "deployer"
    if lowered.startswith("bytes"):
        return 'hex""'
    if lowered.endswith("[]"):
        base = lowered[:-2]
        return f"new {base}[](0)"
    if lowered.startswith("uint") or lowered.startswith("int"):
        return "0"
    return "0"


def _constructor_literals(contract_plan: dict | None) -> list[str]:
    if not isinstance(contract_plan, dict):
        return []
    constructor = contract_plan.get("constructor") or {}
    inputs = constructor.get("inputs") or []
    return [
        _default_constructor_literal(item)
        for item in inputs
        if isinstance(item, dict)
    ]


def _constructor_literals_from_manifest(contract_manifest) -> list[str]:
    literals: list[str] = []
    for item in getattr(contract_manifest, "constructor_args_schema", []) or []:
        literals.append(render_manifest_constructor_argument(item))
    return literals


def _deployment_constraints(contract_plan: dict | None) -> list[str]:
    constraints = [
        (
            "For any address-valued deployment parameter without an explicit "
            "wallet, use deployer derived from FUJI_PRIVATE_KEY instead of "
            "address(0)."
        )
    ]
    if not isinstance(contract_plan, dict):
        return constraints

    constructor = contract_plan.get("constructor") or {}
    inputs = constructor.get("inputs") or []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")).lower() != "address":
            continue
        input_name = str(item.get("name") or "addressArg")
        default_value = _default_constructor_literal(item)
        constraints.append(
            f"Constructor address input {input_name} should use {default_value}."
        )
    return constraints


def _summarize_artifact_metadata(artifacts: list[dict]) -> list[dict]:
    summary: list[dict] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        summary.append(
            {
                "path": artifact.get("path"),
                "language": artifact.get("language"),
                "contract_names": artifact.get("contract_names"),
                "plan_contract_ids": artifact.get("plan_contract_ids"),
                "description": artifact.get("description"),
            }
        )
    return summary


def _slim_task_context(task_context: dict | None) -> dict:
    if not isinstance(task_context, dict):
        return {}

    slim: dict[str, object] = {}
    for key in (
        "artifact_revision",
        "script_path",
        "contract_name",
        "plan_contract_id",
        "plan_contract_ids",
        "expected_outputs",
        "failure_context",
        "upstream_task",
    ):
        value = task_context.get(key)
        if value is not None:
            slim[key] = value

    if isinstance(task_context.get("plan_summary"), dict):
        plan_summary = task_context["plan_summary"]
        slim["plan_summary"] = {
            "project_name": plan_summary.get("project_name"),
            "erc_standard": plan_summary.get("erc_standard"),
            "contract_names": plan_summary.get("contract_names"),
            "plan_contracts": plan_summary.get("plan_contracts"),
            "key_constraints": plan_summary.get("key_constraints"),
        }

    if isinstance(task_context.get("input_artifacts"), dict):
        slim["input_artifacts"] = {
            key: _summarize_artifact_metadata(value or [])
            for key, value in task_context["input_artifacts"].items()
            if key in {"coding", "testing", "deployment"}
        }

    for key, value in task_context.items():
        if key not in slim and key not in {"input_artifacts", "plan_summary"}:
            slim[key] = value

    return slim


def _load_contract_sources(
    coding_artifacts: list[dict],
    *,
    target_plan_contract_ids: list[str] | None = None,
    target_contract_names: list[str] | None = None,
) -> str:
    target_ids = {value for value in (target_plan_contract_ids or []) if value}
    target_names = {value for value in (target_contract_names or []) if value}
    chunks: list[str] = []
    for artifact in coding_artifacts:
        path = artifact.get("path")
        if not path or not str(path).startswith("contracts/"):
            continue
        artifact_ids = {
            value for value in (artifact.get("plan_contract_ids") or []) if value
        }
        artifact_names = {
            value for value in (artifact.get("contract_names") or []) if value
        }
        if target_ids and not (artifact_ids & target_ids):
            if not target_names or not (artifact_names & target_names):
                continue
        elif target_names and not (artifact_names & target_names):
            continue
        loaded = load_code_artifact.func(path)
        if isinstance(loaded, dict) and loaded.get("code"):
            chunks.append(f"// {path}\n{loaded['code']}")
    return "\n\n".join(chunks)


def _latest_deploy_script(snapshot: dict) -> str | None:
    deployment_artifacts = snapshot.get("deployment") or []
    for artifact in reversed(deployment_artifacts):
        path = artifact.get("path")
        if path and str(path).startswith("script/"):
            return path
    return None


def _should_retry_for_chainlink(text: str) -> bool:
    lowered = text.lower()
    return "chainlink" in lowered or "aggregatorv3interface" in lowered


def _classify_test_failure(output: str) -> tuple[str, str, str, str]:
    lowered = output.lower()
    if any(keyword in lowered for keyword in _TEST_INFRA_KEYWORDS):
        return (
            "testing",
            "testing.run_tests",
            "Transient test infrastructure issue detected; retry test execution.",
            "transient_infra",
        )
    if any(keyword in lowered for keyword in _CONTRACT_FAILURE_KEYWORDS):
        return (
            "coding",
            "coding.generate_contracts",
            "Contract logic or compile issue detected in test run.",
            "contract_logic",
        )
    return (
        "testing",
        "testing.generate_tests",
        "Generated tests require remediation before execution can pass.",
        "artifact_contract_mismatch",
    )


def _classify_deploy_failure(
    output: str,
    *,
    allow_retry: bool,
) -> tuple[str, str, str, str]:
    lowered = output.lower()
    if allow_retry and (
        any(keyword in lowered for keyword in _DEPLOY_RETRY_KEYWORDS)
        or any(keyword in lowered for keyword in _TEST_INFRA_KEYWORDS)
    ):
        return (
            "deployment",
            "deployment.retry_deploy",
            "Deployment infrastructure issue detected; retry deployment.",
            "transient_infra",
        )
    return (
        "coding",
        "coding.generate_contracts",
        "Deployment failure suggests a contract or deployment script issue.",
        "contract_logic",
    )


def _retry_available(task) -> bool:
    retry_budget_key = getattr(task, "retry_budget_key", None) or retry_budget_key_for_task(
        task.task_type
    )
    return int(getattr(task, "retry_attempt", 0) or 0) < retry_budget_for_key(
        retry_budget_key
    )


async def _record_pipeline_evaluation(
    *,
    project_id: str,
    pipeline_run_id: str,
    task,
    stage: str,
    evaluation: dict,
):
    async with async_session_factory() as session:
        return await create_pipeline_evaluation(
            session,
            project_id=uuid.UUID(project_id),
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            pipeline_task_id=task.id if task is not None else None,
            stage=stage,
            evaluation_type=evaluation["evaluation_type"],
            blocking=bool(evaluation.get("blocking", False)),
            status=evaluation["status"],
            summary=evaluation["summary"],
            details_json=evaluation.get("details"),
            artifact_revision=int(evaluation.get("artifact_revision", 0) or 0),
            trace_id=current_trace_id(),
        )


async def _create_gate_for_task(
    *,
    project_id: str,
    pipeline_run_id: str,
    task,
    gate_type: str,
    requested_reason: str,
    requested_payload: dict | None = None,
    evaluation_id=None,
) -> object:
    async with async_session_factory() as session:
        gate = await create_pipeline_human_gate(
            session,
            project_id=uuid.UUID(project_id),
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            pipeline_task_id=task.id if task is not None else None,
            evaluation_id=evaluation_id.id if evaluation_id is not None else None,
            gate_type=gate_type,
            requested_payload=requested_payload,
            requested_reason=requested_reason,
            requested_by="system",
            trace_id=current_trace_id(),
        )
        await update_pipeline_run(
            session,
            uuid.UUID(pipeline_run_id),
            status="waiting_for_approval",
            paused_at=task.completed_at or task.claimed_at,
            current_stage=stage_name_for_task(task.task_type, task.assigned_to),
            current_task_id=task.id,
            failure_class="human_gate",
            failure_reason=requested_reason,
        )
        return gate


async def _enqueue_task(
    *,
    project_id: str,
    pipeline_run_id: str,
    payload: dict,
    created_by: str,
):
    async with async_session_factory() as session:
        return await create_pipeline_task(
            session,
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            project_id=uuid.UUID(project_id),
            assigned_to=payload["assigned_to"],
            created_by=created_by,
            task_type=payload["task_type"],
            description=payload["description"],
            context=payload.get("context"),
            parent_task_id=uuid.UUID(payload["parent_task_id"])
            if payload.get("parent_task_id")
            else None,
            sequence_index=payload.get("sequence_index", 0),
            artifact_revision=payload.get("artifact_revision", 0),
            depends_on_task_ids=payload.get("depends_on_task_ids"),
            retry_budget_key=payload.get("retry_budget_key"),
            retry_attempt=payload.get("retry_attempt", 0),
            failure_class=payload.get("failure_class"),
            gate_id=uuid.UUID(payload["gate_id"])
            if payload.get("gate_id")
            else None,
            status=payload.get("status", "pending"),
        )


async def _validate_execution_result(
    pipeline_run_id: str,
    task,
) -> tuple[bool, str | None, dict | None]:
    async with async_session_factory() as session:
        if task.task_type == "testing.run_tests":
            row = await get_test_run_for_task(
                session,
                uuid.UUID(pipeline_run_id),
                task.id,
            )
        elif task.task_type in TERMINAL_SUCCESS_TASK_TYPES:
            row = await get_deployment_for_task(
                session,
                uuid.UUID(pipeline_run_id),
                task.id,
            )
        else:
            return True, None, None

    if row is None:
        task_summary = str(getattr(task, "result_summary", "") or "").strip()
        if (
            task.task_type in TERMINAL_SUCCESS_TASK_TYPES
            and task.status == "failed"
            and "authoritative deployment record could not be persisted"
            in task_summary.lower()
        ):
            return False, task_summary, None
        return (
            False,
            f"Task '{task.task_type}' finished without an authoritative execution record.",
            None,
        )

    entry = {
        "status": getattr(row, "status", None),
        "exit_code": getattr(row, "exit_code", None),
        "stdout_path": getattr(row, "stdout_path", None),
        "stderr_path": getattr(row, "stderr_path", None),
    }
    exit_code = entry.get("exit_code")
    if task.status == "completed" and exit_code != 0:
        return (
            False,
            f"Task '{task.task_type}' was marked completed but exit_code was {exit_code}.",
            entry,
        )
    if task.status == "failed" and exit_code == 0:
        return (
            False,
            f"Task '{task.task_type}' was marked failed but exit_code was 0.",
            entry,
        )
    return True, None, entry


async def _finalize_direct_task(
    *,
    project_id: str,
    pipeline_run_id: str,
    task,
    task_status: str,
    result_summary: str,
    next_tasks: list[dict],
) -> None:
    async with async_session_factory() as session:
        await complete_pipeline_task_and_create_next(
            session,
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            project_id=uuid.UUID(project_id),
            task_id=task.id,
            task_status=task_status,
            result_summary=result_summary,
            next_tasks=next_tasks,
            created_by=task.assigned_to,
        )


async def _handle_testing_run(task, project_id: str, user_id: str, pipeline_run_id: str) -> list[dict]:
    events = [
        {"type": "tool_call", "stage": "testing", "tool": "run_foundry_tests", "args": "[]"},
    ]
    result = run_foundry_tests.func()
    if "error" in result:
        combined_error = result["error"]
    else:
        combined_error = f"{result.get('stdout', '')}\n{result.get('stderr', '')}"

    if _should_retry_for_chainlink(combined_error):
        events.append(
            {
                "type": "tool_call",
                "stage": "testing",
                "tool": "ensure_chainlink_contracts",
                "args": "{}",
            }
        )
        ensure_chainlink_contracts.func()
        result = run_foundry_tests.func()

    mm = MemoryManager(user_id=user_id, project_id=project_id)
    snapshot = _artifact_snapshot(mm)
    artifact_revision = task.artifact_revision

    if result.get("cancelled"):
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="cancelled",
            result_summary="Testing execution cancelled.",
            next_tasks=[],
        )
        return events

    if result.get("success"):
        result_summary = compact_execution_summary(
            result.get("exit_code", 0),
            result.get("stdout", ""),
            result.get("stderr", ""),
        )
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="deployment",
            task_type="deployment.prepare_script",
            description="Prepare the Foundry deployment script for Avalanche Fuji.",
            context=None,
            artifact_revision=artifact_revision,
            task_status="completed",
            result_summary=result_summary,
        )
        next_task["context"]["input_artifacts"] = snapshot
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="completed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events

    combined = f"{result.get('error', '')}\n{result.get('stdout', '')}\n{result.get('stderr', '')}"
    next_agent, next_task_type, reason, failure_class = _classify_test_failure(combined)
    result_summary = reason
    context = {
        "failure_context": {
            "summary": compact_execution_summary(
                result.get("exit_code", 1),
                result.get("stdout", ""),
                result.get("stderr", ""),
            ),
            "stdout_path": result.get("stdout_path"),
            "stderr_path": result.get("stderr_path"),
            "exit_code": result.get("exit_code"),
        }
    }

    if next_task_type == task.task_type and not _retry_available(task):
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[],
        )
        gate = await _create_gate_for_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            gate_type="override",
            requested_reason=reason,
            requested_payload=context,
        )
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to=next_agent,
            task_type=next_task_type,
            description=reason,
            context=context,
            artifact_revision=artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="human_gate",
            status="waiting_for_approval",
            gate_id=str(gate.id),
        )
        next_task["context"]["input_artifacts"] = snapshot
        await _enqueue_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            payload=next_task,
            created_by=task.assigned_to,
        )
        return events

    next_task = await _next_task_payload(
        pipeline_run_id,
        task,
        assigned_to=next_agent,
        task_type=next_task_type,
        description=reason,
        context=context,
        artifact_revision=artifact_revision,
        task_status="failed",
        result_summary=result_summary,
        failure_class=failure_class,
    )
    next_task["context"]["input_artifacts"] = snapshot
    await _finalize_direct_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        task_status="failed",
        result_summary=result_summary,
        next_tasks=[next_task],
    )
    return events


async def _handle_prepare_script(task, project_id: str, user_id: str, pipeline_run_id: str) -> list[dict]:
    events = [
        {
            "type": "tool_call",
            "stage": "deployment",
            "tool": "generate_foundry_deploy_script_direct",
            "args": "{}",
        }
    ]
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    snapshot = _artifact_snapshot(mm)
    manifest, manifest_error = load_saved_manifest(project_id)
    if manifest is None:
        evaluation = {
            "status": "failed",
            "blocking": True,
            "evaluation_type": "deployment_prepare",
            "summary": "Deployment manifest is missing or invalid.",
            "details": {"issues": [manifest_error or "manifest missing"]},
            "artifact_revision": task.artifact_revision,
        }
        await _record_pipeline_evaluation(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            stage="deployment",
            evaluation=evaluation,
        )
        result_summary = evaluation["summary"]
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description="Regenerate code artifacts and deployment manifest so deployment preparation can proceed.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="artifact_contract_mismatch",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events

    primary_contract = next(
        (
            contract
            for contract in manifest.contracts
            if contract.role == "primary_deployable"
        ),
        None,
    )
    if primary_contract is None:
        evaluation = {
            "status": "failed",
            "blocking": True,
            "evaluation_type": "deployment_prepare",
            "summary": "Deployment manifest has no primary_deployable contract.",
            "details": {"issues": ["Missing primary_deployable contract in manifest."]},
            "artifact_revision": task.artifact_revision,
        }
        await _record_pipeline_evaluation(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            stage="deployment",
            evaluation=evaluation,
        )
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description=evaluation["summary"],
            context={"failure_context": {"summary": evaluation["summary"]}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=evaluation["summary"],
            failure_class="artifact_contract_mismatch",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=evaluation["summary"],
            next_tasks=[next_task],
        )
        return events

    contract_name = primary_contract.name
    plan_contract_id = primary_contract.plan_contract_id
    all_manifest_contract_ids = [contract.plan_contract_id for contract in manifest.contracts]
    script_name = f"Deploy{contract_name}"
    script_path = f"script/{script_name}.s.sol"
    serialized_manifest = manifest.model_dump()
    generation = generate_foundry_deploy_script_direct(
        FoundryDeployScriptGenerationRequest(
            goal=f"Deploy {contract_name} to Avalanche Fuji.",
            contract_name=contract_name,
            script_name=script_name,
            constraints=_deployment_constraints(None),
            plan_summary=json.dumps(_current_plan_summary(mm, task.context), indent=2),
            contract_sources=_load_contract_sources(
                snapshot.get("coding", []),
                target_plan_contract_ids=all_manifest_contract_ids,
            ),
            deployment_manifest=serialized_manifest,
        )
    )
    if generation.get("error"):
        result_summary = generation["error"]
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description="Fix the contract or artifact state so deployment script generation can succeed.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="contract_logic",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events

    events.append(
        {
            "type": "tool_call",
            "stage": "deployment",
            "tool": "save_deploy_artifact",
            "args": script_path,
        }
    )
    save_result = save_deploy_artifact.func(
        CodeArtifact(
            path=script_path,
            language="solidity",
            description=f"Foundry deployment script for {contract_name}",
            contract_names=[script_name],
            plan_contract_ids=all_manifest_contract_ids,
            code=generation.get("generated_script", ""),
        )
    )
    if save_result.get("error"):
        result_summary = save_result["error"]
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description="Fix the contract or storage state so deployment script persistence can succeed.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="contract_logic",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events

    evaluation = evaluate_deployment_prepare(project_id, user_id, script_path)
    evaluation_row = await _record_pipeline_evaluation(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        stage="deployment",
        evaluation=evaluation,
    )

    if evaluation["status"] != "passed":
        result_summary = evaluation["summary"]
        if _retry_available(task):
            next_task = await _next_task_payload(
                pipeline_run_id,
                task,
                assigned_to="deployment",
                task_type="deployment.prepare_script",
                description="Regenerate the deployment script so it matches the authoritative deployment manifest.",
                context={"failure_context": {"summary": result_summary}},
                artifact_revision=task.artifact_revision,
                task_status="failed",
                result_summary=result_summary,
                failure_class="evaluation_failed",
            )
            await _finalize_direct_task(
                project_id=project_id,
                pipeline_run_id=pipeline_run_id,
                task=task,
                task_status="failed",
                result_summary=result_summary,
                next_tasks=[next_task],
            )
            return events

        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[],
        )
        gate = await _create_gate_for_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            gate_type="override",
            requested_reason=result_summary,
            requested_payload=evaluation.get("details"),
            evaluation_id=evaluation_row,
        )
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="deployment",
            task_type="deployment.prepare_script",
            description="Operator override requested for deployment preparation remediation.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="human_gate",
            status="waiting_for_approval",
            gate_id=str(gate.id),
        )
        await _enqueue_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            payload=next_task,
            created_by=task.assigned_to,
        )
        return events

    gate = await _create_gate_for_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        gate_type="pre_deploy",
        requested_reason="Deployment script is ready. Awaiting operator approval before on-chain deployment.",
        requested_payload={
            "script_path": script_path,
            "manifest_path": MANIFEST_PATH,
            "contract_name": contract_name,
            "plan_contract_id": plan_contract_id,
            "plan_contract_ids": all_manifest_contract_ids,
        },
        evaluation_id=evaluation_row,
    )
    result_summary = f"Prepared deployment script at {script_path}; awaiting deploy approval."
    next_task = await _next_task_payload(
        pipeline_run_id,
        task,
        assigned_to="deployment",
        task_type="deployment.execute_deploy",
        description=f"Execute the prepared deployment script {script_path} on Avalanche Fuji.",
        context={
            "script_path": script_path,
            "script_name": script_name,
            "contract_name": contract_name,
            "plan_contract_id": plan_contract_id,
            "plan_contract_ids": all_manifest_contract_ids,
        },
        artifact_revision=task.artifact_revision,
        task_status="completed",
        result_summary=result_summary,
        failure_class="human_gate",
        status="waiting_for_approval",
        gate_id=str(gate.id),
    )
    next_task["context"]["input_artifacts"] = _artifact_snapshot(mm)
    await _finalize_direct_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        task_status="completed",
        result_summary=result_summary,
        next_tasks=[next_task],
    )
    return events


async def _handle_execute_deploy(task, project_id: str, user_id: str, pipeline_run_id: str) -> list[dict]:
    stage = "deployment"
    events = [
        {"type": "tool_call", "stage": stage, "tool": "run_foundry_deploy", "args": "{}"},
    ]
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    snapshot = _artifact_snapshot(mm)
    task_context = task.context or {}
    script_path = task_context.get("script_path") or _latest_deploy_script(snapshot)
    plan_contract_id = task_context.get("plan_contract_id")
    if not script_path:
        result_summary = "No deployment script artifact available to execute."
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description=result_summary,
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="artifact_contract_mismatch",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events
    if not plan_contract_id:
        result_summary = "Deployment task is missing plan_contract_id."
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description=result_summary,
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
            failure_class="artifact_contract_mismatch",
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=result_summary,
            next_tasks=[next_task],
        )
        return events

    manifest, _ = load_saved_manifest(project_id)
    target = DeploymentTarget.model_validate(
        manifest.deployment_target.model_dump()
        if manifest is not None
        else default_deployment_target_payload()
    )
    target_payload = target.model_dump()

    deploy_result = run_foundry_deploy.func(
        FoundryDeployRequest(
            script_path=script_path,
            network=target_payload["network"],
            chain_id=target_payload.get("chain_id") or 43113,
            rpc_url_env_var=target_payload.get("rpc_url_env_var") or "FUJI_RPC_URL",
            private_key_env_var=target_payload.get("private_key_env_var")
            or "FUJI_PRIVATE_KEY",
            plan_contract_id=plan_contract_id,
            deployment_manifest=manifest.model_dump() if manifest is not None else None,
            quiet_output=True,
        )
    )
    combined = (
        f"{deploy_result.get('error', '')}\n"
        f"{deploy_result.get('stdout', '')}\n"
        f"{deploy_result.get('stderr', '')}"
    )
    if _should_retry_for_chainlink(combined):
        events.append(
            {
                "type": "tool_call",
                "stage": stage,
                "tool": "ensure_chainlink_contracts",
                "args": "{}",
            }
        )
        ensure_chainlink_contracts.func()
        deploy_result = run_foundry_deploy.func(
            FoundryDeployRequest(
                script_path=script_path,
                network=target_payload["network"],
                chain_id=target_payload.get("chain_id") or 43113,
                rpc_url_env_var=target_payload.get("rpc_url_env_var") or "FUJI_RPC_URL",
                private_key_env_var=target_payload.get("private_key_env_var")
                or "FUJI_PRIVATE_KEY",
                plan_contract_id=plan_contract_id,
                deployment_manifest=manifest.model_dump() if manifest is not None else None,
                quiet_output=True,
            )
        )
    if deploy_result.get("success"):
        events.append(
            {
                "type": "tool_call",
                "stage": stage,
                "tool": "record_deployment",
                "args": "{}",
            }
        )
        record_deployment.func(
            DeploymentRecord(
                target=target,
                tx_hash=deploy_result.get("tx_hash"),
                status=DeploymentStatus.SUCCESS,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=str(task.id),
                deployed_address=deploy_result.get("deployed_address"),
                contract_name=task_context.get("contract_name"),
                plan_contract_id=plan_contract_id,
                script_path=script_path,
                chain_id=target_payload.get("chain_id") or 43113,
                command=deploy_result.get("command"),
                stdout_path=deploy_result.get("stdout_path"),
                stderr_path=deploy_result.get("stderr_path"),
                exit_code=deploy_result.get("exit_code"),
                deployed_contracts=deploy_result.get("deployed_contracts") or [],
                executed_calls=deploy_result.get("executed_calls") or [],
            )
        )
        result_summary = compact_execution_summary(
            deploy_result.get("exit_code", 0),
            deploy_result.get("stdout", ""),
            deploy_result.get("stderr", ""),
        )
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="completed",
            result_summary=result_summary,
            next_tasks=[],
        )
        return events

    if deploy_result.get("cancelled"):
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="cancelled",
            result_summary="Deployment execution cancelled.",
            next_tasks=[],
        )
        return events

    failure_summary = (
        str(deploy_result.get("error") or "").strip()
        or compact_execution_summary(
            deploy_result.get("exit_code", 1),
            deploy_result.get("stdout", ""),
            deploy_result.get("stderr", ""),
        )
    )
    if deploy_result.get("authoritative_record_error"):
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=failure_summary,
            next_tasks=[],
        )
        return events

    events.append(
        {
            "type": "tool_call",
            "stage": stage,
            "tool": "record_deployment",
            "args": "{}",
        }
    )
    record_deployment.func(
        DeploymentRecord(
            target=target,
            tx_hash=deploy_result.get("tx_hash"),
            status=DeploymentStatus.FAILED,
            pipeline_run_id=pipeline_run_id,
            pipeline_task_id=str(task.id),
            deployed_address=deploy_result.get("deployed_address"),
            contract_name=task_context.get("contract_name"),
            plan_contract_id=plan_contract_id,
            script_path=script_path,
            chain_id=target_payload.get("chain_id") or 43113,
            command=deploy_result.get("command"),
            stdout_path=deploy_result.get("stdout_path"),
            stderr_path=deploy_result.get("stderr_path"),
            exit_code=deploy_result.get("exit_code"),
            deployed_contracts=deploy_result.get("deployed_contracts") or [],
            executed_calls=deploy_result.get("executed_calls") or [],
        )
    )

    next_agent, next_task_type, reason, failure_class = _classify_deploy_failure(
        f"{deploy_result.get('error', '')}\n{deploy_result.get('stdout', '')}\n{deploy_result.get('stderr', '')}",
        allow_retry=task.task_type != "deployment.retry_deploy",
    )
    context = {
        "script_path": script_path,
        "contract_name": task_context.get("contract_name"),
        "plan_contract_id": plan_contract_id,
        "plan_contract_ids": task_context.get("plan_contract_ids"),
        "failure_context": {
            "summary": failure_summary,
            "stdout_path": deploy_result.get("stdout_path"),
            "stderr_path": deploy_result.get("stderr_path"),
            "exit_code": deploy_result.get("exit_code"),
        },
    }
    if next_task_type == "deployment.retry_deploy" and not _retry_available(task):
        await _finalize_direct_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            task_status="failed",
            result_summary=failure_summary,
            next_tasks=[],
        )
        gate = await _create_gate_for_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            task=task,
            gate_type="override",
            requested_reason=reason,
            requested_payload=context,
        )
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to=next_agent,
            task_type=next_task_type,
            description=reason,
            context=context,
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=reason,
            failure_class="human_gate",
            status="waiting_for_approval",
            gate_id=str(gate.id),
        )
        next_task["context"]["input_artifacts"] = snapshot
        await _enqueue_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            payload=next_task,
            created_by=task.assigned_to,
        )
        return events

    next_task = await _next_task_payload(
        pipeline_run_id,
        task,
        assigned_to=next_agent,
        task_type=next_task_type,
        description=reason,
        context=context,
        artifact_revision=task.artifact_revision,
        task_status="failed",
        result_summary=reason,
        failure_class=failure_class,
    )
    next_task["context"]["input_artifacts"] = snapshot
    await _finalize_direct_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        task_status="failed",
        result_summary=failure_summary,
        next_tasks=[next_task],
    )
    return events


async def _execute_direct_task(
    task,
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
) -> list[dict]:
    if task.task_type == "testing.run_tests":
        return await _handle_testing_run(task, project_id, user_id, pipeline_run_id)
    if task.task_type == "deployment.prepare_script":
        return await _handle_prepare_script(task, project_id, user_id, pipeline_run_id)
    if task.task_type in {"deployment.execute_deploy", "deployment.retry_deploy"}:
        return await _handle_execute_deploy(task, project_id, user_id, pipeline_run_id)
    return []


async def _postprocess_task(
    *,
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    task,
) -> list[dict]:
    stage = stage_name_for_task(task.task_type, task.assigned_to)
    events: list[dict] = []

    if task.task_type == "coding.generate_contracts" and task.status == "completed":
        evaluation = evaluate_code_generation(project_id, user_id)
    elif task.task_type == "testing.generate_tests" and task.status == "completed":
        evaluation = evaluate_generated_tests(project_id, user_id)
    elif task.task_type == "testing.run_tests":
        evaluation = {
            "status": "passed" if task.status == "completed" else "failed",
            "blocking": True,
            "evaluation_type": "test_execution",
            "summary": (
                "Foundry test execution produced a passing authoritative test_run."
                if task.status == "completed"
                else (task.result_summary or "Foundry test execution failed.")
            ),
            "details": {"task_status": task.status},
            "artifact_revision": task.artifact_revision,
        }
    elif task.task_type in TERMINAL_SUCCESS_TASK_TYPES:
        evaluation = {
            "status": "passed" if task.status == "completed" else "failed",
            "blocking": True,
            "evaluation_type": "deployment_execution",
            "summary": (
                "Deployment execution satisfied terminal success criteria."
                if task.status == "completed"
                else (task.result_summary or "Deployment execution failed.")
            ),
            "details": {"task_status": task.status},
            "artifact_revision": task.artifact_revision,
        }
    else:
        return events

    evaluation_row = await _record_pipeline_evaluation(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        stage=stage,
        evaluation=evaluation,
    )
    events.append(
        {
            "type": "evaluation",
            "stage": stage,
            "task_id": str(task.id),
            "evaluation_type": evaluation["evaluation_type"],
            "status": evaluation["status"],
            "blocking": evaluation.get("blocking", False),
            "summary": evaluation["summary"],
        }
    )

    if evaluation["status"] == "passed" or not evaluation.get("blocking", False):
        return events

    if task.task_type in DIRECT_TASK_TYPES:
        return events

    async with async_session_factory() as session:
        await cancel_pending_followup_tasks(
            session,
            uuid.UUID(pipeline_run_id),
            task.id,
        )
        await update_pipeline_task(
            session,
            task.id,
            status="failed",
            result_summary=evaluation["summary"],
            failure_class="evaluation_failed",
        )

    if _retry_available(task):
        next_task = await _next_task_payload(
            pipeline_run_id,
            task,
            assigned_to=task.assigned_to,
            task_type=task.task_type,
            description=f"Remediate blocking evaluation failure for {task.task_type}.",
            context={"failure_context": {"summary": evaluation["summary"]}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=evaluation["summary"],
            failure_class="evaluation_failed",
        )
        await _enqueue_task(
            project_id=project_id,
            pipeline_run_id=pipeline_run_id,
            payload=next_task,
            created_by="orchestrator",
        )
        return events

    gate = await _create_gate_for_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        gate_type="override",
        requested_reason=evaluation["summary"],
        requested_payload=evaluation.get("details"),
        evaluation_id=evaluation_row,
    )
    next_task = await _next_task_payload(
        pipeline_run_id,
        task,
        assigned_to=task.assigned_to,
        task_type=task.task_type,
        description=f"Operator override requested for {task.task_type} after blocking evaluation failure.",
        context={"failure_context": {"summary": evaluation["summary"]}},
        artifact_revision=task.artifact_revision,
        task_status="failed",
        result_summary=evaluation["summary"],
        failure_class="human_gate",
        status="waiting_for_approval",
        gate_id=str(gate.id),
    )
    await _enqueue_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        payload=next_task,
        created_by="orchestrator",
    )
    return events


async def run_autonomous_pipeline(
    project_id: str,
    user_id: str,
    pipeline_run_id: str | None = None,
) -> AsyncIterator[dict]:
    set_project_context(project_id, user_id)

    if pipeline_run_id is None:
        mm = MemoryManager(user_id=user_id, project_id=project_id)
        initial_context = standardize_task_context(
            None,
            plan_summary=_current_plan_summary(mm),
            artifact_revision=_current_artifact_revision(mm),
            input_artifacts=_artifact_snapshot(mm),
            expected_outputs=default_expected_outputs(INITIAL_TASK["task_type"]),
        )
        try:
            with start_span(
                "pipeline.run",
                {"project_id": project_id, "task_type": "pipeline"},
            ):
                trace_id = current_trace_id()
                async with async_session_factory() as session:
                    plan_row = await get_current_plan_row(session, uuid.UUID(project_id))
                    run = await create_pipeline_run(
                        session,
                        project_id=uuid.UUID(project_id),
                        user_id=uuid.UUID(user_id),
                        plan_id=plan_row.id if plan_row else None,
                        deployment_target=DeploymentTarget.model_validate(
                            (
                                (plan_row.plan_data or {}).get("deployment_target")
                                if plan_row
                                else default_deployment_target_payload()
                            )
                        ).model_dump(),
                        trace_id=trace_id,
                    )
                    pipeline_run_id = str(run.id)
                    initial_retry_key = retry_budget_key_for_task(
                        INITIAL_TASK["task_type"]
                    )
                    initial_retry_attempt = await get_next_retry_attempt(
                        session,
                        uuid.UUID(pipeline_run_id),
                        initial_retry_key,
                    )
                    await create_pipeline_task(
                        session,
                        pipeline_run_id=uuid.UUID(pipeline_run_id),
                        project_id=uuid.UUID(project_id),
                        assigned_to=INITIAL_TASK["assigned_to"],
                        created_by="orchestrator",
                        task_type=INITIAL_TASK["task_type"],
                        description=INITIAL_TASK["description"],
                        context=initial_context,
                        artifact_revision=initial_context["artifact_revision"],
                        sequence_index=0,
                        retry_budget_key=initial_retry_key,
                        retry_attempt=initial_retry_attempt,
                    )
                    await update_pipeline_run(
                        session,
                        uuid.UUID(pipeline_run_id),
                        status="running",
                        started_at=datetime.now(timezone.utc),
                        current_stage="coding",
                    )
        except Exception as e:
            if pipeline_run_id is not None:
                await _finalize_pipeline_terminal_status(
                    project_id=project_id,
                    user_id=user_id,
                    pipeline_run_id=pipeline_run_id,
                    status="failed",
                    failure_class="unknown",
                    failure_reason=f"Could not seed first task: {e}",
                )
            yield {
                "type": "pipeline_error",
                "stage": "init",
                "error": f"Could not seed first task: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            clear_project_context()
            return

        yield {
            "type": "pipeline_start",
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
        }
    else:
        async with async_session_factory() as session:
            run = await get_pipeline_run(session, uuid.UUID(pipeline_run_id))
            if run is None:
                yield {
                    "type": "pipeline_error",
                    "stage": "resume",
                    "error": f"Pipeline run '{pipeline_run_id}' was not found.",
                }
                clear_project_context()
                return
            if run.status == "waiting_for_approval":
                gates = await list_pipeline_human_gates(session, uuid.UUID(pipeline_run_id))
                if any(gate.status == "pending" for gate in gates):
                    yield {
                        "type": "pipeline_waiting_for_approval",
                        "pipeline_run_id": pipeline_run_id,
                    }
                    clear_project_context()
                    return
            await update_pipeline_run(
                session,
                uuid.UUID(pipeline_run_id),
                status="running",
                resumed_at=datetime.now(timezone.utc),
            )
        yield {
            "type": "pipeline_resumed",
            "pipeline_run_id": pipeline_run_id,
            "project_id": project_id,
        }

    set_pipeline_run_id(pipeline_run_id)

    while True:
        if is_pipeline_cancelled(pipeline_run_id):
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="cancelled",
                failure_class="cancelled",
                failure_reason="Cancellation requested by operator.",
            )
            yield {
                "type": "pipeline_cancelled",
                "pipeline_run_id": pipeline_run_id,
            }
            _update_plan_status(project_id, user_id, "ready")
            break

        set_project_context(project_id, user_id)
        set_pipeline_run_id(pipeline_run_id)

        try:
            async with async_session_factory() as session:
                claimed_count = await count_claimed_tasks_for_run(
                    session, uuid.UUID(pipeline_run_id)
                )
                if claimed_count >= EMERGENCY_TASK_FUSE:
                    await _finalize_pipeline_terminal_status(
                        project_id=project_id,
                        user_id=user_id,
                        pipeline_run_id=pipeline_run_id,
                        status="failed",
                        failure_class="unknown",
                        failure_reason=(
                            f"Run exceeded the emergency claimed-task fuse ({EMERGENCY_TASK_FUSE})."
                        ),
                    )
                    yield {
                        "type": "pipeline_error",
                        "stage": "orchestrator",
                        "pipeline_run_id": pipeline_run_id,
                        "error": (
                            f"Run exceeded the emergency claimed-task fuse ({EMERGENCY_TASK_FUSE})."
                        ),
                    }
                    break

                task = await claim_next_pending_task(session, uuid.UUID(pipeline_run_id))
                if task is not None:
                    await update_pipeline_run(
                        session,
                        uuid.UUID(pipeline_run_id),
                        status="running",
                        current_stage=stage_name_for_task(
                            task.task_type, task.assigned_to
                        ),
                        current_task_id=task.id,
                    )
        except Exception as e:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=f"DB error reading tasks: {e}",
            )
            yield {
                "type": "pipeline_error",
                "stage": "dispatch",
                "error": f"DB error reading tasks: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        if task is None:
            async with async_session_factory() as session:
                run = await get_pipeline_run(session, uuid.UUID(pipeline_run_id))
                tasks = await get_pipeline_run_tasks(session, uuid.UUID(pipeline_run_id))
                gates = await list_pipeline_human_gates(session, uuid.UUID(pipeline_run_id))
                terminal = await get_successful_terminal_deployment(
                    session, uuid.UUID(pipeline_run_id)
                )

                if terminal is not None:
                    await _finalize_pipeline_terminal_status(
                        project_id=project_id,
                        user_id=user_id,
                        pipeline_run_id=pipeline_run_id,
                        status="completed",
                        terminal_deployment=terminal,
                    )
                    yield {
                        "type": "pipeline_complete",
                        "pipeline_run_id": pipeline_run_id,
                        "tasks_completed": len(
                            [
                                current
                                for current in tasks
                                if current.status == "completed"
                            ]
                        ),
                    }
                    _update_plan_status(project_id, user_id, "deployed")
                    break

                if any(gate.status == "pending" for gate in gates):
                    yield {
                        "type": "pipeline_waiting_for_approval",
                        "pipeline_run_id": pipeline_run_id,
                    }
                    break

                status, failure_reason = _derive_pipeline_status(run, tasks)
                terminal_status = (
                    "failed" if status not in {"cancelled", "completed"} else status
                )
                final_reason = (
                    failure_reason
                    or "Pipeline ended without a successful terminal deployment."
                )
                await _finalize_pipeline_terminal_status(
                    project_id=project_id,
                    user_id=user_id,
                    pipeline_run_id=pipeline_run_id,
                    status=terminal_status,
                    failure_class=(run.failure_class if run else "unknown"),
                    failure_reason=final_reason,
                )
                yield {
                    "type": "pipeline_error",
                    "stage": "deployment",
                    "pipeline_run_id": pipeline_run_id,
                    "error": final_reason,
                    "status": status,
                }
                _update_plan_status(
                    project_id,
                    user_id,
                    "ready" if terminal_status == "cancelled" else "failed",
                )
                break

        if task.assigned_to not in VALID_AGENTS:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=f"Invalid agent assignment: '{task.assigned_to}'",
            )
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

        plan_status = _AGENT_TO_PLAN_STATUS.get(stage, "generating")
        _update_plan_status(project_id, user_id, plan_status)

        yield {
            "type": "stage_start",
            "stage": stage,
            "task_id": task_id,
            "task_type": task.task_type,
            "description": task.description,
            "queued_at": task.created_at.isoformat() if task.created_at else None,
            "claimed_at": task.claimed_at.isoformat() if task.claimed_at else None,
            "queue_duration_ms": duration_ms(task.created_at, task.claimed_at),
            "retry_budget_key": getattr(task, "retry_budget_key", None),
            "retry_attempt": getattr(task, "retry_attempt", 0),
        }

        try:
            if task.task_type in DIRECT_TASK_TYPES:
                for event in await _execute_direct_task(
                    task,
                    project_id=project_id,
                    user_id=user_id,
                    pipeline_run_id=pipeline_run_id,
                ):
                    yield event
            else:
                agent_message = task.description
                if task.context:
                    slim_context = _slim_task_context(task.context)
                    agent_message = (
                        f"{task.description}\n\n"
                        "Pipeline task context:\n"
                        f"{json.dumps(slim_context, indent=2, sort_keys=True)}"
                    )
                with start_span(
                    "model.call",
                    {
                        "project_id": project_id,
                        "pipeline_run_id": pipeline_run_id,
                        "task_type": task.task_type,
                        "model": "deepagents",
                    },
                ):
                    async for event in stream_chat_with_intent(
                        intent=stage,
                        session_id=f"pipeline-{pipeline_run_id}-{task.retry_attempt}",
                        user_message=agent_message,
                        project_id=project_id,
                        thread_id_override=f"pipeline:{pipeline_run_id}:{task_id}",
                    ):
                        if is_pipeline_cancelled(pipeline_run_id):
                            async with async_session_factory() as session:
                                await update_pipeline_task(
                                    session,
                                    uuid.UUID(task_id),
                                    status="cancelled",
                                    result_summary="Cancellation requested by operator.",
                                    completed_at=datetime.now(timezone.utc),
                                )
                            await _finalize_pipeline_terminal_status(
                                project_id=project_id,
                                user_id=user_id,
                                pipeline_run_id=pipeline_run_id,
                                status="cancelled",
                                failure_class="cancelled",
                                failure_reason="Cancellation requested by operator.",
                            )
                            yield {
                                "type": "pipeline_cancelled",
                                "pipeline_run_id": pipeline_run_id,
                                "stage": stage,
                            }
                            _update_plan_status(project_id, user_id, "ready")
                            clear_project_context()
                            return

                        if event.get("type") == "step":
                            if event.get("tool_calls"):
                                for tc in event["tool_calls"]:
                                    with start_span(
                                        "tool.call",
                                        {
                                            "project_id": project_id,
                                            "pipeline_run_id": pipeline_run_id,
                                            "task_type": task.task_type,
                                            "tool": tc.get("name", ""),
                                        },
                                    ):
                                        yield {
                                            "type": "tool_call",
                                            "stage": stage,
                                            "tool": tc.get("name", ""),
                                            "args": tc.get("args", ""),
                                        }
                            if event.get("content"):
                                yield {
                                    "type": "agent_message",
                                    "stage": stage,
                                    "content": event["content"],
                                }
        except Exception as e:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=f"Task '{task.task_type}' raised an exception: {e}",
            )
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": f"Task '{task.task_type}' raised an exception: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break
        finally:
            set_pipeline_task_id(None)

        try:
            async with async_session_factory() as session:
                updated_task = await get_pipeline_task(session, uuid.UUID(task_id))
        except Exception as e:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=f"Could not reload task after execution: {e}",
            )
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": f"Could not reload task after execution: {e}",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        if updated_task is None:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason="Pipeline task disappeared before completion could be verified.",
            )
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": "Pipeline task disappeared before completion could be verified.",
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        if updated_task.status not in {"completed", "failed", "cancelled"}:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=(
                    "Task finished without resolving its pipeline task into a terminal state."
                ),
            )
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": (
                    "Task finished without resolving its pipeline task into a terminal state."
                ),
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        valid_result, validation_error, execution_result = await _validate_execution_result(
            pipeline_run_id=pipeline_run_id,
            task=updated_task,
        )
        if updated_task.task_type in DIRECT_TASK_TYPES and not valid_result:
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="failed",
                failure_class="unknown",
                failure_reason=validation_error,
            )
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": validation_error,
            }
            _update_plan_status(project_id, user_id, "failed")
            break

        for event in await _postprocess_task(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            task=updated_task,
        ):
            yield event

        async with async_session_factory() as session:
            updated_task = await get_pipeline_task(session, uuid.UUID(task_id))
            run = await get_pipeline_run(session, uuid.UUID(pipeline_run_id))

        if updated_task.status == "cancelled":
            await _finalize_pipeline_terminal_status(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
                status="cancelled",
                failure_class="cancelled",
                failure_reason=updated_task.result_summary
                or "Cancellation requested by operator.",
            )
            yield {
                "type": "pipeline_cancelled",
                "pipeline_run_id": pipeline_run_id,
                "stage": stage,
            }
            _update_plan_status(project_id, user_id, "ready")
            break

        yield {
            "type": "stage_complete",
            "stage": stage,
            "task_id": task_id,
            "task_type": updated_task.task_type,
            "task_status": updated_task.status,
            "result_exit_code": (
                execution_result.get("exit_code") if execution_result else None
            ),
            "queued_at": updated_task.created_at.isoformat()
            if updated_task.created_at
            else None,
            "claimed_at": updated_task.claimed_at.isoformat()
            if updated_task.claimed_at
            else None,
            "completed_at": updated_task.completed_at.isoformat()
            if updated_task.completed_at
            else None,
            "queue_duration_ms": duration_ms(
                updated_task.created_at, updated_task.claimed_at
            ),
            "execution_duration_ms": duration_ms(
                updated_task.claimed_at, updated_task.completed_at
            ),
            "total_duration_ms": duration_ms(
                updated_task.created_at, updated_task.completed_at
            ),
            "retry_budget_key": getattr(updated_task, "retry_budget_key", None),
            "retry_attempt": getattr(updated_task, "retry_attempt", 0),
            "failure_class": getattr(updated_task, "failure_class", None),
        }

        if run is not None and run.status == "waiting_for_approval":
            yield {
                "type": "pipeline_waiting_for_approval",
                "pipeline_run_id": pipeline_run_id,
                "stage": stage,
            }
            break

    clear_project_context()


async def get_pipeline_status(
    project_id: str,
    user_id: str,
    pipeline_run_id: str,
    *,
    include_tasks: bool = True,
    include_gates: bool = True,
    include_evaluations: bool = True,
) -> dict:
    """
    Return the full task history for a pipeline run.
    Used by GET /pipeline/status for frontend display.
    """
    try:
        run_uuid = uuid.UUID(pipeline_run_id)
        async with async_session_factory() as session:
            run = await get_pipeline_run(session, run_uuid)
            snapshot = await get_pipeline_run_snapshot(session, run_uuid)
            if snapshot is not None:
                payload = dict(snapshot.snapshot_json or {})
            else:
                refreshed_snapshot = await refresh_pipeline_run_snapshot(session, run_uuid)
                if refreshed_snapshot is not None:
                    payload = dict(refreshed_snapshot.snapshot_json or {})
                else:
                    tasks = await get_pipeline_run_tasks(session, run_uuid)
                    gates = await list_pipeline_human_gates(session, run_uuid)
                    evaluations = await list_pipeline_evaluations(session, run_uuid)
                    payload = build_pipeline_status_payload(
                        project_id=project_id,
                        pipeline_run_id=pipeline_run_id,
                        run=run,
                        tasks=tasks,
                        gates=gates,
                        evaluations=evaluations,
                    )

        return project_pipeline_status_payload(
            payload,
            include_tasks=include_tasks,
            include_gates=include_gates,
            include_evaluations=include_evaluations,
        )
    except Exception as e:
        return {"error": f"Could not retrieve pipeline status: {e}"}
