"""
Planning Agent Tool Registry
-----------------------------
Tools defined here:
    1. get_current_plan: to read current draft from memory
    2. save_plan_draft: to persist intermediate draft mid-conversation
    3. get_erc_standard: return canonical ERC function definitions #TO CHECK
    4. validate_plan: run Pydantic schema check explicitly
    5. publish_final_plan: to finalise and save to agent + global memory
    6. save_reasoning_note: to log WHY a decision was made (more an episodic memory)
"""

import json
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool
from schemas.plan_schema import SmartContractPlan, PlanStatus


ERC_STANDARDS = {
    "ERC-20": {
        "description": "Fungible token standard. All tokens are identical and interchangeable.",
        "standard_functions": [
            "totalSupply() → uint256",
            "balanceOf(address account) → uint256",
            "transfer(address to, uint256 amount) → bool",
            "allowance(address owner, address spender) → uint256",
            "approve(address spender, uint256 amount) → bool",
            "transferFrom(address from, address to, uint256 amount) → bool",
        ],
        "standard_events": [
            "Transfer(address from, address to, uint256 value)",
            "Approval(address owner, address spender, uint256 value)",
        ],
        "typical_extensions": ["Mintable", "Burnable", "Pausable", "Ownable", "Capped"],
    },
    "ERC-721": {
        "description": "Non-fungible token standard. Each token is unique.",
        "standard_functions": [
            "balanceOf(address owner) → uint256",
            "ownerOf(uint256 tokenId) → address",
            "safeTransferFrom(address from, address to, uint256 tokenId)",
            "transferFrom(address from, address to, uint256 tokenId)",
            "approve(address to, uint256 tokenId)",
            "getApproved(uint256 tokenId) → address",
            "setApprovalForAll(address operator, bool approved)",
            "isApprovedForAll(address owner, address operator) → bool",
        ],
        "standard_events": [
            "Transfer(address from, address to, uint256 tokenId)",
            "Approval(address owner, address approved, uint256 tokenId)",
            "ApprovalForAll(address owner, address operator, bool approved)",
        ],
        "typical_extensions": [
            "Mintable",
            "Burnable",
            "URIStorage",
            "Royalties (EIP-2981)",
            "Enumerable",
        ],
    },
    "ERC-1155": {
        "description": "Multi-token standard. Supports both fungible and non-fungible tokens in one contract.",
        "standard_functions": [
            "balanceOf(address account, uint256 id) → uint256",
            "balanceOfBatch(address[] accounts, uint256[] ids) → uint256[]",
            "setApprovalForAll(address operator, bool approved)",
            "isApprovedForAll(address account, address operator) → bool",
            "safeTransferFrom(address from, address to, uint256 id, uint256 amount, bytes data)",
            "safeBatchTransferFrom(address from, address to, uint256[] ids, uint256[] amounts, bytes data)",
        ],
        "standard_events": ["TransferSingle", "TransferBatch", "ApprovalForAll", "URI"],
        "typical_extensions": ["Mintable", "Burnable", "Supply tracking", "Pausable"],
    },
}


# In-memory store for this session (will be replaced by Letta calls)
# Using a simple dict as a stand-in — the MemoryManager handles persistence
_session_store: dict = {
    "current_plan": None,
    "reasoning_notes": [],
}


def _get_memory_manager():
    """Lazy import to avoid circular dependencies."""
    from memory_manager import MemoryManager

    return MemoryManager()


@tool
def get_current_plan() -> str:
    """
    Retrieve the current smart contract plan draft from memory.

    Call this tool:
    - At the start of a conversation to check if a plan already exists
    - When the user asks to continue or modify an existing plan
    - Before saving a new draft to understand the current state
    - When resuming after a session gap

    Returns the plan as a JSON string, or a message saying no plan exists yet.
    """
    try:
        mm = _get_memory_manager()
        plan = mm.get_plan()
        if plan:
            return f"Current plan found:\n{json.dumps(plan, indent=2)}"
        return "No plan exists yet. This is a fresh start."
    except Exception as e:
        return f"Could not retrieve plan: {str(e)}"


@tool
def save_plan_draft(plan_json: str) -> str:
    """
    Save an intermediate draft of the smart contract plan to memory.

    Call this tool:
    - After collecting each major piece of information (contract name, each function, constructor)
    - Mid-conversation, NOT just at the end — this prevents data loss if the session ends
    - After the user confirms a section is correct
    - Whenever significant new information has been added to the plan

    Args:
        plan_json: The current plan as a valid JSON string matching the SmartContractPlan schema.
                   Status should be 'draft'. Include all fields collected so far.

    Returns a confirmation message or an error if the JSON is invalid.
    """
    try:
        raw = json.loads(plan_json)

        # Force status to draft for intermediate saves
        raw["status"] = PlanStatus.DRAFT.value

        # Validate against schema (catch issues early)
        plan = SmartContractPlan(**raw)

        mm = _get_memory_manager()
        mm.save_plan(plan.model_dump())

        return f"Draft saved successfully. Plan has {len(plan.contracts)} contract(s) and status is '{plan.status}'."
    except json.JSONDecodeError as e:
        return f"Invalid JSON. Could not save draft: {str(e)}"
    except Exception as e:
        return f"Could not save draft: {str(e)}"


@tool
def get_erc_standard(standard_name: str) -> str:
    """
    Get the canonical function definitions and details for a specific ERC standard.

    Call this tool:
    - When the user mentions or selects an ERC standard (ERC-20, ERC-721, ERC-1155)
    - Before asking about functions, to know which ones are already standard (don't ask about those)
    - When you need to clarify what a standard includes vs what needs to be custom-built
    - To suggest appropriate extensions for the user's use case

    Args:
        standard_name: One of "ERC-20", "ERC-721", or "ERC-1155"

    Returns the standard's description, built-in functions, events, and typical extensions.
    """
    standard = ERC_STANDARDS.get(standard_name.upper().replace("ERC", "ERC-").strip())

    if not standard:
        available = ", ".join(ERC_STANDARDS.keys())
        return f"Unknown standard '{standard_name}'. Available standards: {available}. If the user wants something custom, use null for erc_template."

    return json.dumps({"standard": standard_name, **standard}, indent=2)


@tool
def validate_plan(plan_json: str) -> str:
    """
    Validate a smart contract plan against the SmartContractPlan Pydantic schema.

    Call this tool:
    - Before saving a final plan to confirm it is schema-valid
    - When you think you have collected enough information to finalise
    - After making changes to an existing plan to confirm nothing broke
    - To check what required fields are still missing

    Args:
        plan_json: The plan as a JSON string to validate.

    Returns a success message with a summary, or a detailed error describing what is wrong.
    """
    try:
        raw = json.loads(plan_json)
        plan = SmartContractPlan(**raw)

        # Build a summary of what was validated
        contract_summaries = []
        for c in plan.contracts:
            contract_summaries.append(
                f"  - {c.name} ({c.erc_template or 'custom'}): {len(c.functions)} function(s)"
            )

        summary = "\n".join(contract_summaries)
        return (
            f"Plan is valid.\n"
            f"Project: {plan.project_name}\n"
            f"Status: {plan.status}\n"
            f"Contracts:\n{summary}"
        )
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {str(e)}"
    except Exception as e:
        return f"Validation failed: {str(e)}"


@tool
def publish_final_plan(plan_json: str) -> str:
    """
    Finalise and publish the smart contract plan to both agent memory and global shared memory.

    Call this tool ONLY when:
    - The user has confirmed they are happy with the complete plan
    - All required fields are filled (project name, at least one contract, at least one function)
    - validate_plan has already been called and returned success
    - The plan is ready for the code generation agent to pick up

    This sets the plan status to 'ready' and makes it available to ALL other agents.
    Do NOT call this for intermediate saves — use save_plan_draft instead.

    Args:
        plan_json: The complete, validated plan as a JSON string.

    Returns confirmation that the plan is published and ready for the next pipeline stage.
    """
    try:
        raw = json.loads(plan_json)

        # Set status to ready (this signals to the Create agent it can start)
        raw["status"] = PlanStatus.READY.value

        plan = SmartContractPlan(**raw)

        mm = _get_memory_manager()
        mm.save_plan(plan.model_dump())

        return (
            f"Plan published successfully!\n"
            f"Project '{plan.project_name}' is now status '{plan.status}'.\n"
            f"The code generation agent can now pick this up from global memory.\n"
            f"The user can still edit the plan as long as the contract is not deployed."
        )
    except json.JSONDecodeError as e:
        return f"Invalid JSON — could not publish: {str(e)}"
    except Exception as e:
        return f"Could not publish plan: {str(e)}"


@tool
def save_reasoning_note(note: str) -> str:
    """
    Save a note explaining WHY a decision was made during this planning session.

    Call this tool:
    - When the user makes a significant choice (e.g. chose ERC-721 over ERC-20 because tokens are unique)
    - When something ambiguous was resolved (e.g. user wasn't sure about supply cap, defaulted to unlimited)
    - When the user explicitly rejects a suggestion and explains why
    - When an important constraint or preference is revealed
    - At the end of the session to summarise what was decided

    This builds the episodic memory layer — future sessions can load these notes
    to understand the reasoning behind the plan, not just the plan itself.

    Args:
        note: A clear, concise note in plain English describing the decision and its rationale.

    Returns confirmation that the note was saved.
    """
    try:
        mm = _get_memory_manager()

        # Getting existing notes if any
        existing_notes = []
        try:
            plan = mm.get_plan()
            if plan and "_reasoning_notes" in plan:
                existing_notes = plan["_reasoning_notes"]
        except Exception:
            pass

        existing_notes.append(note)

        # Save notes to agent user_context block in Letta
        notes_text = "\n---\n".join(existing_notes)
        mm.update_user_context(f"Reasoning notes from this session:\n{notes_text}")

        return f"Reasoning note saved: '{note}'"
    except Exception as e:
        return f"Could not save reasoning note: {str(e)}"


PLANNING_TOOLS = [
    get_current_plan,
    save_plan_draft,
    get_erc_standard,
    validate_plan,
    publish_final_plan,
    save_reasoning_note,
]
