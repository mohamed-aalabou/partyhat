"""
Exposes the full smart contract pipeline as MCP tools.
External AI agents can call these tools to plan, generate,
test, deploy, and audit Avalanche smart contracts.

Usage:
    # Run standalone MCP server (stdio transport for local testing)
    cd partyhat/agents
    uv run python mcp/server.py

    # Run as HTTP SSE server (for remote agents over the network)
    uv run python mcp/server.py --transport sse --port 8001

The MCP server runs on a separate port from the main FastAPI server (8000).
Both can run simultaneously.

x402 payments:
    Set PARTYHAT_PAYMENTS_ENABLED=true to enable payment verification.
    See partyhat_mcp/auth.py for pricing config and verification hook.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastmcp import FastMCP

from partyhat_mcp.tools import (
    start_planning,
    generate_contract,
    run_tests,
    deploy_contract,
    audit_contract,
)
from partyhat_mcp.auth import TOOL_PRICES, PAYMENT_ADDRESS

load_dotenv()

mcp = FastMCP(
    name="PartyHat",
    instructions=f"""
You have access to PartyHat: an AI-powered smart contract pipeline for Avalanche.

PartyHat lets you build, test, and deploy Solidity smart contracts through
a simple conversation-driven pipeline.

PIPELINE ORDER (follow this sequence):
1. start_planning    → describe what you want to build, answer questions
2. generate_contract → generate production Solidity code from the approved plan
3. run_tests         → run Foundry tests against the generated contracts
4. deploy_contract   → deploy to Avalanche Fuji testnet
5. audit_contract    → security audit (can run after generate_contract)

IMPORTANT RULES:
- Always call start_planning first and continue until plan_status is "ready"
- Do not call generate_contract until plan_status is "ready"
- Do not call deploy_contract until last_test_status is "passed"
- Use the same project_id across all tool calls for the same contract project
- Generate a UUID for project_id on your first call and reuse it throughout

PRICING (USDC per call, paid via x402 on Avalanche):
{chr(10).join(f'  {tool}: ${price}' for tool, price in TOOL_PRICES.items())}

Payment address: {PAYMENT_ADDRESS or 'not configured'}
""",
)


@mcp.tool()
def partyhat_start_planning(
    project_id: str,
    message: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Start or continue a smart contract planning conversation with PartyHat.

    Describe what smart contract you want to build. The planning agent will
    ask clarifying questions in batches of up to 5 and build a structured plan.
    Keep calling this tool to answer questions until plan_status is "ready".

    Args:
        project_id:     Unique project ID (UUID). Generate once and reuse.
        message:        Your message i.e describe what you want to build or
                        answer the agent's latest question.
        user_id:        Optional identifier for the calling agent.
        payment_proof:  x402 payment proof (when payments are enabled).
    """
    return start_planning(project_id, message, user_id, payment_proof)


@mcp.tool()
def partyhat_generate_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Generate production Solidity code from the approved smart contract plan.

    Requires plan_status to be "ready". Complete the planning conversation
    using partyhat_start_planning first.

    Args:
        project_id:     The project ID used during planning.
        user_id:        Optional identifier for the calling agent.
        payment_proof:  x402 payment proof (when payments are enabled).
    """
    return generate_contract(project_id, user_id, payment_proof)


@mcp.tool()
def partyhat_run_tests(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Generate and run Foundry tests against the generated smart contracts.

    Requires contracts to be generated first using partyhat_generate_contract.

    Args:
        project_id:     The project ID used throughout the pipeline.
        user_id:        Optional identifier for the calling agent.
        payment_proof:  x402 payment proof (when payments are enabled).
    """
    return run_tests(project_id, user_id, payment_proof)


@mcp.tool()
def partyhat_deploy_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Deploy the tested smart contracts to Avalanche Fuji testnet.

    Requires last_test_status to be "passed". Run partyhat_run_tests first.
    Returns the deployed contract address and Snowtrace verification link.

    Args:
        project_id:     The project ID used throughout the pipeline.
        user_id:        Optional identifier for the calling agent.
        payment_proof:  x402 payment proof (when payments are enabled).
    """
    return deploy_contract(project_id, user_id, payment_proof)


@mcp.tool()
def partyhat_audit_contract(
    project_id: str,
    user_id: str = "mcp-user",
    payment_proof: str | None = None,
) -> dict:
    """
    Run a security audit on the generated smart contracts.

    Can be called any time after partyhat_generate_contract. Identifies
    security vulnerabilities, access control issues, and Solidity best
    practice violations.

    Args:
        project_id:     The project ID used throughout the pipeline.
        user_id:        Optional identifier for the calling agent.
        payment_proof:  x402 payment proof (when payments are enabled).
    """
    return audit_contract(project_id, user_id, payment_proof)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PartyHat MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport type: stdio (local testing) or sse (HTTP remote agents)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for SSE transport (default: 8001)",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        print(f"Starting PartyHat MCP server (SSE) on port {args.port}...")
        mcp.run(transport="sse", port=args.port)
    else:
        print("Starting PartyHat MCP server (stdio — local testing)...")
        mcp.run(transport="stdio")
