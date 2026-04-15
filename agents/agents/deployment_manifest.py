from __future__ import annotations

import json
import re
from typing import Any

from agents.contract_identity import resolve_plan_contract_ids
from agents.pipeline_specs import default_deployment_target_payload
from schemas.deployment_schema import (
    ConstructorArgSchema,
    DeploymentManifest,
    DeploymentManifestContract,
    DeploymentManifestPostDeployCall,
)


MANIFEST_PATH = "manifests/deployment.json"
_DEPLOYED_TOKEN_PATTERN = re.compile(r"<deployed:(?P<name>[A-Za-z_][A-Za-z0-9_]*)\.address>")
_ANY_DEPLOYED_TOKEN_PATTERN = re.compile(r"<deployed:[^>]+>")
_HEX_ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
_QUOTED_STRING_PATTERN = re.compile(r"""^(['"]).*\1$""", re.DOTALL)
_UNSIGNED_NUMERIC_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9_]*)(?:\s+(?:wei|gwei|szabo|finney|ether|seconds|minutes|hours|days|weeks))?$"
)
_SIGNED_NUMERIC_PATTERN = re.compile(
    r"^-?(?:0|[1-9][0-9_]*)(?:\s+(?:wei|gwei|szabo|finney|ether|seconds|minutes|hours|days|weeks))?$"
)
_UNRESOLVED_DEPLOY_ARG_TOKENS = {
    "tbd",
    "todo",
    "replace_me",
    "replace-me",
    "changeme",
    "change_me",
    "unknown",
}


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _string_field(value: Any, key: str) -> str:
    return str(_field(value, key, "") or "").strip()


def _plan_contracts(plan: Any) -> list[Any]:
    contracts = _field(plan, "contracts", []) or []
    return list(contracts)


def _plan_post_deploy_calls(plan: Any) -> list[Any]:
    calls = _field(plan, "post_deploy_calls", []) or []
    return list(calls)


def _call_arg_strings(call: Any) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    args: list[str] = []
    for index, arg in enumerate(_field(call, "args", []) or [], start=1):
        if isinstance(arg, bool):
            args.append("true" if arg else "false")
        elif isinstance(arg, (str, int, float)):
            args.append(str(arg))
        else:
            issues.append(
                f"post_deploy_calls arg {index} must be a scalar string/number/bool literal."
            )
    return args, issues


def _is_quoted_string_literal(value: str) -> bool:
    return _QUOTED_STRING_PATTERN.fullmatch(value.strip()) is not None


def _is_unresolved_deploy_arg(value: str) -> bool:
    normalized = value.strip().lower()
    return normalized in _UNRESOLVED_DEPLOY_ARG_TOKENS


def _is_bool_literal(value: str) -> bool:
    return value.strip().lower() in {"true", "false"}


def _is_numeric_literal(value: str, *, signed: bool) -> bool:
    pattern = _SIGNED_NUMERIC_PATTERN if signed else _UNSIGNED_NUMERIC_PATTERN
    return pattern.fullmatch(value.strip()) is not None


def _function_inputs_for_post_deploy_call(
    plan: Any,
    *,
    target_contract_name: str,
    function_name: str,
    arg_count: int,
    context: str,
    strict: bool = True,
) -> tuple[list[str] | None, list[str]]:
    issues: list[str] = []
    target_contract = next(
        (
            contract
            for contract in _plan_contracts(plan)
            if _string_field(contract, "name") == target_contract_name
        ),
        None,
    )
    if target_contract is None:
        return None, issues

    matching_functions = [
        function
        for function in (_field(target_contract, "functions", []) or [])
        if _string_field(function, "name") == function_name
    ]
    if not matching_functions:
        if not strict:
            return None, issues
        issues.append(
            f"{context} references unknown function '{target_contract_name}.{function_name}'."
        )
        return None, issues

    exact_match = next(
        (
            function
            for function in matching_functions
            if len(_field(function, "inputs", []) or []) == arg_count
        ),
        None,
    )
    selected = exact_match or matching_functions[0]
    input_types = [
        _string_field(item, "type")
        for item in (_field(selected, "inputs", []) or [])
    ]
    if len(input_types) != arg_count:
        if len(matching_functions) == 1:
            issues.append(
                f"{context} expects {len(input_types)} arg(s) for "
                f"'{target_contract_name}.{function_name}' but got {arg_count}."
            )
        else:
            counts = ", ".join(
                str(len(_field(function, "inputs", []) or []))
                for function in matching_functions
            )
            issues.append(
                f"{context} has no overload for '{target_contract_name}.{function_name}' "
                f"accepting {arg_count} arg(s). Available counts: {counts}."
            )
    return input_types, issues


def _typed_post_deploy_arg_issues(
    value: str,
    *,
    expected_type: str,
    context: str,
    known_contract_names: set[str],
) -> list[str]:
    issues = validate_deployed_placeholders(
        value,
        context=context,
        known_contract_names=known_contract_names,
    )
    if issues:
        return issues

    literal = value.strip()
    if not literal:
        return [f"{context} must not be empty."]
    if _is_unresolved_deploy_arg(literal):
        return [f"{context} contains unresolved value '{literal}'."]

    lowered_type = expected_type.strip().lower()
    if lowered_type == "string":
        if _is_quoted_string_literal(literal):
            return []
        return [f"{context} must be a quoted string literal for Solidity type 'string'."]

    if lowered_type == "bool":
        if _is_bool_literal(literal):
            return []
        return [f"{context} must be 'true' or 'false' for Solidity type 'bool'."]

    if lowered_type == "address":
        if literal == "deployer":
            return []
        if _HEX_ADDRESS_PATTERN.fullmatch(literal):
            return []
        if _DEPLOYED_TOKEN_PATTERN.fullmatch(literal):
            return []
        return [
            f"{context} must be 'deployer', a 0x-prefixed address, or "
            "<deployed:Contract.address> for Solidity type 'address'."
        ]

    if lowered_type.startswith("uint"):
        if _is_numeric_literal(literal, signed=False):
            return []
        return [f"{context} must be a numeric literal for Solidity type '{expected_type}'."]

    if lowered_type.startswith("int"):
        if _is_numeric_literal(literal, signed=True):
            return []
        return [f"{context} must be a numeric literal for Solidity type '{expected_type}'."]

    return [
        f"{context} uses unsupported Solidity type '{expected_type}' for post-deploy arg validation."
    ]


def validate_post_deploy_calls(plan: Any) -> list[str]:
    issues: list[str] = []
    known_contract_names = {
        _string_field(contract, "name")
        for contract in _plan_contracts(plan)
        if _string_field(contract, "name")
    }
    seen_call_orders: set[int] = set()

    for index, call in enumerate(_plan_post_deploy_calls(plan), start=1):
        target_contract_name = _string_field(call, "target_contract_name")
        function_name = _string_field(call, "function_name")
        context = f"post_deploy_calls[{index}]"
        args, arg_issues = _call_arg_strings(call)
        issues.extend(f"{context} {issue}" for issue in arg_issues)

        if not target_contract_name:
            issues.append(f"{context} is missing target_contract_name.")
        elif target_contract_name not in known_contract_names:
            issues.append(
                f"{context} references unknown target_contract_name '{target_contract_name}'."
            )

        if not function_name:
            issues.append(f"{context} is missing function_name.")

        call_order = _field(call, "call_order")
        if call_order is None:
            issues.append(f"{context} is missing call_order.")
        else:
            try:
                normalized_order = int(call_order)
            except (TypeError, ValueError):
                issues.append(f"{context} has non-integer call_order '{call_order}'.")
            else:
                if normalized_order in seen_call_orders:
                    issues.append(
                        f"Duplicate post_deploy_calls call_order {normalized_order}."
                    )
                seen_call_orders.add(normalized_order)

        if not target_contract_name or not function_name:
            continue

        input_types, lookup_issues = _function_inputs_for_post_deploy_call(
            plan,
            target_contract_name=target_contract_name,
            function_name=function_name,
            arg_count=len(args),
            context=context,
        )
        issues.extend(lookup_issues)
        if not input_types:
            continue

        for arg_index, arg in enumerate(args, start=1):
            expected_type = (
                input_types[arg_index - 1]
                if arg_index - 1 < len(input_types)
                else ""
            )
            if not expected_type:
                continue
            issues.extend(
                _typed_post_deploy_arg_issues(
                    arg,
                    expected_type=expected_type,
                    context=(
                        f"{context} arg {arg_index} for "
                        f"{target_contract_name}.{function_name}"
                    ),
                    known_contract_names=known_contract_names,
                )
            )
    return issues


def remediate_manifest_post_deploy_calls(
    plan: Any,
    manifest: DeploymentManifest,
) -> tuple[DeploymentManifest, list[str], list[str], bool]:
    remediated = manifest.model_copy(deep=True)
    known_contract_names = {contract.name for contract in remediated.contracts}
    notes: list[str] = []
    issues: list[str] = []
    changed = False

    for index, call in enumerate(remediated.post_deploy_calls, start=1):
        context = f"post_deploy_calls[{index}]"
        input_types, lookup_issues = _function_inputs_for_post_deploy_call(
            plan,
            target_contract_name=call.target_contract_name,
            function_name=call.function_name,
            arg_count=len(call.args),
            context=context,
            strict=False,
        )
        issues.extend(lookup_issues)
        if not input_types:
            continue

        normalized_args: list[str] = []
        for arg_index, arg in enumerate(call.args, start=1):
            expected_type = (
                input_types[arg_index - 1]
                if arg_index - 1 < len(input_types)
                else ""
            )
            literal = str(arg or "").strip()
            arg_context = (
                f"{context} arg {arg_index} for "
                f"{call.target_contract_name}.{call.function_name}"
            )
            updated_literal = literal

            if expected_type.strip().lower() == "string":
                placeholder_issues = validate_deployed_placeholders(
                    literal,
                    context=arg_context,
                    known_contract_names=known_contract_names,
                )
                if (
                    literal
                    and not placeholder_issues
                    and not _is_unresolved_deploy_arg(literal)
                    and not _is_quoted_string_literal(literal)
                ):
                    updated_literal = json.dumps(literal)
                    changed = True
                    notes.append(f"{arg_context} was normalized to {updated_literal}.")

            normalized_args.append(updated_literal)
            issues.extend(
                _typed_post_deploy_arg_issues(
                    updated_literal,
                    expected_type=expected_type,
                    context=arg_context,
                    known_contract_names=known_contract_names,
                )
            )

        call.args = normalized_args

    return remediated, notes, issues, changed


def _contract_artifact_lookup(
    plan: dict | None,
    coding_artifacts: list[dict],
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    by_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    issues: list[str] = []
    for artifact in coding_artifacts:
        path = str(artifact.get("path") or "")
        if not path.startswith("contracts/"):
            continue
        resolved_ids, artifact_issues = resolve_plan_contract_ids(
            plan,
            artifact,
            allow_name_fallback=True,
        )
        issues.extend(artifact_issues)
        for plan_contract_id in resolved_ids:
            if plan_contract_id and plan_contract_id not in by_id:
                by_id[plan_contract_id] = path
        for name in artifact.get("contract_names") or []:
            if name and name not in by_name:
                by_name[str(name)] = path
    return by_id, by_name, issues


def _constructor_arg_schema(contract_plan: dict) -> list[ConstructorArgSchema]:
    constructor = contract_plan.get("constructor") or {}
    inputs = constructor.get("inputs") or []
    schema: list[ConstructorArgSchema] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        default_value = item.get("default_value")
        source = "runtime_required"
        if isinstance(default_value, str) and default_value.strip():
            source = "deployer" if default_value.strip().lower() == "deployer" else "plan_default"
        schema.append(
            ConstructorArgSchema(
                name=str(item.get("name") or ""),
                type=str(item.get("type") or ""),
                source=source,
                default_value=default_value,
            )
        )
    return schema


def extract_deployed_contract_references(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [match.group("name") for match in _DEPLOYED_TOKEN_PATTERN.finditer(value)]


def validate_deployed_placeholders(
    value: Any,
    *,
    context: str,
    known_contract_names: set[str] | None = None,
) -> list[str]:
    if not isinstance(value, str) or "<deployed:" not in value:
        return []

    issues: list[str] = []
    matched_tokens = list(_ANY_DEPLOYED_TOKEN_PATTERN.finditer(value))
    if value.count("<deployed:") != len(matched_tokens):
        issues.append(
            f"{context} contains a malformed deployment placeholder. Use <deployed:ContractName.address>."
        )

    for match in matched_tokens:
        token = match.group(0)
        normalized = _DEPLOYED_TOKEN_PATTERN.fullmatch(token)
        if normalized is None:
            issues.append(
                f"{context} contains unsupported placeholder '{token}'. Use <deployed:ContractName.address>."
            )
            continue
        contract_name = normalized.group("name")
        if known_contract_names is not None and contract_name not in known_contract_names:
            issues.append(
                f"{context} references unknown deployed contract '{contract_name}'."
            )
    return issues


def _validate_constructor_placeholders(
    manifest_contracts: list[DeploymentManifestContract],
) -> list[str]:
    issues: list[str] = []
    contracts_by_name = {contract.name: contract for contract in manifest_contracts}
    known_names = set(contracts_by_name)

    for contract in manifest_contracts:
        for arg in contract.constructor_args_schema:
            if not isinstance(arg.default_value, str) or not arg.default_value.strip():
                continue
            context = (
                f"Contract '{contract.name}' constructor arg '{arg.name}'"
            )
            issues.extend(
                validate_deployed_placeholders(
                    arg.default_value,
                    context=context,
                    known_contract_names=known_names,
                )
            )
            for ref_name in extract_deployed_contract_references(arg.default_value):
                referenced = contracts_by_name.get(ref_name)
                if referenced is None:
                    continue
                if referenced.deploy_order >= contract.deploy_order:
                    issues.append(
                        f"{context} references '{ref_name}' but deploy_order {referenced.deploy_order} "
                        f"is not earlier than '{contract.name}' deploy_order {contract.deploy_order}."
                    )
    return issues


def _build_post_deploy_calls(
    plan: dict[str, Any],
    manifest_contracts: list[DeploymentManifestContract],
) -> tuple[list[DeploymentManifestPostDeployCall], list[str]]:
    issues: list[str] = list(validate_post_deploy_calls(plan))
    calls: list[DeploymentManifestPostDeployCall] = []
    contracts_by_name = {contract.name: contract for contract in manifest_contracts}

    for index, entry in enumerate(plan.get("post_deploy_calls") or [], start=1):
        if not isinstance(entry, dict):
            continue

        target_contract_name = str(entry.get("target_contract_name") or "").strip()
        function_name = str(entry.get("function_name") or "").strip()
        description = str(entry.get("description") or "").strip()
        call_order = entry.get("call_order")
        args, _ = _call_arg_strings(entry)
        try:
            call_order = int(call_order)
        except (TypeError, ValueError):
            call_order = None

        target_contract = contracts_by_name.get(target_contract_name)
        if (
            target_contract is not None
            and target_contract_name
            and function_name
            and call_order is not None
        ):
            calls.append(
                DeploymentManifestPostDeployCall(
                    target_contract_name=target_contract_name,
                    target_plan_contract_id=target_contract.plan_contract_id,
                    function_name=function_name,
                    args=args,
                    call_order=call_order,
                    description=description,
                )
            )

    calls.sort(key=lambda entry: (entry.call_order, entry.target_contract_name, entry.function_name))
    return calls, issues


def build_deployment_manifest(
    plan: dict | None,
    coding_artifacts: list[dict],
) -> tuple[DeploymentManifest | None, list[str]]:
    if not isinstance(plan, dict):
        return None, ["Missing validated plan."]

    contracts = plan.get("contracts") or []
    if not contracts:
        return None, ["Plan has no contracts."]

    lookup_by_id, lookup_by_name, lookup_issues = _contract_artifact_lookup(
        plan,
        coding_artifacts,
    )
    manifest_contracts: list[DeploymentManifestContract] = []
    issues: list[str] = list(lookup_issues)
    multiple_contracts = len(contracts) > 1

    for index, contract in enumerate(contracts, start=1):
        if not isinstance(contract, dict):
            continue
        name = str(contract.get("name") or "")
        if not name:
            issues.append("Encountered a contract without a name in the plan.")
            continue
        plan_contract_id = str(contract.get("plan_contract_id") or "")
        if not plan_contract_id:
            issues.append(f"Contract '{name}' is missing plan_contract_id.")
            continue
        source_path = lookup_by_id.get(plan_contract_id) or lookup_by_name.get(name)
        if not source_path:
            issues.append(f"Missing generated contract artifact for '{name}'.")
            continue

        role = contract.get("deployment_role")
        deploy_order = contract.get("deploy_order")
        if multiple_contracts:
            if role and deploy_order is None:
                issues.append(
                    f"Contract '{name}' defines deployment_role='{role}' but has no deploy_order."
                )
            if not role and any(
                isinstance(other, dict) and other.get("deployment_role")
                for other in contracts
            ):
                role = "supporting"
        else:
            role = role or "primary_deployable"
            deploy_order = deploy_order or 1

        if role:
            manifest_contracts.append(
                DeploymentManifestContract(
                    plan_contract_id=plan_contract_id,
                    name=name,
                    role=str(role),
                    deploy_order=int(deploy_order or 1),
                    source_path=source_path,
                    constructor_args_schema=_constructor_arg_schema(contract),
                )
            )

    manifest_contracts.sort(key=lambda contract: (contract.deploy_order, contract.name))

    if multiple_contracts:
        primary_count = sum(
            1 for contract in manifest_contracts if contract.role == "primary_deployable"
        )
        if primary_count != 1:
            issues.append(
                "Multi-contract plans must mark exactly one contract as deployment_role='primary_deployable'."
            )
    elif manifest_contracts:
        manifest_contracts[0].role = "primary_deployable"
        manifest_contracts[0].deploy_order = manifest_contracts[0].deploy_order or 1

    if not manifest_contracts:
        issues.append("No deployable contracts were produced for the deployment manifest.")

    issues.extend(_validate_constructor_placeholders(manifest_contracts))
    post_deploy_calls, post_deploy_issues = _build_post_deploy_calls(plan, manifest_contracts)
    issues.extend(post_deploy_issues)

    target = plan.get("deployment_target") or default_deployment_target_payload()
    manifest = (
        DeploymentManifest(
            deployment_target=target,
            contracts=manifest_contracts,
            post_deploy_calls=post_deploy_calls,
        )
        if not issues
        else None
    )
    return manifest, issues


def dump_deployment_manifest(manifest: DeploymentManifest) -> str:
    return json.dumps(manifest.model_dump(), indent=2, sort_keys=True)


def load_deployment_manifest(raw: str | dict[str, Any]) -> DeploymentManifest:
    if isinstance(raw, str):
        payload = json.loads(raw)
    else:
        payload = raw
    return DeploymentManifest.model_validate(payload)


def validate_deploy_script_against_manifest(
    manifest: DeploymentManifest,
    script_code: str,
) -> list[str]:
    issues: list[str] = []
    primary = next(
        (contract for contract in manifest.contracts if contract.role == "primary_deployable"),
        None,
    )
    if primary is None:
        return ["Deployment manifest is missing a primary_deployable contract."]

    if "<deployed:" in script_code:
        issues.append("Deployment script contains unresolved <deployed:...> placeholders.")

    deployment_positions: list[int] = []
    for contract in sorted(manifest.contracts, key=lambda entry: (entry.deploy_order, entry.name)):
        import_path = f"../{contract.source_path}"
        import_position = script_code.find(import_path)
        if import_position == -1:
            issues.append(
                f"Deployment script does not import manifest contract '{contract.name}' from '{import_path}'."
            )
        deploy_position = script_code.find(f"new {contract.name}(")
        if deploy_position == -1:
            issues.append(
                f"Deployment script does not deploy manifest contract '{contract.name}'."
            )
            continue
        deployment_positions.append(deploy_position)

    if deployment_positions != sorted(deployment_positions):
        issues.append("Deployment script does not deploy contracts in manifest deploy_order.")

    last_deploy_position = max(deployment_positions) if deployment_positions else -1
    call_positions: list[int] = []
    for call in sorted(
        manifest.post_deploy_calls,
        key=lambda entry: (entry.call_order, entry.target_contract_name, entry.function_name),
    ):
        call_marker = f"post-deploy:{call.call_order} {call.target_contract_name}.{call.function_name}"
        call_position = script_code.find(call_marker)
        if call_position == -1:
            call_position = script_code.find(f".{call.function_name}(")
        if call_position == -1:
            issues.append(
                f"Deployment script does not execute post-deploy call "
                f"'{call.target_contract_name}.{call.function_name}'."
            )
            continue
        if call_position <= last_deploy_position:
            issues.append(
                f"Deployment script executes post-deploy call "
                f"'{call.target_contract_name}.{call.function_name}' before all deployments."
            )
        call_positions.append(call_position)

    if call_positions != sorted(call_positions):
        issues.append("Deployment script does not execute post-deploy calls in manifest call_order.")
    return issues
