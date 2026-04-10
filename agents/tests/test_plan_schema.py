from schemas.deployment_schema import DeploymentTarget, FoundryDeployRequest
from schemas.plan_schema import SmartContractPlan


def test_deployment_target_normalizes_split_fuji_fields():
    target = DeploymentTarget.model_validate(
        {
            "network": "avalanche",
            "name": "fuji",
            "description": "Avalanche Fuji testnet",
            "chain_id": 43113,
            "rpc_url_env_var": "FUJI_RPC_URL",
            "private_key_env_var": "FUJI_PRIVATE_KEY",
        }
    )

    assert target.network == "avalanche_fuji"
    assert target.name == "Avalanche Fuji"
    assert target.chain_id == 43113


def test_foundry_deploy_request_normalizes_split_fuji_fields():
    request = FoundryDeployRequest.model_validate(
        {
            "script_path": "script/DeployPartyToken.s.sol",
            "network": "avalanche",
            "name": "fuji",
            "chain_id": 43113,
            "rpc_url_env_var": "FUJI_RPC_URL",
            "private_key_env_var": "FUJI_PRIVATE_KEY",
        }
    )

    assert request.network == "avalanche_fuji"
    assert request.chain_id == 43113


def test_smart_contract_plan_normalizes_legacy_fuji_target():
    plan = SmartContractPlan.model_validate(
        {
            "project_name": "PartyToken",
            "description": "Token plan",
            "status": "draft",
            "deployment_target": {
                "network": "avalanche",
                "name": "fuji",
                "description": "Avalanche Fuji testnet",
                "chain_id": 43113,
                "rpc_url_env_var": "FUJI_RPC_URL",
                "private_key_env_var": "FUJI_PRIVATE_KEY",
            },
            "contracts": [],
        }
    )

    assert plan.deployment_target.network == "avalanche_fuji"
    assert plan.deployment_target.name == "Avalanche Fuji"
