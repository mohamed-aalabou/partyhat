from __future__ import annotations

import json
from typing import Any

from agents.pipeline_specs import default_deployment_target_payload
from schemas.deployment_schema import (
    ConstructorArgSchema,
    DeploymentManifest,
    DeploymentManifestContract,
)


MANIFEST_PATH = "manifests/deployment.json"


def _contract_artifact_lookup(coding_artifacts: list[dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for artifact in coding_artifacts:
        path = str(artifact.get("path") or "")
        if not path.startswith("contracts/"):
            continue
        for name in artifact.get("contract_names") or []:
            if name and name not in lookup:
                lookup[str(name)] = path
    return lookup


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


def build_deployment_manifest(
    plan: dict | None,
    coding_artifacts: list[dict],
) -> tuple[DeploymentManifest | None, list[str]]:
    if not isinstance(plan, dict):
        return None, ["Missing validated plan."]

    contracts = plan.get("contracts") or []
    if not contracts:
        return None, ["Plan has no contracts."]

    lookup = _contract_artifact_lookup(coding_artifacts)
    manifest_contracts: list[DeploymentManifestContract] = []
    issues: list[str] = []
    explicit_primary_count = 0
    multiple_contracts = len(contracts) > 1

    for index, contract in enumerate(contracts, start=1):
        if not isinstance(contract, dict):
            continue
        name = str(contract.get("name") or "")
        if not name:
            issues.append("Encountered a contract without a name in the plan.")
            continue
        source_path = lookup.get(name)
        if not source_path:
            issues.append(f"Missing generated contract artifact for '{name}'.")
            continue

        role = contract.get("deployment_role")
        deploy_order = contract.get("deploy_order")
        if multiple_contracts:
            if role == "primary_deployable":
                explicit_primary_count += 1
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

        if role == "primary_deployable":
            explicit_primary_count += 0 if not multiple_contracts else 0

        if role:
            manifest_contracts.append(
                DeploymentManifestContract(
                    name=name,
                    role=str(role),
                    deploy_order=int(deploy_order or 1),
                    source_path=source_path,
                    constructor_args_schema=_constructor_arg_schema(contract),
                )
            )

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

    target = plan.get("deployment_target") or default_deployment_target_payload()
    manifest = (
        DeploymentManifest(deployment_target=target, contracts=manifest_contracts)
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

    if f"../contracts/{primary.name}.sol" not in script_code:
        issues.append(
            f"Deployment script does not import the manifest primary contract '{primary.name}'."
        )
    if primary.name not in script_code:
        issues.append(
            f"Deployment script does not reference the manifest primary contract '{primary.name}'."
        )
    return issues
