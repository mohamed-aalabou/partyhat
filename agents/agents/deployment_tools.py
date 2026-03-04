import os
import sys
import json
from typing import List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool

from schemas.deployment_schema import DeploymentTarget, DeploymentRecord


def _get_memory_manager(user_id: str = "default"):
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id)


@tool
def save_deployment_target(target: DeploymentTarget) -> dict:
    """
    Save a deployment target (network + label) to the deployment agent's state.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        deployment_state = data["agents"]["deployment"]

        targets: List[dict] = deployment_state.get("targets", [])
        targets.append(target.model_dump())
        deployment_state["targets"] = targets

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="deployment",
            action="deployment_target_saved",
            output_produced=target.model_dump(),
            why="Deployment agent saved a deployment target",
            how="save_deployment_target tool",
        )

        return {"success": True, "network": target.network}
    except Exception as e:
        return {"error": f"Could not save deployment target: {str(e)}"}


@tool
def record_deployment(record: DeploymentRecord) -> dict:
    """
    Record a deployment attempt and its outcome.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        deployment_state = data["agents"]["deployment"]

        deployments: List[dict] = deployment_state.get("deployments", [])
        deployments.append(record.model_dump())
        deployment_state["deployments"] = deployments

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="deployment",
            action="deployment_recorded",
            output_produced=record.model_dump(),
            why="Deployment agent recorded a deployment attempt",
            how="record_deployment tool",
        )

        return {"success": True, "network": record.target.network}
    except Exception as e:
        return {"error": f"Could not record deployment: {str(e)}"}


@tool
def get_deployment_history() -> dict:
    """
    Retrieve the history of deployments for this user.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("deployment")
        return {
            "deployments": state.get("deployments", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve deployment history: {str(e)}"}


DEPLOYMENT_TOOLS = [
    save_deployment_target,
    record_deployment,
    get_deployment_history,
]

