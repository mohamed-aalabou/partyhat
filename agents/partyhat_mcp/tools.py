"""
Tools:
    start_planning: kicks off or continues a planning session
    generate_contract: triggers the coding agent
    run_tests: triggers the testing agent
    deploy_contract: triggers the deployment agent
    audit_contract: triggers the audit agent
"""

import uuid
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.agent_registry import chat_with_intent
from agents.context import set_project_context
from agents.memory_manager import MemoryManager
from partyhat_mcp.auth import verify_payment


def _make_session_id() -> str:
    """Generate a unique session ID for a new MCP tool call."""
    return f"mcp-{uuid.uuid4().hex[:12]}"


def _set_context(project_id: str, user_id: str) -> None:
    """Set project/user context so all downstream tools scope correctly."""
    set_project_context(project_id, user_id)


def start_planning(
    project_id: str,
    message: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Start or continue a smart contract planning conversation.

    Use this tool to describe what smart contract you want to build.
    The planning agent will ask clarifying questions one at a time and
    build a structured plan. Call this tool repeatedly to continue
    the conversation until the plan is complete.

    Args:
        project_id:     Unique ID for this contract project. Generate a UUID
                        on first call and reuse it for all subsequent calls
                        on the same project.
        message:        Your message to the planning agent. On the first call,
                        describe what you want to build. On subsequent calls,
                        answer the agent's questions.
        user_id:        Optional identifier for the calling agent or user.
        payment_proof:  x402 payment proof (required when payments are enabled).

    Returns:
        dict with:
            session_id:  Session identifier (pass back on subsequent calls)
            response:    The planning agent's reply
            tool_calls:  Which internal tools were called
            plan_status: Current plan status (draft/ready/etc.)
    """
    verify_payment("start_planning", payment_proof)
    _set_context(project_id, user_id)

    session_id = _make_session_id()

    result = chat_with_intent(
        intent="planning",
        session_id=session_id,
        user_message=message,
        project_id=project_id,
    )

    # Reading the current plan status from memory
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    agent_state = mm.get_agent_state("planning")
    plan_status = agent_state.get("plan_status", "draft")

    return {
        "session_id": result["session_id"],
        "response": result["response"],
        "tool_calls": result.get("tool_calls", []),
        "plan_status": plan_status,
    }


def generate_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Generate Solidity code from the approved smart contract plan.

    The plan must be in 'ready' status before calling this tool.
    Complete the planning conversation first using start_planning until
    the plan is approved.

    Args:
        project_id:     The project ID used during planning.
        user_id:        Optional identifier for the calling agent or user.
        payment_proof:  x402 payment proof (required when payments are enabled).

    Returns:
        dict with:
            session_id:   Session identifier
            response:     Coding agent's response describing what was generated
            tool_calls:   Which internal tools were called
            artifacts:    List of generated file paths
    """
    verify_payment("generate_contract", payment_proof)
    _set_context(project_id, user_id)

    # Checking that the plan is ready before triggering coding
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    agent_state = mm.get_agent_state("planning")
    plan_status = agent_state.get("plan_status")

    if plan_status not in ("ready", "generating"):
        return {
            "error": (
                f"Plan is not ready for code generation (current status: '{plan_status}'). "
                f"Complete the planning conversation first using start_planning."
            )
        }

    session_id = _make_session_id()

    result = chat_with_intent(
        intent="coding",
        session_id=session_id,
        user_message="The plan is approved. Please generate the Solidity contracts now.",
        project_id=project_id,
    )

    # Reading artifact metadata from coding state
    coding_state = mm.get_agent_state("coding")

    return {
        "session_id": result["session_id"],
        "response": result["response"],
        "tool_calls": result.get("tool_calls", []),
        "artifact_count": coding_state.get("artifact_count", 0),
        "last_artifact_path": coding_state.get("last_artifact_path"),
    }


def run_tests(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Generate and run Foundry tests against the generated smart contracts.

    Contracts must be generated first using generate_contract.

    Args:
        project_id:     The project ID used during planning and generation.
        user_id:        Optional identifier for the calling agent or user.
        payment_proof:  x402 payment proof (required when payments are enabled).

    Returns:
        dict with:
            session_id:       Session identifier
            response:         Testing agent's response with test results
            tool_calls:       Which internal tools were called
            last_test_status: "passed" | "failed" | "error"
    """
    verify_payment("run_tests", payment_proof)
    _set_context(project_id, user_id)

    session_id = _make_session_id()

    result = chat_with_intent(
        intent="testing",
        session_id=session_id,
        user_message="Please generate and run Foundry tests for the generated contracts.",
        project_id=project_id,
    )

    mm = MemoryManager(user_id=user_id, project_id=project_id)
    testing_state = mm.get_agent_state("testing")

    return {
        "session_id": result["session_id"],
        "response": result["response"],
        "tool_calls": result.get("tool_calls", []),
        "last_test_status": testing_state.get("last_test_status"),
    }


def deploy_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Deploy the tested smart contracts to Avalanche Fuji testnet.

    Tests must pass before deployment. Run run_tests first and confirm
    last_test_status is "passed".

    Args:
        project_id:     The project ID used throughout the pipeline.
        user_id:        Optional identifier for the calling agent or user.
        payment_proof:  x402 payment proof (required when payments are enabled).

    Returns:
        dict with:
            session_id:       Session identifier
            response:         Deployment agent's response
            tool_calls:       Which internal tools were called
            deployed_address: The on-chain contract address (if successful)
            tx_hash:          The deployment transaction hash
            snowtrace_url:    Snowtrace verification link
    """
    verify_payment("deploy_contract", payment_proof)
    _set_context(project_id, user_id)

    # Checking tests passed before deploying
    mm = MemoryManager(user_id=user_id, project_id=project_id)
    testing_state = mm.get_agent_state("testing")
    last_test_status = testing_state.get("last_test_status")

    if last_test_status != "passed":
        return {
            "error": (
                f"Tests have not passed (current status: '{last_test_status}'). "
                f"Run run_tests first and ensure all tests pass before deploying."
            )
        }

    session_id = _make_session_id()

    result = chat_with_intent(
        intent="deployment",
        session_id=session_id,
        user_message="Tests have passed. Please deploy the contracts to Avalanche Fuji.",
        project_id=project_id,
    )

    deployment_state = mm.get_agent_state("deployment")

    return {
        "session_id": result["session_id"],
        "response": result["response"],
        "tool_calls": result.get("tool_calls", []),
        "deployed_address": deployment_state.get("deployed_address"),
        "tx_hash": deployment_state.get("tx_hash"),
        "snowtrace_url": deployment_state.get("snowtrace_url"),
    }


def audit_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Run a security audit on the generated smart contracts.

    Can be called at any point after generate_contract. Identifies
    potential security issues, access control problems, and common
    Solidity vulnerabilities.

    Args:
        project_id:     The project ID used throughout the pipeline.
        user_id:        Optional identifier for the calling agent or user.
        payment_proof:  x402 payment proof (required when payments are enabled).

    Returns:
        dict with:
            session_id:  Session identifier
            response:    Audit agent's findings and recommendations
            tool_calls:  Which internal tools were called
            open_issues: Number of open security issues found
    """
    verify_payment("audit_contract", payment_proof)
    _set_context(project_id, user_id)

    session_id = _make_session_id()

    result = chat_with_intent(
        intent="audit",
        session_id=session_id,
        user_message="Please audit the generated contracts for security issues.",
        project_id=project_id,
    )

    mm = MemoryManager(user_id=user_id, project_id=project_id)
    audit_state = mm.get_agent_state("audit")

    return {
        "session_id": result["session_id"],
        "response": result["response"],
        "tool_calls": result.get("tool_calls", []),
        "open_issues": audit_state.get("open_issues", 0),
    }
