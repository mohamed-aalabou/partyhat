import uuid
from types import SimpleNamespace

from agents.memory_manager import MemoryManager


class FakeBlocks:
    def __init__(self):
        self._by_id = {}
        self._by_label = {}

    def list(self):
        return list(self._by_id.values())

    def create(self, label, value, limit):
        block_id = str(uuid.uuid4())
        block = SimpleNamespace(id=block_id, label=label, value=value, limit=limit)
        self._by_id[block_id] = block
        self._by_label[label] = block
        return block

    def get(self, block_id):
        return self._by_id[block_id]

    def update(self, block_id, value):
        self._by_id[block_id].value = value
        return self._by_id[block_id]


class FakeLettaClient:
    def __init__(self):
        self.blocks = FakeBlocks()


def test_save_plan_keeps_compact_letta_state_and_full_neon_plan(monkeypatch):
    fake_client = FakeLettaClient()
    monkeypatch.setattr("agents.memory_manager._get_letta_client", lambda api_key: fake_client)

    project_id = str(uuid.uuid4())
    mm = MemoryManager(user_id="user-123", project_id=project_id)
    monkeypatch.setattr(mm, "_db_available", True)

    saved_plan_row = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(mm, "_db_call", lambda coro_factory: saved_plan_row)

    plan = {
        "project_name": "PartyToken",
        "status": "ready",
        "description": "A token contract.",
        "contracts": [
            {
                "name": "PartyToken",
                "description": "Main token",
                "erc_template": "ERC-20",
                "dependencies": ["Ownable"],
                "constructor": {"inputs": [], "description": "Default"},
                "functions": [{"name": "mint"}],
            }
        ],
    }

    mm.save_plan(plan)
    data, _ = mm._read_user_block()
    planning = data["agents"]["planning"]

    assert planning["plan_id"] == str(saved_plan_row.id)
    assert planning["plan_status"] == "ready"
    assert planning["plan_summary"]["project_name"] == "PartyToken"
    assert planning["plan_summary"]["contract_names"] == ["PartyToken"]
    assert planning["current_plan"] is None

    monkeypatch.setattr(
        mm,
        "_db_call",
        lambda coro_factory: SimpleNamespace(plan_data=plan),
    )
    assert mm.get_plan() == plan
