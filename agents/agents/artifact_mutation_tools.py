from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from agents.code_storage import get_code_storage


def _get_memory_manager():
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


def _normalize_artifact_path(path: str) -> str:
    normalized = str(path or "").strip().lstrip("/")
    if not normalized:
        raise ValueError("Artifact path is required.")
    if ".." in normalized.split("/"):
        raise ValueError("Path traversal is not allowed.")
    return normalized


def _get_agent_artifacts(agent_name: str) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    mm = _get_memory_manager()
    state = mm.get_agent_state(agent_name)
    artifacts = [item for item in state.get("artifacts", []) if isinstance(item, dict)]
    return mm, state, artifacts


def _set_agent_artifacts(
    *,
    mm: Any,
    agent_name: str,
    state: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> None:
    state["artifacts"] = artifacts
    if agent_name == "coding":
        state["artifact_count"] = len(artifacts)
        state["last_artifact_path"] = artifacts[-1].get("path") if artifacts else None
    mm.set_agent_state(agent_name, state)


def _tracked_matches(artifacts: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    return [
        artifact
        for artifact in artifacts
        if str(artifact.get("path") or "").lstrip("/") == path
    ]


def _edit_artifact(
    *,
    agent_name: str,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> dict[str, Any]:
    normalized_path = _normalize_artifact_path(path)
    mm, state, artifacts = _get_agent_artifacts(agent_name)
    matches = _tracked_matches(artifacts, normalized_path)
    if not matches:
        return {
            "error": (
                f"Artifact '{normalized_path}' is not tracked for the {agent_name} agent. "
                "Load the current artifacts first and use one of those exact paths."
            )
        }

    storage = get_code_storage()
    try:
        occurrences = storage.edit_code(
            normalized_path,
            old_string,
            new_string,
            replace_all=replace_all,
        )
    except FileNotFoundError:
        return {"error": f"Artifact file '{normalized_path}' was not found in storage."}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Could not edit artifact '{normalized_path}': {str(e)}"}

    mm.log_agent_action(
        agent_name=agent_name,
        action="artifact_edited",
        output_produced={
            "path": normalized_path,
            "occurrences": occurrences,
        },
        why=f"{agent_name.capitalize()} agent updated an existing artifact in storage",
        how=f"edit_{agent_name}_artifact tool",
    )

    return {
        "success": True,
        "artifact_path": normalized_path,
        "occurrences": occurrences,
    }


def _delete_artifact(*, agent_name: str, path: str) -> dict[str, Any]:
    normalized_path = _normalize_artifact_path(path)
    mm, state, artifacts = _get_agent_artifacts(agent_name)
    matches = _tracked_matches(artifacts, normalized_path)
    if not matches:
        return {
            "error": (
                f"Artifact '{normalized_path}' is not tracked for the {agent_name} agent. "
                "Load the current artifacts first and use one of those exact paths."
            )
        }

    storage = get_code_storage()
    file_deleted = True
    try:
        storage.delete_code(normalized_path)
    except FileNotFoundError:
        file_deleted = False
    except IsADirectoryError:
        return {"error": f"Artifact path '{normalized_path}' points to a directory, not a file."}
    except Exception as e:
        return {"error": f"Could not delete artifact '{normalized_path}': {str(e)}"}

    remaining = [
        artifact
        for artifact in artifacts
        if str(artifact.get("path") or "").lstrip("/") != normalized_path
    ]
    _set_agent_artifacts(
        mm=mm,
        agent_name=agent_name,
        state=state,
        artifacts=remaining,
    )

    mm.log_agent_action(
        agent_name=agent_name,
        action="artifact_deleted",
        output_produced={
            "path": normalized_path,
            "metadata_entries_removed": len(matches),
            "file_deleted": file_deleted,
        },
        why=f"{agent_name.capitalize()} agent removed an obsolete artifact from storage",
        how=f"delete_{agent_name}_artifact tool",
    )

    return {
        "success": True,
        "artifact_path": normalized_path,
        "file_deleted": file_deleted,
        "metadata_entries_removed": len(matches),
    }


@tool
def edit_code_artifact(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """
    Edit an existing coding artifact in place in artifact storage.

    Use this when revising an already-saved contract or manifest file at the
    same path. This avoids creating duplicate artifacts for the same contract.
    """
    return _edit_artifact(
        agent_name="coding",
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
    )


@tool
def delete_code_artifact(path: str) -> dict[str, Any]:
    """
    Delete an obsolete coding artifact file and remove its metadata entry.

    Use this before saving a replacement under a new path so old contract files
    do not linger in storage.
    """
    return _delete_artifact(agent_name="coding", path=path)


@tool
def get_current_test_artifacts() -> dict[str, Any]:
    """
    Retrieve the current list of metadata-only test artifacts for this user.

    Use this before editing or deleting an existing test file so you have the
    exact stored path.
    """
    try:
        _, _, artifacts = _get_agent_artifacts("testing")
        return {"artifacts": artifacts}
    except Exception as e:
        return {"error": f"Could not retrieve test artifacts: {str(e)}"}


@tool
def edit_test_artifact(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """
    Edit an existing saved Foundry test artifact in place in artifact storage.

    Use this for test-only fixes to an already-saved file at the same path.
    """
    return _edit_artifact(
        agent_name="testing",
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=replace_all,
    )


@tool
def delete_test_artifact(path: str) -> dict[str, Any]:
    """
    Delete an obsolete test artifact file and remove its metadata entry.

    Use this before replacing or renaming a saved test file.
    """
    return _delete_artifact(agent_name="testing", path=path)
