import os
import shlex
import sys
import threading
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

from agents.contract_identity import validate_artifact_for_save
from schemas.coding_schema import CodeArtifact
from agents.code_storage import get_code_storage, save_execution_logs
from agents.planning_tools import get_current_plan as planning_get_current_plan
from agents.coding_tools import (
    get_current_artifacts as coding_get_current_artifacts,
    load_code_artifact as coding_load_code_artifact,
    ensure_chainlink_contracts as coding_ensure_chainlink_contracts,
)
from agents.modal_runtime import (
    build_foundry_bootstrap_cmd,
    build_project_volume_name,
    default_foundry_remappings,
    get_modal_app,
    get_modal_volume,
)
from agents.pipeline_context import compact_execution_summary
from agents.pipeline_cancel import is_pipeline_cancelled
from agents.task_tools import TASK_TOOLS
from agents.tracing import current_trace_id, start_span


def _get_memory_manager():
    """Lazy import helper. Uses project/user from context."""
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


def _record_test_result(
    *,
    project_id: str | None,
    pipeline_run_id: str | None,
    pipeline_task_id: str | None,
    root: str,
    sandbox_workdir: str,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    modal_app: str,
) -> dict:
    stdout_path, stderr_path = save_execution_logs(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        stdout=stdout,
        stderr=stderr,
    )

    mm = _get_memory_manager()
    testing_state = mm.get_agent_state("testing")

    history: List[Dict[str, Any]] = testing_state.get("last_test_results", [])
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pipeline_run_id": pipeline_run_id,
        "pipeline_task_id": pipeline_task_id,
        "project_root": root,
        "sandbox_workdir": sandbox_workdir,
        "command": command,
        "exit_code": exit_code,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "summary": compact_execution_summary(exit_code, stdout, stderr),
        "modal_app": modal_app,
    }
    history.append(entry)
    testing_state["last_test_results"] = history
    testing_state["last_test_status"] = "passed" if exit_code == 0 else "failed"
    mm.set_agent_state("testing", testing_state)
    return entry


def _terminate_sandbox(sandbox: Any) -> None:
    for method_name in ("terminate", "kill"):
        method = getattr(sandbox, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                pass
            return


def _safe_stream_read(stream: Any) -> str:
    if stream is None:
        return ""
    try:
        return stream.read()
    except Exception:
        return ""


def _wait_for_sandbox_completion(
    sandbox: Any,
    pipeline_run_id: str | None,
    *,
    poll_interval_s: float = 1.0,
) -> bool:
    wait_exc: list[Exception] = []

    def _wait() -> None:
        try:
            sandbox.wait(raise_on_termination=False)
        except TypeError:
            sandbox.wait()
        except Exception as exc:  # pragma: no cover - defensive
            wait_exc.append(exc)

    worker = threading.Thread(target=_wait, daemon=True)
    worker.start()

    while worker.is_alive():
        if pipeline_run_id and is_pipeline_cancelled(pipeline_run_id):
            _terminate_sandbox(sandbox)
            worker.join(timeout=2)
            return True
        worker.join(timeout=poll_interval_s)

    if wait_exc:
        raise wait_exc[0]
    return False


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
        '- Keep `forge-std` import as `import {Test} from "forge-std/Test.sol";`.\n'
        "- OpenZeppelin imports MUST use `@openzeppelin/contracts/...` or\n"
        "  `@openzeppelin/contracts-upgradeable/...` remappings as appropriate.\n"
        "- Cover constructor behaviour, state initialisation, happy-path flows,\n"
        "  revert conditions, access control, and relevant edge cases.\n"
        "- When mocking a Solidity interface, the mock contract MUST implement every\n"
        "  function of that interface (or the contract must be marked abstract and cannot\n"
        "  be instantiated). For Chainlink AggregatorV3Interface, implement ALL of:\n"
        "  decimals(), description(), version(), getRoundData(uint80), latestRoundData().\n"
        '  Use stub return values (e.g. 0, "", or a struct of zeros) where the test\n'
        "  does not depend on them.\n"
        "- Output ONLY raw Solidity test contracts (no markdown fences, no prose).\n\n"
        f"Testing goal:\n{request.goal.strip()}"
        f"{constraints_section}"
        f"{plan_section}"
        f"{source_section}"
    )

    model_name = os.getenv("FOUNDRY_TEST_MODEL", "gpt-5.2-2025-12-11")
    llm = ChatOpenAI(model=model_name, temperature=0.1)

    try:
        with start_span(
            "model.call",
            {
                "task_type": "testing.generate_tests",
                "model": model_name,
            },
        ) as span:
            response = llm.invoke([HumanMessage(content=prompt)])
            usage = getattr(response, "usage_metadata", None) or {}
            total_tokens = usage.get("total_tokens")
            if total_tokens is not None:
                span.set_attribute("token_count", int(total_tokens))
        generated_text = response.content or ""
    except Exception as e:
        return {"error": f"Failed to call Foundry test generation model: {str(e)}"}

    # Guardrail normalization to keep generated tests aligned with this repo's
    # Foundry layout, even if the model drifts to common src/ conventions.
    generated_text = generated_text.replace("../src/", "../contracts/")
    generated_text = generated_text.replace(
        'import "forge-std/Test.sol";', 'import {Test} from "forge-std/Test.sol";'
    )

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
        testing_state = mm.get_agent_state("testing")
        plan = mm.get_plan()

        storage = get_code_storage()

        raw = artifact.model_dump()
        code = raw.pop("code", None)
        raw, issues = validate_artifact_for_save(plan, raw)
        if issues:
            return {"error": "; ".join(issues)}

        if code:
            with start_span(
                "artifact.write",
                {
                    "artifact.path": artifact.path,
                    "artifact.language": artifact.language,
                },
            ):
                stored_path = storage.save_code(artifact, code)
            raw["path"] = stored_path

        artifacts: List[Dict[str, Any]] = testing_state.get("artifacts", [])
        artifacts.append(raw)
        testing_state["artifacts"] = artifacts
        mm.set_agent_state("testing", testing_state)

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
        from agents.context import (
            get_pipeline_run_id,
            get_pipeline_task_id,
            get_project_context,
        )

        project_id_ctx, _ = get_project_context()
        pipeline_run_id = get_pipeline_run_id()
        pipeline_task_id = get_pipeline_task_id()
        default_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
        if project_id_ctx:
            default_root = f"{default_root.rstrip('/')}/{project_id_ctx}"
        root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT") or default_root
        trace_id = current_trace_id()

        # Build the underlying forge command. The Modal image ensures that
        # /root/.foundry/bin is added to PATH via /root/.bashrc, so running
        # through `bash -lc` will pick it up.
        forge_cmd = ["forge", "test"]
        user_args = list(extra_args or [])
        forge_cmd.extend(user_args)

        # Always keep discovery scoped to canonical Foundry test files unless
        # the caller already provided an explicit match filter.
        has_match_filter = any(
            a in ("--match-path", "--match-test", "--match-contract") for a in user_args
        )
        if not has_match_filter:
            forge_cmd.extend(["--match-path", "test/*Test.t.sol"])

        # Inject remappings unless the caller already set custom remappings.
        has_remappings = any(
            a == "--remappings" or a.startswith("--remappings=") for a in user_args
        )
        if not has_remappings:
            forge_cmd.extend(default_foundry_remappings())

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))

        app = get_modal_app(app_name)

        base_volume_name = os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        volume_name = build_project_volume_name(base_volume_name, project_id_ctx)
        vol = get_modal_volume(volume_name)

        sandbox_workdir = "/workspace/project"
        sandbox_image = foundry_image

        forge_cmd_str = " ".join(shlex.quote(part) for part in forge_cmd)
        bootstrap_cmd = build_foundry_bootstrap_cmd(root, forge_cmd_str)

        with start_span(
            "test.execute",
            {
                "project_id": project_id_ctx,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "artifact_revision": 0,
            },
        ) as span:
            sandbox = modal.Sandbox.create(
                "bash",
                "-lc",
                bootstrap_cmd,
                image=sandbox_image,
                app=app,
                workdir=sandbox_workdir,
                timeout=timeout,
                volumes={sandbox_workdir: vol},
            )

            cancelled = _wait_for_sandbox_completion(sandbox, pipeline_run_id)
            stdout = _safe_stream_read(getattr(sandbox, "stdout", None))
            stderr = _safe_stream_read(getattr(sandbox, "stderr", None))
            exit_code = getattr(sandbox, "returncode", None)
            if cancelled:
                exit_code = 130 if exit_code is None else exit_code
                span.set_attribute("failure_class", "cancelled")
            elif exit_code is not None:
                span.set_attribute("exit_code", int(exit_code))
        entry = _record_test_result(
            project_id=project_id_ctx,
            pipeline_run_id=pipeline_run_id,
            pipeline_task_id=pipeline_task_id,
            root=root,
            sandbox_workdir=sandbox_workdir,
            command=" ".join(forge_cmd),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            modal_app=app_name,
        )
        stdout_path = entry["stdout_path"]
        stderr_path = entry["stderr_path"]

        mm = _get_memory_manager()

        try:
            mm.save_test_run(
                status=(
                    "cancelled"
                    if cancelled
                    else ("passed" if exit_code == 0 else "failed")
                ),
                tests_run=None,
                tests_passed=None,
                output=compact_execution_summary(exit_code or 0, stdout, stderr),
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                exit_code=exit_code,
                trace_id=trace_id,
            )
        except Exception:
            pass

        mm.log_agent_action(
            agent_name="testing",
            action="foundry_tests_run",
            output_produced=entry,
            why="Testing agent executed forge test inside a Modal Sandbox",
            how="run_foundry_tests tool (Modal Sandbox)",
            error=None if exit_code == 0 else "forge test reported failures",
        )

        return {
            "success": exit_code == 0 and not cancelled,
            "cancelled": cancelled,
            "exit_code": exit_code,
            "pipeline_run_id": pipeline_run_id,
            "pipeline_task_id": pipeline_task_id,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "project_root": root,
            "sandbox_workdir": sandbox_workdir,
            "modal_app": app_name,
        }
    except Exception as e:
        try:
            from agents.context import (
                get_pipeline_run_id,
                get_pipeline_task_id,
                get_project_context,
            )

            project_id_ctx, _ = get_project_context()
            pipeline_run_id = get_pipeline_run_id()
            pipeline_task_id = get_pipeline_task_id()
            default_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
            if project_id_ctx:
                default_root = f"{default_root.rstrip('/')}/{project_id_ctx}"
            root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT") or default_root
            sandbox_workdir = "/workspace/project"
            app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
            command = "forge test"
            entry = _record_test_result(
                project_id=project_id_ctx,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                root=root,
                sandbox_workdir=sandbox_workdir,
                command=command,
                exit_code=1,
                stdout="",
                stderr=str(e),
                modal_app=app_name,
            )
            try:
                _get_memory_manager().save_test_run(
                    status="failed",
                    tests_run=None,
                    tests_passed=None,
                    output=compact_execution_summary(1, "", str(e)),
                    pipeline_run_id=pipeline_run_id,
                    pipeline_task_id=pipeline_task_id,
                    stdout_path=entry["stdout_path"],
                    stderr_path=entry["stderr_path"],
                    exit_code=1,
                    trace_id=current_trace_id(),
                )
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": 1,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "stdout": "",
                "stderr": str(e),
                "stdout_path": entry["stdout_path"],
                "stderr_path": entry["stderr_path"],
                "project_root": root,
                "sandbox_workdir": sandbox_workdir,
                "modal_app": app_name,
                "error": f"Could not run forge test in Modal Sandbox: {str(e)}",
            }
        except Exception:
            return {"error": f"Could not run forge test in Modal Sandbox: {str(e)}"}


@tool
def save_testing_note(note: str) -> Dict[str, Any]:
    """
    Save a testing-related note (issues found, coverage gaps, observations)
    for this user in the testing agent's memory slice.
    """
    try:
        mm = _get_memory_manager()
        testing_state = mm.get_agent_state("testing")

        notes: List[Dict[str, Any]] = testing_state.get("notes", [])
        notes.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": note,
            }
        )
        testing_state["notes"] = notes
        mm.set_agent_state("testing", testing_state)

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
    coding_ensure_chainlink_contracts,
    generate_foundry_tests,
    save_test_artifact,
    run_foundry_tests,
    save_testing_note,
] + TASK_TOOLS
