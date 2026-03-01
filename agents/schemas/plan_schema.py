from typing import Optional
from enum import Enum
from pydantic import BaseModel


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
    DEPLOYED = "deployed"  # On-chain, so locked forever?!


# Functions inside a contract
class FunctionInput(BaseModel):
    name: str
    type: str
    description: str


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


# The schema of a smart contract
class ContractPlan(BaseModel):
    name: str
    description: str
    erc_template: Optional[str]
    dependencies: list[str]
    constructor: Constructor
    functions: list[ContractFunction]


# Top-level output of the planning agent
class SmartContractPlan(BaseModel):
    project_name: str
    description: str
    status: PlanStatus = PlanStatus.DRAFT  # always starts as draft
    contracts: list[ContractPlan]


EXAMPLE_PLAN = SmartContractPlan(
    project_name="PartyToken",
    description="A simple ERC-20 token with minting and burning capabilities",
    status=PlanStatus.DRAFT,
    contracts=[
        ContractPlan(
            name="PartyToken",
            description="Main ERC-20 token contract",
            erc_template="ERC-20",
            dependencies=["Ownable"],
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
)

if __name__ == "__main__":
    import json

    print(json.dumps(EXAMPLE_PLAN.model_dump(), indent=2))
