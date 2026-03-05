import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
import modal

from modal_foundry_app import foundry_image
from schemas.coding_schema import CodeArtifact
from schemas.deployment_schema import (
    DeploymentTarget,
    DeploymentRecord,
    FoundryDeployScriptGenerationRequest,
    FoundryDeployRequest,
)
from agents.code_storage import LocalCodeStorage
from agents.planning_tools import get_current_plan as planning_get_current_plan
from agents.coding_tools import (
    get_current_artifacts as coding_get_current_artifacts,
    load_code_artifact as coding_load_code_artifact,
)


def _get_memory_manager(user_id: str = "default"):
    from agents.memory_manager import MemoryManager

    return MemoryManager(user_id=user_id)


def _redact_text(value: Optional[str], secrets: List[str]) -> str:
    if not value:
        return ""
    redacted = value
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


def _extract_first(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _parse_deploy_output(stdout: str, stderr: str) -> Dict[str, Optional[str]]:
    combined = f"{stdout}\n{stderr}"
    tx_hash = _extract_first(r"(0x[a-fA-F0-9]{64})", combined)

    # Prefer lines that look like deployment output if present.
    deployed_address = _extract_first(
        r"(?:deployed to|deployed at|contract address)\s*[:=]\s*(0x[a-fA-F0-9]{40})",
        combined,
    )
    if not deployed_address:
        deployed_address = _extract_first(r"\b(0x[a-fA-F0-9]{40})\b", combined)

    return {"tx_hash": tx_hash, "deployed_address": deployed_address}


def generate_foundry_deploy_script_direct(
    request: FoundryDeployScriptGenerationRequest,
) -> Dict[str, Any]:
    constraints_section = ""
    if request.constraints:
        joined = "\n".join(f"- {c}" for c in request.constraints)
        constraints_section = f"\n\nDeployment constraints:\n{joined}"

    plan_section = ""
    if request.plan_summary:
        plan_section = f"\n\nValidated plan summary:\n{request.plan_summary.strip()}"

    source_section = ""
    if request.contract_sources:
        source_section = (
            "\n\nSolidity source context:\n"
            "---------------- SOURCE START ----------------\n"
            f"{request.contract_sources.strip()}\n"
            "----------------- SOURCE END -----------------\n"
        )

    args_comment = ", ".join(request.constructor_args) if request.constructor_args else "none"
    prompt = (
        "You are a Solidity deployment expert.\n"
        "Generate a Foundry deployment script for Avalanche Fuji.\n\n"
        "Requirements:\n"
        "- Output ONLY raw Solidity code (no markdown fences, no prose).\n"
        "- File target is script/<Name>.s.sol.\n"
        "- Use pragma solidity ^0.8.x and import Script from forge-std.\n"
        "- Import target contract from ../contracts/<ContractName>.sol.\n"
        "- Define contract name exactly as requested.\n"
        "- Implement run() with vm.startBroadcast() and vm.stopBroadcast().\n"
        "- Deploy exactly one requested contract instance.\n"
        "- Keep constructor arguments hardcoded literals in script for v1.\n"
        "- Add brief inline comments where non-obvious.\n\n"
        f"Deployment goal:\n{request.goal.strip()}\n\n"
        f"Target contract name: {request.contract_name}\n"
        f"Script contract name: {request.script_name}\n"
        f"Constructor arguments (Solidity literals): {args_comment}"
        f"{constraints_section}"
        f"{plan_section}"
        f"{source_section}"
    )

    model_name = os.getenv("FOUNDRY_DEPLOY_SCRIPT_MODEL", "gpt-5.2-2025-12-11")
    llm = ChatOpenAI(model=model_name, temperature=0.1)

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        generated_text = response.content or ""
    except Exception as e:
        return {"error": f"Failed to generate Foundry deployment script: {str(e)}"}

    generated_text = generated_text.replace("../src/", "../contracts/")
    generated_text = generated_text.replace(
        'import "forge-std/Script.sol";',
        'import {Script} from "forge-std/Script.sol";',
    )
    generated_text = generated_text.replace(
        'import "forge-std/console2.sol";',
        'import {console2} from "forge-std/console2.sol";',
    )

    return {
        "goal": request.goal,
        "contract_name": request.contract_name,
        "script_name": request.script_name,
        "generated_script": generated_text,
    }


@tool
def generate_foundry_deploy_script(
    request: FoundryDeployScriptGenerationRequest,
) -> Dict[str, Any]:
    """
    Generate a Foundry Solidity deployment script for Avalanche Fuji.
    """
    return generate_foundry_deploy_script_direct(request)


@tool
def save_deploy_artifact(artifact: CodeArtifact) -> Dict[str, Any]:
    """
    Persist generated deployment script files and store metadata in deployment state.
    """
    try:
        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        deployment_state = data["agents"]["deployment"]

        storage = LocalCodeStorage()

        raw = artifact.model_dump()
        code = raw.pop("code", None)

        if code:
            stored_path = storage.save_code(artifact, code)
            raw["path"] = stored_path

        artifacts: List[Dict[str, Any]] = deployment_state.get("artifacts", [])
        artifacts.append(raw)
        deployment_state["artifacts"] = artifacts

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="deployment",
            action="deploy_artifact_saved",
            output_produced=raw,
            why="Deployment agent saved or updated a Foundry deployment script artifact",
            how="save_deploy_artifact tool",
        )

        return {"success": True, "artifact_path": raw.get("path", artifact.path)}
    except Exception as e:
        return {"error": f"Could not save deployment artifact: {str(e)}"}


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
def run_foundry_deploy(
    request: FoundryDeployRequest,
    project_root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Execute `forge script ... --broadcast` for Avalanche Fuji in a Modal Sandbox.
    Requires FUJI_RPC_URL and FUJI_PRIVATE_KEY to be set in the environment.
    """
    try:
        if request.network != "avalanche_fuji":
            return {
                "error": (
                    "Unsupported network. This deployment tool currently supports "
                    "only avalanche_fuji."
                )
            }

        rpc_url = os.getenv(request.rpc_url_env_var)
        private_key = os.getenv(request.private_key_env_var)
        if not rpc_url:
            return {"error": f"Missing required env var: {request.rpc_url_env_var}"}
        if not private_key:
            return {"error": f"Missing required env var: {request.private_key_env_var}"}

        default_root = Path.cwd() / "generated_contracts"
        root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT")
        if not root:
            root = str(default_root if default_root.exists() else Path.cwd())

        forge_cmd = [
            "forge",
            "script",
            request.script_path,
            "--rpc-url",
            f"${request.rpc_url_env_var}",
            "--private-key",
            f"${request.private_key_env_var}",
        ]
        if request.broadcast:
            forge_cmd.append("--broadcast")
        if request.contract_name:
            forge_cmd.extend(["--tc", request.contract_name])
        forge_cmd.extend(request.extra_args or [])

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))
        app = modal.App.lookup(app_name, create_if_missing=True)

        local_root = Path(root)
        sandbox_workdir = root
        sandbox_image = foundry_image
        if local_root.exists():
            foundry_toml = local_root / "foundry.toml"
            if not foundry_toml.exists():
                foundry_toml.write_text(
                    (
                        "[profile.default]\n"
                        "src = \"contracts\"\n"
                        "test = \"test\"\n"
                        "script = \"script\"\n"
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
            env={
                request.rpc_url_env_var: rpc_url,
                request.private_key_env_var: private_key,
            },
        )

        stdout_raw = sandbox.stdout.read()
        stderr_raw = sandbox.stderr.read()
        sandbox.wait(raise_on_termination=False)
        exit_code = sandbox.returncode

        secrets = [rpc_url, private_key]
        stdout = _redact_text(stdout_raw, secrets)
        stderr = _redact_text(stderr_raw, secrets)
        parsed = _parse_deploy_output(stdout, stderr)
        command_display = " ".join(forge_cmd)

        mm = _get_memory_manager()
        data, block = mm._read_user_block()  # type: ignore[attr-defined]
        mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
        deployment_state = data["agents"]["deployment"]

        history: List[Dict[str, Any]] = deployment_state.get("last_deploy_results", [])
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "project_root": root,
            "sandbox_workdir": sandbox_workdir,
            "network": request.network,
            "chain_id": request.chain_id,
            "script_path": request.script_path,
            "command": command_display,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "modal_app": app_name,
            "tx_hash": parsed.get("tx_hash"),
            "deployed_address": parsed.get("deployed_address"),
        }
        history.append(entry)
        deployment_state["last_deploy_results"] = history

        mm.client.blocks.update(  # type: ignore[attr-defined]
            block.id,
            value=mm._serialize(data),  # type: ignore[attr-defined]
        )

        mm.log_agent_action(
            agent_name="deployment",
            action="foundry_deploy_run",
            output_produced=entry,
            why="Deployment agent executed forge script broadcast in Modal Sandbox",
            how="run_foundry_deploy tool (Modal Sandbox)",
            error=None if exit_code == 0 else "forge script returned non-zero exit code",
        )

        return {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "project_root": root,
            "sandbox_workdir": sandbox_workdir,
            "modal_app": app_name,
            "network": request.network,
            "chain_id": request.chain_id,
            "script_path": request.script_path,
            "command": command_display,
            "tx_hash": parsed.get("tx_hash"),
            "deployed_address": parsed.get("deployed_address"),
        }
    except Exception as e:
        return {"error": f"Could not run forge deployment in Modal Sandbox: {str(e)}"}


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
            "targets": state.get("targets", []),
            "artifacts": state.get("artifacts", []),
            "last_deploy_results": state.get("last_deploy_results", []),
            "deployments": state.get("deployments", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve deployment history: {str(e)}"}


DEPLOYMENT_TOOLS = [
    planning_get_current_plan,
    coding_get_current_artifacts,
    coding_load_code_artifact,
    generate_foundry_deploy_script,
    save_deploy_artifact,
    save_deployment_target,
    run_foundry_deploy,
    record_deployment,
    get_deployment_history,
]
