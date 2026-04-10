from enum import Enum
import re
from typing import Any, Optional, List

from pydantic import BaseModel, Field, model_validator


FUJI_NETWORK = "avalanche_fuji"
FUJI_NAME = "Avalanche Fuji"
FUJI_DESCRIPTION = "Avalanche Fuji testnet"
FUJI_CHAIN_ID = 43113
FUJI_RPC_ENV_VAR = "FUJI_RPC_URL"
FUJI_PRIVATE_KEY_ENV_VAR = "FUJI_PRIVATE_KEY"


def _normalize_network_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or None


def _coerce_fuji_target_payload(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    payload = dict(value)
    network_token = _normalize_network_token(payload.get("network"))
    name_token = _normalize_network_token(payload.get("name"))
    description_token = _normalize_network_token(payload.get("description"))
    chain_id = payload.get("chain_id")
    rpc_url_env_var = payload.get("rpc_url_env_var")
    private_key_env_var = payload.get("private_key_env_var")

    looks_like_fuji = any(
        [
            network_token == FUJI_NETWORK,
            network_token == "fuji",
            network_token in {"avalanche", "avax"} and name_token == "fuji",
            name_token in {FUJI_NETWORK, "fuji"},
            description_token is not None
            and "avalanche" in description_token
            and "fuji" in description_token,
            chain_id == FUJI_CHAIN_ID,
            rpc_url_env_var == FUJI_RPC_ENV_VAR,
            private_key_env_var == FUJI_PRIVATE_KEY_ENV_VAR,
        ]
    )
    if not looks_like_fuji:
        return payload

    payload["network"] = FUJI_NETWORK
    payload["name"] = FUJI_NAME
    payload.setdefault("description", FUJI_DESCRIPTION)
    payload.setdefault("chain_id", FUJI_CHAIN_ID)
    payload.setdefault("rpc_url_env_var", FUJI_RPC_ENV_VAR)
    payload.setdefault("private_key_env_var", FUJI_PRIVATE_KEY_ENV_VAR)
    return payload


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

    @model_validator(mode="before")
    @classmethod
    def _normalize_fuji_target(cls, value: Any) -> Any:
        return _coerce_fuji_target_payload(value)


class ConstructorArgSchema(BaseModel):
    name: str
    type: str
    source: str
    default_value: Optional[str] = None


class DeploymentManifestContract(BaseModel):
    plan_contract_id: str
    name: str
    role: str
    deploy_order: int
    source_path: str
    constructor_args_schema: List[ConstructorArgSchema] = Field(default_factory=list)


class DeploymentManifestPostDeployCall(BaseModel):
    target_contract_name: str
    target_plan_contract_id: str
    function_name: str
    args: List[str] = Field(default_factory=list)
    call_order: int
    description: str


class DeploymentManifest(BaseModel):
    deployment_target: DeploymentTarget
    contracts: List[DeploymentManifestContract]
    post_deploy_calls: List[DeploymentManifestPostDeployCall] = Field(default_factory=list)


class DeployedContractResult(BaseModel):
    contract_name: str
    plan_contract_id: Optional[str] = None
    deploy_order: Optional[int] = None
    tx_hash: Optional[str] = None
    deployed_address: Optional[str] = None


class ExecutedCallResult(BaseModel):
    target_contract_name: str
    target_plan_contract_id: Optional[str] = None
    function_name: str
    args: List[str] = Field(default_factory=list)
    call_order: Optional[int] = None
    tx_hash: Optional[str] = None
    status: Optional[str] = None


class DeploymentRecord(BaseModel):
    target: DeploymentTarget
    tx_hash: Optional[str] = None
    status: DeploymentStatus
    notes: Optional[str] = None
    pipeline_run_id: Optional[str] = None
    pipeline_task_id: Optional[str] = None
    deployed_address: Optional[str] = None
    contract_name: Optional[str] = None
    plan_contract_id: Optional[str] = None
    script_path: Optional[str] = None
    chain_id: Optional[int] = None
    command: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    exit_code: Optional[int] = None
    deployed_contracts: List[DeployedContractResult] = Field(default_factory=list)
    executed_calls: List[ExecutedCallResult] = Field(default_factory=list)


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
            "Constructor arguments as Solidity expressions (e.g. "
            "\"\\\"Party\\\"\", \"\\\"PRTY\\\"\", \"1000000 ether\", "
            "or \"deployer\" for the broadcaster address derived from the "
            "deployment private key)."
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
    deployment_manifest: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional authoritative deployment manifest used to generate a "
            "single ordered multi-contract deployment script."
        ),
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
    plan_contract_id: Optional[str] = Field(
        default=None,
        description="Optional planned contract identifier to persist with deployment records.",
    )
    constructor_args: List[str] = Field(
        default_factory=list,
        description=(
            "Optional constructor args as Solidity expressions passed via "
            "--sig run(...)."
        ),
    )
    extra_args: List[str] = Field(
        default_factory=list,
        description="Optional extra forge script CLI arguments.",
    )
    deployment_manifest: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Optional authoritative deployment manifest used to parse multi-contract "
            "broadcast output."
        ),
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

    @model_validator(mode="before")
    @classmethod
    def _normalize_fuji_request(cls, value: Any) -> Any:
        return _coerce_fuji_target_payload(value)


class FoundryDeployResult(BaseModel):
    success: bool
    exit_code: int
    command: str
    script_path: str
    network: str
    chain_id: int
    pipeline_run_id: Optional[str] = None
    pipeline_task_id: Optional[str] = None
    tx_hash: Optional[str] = None
    deployed_address: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    stdout_path: Optional[str] = None
    stderr_path: Optional[str] = None
    deployed_contracts: List[DeployedContractResult] = Field(default_factory=list)
    executed_calls: List[ExecutedCallResult] = Field(default_factory=list)


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
