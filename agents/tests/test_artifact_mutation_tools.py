from pathlib import Path

from agents import artifact_mutation_tools
from agents.code_storage import LocalCodeStorage
from schemas.coding_schema import CodeArtifact


class FakeMemoryManager:
    def __init__(self):
        self.states = {
            "coding": {
                "artifact_count": 2,
                "last_artifact_path": "contracts/Bar.sol",
                "artifacts": [
                    {
                        "path": "contracts/Foo.sol",
                        "plan_contract_ids": ["pc_foo"],
                    },
                    {
                        "path": "contracts/Bar.sol",
                        "plan_contract_ids": ["pc_bar"],
                    },
                ],
            },
            "testing": {
                "artifacts": [
                    {
                        "path": "test/FooTest.t.sol",
                        "plan_contract_ids": ["pc_foo"],
                    }
                ]
            },
        }
        self.logged_actions = []

    def get_agent_state(self, agent_name: str):
        return self.states[agent_name]

    def set_agent_state(self, agent_name: str, state: dict):
        self.states[agent_name] = state

    def log_agent_action(self, **kwargs):
        self.logged_actions.append(kwargs)


class FakeStorage:
    def __init__(self):
        self.files = {
            "contracts/Foo.sol": "contract Foo {}",
            "contracts/Bar.sol": "contract Bar {}",
            "test/FooTest.t.sol": "contract FooTest {}",
        }

    def edit_code(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> int:
        current = self.files[path]
        occurrences = current.count(old_string)
        if occurrences == 0:
            raise ValueError(f"String '{old_string}' not found in file.")
        if occurrences > 1 and not replace_all:
            raise ValueError(
                f"String '{old_string}' appears {occurrences} times in file. "
                "Specify replace_all=True to replace all instances."
            )
        self.files[path] = current.replace(old_string, new_string)
        return occurrences if replace_all else 1

    def delete_code(self, path: str) -> None:
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]


def test_local_code_storage_can_edit_and_delete_files(tmp_path):
    storage = LocalCodeStorage(base_dir=tmp_path / "generated_contracts")
    artifact = CodeArtifact(path="contracts/Foo.sol", language="solidity")

    storage.save_code(artifact, "contract Foo {}\n")
    occurrences = storage.edit_code("contracts/Foo.sol", "Foo", "Bar")

    assert occurrences == 1
    assert storage.load_code("contracts/Foo.sol") == "contract Bar {}\n"

    storage.delete_code("contracts/Foo.sol")

    assert storage.list_paths() == []
    assert not Path(tmp_path / "generated_contracts" / "contracts" / "Foo.sol").exists()


def test_edit_code_artifact_updates_existing_file_without_new_metadata(monkeypatch):
    fake_mm = FakeMemoryManager()
    fake_storage = FakeStorage()
    monkeypatch.setattr(artifact_mutation_tools, "_get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(artifact_mutation_tools, "get_code_storage", lambda: fake_storage)

    result = artifact_mutation_tools.edit_code_artifact.func(
        path="contracts/Foo.sol",
        old_string="Foo",
        new_string="Baz",
    )

    assert result["success"] is True
    assert result["artifact_path"] == "contracts/Foo.sol"
    assert fake_storage.files["contracts/Foo.sol"] == "contract Baz {}"
    assert len(fake_mm.states["coding"]["artifacts"]) == 2


def test_delete_code_artifact_removes_file_and_updates_coding_metadata(monkeypatch):
    fake_mm = FakeMemoryManager()
    fake_storage = FakeStorage()
    monkeypatch.setattr(artifact_mutation_tools, "_get_memory_manager", lambda: fake_mm)
    monkeypatch.setattr(artifact_mutation_tools, "get_code_storage", lambda: fake_storage)

    result = artifact_mutation_tools.delete_code_artifact.func("contracts/Foo.sol")

    assert result == {
        "success": True,
        "artifact_path": "contracts/Foo.sol",
        "file_deleted": True,
        "metadata_entries_removed": 1,
    }
    assert "contracts/Foo.sol" not in fake_storage.files
    assert fake_mm.states["coding"]["artifact_count"] == 1
    assert fake_mm.states["coding"]["last_artifact_path"] == "contracts/Bar.sol"
    assert fake_mm.states["coding"]["artifacts"] == [
        {
            "path": "contracts/Bar.sol",
            "plan_contract_ids": ["pc_bar"],
        }
    ]


def test_get_current_test_artifacts_returns_testing_slice(monkeypatch):
    fake_mm = FakeMemoryManager()
    monkeypatch.setattr(artifact_mutation_tools, "_get_memory_manager", lambda: fake_mm)

    result = artifact_mutation_tools.get_current_test_artifacts.func()

    assert result == {
        "artifacts": [
            {
                "path": "test/FooTest.t.sol",
                "plan_contract_ids": ["pc_foo"],
            }
        ]
    }
