from __future__ import annotations

from typing import Any


def extract_plan_summary(plan: dict | None) -> dict:
    """Build a compact project summary suitable for Letta and task context."""
    if not isinstance(plan, dict):
        return {
            "project_name": None,
            "erc_standard": None,
            "contract_names": [],
            "key_constraints": [],
        }

    contracts = plan.get("contracts") or []
    contract_names = [
        contract.get("name")
        for contract in contracts
        if isinstance(contract, dict) and contract.get("name")
    ]

    erc_templates = []
    constraints: list[str] = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        erc_template = contract.get("erc_template")
        if erc_template:
            erc_templates.append(str(erc_template))
        for dependency in contract.get("dependencies") or []:
            if dependency:
                constraints.append(f"dependency:{dependency}")
        description = contract.get("description")
        if description:
            constraints.append(str(description))

    unique_ercs = list(dict.fromkeys(erc_templates))
    if len(unique_ercs) == 1:
        erc_standard: str | list[str] | None = unique_ercs[0]
    elif unique_ercs:
        erc_standard = unique_ercs
    else:
        erc_standard = None

    unique_constraints = list(dict.fromkeys(constraints))
    return {
        "project_name": plan.get("project_name"),
        "erc_standard": erc_standard,
        "contract_names": contract_names,
        "key_constraints": unique_constraints[:12],
    }


def default_expected_outputs(task_type: str) -> list[str]:
    mapping = {
        "coding.generate_contracts": ["contracts/**/*.sol artifacts saved"],
        "testing.generate_tests": ["test/**/*Test.t.sol artifacts saved"],
        "testing.run_tests": ["compact Foundry test result recorded", "next pipeline task routed"],
        "deployment.prepare_script": ["script/**/*.s.sol artifact saved"],
        "deployment.execute_deploy": ["deployment attempt recorded"],
        "deployment.retry_deploy": ["deployment retry recorded"],
    }
    return mapping.get(task_type, ["task result recorded"])


def standardize_task_context(
    context: dict | None = None,
    *,
    plan_summary: dict | None = None,
    artifact_revision: int = 0,
    input_artifacts: dict | list | None = None,
    upstream_task: dict | None = None,
    failure_context: dict | None = None,
    expected_outputs: list[str] | None = None,
) -> dict:
    """Normalize task context so every pipeline task carries the same shape."""
    normalized = dict(context or {})
    normalized["artifact_revision"] = int(
        normalized.get("artifact_revision", artifact_revision) or 0
    )
    normalized["plan_summary"] = normalized.get("plan_summary") or plan_summary or {}
    normalized["input_artifacts"] = (
        normalized.get("input_artifacts")
        if normalized.get("input_artifacts") is not None
        else (input_artifacts if input_artifacts is not None else {})
    )
    normalized["upstream_task"] = normalized.get("upstream_task") or upstream_task
    normalized["failure_context"] = (
        normalized.get("failure_context")
        if normalized.get("failure_context") is not None
        else failure_context
    )
    normalized["expected_outputs"] = (
        normalized.get("expected_outputs")
        or expected_outputs
        or default_expected_outputs(str(normalized.get("task_type", "")))
    )
    return normalized


def compact_execution_summary(exit_code: int, stdout: str, stderr: str) -> str:
    """Return a short summary line without embedding full execution logs."""
    text = (stderr or stdout or "").strip()
    if not text:
        return f"exit_code={exit_code}"
    first_line = text.splitlines()[0].strip()
    return f"exit_code={exit_code}: {first_line[:200]}"


def duration_ms(start, end) -> int | None:
    if start is None or end is None:
        return None
    delta = end - start
    return max(0, int(delta.total_seconds() * 1000))


def merge_artifact_snapshots(*snapshots: Any) -> dict:
    merged = {"coding": [], "testing": [], "deployment": []}
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        for key in merged:
            value = snapshot.get(key)
            if isinstance(value, list):
                merged[key] = value
    return merged
