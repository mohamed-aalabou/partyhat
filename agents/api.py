import json
import uuid
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from agents.planning_agent import build_planning_agent, chat
from agents.agent_registry import chat_with_intent, stream_chat_with_intent
from agents.memory_manager import MemoryManager
from agents.context import set_project_context, get_project_context
from agents.db import get_session, create_tables, async_session_factory
from agents.db.crud import (
    create_user as db_create_user,
    create_project as db_create_project,
    get_project as db_get_project,
    get_user_by_wallet as db_get_user_by_wallet,
    list_projects_by_user,
    update_project as db_update_project,
)
from agents.db.models import User, Project
from schemas.plan_schema import PlanStatus
from agents.planning_tools import load_planning_tools, set_planning_mcp_tools
from schemas.coding_schema import CodeGenerationRequest
from agents.coding_tools import generate_solidity_code_direct
from agents.code_storage import get_code_storage
from sqlalchemy.ext.asyncio import AsyncSession

load_dotenv()

app = FastAPI(
    title="PartyHat API",
    description="AI-powered smart contract planning agent",
    version="0.1.0",
)

origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "https://partyhat-app.vercel.app",
    "https://partyhat-backend.onrender.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


planning_agent = build_planning_agent()


@app.on_event("startup")
async def load_mcp_tools_startup() -> None:
    """
    FastAPI startup hook to load OpenZeppelin MCP tools and inject them
    into the global PLANNING_TOOLS used by the planning agent.
    """
    tools = await load_planning_tools()
    set_planning_mcp_tools(tools)


@app.on_event("startup")
async def db_startup() -> None:
    """Create Neon DB tables on startup."""
    await create_tables()


async def ensure_project_context(
    project_id: str,
    user_id: str,
    session: AsyncSession | None,
) -> None:
    """
    Validate project belongs to user (when not default) and set context vars.
    """
    if project_id != "default" and user_id != "default":
        if session is None:
            raise HTTPException(
                status_code=503,
                detail="DATABASE_URL required for project-scoped features",
            )
        try:
            proj = await db_get_project(
                session, uuid.UUID(project_id), user_id=uuid.UUID(user_id)
            )
            if not proj:
                raise HTTPException(
                    status_code=404,
                    detail="Project not found or does not belong to this user",
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid project_id or user_id format")
    set_project_context(project_id, user_id)


def _parse_project_uuid(project_id: str) -> uuid.UUID | None:
    if not project_id or project_id == "default":
        return None
    try:
        return uuid.UUID(project_id)
    except ValueError:
        return None


async def _append_chat_message(
    session: AsyncSession | None,
    project_id: str,
    session_id: str,
    sender: str,
    content: str,
) -> None:
    if session is None:
        return
    project_uuid = _parse_project_uuid(project_id)
    if project_uuid is None:
        return
    if sender not in ("user", "agent"):
        return
    if not content or not content.strip():
        return
    from agents.db.crud import append_message as db_append_message

    await db_append_message(
        session,
        project_id=project_uuid,
        session_id=session_id,
        sender=sender,
        content=content,
    )


async def _append_chat_message_new_session(
    project_uuid: uuid.UUID | None,
    session_id: str,
    sender: str,
    content: str,
) -> None:
    """
    Streaming responses outlive the request lifecycle; use a fresh DB session.
    """
    if project_uuid is None:
        return
    if sender not in ("user", "agent"):
        return
    if not content or not content.strip():
        return
    if not os.getenv("DATABASE_URL"):
        return
    from agents.db.crud import append_message as db_append_message

    async with async_session_factory() as db_session:
        await db_append_message(
            db_session,
            project_id=project_uuid,
            session_id=session_id,
            sender=sender,
            content=content,
        )


class AnswerRecommendationResponse(BaseModel):
    text: str
    recommended: Optional[bool] = None


class StartSessionRequest(BaseModel):
    project_id: Optional[str] = None
    user_id: Optional[str] = None


class StartSessionResponse(BaseModel):
    session_id: str
    message: str  # agent's opening message
    answer_recommendations: List[AnswerRecommendationResponse] = []


class CreateUserResponse(BaseModel):
    user_id: str


class CreateProjectRequest(BaseModel):
    user_id: str
    name: Optional[str] = None
    screenshot_base64: Optional[str] = None


class CreateProjectResponse(BaseModel):
    project_id: str


class ProjectResponse(BaseModel):
    id: str
    user_id: str
    name: Optional[str]
    screenshot_base64: Optional[str]
    created_at: str


class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    screenshot_base64: Optional[str] = None


class RequestContext(BaseModel):
    """
    Request-scoped project/user identifiers, typically resolved from headers.

    X-Project-Id / X-User-Id headers are the primary source; when absent,
    callers can still override via body or query parameters for backwards
    compatibility.
    """

    project_id: str = "default"
    user_id: str = "default"


async def get_request_context(
    x_project_id: Optional[str] = Header(default=None, alias="X-Project-Id"),
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
) -> RequestContext:
    """
    FastAPI dependency to resolve project/user IDs from headers.

    This does not perform DB validation or set contextvars itself; callers
    should pass the resolved IDs into ensure_project_context(), which handles
    both default and project-scoped behavior.
    """
    project_id = x_project_id or "default"
    user_id = x_user_id or "default"
    return RequestContext(project_id=project_id, user_id=user_id)


class MessageRequest(BaseModel):
    session_id: str
    message: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None


class MessageResponse(BaseModel):
    session_id: str
    response: str
    tool_calls: list[str]  # which tools were called
    answer_recommendations: List[AnswerRecommendationResponse] = []


class PlanResponse(BaseModel):
    plan: Optional[dict]
    status: Optional[str]


class ApproveRequest(BaseModel):
    session_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None


class ApproveResponse(BaseModel):
    session_id: str
    success: bool
    message: str


class RoutedMessageRequest(BaseModel):
    session_id: str
    intent: str
    message: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None


class CodeGenerationResponse(BaseModel):
    generated_code: str
    goal: str


class CodeArtifactsResponse(BaseModel):
    artifacts: List[Dict[str, Any]]


class DeploymentCurrentResponse(BaseModel):
    """Current deployment state: last deploy results for this user/project."""

    last_deploy_results: List[Dict[str, Any]]


class TestingCurrentResponse(BaseModel):
    """Current testing state: last test results for this user/project."""

    last_test_results: List[Dict[str, Any]]


class ArtifactTreeNode(BaseModel):
    name: str
    path: str
    type: str  # "file" or "directory"
    children: Optional[List["ArtifactTreeNode"]] = None


ArtifactTreeNode.update_forward_refs()


class ArtifactFileResponse(BaseModel):
    path: str
    content: str


class MemorySnapshotResponse(BaseModel):
    """
    Debug helper response returning the full project-scoped user memory block
    and the global agent log block as stored in Letta.
    """

    user_block_label: str
    user_memory: Dict[str, Any]
    global_block_label: str
    global_memory: Dict[str, Any]


class ChatMessageResponse(BaseModel):
    id: str
    project_id: str
    session_id: str
    sender: str
    content: str
    created_at: str


class ListMessagesResponse(BaseModel):
    messages: List[ChatMessageResponse]


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "partyhat-agents"}


@app.get("/messages", response_model=ListMessagesResponse)
async def list_messages_endpoint(
    session_id: Optional[str] = None,
    limit: int = 200,
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)

    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")

    project_uuid = _parse_project_uuid(effective_project_id)
    if project_uuid is None:
        raise HTTPException(status_code=400, detail="project_id is required")

    from agents.db.crud import list_messages as db_list_messages

    rows = await db_list_messages(
        session,
        project_id=project_uuid,
        session_id=session_id,
        limit=limit,
    )
    return ListMessagesResponse(
        messages=[
            ChatMessageResponse(
                id=str(r.id),
                project_id=str(r.project_id),
                session_id=r.session_id,
                sender=r.sender,
                content=r.content,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ]
    )


@app.post("/users", response_model=CreateUserResponse)
async def create_user_endpoint(
    wallet: str,
    session: AsyncSession | None = Depends(get_session),
):
    """Create or get user by wallet. Requires wallet. If wallet is already linked, returns that user_id; otherwise creates a new user and links the wallet."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    existing = await db_get_user_by_wallet(session, wallet)
    if existing is not None:
        return CreateUserResponse(user_id=str(existing.id))
    user = await db_create_user(session, wallet=wallet)
    return CreateUserResponse(user_id=str(user.id))


@app.post("/projects", response_model=CreateProjectResponse)
async def create_project_endpoint(
    request: CreateProjectRequest,
    session: AsyncSession | None = Depends(get_session),
):
    """Create a new project for the given user. Returns project_id."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        user_uuid = uuid.UUID(request.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    project = await db_create_project(
        session,
        user_id=user_uuid,
        name=request.name,
        screenshot_base64=request.screenshot_base64,
    )
    return CreateProjectResponse(project_id=str(project.id))


@app.get("/projects", response_model=List[ProjectResponse])
async def list_projects_endpoint(
    user_id: str,
    session: AsyncSession | None = Depends(get_session),
):
    """List all projects for a user."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    projects = await list_projects_by_user(session, user_uuid)
    return [
        ProjectResponse(
            id=str(p.id),
            user_id=str(p.user_id),
            name=p.name,
            screenshot_base64=p.screenshot_base64,
            created_at=p.created_at.isoformat(),
        )
        for p in projects
    ]


@app.get("/users/{user_id}/projects", response_model=List[ProjectResponse])
async def list_projects_by_user_endpoint(
    user_id: str,
    session: AsyncSession | None = Depends(get_session),
):
    """List all projects for a user (alias endpoint)."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    projects = await list_projects_by_user(session, user_uuid)
    return [
        ProjectResponse(
            id=str(p.id),
            user_id=str(p.user_id),
            name=p.name,
            screenshot_base64=p.screenshot_base64,
            created_at=p.created_at.isoformat(),
        )
        for p in projects
    ]


@app.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project_endpoint(
    project_id: str,
    user_id: str,
    session: AsyncSession | None = Depends(get_session),
):
    """Get a project by id. Validates ownership when user_id is provided."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        proj_uuid = uuid.UUID(project_id)
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id or user_id format")
    project = await db_get_project(session, proj_uuid, user_id=user_uuid)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse(
        id=str(project.id),
        user_id=str(project.user_id),
        name=project.name,
        screenshot_base64=project.screenshot_base64,
        created_at=project.created_at.isoformat(),
    )


@app.patch("/projects/{project_id}", response_model=ProjectResponse)
async def update_project_endpoint(
    project_id: str,
    request: UpdateProjectRequest,
    session: AsyncSession | None = Depends(get_session),
):
    """Update project name by project id."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        proj_uuid = uuid.UUID(project_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid project_id format")

    update_data = request.model_dump(exclude_unset=True)
    project = await db_update_project(
        session,
        proj_uuid,
        name=update_data.get("name"),
        screenshot_base64=update_data.get("screenshot_base64"),
        set_name="name" in update_data,
        set_screenshot_base64="screenshot_base64" in update_data,
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return ProjectResponse(
        id=str(project.id),
        user_id=str(project.user_id),
        name=project.name,
        screenshot_base64=project.screenshot_base64,
        created_at=project.created_at.isoformat(),
    )


@app.post("/plan/start", response_model=StartSessionResponse)
async def start_session(
    request: StartSessionRequest | None = None,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Creates a unique session_id and sends the user's first message to the agent.
    The frontend will store the session_id and use it for all subsequent calls.
    Pass project_id and user_id for project-scoped memory and sandbox.
    """
    session_id = str(uuid.uuid4())
    body_project_id = request.project_id if request else None
    body_user_id = request.user_id if request else None
    project_id = body_project_id or ctx.project_id
    user_id = body_user_id or ctx.user_id
    await ensure_project_context(project_id, user_id, session)

    try:
        result = chat(
            agent=planning_agent,
            session_id=session_id,
            user_message="Hello, I want to plan a new smart contract.",
            project_id=project_id if project_id != "default" else None,
        )
        return StartSessionResponse(
            session_id=session_id,
            message=result["response"],
            answer_recommendations=result.get("answer_recommendations", []),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not start session: {str(e)}"
        )


@app.post("/plan/message", response_model=MessageResponse)
async def send_message(
    request: MessageRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    project_id = request.project_id or ctx.project_id
    user_id = request.user_id or ctx.user_id
    await ensure_project_context(project_id, user_id, session)

    try:
        await _append_chat_message(
            session=session,
            project_id=project_id,
            session_id=request.session_id,
            sender="user",
            content=request.message,
        )
        result = chat(
            agent=planning_agent,
            session_id=request.session_id,
            user_message=request.message,
            project_id=project_id if project_id != "default" else None,
        )
        await _append_chat_message(
            session=session,
            project_id=project_id,
            session_id=request.session_id,
            sender="agent",
            content=result.get("response", ""),
        )
        return MessageResponse(
            session_id=result["session_id"],
            response=result["response"],
            tool_calls=result["tool_calls"],
            answer_recommendations=result.get("answer_recommendations", []),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")


@app.get("/plan/current", response_model=PlanResponse)
async def get_current_plan(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    try:
        mm = MemoryManager(
            user_id=effective_user_id,
            project_id=effective_project_id if effective_project_id != "default" else None,
        )
        plan = mm.get_plan()
        if plan:
            return PlanResponse(
                plan=plan,
                status=plan.get("status", PlanStatus.DRAFT.value),
            )
        return PlanResponse(plan=None, status=None)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not retrieve plan: {str(e)}"
        )


@app.get("/coding/current", response_model=CodeArtifactsResponse)
async def get_current_code_artifacts(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the current list of code artifacts for this user/project.
    Mirrors the behavior of get_current_artifacts() from coding_tools.
    """
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    try:
        mm = MemoryManager(
            user_id=effective_user_id,
            project_id=effective_project_id if effective_project_id != "default" else None,
        )
        state = mm.get_agent_state("coding")
        artifacts = state.get("artifacts", [])
        return CodeArtifactsResponse(artifacts=artifacts)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve code artifacts: {str(e)}",
        )


@app.get("/deployment/current", response_model=DeploymentCurrentResponse)
async def get_current_deployment(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the last deploy results for this user/project.
    Mirrors the behavior of get_deployment_history() from deployment_tools (last_deploy_results slice).
    """
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    try:
        mm = MemoryManager(
            user_id=effective_user_id,
            project_id=effective_project_id if effective_project_id != "default" else None,
        )
        state = mm.get_agent_state("deployment")
        last_deploy_results = state.get("last_deploy_results", [])
        return DeploymentCurrentResponse(last_deploy_results=last_deploy_results)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve deployment state: {str(e)}",
        )


@app.get("/testing/current", response_model=TestingCurrentResponse)
async def get_current_test_results(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the last test results for this user/project.
    Mirrors the testing agent state (last_test_results from run_foundry_tests).
    """
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    try:
        mm = MemoryManager(
            user_id=effective_user_id,
            project_id=effective_project_id if effective_project_id != "default" else None,
        )
        state = mm.get_agent_state("testing")
        last_test_results = state.get("last_test_results", [])
        return TestingCurrentResponse(last_test_results=last_test_results)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve test results: {str(e)}",
        )


@app.post("/plan/approve", response_model=ApproveResponse)
async def approve_plan(
    request: ApproveRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    project_id = request.project_id or ctx.project_id
    user_id = request.user_id or ctx.user_id
    await ensure_project_context(project_id, user_id, session)

    try:
        mm = MemoryManager(user_id=user_id, project_id=project_id if project_id != "default" else None)
        plan = mm.get_plan()

        if not plan:
            raise HTTPException(status_code=404, detail="No plan found to approve")

        if plan.get("status") == PlanStatus.DEPLOYED.value:
            raise HTTPException(
                status_code=400,
                detail="Contract is deployed on-chain and cannot be modified",
            )

        plan["status"] = PlanStatus.READY.value
        mm.save_plan(plan)

        return ApproveResponse(
            session_id=request.session_id,
            success=True,
            message=f"Plan approved. Project '{plan['project_name']}' is ready for code generation.",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not approve plan: {str(e)}")


def _build_artifact_tree(project_id: str | None = None) -> ArtifactTreeNode:
    storage = get_code_storage(project_id=project_id)
    base_root = os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts").strip("/") or "generated_contracts"
    if project_id:
        base_root = f"{base_root}/{project_id}"

    try:
        file_paths = storage.list_paths()

        root = {"name": Path(base_root).name or "artifacts", "children": {}, "type": "directory"}

        for rel_path in sorted(file_paths):
            parts = [p for p in Path(rel_path).parts if p]
            cursor = root
            cumulative: list[str] = []
            for idx, part in enumerate(parts):
                cumulative.append(part)
                is_file = idx == len(parts) - 1
                children = cursor.setdefault("children", {})
                if part not in children:
                    children[part] = {
                        "name": part,
                        "path": "/".join(cumulative),
                        "type": "file" if is_file else "directory",
                        "children": {} if not is_file else None,
                    }
                cursor = children[part]

        def to_node(node: dict) -> ArtifactTreeNode:
            if node["type"] == "file":
                return ArtifactTreeNode(
                    name=node["name"],
                    path=node.get("path", node["name"]),
                    type="file",
                    children=None,
                )
            raw_children = list((node.get("children") or {}).values())
            children_nodes = sorted(
                [to_node(child) for child in raw_children],
                key=lambda n: (n.type != "directory", n.name),
            )
            return ArtifactTreeNode(
                name=node["name"],
                path=node.get("path", ""),
                type="directory",
                children=children_nodes,
            )

        return to_node(root)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not build artifact tree: {str(e)}",
        )


@app.get("/artifacts/tree", response_model=ArtifactTreeNode)
async def get_artifact_tree(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the directory tree structure of generated artifacts.
    Scoped to project when project_id is provided.
    """
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    pid = effective_project_id if effective_project_id != "default" else None
    return _build_artifact_tree(project_id=pid)


@app.get("/artifacts/file", response_model=ArtifactFileResponse)
async def get_artifact_file(
    relative_path: str,
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return raw artifact file content by relative path.
    Scoped to project when project_id is provided.
    """
    if not relative_path.strip():
        raise HTTPException(status_code=400, detail="relative_path cannot be empty")

    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)
    pid = effective_project_id if effective_project_id != "default" else None
    storage = get_code_storage(project_id=pid)
    try:
        content = storage.load_code(relative_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact file not found")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load artifact file: {str(e)}",
        )

    return ArtifactFileResponse(path=relative_path, content=content)


@app.get("/memory/full", response_model=MemorySnapshotResponse)
async def get_full_memory_snapshot(
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Return the full project-scoped user memory block and global memory block
    from Letta for debugging and observability.

    When project_id/user_id are not provided explicitly, values are resolved
    from headers via RequestContext, matching other endpoints.
    """
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)

    try:
        mm = MemoryManager(
            user_id=effective_user_id,
            project_id=effective_project_id if effective_project_id != "default" else None,
        )
        # Use the MemoryManager's helpers to read the raw Letta blocks.
        user_data, _ = mm._read_user_block()  # type: ignore[attr-defined]
        global_data, _ = mm._read_global_block()  # type: ignore[attr-defined]

        return MemorySnapshotResponse(
            user_block_label=mm.user_block_label,
            user_memory=user_data,
            global_block_label=mm.global_block_label,
            global_memory=global_data,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Could not retrieve memory from Letta: {str(e)}"
        )


@app.post("/agent/message", response_model=MessageResponse)
async def routed_message(
    request: RoutedMessageRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    """
    Generic entrypoint that routes the message to the appropriate agent based on intent.
    Pass project_id and user_id for project-scoped memory and sandbox.
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    project_id = request.project_id or ctx.project_id
    user_id = request.user_id or ctx.user_id
    await ensure_project_context(project_id, user_id, session)

    try:
        await _append_chat_message(
            session=session,
            project_id=project_id,
            session_id=request.session_id,
            sender="user",
            content=request.message,
        )
        result = chat_with_intent(
            intent=request.intent,
            session_id=request.session_id,
            user_message=request.message,
            project_id=project_id if project_id != "default" else None,
        )
        await _append_chat_message(
            session=session,
            project_id=project_id,
            session_id=request.session_id,
            sender="agent",
            content=result.get("response", ""),
        )
        return MessageResponse(
            session_id=result["session_id"],
            response=result["response"],
            tool_calls=result["tool_calls"],
            answer_recommendations=result.get("answer_recommendations", []),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent error: {str(e)}")


@app.post("/agent/message/stream")
async def routed_message_stream(
    request: RoutedMessageRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    """
    Stream agent responses and tool calls via Server-Sent Events.
    Same request body as /agent/message; events are JSON objects with type:
    "step" (content, tool_calls) and "done" (session_id, response, tool_calls).
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    project_id = request.project_id or ctx.project_id
    user_id = request.user_id or ctx.user_id
    await ensure_project_context(project_id, user_id, session)

    project_uuid = _parse_project_uuid(project_id)
    await _append_chat_message(
        session=session,
        project_id=project_id,
        session_id=request.session_id,
        sender="user",
        content=request.message,
    )

    async def event_stream():
        try:
            async for event in stream_chat_with_intent(
                intent=request.intent,
                session_id=request.session_id,
                user_message=request.message,
                project_id=project_id if project_id != "default" else None,
            ):
                if event.get("type") == "done":
                    await _append_chat_message_new_session(
                        project_uuid=project_uuid,
                        session_id=request.session_id,
                        sender="agent",
                        content=event.get("response", "") or "",
                    )
                yield f"data: {json.dumps(event)}\n\n"
        except ValueError as e:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/coding/generate", response_model=CodeGenerationResponse)
async def generate_solidity_endpoint(
    request: CodeGenerationRequest,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Lightweight endpoint to exercise the generate_solidity_code tool directly.
    Pass project_id and user_id for project-scoped context (optional).
    """
    if not request.goal.strip():
        raise HTTPException(status_code=400, detail="Goal cannot be empty")

    pid = project_id or ctx.project_id
    uid = user_id or ctx.user_id
    await ensure_project_context(pid, uid, session)

    try:
        # Call the direct helper, which encapsulates all generation logic.
        result = generate_solidity_code_direct(request)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Code generation failed: {str(e)}"
        )

    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    generated = result.get("generated_code", "")
    return CodeGenerationResponse(generated_code=generated, goal=request.goal)
