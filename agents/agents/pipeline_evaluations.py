from __future__ import annotations

import re
from typing import Any

from agents.code_storage import get_code_storage
from agents.deployment_manifest import (
    MANIFEST_PATH,
    build_deployment_manifest,
    dump_deployment_manifest,
    load_deployment_manifest,
    validate_deploy_script_against_manifest,
)
from agents.memory_manager import MemoryManager
from schemas.coding_schema import CodeArtifact


def _upsert_agent_artifact(mm: MemoryManager, agent_name: str, artifact: dict[str, Any]) -> None:
    data, block = mm._read_user_block()  # type: ignore[attr-defined]
    mm._ensure_agents_structure(data)  # type: ignore[attr-defined]
    agent_state = data["agents"][agent_name]
    artifacts = agent_state.get("artifacts", [])
    filtered = [entry for entry in artifacts if entry.get("path") != artifact.get("path")]
    filtered.append(artifact)
    agent_state["artifacts"] = filtered
    mm.client.blocks.update(  # type: ignore[attr-defined]
        block.id,
        value=mm._serialize(data),  # type: ignore[attr-defined]
    )


def _memory_manager(project_id: str, user_id: str) -> MemoryManager:
    return MemoryManager(user_id=user_id, project_id=project_id)


def evaluate_code_generation(project_id: str, user_id: str) -> dict[str, Any]:
    mm = _memory_manager(project_id, user_id)
    plan = mm.get_plan() or {}
    coding_state = mm.get_agent_state("coding")
    artifacts = coding_state.get("artifacts", [])
    manifest, issues = build_deployment_manifest(plan, artifacts)
    artifact_revision = int(coding_state.get("latest_artifact_revision", 0) or 0)

    if manifest is None:
        return {
            "status": "failed",
            "blocking": True,
            "evaluation_type": "codegen_manifest",
            "summary": "Generated artifacts do not satisfy deployment manifest requirements.",
            "details": {"issues": issues},
            "artifact_revision": artifact_revision,
        }

    raw_manifest = dump_deployment_manifest(manifest)
    storage = get_code_storage(project_id=project_id)
    storage.save_code(
        CodeArtifact(path=MANIFEST_PATH, language="json"),
        raw_manifest,
    )
    _upsert_agent_artifact(
        mm,
        "coding",
        {
            "path": MANIFEST_PATH,
            "language": "json",
            "description": "Authoritative deployment manifest",
            "contract_names": [c.name for c in manifest.contracts],
        },
    )

    return {
        "status": "passed",
        "blocking": True,
        "evaluation_type": "codegen_manifest",
        "summary": "Generated contract artifacts and deployment manifest are valid.",
        "details": {"manifest_path": MANIFEST_PATH},
        "artifact_revision": artifact_revision,
    }


def evaluate_generated_tests(project_id: str, user_id: str) -> dict[str, Any]:
    mm = _memory_manager(project_id, user_id)
    testing_state = mm.get_agent_state("testing")
    artifacts = testing_state.get("artifacts", [])
    storage = get_code_storage(project_id=project_id)
    issues: list[str] = []
    canonical_pattern = re.compile(r"^test/.+Test\.t\.sol$")

    test_artifacts = [artifact for artifact in artifacts if str(artifact.get("path") or "").startswith("test/")]
    if not test_artifacts:
        issues.append("No test artifacts were generated under test/.")

    for artifact in test_artifacts:
        path = str(artifact.get("path") or "")
        if not canonical_pattern.match(path):
            issues.append(f"Test artifact '{path}' is not in canonical test/*Test.t.sol form.")
            continue
        try:
            code = storage.load_code(path)
        except Exception as exc:
            issues.append(f"Could not load generated test artifact '{path}': {exc}")
            continue
        if "../src/" in code:
            issues.append(f"Test artifact '{path}' still imports ../src/.")

    return {
        "status": "failed" if issues else "passed",
        "blocking": True,
        "evaluation_type": "test_artifacts",
        "summary": (
            "Generated Foundry tests are canonical and loadable."
            if not issues
            else "Generated Foundry tests failed canonical validation."
        ),
        "details": {"issues": issues} if issues else {"count": len(test_artifacts)},
        "artifact_revision": int(
            mm.get_agent_state("coding").get("latest_artifact_revision", 0) or 0
        ),
    }


def load_saved_manifest(project_id: str) -> tuple[Any | None, str | None]:
    storage = get_code_storage(project_id=project_id)
    try:
        raw = storage.load_code(MANIFEST_PATH)
    except Exception as exc:
        return None, str(exc)
    try:
        return load_deployment_manifest(raw), None
    except Exception as exc:
        return None, str(exc)


def evaluate_deployment_prepare(
    project_id: str,
    user_id: str,
    script_path: str,
) -> dict[str, Any]:
    manifest, manifest_error = load_saved_manifest(project_id)
    if manifest is None:
        return {
            "status": "failed",
            "blocking": True,
            "evaluation_type": "deployment_prepare",
            "summary": "Deployment manifest could not be loaded.",
            "details": {"issues": [manifest_error or "manifest missing"]},
            "artifact_revision": int(
                _memory_manager(project_id, user_id)
                .get_agent_state("coding")
                .get("latest_artifact_revision", 0)
                or 0
            ),
        }

    storage = get_code_storage(project_id=project_id)
    try:
        script_code = storage.load_code(script_path)
    except Exception as exc:
        return {
            "status": "failed",
            "blocking": True,
            "evaluation_type": "deployment_prepare",
            "summary": "Deployment script could not be loaded.",
            "details": {"issues": [str(exc)]},
            "artifact_revision": int(
                _memory_manager(project_id, user_id)
                .get_agent_state("coding")
                .get("latest_artifact_revision", 0)
                or 0
            ),
        }

    issues = validate_deploy_script_against_manifest(manifest, script_code)
    return {
        "status": "failed" if issues else "passed",
        "blocking": True,
        "evaluation_type": "deployment_prepare",
        "summary": (
            "Deployment script matches the deployment manifest."
            if not issues
            else "Deployment script does not match the deployment manifest."
        ),
        "details": {"issues": issues, "script_path": script_path},
        "artifact_revision": int(
            _memory_manager(project_id, user_id)
            .get_agent_state("coding")
            .get("latest_artifact_revision", 0)
            or 0
        ),
    }
