import uuid
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from agents.planning_agent import build_planning_agent, chat
from agents.memory_manager import MemoryManager
from schemas.plan_schema import PlanStatus

load_dotenv()

app = FastAPI(
    title="PartyHat API",
    description="AI-powered smart contract planning agent",
    version="0.1.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


planning_agent = build_planning_agent()
memory_manager = MemoryManager()


class StartSessionResponse(BaseModel):
    session_id: str
    message: str  # agent's opening message


class MessageRequest(BaseModel):
    session_id: str
    message: str


class MessageResponse(BaseModel):
    session_id: str
    response: str
    tool_calls: list[str]  # which tools were called


class PlanResponse(BaseModel):
    session_id: str
    plan: Optional[dict]
    status: Optional[str]


class ApproveRequest(BaseModel):
    session_id: str


class ApproveResponse(BaseModel):
    session_id: str
    success: bool
    message: str


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "partyhat-agents"}


@app.post("/plan/start", response_model=StartSessionResponse)
async def start_session():
    """
    Creates a unique session_id and sends the user's first message to the agent.
    The frontend will store the session_id and use it for all subsequent calls.

    Returns the agent's opening message.
    """
    session_id = str(uuid.uuid4())

    try:
        result = chat(
            agent=planning_agent,
            session_id=session_id,
            user_message="Hello, I want to plan a new smart contract.",
        )
        return StartSessionResponse(
            session_id=session_id,
            message=result["response"],
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not start session: {str(e)}"
        )


@app.post("/plan/message", response_model=MessageResponse)
async def send_message(request: MessageRequest):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        result = chat(
            agent=planning_agent,
            session_id=request.session_id,
            user_message=request.message,
        )
        return MessageResponse(
            session_id=result["session_id"],
            response=result["response"],
            tool_calls=result["tool_calls"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")


@app.get("/plan/current", response_model=PlanResponse)
async def get_current_plan(session_id: str):
    try:
        plan = memory_manager.get_plan()
        if plan:
            return PlanResponse(
                session_id=session_id,
                plan=plan,
                status=plan.get("status", PlanStatus.DRAFT.value),
            )
        return PlanResponse(session_id=session_id, plan=None, status=None)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not retrieve plan: {str(e)}"
        )


@app.post("/plan/approve", response_model=ApproveResponse)
async def approve_plan(request: ApproveRequest):

    try:
        plan = memory_manager.get_plan()

        if not plan:
            raise HTTPException(status_code=404, detail="No plan found to approve")

        if plan.get("status") == PlanStatus.DEPLOYED.value:
            raise HTTPException(
                status_code=400,
                detail="Contract is deployed on-chain and cannot be modified",
            )

        plan["status"] = PlanStatus.READY.value
        memory_manager.save_plan(plan)

        return ApproveResponse(
            session_id=request.session_id,
            success=True,
            message=f"Plan approved. Project '{plan['project_name']}' is ready for code generation.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not approve plan: {str(e)}")
