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
    quiet_output: bool = Field(
        default=False,
        description=(
            "If True, forge is run without high verbosity (-v/-vv/-vvv/-vvvv) and "
            "stdout/stderr in the response are truncated to stay under the platform 50k limit. "
            "Use when the agent hits INVALID_ARGUMENT response length errors."
        ),
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


class SnowtraceVerifyRequest(BaseModel):
    """Request to verify a deployed contract on Snowtrace (Avalanche C-Chain explorer)."""

    contract_address: str = Field(
        ...,
        description="Deployed contract address (0x...) to verify on Snowtrace.",
    )
    contract_path: str = Field(
        ...,
        description=(
            "Contract path in format path/to/Contract.sol:ContractName, "
            "e.g. contracts/MyToken.sol:MyToken. Must be relative to project root."
        ),
    )
    chain_id: int = Field(
        default=43113,
        description="Chain ID. Use 43113 for Fuji testnet, 43114 for C-Chain mainnet.",
    )
    constructor_args: Optional[str] = Field(
        default=None,
        description=(
            "ABI-encoded constructor arguments as hex (e.g. from cast abi-encode). "
            "Omit if contract has no constructor args."
        ),
    )
    compiler_version: Optional[str] = Field(
        default=None,
        description="Solidity compiler version (e.g. v0.8.20). If omitted, Foundry uses build cache.",
    )
    optimizer_runs: Optional[int] = Field(
        default=None,
        description="Number of optimizer runs used during compilation. Omit to use build cache.",
    )
    api_key_env_var: str = Field(
        default="SNOWTRACE_API_KEY",
        description="Environment variable name for Snowtrace API key (optional for public use).",
    )
    project_root: Optional[str] = Field(
        default=None,
        description="Foundry project root path. If omitted, uses FOUNDRY_ARTIFACT_ROOT/project_id.",
    )

