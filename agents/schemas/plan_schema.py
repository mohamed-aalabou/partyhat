from typing import Optional
from enum import Enum
from pydantic import BaseModel, Field

from schemas.deployment_schema import DeploymentTarget


class PlanStatus(str, Enum):
    """
    Lifecycle status of a smart contract plan.
    Rule: anything except 'deployed' can be freely edited by the user.
    Once deployed to Avalanche, the contract is immutable.
    """

    DRAFT = "draft"  # Actively being planned
    READY = "ready"  # Plan complete, Create agent can start
    GENERATING = "generating"  # Code being generated
    TESTING = "testing"  # Tests running
    DEPLOYING = "deploying"  # Deployment in progress
    DEPLOYED = "deployed"  # On-chain, immutable
    FAILED = "failed"  # Pipeline hit an unrecoverable error


# Functions inside a contract
class FunctionInput(BaseModel):
    name: str
    type: str
    description: str
    default_value: Optional[str] = Field(
        default=None,
        description=(
            "Optional deployment-time default. For constructor address inputs, "
            "use a concrete wallet address or the string 'deployer' when the "
            "deployer wallet should be used as the fallback."
        ),
    )


class FunctionOutput(BaseModel):
    type: str
    description: str


class ContractFunction(BaseModel):
    name: str
    description: str
    inputs: list[FunctionInput]
    outputs: list[FunctionOutput]
    conditions: list[str]


# The constructor that runs once on deployment
class Constructor(BaseModel):
    inputs: list[FunctionInput]
    description: str


class PostDeployCall(BaseModel):
    target_contract_name: str
    function_name: str
    args: list[str] = Field(default_factory=list)
    call_order: int
    description: str


# The schema of a smart contract
class ContractPlan(BaseModel):
    plan_contract_id: Optional[str] = Field(
        default=None,
        description="Stable opaque identifier linking this planned contract to downstream artifacts and deployments.",
    )
    name: str
    description: str
    erc_template: Optional[str]
    dependencies: list[str]
    deployment_role: Optional[str] = Field(
        default=None,
        description=(
            "Optional deployment role. Use 'primary_deployable' for the contract "
            "the default deployment pipeline should deploy."
        ),
    )
    deploy_order: Optional[int] = Field(
        default=None,
        description=(
            "Optional explicit deployment order for deployable contracts. "
            "Required when more than one contract participates in deployment."
        ),
    )
    constructor: Constructor
    functions: list[ContractFunction]


# Top-level output of the planning agent
class SmartContractPlan(BaseModel):
    project_name: str
    description: str
    status: PlanStatus = PlanStatus.DRAFT  # always starts as draft
    deployment_target: DeploymentTarget
    contracts: list[ContractPlan]
    post_deploy_calls: list[PostDeployCall] = Field(default_factory=list)


EXAMPLE_PLAN = SmartContractPlan(
    project_name="PartyToken",
    description="A simple ERC-20 token with minting and burning capabilities",
    status=PlanStatus.DRAFT,
    deployment_target=DeploymentTarget(
        network="avalanche_fuji",
        name="Avalanche Fuji",
        description="Default Avalanche Fuji deployment target.",
        chain_id=43113,
        rpc_url_env_var="FUJI_RPC_URL",
        private_key_env_var="FUJI_PRIVATE_KEY",
    ),
    contracts=[
        ContractPlan(
            name="PartyToken",
            description="Main ERC-20 token contract",
            erc_template="ERC-20",
            dependencies=["Ownable"],
            deployment_role="primary_deployable",
            deploy_order=1,
            constructor=Constructor(
                description="Sets token name, symbol, and mints initial supply to deployer",
                inputs=[
                    FunctionInput(name="name", type="string", description="Token name"),
                    FunctionInput(
                        name="symbol", type="string", description="Token symbol"
                    ),
                    FunctionInput(
                        name="initialSupply",
                        type="uint256",
                        description="Initial token supply",
                    ),
                ],
            ),
            functions=[
                ContractFunction(
                    name="mint",
                    description="Creates new tokens and assigns them to an address",
                    inputs=[
                        FunctionInput(
                            name="to", type="address", description="Recipient address"
                        ),
                        FunctionInput(
                            name="amount",
                            type="uint256",
                            description="Number of tokens to mint",
                        ),
                    ],
                    outputs=[],
                    conditions=[
                        "Caller must be the contract owner",
                        "to address cannot be zero address",
                    ],
                ),
                ContractFunction(
                    name="burn",
                    description="Destroys tokens from caller's balance",
                    inputs=[
                        FunctionInput(
                            name="amount",
                            type="uint256",
                            description="Number of tokens to burn",
                        ),
                    ],
                    outputs=[],
                    conditions=["Caller must have at least amount tokens"],
                ),
            ],
        )
    ],
    post_deploy_calls=[],
)

if __name__ == "__main__":
    import json

    print(json.dumps(EXAMPLE_PLAN.model_dump(), indent=2))
