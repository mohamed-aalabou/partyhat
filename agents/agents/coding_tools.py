import os
import shlex
import sys
from typing import List, Dict, Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
import modal

from schemas.coding_schema import CodeArtifact, CodeGenerationRequest
from agents.code_storage import get_code_storage
from agents.task_tools import TASK_TOOLS
from modal_foundry_app import foundry_image
from agents.modal_runtime import (
    build_foundry_bootstrap_cmd,
    build_project_volume_name,
    get_modal_app,
    get_modal_volume,
)


def _get_memory_manager():
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


def generate_solidity_code_direct(request: CodeGenerationRequest) -> dict:
    """
    Direct helper for Solidity generation used outside the tool graph.

    This function contains the core generation logic and is safe to call
    directly from HTTP endpoints or other Python code. It calls an OpenAI
    chat model to generate Solidity code, rather than going through the
    /coding/generate HTTP endpoint or an external inference service.
    """
    constraints_section = ""
    if request.constraints:
        joined = "\n".join(f"- {c}" for c in request.constraints)
        constraints_section = f"\n\nConstraints:\n{joined}"

    prompt = (
        "You are a Solidity expert. Generate production-grade Solidity code "
        "that satisfies the following goal.\n\n"
        f"Goal:\n{request.goal}{constraints_section}\n\n"
        "Output only Solidity code, with SPDX license identifier, pragma, "
        "imports, contract definitions, and any necessary comments. Do not "
        "include explanations or markdown fences, return only raw Solidity."
    )

    model_name = os.getenv("SOLIDITY_MODEL", "gpt-5.2-2025-12-11")
    llm = ChatOpenAI(model=model_name, temperature=0.1)

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        generated_text = response.content or ""
    except Exception as e:
        return {"error": f"Failed to call Solidity generation model: {str(e)}"}

    return {
        "goal": request.goal,
        "generated_code": generated_text,
    }


def get_current_plan() -> dict:
    """
    Retrieve the current smart contract plan draft from memory.

    Call this tool:
    - At the start of EVERY conversation to check if a plan already exists
    - When the user asks to continue or modify an existing plan
    - Before saving a new draft to understand the current state
    - When resuming after a session gap

    Returns the plan as a dict, or an empty dict if no plan exists yet.
    """
    try:
        mm = _get_memory_manager()
        plan = mm.get_plan()
        if plan:
            return plan
        return {"message": "No plan exists yet. This is a fresh start."}
    except Exception as e:
        return {"error": f"Could not retrieve plan: {str(e)}"}


@tool
def generate_solidity_code(request: CodeGenerationRequest) -> dict:
    """
    Tool wrapper around the direct Solidity generation helper.

    This allows the coding agent to call the generator while reusing the
    same core implementation used by the HTTP endpoint.
    """
    return generate_solidity_code_direct(request)


@tool
def get_current_artifacts() -> dict:
    """
    Retrieve the current list of metadata-only code artifacts for this user.

    The returned artifacts describe where code is stored (paths/keys),
    along with lightweight metadata such as descriptions and contract names.
    Full source blobs are not stored in memory.
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
    Persist generated code via the storage backend and record metadata in memory.

    Expected usage:
    - Callers provide a CodeArtifact that may include a transient ``code`` field.
    - This tool writes the code to disk using LocalCodeStorage.
    - Only metadata (no code blobs) is appended to the coding agent's artifacts.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        coding_state = data["agents"]["coding"]

        storage = get_code_storage()

        # Write code to storage if provided, then build a metadata-only record.
        raw = artifact.model_dump()
        code = raw.pop("code", None)

        if code:
            stored_path = storage.save_code(artifact, code)
            raw["path"] = stored_path

        artifacts: List[Dict[str, Any]] = coding_state.get("artifacts", [])
        artifacts.append(raw)
        coding_state["artifacts"] = artifacts

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="coding",
            action="code_artifact_saved",
            output_produced=raw,
            why="Coding agent saved or updated an artifact",
            how="save_code_artifact tool",
        )

        return {"success": True, "artifact_path": raw.get("path", artifact.path)}
    except Exception as e:
        return {"error": f"Could not save artifact: {str(e)}"}


@tool
def load_code_artifact(path: str) -> dict:
    """
    Load full source code for a previously saved artifact by its stored path.

    This is an explicit, opt-in retrieval path; normal flows should rely on
    metadata from get_current_artifacts and only call this when code content
    is truly needed.
    """
    try:
        storage = get_code_storage()
        code = storage.load_code(path)
        return {"path": path, "code": code}
    except Exception as e:
        return {"error": f"Could not load artifact: {str(e)}"}


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


@tool
def ensure_chainlink_contracts(project_root: Optional[str] = None) -> dict:
    """
    Ensure Chainlink contracts are installed in the Foundry sandbox project.

    This tool is intended for coding/test compile failures that mention missing
    Chainlink imports (for example AggregatorV3Interface).
    """
    try:
        from agents.context import get_project_context

        project_id_ctx, _ = get_project_context()
        default_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
        if project_id_ctx:
            default_root = f"{default_root.rstrip('/')}/{project_id_ctx}"
        root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT") or default_root

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))

        app = get_modal_app(app_name)

        base_volume_name = os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        volume_name = build_project_volume_name(base_volume_name, project_id_ctx)
        vol = get_modal_volume(volume_name)

        sandbox_workdir = "/workspace/project"
        bootstrap_cmd = build_foundry_bootstrap_cmd(
            root,
            "touch remappings.txt; "
            "grep -qxF '@chainlink/contracts/=lib/chainlink-evm/contracts/' remappings.txt "
            "|| echo '@chainlink/contracts/=lib/chainlink-evm/contracts/' >> remappings.txt; "
            "grep -qxF '@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/' remappings.txt "
            "|| echo '@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/' >> remappings.txt; "
            "ls -la lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol"
        )

        sandbox = modal.Sandbox.create(
            "bash",
            "-lc",
            bootstrap_cmd,
            image=foundry_image,
            app=app,
            workdir=sandbox_workdir,
            timeout=timeout,
            volumes={sandbox_workdir: vol},
        )

        stdout = sandbox.stdout.read()
        stderr = sandbox.stderr.read()
        sandbox.wait(raise_on_termination=False)
        exit_code = sandbox.returncode

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "project_root": root,
            "sandbox_workdir": sandbox_workdir,
            "modal_app": app_name,
        }
    except Exception as e:
        return {"error": f"Could not ensure Chainlink contracts in sandbox: {str(e)}"}


CODING_TOOLS = [
    get_current_plan,
    get_current_artifacts,
    generate_solidity_code,
    save_code_artifact,
    save_coding_note,
    load_code_artifact,
    ensure_chainlink_contracts,
] + TASK_TOOLS
