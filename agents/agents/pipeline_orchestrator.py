import json
import uuid
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
    claim_next_pending_task,
    complete_pipeline_task_and_create_next,
    create_pipeline_task,
    get_pipeline_task,
    get_pipeline_run_tasks,
)
from agents.deployment_tools import (
    generate_foundry_deploy_script_direct,
    record_deployment,
    run_foundry_deploy,
    save_deploy_artifact,
)
from agents.memory_manager import MemoryManager
from agents.pipeline_cancel import is_pipeline_cancelled, clear_cancellation
from agents.pipeline_context import (
    compact_execution_summary,
    default_expected_outputs,
    duration_ms,
    extract_plan_summary,
    standardize_task_context,
)
from agents.testing_tools import run_foundry_tests
from schemas.coding_schema import CodeArtifact
from schemas.deployment_schema import (
    DeploymentRecord,
    DeploymentStatus,
    DeploymentTarget,
    FoundryDeployRequest,
    FoundryDeployScriptGenerationRequest,
)

MAX_ITERATIONS = 10  # Just a hard cap to prevent infinite agent loops

VALID_AGENTS = {"coding", "testing", "deployment", "audit"}
DIRECT_TASK_TYPES = {
    "testing.run_tests",
    "deployment.prepare_script",
    "deployment.execute_deploy",
    "deployment.retry_deploy",
}
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
    if planning_state.get("plan_summary"):
        return planning_state["plan_summary"]
    return extract_plan_summary(mm.get_plan())


def _serialize_task(task) -> dict:
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


def _build_upstream_task(task, task_status: str, result_summary: str) -> dict:
    return {
        "task_id": str(task.id),
        "task_type": task.task_type,
        "assigned_to": task.assigned_to,
        "status": task_status,
        "result_summary": result_summary,
    }


def _next_task_payload(
    task,
    *,
    assigned_to: str,
    task_type: str,
    description: str,
    context: dict | None,
    artifact_revision: int,
    task_status: str,
    result_summary: str,
) -> dict:
    failure_context = None
    if task_status == "failed":
        failure_context = {
            "task_id": str(task.id),
            "task_type": task.task_type,
            "result_summary": result_summary,
        }
    return {
        "assigned_to": assigned_to,
        "task_type": task_type,
        "description": description,
        "parent_task_id": str(task.id),
        "sequence_index": 0,
        "artifact_revision": artifact_revision,
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


def _load_contract_sources(coding_artifacts: list[dict]) -> str:
    chunks: list[str] = []
    for artifact in coding_artifacts:
        path = artifact.get("path")
        if not path or not str(path).startswith("contracts/"):
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


def _classify_test_failure(output: str) -> tuple[str, str, str]:
    lowered = output.lower()
    if any(keyword in lowered for keyword in _TEST_INFRA_KEYWORDS):
        return "testing", "testing.generate_tests", "Test infrastructure or remapping issue detected."
    if any(keyword in lowered for keyword in _CONTRACT_FAILURE_KEYWORDS):
        return "coding", "coding.generate_contracts", "Contract logic or compile issue detected in test run."
    return "coding", "coding.generate_contracts", "Test execution failed; route back to coding for review."


def _classify_deploy_failure(output: str, *, allow_retry: bool) -> tuple[str, str, str]:
    lowered = output.lower()
    if allow_retry and (
        any(keyword in lowered for keyword in _DEPLOY_RETRY_KEYWORDS)
        or any(keyword in lowered for keyword in _TEST_INFRA_KEYWORDS)
    ):
        return "deployment", "deployment.retry_deploy", "Deployment infrastructure issue detected; retry deployment."
    return "coding", "coding.generate_contracts", "Deployment failure suggests a contract or script issue."


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

    if result.get("success"):
        result_summary = compact_execution_summary(
            result.get("exit_code", 0),
            result.get("stdout", ""),
            result.get("stderr", ""),
        )
        next_task = _next_task_payload(
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
    next_agent, next_task_type, reason = _classify_test_failure(combined)
    result_summary = reason
    next_task = _next_task_payload(
        task,
        assigned_to=next_agent,
        task_type=next_task_type,
        description=reason,
        context={
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
        },
        artifact_revision=artifact_revision,
        task_status="failed",
        result_summary=result_summary,
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
    plan = mm.get_plan() or {}
    snapshot = _artifact_snapshot(mm)
    coding_artifacts = snapshot.get("coding", [])
    contract_name, contract_plan = _select_primary_contract(plan, coding_artifacts)
    if not contract_name:
        result_summary = "No contract artifact available to prepare a deployment script."
        next_task = _next_task_payload(
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description=result_summary,
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
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

    script_name = f"Deploy{contract_name}"
    script_path = f"script/{script_name}.s.sol"
    generation = generate_foundry_deploy_script_direct(
        FoundryDeployScriptGenerationRequest(
            goal=f"Deploy {contract_name} to Avalanche Fuji.",
            contract_name=contract_name,
            script_name=script_name,
            constructor_args=_constructor_literals(contract_plan),
            constraints=_deployment_constraints(contract_plan),
            plan_summary=json.dumps(_current_plan_summary(mm, task.context), indent=2),
            contract_sources=_load_contract_sources(coding_artifacts),
        )
    )
    if generation.get("error"):
        result_summary = generation["error"]
        next_task = _next_task_payload(
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description="Fix the contract or artifact state so deployment script generation can succeed.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
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
            code=generation.get("generated_script", ""),
        )
    )
    if save_result.get("error"):
        result_summary = save_result["error"]
        next_task = _next_task_payload(
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description="Fix the contract or storage state so deployment script persistence can succeed.",
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
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

    result_summary = f"Prepared deployment script at {script_path}."
    next_task = _next_task_payload(
        task,
        assigned_to="deployment",
        task_type="deployment.execute_deploy",
        description=f"Execute the prepared deployment script {script_path} on Avalanche Fuji.",
        context={
            "script_path": script_path,
            "script_name": script_name,
            "contract_name": contract_name,
        },
        artifact_revision=task.artifact_revision,
        task_status="completed",
        result_summary=result_summary,
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
    if not script_path:
        result_summary = "No deployment script artifact available to execute."
        next_task = _next_task_payload(
            task,
            assigned_to="coding",
            task_type="coding.generate_contracts",
            description=result_summary,
            context={"failure_context": {"summary": result_summary}},
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary=result_summary,
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

    deploy_result = run_foundry_deploy.func(
        FoundryDeployRequest(
            script_path=script_path,
            network="avalanche_fuji",
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
                network="avalanche_fuji",
                quiet_output=True,
            )
        )

    target = DeploymentTarget(
        network="avalanche_fuji",
        name="Avalanche Fuji",
        chain_id=43113,
        rpc_url_env_var="FUJI_RPC_URL",
        private_key_env_var="FUJI_PRIVATE_KEY",
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
                script_path=script_path,
                chain_id=43113,
                command=deploy_result.get("command"),
                stdout_path=deploy_result.get("stdout_path"),
                stderr_path=deploy_result.get("stderr_path"),
                exit_code=deploy_result.get("exit_code"),
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
            script_path=script_path,
            chain_id=43113,
            command=deploy_result.get("command"),
            stdout_path=deploy_result.get("stdout_path"),
            stderr_path=deploy_result.get("stderr_path"),
            exit_code=deploy_result.get("exit_code"),
        )
    )

    next_agent, next_task_type, reason = _classify_deploy_failure(
        f"{deploy_result.get('error', '')}\n{deploy_result.get('stdout', '')}\n{deploy_result.get('stderr', '')}",
        allow_retry=task.task_type != "deployment.retry_deploy",
    )
    next_task = _next_task_payload(
        task,
        assigned_to=next_agent,
        task_type=next_task_type,
        description=reason,
        context={
            "script_path": script_path,
            "contract_name": task_context.get("contract_name"),
            "failure_context": {
                "summary": compact_execution_summary(
                    deploy_result.get("exit_code", 1),
                    deploy_result.get("stdout", ""),
                    deploy_result.get("stderr", ""),
                ),
                "stdout_path": deploy_result.get("stdout_path"),
                "stderr_path": deploy_result.get("stderr_path"),
                "exit_code": deploy_result.get("exit_code"),
            },
        },
        artifact_revision=task.artifact_revision,
        task_status="failed",
        result_summary=reason,
    )
    next_task["context"]["input_artifacts"] = snapshot
    await _finalize_direct_task(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        task=task,
        task_status="failed",
        result_summary=reason,
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


async def run_autonomous_pipeline(
    project_id: str,
    user_id: str,
    max_iterations: int = MAX_ITERATIONS,
) -> AsyncIterator[dict]:
    """
    Run the autonomous post-approval pipeline.
    """
    pipeline_run_id = str(uuid.uuid4())

    yield {
        "type": "pipeline_start",
        "pipeline_run_id": pipeline_run_id,
        "project_id": project_id,
    }

    set_project_context(project_id, user_id)
    set_pipeline_run_id(pipeline_run_id)

    mm = MemoryManager(user_id=user_id, project_id=project_id)
    initial_context = standardize_task_context(
        None,
        plan_summary=_current_plan_summary(mm),
        artifact_revision=_current_artifact_revision(mm),
        input_artifacts=_artifact_snapshot(mm),
        expected_outputs=default_expected_outputs(INITIAL_TASK["task_type"]),
    )

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
                context=initial_context,
                artifact_revision=initial_context["artifact_revision"],
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
                task = await claim_next_pending_task(session, uuid.UUID(pipeline_run_id))
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
                        [current for current in tasks if current.status == "completed"]
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

        plan_status = _AGENT_TO_PLAN_STATUS.get(stage, "generating")
        _update_plan_status(project_id, user_id, plan_status)

        stage_start_event = {
            "type": "stage_start",
            "stage": stage,
            "task_id": task_id,
            "task_type": task.task_type,
            "description": task.description,
            "iteration": iteration,
            "queued_at": task.created_at.isoformat() if task.created_at else None,
            "claimed_at": task.claimed_at.isoformat() if task.claimed_at else None,
            "queue_duration_ms": duration_ms(task.created_at, task.claimed_at),
        }
        yield stage_start_event

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
                    agent_message = (
                        f"{task.description}\n\n"
                        "Pipeline task context:\n"
                        f"{json.dumps(task.context, indent=2, sort_keys=True)}"
                    )
                async for event in stream_chat_with_intent(
                    intent=stage,
                    session_id=f"pipeline-{pipeline_run_id}-{iteration}",
                    user_message=agent_message,
                    project_id=project_id,
                    thread_id_override=f"pipeline:{pipeline_run_id}:{task_id}",
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
        except Exception as e:
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
            yield {
                "type": "pipeline_error",
                "stage": stage,
                "task_id": task_id,
                "error": f"Could not reload task after execution: {e}",
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
                    "Task finished without resolving its pipeline task via "
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
            "tasks": [_serialize_task(task) for task in tasks],
        }
    except Exception as e:
        return {"error": f"Could not retrieve pipeline status: {e}"}
