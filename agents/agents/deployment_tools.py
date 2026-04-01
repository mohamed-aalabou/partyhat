import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional

# Platform limit for tool response payload (e.g. Modal/OpenAI). Stay under to avoid INVALID_ARGUMENT.
MAX_RESPONSE_CHARS = 48_000

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
    SnowtraceVerifyRequest,
)
from agents.code_storage import LocalCodeStorage
from agents.planning_tools import get_current_plan as planning_get_current_plan
from agents.coding_tools import (
    get_current_artifacts as coding_get_current_artifacts,
    load_code_artifact as coding_load_code_artifact,
)
from agents.code_storage import get_code_storage


def _get_memory_manager():
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


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


def _normalize_private_key_hex(value: str) -> str:
    normalized = (value or "").strip()
    if normalized.startswith(("0x", "0X")):
        return f"0x{normalized[2:]}"
    if re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        return f"0x{normalized}"
    return normalized


def _truncate_for_display(text: str, max_chars: int, label: str = "output") -> str:
    """Return text truncated to max_chars with head and tail kept and a middle notice."""
    if not text or len(text) <= max_chars:
        return text
    notice = f"\n... [{label} truncated for platform limit] ...\n"
    half = (max_chars - len(notice)) // 2
    return text[:half] + notice + text[-half:]


def _cap_response_with_stdout_stderr(
    response: Dict[str, Any], truncation_note: str
) -> Dict[str, Any]:
    """If response JSON would exceed MAX_RESPONSE_CHARS, truncate stdout/stderr."""
    payload = json.dumps(response)
    if len(payload) <= MAX_RESPONSE_CHARS:
        return response
    response = dict(response)
    overhead = len(
        json.dumps(
            {
                **response,
                "stdout": "",
                "stderr": "",
                "output_truncated": True,
                "truncation_note": truncation_note,
            }
        )
    )
    allowance = max(0, MAX_RESPONSE_CHARS - overhead - 200)
    max_stdout = allowance // 2
    max_stderr = allowance - max_stdout
    if response.get("stdout") and len(response["stdout"]) > max_stdout:
        response["stdout"] = _truncate_for_display(
            response["stdout"], max_stdout, "stdout"
        )
    if response.get("stderr") and len(response["stderr"]) > max_stderr:
        response["stderr"] = _truncate_for_display(
            response["stderr"], max_stderr, "stderr"
        )
    response["output_truncated"] = True
    response["truncation_note"] = truncation_note
    return response


def _cap_deploy_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """If response JSON would exceed MAX_RESPONSE_CHARS, truncate stdout/stderr in place."""
    return _cap_response_with_stdout_stderr(
        response, "stdout/stderr truncated to stay under 50k platform limit."
    )


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
        "- Load PRIVATE_KEY in a robust way: accept both `0x`-prefixed and non-prefixed hex env values.\n"
        "- Prefer `vm.envString(\"FUJI_PRIVATE_KEY\")` + normalization + `vm.parseUint(...)` over plain `vm.envUint(...)`.\n"
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

        storage = get_code_storage()

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
    Set quiet_output=True to avoid high verbosity and to truncate stdout/stderr so the
    response stays under the platform 50k character limit (use if you get INVALID_ARGUMENT
    response length errors). tx_hash and deployed_address are always included.
    """
    try:
        from agents.context import get_project_context

        project_id_ctx, _ = get_project_context()
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
        private_key = _normalize_private_key_hex(private_key)

        default_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
        if project_id_ctx:
            default_root = f"{default_root.rstrip('/')}/{project_id_ctx}"
        root = project_root or os.getenv("FOUNDRY_PROJECT_ROOT") or default_root

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
        user_args = list(request.extra_args or [])
        if request.quiet_output:
            # Strip high verbosity so forge output stays smaller and under platform limit
            user_args = [
                a
                for a in user_args
                if a not in ("-v", "-vv", "-vvv", "-vvvv")
            ]
        forge_cmd.extend(user_args)

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
                    "--remappings",
                    "@chainlink/contracts/=lib/chainlink-evm/contracts/",
                    "--remappings",
                    "@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/",
                ]
            )

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))
        app = modal.App.lookup(app_name, create_if_missing=True)
        base_volume_name = os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        volume_name = (
            f"{base_volume_name}-{project_id_ctx}"
            if project_id_ctx
            else base_volume_name
        )
        vol = modal.Volume.from_name(volume_name, create_if_missing=True)

        sandbox_workdir = "/workspace/project"
        sandbox_image = foundry_image

        quoted_root = shlex.quote(root)
        forge_cmd_str = " ".join(
            part
            if isinstance(part, str) and part.startswith("$")
            else shlex.quote(str(part))
            for part in forge_cmd
        )
        bootstrap_cmd = (
            "set -e; "
            + f"cd {quoted_root}; "
            + "mkdir -p lib; "
            "if [ ! -d lib/forge-std ]; then "
            "  if [ -d /opt/foundry-deps/forge-std ]; then cp -R /opt/foundry-deps/forge-std lib/forge-std; "
            "  else git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std; fi; "
            "fi; "
            "if [ ! -d lib/openzeppelin-contracts ]; then "
            "  if [ -d /opt/foundry-deps/openzeppelin-contracts ]; then cp -R /opt/foundry-deps/openzeppelin-contracts lib/openzeppelin-contracts; "
            "  else git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts lib/openzeppelin-contracts; fi; "
            "fi; "
            "if [ ! -d lib/chainlink-evm ]; then "
            "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
            "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
            "fi; "
            "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
            "  rm -rf lib/chainlink-evm; "
            "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
            "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
            "fi; "
            "mkdir -p lib/chainlink-evm/contracts/src/v0.8/interfaces; "
            "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol ] "
            "&& [ -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
            "  cp lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol "
            "lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol; "
            "fi; "
            + forge_cmd_str
        )

        sandbox = modal.Sandbox.create(
            "bash",
            "-lc",
            bootstrap_cmd,
            image=sandbox_image,
            app=app,
            workdir=sandbox_workdir,
            timeout=timeout,
            volumes={sandbox_workdir: vol},
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

        # Keep existing Letta behavior, and additionally persist this run in Neon
        # when DB/project context is available.
        try:
            project_uuid = mm._project_uuid()  # type: ignore[attr-defined]
            if project_uuid and getattr(mm, "_db_available", False):
                from agents.db.crud import save_deployment as db_save_deployment

                mm._db_call(  # type: ignore[attr-defined]
                    lambda session: db_save_deployment(
                        session,
                        project_id=project_uuid,
                        status="success" if exit_code == 0 else "failed",
                        contract_name=request.contract_name,
                        deployed_address=parsed.get("deployed_address"),
                        tx_hash=parsed.get("tx_hash"),
                        snowtrace_url=None,
                        network=request.network,
                    )
                )
        except Exception:
            # DB persistence must not change current tool behavior.
            pass

        mm.log_agent_action(
            agent_name="deployment",
            action="foundry_deploy_run",
            output_produced=entry,
            why="Deployment agent executed forge script broadcast in Modal Sandbox",
            how="run_foundry_deploy tool (Modal Sandbox)",
            error=None if exit_code == 0 else "forge script returned non-zero exit code",
        )

        response = {
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
        return _cap_deploy_response(response)
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


# Snowtrace (Etherscan-compatible) API base URLs per chain
SNOWTRACE_VERIFIER_URLS = {
    43113: "https://api-testnet.snowtrace.io/api",   # Fuji testnet
    43114: "https://api.snowtrace.io/api",           # Avalanche C-Chain mainnet
}


@tool
def verify_contract_on_snowtrace(
    request: SnowtraceVerifyRequest,
    project_root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Verify a deployed contract on Snowtrace (Avalanche C-Chain block explorer) using
    forge verify-contract. Run this after a successful deployment to publish source
    code on Snowtrace. Supports Fuji (43113) and mainnet (43114).
    """
    try:
        from agents.context import get_project_context

        project_id_ctx, _ = get_project_context()
        verifier_url = SNOWTRACE_VERIFIER_URLS.get(request.chain_id)
        if not verifier_url:
            return {
                "error": (
                    f"Unsupported chain_id {request.chain_id}. "
                    "Use 43113 (Fuji) or 43114 (C-Chain mainnet)."
                )
            }

        api_key = os.getenv(request.api_key_env_var) or "placeholder"
        default_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts")
        if project_id_ctx:
            default_root = f"{default_root.rstrip('/')}/{project_id_ctx}"
        root = project_root or request.project_root or os.getenv("FOUNDRY_PROJECT_ROOT") or default_root

        forge_cmd = [
            "forge",
            "verify-contract",
            request.contract_address,
            request.contract_path,
            "--verifier",
            "etherscan",
            "--verifier-url",
            verifier_url,
            "--etherscan-api-key",
            api_key,
            "--chain-id",
            str(request.chain_id),
            "--watch",
        ]
        if request.constructor_args:
            forge_cmd.extend(["--constructor-args", request.constructor_args])
        if request.compiler_version:
            forge_cmd.extend(["--compiler-version", request.compiler_version])
        if request.optimizer_runs is not None:
            forge_cmd.extend(["--optimizer-runs", str(request.optimizer_runs)])

        remappings = [
            "--remappings",
            "@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/",
            "--remappings",
            "forge-std/=lib/forge-std/src/",
            "--remappings",
            "@chainlink/contracts/=lib/chainlink-evm/contracts/",
            "--remappings",
            "@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/",
        ]
        forge_cmd.extend(remappings)

        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))
        app = modal.App.lookup(app_name, create_if_missing=True)
        base_volume_name = os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        volume_name = (
            f"{base_volume_name}-{project_id_ctx}"
            if project_id_ctx
            else base_volume_name
        )
        vol = modal.Volume.from_name(volume_name, create_if_missing=True)
        sandbox_workdir = "/workspace/project"
        sandbox_image = foundry_image

        quoted_root = shlex.quote(root)
        forge_cmd_str = " ".join(shlex.quote(str(part)) for part in forge_cmd)
        bootstrap_cmd = (
            "set -e; "
            + f"cd {quoted_root}; "
            + "mkdir -p lib; "
            "if [ ! -d lib/forge-std ]; then "
            "  if [ -d /opt/foundry-deps/forge-std ]; then cp -R /opt/foundry-deps/forge-std lib/forge-std; "
            "  else git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std; fi; "
            "fi; "
            "if [ ! -d lib/openzeppelin-contracts ]; then "
            "  if [ -d /opt/foundry-deps/openzeppelin-contracts ]; then cp -R /opt/foundry-deps/openzeppelin-contracts lib/openzeppelin-contracts; "
            "  else git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts lib/openzeppelin-contracts; fi; "
            "fi; "
            "if [ ! -d lib/chainlink-evm ]; then "
            "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
            "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
            "fi; "
            "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
            "  rm -rf lib/chainlink-evm; "
            "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
            "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
            "fi; "
            "mkdir -p lib/chainlink-evm/contracts/src/v0.8/interfaces; "
            "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol ] "
            "&& [ -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
            "  cp lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol "
            "lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol; "
            "fi; "
            + forge_cmd_str
        )

        sandbox = modal.Sandbox.create(
            "bash",
            "-lc",
            bootstrap_cmd,
            image=sandbox_image,
            app=app,
            workdir=sandbox_workdir,
            timeout=timeout,
            volumes={sandbox_workdir: vol},
            env={request.api_key_env_var: api_key},
        )

        stdout_raw = sandbox.stdout.read()
        stderr_raw = sandbox.stderr.read()
        sandbox.wait(raise_on_termination=False)
        exit_code = sandbox.returncode

        stdout = _redact_text(stdout_raw, [api_key] if api_key != "placeholder" else [])
        stderr = _redact_text(stderr_raw, [api_key] if api_key != "placeholder" else [])

        mm = _get_memory_manager()
        mm.log_agent_action(
            agent_name="deployment",
            action="snowtrace_verify",
            output_produced={
                "contract_address": request.contract_address,
                "contract_path": request.contract_path,
                "chain_id": request.chain_id,
                "exit_code": exit_code,
                "success": exit_code == 0,
            },
            why="Deployment agent ran Snowtrace contract verification",
            how="verify_contract_on_snowtrace tool",
            error=None if exit_code == 0 else "forge verify-contract returned non-zero exit code",
        )

        explorer_base = "https://testnet.snowtrace.io" if request.chain_id == 43113 else "https://snowtrace.io"
        response = {
            "success": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "contract_address": request.contract_address,
            "contract_path": request.contract_path,
            "chain_id": request.chain_id,
            "verifier_url": verifier_url,
            "explorer_link": f"{explorer_base}/address/{request.contract_address}#code",
        }
        return _cap_response_with_stdout_stderr(
            response, "stdout/stderr truncated to stay under 50k platform limit."
        )
    except Exception as e:
        return {"error": f"Snowtrace verification failed: {str(e)}"}


DEPLOYMENT_TOOLS = [
    planning_get_current_plan,
    coding_get_current_artifacts,
    coding_load_code_artifact,
    generate_foundry_deploy_script,
    save_deploy_artifact,
    save_deployment_target,
    run_foundry_deploy,
    verify_contract_on_snowtrace,
    record_deployment,
    get_deployment_history,
]
