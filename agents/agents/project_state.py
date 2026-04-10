import hashlib
import json
from typing import Any

from agents.contract_identity import enrich_artifact_with_plan_contract_ids
from agents.memory_manager import MemoryManager
from schemas.plan_schema import PlanStatus


def compact_execution_history(
    entries: list[dict],
    *,
    drop_output: bool = False,
) -> list[dict]:
    compacted: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        compact = dict(entry)
        compact.pop("stdout", None)
        compact.pop("stderr", None)
        if drop_output:
            compact.pop("output", None)
        compacted.append(compact)
    return compacted


def _stable_version(payload: dict[str, Any] | list[Any] | None) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def get_plan_state(mm: MemoryManager) -> dict[str, Any]:
    plan = mm.get_plan()
    return {
        "plan": plan,
        "status": (
            plan.get("status", PlanStatus.DRAFT.value)
            if isinstance(plan, dict)
            else None
        ),
    }


def get_code_state(
    mm: MemoryManager,
    *,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan is None:
        plan = mm.get_plan()
    state = mm.get_agent_state("coding")
    artifacts = [
        enrich_artifact_with_plan_contract_ids(
            plan,
            artifact,
            allow_name_fallback=True,
        )[0]
        for artifact in state.get("artifacts", [])
        if isinstance(artifact, dict)
    ]
    return {"artifacts": artifacts}


def get_deployment_state(mm: MemoryManager) -> dict[str, Any]:
    return {
        "last_deploy_results": compact_execution_history(mm.list_deployments(limit=20))
    }


def get_project_state_versions(
    *,
    user_id: str,
    project_id: str | None,
    allow_recompute: bool = True,
) -> dict[str, str]:
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    versions = mm.get_project_state_versions()
    if any(value != "0" for value in versions.values()) or not allow_recompute:
        return versions

    plan_state = get_plan_state(mm)
    code_state = get_code_state(mm, plan=plan_state.get("plan"))
    deployment_state = get_deployment_state(mm)
    return {
        "plan": _stable_version(plan_state),
        "code": _stable_version(code_state),
        "deployment": _stable_version(deployment_state),
    }


def get_project_state_resource(
    *,
    user_id: str,
    project_id: str | None,
    resource: str,
) -> dict[str, Any]:
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    versions = get_project_state_versions(user_id=user_id, project_id=project_id)

    if resource == "plan":
        state = get_plan_state(mm)
    elif resource == "code":
        plan_state = get_plan_state(mm)
        state = get_code_state(mm, plan=plan_state.get("plan"))
    elif resource == "deployment":
        state = get_deployment_state(mm)
    else:
        raise ValueError(f"Unknown project state resource '{resource}'")

    state["version"] = versions[resource]
    return state


def get_project_state_snapshot(
    *,
    user_id: str,
    project_id: str | None,
) -> dict[str, Any]:
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    plan_state = get_plan_state(mm)
    code_state = get_code_state(mm, plan=plan_state.get("plan"))
    deployment_state = get_deployment_state(mm)
    versions = get_project_state_versions(user_id=user_id, project_id=project_id)
    return {
        "plan": {
            **plan_state,
            "version": versions["plan"],
        },
        "code": {
            **code_state,
            "version": versions["code"],
        },
        "deployment": {
            **deployment_state,
            "version": versions["deployment"],
        },
        "versions": versions,
    }
