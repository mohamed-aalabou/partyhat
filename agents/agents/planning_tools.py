"""
Planning Agent Tool Registry
-----------------------------
Tools defined here:
    1. get_current_plan: to read current draft from memory
    2. send_question_batch: to expose up to 5 pending questions to UI
    3. send_answer_recommendations: legacy quick replies for single-question UI
    4. save_plan_draft: to persist intermediate draft mid-conversation
    5. validate_plan: to run Pydantic schema check explicitly
    6. publish_final_plan: to finalise and save to user + global memory
    7. save_reasoning_note: to log WHY a decision was made (episodic memory)
"""

import sys
import os
from typing import List, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.tools import tool
from pydantic import BaseModel, Field
from schemas.plan_schema import SmartContractPlan, PlanStatus


def _get_memory_manager():
    """Lazy import to avoid circular dependencies. Uses project/user from context."""
    from agents.memory_manager import MemoryManager
    from agents.context import get_project_context

    project_id, user_id = get_project_context()
    return MemoryManager(user_id=user_id or "default", project_id=project_id)


class AnswerRecommendation(BaseModel):
    text: str = Field(
        ...,
        description="Suggested answer text the frontend can render as a quick reply.",
    )
    recommended: Optional[bool] = Field(
        default=None,
        description=(
            "Optional hint whether this answer is the primary recommendation. "
            "Omit when no specific recommendation is intended."
        ),
    )


class PlanningQuestion(BaseModel):
    question: str = Field(
        ...,
        description="A concise planning question for the user to answer.",
    )
    answer_recommendations: List[AnswerRecommendation] = Field(
        default_factory=list,
        description=(
            "Optional quick-reply suggestions for this specific question. "
            "Use 0-5 items."
        ),
    )


@tool
def get_current_plan() -> dict:
    """
    Retrieve the current smart contract plan draft from memory.

    Call this tool:
    - At the start of EVERY conversation to check if a plan already exists
    - When the user asks to continue or modify an existing plan
    - Before saving a new draft to understand the current state
    - When resuming after a session gap

    Returns the plan as a dict, or an empty dict if no plan exists yet.
    """
    try:
        mm = _get_memory_manager()
        plan = mm.get_plan()
        if plan:
            return plan
        return {"message": "No plan exists yet. This is a fresh start."}
    except Exception as e:
        return {"error": f"Could not retrieve plan: {str(e)}"}


def get_answer_recommendations() -> List[dict]:
    """
    Read the latest answer recommendations from planning agent state.
    Returns an empty list when no recommendations are available.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        recommendations = state.get("answer_recommendations", [])
        if isinstance(recommendations, list):
            return recommendations
        return []
    except Exception:
        return []


def get_pending_questions() -> List[dict]:
    """
    Read the latest structured planning questions from agent state.
    Returns an empty list when no pending questions are available.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        questions = state.get("pending_questions", [])
        if isinstance(questions, list):
            return questions
        return []
    except Exception:
        return []


def clear_answer_recommendations() -> None:
    """
    Clear transient answer recommendations before each planning turn.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        if state.get("answer_recommendations"):
            state["answer_recommendations"] = []
            mm.set_agent_state("planning", state)
    except Exception:
        # Best-effort cleanup; failures should never block planning flow.
        pass


def clear_pending_questions() -> None:
    """
    Clear transient structured planning questions before each planning turn.
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        updated = False
        if state.get("pending_questions"):
            state["pending_questions"] = []
            updated = True
        if state.get("answer_recommendations"):
            state["answer_recommendations"] = []
            updated = True
        if updated:
            mm.set_agent_state("planning", state)
    except Exception:
        # Best-effort cleanup; failures should never block planning flow.
        pass


def _deployment_address_issues(plan: SmartContractPlan) -> List[str]:
    issues: List[str] = []
    for contract in plan.contracts:
        constructor = contract.constructor
        if not constructor:
            continue
        for item in constructor.inputs:
            if item.type.lower() != "address":
                continue
            if item.default_value and item.default_value.strip():
                continue
            issues.append(
                (
                    f"Contract '{contract.name}' constructor input '{item.name}' "
                    "is an address and needs a deployment wallet. Ask for a "
                    "specific wallet address or record default_value='deployer'."
                )
            )
    return issues


def _deployment_manifest_issues(plan: SmartContractPlan) -> List[str]:
    issues: List[str] = []
    target = plan.deployment_target
    if not target.network or target.network != "avalanche_fuji":
        issues.append(
            "deployment_target.network must be 'avalanche_fuji' for the current runtime."
        )
    if not target.chain_id:
        issues.append("deployment_target.chain_id is required.")
    if not target.rpc_url_env_var:
        issues.append("deployment_target.rpc_url_env_var is required.")
    if not target.private_key_env_var:
        issues.append("deployment_target.private_key_env_var is required.")

    if len(plan.contracts) <= 1:
        return issues

    primary = [c for c in plan.contracts if c.deployment_role == "primary_deployable"]
    if len(primary) != 1:
        issues.append(
            "Multi-contract plans must mark exactly one contract as deployment_role='primary_deployable'."
        )

    deploy_orders = [
        c.deploy_order for c in plan.contracts if c.deployment_role and c.deploy_order
    ]
    if len(deploy_orders) != len(
        [c for c in plan.contracts if c.deployment_role and c.deploy_order is not None]
    ):
        issues.append(
            "Every deployable contract in a multi-contract plan must define deploy_order."
        )
    if len(deploy_orders) != len(set(deploy_orders)):
        issues.append("deploy_order values must be unique across deployable contracts.")
    return issues


@tool
def save_plan_draft(plan: SmartContractPlan) -> dict:
    """
    Save an intermediate draft of the smart contract plan to memory.

    Call this tool:
    - ONCE per conversation turn after collecting a meaningful new piece of
      information (project name, a new function, constructor details).
    - Mid-conversation, NOT just at the end, to prevent data loss if the
      session ends unexpectedly
    - After the user confirms a section is correct
    - Whenever significant new information has been added

    The plan status will be forced to 'draft' automatically.
    Do NOT use this for the final save; Use publish_final_plan instead.

    Args:
        plan: The current SmartContractPlan with all fields collected so far.

    Returns a confirmation dict or an error dict.
    """
    try:
        # Forcing status to draft for intermediate saves
        plan.status = PlanStatus.DRAFT

        mm = _get_memory_manager()
        mm.save_plan(plan.model_dump())

        # Logging to global audit trail
        mm.log_agent_action(
            agent_name="planning_agent",
            action="plan_draft_saved",
            why=f"Draft saved with {len(plan.contracts)} contract(s) — status: draft",
            how="save_plan_draft tool",
        )

        return {
            "success": True,
            "project_name": plan.project_name,
            "contracts": len(plan.contracts),
            "status": plan.status.value,
        }
    except Exception as e:
        return {"error": f"Could not save draft: {str(e)}"}


@tool
def send_question_batch(questions: List[PlanningQuestion]) -> dict:
    """
    Save a batch of pending planning questions for the latest agent turn.

    Use this whenever you ask the user one or more clarifying questions.
    Rules:
    - Ask at most 5 questions per turn
    - Each question must be independently answerable
    - Use answer_recommendations inside each question when quick replies help

    For backward compatibility with older clients, the first question's
    recommendations are also mirrored into answer_recommendations.
    """
    if len(questions) > 5:
        return {"error": "A question batch may contain at most 5 questions."}

    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        state["pending_questions"] = []

        for question in questions:
            if len(question.answer_recommendations) > 5:
                return {
                    "error": (
                        "Each planning question may contain at most 5 "
                        "answer recommendations."
                    )
                }
            state["pending_questions"].append(
                question.model_dump(exclude_none=True)
            )

        first_recommendations = (
            questions[0].answer_recommendations if questions else []
        )
        state["answer_recommendations"] = [
            rec.model_dump(exclude_none=True) for rec in first_recommendations
        ]
        mm.set_agent_state("planning", state)

        return {
            "success": True,
            "count": len(state["pending_questions"]),
            "pending_questions": state["pending_questions"],
            "answer_recommendations": state["answer_recommendations"],
        }
    except Exception as e:
        return {"error": f"Could not save question batch: {str(e)}"}


@tool
def send_answer_recommendations(recommendations: List[AnswerRecommendation]) -> dict:
    """
    Save answer recommendations for the latest planning question.

    Use this when asking the user a question and you want the UI to show
    suggested quick-reply options. Each item must include:
    - text: the answer option text
    - recommended (optional): set true for the best option(s)
    """
    try:
        mm = _get_memory_manager()
        state = mm.get_agent_state("planning")
        state["answer_recommendations"] = [
            rec.model_dump(exclude_none=True) for rec in recommendations
        ]
        if state.get("pending_questions") and isinstance(
            state["pending_questions"], list
        ):
            first_question = state["pending_questions"][0]
            if isinstance(first_question, dict):
                first_question["answer_recommendations"] = state[
                    "answer_recommendations"
                ]
        mm.set_agent_state("planning", state)

        return {
            "success": True,
            "count": len(recommendations),
            "answer_recommendations": state["answer_recommendations"],
        }
    except Exception as e:
        return {"error": f"Could not save answer recommendations: {str(e)}"}


@tool
def validate_plan(plan: SmartContractPlan) -> dict:
    """
    Validate a smart contract plan against the SmartContractPlan schema.

    Call this tool:
    - Before publishing the final plan to confirm it is complete
    - When you think you have collected enough information to finalise
    - After making changes to an existing plan to confirm nothing broke

    Pydantic validation runs automatically when this tool is called.
    If the plan reaches this function, it is already structurally valid.
    This tool checks for semantic completeness (at least one contract,
    at least one function per contract, etc.)

    Args:
        plan: The SmartContractPlan to validate.

    Returns a validation result dict with a summary or list of issues.
    """
    issues = []

    if not plan.contracts:
        issues.append("No contracts defined yet.")

    for contract in plan.contracts:
        if not contract.functions:
            issues.append(f"Contract '{contract.name}' has no functions defined.")
        if not contract.constructor:
            issues.append(f"Contract '{contract.name}' has no constructor defined.")

    issues.extend(_deployment_address_issues(plan))
    issues.extend(_deployment_manifest_issues(plan))

    if issues:
        return {
            "valid": False,
            "issues": issues,
        }

    contract_summaries = [
        {
            "name": c.name,
            "erc_template": c.erc_template,
            "functions": len(c.functions),
            "dependencies": c.dependencies,
        }
        for c in plan.contracts
    ]

    return {
        "valid": True,
        "project_name": plan.project_name,
        "status": plan.status.value,
        "contracts": contract_summaries,
    }


@tool
def publish_final_plan(plan: SmartContractPlan) -> dict:
    """
    Finalise and publish the smart contract plan to user and global memory.

    Call this tool ONLY when:
    - The user has explicitly confirmed they are happy with the complete plan
    - validate_plan has been called and returned valid: true
    - All contracts have at least one function and a constructor
    - The plan is ready for the code generation agent to pick up

    This sets status to 'ready' and logs the full action to the global
    audit trail so downstream agents know the plan is available.

    Do NOT call this for intermediate saves; Use save_plan_draft instead.

    Args:
        plan: The complete, validated SmartContractPlan.

    Returns confirmation dict with plan summary.
    """
    try:
        issues = _deployment_address_issues(plan)
        if issues:
            return {
                "error": (
                    "Plan is missing required deployment wallet decisions for one "
                    "or more constructor address inputs."
                ),
                "issues": issues,
            }

        # Setting status to ready, it signals to Create agent it can start
        plan.status = PlanStatus.READY

        mm = _get_memory_manager()
        mm.save_plan(plan.model_dump())

        decisions = [
            f"Selected {c.erc_template or 'custom'} for contract {c.name}"
            for c in plan.contracts
        ]

        mm.log_agent_action(
            agent_name="planning_agent",
            action="plan_published",
            decisions_made=decisions,
            why="User confirmed plan complete and ready for code generation",
            how="publish_final_plan tool",
        )

        return {
            "success": True,
            "project_name": plan.project_name,
            "status": plan.status.value,
            "message": (
                f"Plan published. '{plan.project_name}' is ready for code generation. "
                f"The user can still edit as long as the contract is not deployed on-chain."
            ),
        }
    except Exception as e:
        return {"error": f"Could not publish plan: {str(e)}"}


@tool
def save_reasoning_note(note: str) -> dict:
    """
    Save a note explaining WHY a decision was made during this planning session.

    Call this tool:
    - When the user makes a significant choice (e.g. chose ERC-721 over
      ERC-20 because their tokens are unique)
    - When something ambiguous was resolved (e.g. user unsure about supply
      cap, defaulted to unlimited)
    - When the user explicitly rejects a suggestion and explains why
    - When an important constraint or preference is revealed
    - At the end of a session to summarise what was decided

    This builds the episodic memory layer; Future sessions load these notes
    to understand the reasoning behind the plan, not just the plan itself.

    Args:
        note: A clear, concise plain English explanation of the decision
              and its rationale.

    Returns confirmation dict.
    """
    try:
        mm = _get_memory_manager()
        mm.save_reasoning_note(note)

        return {
            "success": True,
            "note_saved": note,
        }
    except Exception as e:
        return {"error": f"Could not save reasoning note: {str(e)}"}


# Default planning tools; MCP tools can be injected at runtime.
_mcp_tools: List = []

PLANNING_TOOLS = [
    get_current_plan,
    send_question_batch,
    send_answer_recommendations,
    save_plan_draft,
    validate_plan,
    publish_final_plan,
    save_reasoning_note,
]


async def load_planning_tools() -> List:
    """
    Async helper to load OpenZeppelin MCP tools via MultiServerMCPClient.

    This should be called from an async context (e.g. FastAPI startup event),
    not at import time.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient

        client = MultiServerMCPClient(
            {
                "openzeppelin": {
                    "transport": "http",
                    "url": "https://mcp.openzeppelin.com/contracts/solidity/mcp",
                }
            }
        )

        tools = await client.get_tools(server_name="openzeppelin")
        # Log tool names for visibility
        tool_names = [getattr(t, "name", repr(t)) for t in tools]
        print("OpenZeppelin MCP tools loaded:", tool_names)
        return tools
    except Exception as e:
        print(f"Warning: OpenZeppelin MCP tools could not be loaded: {e}")
        return []


def set_planning_mcp_tools(tools: List) -> None:
    """
    Inject MCP tools into the global PLANNING_TOOLS list.

    Call this after load_planning_tools() has completed.
    """
    global _mcp_tools, PLANNING_TOOLS
    _mcp_tools = tools or []
    PLANNING_TOOLS = _mcp_tools + [
        get_current_plan,
        send_question_batch,
        send_answer_recommendations,
        save_plan_draft,
        validate_plan,
        publish_final_plan,
        save_reasoning_note,
    ]
