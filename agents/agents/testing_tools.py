import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modal_foundry_app import foundry_image
import modal
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from schemas.coding_schema import CodeArtifact
from agents.code_storage import LocalCodeStorage
from agents.planning_tools import get_current_plan as planning_get_current_plan
from agents.coding_tools import (
    get_current_artifacts as coding_get_current_artifacts,
    load_code_artifact as coding_load_code_artifact,
)


def _get_memory_manager(user_id: str = "default"):
    """
    Lazy import helper to avoid circular dependencies.
    """
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id)


class FoundryTestGenerationRequest(BaseModel):
    """
    Input payload for generating Foundry tests.

    The testing agent should:
    - Use get_current_plan() to understand the validated architecture.
    - Use get_current_artifacts() and load_code_artifact() to gather contract code.
    - Provide a clear goal for what behaviours and invariants to test.
    """

    goal: str = Field(
        ...,
        description=(
            "High-level description of what should be tested, including key "
            "behaviours, workflows, and edge cases."
        ),
    )
    contract_summaries: Optional[str] = Field(
        default=None,
        description=(
            "Optional natural-language summary of the contracts under test. "
            "Typically derived from the planning agent's plan."
        ),
    )
    contract_sources: Optional[str] = Field(
        default=None,
        description=(
            "Optional concatenated Solidity source for the contracts under test. "
            "Use load_code_artifact() to populate this when needed."
        ),
    )
    constraints: List[str] = Field(
        default_factory=list,
        description=(
            "Optional list of constraints or special requirements for the tests "
            "(e.g. gas limits, invariants, reentrancy checks)."
        ),
    )


def generate_foundry_tests_direct(
    request: FoundryTestGenerationRequest,
) -> Dict[str, Any]:
    """
    Direct helper for Foundry test generation used outside the tool graph.

    This mirrors generate_solidity_code_direct but produces Solidity test
    contracts that follow Foundry conventions.
    """
    constraints_section = ""
    if request.constraints:
        joined = "\n".join(f"- {c}" for c in request.constraints)
        constraints_section = f"\n\nTesting constraints:\n{joined}"

    plan_section = ""
    if request.contract_summaries:
        plan_section = (
            "\n\nContract architecture summary:\n"
            f"{request.contract_summaries.strip()}"
        )

    source_section = ""
    if request.contract_sources:
        source_section = (
            "\n\nSolidity source for contracts under test:\n"
            "---------------- SOURCE START ----------------\n"
            f"{request.contract_sources.strip()}\n"
            "----------------- SOURCE END -----------------\n"
        )

    prompt = (
        "You are a Solidity testing expert.\n"
        "Generate production-quality Foundry tests for the described contracts.\n\n"
        "Requirements:\n"
        "- Use `forge-std/Test.sol`.\n"
        "- Each test contract must inherit from `Test`.\n"
        "- Deploy contracts in `setUp()`.\n"
        "- Name test functions with the `test_` prefix or `test` prefix.\n"
        "- Project layout is STRICTLY:\n"
        "  - contracts are in `contracts/`\n"
        "  - tests are in `test/`\n"
        "- Contract imports in tests MUST use `../contracts/<ContractName>.sol`.\n"
        "- NEVER import from `../src/...`.\n"
        "- Keep `forge-std` import as `import {Test} from \"forge-std/Test.sol\";`.\n"
        "- OpenZeppelin imports MUST use `@openzeppelin/contracts/...` remapping.\n"
        "- Cover constructor behaviour, state initialisation, happy-path flows,\n"
        "  revert conditions, access control, and relevant edge cases.\n"
        "- Output ONLY raw Solidity test contracts (no markdown fences, no prose).\n\n"
        f"Testing goal:\n{request.goal.strip()}"
        f"{constraints_section}"
        f"{plan_section}"
        f"{source_section}"
    )

    model_name = os.getenv("FOUNDRY_TEST_MODEL", "gpt-5.2-2025-12-11")
    llm = ChatOpenAI(model=model_name, temperature=0.1)

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        generated_text = response.content or ""
    except Exception as e:
        return {"error": f"Failed to call Foundry test generation model: {str(e)}"}

    # Guardrail normalization to keep generated tests aligned with this repo's
    # Foundry layout, even if the model drifts to common src/ conventions.
    generated_text = generated_text.replace("../src/", "../contracts/")
    generated_text = generated_text.replace('import "forge-std/Test.sol";', 'import {Test} from "forge-std/Test.sol";')

    return {
        "goal": request.goal,
        "generated_tests": generated_text,
    }


@tool
def generate_foundry_tests(request: FoundryTestGenerationRequest) -> Dict[str, Any]:
    """
    Tool wrapper around the direct Foundry test generation helper.

    The testing agent should:
    - Call get_current_plan() and get_current_artifacts() first.
    - Use load_code_artifact() if full Solidity source is required.
    - Then call this tool with a clear goal and optional context.
    """
    return generate_foundry_tests_direct(request)


@tool
def save_test_artifact(artifact: CodeArtifact) -> Dict[str, Any]:
    """
    Persist generated Foundry test files via the storage backend and
    record metadata in the testing agent's memory slice.

    Expected usage:
    - Callers provide a CodeArtifact that may include a transient ``code`` field.
    - This tool writes the code to disk using LocalCodeStorage.
    - Only metadata (no code blobs) is appended to the testing agent's artifacts.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        testing_state = data["agents"]["testing"]

        storage = LocalCodeStorage()

        raw = artifact.model_dump()
        code = raw.pop("code", None)

        if code:
            stored_path = storage.save_code(artifact, code)
            raw["path"] = stored_path

        artifacts: List[Dict[str, Any]] = testing_state.get("artifacts", [])
        artifacts.append(raw)
        testing_state["artifacts"] = artifacts

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="testing",
            action="test_artifact_saved",
            output_produced=raw,
            why="Testing agent saved or updated a Foundry test artifact",
            how="save_test_artifact tool",
        )

        return {"success": True, "artifact_path": raw.get("path", artifact.path)}
    except Exception as e:
        return {"error": f"Could not save test artifact: {str(e)}"}


@tool
def run_foundry_tests(
    project_root: Optional[str] = None,
    extra_args: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Run `forge test` inside a Modal Sandbox and capture the results.

    Configuration is controlled via environment variables:
    - MODAL_APP_NAME:   Name of the Modal app to associate the sandbox with
                        (default: 'partyhat-foundry-tests').
    - FOUNDRY_PROJECT_ROOT: Default working directory inside the sandbox if
                            project_root is not provided (default: current
                            working directory path).
    - FOUNDRY_SANDBOX_TIMEOUT: Maximum lifetime of the sandbox in seconds
                               (default: 900).

    Args:
        project_root: Directory (inside the sandbox filesystem) containing the
                      Foundry project. If not provided, FOUNDRY_PROJECT_ROOT
                      env var or the current working directory is used.
        extra_args:   Optional additional CLI flags for `forge test`
                      (e.g. ['-vvv']).

    Returns a dict with stdout, stderr, exit_code, project_root, and success flag.
    """
    try:
        default_root = Path.cwd() / "generated_contracts"
        root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT")
        if not root:
            root = str(default_root if default_root.exists() else Path.cwd())

        # Build the underlying forge command. The Modal image ensures that
        # /root/.foundry/bin is added to PATH via /root/.bashrc, so running
        # through `bash -lc` will pick it up.
        forge_cmd = ["forge", "test"]
        user_args = list(extra_args or [])
        forge_cmd.extend(user_args)

        # Always keep discovery scoped to canonical Foundry test files unless
        # the caller already provided an explicit match filter.
        has_match_filter = any(
            a in ("--match-path", "--match-test", "--match-contract")
            for a in user_args
        )
        if not has_match_filter:
            forge_cmd.extend(["--match-path", "test/*Test.t.sol"])

        # Inject remappings unless the caller already set custom remappings.
        has_remappings = any(
            a == "--remappings" or a.startswith("--remappings=") for a in user_args
        )
        if not has_remappings:
            forge_cmd.extend(
                [
                    "--remappings",
                    "@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/",
                    "--remappings",
                    "forge-std/=lib/forge-std/src/",
                ]
            )

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))

        # Look up or create the Modal app context for sandboxes.
        app = modal.App.lookup(app_name, create_if_missing=True)

        # Ensure the sandbox can actually see the Foundry project files by
        # packaging the local project directory into the sandbox image.
        local_root = Path(root)
        sandbox_workdir = root
        sandbox_image = foundry_image
        if local_root.exists():
            # Ensure a basic Foundry config exists and uses the project's layout.
            # This keeps compilation deterministic for generated_contracts/*
            # where contracts are under contracts/ and tests under test/.
            foundry_toml = local_root / "foundry.toml"
            if not foundry_toml.exists():
                foundry_toml.write_text(
                    (
                        "[profile.default]\n"
                        "src = \"contracts\"\n"
                        "test = \"test\"\n"
                        "libs = [\"lib\"]\n"
                        "remappings = [\n"
                        "  \"@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/\",\n"
                        "  \"forge-std/=lib/forge-std/src/\"\n"
                        "]\n"
                    ),
                    encoding="utf-8",
                )

            sandbox_workdir = "/workspace/project"
            sandbox_image = foundry_image.add_local_dir(
                local_path=str(local_root),
                remote_path=sandbox_workdir,
            )

        bootstrap_cmd = (
            "set -e; "
            "mkdir -p lib; "
            "if [ ! -d lib/forge-std ]; then "
            "  git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std; "
            "fi; "
            "if [ ! -d lib/openzeppelin-contracts ]; then "
            "  git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts lib/openzeppelin-contracts; "
            "fi; "
            + " ".join(forge_cmd)
        )

        sandbox = modal.Sandbox.create(
            "bash",
            "-lc",
            bootstrap_cmd,
            image=sandbox_image,
            app=app,
            workdir=sandbox_workdir,
            timeout=timeout,
        )

        # Read full stdout/stderr streams and wait for completion.
        stdout = sandbox.stdout.read()
        stderr = sandbox.stderr.read()
        sandbox.wait(raise_on_termination=False)
        exit_code = sandbox.returncode

        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        testing_state = data["agents"]["testing"]

        history: List[Dict[str, Any]] = testing_state.get("last_test_results", [])
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project_root": root,
            "sandbox_workdir": sandbox_workdir,
            "command": " ".join(forge_cmd),
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "modal_app": app_name,
        }
        history.append(entry)
        testing_state["last_test_results"] = history

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="testing",
            action="foundry_tests_run",
            output_produced=entry,
            why="Testing agent executed forge test inside a Modal Sandbox",
            how="run_foundry_tests tool (Modal Sandbox)",
            error=None if exit_code == 0 else "forge test reported failures",
        )

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
        return {"error": f"Could not run forge test in Modal Sandbox: {str(e)}"}


@tool
def save_testing_note(note: str) -> Dict[str, Any]:
    """
    Save a testing-related note (issues found, coverage gaps, observations)
    for this user in the testing agent's memory slice.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        testing_state = data["agents"]["testing"]

        notes: List[Dict[str, Any]] = testing_state.get("notes", [])
        notes.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": note,
            }
        )
        testing_state["notes"] = notes

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="testing",
            action="testing_note_saved",
            output_produced={"note": note},
            why="Testing agent recorded a test-related issue or observation",
            how="save_testing_note tool",
        )

        return {"success": True}
    except Exception as e:
        return {"error": f"Could not save testing note: {str(e)}"}


TESTING_TOOLS = [
    planning_get_current_plan,
    coding_get_current_artifacts,
    coding_load_code_artifact,
    generate_foundry_tests,
    save_test_artifact,
    run_foundry_tests,
    save_testing_note,
]