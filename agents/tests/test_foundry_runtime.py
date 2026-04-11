import asyncio
import uuid
from types import SimpleNamespace

from agents.db.crud import save_deployment as db_save_deployment
from agents.context import (
    clear_project_context,
    set_pipeline_run_id,
    set_pipeline_task_id,
    set_project_context,
)
from agents.deployment_tools import (
    _evaluate_deploy_success,
    _parse_broadcast_deploy_output,
    _parse_deploy_output,
    generate_foundry_deploy_script_direct,
    run_foundry_deploy,
)
from agents.pipeline_specs import default_deployment_target_payload
from agents.modal_runtime import (
    build_foundry_bootstrap_cmd,
    default_foundry_remappings,
)
from schemas.deployment_schema import (
    FoundryDeployRequest,
    FoundryDeployScriptGenerationRequest,
)


def test_default_foundry_remappings_include_openzeppelin_upgradeable():
    remappings = default_foundry_remappings()

    assert (
        "@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/"
        in remappings
    )


def test_bootstrap_configures_upgradeable_remapping_file():
    command = build_foundry_bootstrap_cmd("generated_contracts/project", "forge test")

    assert "lib/openzeppelin-contracts-upgradeable" in command
    assert (
        "@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/"
        in command
    )
    assert "touch remappings.txt" in command


def test_generate_foundry_deploy_script_quotes_manifest_string_defaults():
    manifest = {
        "deployment_target": default_deployment_target_payload(),
        "contracts": [
            {
                "plan_contract_id": "pc_helper",
                "name": "Helper",
                "role": "supporting",
                "deploy_order": 1,
                "source_path": "contracts/Helper.sol",
                "constructor_args_schema": [],
            },
            {
                "plan_contract_id": "pc_partcredits",
                "name": "PartCredits",
                "role": "primary_deployable",
                "deploy_order": 2,
                "source_path": "contracts/PartCredits.sol",
                "constructor_args_schema": [
                    {
                        "name": "treasury",
                        "type": "address",
                        "source": "deployer",
                        "default_value": "deployer",
                    },
                    {
                        "name": "name_",
                        "type": "string",
                        "source": "plan_default",
                        "default_value": "PartCredits",
                    },
                    {
                        "name": "symbol_",
                        "type": "string",
                        "source": "plan_default",
                        "default_value": "PART",
                    },
                    {
                        "name": "helper",
                        "type": "address",
                        "source": "plan_default",
                        "default_value": "<deployed:Helper.address>",
                    },
                    {
                        "name": "paused",
                        "type": "bool",
                        "source": "plan_default",
                        "default_value": "TRUE",
                    },
                ],
            },
        ],
        "post_deploy_calls": [],
    }

    result = generate_foundry_deploy_script_direct(
        FoundryDeployScriptGenerationRequest(
            goal="Deploy PartCredits.",
            contract_name="PartCredits",
            script_name="DeployPartCredits",
            deployment_manifest=manifest,
        )
    )

    assert result.get("error") is None
    assert (
        'PartCredits partCredits = new PartCredits(deployer, "PartCredits", "PART", address(helper), true);'
        in result["generated_script"]
    )


def test_run_foundry_deploy_records_tagged_preflight_failure(monkeypatch):
    class FakeMemoryManager:
        def __init__(self):
            self.client = SimpleNamespace(
                blocks=SimpleNamespace(update=lambda block_id, value: None)
            )
            self.data = {
                "agents": {
                    "deployment": {
                        "last_deploy_results": [],
                        "last_deploy_status": None,
                    }
                }
            }

        def _read_user_block(self):
            return self.data, SimpleNamespace(id="block-1")

        def _ensure_agents_structure(self, data):
            agents = data.setdefault("agents", {})
            deployment = agents.setdefault("deployment", {})
            deployment.setdefault("last_deploy_results", [])
            deployment.setdefault("last_deploy_status", None)

        def _serialize(self, data):
            return data

        def get_agent_state(self, agent_name: str):
            return self.data.setdefault("agents", {}).setdefault(agent_name, {})

        def set_agent_state(self, agent_name: str, state: dict):
            self.data.setdefault("agents", {})[agent_name] = state

        def save_deployment(self, **kwargs):
            return None

    fake_mm = FakeMemoryManager()

    monkeypatch.delenv("FUJI_RPC_URL", raising=False)
    monkeypatch.delenv("FUJI_PRIVATE_KEY", raising=False)
    monkeypatch.setattr("agents.deployment_tools._get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(
        "agents.deployment_tools.save_execution_logs",
        lambda **kwargs: ("logs/run/task/stdout.log", "logs/run/task/stderr.log"),
    )

    set_project_context("project-123", "user-123")
    set_pipeline_run_id("run-123")
    set_pipeline_task_id("task-123")
    try:
        result = run_foundry_deploy.func(
            FoundryDeployRequest(script_path="script/DeployPartyToken.s.sol")
        )
    finally:
        clear_project_context()

    assert result["success"] is False
    assert result["exit_code"] == 1
    assert result["pipeline_run_id"] == "run-123"
    assert result["pipeline_task_id"] == "task-123"
    assert result["stderr_path"] == "logs/run/task/stderr.log"
    assert "Missing required env var: FUJI_RPC_URL" in result["error"]

    history = fake_mm.data["agents"]["deployment"]["last_deploy_results"]
    assert len(history) == 1
    assert history[0]["pipeline_run_id"] == "run-123"
    assert history[0]["pipeline_task_id"] == "task-123"
    assert history[0]["exit_code"] == 1
    assert history[0]["stderr_path"] == "logs/run/task/stderr.log"
    assert fake_mm.data["agents"]["deployment"]["last_deploy_status"] == "failed"


def test_save_deployment_accepts_structured_metadata():
    class FakeSession:
        def __init__(self):
            self.added = None
            self.committed = False
            self.refreshed = None

        def add(self, obj):
            self.added = obj

        async def commit(self):
            self.committed = True

        async def refresh(self, obj):
            self.refreshed = obj

    session = FakeSession()
    deployed_contracts = [
        {
            "contract_name": "PartCredits",
            "plan_contract_id": "pc_partcredits",
            "tx_hash": "0x" + "a" * 64,
            "deployed_address": "0x" + "1" * 40,
        }
    ]
    executed_calls = [
        {
            "target_contract_name": "PartCredits",
            "function_name": "setTreasury",
            "args": ["0x" + "2" * 40],
            "status": "success",
        }
    ]

    deployment = asyncio.run(
        db_save_deployment(
            session,
            uuid.uuid4(),
            status="failed",
            contract_name="PartCredits",
            pipeline_run_id=uuid.uuid4(),
            pipeline_task_id=uuid.uuid4(),
            deployed_contracts=deployed_contracts,
            executed_calls=executed_calls,
        )
    )

    assert session.added is deployment
    assert session.committed is True
    assert session.refreshed is deployment
    assert deployment.deployed_contracts == deployed_contracts
    assert deployment.executed_calls == executed_calls


def test_parse_broadcast_deploy_output_uses_receipt_address_for_matching_contract():
    broadcast = """
    {
      "transactions": [
        {
          "hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "transactionType": "CREATE",
          "contractName": "Helper",
          "contractAddress": "0x1111111111111111111111111111111111111111"
        },
        {
          "hash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "transactionType": "CREATE",
          "contractName": "PipPip",
          "contractAddress": "0x2222222222222222222222222222222222222222"
        }
      ],
      "receipts": [
        {
          "transactionHash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "contractAddress": "0x3333333333333333333333333333333333333333"
        }
      ],
      "returns": {
        "pipPip": {
          "internal_type": "contract PipPip",
          "type": "address",
          "value": "0x3333333333333333333333333333333333333333"
        }
      }
    }
    """

    parsed = _parse_broadcast_deploy_output(broadcast, contract_name="PipPip")

    assert (
        parsed["tx_hash"]
        == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    assert parsed["deployed_address"] == "0x3333333333333333333333333333333333333333"


def test_parse_broadcast_deploy_output_falls_back_to_last_create_without_contract_name():
    broadcast = """
    {
      "transactions": [
        {
          "hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "transactionType": "CALL"
        },
        {
          "hash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "transactionType": "CREATE",
          "contractAddress": "0x4444444444444444444444444444444444444444"
        }
      ]
    }
    """

    parsed = _parse_broadcast_deploy_output(broadcast, contract_name=None)

    assert (
        parsed["tx_hash"]
        == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    )
    assert parsed["deployed_address"] == "0x4444444444444444444444444444444444444444"


def test_parse_broadcast_deploy_output_collects_all_manifest_contracts_and_calls():
    manifest = {
        "deployment_target": default_deployment_target_payload(),
        "contracts": [
            {
                "plan_contract_id": "pc_vesting",
                "name": "AvaVestVesting",
                "role": "primary_deployable",
                "deploy_order": 1,
                "source_path": "contracts/AvaVestVesting.sol",
                "constructor_args_schema": [],
            },
            {
                "plan_contract_id": "pc_token",
                "name": "AvaVestToken",
                "role": "supporting",
                "deploy_order": 2,
                "source_path": "contracts/AvaVestToken.sol",
                "constructor_args_schema": [
                    {
                        "name": "vesting",
                        "type": "address",
                        "source": "plan_default",
                        "default_value": "<deployed:AvaVestVesting.address>",
                    }
                ],
            },
        ],
        "post_deploy_calls": [
            {
                "target_contract_name": "AvaVestVesting",
                "target_plan_contract_id": "pc_vesting",
                "function_name": "setToken",
                "args": ["<deployed:AvaVestToken.address>"],
                "call_order": 1,
                "description": "Wire token",
            }
        ],
    }
    broadcast = """
    {
      "transactions": [
        {
          "hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "transactionType": "CREATE",
          "contractName": "AvaVestVesting",
          "contractAddress": "0x1111111111111111111111111111111111111111"
        },
        {
          "hash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "transactionType": "CREATE",
          "contractName": "AvaVestToken",
          "contractAddress": "0x2222222222222222222222222222222222222222"
        },
        {
          "hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
          "transactionType": "CALL"
        }
      ],
      "receipts": [
        {
          "transactionHash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "contractAddress": "0x1111111111111111111111111111111111111111"
        },
        {
          "transactionHash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "contractAddress": "0x2222222222222222222222222222222222222222"
        }
      ]
    }
    """

    parsed = _parse_broadcast_deploy_output(
        broadcast,
        contract_name=None,
        deployment_manifest=manifest,
    )

    assert (
        parsed["tx_hash"]
        == "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert parsed["deployed_address"] == "0x1111111111111111111111111111111111111111"
    assert parsed["deployed_contracts"] == [
        {
            "contract_name": "AvaVestVesting",
            "plan_contract_id": "pc_vesting",
            "deploy_order": 1,
            "tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "deployed_address": "0x1111111111111111111111111111111111111111",
        },
        {
            "contract_name": "AvaVestToken",
            "plan_contract_id": "pc_token",
            "deploy_order": 2,
            "tx_hash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "deployed_address": "0x2222222222222222222222222222222222222222",
        },
    ]
    assert parsed["executed_calls"] == [
        {
            "target_contract_name": "AvaVestVesting",
            "target_plan_contract_id": "pc_vesting",
            "function_name": "setToken",
            "args": ["<deployed:AvaVestToken.address>"],
            "call_order": 1,
            "tx_hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "status": "success",
        }
    ]


def test_parse_deploy_output_ignores_revert_data_and_unlabelled_addresses():
    parsed = _parse_deploy_output(
        "",
        "Error: script failed: OwnableInvalidOwner(0x0000000000000000000000000000000000000000)\n0x1e4fbdf700000000000000000000000000000000000000000000000000000000",
    )

    assert parsed["tx_hash"] is None
    assert parsed["deployed_address"] is None


def test_evaluate_deploy_success_accepts_broadcast_tx_hash(monkeypatch):
    monkeypatch.setattr(
        "agents.deployment_tools._contract_has_code",
        lambda rpc_url, contract_address: (_ for _ in ()).throw(
            AssertionError("RPC confirmation should not run when tx hash is present")
        ),
    )

    success, error = _evaluate_deploy_success(
        exit_code=0,
        tx_hash="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        deployed_address=None,
        rpc_url="https://rpc.example",
    )

    assert success is True
    assert error is None


def test_evaluate_deploy_success_accepts_confirmed_contract_address(monkeypatch):
    monkeypatch.setattr(
        "agents.deployment_tools._contract_has_code",
        lambda rpc_url, contract_address: contract_address
        == "0x4444444444444444444444444444444444444444",
    )

    success, error = _evaluate_deploy_success(
        exit_code=0,
        tx_hash=None,
        deployed_address="0x4444444444444444444444444444444444444444",
        rpc_url="https://rpc.example",
    )

    assert success is True
    assert error is None


def test_evaluate_deploy_success_rejects_exit_zero_without_tx_hash_or_confirmed_code(
    monkeypatch,
):
    monkeypatch.setattr(
        "agents.deployment_tools._contract_has_code",
        lambda rpc_url, contract_address: False,
    )

    success, error = _evaluate_deploy_success(
        exit_code=0,
        tx_hash=None,
        deployed_address="0x5555555555555555555555555555555555555555",
        rpc_url="https://rpc.example",
    )

    assert success is False
    assert "could not be confirmed" in error


def test_run_foundry_deploy_marks_exit_zero_without_deploy_metadata_as_failed(monkeypatch):
    class FakeMemoryManager:
        def __init__(self):
            self.client = SimpleNamespace(
                blocks=SimpleNamespace(update=lambda block_id, value: None)
            )
            self.data = {
                "agents": {
                    "deployment": {
                        "last_deploy_results": [],
                        "last_deploy_status": None,
                    }
                }
            }
            self.saved = []
            self.logged = []

        def _read_user_block(self):
            return self.data, SimpleNamespace(id="block-1")

        def _ensure_agents_structure(self, data):
            agents = data.setdefault("agents", {})
            deployment = agents.setdefault("deployment", {})
            deployment.setdefault("last_deploy_results", [])
            deployment.setdefault("last_deploy_status", None)

        def _serialize(self, data):
            return data

        def get_agent_state(self, agent_name: str):
            return self.data.setdefault("agents", {}).setdefault(agent_name, {})

        def set_agent_state(self, agent_name: str, state: dict):
            self.data.setdefault("agents", {})[agent_name] = state

        def save_deployment(self, **kwargs):
            self.saved.append(kwargs)

        def log_agent_action(self, **kwargs):
            self.logged.append(kwargs)

    class FakeStream:
        def __init__(self, text: str):
            self._text = text

        def read(self):
            return self._text

    class FakeSandbox:
        def __init__(self):
            self.stdout = FakeStream("Compiling 1 files with Solc 0.8.33\n")
            self.stderr = FakeStream("")
            self.returncode = 0

        def wait(self, raise_on_termination=False):
            return None

    class FakeVolume:
        def reload(self):
            return None

        def read_file(self, path):
            raise FileNotFoundError("No such file or directory")

    fake_mm = FakeMemoryManager()

    monkeypatch.setenv("FUJI_RPC_URL", "https://rpc.example")
    monkeypatch.setenv(
        "FUJI_PRIVATE_KEY",
        "1111111111111111111111111111111111111111111111111111111111111111",
    )
    monkeypatch.setattr("agents.deployment_tools._get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(
        "agents.deployment_tools.save_execution_logs",
        lambda **kwargs: ("logs/run/task/stdout.log", "logs/run/task/stderr.log"),
    )
    monkeypatch.setattr("agents.deployment_tools.get_modal_app", lambda app_name: object())
    monkeypatch.setattr("agents.deployment_tools.get_modal_volume", lambda volume_name: FakeVolume())
    monkeypatch.setattr(
        "agents.deployment_tools.modal.Sandbox.create",
        lambda *args, **kwargs: FakeSandbox(),
    )
    monkeypatch.setattr(
        "agents.deployment_tools._contract_has_code",
        lambda rpc_url, contract_address: False,
    )

    set_project_context("project-123", "user-123")
    set_pipeline_run_id("run-123")
    set_pipeline_task_id("task-456")
    try:
        result = run_foundry_deploy.func(
            FoundryDeployRequest(script_path="script/DeployPartyToken.s.sol")
        )
    finally:
        clear_project_context()

    assert result["success"] is False
    assert result["exit_code"] == 0
    assert "produced no deployment transaction hash or confirmed contract address" in result["error"]
    assert fake_mm.data["agents"]["deployment"]["last_deploy_status"] == "failed"
    assert fake_mm.saved[-1]["status"] == "failed"


def test_run_foundry_deploy_surfaces_authoritative_record_persistence_failure(monkeypatch):
    class FakeMemoryManager:
        def __init__(self):
            self.client = SimpleNamespace(
                blocks=SimpleNamespace(update=lambda block_id, value: None)
            )
            self.data = {
                "agents": {
                    "deployment": {
                        "last_deploy_results": [],
                        "last_deploy_status": None,
                    }
                }
            }
            self.logged = []

        def _read_user_block(self):
            return self.data, SimpleNamespace(id="block-1")

        def _ensure_agents_structure(self, data):
            agents = data.setdefault("agents", {})
            deployment = agents.setdefault("deployment", {})
            deployment.setdefault("last_deploy_results", [])
            deployment.setdefault("last_deploy_status", None)

        def _serialize(self, data):
            return data

        def get_agent_state(self, agent_name: str):
            return self.data.setdefault("agents", {}).setdefault(agent_name, {})

        def set_agent_state(self, agent_name: str, state: dict):
            self.data.setdefault("agents", {})[agent_name] = state

        def save_deployment(self, **kwargs):
            raise RuntimeError("db write failed")

        def log_agent_action(self, **kwargs):
            self.logged.append(kwargs)

    class FakeStream:
        def __init__(self, text: str):
            self._text = text

        def read(self):
            return self._text

    class FakeSandbox:
        def __init__(self):
            self.stdout = FakeStream("Broadcasting script...\n")
            self.stderr = FakeStream("")
            self.returncode = 0

        def wait(self, raise_on_termination=False):
            return None

    class FakeVolume:
        def reload(self):
            return None

    fake_mm = FakeMemoryManager()

    monkeypatch.setenv("FUJI_RPC_URL", "https://rpc.example")
    monkeypatch.setenv(
        "FUJI_PRIVATE_KEY",
        "1111111111111111111111111111111111111111111111111111111111111111",
    )
    monkeypatch.setattr("agents.deployment_tools._get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(
        "agents.deployment_tools.save_execution_logs",
        lambda **kwargs: ("logs/run/task/stdout.log", "logs/run/task/stderr.log"),
    )
    monkeypatch.setattr("agents.deployment_tools.get_modal_app", lambda app_name: object())
    monkeypatch.setattr(
        "agents.deployment_tools.get_modal_volume", lambda volume_name: FakeVolume()
    )
    monkeypatch.setattr(
        "agents.deployment_tools.modal.Sandbox.create",
        lambda *args, **kwargs: FakeSandbox(),
    )
    monkeypatch.setattr(
        "agents.deployment_tools._extract_deploy_metadata",
        lambda **kwargs: {
            "tx_hash": "0x" + "a" * 64,
            "deployed_address": "0x" + "1" * 40,
            "deployed_contracts": [
                {
                    "contract_name": "PartCredits",
                    "plan_contract_id": "pc_partcredits",
                    "tx_hash": "0x" + "a" * 64,
                    "deployed_address": "0x" + "1" * 40,
                }
            ],
            "executed_calls": [],
        },
    )

    set_project_context("project-123", "user-123")
    set_pipeline_run_id("run-123")
    set_pipeline_task_id("task-456")
    try:
        result = run_foundry_deploy.func(
            FoundryDeployRequest(script_path="script/DeployPartCredits.s.sol")
        )
    finally:
        clear_project_context()

    assert result["success"] is False
    assert (
        result["error"]
        == "Deployment executed but authoritative deployment record could not be persisted: db write failed"
    )
    assert (
        result["authoritative_record_error"]
        == "Authoritative deployment record could not be persisted: db write failed"
    )
    assert fake_mm.logged[-1]["error"] == result["error"]


def test_run_foundry_deploy_persists_structured_deploy_metadata(monkeypatch):
    class FakeMemoryManager:
        def __init__(self):
            self.client = SimpleNamespace(
                blocks=SimpleNamespace(update=lambda block_id, value: None)
            )
            self.data = {
                "agents": {
                    "deployment": {
                        "last_deploy_results": [],
                        "last_deploy_status": None,
                    }
                }
            }
            self.saved = []
            self.logged = []

        def _read_user_block(self):
            return self.data, SimpleNamespace(id="block-1")

        def _ensure_agents_structure(self, data):
            agents = data.setdefault("agents", {})
            deployment = agents.setdefault("deployment", {})
            deployment.setdefault("last_deploy_results", [])
            deployment.setdefault("last_deploy_status", None)

        def _serialize(self, data):
            return data

        def get_agent_state(self, agent_name: str):
            return self.data.setdefault("agents", {}).setdefault(agent_name, {})

        def set_agent_state(self, agent_name: str, state: dict):
            self.data.setdefault("agents", {})[agent_name] = state

        def save_deployment(self, **kwargs):
            self.saved.append(kwargs)

        def log_agent_action(self, **kwargs):
            self.logged.append(kwargs)

    class FakeStream:
        def __init__(self, text: str):
            self._text = text

        def read(self):
            return self._text

    class FakeSandbox:
        def __init__(self):
            self.stdout = FakeStream("Broadcasting script...\n")
            self.stderr = FakeStream("")
            self.returncode = 0

        def wait(self, raise_on_termination=False):
            return None

    class FakeVolume:
        def reload(self):
            return None

    fake_mm = FakeMemoryManager()
    deployed_contracts = [
        {
            "contract_name": "PartCredits",
            "plan_contract_id": "pc_partcredits",
            "tx_hash": "0x" + "a" * 64,
            "deployed_address": "0x" + "1" * 40,
        }
    ]
    executed_calls = [
        {
            "target_contract_name": "PartCredits",
            "function_name": "setTreasury",
            "args": ["0x" + "2" * 40],
            "status": "success",
        }
    ]

    monkeypatch.setenv("FUJI_RPC_URL", "https://rpc.example")
    monkeypatch.setenv(
        "FUJI_PRIVATE_KEY",
        "1111111111111111111111111111111111111111111111111111111111111111",
    )
    monkeypatch.setattr("agents.deployment_tools._get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(
        "agents.deployment_tools.save_execution_logs",
        lambda **kwargs: ("logs/run/task/stdout.log", "logs/run/task/stderr.log"),
    )
    monkeypatch.setattr("agents.deployment_tools.get_modal_app", lambda app_name: object())
    monkeypatch.setattr(
        "agents.deployment_tools.get_modal_volume", lambda volume_name: FakeVolume()
    )
    monkeypatch.setattr(
        "agents.deployment_tools.modal.Sandbox.create",
        lambda *args, **kwargs: FakeSandbox(),
    )
    monkeypatch.setattr(
        "agents.deployment_tools._extract_deploy_metadata",
        lambda **kwargs: {
            "tx_hash": "0x" + "a" * 64,
            "deployed_address": "0x" + "1" * 40,
            "deployed_contracts": deployed_contracts,
            "executed_calls": executed_calls,
        },
    )

    set_project_context("project-123", "user-123")
    set_pipeline_run_id("run-123")
    set_pipeline_task_id("task-789")
    try:
        result = run_foundry_deploy.func(
            FoundryDeployRequest(script_path="script/DeployPartCredits.s.sol")
        )
    finally:
        clear_project_context()

    assert result["success"] is True
    assert fake_mm.saved[-1]["deployed_contracts"] == deployed_contracts
    assert fake_mm.saved[-1]["executed_calls"] == executed_calls
