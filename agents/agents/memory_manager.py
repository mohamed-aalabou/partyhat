import os
import sys
import json
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from letta_client import Letta

try:
    # Prefer TOON for token-efficient storage, but fall back to JSON if unavailable
    from toon import (
        encode as toon_encode,
        decode as toon_decode,
        DecodeOptions as ToonDecodeOptions,
    )
except Exception:  # pragma: no cover - extremely unlikely in production environment
    toon_encode = None  # type: ignore[assignment]
    toon_decode = None  # type: ignore[assignment]
    ToonDecodeOptions = None  # type: ignore[assignment]

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
)


class MemoryManager:
    def __init__(self, user_id: str = "default", project_id: str | None = None):
        """
        Args:
            user_id: The authenticated user's ID from the backend auth layer.
                     Defaults to "default" for local testing only.
            project_id: Optional project ID. When set, memory is scoped primarily
                        to this project (user_block_label becomes project:{project_id}),
                        so each project has its own isolated state regardless of user,
                        and global logs are also project-specific.
        """
        self.client = Letta(api_key=os.getenv("LETTA_API_KEY"))
        self.user_id = user_id
        self.project_id = project_id
        if project_id:
            # Project-scoped memory: one block per project, shared across users
            # working on the same project. This prevents a single user from
            # accumulating all project state into one block and hitting size limits.
            self.user_block_label = f"project:{project_id}"
        else:
            self.user_block_label = f"user:{user_id}"
        # Global log is project-scoped when a project_id is present,
        # otherwise we fall back to the legacy shared global log.
        if project_id:
            self.global_block_label = f"project:{project_id}:agent_log"
        else:
            self.global_block_label = "global_agent_log"

    def _serialize(self, data: dict) -> str:
        """
        Serialize the in-memory JSON-compatible dict to a string.

        Prefers TOON for token-efficiency, but falls back to JSON. Also used by
        other agent tools when they update the shared user block.
        """
        if toon_encode is not None:
            try:
                return toon_encode(data)
            except Exception:
                # If TOON encoding fails for any reason, fall back to JSON
                return json.dumps(data, indent=2)
        return json.dumps(data, indent=2)

    def _deserialize(self, value: str) -> dict:
        """
        Parse the stored string value back into a JSON-compatible dict.

        Supports both TOON (preferred for new data) and legacy JSON-encoded
        blocks for backwards compatibility.
        """
        if not value:
            return {}

        if toon_decode is not None:
            strict_decode_error = None
            try:
                return toon_decode(value)
            except Exception as exc:
                strict_decode_error = exc
                # Some existing blocks may only decode in lenient mode.
                if ToonDecodeOptions is not None:
                    try:
                        return toon_decode(value, ToonDecodeOptions(strict=False))
                    except Exception:
                        pass

                # Backwards-compatibility path for legacy JSON blocks.
                try:
                    return json.loads(value)
                except Exception:
                    # If neither decoder works, propagate the original TOON error
                    # so callers get the root-cause decode failure.
                    raise strict_decode_error

        return json.loads(value)

    def _get_or_create_user_block(self):
        """Get or create the user-scoped memory block."""
        existing = self.client.blocks.list()
        for block in existing:
            if block.label == self.user_block_label:
                return block

        print(f"Creating user memory block: {self.user_block_label}")
        initial_value = {
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
                "preferred_chain": "Avalanche C-Chain",
            },
            # Per-agent state slices
            "agents": {
                # Planning agent state (backwards-compatible with previous layout)
                "planning": {
                    "current_plan": None,
                    "plan_history": [],
                    "reasoning_notes": [],
                    "sessions": [],
                },
                # Additional agents can extend these sections as needed
                "coding": {
                    "artifacts": [],
                    "sessions": [],
                    "notes": [],
                },
                "testing": {
                    "test_plans": [],
                    "last_test_results": [],
                    "sessions": [],
                },
                "deployment": {
                    "targets": [],
                    "deployments": [],
                    "sessions": [],
                },
                "audit": {
                    "issues": [],
                    "risk_notes": [],
                    "sessions": [],
                },
            },
        }

        return self.client.blocks.create(
            label=self.user_block_label,
            value=self._serialize(initial_value),
            limit=50000,
        )

    def _ensure_agents_structure(self, data: dict) -> None:
        """
        Ensure that the 'agents' container and known agent slices exist.
        Also performs a light, backwards-compatible migration from the old
        top-level planning keys into agents['planning'] if present.
        """
        agents = data.setdefault("agents", {})

        planning_state = agents.setdefault(
            "planning",
            {
                "current_plan": None,
                "plan_history": [],
                "reasoning_notes": [],
                "sessions": [],
            },
        )

        # Backwards-compatibility: migrate legacy top-level planning fields
        if "current_plan" in data and planning_state.get("current_plan") is None:
            planning_state["current_plan"] = data.get("current_plan")
        if "plan_history" in data and not planning_state.get("plan_history"):
            planning_state["plan_history"] = data.get("plan_history", [])
        if "reasoning_notes" in data and not planning_state.get("reasoning_notes"):
            planning_state["reasoning_notes"] = data.get("reasoning_notes", [])
        if "sessions" in data and not planning_state.get("sessions"):
            planning_state["sessions"] = data.get("sessions", [])

        agents.setdefault(
            "coding",
            {
                "artifacts": [],
                "sessions": [],
                "notes": [],
            },
        )
        agents.setdefault(
            "testing",
            {
                "test_plans": [],
                "last_test_results": [],
                "sessions": [],
            },
        )
        agents.setdefault(
            "deployment",
            {
                "targets": [],
                "deployments": [],
                "sessions": [],
            },
        )
        agents.setdefault(
            "audit",
            {
                "issues": [],
                "risk_notes": [],
                "sessions": [],
            },
        )

    def _get_agent_state(self, data: dict, agent_name: str) -> dict:
        """
        Return the mutable state dict for a given agent inside the user block,
        ensuring the agents container exists.
        """
        self._ensure_agents_structure(data)
        return data["agents"].setdefault(agent_name, {})

    def get_agent_state(self, agent_name: str) -> dict:
        """
        Public helper to read the state slice for a given agent.
        """
        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        return data["agents"].get(agent_name, {})

    def set_agent_state(self, agent_name: str, state: dict) -> None:
        """
        Public helper to replace the state slice for a given agent.
        """
        data, block = self._read_user_block()
        self._ensure_agents_structure(data)
        data["agents"][agent_name] = state
        self.client.blocks.update(block.id, value=self._serialize(data))

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
            value=self._serialize({"agent_log": []}),
            limit=100000,
        )

    def _read_user_block(self):
        """Read and parse the user block. Returns (data dict, block object)."""
        block = self._get_or_create_user_block()
        return self._deserialize(block.value), block

    def _read_global_block(self):
        """Read and parse the global block. Returns (data dict, block object)."""
        block = self._get_or_create_global_block()
        return self._deserialize(block.value), block

    def save_plan(self, plan: dict) -> None:
        """
        Save the current smart contract plan to user memory.
        Archives only the previous plan to plan_history before overwriting,
        keeping at most the most recent prior version to avoid unbounded growth.
        Args:
            plan: The SmartContractPlan as a dict (from model.model_dump())
        """
        data, block = self._read_user_block()
        planning_state = self._get_agent_state(data, "planning")

        if planning_state.get("current_plan") is not None:
            # Always truncate plan_history to a single most recent entry to
            # prevent the user block from growing without bound. We only
            # retain the immediately previous plan version.
            previous_entry = {
                "archived_at": datetime.utcnow().isoformat(),
                "plan": planning_state["current_plan"],
            }
            planning_state["plan_history"] = [previous_entry]

        planning_state["current_plan"] = plan
        self.client.blocks.update(block.id, value=self._serialize(data))

    def get_plan(self) -> dict | None:
        """Retrieve the current plan from user memory."""
        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        planning_state = data["agents"]["planning"]
        # Prefer the agent slice but fall back to legacy top-level key
        return planning_state.get("current_plan") or data.get("current_plan")

    def get_plan_history(self) -> list:
        """Retrieve all previous versions of the plan."""
        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        planning_state = data["agents"]["planning"]
        return planning_state.get("plan_history") or data.get("plan_history", [])

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

        self.client.blocks.update(block.id, value=self._serialize(data))

    def get_user_profile(self) -> dict:
        data, _ = self._read_user_block()
        return data.get("profile", {})

    def save_user_preference(self, key: str, value: str) -> None:

        data, block = self._read_user_block()
        data["preferences"][key] = value
        self.client.blocks.update(block.id, value=self._serialize(data))

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
        planning_state = self._get_agent_state(data, "planning")
        planning_state.setdefault("reasoning_notes", [])
        planning_state["reasoning_notes"].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "note": note,
            }
        )
        self.client.blocks.update(block.id, value=self._serialize(data))

    def get_reasoning_notes(self) -> list:
        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        planning_state = data["agents"]["planning"]
        return planning_state.get("reasoning_notes") or data.get(
            "reasoning_notes", []
        )

    def save_session_summary(self, summary: str) -> None:

        data, block = self._read_user_block()
        planning_state = self._get_agent_state(data, "planning")
        planning_state.setdefault("sessions", [])
        planning_state["sessions"].append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "summary": summary,
            }
        )
        self.client.blocks.update(block.id, value=self._serialize(data))

    def get_session_history(self) -> list:
        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        planning_state = data["agents"]["planning"]
        return planning_state.get("sessions") or data.get("sessions", [])

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

        self.client.blocks.update(block.id, value=self._serialize(data))

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
