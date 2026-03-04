import os
import sys
import json
from typing import List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool

from schemas.testing_schema import TestPlan, TestResultSummary


def _get_memory_manager(user_id: str = "default"):
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id)


@tool
def save_test_plan(plan: TestPlan) -> dict:
    """
    Save a test plan in the testing agent's state.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        testing_state = data["agents"]["testing"]

        plans: List[dict] = testing_state.get("test_plans", [])
        plans.append(plan.model_dump())
        testing_state["test_plans"] = plans

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="testing",
            action="test_plan_saved",
            output_produced=plan.model_dump(),
            why="Testing agent saved a test plan",
            how="save_test_plan tool",
        )

        return {"success": True, "plan_name": plan.name}
    except Exception as e:
        return {"error": f"Could not save test plan: {str(e)}"}


@tool
def save_test_results(summary: TestResultSummary) -> dict:
    """
    Save the latest test run summary.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        testing_state = data["agents"]["testing"]

        results: List[dict] = testing_state.get("last_test_results", [])
        results.append(summary.model_dump())
        testing_state["last_test_results"] = results

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="testing",
            action="test_results_saved",
            output_produced=summary.model_dump(),
            why="Testing agent recorded test run results",
            how="save_test_results tool",
        )

        return {"success": True, "plan_name": summary.plan_name}
    except Exception as e:
        return {"error": f"Could not save test results: {str(e)}"}


@tool
def get_last_test_results() -> dict:
    """
    Retrieve the most recent test results for this user.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("testing")
        return {
            "last_test_results": state.get("last_test_results", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve test results: {str(e)}"}


TESTING_TOOLS = [
    save_test_plan,
    save_test_results,
    get_last_test_results,
]

