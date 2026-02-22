import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from typing import Annotated
from typing_extensions import TypedDict
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from schemas.plan_schema import SmartContractPlan

from memory_manager import MemoryManager

load_dotenv()


class PlanningState(TypedDict):
    messages: Annotated[list, add_messages]
    plan_ready: bool  # True once agent is done
    final_plan: dict | None


SYSTEM_PROMPT = """You are PartyHat's smart contract planning assistant. 
Your job is to help users design their smart contract by asking clear, 
simple questions ONE AT A TIME like a friendly expert, not a form.

Your goal is to collect:
- Project name and what it does
- Which ERC standard to use (ERC-20, ERC-721, ERC-1155, or custom)
- What functions are needed (name, what it does, inputs, outputs, rules/conditions)
- Constructor inputs (what gets set at deployment)
- Any contract dependencies (like Ownable, or other contracts in the project)

Rules:
- You are ONLY a smart contract planning assistant. If the user asks anything unrelated 
  to their smart contract project (general blockchain questions, price questions, 
  coding help etc), politely decline and redirect them back to planning their contract.
  Example: "I'm focused on helping you plan your smart contract! Do you have a project 
  in mind, or shall we continue with what we were working on?"
- Ask ONE question at a time. Never ask multiple questions in one message.
- Keep your messages short and conversational.
- If the user provides all the necessary information in a single message (project name, 
  ERC template, functions, supply/constructor details, and dependencies), skip the 
  clarifying questions and output PLAN_READY with the JSON immediately.
- When you have enough information to build a complete plan, output EXACTLY this on its own line:
  PLAN_READY
  Then immediately output the plan as a valid JSON object matching this structure:
  {
    "project_name": "...",
    "description": "...",
    "contracts": [
      {
        "name": "...",
        "description": "...",
        "erc_template": "ERC-20" or null,
        "dependencies": ["..."],
        "constructor": {
          "description": "...",
          "inputs": [{"name": "...", "type": "...", "description": "..."}]
        },
        "functions": [
          {
            "name": "...",
            "description": "...",
            "inputs": [{"name": "...", "type": "...", "description": "..."}],
            "outputs": [{"type": "...", "description": "..."}],
            "conditions": ["..."]
          }
        ]
      }
    ]
  }
- Do not output PLAN_READY until you have project name, at least one function, 
  and the ERC template confirmed.
"""


def chatbot(state: PlanningState) -> PlanningState:
    llm = ChatOpenAI(model="gpt-4o", temperature=0.3)

    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    response = llm.invoke(messages)

    if "PLAN_READY" in response.content:
        return {
            "messages": [response],
            "plan_ready": True,
            "final_plan": None,  # will extract in next step
        }

    return {"messages": [response], "plan_ready": False, "final_plan": None}


# The extractor node
def extract_plan(state: PlanningState) -> PlanningState:
    last_message = state["messages"][-1].content

    # Handling both PLAN_READY signal and raw JSON/markdown code blocks
    if "PLAN_READY" in last_message:
        json_str = last_message.split("PLAN_READY")[-1].strip()
    else:
        json_str = last_message

    if "```" in json_str:
        json_str = json_str.split("```")[1]
        if json_str.startswith("json"):
            json_str = json_str[4:]

    json_str = json_str.strip()

    try:
        raw = json.loads(json_str)
        plan = SmartContractPlan(**raw)
        return {"final_plan": plan.model_dump(), "plan_ready": True, "messages": []}
    except Exception as e:
        print(f"\n Could not parse plan: {e}")
        return {"final_plan": None, "plan_ready": False, "messages": []}


# A router to decide if we continue chatting or extract the plan already
def should_continue(state: PlanningState) -> str:
    last_message = state["messages"][-1].content
    if (
        state.get("plan_ready")
        or "```json" in last_message
        or "PLAN_READY" in last_message
    ):
        return "extract_plan"
    return "human_input"


def build_graph():
    graph = StateGraph(PlanningState)

    graph.add_node("chatbot", chatbot)
    graph.add_node("extract_plan", extract_plan)

    graph.set_entry_point("chatbot")

    graph.add_conditional_edges(
        "chatbot",
        should_continue,
        {
            "extract_plan": "extract_plan",
            "human_input": END,  # pausing and waiting for user input
        },
    )

    graph.add_edge("extract_plan", END)

    return graph.compile()


def run():
    print("\nWelcome to PartyHat! I'll help you plan your smart contract.")
    print("   (type 'quit' to exit)\n")

    app = build_graph()
    state = {"messages": [], "plan_ready": False, "final_plan": None}

    state = app.invoke(state)
    print(f"Agent: {state['messages'][-1].content}\n")

    while not state.get("plan_ready") or state.get("final_plan") is None:
        user_input = input("You: ").strip()

        if user_input.lower() == "quit":
            print("See you later!")
            break

        if not user_input:
            continue

        state["messages"].append(HumanMessage(content=user_input))
        state = app.invoke(state)

        last_msg = state["messages"][-1]
        if hasattr(last_msg, "content") and "PLAN_READY" not in last_msg.content:
            print(f"\nAgent: {last_msg.content}\n")

        if state.get("final_plan"):
            print("\n" + "=" * 50)
            print("Smart Contract Plan Generated!")
            print("=" * 50)
            print(json.dumps(state["final_plan"], indent=2))
            print("=" * 50)

            mm = MemoryManager()
            mm.save_plan(state["final_plan"])
            break


if __name__ == "__main__":
    run()
