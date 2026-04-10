from __future__ import annotations

import uuid
from typing import Any


def new_plan_contract_id() -> str:
    return f"pc_{uuid.uuid4().hex}"


def _normalize_contract_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_plan_contract_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def extract_plan_contracts(plan: dict | None) -> list[dict[str, Any]]:
    if not isinstance(plan, dict):
        return []

    entries: list[dict[str, Any]] = []
    for contract in plan.get("contracts") or []:
        if not isinstance(contract, dict):
            continue
        plan_contract_id = _normalize_plan_contract_id(contract.get("plan_contract_id"))
        name = _normalize_contract_name(contract.get("name"))
        entries.append(
            {
                "plan_contract_id": plan_contract_id,
                "name": name,
                "deployment_role": contract.get("deployment_role"),
                "deploy_order": contract.get("deploy_order"),
                "contract": contract,
            }
        )
    return entries


def _remaining_unique_name_matches(
    entries: list[dict[str, Any]],
    used_ids: set[str],
) -> dict[str, str]:
    grouped: dict[str, list[str]] = {}
    for entry in entries:
        plan_contract_id = entry.get("plan_contract_id")
        name = entry.get("name")
        if not plan_contract_id or not name or plan_contract_id in used_ids:
            continue
        grouped.setdefault(name, []).append(plan_contract_id)
    return {
        name: ids[0]
        for name, ids in grouped.items()
        if len(ids) == 1
    }


def normalize_plan_contracts(
    plan: dict | None,
    *,
    previous_plan: dict | None = None,
) -> dict | None:
    if not isinstance(plan, dict):
        return plan

    normalized = dict(plan)
    raw_contracts = plan.get("contracts") or []
    previous_entries = extract_plan_contracts(previous_plan)

    used_previous_ids: set[str] = set()
    normalized_contracts: list[Any] = []
    unresolved_indices: list[int] = []

    for contract in raw_contracts:
        if not isinstance(contract, dict):
            normalized_contracts.append(contract)
            continue
        item = dict(contract)
        explicit_id = _normalize_plan_contract_id(item.get("plan_contract_id"))
        if explicit_id:
            item["plan_contract_id"] = explicit_id
            used_previous_ids.add(explicit_id)
        else:
            unresolved_indices.append(len(normalized_contracts))
        normalized_contracts.append(item)

    if unresolved_indices:
        name_matches = _remaining_unique_name_matches(previous_entries, used_previous_ids)
        for idx in list(unresolved_indices):
            item = normalized_contracts[idx]
            if not isinstance(item, dict):
                continue
            name = _normalize_contract_name(item.get("name"))
            matched_id = name_matches.get(name or "")
            if matched_id:
                item["plan_contract_id"] = matched_id
                used_previous_ids.add(matched_id)
                unresolved_indices.remove(idx)

    if unresolved_indices and len(raw_contracts) == len(previous_entries):
        for idx in list(unresolved_indices):
            item = normalized_contracts[idx]
            if not isinstance(item, dict):
                continue
            previous_entry = previous_entries[idx] if idx < len(previous_entries) else None
            matched_id = (
                previous_entry.get("plan_contract_id")
                if isinstance(previous_entry, dict)
                else None
            )
            if matched_id and matched_id not in used_previous_ids:
                item["plan_contract_id"] = matched_id
                used_previous_ids.add(matched_id)
                unresolved_indices.remove(idx)

    for idx in unresolved_indices:
        item = normalized_contracts[idx]
        if not isinstance(item, dict):
            continue
        item["plan_contract_id"] = new_plan_contract_id()

    normalized["contracts"] = normalized_contracts
    return normalized


def plan_contract_lookup(plan: dict | None) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    entries = extract_plan_contracts(plan)
    by_id = {
        entry["plan_contract_id"]: entry["contract"]
        for entry in entries
        if entry.get("plan_contract_id")
    }
    by_unique_name = _remaining_unique_name_matches(entries, set())
    return by_id, by_unique_name


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def resolve_plan_contract_ids(
    plan: dict | None,
    artifact: dict[str, Any],
    *,
    allow_name_fallback: bool,
) -> tuple[list[str], list[str]]:
    by_id, by_unique_name = plan_contract_lookup(plan)
    issues: list[str] = []

    resolved_from_ids: list[str] = []
    raw_ids = artifact.get("plan_contract_ids") or []
    for raw_id in raw_ids:
        normalized_id = _normalize_plan_contract_id(raw_id)
        if not normalized_id:
            continue
        if normalized_id not in by_id:
            issues.append(
                f"Unknown plan_contract_id '{normalized_id}' for artifact "
                f"'{artifact.get('path') or '<unknown>'}'."
            )
            continue
        resolved_from_ids.append(normalized_id)

    resolved_from_ids = _dedupe_strings(resolved_from_ids)
    if resolved_from_ids:
        return resolved_from_ids, issues

    if not allow_name_fallback:
        issues.append(
            f"Artifact '{artifact.get('path') or '<unknown>'}' is missing plan_contract_ids."
        )
        return [], issues

    resolved_from_names: list[str] = []
    unresolved_names: list[str] = []
    for raw_name in artifact.get("contract_names") or []:
        name = _normalize_contract_name(raw_name)
        if not name:
            continue
        matched_id = by_unique_name.get(name)
        if matched_id:
            resolved_from_names.append(matched_id)
        else:
            unresolved_names.append(name)

    resolved_from_names = _dedupe_strings(resolved_from_names)
    if resolved_from_names:
        return resolved_from_names, issues

    if not unresolved_names:
        issues.append(
            f"Artifact '{artifact.get('path') or '<unknown>'}' could not be linked to any planned contract."
        )
        return resolved_from_names, issues

    for name in unresolved_names:
        issues.append(
            f"Artifact '{artifact.get('path') or '<unknown>'}' contract name '{name}' "
            "could not be linked to a unique planned contract."
        )
    return resolved_from_names, issues


def enrich_artifact_with_plan_contract_ids(
    plan: dict | None,
    artifact: dict[str, Any],
    *,
    allow_name_fallback: bool,
) -> tuple[dict[str, Any], list[str]]:
    enriched = dict(artifact)
    resolved_ids, issues = resolve_plan_contract_ids(
        plan,
        enriched,
        allow_name_fallback=allow_name_fallback,
    )
    if resolved_ids:
        enriched["plan_contract_ids"] = resolved_ids
    else:
        enriched["plan_contract_ids"] = []
    return enriched, issues


def validate_artifact_for_save(
    plan: dict | None,
    artifact: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    enriched, issues = enrich_artifact_with_plan_contract_ids(
        plan,
        artifact,
        allow_name_fallback=False,
    )
    if issues:
        return enriched, issues

    by_id, _ = plan_contract_lookup(plan)
    canonical_names: list[str] = []
    for plan_contract_id in enriched.get("plan_contract_ids") or []:
        contract = by_id.get(plan_contract_id)
        name = _normalize_contract_name((contract or {}).get("name"))
        if name:
            canonical_names.append(name)

    existing_names = [
        name
        for name in enriched.get("contract_names") or []
        if _normalize_contract_name(name)
    ]
    enriched["contract_names"] = _dedupe_strings(existing_names + canonical_names)
    return enriched, []
