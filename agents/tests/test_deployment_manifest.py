from agents.deployment_manifest import (
    build_deployment_manifest,
    validate_deploy_script_against_manifest,
)
from agents.deployment_tools import generate_foundry_deploy_script_direct
from agents.pipeline_specs import default_deployment_target_payload
from schemas.deployment_schema import FoundryDeployScriptGenerationRequest


def _avavest_plan() -> dict:
    return {
        "project_name": "AvaVest",
        "description": "Token plus vesting",
        "status": "ready",
        "deployment_target": default_deployment_target_payload(),
        "contracts": [
            {
                "plan_contract_id": "pc_vesting",
                "name": "AvaVestVesting",
                "description": "Primary vesting contract",
                "deployment_role": "primary_deployable",
                "deploy_order": 1,
                "constructor": {"inputs": [], "description": "Default constructor"},
                "functions": [{"name": "setToken"}],
            },
            {
                "plan_contract_id": "pc_token",
                "name": "AvaVestToken",
                "description": "Supporting token contract",
                "deployment_role": "supporting",
                "deploy_order": 2,
                "constructor": {
                    "inputs": [
                        {
                            "name": "vesting",
                            "type": "address",
                            "description": "Linked vesting contract",
                            "default_value": "<deployed:AvaVestVesting.address>",
                        }
                    ],
                    "description": "Store vesting address",
                },
                "functions": [{"name": "mint"}],
            },
        ],
        "post_deploy_calls": [
            {
                "target_contract_name": "AvaVestVesting",
                "function_name": "setToken",
                "args": ["<deployed:AvaVestToken.address>"],
                "call_order": 1,
                "description": "Wire the token back into vesting",
            }
        ],
    }


def _coding_artifacts() -> list[dict]:
    return [
        {
            "path": "contracts/AvaVestVesting.sol",
            "contract_names": ["AvaVestVesting"],
            "plan_contract_ids": ["pc_vesting"],
        },
        {
            "path": "contracts/AvaVestToken.sol",
            "contract_names": ["AvaVestToken"],
            "plan_contract_ids": ["pc_token"],
        },
    ]


def test_build_deployment_manifest_includes_ordered_contracts_and_post_deploy_calls():
    manifest, issues = build_deployment_manifest(_avavest_plan(), _coding_artifacts())

    assert issues == []
    assert manifest is not None
    assert [contract.name for contract in manifest.contracts] == [
        "AvaVestVesting",
        "AvaVestToken",
    ]
    assert [contract.deploy_order for contract in manifest.contracts] == [1, 2]
    assert manifest.post_deploy_calls[0].target_contract_name == "AvaVestVesting"
    assert manifest.post_deploy_calls[0].target_plan_contract_id == "pc_vesting"
    assert manifest.post_deploy_calls[0].args == ["<deployed:AvaVestToken.address>"]


def test_build_deployment_manifest_rejects_unknown_placeholder_and_duplicate_call_order():
    plan = _avavest_plan()
    plan["contracts"][1]["constructor"]["inputs"][0]["default_value"] = (
        "<deployed:MissingToken.address>"
    )
    plan["post_deploy_calls"].append(
        {
            "target_contract_name": "AvaVestVesting",
            "function_name": "setAdmin",
            "args": ["deployer"],
            "call_order": 1,
            "description": "Duplicate order",
        }
    )

    manifest, issues = build_deployment_manifest(plan, _coding_artifacts())

    assert manifest is None
    assert any("unknown deployed contract 'MissingToken'" in issue for issue in issues)
    assert any("Duplicate post_deploy_calls call_order 1" in issue for issue in issues)


def test_generate_foundry_deploy_script_direct_builds_multi_contract_script_from_manifest():
    manifest, issues = build_deployment_manifest(_avavest_plan(), _coding_artifacts())
    assert issues == []
    assert manifest is not None

    result = generate_foundry_deploy_script_direct(
        FoundryDeployScriptGenerationRequest(
            goal="Deploy the AvaVest contracts.",
            contract_name="AvaVestVesting",
            script_name="DeployAvaVestVesting",
            deployment_manifest=manifest.model_dump(),
        )
    )

    script = result["generated_script"]
    vesting_index = script.index("new AvaVestVesting(")
    token_index = script.index("new AvaVestToken(address(avaVestVesting))")
    call_index = script.index("avaVestVesting.setToken(address(avaVestToken));")

    assert 'import {AvaVestVesting} from "../contracts/AvaVestVesting.sol";' in script
    assert 'import {AvaVestToken} from "../contracts/AvaVestToken.sol";' in script
    assert vesting_index < token_index < call_index
    assert validate_deploy_script_against_manifest(manifest, script) == []
