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
        """Saving the current smart contract plan to Letta memory."""
        plan_json = json.dumps(plan, indent=2)
        self.client.agents.blocks.update(
            "current_plan", agent_id=self.agent.id, value=plan_json
        )
        print("Plan saved to Letta memory!")

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


if __name__ == "__main__":
    mm = MemoryManager()
    print(f"Memory manager ready! Agent ID: {mm.agent.id}")
