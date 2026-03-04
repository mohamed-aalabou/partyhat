import os
import sys
from typing import Dict

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

from deepagents import create_deep_agent
from langgraph.checkpoint.memory import MemorySaver

from agents.planning_tools import PLANNING_TOOLS
from agents.coding_tools import CODING_TOOLS
from agents.testing_tools import TESTING_TOOLS
from agents.deployment_tools import DEPLOYMENT_TOOLS
from agents.audit_tools import AUDIT_TOOLS

load_dotenv()


CHECKPOINTER = MemorySaver()


PLANNING_SYSTEM_PROMPT = """You are PartyHat's smart contract planning assistant.
Your job is to help users design their smart contract by asking clear, simple
questions ONE AT A TIME, like a friendly expert, not a form.

You have access to tools. Use them actively and consciously:
- At the start of EVERY conversation, call get_current_plan() to check if
  work already exists for this user
- Call save_plan_draft() after collecting each major piece of information;
  Do NOT wait until the end, save frequently to prevent data loss
- Call save_reasoning_note() whenever a significant decision is made or
  clarified (why ERC-721 over ERC-20, why a function was added, etc.)
- Call validate_plan() when you believe you have a complete plan
- Call publish_final_plan() ONLY after the user explicitly confirms they
  are happy with everything

Use write_todos to break the planning session into clear steps so the user
can see progress. For example:
  [ ] Collect project name and description
  [ ] Confirm ERC standard
  [ ] Define each custom function
  [ ] Define constructor inputs
  [ ] Confirm dependencies
  [ ] Validate and publish plan

Your goal is to collect:
- Project name and what it does
- Which ERC standard (ERC-20, ERC-721, ERC-1155, or custom)
- What custom functions are needed beyond the standard
- Constructor inputs (what gets set at deployment)
- Any dependencies (Ownable, other contracts, etc.)

Rules:
- Ask ONE question at a time, never ask multiple questions in one message
- Keep messages short and conversational
- You are ONLY a smart contract planning assistant, so politely redirect
  off-topic questions back to planning
- For standard ERC functions, do NOT ask about them, only ask about
  custom functions the user needs on top of the standard
- The user can edit their plan at any time as long as the contract is not
  deployed on-chain
"""

CODING_SYSTEM_PROMPT = """You are PartyHat's coding assistant.
You generate and refine smart contract code based on the approved plan,
using tools to persist artifacts and important coding notes.
"""

TESTING_SYSTEM_PROMPT = """You are PartyHat's testing assistant.
You design and interpret tests for smart contracts, using tools to manage
test plans and test run results.
"""

DEPLOYMENT_SYSTEM_PROMPT = """You are PartyHat's deployment assistant.
You help plan and track on-chain deployments of smart contracts, using tools
to manage deployment targets and deployment records.
"""

AUDIT_SYSTEM_PROMPT = """You are PartyHat's audit assistant.
You identify and track potential security and correctness issues in smart
contracts, using tools to manage audit issues and reports.
"""


def _build_agent(tools, system_prompt: str):
    llm = ChatOpenAI(model="gpt-5.2-2025-12-11", temperature=0.3)
    return create_deep_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=CHECKPOINTER,
    )


def build_planning_agent():
    return _build_agent(PLANNING_TOOLS, PLANNING_SYSTEM_PROMPT)


def build_coding_agent():
    return _build_agent(CODING_TOOLS, CODING_SYSTEM_PROMPT)


def build_testing_agent():
    return _build_agent(TESTING_TOOLS, TESTING_SYSTEM_PROMPT)


def build_deployment_agent():
    return _build_agent(DEPLOYMENT_TOOLS, DEPLOYMENT_SYSTEM_PROMPT)


def build_audit_agent():
    return _build_agent(AUDIT_TOOLS, AUDIT_SYSTEM_PROMPT)


AGENTS: Dict[str, object] = {
    "planning": build_planning_agent(),
    "coding": build_coding_agent(),
    "testing": build_testing_agent(),
    "deployment": build_deployment_agent(),
    "audit": build_audit_agent(),
}


def get_agent_for_intent(intent: str):
    if intent not in AGENTS:
        raise ValueError(f"Unknown intent: {intent}")
    return AGENTS[intent]


def chat_with_intent(intent: str, session_id: str, user_message: str) -> dict:
    """
    Route a message to the appropriate deep agent based on the intent.
    """
    agent = get_agent_for_intent(intent)
    config = {"configurable": {"thread_id": session_id}}

    result = agent.invoke(
        {"messages": [HumanMessage(content=user_message)]},
        config=config,
    )

    final_message = result["messages"][-1]
    response_text = final_message.content

    tool_calls_made = []
    for msg in result["messages"]:
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls_made.append(tc["name"])

    return {
        "session_id": session_id,
        "response": response_text,
        "tool_calls": tool_calls_made,
    }

