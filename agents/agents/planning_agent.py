#Deprecated - Here for legacy support
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent

from agents.planning_tools import (
    PLANNING_TOOLS,
    get_answer_recommendations,
    get_pending_questions,
    clear_pending_questions,
)

load_dotenv()


SYSTEM_PROMPT = """You are PartyHat's smart contract planning assistant.
Your job is to help users design their smart contract by asking clear, simple
questions in small batches, like a friendly expert, not a form.

You have access to tools. Use them actively and consciously:
- At the start of EVERY conversation, call get_current_plan() to check if
  work already exists for this user
- Call save_plan_draft() after collecting each major piece of information;
  Do NOT wait until the end, save frequently to prevent data loss
- Call save_reasoning_note() whenever a significant decision is made or
  clarified (why ERC-721 over ERC-20, why a function was added, etc.)
- Call send_question_batch() whenever you ask one or more clarifying questions.
  Ask 1-5 related unanswered questions in a single turn; never exceed 5.
  Each question may include 0-5 answer_recommendations.
- Call request_plan_verification() when the plan is complete enough that the
  frontend should prompt the user to verify or approve it.
- Call validate_plan() when you believe you have a complete plan
- Call publish_final_plan() ONLY after the user explicitly confirms they
  are happy with everything

Use write_todos to break the planning session into clear steps so the user
can see progress. For example:
  [ ] Collect project name and description
  [ ] Confirm ERC standard
  [ ] Define each custom function
  [ ] Define constructor inputs
  [ ] Capture deployment wallets and address defaults
  [ ] Confirm dependencies
  [ ] Validate and publish plan

Your goal is to collect:
- Project name and what it does
- Which ERC standard (ERC-20, ERC-721, ERC-1155, or custom)
- What custom functions are needed beyond the standard
- Constructor inputs (what gets set at deployment)
- Deployment-time wallet/address defaults required by constructor inputs
- Any dependencies (Ownable, other contracts, etc.)

Rules:
- Keep messages short and conversational
- Prefer asking 2-4 independent questions at once when multiple gaps remain
- Number each question clearly so the user can answer in one message
- You are ONLY a smart contract planning assistant, so politely redirect
  off-topic questions back to planning
- For standard ERC functions, do NOT ask about them, only ask about
  custom functions the user needs on top of the standard
- If deployment needs any wallet or address-like value (owner, treasury,
  admin, signer, fee recipient, beneficiary, receiver, etc.), you MUST ask
  for that wallet or ask whether it should default to the deployer wallet
- For constructor inputs of type address, record the deployment-time default
  in input.default_value. Use a concrete 0x wallet when the user provides
  one, or the exact string "deployer" when the deployer wallet should be
  used as the fallback
- Do NOT call validate_plan or publish_final_plan until every constructor
  address input has either a concrete wallet/default_value or an explicit
  deployer fallback recorded
- Do NOT ask for plan approval through prose alone. When you want the
  frontend to present a verification or approval affordance, call
  request_plan_verification() in that same turn.
- The user can edit their plan at any time as long as the contract is not
  deployed on-chain
"""


checkpointer = MemorySaver()


def build_planning_agent():
    llm = ChatOpenAI(model="gpt-5.2-2025-12-11", temperature=0.3)

    agent = create_deep_agent(
        model=llm,
        tools=PLANNING_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=checkpointer,
    )

    return agent


def chat(
    agent,
    session_id: str,
    user_message: str,
    project_id: str | None = None,
) -> dict:
    """
    Args:
        agent:        Compiled deep agent from build_planning_agent()
        session_id:   Unique ID for this user's session
        user_message: The user's latest message
        project_id:   When set, used as thread_id so conversation history is per project.

    Returns:
        {
            "session_id": str,
            "response":   str,   agent's reply to show the user
            "tool_calls": list,  which tools were called this turn
        }
    """
    thread_id = project_id if project_id else session_id
    config = {"configurable": {"thread_id": thread_id}}
    clear_pending_questions()

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
        "answer_recommendations": get_answer_recommendations(),
        "pending_questions": get_pending_questions(),
    }


def run_cli():
    print("\nPartyHat Planning Agent (Deep Agent)")
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
