import asyncio
import json
import uuid
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Header, Query, Request
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
    get_user_by_id as db_get_user_by_id,
    project_access_exists as db_project_access_exists,
    get_user_by_wallet as db_get_user_by_wallet,
    get_pipeline_run as db_get_pipeline_run,
    create_telegram_link_token,
    delete_telegram_user_link,
    delete_unused_telegram_link_tokens,
    get_telegram_user_link,
    list_pipeline_human_gates,
    list_projects_by_user,
    update_project as db_update_project,
)
from agents.db.models import User, Project
from schemas.plan_schema import PlanStatus
from agents.planning_tools import load_planning_tools, set_planning_mcp_tools
from schemas.coding_schema import CodeGenerationRequest
from agents.coding_tools import generate_solidity_code_direct
from agents.code_storage import get_code_storage
from agents.project_state import (
    get_code_state,
    get_deployment_state,
    get_plan_state,
    get_project_state_snapshot,
    get_project_state_versions,
)
from sqlalchemy.ext.asyncio import AsyncSession

from agents.pipeline_orchestrator import get_pipeline_status
from agents.pipeline_cancel import cancel_pipeline_run
from agents.db import async_session_factory
from agents.pipeline_runtime import (
    build_pipeline_control_response,
    get_pipeline_run_poll_record,
    get_pipeline_run_record,
    list_serialized_pipeline_run_events,
    spawn_detached_pipeline_runner,
)
from agents.telegram_service import (
    TelegramApiError,
    build_telegram_deep_link,
    configure_telegram_webhook,
    generate_telegram_connect_token,
    get_telegram_bot_display_name,
    get_telegram_bot_username,
    get_telegram_connect_token_ttl_seconds,
    get_telegram_webhook_secret,
    handle_telegram_webhook_update,
    hash_telegram_connect_token,
    start_notification_dispatcher,
    stop_notification_dispatcher,
)
from agents.tracing import configure_tracing

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
    "https://app.partyhat.dev",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


planning_agent = build_planning_agent()
_ARTIFACT_TREE_CACHE: dict[tuple[str, str], Any] = {}


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
    configure_tracing()


@app.on_event("startup")
async def telegram_startup() -> None:
    try:
        webhook_result = await configure_telegram_webhook()
        if webhook_result.get("configured") is False:
            print(f"[Telegram] Webhook setup skipped: {webhook_result.get('reason')}")
    except Exception as exc:
        print(f"[Telegram] Webhook setup failed: {exc}")
    await start_notification_dispatcher()


@app.on_event("shutdown")
async def telegram_shutdown() -> None:
    await stop_notification_dispatcher()


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
            has_access = await db_project_access_exists(
                session,
                uuid.UUID(project_id),
                uuid.UUID(user_id),
            )
            if not has_access:
                raise HTTPException(
                    status_code=404,
                    detail="Project not found or does not belong to this user",
                )
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid project_id or user_id format"
            )
    set_project_context(project_id, user_id)


def _parse_project_uuid(project_id: str) -> uuid.UUID | None:
    if not project_id or project_id == "default":
        return None
    try:
        return uuid.UUID(project_id)
    except ValueError:
        return None


def _parse_user_uuid(user_id: str) -> uuid.UUID | None:
    if not user_id or user_id == "default":
        return None
    try:
        return uuid.UUID(user_id)
    except ValueError:
        return None


async def _require_real_user(
    session: AsyncSession | None,
    user_id: str,
):
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    user_uuid = _parse_user_uuid(user_id)
    if user_uuid is None:
        raise HTTPException(status_code=400, detail="X-User-Id is required")
    user = await db_get_user_by_id(session, user_uuid)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _coerce_bool_query(value) -> bool:
    if isinstance(value, bool):
        return value
    default = getattr(value, "default", None)
    if isinstance(default, bool):
        return default
    return bool(value)


def _compact_execution_history(
    entries: list[dict],
    *,
    drop_output: bool = False,
) -> list[dict]:
    compacted: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        compact = dict(entry)
        compact.pop("stdout", None)
        compact.pop("stderr", None)
        if drop_output:
            compact.pop("output", None)
        compacted.append(compact)
    return compacted


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


class PendingQuestionResponse(BaseModel):
    question: str
    answer_recommendations: List[AnswerRecommendationResponse] = []


class StartSessionRequest(BaseModel):
    project_id: Optional[str] = None
    user_id: Optional[str] = None


class StartSessionResponse(BaseModel):
    session_id: str
    message: str  # agent's opening message
    answer_recommendations: List[AnswerRecommendationResponse] = []
    pending_questions: List[PendingQuestionResponse] = []


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


class TelegramLinkResponse(BaseModel):
    linked: bool
    bot_username: str
    bot_display_name: str
    deep_link_url: str
    expires_at: str


class TelegramStatusResponse(BaseModel):
    linked: bool
    enabled: bool
    bot_username: str
    bot_display_name: str
    chat_username: Optional[str] = None
    linked_at: Optional[str] = None


class TelegramUnlinkResponse(BaseModel):
    success: bool


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
    pending_questions: List[PendingQuestionResponse] = []


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
    include_screenshot: bool = False,
    session: AsyncSession | None = Depends(get_session),
):
    """List all projects for a user."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    projects = await list_projects_by_user(
        session,
        user_uuid,
        include_screenshot=include_screenshot,
    )
    return [
        ProjectResponse(
            id=str(p.id),
            user_id=str(p.user_id),
            name=p.name,
            screenshot_base64=p.screenshot_base64 if include_screenshot else None,
            created_at=p.created_at.isoformat(),
        )
        for p in projects
    ]


@app.get("/users/{user_id}/projects", response_model=List[ProjectResponse])
async def list_projects_by_user_endpoint(
    user_id: str,
    include_screenshot: bool = False,
    session: AsyncSession | None = Depends(get_session),
):
    """List all projects for a user (alias endpoint)."""
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    try:
        user_uuid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user_id format")
    projects = await list_projects_by_user(
        session,
        user_uuid,
        include_screenshot=include_screenshot,
    )
    return [
        ProjectResponse(
            id=str(p.id),
            user_id=str(p.user_id),
            name=p.name,
            screenshot_base64=p.screenshot_base64 if include_screenshot else None,
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
        raise HTTPException(
            status_code=400, detail="Invalid project_id or user_id format"
        )
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


@app.post("/integrations/telegram/link", response_model=TelegramLinkResponse)
async def telegram_link(
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    user = await _require_real_user(session, ctx.user_id)

    bot_username = get_telegram_bot_username()
    if not bot_username:
        raise HTTPException(
            status_code=503,
            detail="Telegram bot username is not configured",
        )

    existing_link = await get_telegram_user_link(session, user.id)
    await delete_unused_telegram_link_tokens(session, user.id)

    connect_token = generate_telegram_connect_token()
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=get_telegram_connect_token_ttl_seconds()
    )
    await create_telegram_link_token(
        session,
        user_id=user.id,
        token_hash=hash_telegram_connect_token(connect_token),
        expires_at=expires_at,
    )
    try:
        deep_link_url = build_telegram_deep_link(connect_token)
    except TelegramApiError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    return TelegramLinkResponse(
        linked=existing_link is not None and existing_link.enabled,
        bot_username=bot_username,
        bot_display_name=get_telegram_bot_display_name(),
        deep_link_url=deep_link_url,
        expires_at=expires_at.isoformat(),
    )


@app.get("/integrations/telegram/status", response_model=TelegramStatusResponse)
async def telegram_status(
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    user = await _require_real_user(session, ctx.user_id)
    link = await get_telegram_user_link(session, user.id)
    return TelegramStatusResponse(
        linked=link is not None,
        enabled=bool(link.enabled) if link is not None else False,
        bot_username=get_telegram_bot_username(),
        bot_display_name=get_telegram_bot_display_name(),
        chat_username=(link.chat_username if link is not None else None),
        linked_at=(link.linked_at.isoformat() if link is not None else None),
    )


@app.delete("/integrations/telegram/link", response_model=TelegramUnlinkResponse)
async def telegram_unlink(
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession | None = Depends(get_session),
):
    user = await _require_real_user(session, ctx.user_id)
    await delete_unused_telegram_link_tokens(session, user.id)
    deleted = await delete_telegram_user_link(session, user.id)
    return TelegramUnlinkResponse(success=deleted or True)


@app.post("/integrations/telegram/webhook")
async def telegram_webhook(
    update: Dict[str, Any],
    x_telegram_bot_api_secret_token: Optional[str] = Header(
        default=None,
        alias="X-Telegram-Bot-Api-Secret-Token",
    ),
    session: AsyncSession | None = Depends(get_session),
):
    expected_secret = get_telegram_webhook_secret()
    if not expected_secret or x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")
    if session is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL required")
    result = await handle_telegram_webhook_update(update)
    return {"ok": True, **result}


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
            pending_questions=result.get("pending_questions", []),
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
            pending_questions=result.get("pending_questions", []),
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
            project_id=(
                effective_project_id if effective_project_id != "default" else None
            ),
        )
        plan_state = get_plan_state(mm)
        return PlanResponse(
            plan=plan_state["plan"],
            status=plan_state["status"],
        )
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
            project_id=(
                effective_project_id if effective_project_id != "default" else None
            ),
        )
        code_state = get_code_state(mm)
        return CodeArtifactsResponse(artifacts=code_state["artifacts"])
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
            project_id=(
                effective_project_id if effective_project_id != "default" else None
            ),
        )
        deployment_state = get_deployment_state(mm)
        return DeploymentCurrentResponse(
            last_deploy_results=deployment_state["last_deploy_results"]
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not retrieve deployment state: {str(e)}",
        )


@app.get("/testing/current", response_model=TestingCurrentResponse)
async def get_current_test_results(
    project_id: str = "default",
    user_id: str = "default",
    include_output: bool = False,
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
            project_id=(
                effective_project_id if effective_project_id != "default" else None
            ),
        )
        last_test_results = _compact_execution_history(
            mm.list_test_runs(limit=20, include_output=include_output),
            drop_output=not include_output,
        )
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
        mm = MemoryManager(
            user_id=user_id, project_id=project_id if project_id != "default" else None
        )
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


def _artifact_tree_from_paths(
    file_paths: list[str],
    *,
    project_id: str | None = None,
) -> ArtifactTreeNode:
    base_root = (
        os.getenv("FOUNDRY_ARTIFACT_ROOT", "generated_contracts").strip("/")
        or "generated_contracts"
    )
    if project_id:
        base_root = f"{base_root}/{project_id}"

    root = {
        "name": Path(base_root).name or "artifacts",
        "children": {},
        "type": "directory",
    }

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
    cache_key: tuple[str, str] | None = None
    if pid is not None:
        versions = get_project_state_versions(
            user_id=effective_user_id,
            project_id=pid,
            allow_recompute=False,
        )
        code_version = versions.get("code", "0")
        if code_version != "0":
            cache_key = (pid, code_version)
            cached = _ARTIFACT_TREE_CACHE.get(cache_key)
            if cached is not None:
                return cached
    storage = get_code_storage(project_id=pid)
    try:
        file_paths = await storage.alist_paths()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Could not build artifact tree: {str(e)}",
        )
    tree = _artifact_tree_from_paths(file_paths, project_id=pid)
    if cache_key is not None:
        _ARTIFACT_TREE_CACHE[cache_key] = tree
    return tree


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
        content = await storage.aload_code(relative_path)
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
            project_id=(
                effective_project_id if effective_project_id != "default" else None
            ),
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
            pending_questions=result.get("pending_questions", []),
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
    "step" (content, tool_calls) and "done" (session_id, response, tool_calls,
    plus planning-only UI metadata such as approval_request).
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
        raise HTTPException(status_code=500, detail=f"Code generation failed: {str(e)}")

    if isinstance(result, dict) and "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])

    generated = result.get("generated_code", "")
    return CodeGenerationResponse(generated_code=generated, goal=request.goal)


class PipelineRunRequest(BaseModel):
    project_id: str
    user_id: str


class PipelineCancelRequest(BaseModel):
    project_id: str
    pipeline_run_id: str
    reason: Optional[str] = None


class PipelineResumeRequest(BaseModel):
    project_id: str
    pipeline_run_id: str


class PipelineControlResponse(BaseModel):
    pipeline_run_id: str
    status: str
    events_url: str
    status_url: str


class GateDecisionRequest(BaseModel):
    project_id: str
    reason: Optional[str] = None


def _format_sse_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: int | str | None = None,
) -> str:
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(payload)}")
    return "\n".join(lines) + "\n\n"


def _format_pipeline_sse_event(event: dict[str, Any]) -> str:
    seq = int(event.get("seq") or 0)
    event_type = str(event.get("type") or "message")
    return _format_sse_event(event_type, event, event_id=seq)


def _current_timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@app.post("/pipeline/run", response_model=PipelineControlResponse, status_code=202)
async def run_pipeline(
    request: PipelineRunRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """
    Launch the autonomous pipeline after plan approval.
    Return run metadata immediately; consume live updates via GET /pipeline/events.
    """
    project_id = request.project_id or ctx.project_id
    user_id = request.user_id or ctx.user_id

    if project_id == "default" or user_id == "default":
        raise HTTPException(
            status_code=400,
            detail="project_id and user_id are required for pipeline execution",
        )

    await ensure_project_context(project_id, user_id, session)

    try:
        mm = MemoryManager(user_id=user_id, project_id=project_id)
        plan = mm.get_plan()
        print(f"Plan: {plan}")
        if not plan:
            raise HTTPException(
                status_code=404, detail="No plan found. Complete planning first."
            )
        plan_status = plan.get("status", "draft")
        restartable_statuses = {"ready", "generating", "testing", "deploying", "failed"}
        if plan_status not in restartable_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Plan is not ready for pipeline execution (current status: '{plan_status}'). "
                    f"Approve the plan first or restart the pipeline from a restartable status."
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not validate plan: {e}")

    start_result = await spawn_detached_pipeline_runner(
        project_id=project_id,
        user_id=user_id,
    )
    pipeline_run_id = start_result.get("pipeline_run_id")
    if not isinstance(pipeline_run_id, str):
        raise HTTPException(status_code=500, detail="Could not start the pipeline run")

    run = await get_pipeline_run_record(pipeline_run_id)
    status = run.status if run is not None else "running"
    return build_pipeline_control_response(project_id, pipeline_run_id, status)


@app.post("/pipeline/resume", response_model=PipelineControlResponse, status_code=202)
async def resume_pipeline(
    request: PipelineResumeRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    effective_project_id = request.project_id or ctx.project_id
    await ensure_project_context(effective_project_id, ctx.user_id, session)

    try:
        pipeline_run_uuid = uuid.UUID(request.pipeline_run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pipeline_run_id format")

    run = await db_get_pipeline_run(session, pipeline_run_uuid)
    if run is None or str(run.project_id) != effective_project_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")
    if run.status in {"cancelled", "completed"}:
        raise HTTPException(
            status_code=400,
            detail=f"Pipeline run is already {run.status}.",
        )

    gates = await list_pipeline_human_gates(session, pipeline_run_uuid)
    if any(gate.status == "pending" for gate in gates):
        raise HTTPException(
            status_code=409,
            detail="Pipeline run is waiting for approval. Resolve the pending gate before resuming.",
        )

    await spawn_detached_pipeline_runner(
        project_id=effective_project_id,
        user_id=ctx.user_id,
        pipeline_run_id=request.pipeline_run_id,
        reset_in_progress=True,
    )
    run = await get_pipeline_run_record(request.pipeline_run_id)
    status = run.status if run is not None else "running"
    return build_pipeline_control_response(
        effective_project_id,
        request.pipeline_run_id,
        status,
    )


@app.get("/pipeline/events")
async def pipeline_events(
    request: Request,
    project_id: str,
    pipeline_run_id: str,
    after_seq: int = Query(default=0, ge=0),
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    await ensure_project_context(effective_project_id, ctx.user_id, session)

    try:
        pipeline_run_uuid = uuid.UUID(pipeline_run_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid pipeline_run_id format")

    run = await db_get_pipeline_run(session, pipeline_run_uuid)
    if run is None or str(run.project_id) != effective_project_id:
        raise HTTPException(status_code=404, detail="Pipeline run not found")

    async def event_stream():
        cursor = after_seq
        while True:
            if await request.is_disconnected():
                break

            poll_state = await get_pipeline_run_poll_record(pipeline_run_id)
            if poll_state is None:
                break

            latest_seq = max(0, int(getattr(poll_state, "next_event_seq", 1) or 1) - 1)
            if latest_seq > cursor:
                events = await list_serialized_pipeline_run_events(
                    pipeline_run_id,
                    after_seq=cursor,
                )
                if events:
                    for event in events:
                        cursor = max(cursor, int(event.get("seq") or 0))
                        yield _format_pipeline_sse_event(event)
                    continue

            if getattr(poll_state, "status", None) in {
                "cancelled",
                "completed",
                "failed",
            }:
                break

            yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/state/stream")
async def state_stream(
    request: Request,
    project_id: str = "default",
    user_id: str = "default",
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    effective_user_id = user_id if user_id != "default" else ctx.user_id
    await ensure_project_context(effective_project_id, effective_user_id, session)

    scoped_project_id = (
        effective_project_id if effective_project_id != "default" else None
    )

    async def event_stream():
        try:
            snapshot = get_project_state_snapshot(
                user_id=effective_user_id,
                project_id=scoped_project_id,
            )
        except Exception as e:
            yield _format_sse_event(
                "error",
                {
                    "project_id": effective_project_id,
                    "detail": f"Could not load project state: {str(e)}",
                    "emitted_at": _current_timestamp_iso(),
                },
            )
            return

        yield _format_sse_event(
            "state_snapshot",
            {
                "project_id": effective_project_id,
                "plan": snapshot["plan"],
                "code": snapshot["code"],
                "deployment": snapshot["deployment"],
                "emitted_at": _current_timestamp_iso(),
            },
        )

        previous_versions = dict(snapshot["versions"])

        while True:
            if await request.is_disconnected():
                break

            await asyncio.sleep(1.0)

            if await request.is_disconnected():
                break

            try:
                next_versions = get_project_state_versions(
                    user_id=effective_user_id,
                    project_id=scoped_project_id,
                    allow_recompute=False,
                )
            except Exception as e:
                yield _format_sse_event(
                    "error",
                    {
                        "project_id": effective_project_id,
                        "detail": f"Could not refresh project state: {str(e)}",
                        "emitted_at": _current_timestamp_iso(),
                    },
                )
                return

            changed_resources = [
                resource
                for resource in ("plan", "code", "deployment")
                if next_versions[resource] != previous_versions.get(resource)
            ]
            if not changed_resources:
                yield ": keepalive\n\n"
                continue

            try:
                next_snapshot = get_project_state_snapshot(
                    user_id=effective_user_id,
                    project_id=scoped_project_id,
                )
            except Exception as e:
                yield _format_sse_event(
                    "error",
                    {
                        "project_id": effective_project_id,
                        "detail": f"Could not refresh project state: {str(e)}",
                        "emitted_at": _current_timestamp_iso(),
                    },
                )
                return

            emitted_at = _current_timestamp_iso()
            for resource, event_name in (
                ("plan", "plan_updated"),
                ("code", "code_updated"),
                ("deployment", "deployment_updated"),
            ):
                if resource not in changed_resources:
                    continue
                yield _format_sse_event(
                    event_name,
                    {
                        "project_id": effective_project_id,
                        "plan": next_snapshot["plan"],
                        "code": next_snapshot["code"],
                        "deployment": next_snapshot["deployment"],
                        "emitted_at": emitted_at,
                    },
                )

            previous_versions = next_versions

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/pipeline/status")
async def pipeline_status(
    project_id: str,
    pipeline_run_id: str | None = None,
    summary_only: bool = Query(default=False),
    include_tasks: bool = Query(default=True),
    include_gates: bool = Query(default=True),
    include_evaluations: bool = Query(default=True),
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """Return the task history for a pipeline run."""
    effective_project_id = project_id if project_id != "default" else ctx.project_id
    summary_only = _coerce_bool_query(summary_only)
    include_tasks = _coerce_bool_query(include_tasks)
    include_gates = _coerce_bool_query(include_gates)
    include_evaluations = _coerce_bool_query(include_evaluations)
    await ensure_project_context(effective_project_id, ctx.user_id, session)

    if not pipeline_run_id:
        try:
            async with async_session_factory() as db_session:
                from agents.db.crud import get_latest_pipeline_run_id
                import uuid as _uuid

                run_id = await get_latest_pipeline_run_id(
                    db_session, _uuid.UUID(effective_project_id)
                )
                if not run_id:
                    return {"error": "No pipeline runs found for this project"}
                pipeline_run_id = str(run_id)
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Could not find pipeline run: {e}"
            )

    if summary_only:
        include_tasks = False
        include_gates = False
        include_evaluations = False

    result = await get_pipeline_status(
        effective_project_id,
        ctx.user_id,
        pipeline_run_id,
        include_tasks=include_tasks,
        include_gates=include_gates,
        include_evaluations=include_evaluations,
    )
    if "error" in result:
        raise HTTPException(status_code=500, detail=result["error"])
    return result


@app.post("/pipeline/cancel")
async def cancel_pipeline(
    request: PipelineCancelRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    """Cancel a running pipeline."""
    await ensure_project_context(request.project_id, ctx.user_id, session)
    success = cancel_pipeline_run(request.pipeline_run_id, reason=request.reason)
    return {
        "success": success,
        "message": "Cancellation requested. The pipeline will stop at the next safe point.",
        "pipeline_run_id": request.pipeline_run_id,
        "reason": request.reason,
    }


async def _resolve_gate_transition(
    *,
    gate_id: str,
    action: str,
    request: GateDecisionRequest,
    ctx: RequestContext,
    session: AsyncSession,
):
    from agents.db.crud import (
        get_pipeline_human_gate,
        get_pipeline_run,
        get_pipeline_run_tasks,
        resolve_pipeline_human_gate,
        update_pipeline_run,
        update_pipeline_task,
    )

    gate_uuid = uuid.UUID(gate_id)
    gate = await get_pipeline_human_gate(session, gate_uuid)
    if gate is None or str(gate.project_id) != request.project_id:
        raise HTTPException(status_code=404, detail="Gate not found")
    if gate.status != "pending":
        raise HTTPException(status_code=400, detail="Gate is no longer pending")

    if gate.gate_type == "pre_deploy" and action != "approve":
        raise HTTPException(status_code=400, detail="Use approve/reject for pre_deploy gates")
    if gate.gate_type == "override" and action not in {"override", "reject"}:
        raise HTTPException(status_code=400, detail="Use override/reject for override gates")

    gate = await resolve_pipeline_human_gate(
        session,
        gate_uuid,
        status="approved" if action == "approve" else ("overridden" if action == "override" else "rejected"),
        resolved_payload={"reason": request.reason},
        resolved_reason=request.reason,
        resolved_by=ctx.user_id,
    )

    run = await get_pipeline_run(session, gate.pipeline_run_id)
    tasks = await get_pipeline_run_tasks(session, gate.pipeline_run_id)
    waiting_tasks = [
        task
        for task in tasks
        if getattr(task, "gate_id", None) == gate.id
        and task.status == "waiting_for_approval"
    ]
    waiting_task = waiting_tasks[-1] if waiting_tasks else None

    if action in {"approve", "override"} and waiting_task is not None:
        await update_pipeline_task(
            session,
            waiting_task.id,
            status="pending",
            claimed_at=None,
            completed_at=None,
            result_summary=None,
            failure_class=None if action == "approve" else waiting_task.failure_class,
        )
        await update_pipeline_run(
            session,
            gate.pipeline_run_id,
            status="running",
            resumed_at=datetime.now(timezone.utc),
            failure_class=None,
            failure_reason=None,
        )
    else:
        if waiting_task is not None:
            await update_pipeline_task(
                session,
                waiting_task.id,
                status="cancelled",
                completed_at=datetime.now(timezone.utc),
                result_summary=request.reason or "Operator rejected the human gate.",
                failure_class="human_gate",
            )
        await update_pipeline_run(
            session,
            gate.pipeline_run_id,
            status="failed",
            completed_at=datetime.now(timezone.utc),
            failure_class="human_gate",
            failure_reason=request.reason or "Operator rejected the human gate.",
        )

    return {
        "success": True,
        "gate_id": gate_id,
        "action": action,
        "pipeline_run_id": str(gate.pipeline_run_id),
        "reason": request.reason,
        "run_status": "running" if action in {"approve", "override"} else "failed",
    }


@app.post("/pipeline/gates/{gate_id}/approve")
async def approve_pipeline_gate(
    gate_id: str,
    request: GateDecisionRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    await ensure_project_context(request.project_id, ctx.user_id, session)
    return await _resolve_gate_transition(
        gate_id=gate_id,
        action="approve",
        request=request,
        ctx=ctx,
        session=session,
    )


@app.post("/pipeline/gates/{gate_id}/reject")
async def reject_pipeline_gate(
    gate_id: str,
    request: GateDecisionRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    await ensure_project_context(request.project_id, ctx.user_id, session)
    return await _resolve_gate_transition(
        gate_id=gate_id,
        action="reject",
        request=request,
        ctx=ctx,
        session=session,
    )


@app.post("/pipeline/gates/{gate_id}/override")
async def override_pipeline_gate(
    gate_id: str,
    request: GateDecisionRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_session),
):
    await ensure_project_context(request.project_id, ctx.user_id, session)
    return await _resolve_gate_transition(
        gate_id=gate_id,
        action="override",
        request=request,
        ctx=ctx,
        session=session,
    )
