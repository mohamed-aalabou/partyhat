from __future__ import annotations

import shlex
from functools import lru_cache

import modal


@lru_cache(maxsize=32)
def get_modal_app(app_name: str):
    return modal.App.lookup(app_name, create_if_missing=True)


@lru_cache(maxsize=128)
def get_modal_volume(volume_name: str):
    return modal.Volume.from_name(volume_name, create_if_missing=True)


def build_project_volume_name(base_name: str, project_id: str | None) -> str:
    return f"{base_name}-{project_id}" if project_id else base_name


def default_foundry_remappings() -> list[str]:
    return [
        "--remappings",
        "@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/",
        "--remappings",
        "@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/",
        "--remappings",
        "forge-std/=lib/forge-std/src/",
        "--remappings",
        "@chainlink/contracts/=lib/chainlink-evm/contracts/",
        "--remappings",
        "@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/",
    ]


def build_foundry_bootstrap_cmd(root: str, command: str) -> str:
    quoted_root = shlex.quote(root)
    return (
        "set -e; "
        + f"cd {quoted_root}; "
        + "mkdir -p lib; "
        + "if [ ! -d lib/forge-std ]; then "
        + "  if [ -d /opt/foundry-deps/forge-std ]; then cp -R /opt/foundry-deps/forge-std lib/forge-std; "
        + "  else git clone --depth 1 https://github.com/foundry-rs/forge-std lib/forge-std; fi; "
        + "fi; "
        + "if [ ! -d lib/openzeppelin-contracts ]; then "
        + "  if [ -d /opt/foundry-deps/openzeppelin-contracts ]; then cp -R /opt/foundry-deps/openzeppelin-contracts lib/openzeppelin-contracts; "
        + "  else git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts lib/openzeppelin-contracts; fi; "
        + "fi; "
        + "if [ ! -d lib/openzeppelin-contracts-upgradeable ]; then "
        + "  if [ -d /opt/foundry-deps/openzeppelin-contracts-upgradeable ]; then cp -R /opt/foundry-deps/openzeppelin-contracts-upgradeable lib/openzeppelin-contracts-upgradeable; "
        + "  else git clone --depth 1 https://github.com/OpenZeppelin/openzeppelin-contracts-upgradeable lib/openzeppelin-contracts-upgradeable; fi; "
        + "fi; "
        + "if [ ! -d lib/chainlink-evm ]; then "
        + "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
        + "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
        + "fi; "
        + "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
        + "  rm -rf lib/chainlink-evm; "
        + "  if [ -d /opt/foundry-deps/chainlink-evm ]; then cp -R /opt/foundry-deps/chainlink-evm lib/chainlink-evm; "
        + "  else git clone --depth 1 https://github.com/smartcontractkit/chainlink-evm lib/chainlink-evm; fi; "
        + "fi; "
        + "mkdir -p lib/chainlink-evm/contracts/src/v0.8/interfaces; "
        + "if [ ! -f lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol ] "
        + "&& [ -f lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol ]; then "
        + "  cp lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/AggregatorV3Interface.sol "
        + "lib/chainlink-evm/contracts/src/v0.8/interfaces/AggregatorV3Interface.sol; "
        + "fi; "
        + "touch remappings.txt; "
        + "grep -qxF '@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/' remappings.txt "
        + "|| echo '@openzeppelin/contracts/=lib/openzeppelin-contracts/contracts/' >> remappings.txt; "
        + "grep -qxF '@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/' remappings.txt "
        + "|| echo '@openzeppelin/contracts-upgradeable/=lib/openzeppelin-contracts-upgradeable/contracts/' >> remappings.txt; "
        + "grep -qxF 'forge-std/=lib/forge-std/src/' remappings.txt "
        + "|| echo 'forge-std/=lib/forge-std/src/' >> remappings.txt; "
        + "grep -qxF '@chainlink/contracts/=lib/chainlink-evm/contracts/' remappings.txt "
        + "|| echo '@chainlink/contracts/=lib/chainlink-evm/contracts/' >> remappings.txt; "
        + "grep -qxF '@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/' remappings.txt "
        + "|| echo '@chainlink/contracts/src/v0.8/interfaces/=lib/chainlink-evm/contracts/src/v0.8/shared/interfaces/' >> remappings.txt; "
        + command
    )
