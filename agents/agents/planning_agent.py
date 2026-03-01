import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

from agents.planning_tools import PLANNING_TOOLS

load_dotenv()


SYSTEM_PROMPT = """You are PartyHat's smart contract planning assistant.
Your job is to help users design their smart contract by asking clear, simple
questions ONE AT A TIME; like a friendly expert, not a form.

You have access to tools. Use them actively and consciously:
- At the start of EVERY conversation, call get_current_plan() to check if work exists
- Call get_erc_standard() as soon as the user mentions or picks an ERC standard
- Call save_plan_draft() after collecting each major piece of information. Do NOT wait until the end
- Call save_reasoning_note() whenever a significant decision is made or clarified
- Call validate_plan() when you believe you have a complete plan
- Call publish_final_plan() ONLY after the user explicitly confirms they are happy

Your goal is to collect:
- Project name and what it does
- Which ERC standard (ERC-20, ERC-721, ERC-1155, or custom)
- What functions are needed (name, description, inputs, outputs, conditions)
- Constructor inputs (what gets set at deployment)
- Any dependencies (Ownable, other contracts, etc)

Rules:
- Ask ONE question at a time. Never ask multiple questions in one message
- Keep messages short and conversational
- You are ONLY a smart contract planning assistant; So politely redirect off-topic questions
- For standard ERC functions, do NOT ask about them since they are already included in the standard
- Use get_erc_standard() to know which functions are standard before asking about custom ones
- Save drafts frequently; the user may leave and come back

The user can edit their plan at any time as long as the contract is not deployed on-chain.
If they want changes, load the current plan, apply changes, validate, and save.
"""


# Here the MemorySaver stores conversation state in RAM keyed by session_id (thread_id).
# To swap this later for SqliteSaver or PostgresSaver.
# The API layer will generate and manage session_ids.
checkpointer = MemorySaver()


def build_planning_agent():
    llm = ChatOpenAI(model="gpt-4o", temperature=0.3)

    agent = create_react_agent(
        model=llm,
        tools=PLANNING_TOOLS,
        prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,  # persisting conversation history
    )

    return agent


def chat(
    agent,
    session_id: str,
    user_message: str,
) -> dict:
    """
    Args:
        agent:        The compiled ReAct agent from build_planning_agent()
        session_id:   Unique identifier for this user's session (from the API layer)
        user_message: The user's latest message

    Returns:
        {
            "session_id": str,
            "response": str,        (the agent's reply to show the user)
            "tool_calls": list,     (which tools were called this turn (for debugging))
        }
    """
    config = {
        "configurable": {
            "thread_id": session_id,
        }
    }

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


# CLI runner for local testing only
def run_cli():
    """
    Simple CLI interface for testing the agent locally.
    """
    print("\nPartyHat Planning Agent")
    print("  Type 'quit' to exit")
    print("  Type 'new' to start a fresh session\n")

    agent = build_planning_agent()
    session_id = str(uuid.uuid4())
    print(f"Session ID: {session_id}\n")

    result = chat(agent, session_id, "Hello, I want to plan a smart contract.")
    print(f"Agent: {result['response']}")
    if result["tool_calls"]:
        print(f"  [tools called: {', '.join(result['tool_calls'])}]")
    print()

    while True:
        user_input = input("You: ").strip()

        if not user_input:
            continue

        if user_input.lower() == "quit":
            print("Session ended.")
            break

        if user_input.lower() == "new":
            session_id = str(uuid.uuid4())
            print(f"\nNew session started: {session_id}\n")
            result = chat(agent, session_id, "Hello, I want to plan a smart contract.")
        else:
            result = chat(agent, session_id, user_input)

        print(f"\nAgent: {result['response']}")
        if result["tool_calls"]:
            print(f"  [tools called: {', '.join(result['tool_calls'])}]")
        print()


if __name__ == "__main__":
    run_cli()
