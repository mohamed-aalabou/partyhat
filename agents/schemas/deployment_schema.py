from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class DeploymentTarget(BaseModel):
    network: str
    name: str
    description: Optional[str] = None
    chain_id: Optional[int] = None
    rpc_url_env_var: Optional[str] = None
    private_key_env_var: Optional[str] = None


class DeploymentRecord(BaseModel):
    target: DeploymentTarget
    tx_hash: Optional[str] = None
    status: DeploymentStatus
    notes: Optional[str] = None
    deployed_address: Optional[str] = None
    contract_name: Optional[str] = None
    script_path: Optional[str] = None
    chain_id: Optional[int] = None
    command: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None


class FoundryDeployScriptGenerationRequest(BaseModel):
    goal: str = Field(
        ...,
        description=(
            "High-level deployment goal describing which contract should be "
            "deployed and with what initialization behavior."
        ),
    )
    contract_name: str = Field(
        ...,
        description="Primary Solidity contract to deploy.",
    )
    script_name: str = Field(
        default="DeployScript",
        description="Solidity script contract name used for Foundry deployment.",
    )
    constructor_args: List[str] = Field(
        default_factory=list,
        description=(
            "Constructor arguments as Solidity literals (e.g. "
            "\"\\\"Party\\\"\", \"\\\"PRTY\\\"\", \"1000000 ether\")."
        ),
    )
    plan_summary: Optional[str] = Field(
        default=None,
        description="Optional natural-language summary of the validated contract plan.",
    )
    contract_sources: Optional[str] = Field(
        default=None,
        description="Optional concatenated Solidity sources for deployed contracts.",
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Optional deployment constraints and operational guardrails.",
    )


class FoundryDeployRequest(BaseModel):
    script_path: str = Field(
        ...,
        description="Relative script path, typically script/<Name>.s.sol.",
    )
    network: str = Field(
        default="avalanche_fuji",
        description="Deployment target network. Current supported value: avalanche_fuji.",
    )
    chain_id: int = Field(
        default=43113,
        description="Chain ID for Avalanche Fuji.",
    )
    rpc_url_env_var: str = Field(
        default="FUJI_RPC_URL",
        description="Environment variable holding Avalanche Fuji RPC URL.",
    )
    private_key_env_var: str = Field(
        default="FUJI_PRIVATE_KEY",
        description="Environment variable holding deployer private key.",
    )
    contract_name: Optional[str] = Field(
        default=None,
        description="Optional fully-qualified contract name for --tc filtering.",
    )
    constructor_args: List[str] = Field(
        default_factory=list,
        description="Optional constructor args as Solidity literals passed via --sig run(...).",
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Optional extra forge script CLI arguments.",
    )
    broadcast: bool = Field(
        default=True,
        description="Whether to include --broadcast when running forge script.",
    )


class FoundryDeployResult(BaseModel):
    success: bool
    exit_code: int
    command: str
    script_path: str
    network: str
    chain_id: int
    tx_hash: Optional[str] = None
    deployed_address: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None

