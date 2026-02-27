import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from letta_client import Letta
from schemas.plan_schema import SmartContractPlan

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
)


class MemoryManager:
    def __init__(self):
        self.client = Letta(api_key=os.getenv("LETTA_API_KEY"))
        self.agent_name = "partyhat-planning-agent"
        self.agent = self._get_or_create_agent()

    def _get_or_create_agent(self):
        existing = self.client.agents.list()
        for agent in existing:
            if agent.name == self.agent_name:
                print(f"Found existing Letta agent: {self.agent_name}")
                return agent

        # Creating a new agent with memory blocks
        print(f"Creating new Letta agent: {self.agent_name}")
        agent = self.client.agents.create(
            name=self.agent_name,
            memory_blocks=[
                {"label": "current_plan", "value": "No plan yet."},
                {"label": "user_context", "value": "No user context yet."},
            ],
            model="openai/gpt-4o",
            embedding="openai/text-embedding-ada-002",
        )
        return agent

    def save_plan(self, plan: dict):
        """Save plan to both agent memory and global shared memory."""
        plan_json = json.dumps(plan, indent=2)

        self.client.agents.blocks.update(
            "current_plan", agent_id=self.agent.id, value=plan_json
        )
        print("Plan saved to agent memory!")

        self.save_to_global_memory(plan)

    def get_plan(self) -> dict | None:
        """Retrieving the current plan from Letta memory."""
        blocks = self.client.agents.blocks.list(agent_id=self.agent.id)
        for block in blocks:
            if block.label == "current_plan" and block.value != "No plan yet.":
                return json.loads(block.value)
        return None

    def update_user_context(self, context: str):
        """Saving the user context to memory."""
        self.client.agents.core_memory.update(
            agent_id=self.agent.id, label="user_context", value=context
        )

    def _get_or_create_global_block(self):
        """Getting or create from start a shared block accessible by all agents."""
        # Checking here if the global block already exists
        existing_blocks = self.client.blocks.list()
        for block in existing_blocks:
            if block.label == "global_contract_plan":
                print(f"Found existing global block: global_contract_plan")
                return block

        # Creating new one
        print("Creating global shared block: global_contract_plan")
        block = self.client.blocks.create(
            label="global_contract_plan", value="No plan yet.", limit=50000
        )
        return block

    def save_to_global_memory(self, plan: dict):
        """Save plan to global memory, will be accessible by ALL agents."""
        plan_json = json.dumps(plan, indent=2)
        global_block = self._get_or_create_global_block()

        self.client.blocks.update(global_block.id, value=plan_json)
        print("Plan saved to global memory; All agents can now access it!")

    def get_from_global_memory(self) -> dict | None:
        """Read plan from global memory."""
        existing_blocks = self.client.blocks.list()
        for block in existing_blocks:
            if block.label == "global_contract_plan" and block.value != "No plan yet.":
                return json.loads(block.value)
        return None


if __name__ == "__main__":
    mm = MemoryManager()
    print(f"Memory manager ready! Agent ID: {mm.agent.id}")
