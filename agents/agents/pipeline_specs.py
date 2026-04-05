from __future__ import annotations

from schemas.deployment_schema import DeploymentTarget

VALID_AGENTS = {"coding", "testing", "deployment", "audit"}

DIRECT_TASK_TYPES = {
    "testing.run_tests",
    "deployment.prepare_script",
    "deployment.execute_deploy",
    "deployment.retry_deploy",
}

TERMINAL_SUCCESS_TASK_TYPES = {
    "deployment.execute_deploy",
    "deployment.retry_deploy",
}

RETRY_BUDGETS = {
    "coding": 2,
    "testing.generate": 1,
    "testing.execute": 2,
    "deployment.prepare": 1,
    "deployment.execute": 1,
}

TASK_RETRY_BUDGET_KEYS = {
    "coding.generate_contracts": "coding",
    "testing.generate_tests": "testing.generate",
    "testing.run_tests": "testing.execute",
    "deployment.prepare_script": "deployment.prepare",
    "deployment.execute_deploy": "deployment.execute",
    "deployment.retry_deploy": "deployment.execute",
}

TASK_STAGE_NAMES = {
    "coding.generate_contracts": "coding",
    "testing.generate_tests": "testing",
    "testing.run_tests": "testing",
    "deployment.prepare_script": "deployment",
    "deployment.execute_deploy": "deployment",
    "deployment.retry_deploy": "deployment",
}

FAILURE_CLASSES = {
    "transient_infra",
    "dependency_bootstrap",
    "artifact_contract_mismatch",
    "contract_logic",
    "evaluation_failed",
    "human_gate",
    "cancelled",
    "unknown",
}

EMERGENCY_TASK_FUSE = 100


def default_deployment_target() -> DeploymentTarget:
    return DeploymentTarget(
        network="avalanche_fuji",
        name="Avalanche Fuji",
        description="Default Avalanche Fuji deployment target.",
        chain_id=43113,
        rpc_url_env_var="FUJI_RPC_URL",
        private_key_env_var="FUJI_PRIVATE_KEY",
    )


def default_deployment_target_payload() -> dict:
    return default_deployment_target().model_dump()


def retry_budget_key_for_task(task_type: str) -> str:
    return TASK_RETRY_BUDGET_KEYS.get(task_type, "unknown")


def stage_name_for_task(task_type: str, assigned_to: str | None = None) -> str:
    if task_type in TASK_STAGE_NAMES:
        return TASK_STAGE_NAMES[task_type]
    return assigned_to or task_type.split(".", 1)[0]


def retry_budget_for_key(retry_budget_key: str) -> int:
    return RETRY_BUDGETS.get(retry_budget_key, 0)
