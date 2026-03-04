import os
import sys
import json
from typing import List

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool

from schemas.coding_schema import CodeArtifact, CodeGenerationRequest


def _get_memory_manager(user_id: str = "default"):
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id)


@tool
def get_current_artifacts() -> dict:
    """
    Retrieve the current list of code artifacts for this user.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("coding")
        return {
            "artifacts": state.get("artifacts", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve artifacts: {str(e)}"}


@tool
def save_code_artifact(artifact: CodeArtifact) -> dict:
    """
    Save or update a code artifact in the coding agent's state.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        coding_state = data["agents"]["coding"]

        artifacts: List[dict] = coding_state.get("artifacts", [])
        artifacts.append(artifact.model_dump())
        coding_state["artifacts"] = artifacts

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="coding",
            action="code_artifact_saved",
            output_produced=artifact.model_dump(),
            why="Coding agent saved or updated an artifact",
            how="save_code_artifact tool",
        )

        return {"success": True, "artifact_path": artifact.path}
    except Exception as e:
        return {"error": f"Could not save artifact: {str(e)}"}


@tool
def save_coding_note(note: str) -> dict:
    """
    Save a coding-related note (design decision, trade-off, etc.) for this user.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        coding_state = data["agents"]["coding"]

        notes: List[str] = coding_state.get("notes", [])
        notes.append(note)
        coding_state["notes"] = notes

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="coding",
            action="coding_note_saved",
            output_produced={"note": note},
            why="Coding agent recorded a design or implementation note",
            how="save_coding_note tool",
        )

        return {"success": True}
    except Exception as e:
        return {"error": f"Could not save coding note: {str(e)}"}


CODING_TOOLS = [
    get_current_artifacts,
    save_code_artifact,
    save_coding_note,
]

