import os
import sys
import json
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from letta_client import Letta

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
)


class MemoryManager:
    def __init__(self, user_id: str = "default"):
        """
        Args:
            user_id: The authenticated user's ID from the backend auth layer.
                     Defaults to "default" for local testing only.
        """
        self.client = Letta(api_key=os.getenv("LETTA_API_KEY"))
        self.user_id = user_id
        self.user_block_label = f"user:{user_id}"
        self.global_block_label = "global_agent_log"

    def _get_or_create_user_block(self):
        """Get or create the user-scoped memory block."""
        existing = self.client.blocks.list()
        for block in existing:
            if block.label == self.user_block_label:
                return block

        print(f"Creating user memory block: {self.user_block_label}")
        return self.client.blocks.create(
            label=self.user_block_label,
            value=json.dumps(
                {
                    # Who the user is — for the AI to personalise interactions
                    "user_id": self.user_id,
                    "profile": {
                        "name": None,
                        "experience_level": None,  # "beginner", "intermediate", "expert"
                        "preferred_language": None,
                    },
                    # AI-relevant preferences that will be learned over time
                    "preferences": {
                        "preferred_erc": None,
                        "preferred_license": None,
                        "preferred_chain": None,
                    },
                    # User's current active plan
                    "current_plan": None,
                    # History of all previous plan versions
                    "plan_history": [],
                    # Some reasoning notes i.e WHY decisions were made across all sessions
                    "reasoning_notes": [],
                    # Session history i.e summary of each past session
                    "sessions": [],
                }
            ),
            limit=50000,
        )

    def _get_or_create_global_block(self):
        """
        The global agent log block owned by the top-level graph. Every agent in the system
        logs everything it received, produced, and decided here.
        """
        existing = self.client.blocks.list()
        for block in existing:
            if block.label == self.global_block_label:
                return block

        print(f"Creating global memory block: {self.global_block_label}")
        return self.client.blocks.create(
            label=self.global_block_label,
            value=json.dumps({"agent_log": []}),
            limit=100000,
        )

    def _read_user_block(self):
        """Read and parse the user block. Returns (data dict, block object)."""
        block = self._get_or_create_user_block()
        return json.loads(block.value), block

    def _read_global_block(self):
        """Read and parse the global block. Returns (data dict, block object)."""
        block = self._get_or_create_global_block()
        return json.loads(block.value), block

    def save_plan(self, plan: dict) -> None:
        """
        Save the current smart contract plan to user memory.
        Archives the previous plan to plan_history before overwriting
        so we never lose a version.
        Args:
            plan: The SmartContractPlan as a dict (from model.model_dump())
        """
        data, block = self._read_user_block()

        # Archiving the previous plan before overwriting
        if data["current_plan"] is not None:
            data["plan_history"].append(
                {
                    "archived_at": datetime.utcnow().isoformat(),
                    "plan": data["current_plan"],
                }
            )

        data["current_plan"] = plan
        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_plan(self) -> dict | None:
        """Retrieve the current plan from user memory."""
        data, _ = self._read_user_block()
        return data.get("current_plan")

    def get_plan_history(self) -> list:
        """Retrieve all previous versions of the plan."""
        data, _ = self._read_user_block()
        return data.get("plan_history", [])

    def save_user_profile(
        self,
        name: str = None,
        experience_level: str = None,
        preferred_language: str = None,
    ) -> None:

        data, block = self._read_user_block()

        if name is not None:
            data["profile"]["name"] = name
        if experience_level is not None:
            data["profile"]["experience_level"] = experience_level
        if preferred_language is not None:
            data["profile"]["preferred_language"] = preferred_language

        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_user_profile(self) -> dict:
        data, _ = self._read_user_block()
        return data.get("profile", {})

    def save_user_preference(self, key: str, value: str) -> None:

        data, block = self._read_user_block()
        data["preferences"][key] = value
        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_user_preferences(self) -> dict:
        data, _ = self._read_user_block()
        return data.get("preferences", {})

    def save_reasoning_note(self, note: str) -> None:
        """
        Captures WHY decisions were made and not just what the plan contains.
        This is the episodic memory layer.

        Args:
            note: Plain English explanation of a decision or preference
        """
        data, block = self._read_user_block()
        data["reasoning_notes"].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "note": note,
            }
        )
        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_reasoning_notes(self) -> list:
        data, _ = self._read_user_block()
        return data.get("reasoning_notes", [])

    def save_session_summary(self, summary: str) -> None:

        data, block = self._read_user_block()
        data["sessions"].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "summary": summary,
            }
        )
        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_session_history(self) -> list:
        data, _ = self._read_user_block()
        return data.get("sessions", [])

    def log_agent_action(
        self,
        agent_name: str,
        action: str,
        input_received: dict | str | None = None,
        output_produced: dict | str | None = None,
        decisions_made: list | None = None,
        why: str | None = None,
        how: str | None = None,
        error: str | None = None,
    ) -> None:
        """
        Every agent calls this when it completes ANY significant action.
        This is the full audit trail: input received, output produced,
        decisions made, why, and how.
        Agents read this to understand what happened upstream.

        Args:
            agent_name:       Which agent is logging (e.g. "planning_agent")
            action:           Short action name (e.g. "plan_published")
            input_received:   Everything the agent received as input
            output_produced:  Everything the agent produced as output
            decisions_made:   List of decisions made during this action
            why:              Why this action was taken
            how:              How it was executed (tools, approach, MCP used)
            error:            If something went wrong, what happened
        """
        data, block = self._read_global_block()

        data["agent_log"].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "user_id": self.user_id,
                "agent": agent_name,
                "action": action,
                "input_received": input_received,
                "output_produced": output_produced,
                "decisions_made": decisions_made or [],
                "why": why,
                "how": how,
                "error": error,
            }
        )

        self.client.blocks.update(block.id, value=json.dumps(data, indent=2))

    def get_global_log(self) -> list:
        data, _ = self._read_global_block()
        return data.get("agent_log", [])

    def get_global_log_for_user(self) -> list:
        return [
            entry
            for entry in self.get_global_log()
            if entry.get("user_id") == self.user_id
        ]

    def get_global_log_for_agent(self, agent_name: str) -> list:
        return [
            entry for entry in self.get_global_log() if entry.get("agent") == agent_name
        ]


if __name__ == "__main__":
    mm = MemoryManager(user_id="test-user-123")
    print("Memory manager ready!")
    print(f"User block label:   {mm.user_block_label}")
    print(f"Global block label: {mm.global_block_label}")
