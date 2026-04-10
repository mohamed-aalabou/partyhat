import json
import os
import re
import shlex
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib import request as urllib_request

# Platform limit for tool response payload (e.g. Modal/OpenAI). Stay under to avoid INVALID_ARGUMENT.
MAX_RESPONSE_CHARS = 48_000

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
import modal

from modal_foundry_app import foundry_image
from agents.contract_identity import (
    enrich_artifact_with_plan_contract_ids,
    validate_artifact_for_save,
)
from agents.deployment_manifest import (
    extract_deployed_contract_references,
    load_deployment_manifest,
)
from schemas.coding_schema import CodeArtifact
from schemas.deployment_schema import (
    DeployedContractResult,
    DeploymentTarget,
    DeploymentRecord,
    ExecutedCallResult,
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
from agents.task_tools import TASK_TOOLS
from agents.code_storage import get_code_storage, save_execution_logs
from agents.modal_runtime import (
    build_foundry_bootstrap_cmd,
    build_project_volume_name,
    default_foundry_remappings,
    get_modal_app,
    get_modal_volume,
)
from agents.pipeline_cancel import is_pipeline_cancelled
from agents.pipeline_context import compact_execution_summary
from agents.tracing import current_trace_id, start_span


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


def _safe_volume_reload(volume: Any) -> None:
    try:
        volume.reload()
    except RuntimeError as e:
        if "can only be called from within a running function" in str(e):
            return
        raise
    except Exception as e:
        if "No such file or directory" in str(e):
            return
        raise


def _normalize_hex_match(value: Any, hex_len: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not re.fullmatch(rf"0x[0-9a-fA-F]{{{hex_len}}}", normalized):
        return None
    return f"0x{normalized[2:]}"


def _normalize_contract_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.rsplit(":", 1)[-1]


def _instance_name(contract_name: str, *, used: set[str] | None = None) -> str:
    pieces = [piece for piece in re.split(r"[^A-Za-z0-9]+", contract_name) if piece]
    if not pieces:
        base = "deployedContract"
    else:
        base = pieces[0][:1].lower() + pieces[0][1:]
        for piece in pieces[1:]:
            base += piece[:1].upper() + piece[1:]
    if not re.match(r"^[A-Za-z_]", base):
        base = f"contract{base}"
    candidate = base
    if used is None:
        return candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base}{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _resolve_deployment_expression(
    value: str,
    *,
    instance_names: dict[str, str],
) -> str:
    resolved = value
    for contract_name in extract_deployed_contract_references(value):
        instance_name = instance_names[contract_name]
        resolved = resolved.replace(
            f"<deployed:{contract_name}.address>",
            f"address({instance_name})",
        )
    return resolved


def _normalize_solidity_literal(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(value)


def _default_constructor_expression(arg_type: str) -> str:
    lowered = (arg_type or "").strip().lower()
    if lowered == "address":
        return "deployer"
    if lowered == "bool":
        return "false"
    if lowered == "string":
        return '""'
    if lowered.startswith("bytes"):
        return 'hex""'
    if lowered.endswith("[]"):
        base = lowered[:-2] or "uint256"
        return f"new {base}[](0)"
    if lowered.startswith("uint") or lowered.startswith("int"):
        return "0"
    return "0"


def _build_manifest_deploy_script(
    request: FoundryDeployScriptGenerationRequest,
) -> Optional[Dict[str, Any]]:
    if not request.deployment_manifest:
        return None

    try:
        manifest = load_deployment_manifest(request.deployment_manifest)
    except Exception as exc:
        return {"error": f"Failed to load deployment manifest for script generation: {exc}"}

    contracts = sorted(manifest.contracts, key=lambda entry: (entry.deploy_order, entry.name))
    if not contracts:
        return {"error": "Deployment manifest has no contracts to deploy."}

    primary = next(
        (contract for contract in contracts if contract.role == "primary_deployable"),
        contracts[0],
    )
    script_name = request.script_name or f"Deploy{primary.name}"
    used_names: set[str] = set()
    instance_names = {
        contract.name: _instance_name(contract.name, used=used_names)
        for contract in contracts
    }

    import_lines = [
        f'import {{{contract.name}}} from "../{contract.source_path}";'
        for contract in contracts
    ]
    import_block = "\n".join(dict.fromkeys(import_lines))

    deploy_lines: list[str] = []
    for contract in contracts:
        args: list[str] = []
        for item in contract.constructor_args_schema:
            if str(item.source or "") == "deployer":
                args.append("deployer")
            else:
                args.append(
                    _resolve_deployment_expression(
                        _normalize_solidity_literal(
                            item.default_value
                            if item.default_value not in (None, "")
                            else _default_constructor_expression(item.type)
                        ),
                        instance_names=instance_names,
                    )
                )
        deploy_args = ", ".join(args)
        deploy_lines.append(
            f"        // deploy-order:{contract.deploy_order} {contract.name}\n"
            f"        {contract.name} {instance_names[contract.name]} = new {contract.name}({deploy_args});"
        )

    call_lines: list[str] = []
    for call in sorted(
        manifest.post_deploy_calls,
        key=lambda entry: (entry.call_order, entry.target_contract_name, entry.function_name),
    ):
        resolved_args = [
            _resolve_deployment_expression(str(arg), instance_names=instance_names)
            for arg in call.args
        ]
        call_lines.append(
            f"        // post-deploy:{call.call_order} {call.target_contract_name}.{call.function_name}\n"
            f"        {instance_names[call.target_contract_name]}.{call.function_name}({', '.join(resolved_args)});"
        )

    body_lines = [
        "pragma solidity ^0.8.20;",
        "",
        'import {Script} from "forge-std/Script.sol";',
        import_block,
        "",
        f"contract {script_name} is Script {{",
        "    function _loadPrivateKey() internal view returns (uint256) {",
        '        string memory raw = vm.envString("FUJI_PRIVATE_KEY");',
        "        bytes memory data = bytes(raw);",
        "        if (data.length >= 2 && data[0] == 0x30 && (data[1] == 0x78 || data[1] == 0x58)) {",
        "            return vm.parseUint(raw);",
        "        }",
        '        return vm.parseUint(string.concat("0x", raw));',
        "    }",
        "",
        "    function run() external {",
        "        uint256 privateKey = _loadPrivateKey();",
        "        address deployer = vm.addr(privateKey);",
        "",
        "        vm.startBroadcast(privateKey);",
        *deploy_lines,
    ]

    if call_lines:
        body_lines.extend(["", *call_lines])

    body_lines.extend(
        [
            "        vm.stopBroadcast();",
            "    }",
            "}",
        ]
    )

    return {
        "goal": request.goal,
        "contract_name": primary.name,
        "script_name": script_name,
        "generated_script": "\n".join(body_lines) + "\n",
    }


def _contract_has_code(rpc_url: str, contract_address: str) -> bool:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [contract_address, "latest"],
            "id": 1,
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8")
    except Exception:
        return False

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False

    result = parsed.get("result")
    if not isinstance(result, str):
        return False
    normalized = result.strip().lower()
    return normalized.startswith("0x") and len(normalized) > 2


def _evaluate_deploy_success(
    *,
    exit_code: int,
    tx_hash: str | None,
    deployed_address: str | None,
    rpc_url: str,
) -> tuple[bool, Optional[str]]:
    normalized_tx_hash = _normalize_hex_match(tx_hash, 64)
    normalized_address = _normalize_hex_match(deployed_address, 40)

    if exit_code != 0:
        return False, "forge script returned non-zero exit code"
    if normalized_tx_hash:
        return True, None
    if normalized_address and _contract_has_code(rpc_url, normalized_address):
        return True, None
    if normalized_address:
        return (
            False,
            "forge script exited successfully but no deployment transaction hash was found "
            f"and contract bytecode could not be confirmed at {normalized_address}",
        )
    return (
        False,
        "forge script exited successfully but produced no deployment transaction hash "
        "or confirmed contract address",
    )


def _read_project_artifact(
    root: str,
    relative_path: str,
    *,
    volume: Any | None = None,
) -> Optional[str]:
    rel = Path(relative_path.lstrip("/"))
    if ".." in rel.parts:
        raise ValueError("Attempted to escape project root while reading artifact.")

    if volume is not None:
        _safe_volume_reload(volume)
        volume_path = (Path(str(root).lstrip("/")) / rel).as_posix().lstrip("/")
        try:
            chunks = list(volume.read_file(volume_path))
        except Exception as e:
            if "No such file or directory" in str(e):
                return None
            raise
        return b"".join(chunks).decode("utf-8")

    full_path = Path(root) / rel
    if not full_path.exists():
        return None
    return full_path.read_text(encoding="utf-8")


def _broadcast_run_artifact_path(script_path: str, chain_id: int) -> str:
    return f"broadcast/{Path(script_path).name}/{chain_id}/run-latest.json"


def _extract_returned_contract_address(
    broadcast: Dict[str, Any],
    contract_name: str | None,
) -> Optional[str]:
    expected_name = _normalize_contract_name(contract_name)
    returned_by_name, addresses = _extract_returned_contract_addresses(broadcast)
    if expected_name and expected_name in returned_by_name:
        return returned_by_name[expected_name]
    if len(addresses) == 1:
        return addresses[0]
    return None


def _extract_returned_contract_addresses(
    broadcast: Dict[str, Any],
) -> tuple[Dict[str, str], list[str]]:
    returns = broadcast.get("returns")
    if not isinstance(returns, dict):
        return {}, []

    addresses_by_name: Dict[str, str] = {}
    addresses: list[str] = []
    for value in returns.values():
        if not isinstance(value, dict):
            continue
        addr = _normalize_hex_match(value.get("value"), 40)
        if not addr:
            continue
        internal_type = str(value.get("internal_type") or value.get("internalType") or "")
        normalized_internal = _normalize_contract_name(internal_type.removeprefix("contract "))
        if normalized_internal and normalized_internal not in addresses_by_name:
            addresses_by_name[normalized_internal] = addr
        addresses.append(addr)
    return addresses_by_name, addresses


def _receipt_address_map(broadcast: Dict[str, Any]) -> Dict[str, str]:
    receipt_addresses: Dict[str, str] = {}
    receipts = broadcast.get("receipts")
    if isinstance(receipts, list):
        for receipt in receipts:
            if not isinstance(receipt, dict):
                continue
            tx_hash = _normalize_hex_match(
                receipt.get("transactionHash") or receipt.get("hash"), 64
            )
            contract_address = _normalize_hex_match(receipt.get("contractAddress"), 40)
            if tx_hash and contract_address:
                receipt_addresses[tx_hash] = contract_address
    return receipt_addresses


def _broadcast_create_transactions(
    transactions: List[Dict[str, Any]],
    *,
    receipt_addresses: Dict[str, str],
) -> List[Dict[str, Any]]:
    creates: List[Dict[str, Any]] = []
    for idx, tx in enumerate(transactions):
        if not isinstance(tx, dict):
            continue
        tx_hash = _normalize_hex_match(tx.get("hash"), 64)
        contract_address = receipt_addresses.get(tx_hash or "") or _normalize_hex_match(
            tx.get("contractAddress"), 40
        )
        tx_type = str(tx.get("transactionType") or tx.get("type") or "").upper()
        if tx_type not in {"CREATE", "CREATE2"} and contract_address is None:
            continue
        creates.append(
            {
                "index": idx,
                "tx_hash": tx_hash,
                "deployed_address": contract_address,
                "contract_name": _normalize_contract_name(tx.get("contractName")),
            }
        )
    return creates


def _broadcast_call_transactions(
    transactions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for idx, tx in enumerate(transactions):
        if not isinstance(tx, dict):
            continue
        tx_type = str(tx.get("transactionType") or tx.get("type") or "").upper()
        contract_address = _normalize_hex_match(tx.get("contractAddress"), 40)
        if tx_type in {"CREATE", "CREATE2"} or contract_address is not None:
            continue
        calls.append(
            {
                "index": idx,
                "tx_hash": _normalize_hex_match(tx.get("hash"), 64),
            }
        )
    return calls


def _select_broadcast_transaction(
    transactions: List[Dict[str, Any]],
    *,
    contract_name: str | None,
    returned_address: str | None,
    receipt_addresses: Dict[str, str],
) -> Optional[Dict[str, Optional[str]]]:
    expected_name = _normalize_contract_name(contract_name)
    candidates: list[tuple[int, int, Dict[str, Optional[str]]]] = []

    for idx, tx in enumerate(transactions):
        if not isinstance(tx, dict):
            continue

        tx_hash = _normalize_hex_match(tx.get("hash"), 64)
        receipt_address = receipt_addresses.get(tx_hash or "")
        contract_address = receipt_address or _normalize_hex_match(
            tx.get("contractAddress"), 40
        )
        tx_type = str(tx.get("transactionType") or tx.get("type") or "").upper()
        normalized_name = _normalize_contract_name(tx.get("contractName"))

        is_create = tx_type in {"CREATE", "CREATE2"} or contract_address is not None
        if not is_create:
            continue

        score = 0
        if expected_name and normalized_name == expected_name:
            score += 100
        if returned_address and contract_address == returned_address:
            score += 50
        if contract_address:
            score += 10
        if tx_hash:
            score += 1

        candidates.append(
            (
                score,
                idx,
                {
                    "tx_hash": tx_hash,
                    "deployed_address": contract_address,
                },
            )
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _parse_broadcast_deploy_output(
    broadcast_text: str,
    *,
    contract_name: str | None,
    deployment_manifest: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    try:
        payload = json.loads(broadcast_text)
    except json.JSONDecodeError:
        return {
            "tx_hash": None,
            "deployed_address": None,
            "deployed_contracts": [],
            "executed_calls": [],
        }

    transactions = payload.get("transactions")
    if not isinstance(transactions, list):
        return {
            "tx_hash": None,
            "deployed_address": None,
            "deployed_contracts": [],
            "executed_calls": [],
        }

    receipt_addresses = _receipt_address_map(payload)
    returned_by_name, _ = _extract_returned_contract_addresses(payload)
    create_transactions = _broadcast_create_transactions(
        transactions,
        receipt_addresses=receipt_addresses,
    )
    call_transactions = _broadcast_call_transactions(transactions)

    deployed_contracts: list[dict[str, Any]] = []
    executed_calls: list[dict[str, Any]] = []
    if deployment_manifest:
        try:
            manifest = load_deployment_manifest(deployment_manifest)
        except Exception:
            manifest = None
        if manifest is not None:
            used_indices: set[int] = set()
            for contract in sorted(
                manifest.contracts,
                key=lambda entry: (entry.deploy_order, entry.name),
            ):
                best_index: int | None = None
                best_score = -1
                for idx, tx in enumerate(create_transactions):
                    if idx in used_indices:
                        continue
                    score = 0
                    if tx.get("contract_name") == contract.name:
                        score += 100
                    if returned_by_name.get(contract.name) and tx.get("deployed_address") == returned_by_name.get(contract.name):
                        score += 50
                    if tx.get("deployed_address"):
                        score += 10
                    if tx.get("tx_hash"):
                        score += 1
                    if score > best_score:
                        best_score = score
                        best_index = idx
                if best_index is None:
                    continue
                used_indices.add(best_index)
                selected = create_transactions[best_index]
                deployed_contracts.append(
                    DeployedContractResult(
                        contract_name=contract.name,
                        plan_contract_id=contract.plan_contract_id,
                        deploy_order=contract.deploy_order,
                        tx_hash=selected.get("tx_hash"),
                        deployed_address=selected.get("deployed_address"),
                    ).model_dump()
                )

            for index, call in enumerate(
                sorted(
                    manifest.post_deploy_calls,
                    key=lambda entry: (entry.call_order, entry.target_contract_name, entry.function_name),
                )
            ):
                tx_hash = call_transactions[index].get("tx_hash") if index < len(call_transactions) else None
                executed_calls.append(
                    ExecutedCallResult(
                        target_contract_name=call.target_contract_name,
                        target_plan_contract_id=call.target_plan_contract_id,
                        function_name=call.function_name,
                        args=list(call.args),
                        call_order=call.call_order,
                        tx_hash=tx_hash,
                        status="success" if tx_hash else "missing",
                    ).model_dump()
                )

            primary = next(
                (contract for contract in manifest.contracts if contract.role == "primary_deployable"),
                manifest.contracts[0] if manifest.contracts else None,
            )
            if primary is not None:
                matched_primary = next(
                    (
                        entry
                        for entry in deployed_contracts
                        if entry.get("plan_contract_id") == primary.plan_contract_id
                    ),
                    None,
                )
                if matched_primary is not None:
                    return {
                        "tx_hash": matched_primary.get("tx_hash"),
                        "deployed_address": matched_primary.get("deployed_address"),
                        "deployed_contracts": deployed_contracts,
                        "executed_calls": executed_calls,
                    }
            if deployed_contracts:
                return {
                    "tx_hash": deployed_contracts[0].get("tx_hash"),
                    "deployed_address": deployed_contracts[0].get("deployed_address"),
                    "deployed_contracts": deployed_contracts,
                    "executed_calls": executed_calls,
                }

    returned_address = _extract_returned_contract_address(payload, contract_name)
    selected = _select_broadcast_transaction(
        transactions,
        contract_name=contract_name,
        returned_address=returned_address,
        receipt_addresses=receipt_addresses,
    )
    if selected:
        return {
            **selected,
            "deployed_contracts": deployed_contracts,
            "executed_calls": executed_calls,
        }

    return {
        "tx_hash": None,
        "deployed_address": returned_address,
        "deployed_contracts": deployed_contracts,
        "executed_calls": executed_calls,
    }


def _extract_deploy_metadata(
    *,
    root: str,
    request: FoundryDeployRequest,
    stdout: str,
    stderr: str,
    volume: Any | None = None,
) -> Dict[str, Any]:
    broadcast_path = _broadcast_run_artifact_path(request.script_path, request.chain_id)
    broadcast_text = _read_project_artifact(root, broadcast_path, volume=volume)
    if broadcast_text:
        parsed = _parse_broadcast_deploy_output(
            broadcast_text,
            contract_name=request.contract_name,
            deployment_manifest=request.deployment_manifest,
        )
        if (
            parsed.get("tx_hash")
            or parsed.get("deployed_address")
            or parsed.get("deployed_contracts")
        ):
            return parsed
    parsed = _parse_deploy_output(stdout, stderr)
    parsed["deployed_contracts"] = []
    parsed["executed_calls"] = []
    return parsed


def _normalize_private_key_hex(value: str) -> str:
    normalized = (value or "").strip()
    if normalized.startswith(("0x", "0X")):
        return f"0x{normalized[2:]}"
    if re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        return f"0x{normalized}"
    return normalized


def _record_deploy_result(
    *,
    project_id: str | None,
    pipeline_run_id: str | None,
    pipeline_task_id: str | None,
    root: str,
    sandbox_workdir: str,
    request: FoundryDeployRequest,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    modal_app: str,
    tx_hash: str | None = None,
    deployed_address: str | None = None,
    deployed_contracts: list[dict[str, Any]] | None = None,
    executed_calls: list[dict[str, Any]] | None = None,
    status: str | None = None,
) -> dict:
    stdout_path, stderr_path = save_execution_logs(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        pipeline_task_id=pipeline_task_id,
        stdout=stdout,
        stderr=stderr,
    )

    mm = _get_memory_manager()
    deployment_state = mm.get_agent_state("deployment")

    history: List[Dict[str, Any]] = deployment_state.get("last_deploy_results", [])
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pipeline_run_id": pipeline_run_id,
        "pipeline_task_id": pipeline_task_id,
        "project_root": root,
        "sandbox_workdir": sandbox_workdir,
        "network": request.network,
        "chain_id": request.chain_id,
        "script_path": request.script_path,
        "command": command,
        "exit_code": exit_code,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "summary": compact_execution_summary(exit_code, stdout, stderr),
        "modal_app": modal_app,
        "plan_contract_id": request.plan_contract_id,
        "tx_hash": tx_hash,
        "deployed_address": deployed_address,
        "deployed_contracts": list(deployed_contracts or []),
        "executed_calls": list(executed_calls or []),
    }
    history.append(entry)
    deployment_state["last_deploy_results"] = history
    deployment_state["last_deploy_status"] = status or (
        "success" if exit_code == 0 else "failed"
    )
    mm.set_agent_state("deployment", deployment_state)
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
    tx_hash = _extract_first(
        r"(?:tx hash|transaction hash|hash)\s*[:=]\s*(0x[a-fA-F0-9]{64})",
        combined,
    )

    deployed_address = _extract_first(
        r"(?:deployed to|deployed at|contract address)\s*[:=]\s*(0x[a-fA-F0-9]{40})",
        combined,
    )

    return {"tx_hash": tx_hash, "deployed_address": deployed_address}


def generate_foundry_deploy_script_direct(
    request: FoundryDeployScriptGenerationRequest,
) -> Dict[str, Any]:
    manifest_script = _build_manifest_deploy_script(request)
    if manifest_script is not None and not manifest_script.get("error"):
        return manifest_script
    if manifest_script is not None and manifest_script.get("error"):
        return manifest_script

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

    manifest_section = ""
    if request.deployment_manifest:
        manifest_section = (
            "\n\nAuthoritative deployment manifest:\n"
            f"{json.dumps(request.deployment_manifest, indent=2, sort_keys=True)}"
        )

    args_comment = (
        ", ".join(request.constructor_args) if request.constructor_args else "none"
    )
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
        "- Derive `address deployer = vm.addr(privateKey);` (or an equivalent broadcaster address) before deployment.\n"
        "- If a deployment manifest is provided, deploy every manifest contract in deploy_order using one script.\n"
        "- Resolve constructor and post-deploy placeholders of the form <deployed:ContractName.address> to address(<local deployed instance variable>).\n"
        "- Execute manifest post_deploy_calls only after all deployments complete.\n"
        "- Emit brief marker comments in the form // deploy-order:<n> <ContractName> and // post-deploy:<n> <ContractName>.<function>.\n"
        "- If no manifest is provided, deploy exactly one requested contract instance.\n"
        "- Treat the provided constructor arguments as authoritative Solidity expressions; preserve identifiers like `deployer` as script-local variables instead of converting them to literals.\n"
        "- Never infer `address(0)` for an address input. If no explicit non-zero wallet is provided, use `deployer` as the fallback.\n"
        "- For optional address env vars such as treasury/admin/owner recipients, use `deployer` as the non-zero fallback unless the plan explicitly requires another address.\n"
        "- Add brief inline comments where non-obvious.\n\n"
        f"Deployment goal:\n{request.goal.strip()}\n\n"
        f"Target contract name: {request.contract_name}\n"
        f"Script contract name: {request.script_name}\n"
        f"Constructor arguments (Solidity expressions): {args_comment}"
        f"{constraints_section}"
        f"{plan_section}"
        f"{manifest_section}"
        f"{source_section}"
    )

    model_name = os.getenv("FOUNDRY_DEPLOY_SCRIPT_MODEL", "gpt-5.2-2025-12-11")
    llm = ChatOpenAI(model=model_name, temperature=0.1)

    try:
        with start_span(
            "model.call",
            {
                "task_type": "deployment.prepare_script",
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
        deployment_state = mm.get_agent_state("deployment")
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

        artifacts: List[Dict[str, Any]] = deployment_state.get("artifacts", [])
        artifacts.append(raw)
        deployment_state["artifacts"] = artifacts
        mm.set_agent_state("deployment", deployment_state)

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
        deployment_state = mm.get_agent_state("deployment")

        targets: List[dict] = deployment_state.get("targets", [])
        targets.append(target.model_dump())
        deployment_state["targets"] = targets
        mm.set_agent_state("deployment", deployment_state)

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
    response length errors). A deployment is successful only when forge exits cleanly and
    the run yields either a deployment tx hash or a contract address with confirmed bytecode.
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
        mm = _get_memory_manager()
        app_name = os.getenv("MODAL_APP_NAME", "partyhat-foundry-tests")
        sandbox_workdir = "/workspace/project"
        if request.network != "avalanche_fuji":
            entry = _record_deploy_result(
                project_id=project_id_ctx,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                root=root,
                sandbox_workdir=sandbox_workdir,
                request=request,
                command=f"forge script {request.script_path}",
                exit_code=1,
                stdout="",
                stderr=(
                    "Unsupported network. This deployment tool currently supports "
                    "only avalanche_fuji."
                ),
                modal_app=app_name,
            )
            try:
                mm.save_deployment(
                    status="failed",
                    contract_name=request.contract_name,
                    plan_contract_id=request.plan_contract_id,
                    network=request.network,
                    pipeline_run_id=pipeline_run_id,
                    pipeline_task_id=pipeline_task_id,
                    stdout_path=entry["stdout_path"],
                    stderr_path=entry["stderr_path"],
                    exit_code=1,
                    trace_id=trace_id,
                )
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": 1,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "stdout": "",
                "stderr": (
                    "Unsupported network. This deployment tool currently supports "
                    "only avalanche_fuji."
                ),
                "stdout_path": entry["stdout_path"],
                "stderr_path": entry["stderr_path"],
                "project_root": root,
                "sandbox_workdir": sandbox_workdir,
                "modal_app": app_name,
                "network": request.network,
                "chain_id": request.chain_id,
                "script_path": request.script_path,
                "command": f"forge script {request.script_path}",
                "error": (
                    "Unsupported network. This deployment tool currently supports "
                    "only avalanche_fuji."
                ),
            }

        rpc_url = os.getenv(request.rpc_url_env_var)
        private_key = os.getenv(request.private_key_env_var)
        if not rpc_url:
            entry = _record_deploy_result(
                project_id=project_id_ctx,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                root=root,
                sandbox_workdir=sandbox_workdir,
                request=request,
                command=f"forge script {request.script_path}",
                exit_code=1,
                stdout="",
                stderr=f"Missing required env var: {request.rpc_url_env_var}",
                modal_app=app_name,
            )
            try:
                mm.save_deployment(
                    status="failed",
                    contract_name=request.contract_name,
                    plan_contract_id=request.plan_contract_id,
                    network=request.network,
                    pipeline_run_id=pipeline_run_id,
                    pipeline_task_id=pipeline_task_id,
                    stdout_path=entry["stdout_path"],
                    stderr_path=entry["stderr_path"],
                    exit_code=1,
                    trace_id=trace_id,
                )
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": 1,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "stdout": "",
                "stderr": f"Missing required env var: {request.rpc_url_env_var}",
                "stdout_path": entry["stdout_path"],
                "stderr_path": entry["stderr_path"],
                "project_root": root,
                "sandbox_workdir": sandbox_workdir,
                "modal_app": app_name,
                "network": request.network,
                "chain_id": request.chain_id,
                "script_path": request.script_path,
                "command": f"forge script {request.script_path}",
                "error": f"Missing required env var: {request.rpc_url_env_var}",
            }
        if not private_key:
            entry = _record_deploy_result(
                project_id=project_id_ctx,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                root=root,
                sandbox_workdir=sandbox_workdir,
                request=request,
                command=f"forge script {request.script_path}",
                exit_code=1,
                stdout="",
                stderr=f"Missing required env var: {request.private_key_env_var}",
                modal_app=app_name,
            )
            try:
                mm.save_deployment(
                    status="failed",
                    contract_name=request.contract_name,
                    plan_contract_id=request.plan_contract_id,
                    network=request.network,
                    pipeline_run_id=pipeline_run_id,
                    pipeline_task_id=pipeline_task_id,
                    stdout_path=entry["stdout_path"],
                    stderr_path=entry["stderr_path"],
                    exit_code=1,
                    trace_id=trace_id,
                )
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": 1,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "stdout": "",
                "stderr": f"Missing required env var: {request.private_key_env_var}",
                "stdout_path": entry["stdout_path"],
                "stderr_path": entry["stderr_path"],
                "project_root": root,
                "sandbox_workdir": sandbox_workdir,
                "modal_app": app_name,
                "network": request.network,
                "chain_id": request.chain_id,
                "script_path": request.script_path,
                "command": f"forge script {request.script_path}",
                "error": f"Missing required env var: {request.private_key_env_var}",
            }
        private_key = _normalize_private_key_hex(private_key)

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
                a for a in user_args if a not in ("-v", "-vv", "-vvv", "-vvvv")
            ]
        forge_cmd.extend(user_args)

        has_remappings = any(
            a == "--remappings" or a.startswith("--remappings=") for a in user_args
        )
        if not has_remappings:
            forge_cmd.extend(default_foundry_remappings())

        timeout = int(os.getenv("FOUNDRY_SANDBOX_TIMEOUT", "900"))
        app = get_modal_app(app_name)
        base_volume_name = os.getenv(
            "FOUNDRY_ARTIFACT_VOLUME_NAME", "partyhat-foundry-artifacts"
        )
        volume_name = build_project_volume_name(base_volume_name, project_id_ctx)
        vol = get_modal_volume(volume_name)

        sandbox_image = foundry_image

        forge_cmd_str = " ".join(
            (
                part
                if isinstance(part, str) and part.startswith("$")
                else shlex.quote(str(part))
            )
            for part in forge_cmd
        )
        bootstrap_cmd = build_foundry_bootstrap_cmd(root, forge_cmd_str)

        with start_span(
            "deploy.execute",
            {
                "project_id": project_id_ctx,
                "pipeline_run_id": pipeline_run_id,
                "pipeline_task_id": pipeline_task_id,
                "task_type": "deployment.execute_deploy",
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
                env={
                    request.rpc_url_env_var: rpc_url,
                    request.private_key_env_var: private_key,
                },
            )

            cancelled = _wait_for_sandbox_completion(sandbox, pipeline_run_id)
            stdout_raw = _safe_stream_read(getattr(sandbox, "stdout", None))
            stderr_raw = _safe_stream_read(getattr(sandbox, "stderr", None))
            exit_code = getattr(sandbox, "returncode", None)
            if cancelled:
                exit_code = 130 if exit_code is None else exit_code
                span.set_attribute("failure_class", "cancelled")
            elif exit_code is not None:
                span.set_attribute("exit_code", int(exit_code))

        secrets = [rpc_url, private_key]
        stdout = _redact_text(stdout_raw, secrets)
        stderr = _redact_text(stderr_raw, secrets)
        parsed = _extract_deploy_metadata(
            root=root,
            request=request,
            stdout=stdout,
            stderr=stderr,
            volume=vol,
        )
        success, deploy_error = _evaluate_deploy_success(
            exit_code=exit_code,
            tx_hash=parsed.get("tx_hash"),
            deployed_address=parsed.get("deployed_address"),
            rpc_url=rpc_url,
        )
        if cancelled:
            success = False
            deploy_error = "Deployment cancelled."
        command_display = " ".join(forge_cmd)
        entry = _record_deploy_result(
            project_id=project_id_ctx,
            pipeline_run_id=pipeline_run_id,
            pipeline_task_id=pipeline_task_id,
            root=root,
            sandbox_workdir=sandbox_workdir,
            request=request,
            command=command_display,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            modal_app=app_name,
            tx_hash=parsed.get("tx_hash"),
            deployed_address=parsed.get("deployed_address"),
            deployed_contracts=parsed.get("deployed_contracts"),
            executed_calls=parsed.get("executed_calls"),
            status="cancelled" if cancelled else ("success" if success else "failed"),
        )
        stdout_path = entry["stdout_path"]
        stderr_path = entry["stderr_path"]
        try:
            mm.save_deployment(
                status="cancelled" if cancelled else ("success" if success else "failed"),
                contract_name=request.contract_name,
                plan_contract_id=request.plan_contract_id,
                deployed_address=parsed.get("deployed_address"),
                tx_hash=parsed.get("tx_hash"),
                snowtrace_url=None,
                network=request.network,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                exit_code=exit_code,
                trace_id=trace_id,
                deployed_contracts=parsed.get("deployed_contracts") or [],
                executed_calls=parsed.get("executed_calls") or [],
            )
        except Exception:
            pass

        mm.log_agent_action(
            agent_name="deployment",
            action="foundry_deploy_run",
            output_produced=entry,
            why="Deployment agent executed forge script broadcast in Modal Sandbox",
            how="run_foundry_deploy tool (Modal Sandbox)",
            error=deploy_error,
        )

        response = {
            "success": success and not cancelled,
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
            "network": request.network,
            "chain_id": request.chain_id,
            "script_path": request.script_path,
            "command": command_display,
            "tx_hash": parsed.get("tx_hash"),
            "deployed_address": parsed.get("deployed_address"),
            "deployed_contracts": parsed.get("deployed_contracts") or [],
            "executed_calls": parsed.get("executed_calls") or [],
        }
        if deploy_error:
            response["error"] = deploy_error
        return _cap_deploy_response(response)
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
            entry = _record_deploy_result(
                project_id=project_id_ctx,
                pipeline_run_id=pipeline_run_id,
                pipeline_task_id=pipeline_task_id,
                root=root,
                sandbox_workdir=sandbox_workdir,
                request=request,
                command=f"forge script {request.script_path}",
                exit_code=1,
                stdout="",
                stderr=str(e),
                modal_app=app_name,
            )
            try:
                _get_memory_manager().save_deployment(
                    status="failed",
                    contract_name=request.contract_name,
                    plan_contract_id=request.plan_contract_id,
                    network=request.network,
                    pipeline_run_id=pipeline_run_id,
                    pipeline_task_id=pipeline_task_id,
                    stdout_path=entry["stdout_path"],
                    stderr_path=entry["stderr_path"],
                    exit_code=1,
                    trace_id=current_trace_id(),
                )
            except Exception:
                pass
            return _cap_deploy_response(
                {
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
                    "network": request.network,
                    "chain_id": request.chain_id,
                    "script_path": request.script_path,
                    "command": f"forge script {request.script_path}",
                    "tx_hash": None,
                    "deployed_address": None,
                    "error": f"Could not run forge deployment in Modal Sandbox: {str(e)}",
                }
            )
        except Exception:
            return {"error": f"Could not run forge deployment in Modal Sandbox: {str(e)}"}


@tool
def record_deployment(record: DeploymentRecord) -> dict:
    """
    Record a deployment attempt and its outcome.
    """
    try:
        mm = _get_memory_manager()
        deployment_state = mm.get_agent_state("deployment")

        deployments: List[dict] = deployment_state.get("deployments", [])
        payload = record.model_dump()
        payload.pop("stdout", None)
        payload.pop("stderr", None)
        deployments.append(payload)
        deployment_state["deployments"] = deployments
        mm.set_agent_state("deployment", deployment_state)

        mm.log_agent_action(
            agent_name="deployment",
            action="deployment_recorded",
            output_produced=payload,
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
        plan = mm.get_plan()
        artifacts = [
            enrich_artifact_with_plan_contract_ids(
                plan,
                artifact,
                allow_name_fallback=True,
            )[0]
            for artifact in state.get("artifacts", [])
            if isinstance(artifact, dict)
        ]
        return {
            "targets": state.get("targets", []),
            "artifacts": artifacts,
            "last_deploy_results": state.get("last_deploy_results", []),
            "deployments": state.get("deployments", []),
        }
    except Exception as e:
        return {"error": f"Could not retrieve deployment history: {str(e)}"}


# Snowtrace (Etherscan-compatible) API base URLs per chain
SNOWTRACE_VERIFIER_URLS = {
    43113: "https://api-testnet.snowtrace.io/api",  # Fuji testnet
    43114: "https://api.snowtrace.io/api",  # Avalanche C-Chain mainnet
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
        root = (
            project_root
            or request.project_root
            or os.getenv("FOUNDRY_PROJECT_ROOT")
            or default_root
        )

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

        forge_cmd_str = " ".join(shlex.quote(str(part)) for part in forge_cmd)
        bootstrap_cmd = build_foundry_bootstrap_cmd(root, forge_cmd_str)

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
            error=(
                None
                if exit_code == 0
                else "forge verify-contract returned non-zero exit code"
            ),
        )

        explorer_base = (
            "https://testnet.snowtrace.io"
            if request.chain_id == 43113
            else "https://snowtrace.io"
        )
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
] + TASK_TOOLS
