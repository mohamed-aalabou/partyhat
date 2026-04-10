from agents.contract_identity import (
    enrich_artifact_with_plan_contract_ids,
    normalize_plan_contracts,
    validate_artifact_for_save,
)


def _plan(*contracts):
    return {
        "project_name": "PartyToken",
        "contracts": list(contracts),
    }


def test_normalize_plan_contracts_generates_ids_for_missing_contracts():
    normalized = normalize_plan_contracts(
        _plan(
            {"name": "PartyToken"},
            {"name": "PartyTreasury"},
        )
    )

    ids = [contract["plan_contract_id"] for contract in normalized["contracts"]]
    assert len(ids) == 2
    assert all(plan_contract_id.startswith("pc_") for plan_contract_id in ids)
    assert len(set(ids)) == 2


def test_normalize_plan_contracts_preserves_id_across_rename():
    previous = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_existing"},
    )

    normalized = normalize_plan_contracts(
        _plan({"name": "PartyCoin"}),
        previous_plan=previous,
    )

    assert normalized["contracts"][0]["plan_contract_id"] == "pc_existing"


def test_normalize_plan_contracts_assigns_new_id_only_to_new_contract():
    previous = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_existing"},
    )

    normalized = normalize_plan_contracts(
        _plan(
            {"name": "PartyToken"},
            {"name": "PartyTreasury"},
        ),
        previous_plan=previous,
    )

    assert normalized["contracts"][0]["plan_contract_id"] == "pc_existing"
    assert normalized["contracts"][1]["plan_contract_id"].startswith("pc_")
    assert normalized["contracts"][1]["plan_contract_id"] != "pc_existing"


def test_validate_artifact_for_save_requires_known_plan_contract_ids():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token"},
    )

    enriched, issues = validate_artifact_for_save(
        plan,
        {
            "path": "contracts/PartyToken.sol",
            "contract_names": ["PartyToken"],
            "plan_contract_ids": ["pc_missing"],
        },
    )

    assert enriched["plan_contract_ids"] == []
    assert any("pc_missing" in issue for issue in issues)


def test_validate_artifact_for_save_keeps_supplied_plan_contract_ids():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token"},
    )

    enriched, issues = validate_artifact_for_save(
        plan,
        {
            "path": "contracts/PartyToken.sol",
            "contract_names": ["PartyToken"],
            "plan_contract_ids": ["pc_token"],
        },
    )

    assert issues == []
    assert enriched["plan_contract_ids"] == ["pc_token"]
    assert enriched["contract_names"] == ["PartyToken"]


def test_enrich_artifact_with_plan_contract_ids_uses_unique_name_fallback():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token"},
    )

    enriched, issues = enrich_artifact_with_plan_contract_ids(
        plan,
        {
            "path": "contracts/PartyToken.sol",
            "contract_names": ["PartyToken"],
        },
        allow_name_fallback=True,
    )

    assert issues == []
    assert enriched["plan_contract_ids"] == ["pc_token"]


def test_enrich_artifact_with_plan_contract_ids_ignores_non_plan_alias_when_plan_name_resolves():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token"},
    )

    enriched, issues = enrich_artifact_with_plan_contract_ids(
        plan,
        {
            "path": "test/PartyTokenTest.t.sol",
            "contract_names": ["PartyTokenTest", "PartyToken"],
        },
        allow_name_fallback=True,
    )

    assert issues == []
    assert enriched["plan_contract_ids"] == ["pc_token"]


def test_enrich_artifact_with_plan_contract_ids_leaves_ambiguous_name_unlinked():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token_a"},
        {"name": "PartyToken", "plan_contract_id": "pc_token_b"},
    )

    enriched, issues = enrich_artifact_with_plan_contract_ids(
        plan,
        {
            "path": "contracts/PartyToken.sol",
            "contract_names": ["PartyToken"],
        },
        allow_name_fallback=True,
    )

    assert enriched["plan_contract_ids"] == []
    assert any("could not be linked to a unique planned contract" in issue for issue in issues)


def test_enrich_artifact_with_plan_contract_ids_flags_artifact_without_names_or_ids():
    plan = _plan(
        {"name": "PartyToken", "plan_contract_id": "pc_token"},
    )

    enriched, issues = enrich_artifact_with_plan_contract_ids(
        plan,
        {
            "path": "contracts/PartyToken.sol",
        },
        allow_name_fallback=True,
    )

    assert enriched["plan_contract_ids"] == []
    assert any("could not be linked to any planned contract" in issue for issue in issues)
