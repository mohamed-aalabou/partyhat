import asyncio
import os
import sys
import json
import threading
import uuid
from datetime import datetime
from functools import lru_cache

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from agents.contract_identity import normalize_plan_contracts
from letta_client import Letta
from agents.pipeline_specs import default_deployment_target_payload
from agents.pipeline_context import extract_plan_summary
from schemas.deployment_schema import DeploymentTarget

try:
    # Prefer TOON for token-efficient storage, but fall back to JSON if unavailable
    from toon import (
        encode as toon_encode,
        decode as toon_decode,
        DecodeOptions as ToonDecodeOptions,
    )
except Exception:  # pragma: no cover - extremely unlikely in production environment
    toon_encode = None  # type: ignore[assignment]
    toon_decode = None  # type: ignore[assignment]
    ToonDecodeOptions = None  # type: ignore[assignment]

load_dotenv(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
)

_db_runtime_lock = threading.Lock()
_db_loop_ready = threading.Event()
_db_loop: asyncio.AbstractEventLoop | None = None
_db_loop_thread: threading.Thread | None = None
_db_async_engine = None
_db_async_session_factory = None
_HOT_AGENT_STATE_DEFAULTS: dict[str, dict] = {
    "planning": {
        "plan_id": None,
        "plan_status": None,
        "plan_summary": {},
        "note_count": 0,
        "current_plan": None,
        "approval_request": None,
    },
    "coding": {
        "artifact_count": 0,
        "last_artifact_path": None,
        "latest_artifact_revision": 0,
        "artifacts": [],
        "notes": [],
    },
    "testing": {
        "last_test_status": None,
        "last_run_id": None,
        "last_run": None,
        "last_test_results": [],
        "artifacts": [],
        "notes": [],
    },
    "deployment": {
        "last_deploy_status": None,
        "last_deploy_ref": None,
        "deployed_address": None,
        "tx_hash": None,
        "snowtrace_url": None,
        "deployed_contracts": [],
        "executed_calls": [],
        "last_deploy_results": [],
        "artifacts": [],
        "targets": [],
        "deployments": [],
    },
    "audit": {
        "open_issues": 0,
        "issues": [],
        "reports": [],
    },
}


def _db_loop_worker() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    global _db_loop
    _db_loop = loop
    _db_loop_ready.set()
    loop.run_forever()


def _get_db_session_factory():
    global _db_loop_thread, _db_async_engine, _db_async_session_factory

    with _db_runtime_lock:
        if _db_loop is None or not _db_loop.is_running():
            _db_async_engine = None
            _db_async_session_factory = None
            _db_loop_ready.clear()
            _db_loop_thread = threading.Thread(
                target=_db_loop_worker,
                name="partyhat-db-loop",
                daemon=True,
            )
            _db_loop_thread.start()

        if _db_async_session_factory is None:
            from agents.db import _get_async_url, _is_remote_ssl_host  # type: ignore[attr-defined]
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )

            db_url = _get_async_url()
            if not db_url:
                return None

            connect_args = {"ssl": True} if _is_remote_ssl_host(db_url) else {}
            _db_async_engine = create_async_engine(
                db_url,
                echo=False,
                pool_pre_ping=True,
                pool_recycle=300,
                pool_use_lifo=True,
                connect_args=connect_args,
            )
            _db_async_session_factory = async_sessionmaker(
                _db_async_engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )

    _db_loop_ready.wait()
    return _db_async_session_factory


def _run_db(coro):
    """
    Run an async coroutine from a sync context safely.
    Uses a dedicated event loop thread so asyncpg connections stay bound to
    one loop and can be safely pooled across sync callers.
    """
    session_factory = _get_db_session_factory()
    if session_factory is None or _db_loop is None:
        return None
    future = asyncio.run_coroutine_threadsafe(coro, _db_loop)
    return future.result()


@lru_cache(maxsize=1)
def _get_letta_client(api_key: str | None):
    return Letta(api_key=api_key)


class MemoryManager:
    _block_id_cache_global: dict[str, str] = {}

    def __init__(self, user_id: str = "default", project_id: str | None = None):
        """
        Args:
            user_id:    The authenticated user's ID.
                        Defaults to "default" for local testing only.
            project_id: When set, all memory is scoped to this project.
        """
        self.client = _get_letta_client(os.getenv("LETTA_API_KEY"))
        self.user_id = user_id
        self.project_id = project_id

        # Block labels
        if project_id:
            self.user_block_label = f"project:{project_id}"
        else:
            self.user_block_label = f"user:{user_id}"

        # Block ID cache that are populated on first _get_or_create call
        # Avoids repeated blocks.list() calls within the same instance
        self._block_id_cache = self._block_id_cache_global

        # DB available flag: False if DATABASE_URL is not set
        self._db_available = bool(os.getenv("DATABASE_URL"))

    def _serialize(self, data: dict) -> str:
        if toon_encode is not None:
            try:
                return toon_encode(data)
            except Exception:
                return json.dumps(data, indent=2)
        return json.dumps(data, indent=2)

    def _deserialize(self, value: str) -> dict:
        if not value:
            return {}
        if toon_decode is not None:
            try:
                return toon_decode(value)
            except Exception:
                try:
                    if ToonDecodeOptions is not None:
                        return toon_decode(value, ToonDecodeOptions(strict=False))
                except Exception:
                    pass
                try:
                    return json.loads(value)
                except Exception:
                    pass
        return json.loads(value)

    def _get_or_create_block(
        self, label: str, initial_value: dict, limit: int
    ) -> object:
        """
        Get or create a Letta block by label.
        Caches the block ID after the first lookup so subsequent calls
        skip blocks.list() entirely.
        """
        # Returning cached block ID if we have it
        if label in self._block_id_cache:
            # We still need the block object for its value so fetch directly
            # This is a single GET by ID, not a full list scan
            try:
                return self.client.blocks.get(self._block_id_cache[label])
            except Exception:
                # Cache miss: block may have been deleted, fall through to list
                del self._block_id_cache[label]

        # First call: scan the list once
        existing = self.client.blocks.list()
        for block in existing:
            if block.label == label:
                self._block_id_cache[label] = block.id
                return block

        # Block doesn't exist so create it
        print(f"[MemoryManager] Creating block: {label}")
        block = self.client.blocks.create(
            label=label,
            value=self._serialize(initial_value),
            limit=limit,
        )
        self._block_id_cache[label] = block.id
        return block

    def _get_or_create_user_block(self):
        """Get or create the lean working-state block for this project."""
        initial = {
            "user_id": self.user_id,
            # Tiny profile this stays in Letta (genuinely useful context)
            "profile": {
                "name": None,
                "experience_level": None,
                "preferred_language": None,
            },
            # Tiny preferences; also stays in Letta
            "preferences": {
                "preferred_erc": None,
                "preferred_license": None,
                "preferred_chain": "Avalanche C-Chain",
            },
            # Per-agent working state: pointers and lightweight state
            "agents": {
                "planning": {
                    "plan_id": None,  # Neon plans.id
                    "plan_status": None,  # draft | ready | generating | testing | deployed
                    "plan_summary": {},
                    "note_count": 0,  # how many reasoning notes exist in Neon
                    "current_plan": None,
                },
                "coding": {
                    "artifact_count": 0,
                    "last_artifact_path": None,
                    "latest_artifact_revision": 0,
                },
                "testing": {
                    "last_test_status": None,  # passed | failed | error
                    "last_run_id": None,  # Neon test_runs.id
                    "last_run": None,
                },
                "deployment": {
                    "last_deploy_status": None,
                    "last_deploy_ref": None,
                    "deployed_address": None,
                    "tx_hash": None,
                    "snowtrace_url": None,
                },
                "audit": {
                    "open_issues": 0,
                },
            },
        }
        # 10k as limit for this lean structure
        return self._get_or_create_block(self.user_block_label, initial, limit=10000)

    def _read_user_block(self):
        """Read and parse the user block. Returns (data dict, block object)."""
        block = self._get_or_create_user_block()
        return self._deserialize(block.value), block

    def _write_user_block(self, data: dict, block) -> None:
        """Write updated data back to the user block using cached ID."""
        self.client.blocks.update(block.id, value=self._serialize(data))

    def _get_agent_slice(self, data: dict, agent_name: str) -> dict:
        """Return the mutable state dict for a given agent inside the user block."""
        agents = data.setdefault("agents", {})
        return agents.setdefault(agent_name, {})

    def _ensure_agents_structure(self, data: dict) -> None:
        """
        Ensure the nested agents structure exists on older user blocks.

        New blocks created via _get_or_create_user_block already include this
        structure, but historic blocks created before the refactor may be
        missing some or all of these keys. This helper normalises the shape so
        tools like save_code_artifact can safely mutate coding/testing state.
        """
        agents = data.setdefault("agents", {})

        planning = agents.setdefault("planning", {})
        planning.setdefault("plan_id", None)
        planning.setdefault("plan_status", None)
        planning.setdefault("plan_summary", {})
        planning.setdefault("note_count", 0)
        planning.setdefault("current_plan", None)

        coding = agents.setdefault("coding", {})
        coding.setdefault("artifact_count", 0)
        coding.setdefault("last_artifact_path", None)
        coding.setdefault("latest_artifact_revision", 0)
        coding.setdefault("artifacts", [])
        coding.setdefault("notes", [])

        testing = agents.setdefault("testing", {})
        testing.setdefault("last_test_status", None)
        testing.setdefault("last_run_id", None)
        testing.setdefault("last_run", None)
        testing.setdefault("last_test_results", [])
        testing.setdefault("artifacts", [])
        testing.setdefault("notes", [])

        deployment = agents.setdefault("deployment", {})
        deployment.setdefault("last_deploy_status", None)
        deployment.setdefault("last_deploy_ref", None)
        deployment.setdefault("deployed_address", None)
        deployment.setdefault("tx_hash", None)
        deployment.setdefault("snowtrace_url", None)
        deployment.setdefault("deployed_contracts", [])
        deployment.setdefault("executed_calls", [])
        deployment.setdefault("last_deploy_results", [])
        deployment.setdefault("artifacts", [])
        deployment.setdefault("targets", [])
        deployment.setdefault("deployments", [])

        audit = agents.setdefault("audit", {})
        audit.setdefault("open_issues", 0)

    def _project_uuid(self) -> uuid.UUID | None:
        """Return project_id as UUID, or None if not set / not a valid UUID."""
        if not self.project_id:
            return None
        try:
            return uuid.UUID(self.project_id)
        except ValueError:
            return None

    def _hot_state_enabled(self, agent_name: str) -> bool:
        return (
            self._db_available
            and self._project_uuid() is not None
            and agent_name in _HOT_AGENT_STATE_DEFAULTS
        )

    def _default_agent_state(self, agent_name: str) -> dict:
        template = _HOT_AGENT_STATE_DEFAULTS.get(agent_name, {})
        return json.loads(json.dumps(template))

    def _normalize_agent_state(self, agent_name: str, state: dict | None) -> dict:
        normalized = self._default_agent_state(agent_name)
        if isinstance(state, dict):
            normalized.update(state)
        return normalized

    def get_agent_state_version(self, agent_name: str) -> int:
        project_uuid = self._project_uuid()
        if project_uuid and self._hot_state_enabled(agent_name):
            from agents.db.crud import get_project_runtime_state_versions

            versions = self._db_call(
                lambda session: get_project_runtime_state_versions(
                    session,
                    project_uuid,
                    scopes=[agent_name],
                )
            )
            if isinstance(versions, dict):
                return int(versions.get(agent_name, 0) or 0)
        return 0

    def get_project_state_versions(self) -> dict[str, str]:
        project_uuid = self._project_uuid()
        if project_uuid and self._db_available:
            from agents.db.crud import get_project_runtime_state_versions

            versions = self._db_call(
                lambda session: get_project_runtime_state_versions(
                    session,
                    project_uuid,
                    scopes=["planning", "coding", "deployment"],
                )
            )
            if isinstance(versions, dict):
                return {
                    "plan": str(versions.get("planning", 0)),
                    "code": str(versions.get("coding", 0)),
                    "deployment": str(versions.get("deployment", 0)),
                }
        return {
            "plan": str(self.get_agent_state_version("planning")),
            "code": str(self.get_agent_state_version("coding")),
            "deployment": str(self.get_agent_state_version("deployment")),
        }

    def _db_call(self, coro_factory):
        """
        Run an async DB operation from sync context.
        coro_factory is a callable that accepts a session and returns a coroutine.
        Returns the result or None if DB is unavailable.
        """
        if not self._db_available:
            return None

        session_factory = _get_db_session_factory()
        if session_factory is None:
            return None

        async def _run():
            """
            Run the DB coroutine on the shared DB loop so pooled asyncpg
            connections stay on a single event loop.
            """
            from agents.db import run_with_retry

            async with session_factory() as session:
                return await run_with_retry(
                    session,
                    coro_factory,
                    session_factory=session_factory,
                )

        try:
            return _run_db(_run())
        except Exception as e:
            print(f"[MemoryManager] DB error: {e}")
            return None

    def save_plan(self, plan: dict) -> None:
        """
        Save the smart contract plan.
        Full plan JSON → Neon plans table.
        Letta user block updated with:
          - plan_id + status pointers
          - a copy of the current plan JSON
          - a copy of the previous plan JSON (one-step history only)
        """
        try:
            previous_plan = self.get_plan()
        except Exception:
            previous_plan = None
        plan = self._normalize_plan_payload(plan, previous_plan=previous_plan)
        project_uuid = self._project_uuid()
        status = plan.get("status", "draft")

        # Writing the full plan to Neon
        saved_plan = None
        if project_uuid:
            from agents.db.crud import upsert_plan as db_upsert_plan

            saved_plan = self._db_call(
                lambda session: db_upsert_plan(session, project_uuid, plan, status)
            )

        compact_summary = extract_plan_summary(plan)
        planning = self.get_agent_state("planning")
        planning["plan_status"] = status
        planning["plan_summary"] = compact_summary
        if saved_plan:
            planning["plan_id"] = str(saved_plan.id)
            planning["current_plan"] = None
        else:
            planning["current_plan"] = plan
        self.set_agent_state("planning", planning)

    def _sync_plan_summary(self, plan: dict) -> None:
        summary = extract_plan_summary(plan)
        planning = self.get_agent_state("planning")
        if planning.get("plan_summary") != summary:
            planning["plan_summary"] = summary
            self.set_agent_state("planning", planning)

    def get_plan(self) -> dict | None:
        """
        Retrieve the current plan.
        Reads from Neon if available, falls back to Letta copy when Neon is
        unavailable or has no row.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import get_current_plan as db_get_plan
            from agents.db.crud import upsert_plan as db_upsert_plan

            plan_row = self._db_call(lambda session: db_get_plan(session, project_uuid))
            if plan_row and getattr(plan_row, "plan_data", None) is not None:
                normalized = self._normalize_plan_payload(plan_row.plan_data)
                if normalized != plan_row.plan_data:
                    self._db_call(
                        lambda session: db_upsert_plan(
                            session,
                            project_uuid,
                            normalized,
                            normalized.get("status", getattr(plan_row, "status", "draft")),
                        )
                    )
                self._sync_plan_summary(normalized)
                return normalized

        # Fallback: read from Letta planning slice (current_plan).
        planning = self.get_agent_state("planning")
        plan = planning.get("current_plan") or None
        if not isinstance(plan, dict):
            return plan
        normalized = self._normalize_plan_payload(plan)
        if normalized != plan:
            planning["current_plan"] = normalized
            self.set_agent_state("planning", planning)
        self._sync_plan_summary(normalized)
        return normalized

    def get_plan_history(self) -> list:
        """Plan history is no longer stored; return empty list."""
        return []

    def update_plan_status(self, status: str) -> None:
        """
        Update only the plan status without rewriting the full plan.
        Updates both Neon and the Letta pointer.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import update_plan_status as db_update_status

            self._db_call(
                lambda session: db_update_status(session, project_uuid, status)
            )

        planning = self.get_agent_state("planning")
        planning["plan_status"] = status
        self.set_agent_state("planning", planning)

    def save_reasoning_note(self, note: str) -> None:
        """
        Save a planning reasoning note.
        Full note → Neon reasoning_notes table.
        Letta block updated with note_count pointer only.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import add_reasoning_note as db_add_note

            self._db_call(lambda session: db_add_note(session, project_uuid, note))

        planning = self.get_agent_state("planning")
        planning["note_count"] = planning.get("note_count", 0) + 1
        self.set_agent_state("planning", planning)

    def get_reasoning_notes(self) -> list:
        """
        Retrieve reasoning notes from Neon (last 20, chronological).
        Returns list of dicts with 'note' and 'timestamp' keys.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import get_reasoning_notes as db_get_notes

            rows = self._db_call(lambda session: db_get_notes(session, project_uuid))
            if rows:
                return [
                    {
                        "note": r.note,
                        "timestamp": r.created_at.isoformat(),
                    }
                    for r in rows
                ]
        return []

    def log_agent_action(
        self,
        agent_name: str,
        action: str,
        input_received: dict | str | None = None,
        output_produced: dict | str | None = None,
        decisions_made: list | None = None,
        why: str | None = None,
        how: str | None = None,
        error: str | None = None,
    ) -> None:
        """
        Log a significant agent action to Neon.
        The global Letta log block is eliminated — Neon handles this.

        NOTE: input_received and output_produced are intentionally dropped.
        Full artifacts live in code_storage. The log stores summaries only.
        If we need to log a summary, pass it as the 'why' parameter.
        """
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            print(f"[AgentLog] {agent_name} | {action} | {why or ''}")
            return

        # Building a lean summary from decisions_made and how
        summary_parts = []
        if decisions_made:
            summary_parts.append(
                f"Decisions: {', '.join(str(d) for d in decisions_made)}"
            )
        if how:
            summary_parts.append(f"How: {how}")
        summary = " | ".join(summary_parts) if summary_parts else None

        from agents.db.crud import append_agent_log as db_log

        self._db_call(
            lambda session: db_log(
                session,
                project_id=project_uuid,
                agent=agent_name,
                action=action,
                user_id=self.user_id,
                summary=summary,
                why=why,
                error=error,
            )
        )

    def get_global_log(self) -> list:
        """Get recent agent log entries from Neon (last 30)."""
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return []

        from agents.db.crud import get_agent_log as db_get_log

        rows = self._db_call(lambda session: db_get_log(session, project_uuid))
        if not rows:
            return []
        return [
            {
                "timestamp": r.created_at.isoformat(),
                "agent": r.agent,
                "action": r.action,
                "summary": r.summary,
                "why": r.why,
                "error": r.error,
            }
            for r in rows
        ]

    def get_global_log_for_agent(self, agent_name: str) -> list:
        """Get recent log entries filtered by agent name."""
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return []

        from agents.db.crud import get_agent_log as db_get_log

        rows = self._db_call(
            lambda session: db_get_log(session, project_uuid, agent=agent_name)
        )
        if not rows:
            return []
        return [
            {
                "timestamp": r.created_at.isoformat(),
                "agent": r.agent,
                "action": r.action,
                "summary": r.summary,
                "why": r.why,
                "error": r.error,
            }
            for r in rows
        ]

    def get_global_log_for_user(self) -> list:
        """Alias of get_global_log for backwards compatibility."""
        return self.get_global_log()

    def save_test_run(
        self,
        status: str,
        tests_run: int | None = None,
        tests_passed: int | None = None,
        output: str | None = None,
        pipeline_run_id: str | None = None,
        pipeline_task_id: str | None = None,
        artifact_revision: int = 0,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        exit_code: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        """
        Save a test run result.
        Full output → Neon test_runs table.
        Letta block updated with last_test_status pointer only.
        """
        project_uuid = self._project_uuid()
        run = None

        if project_uuid and self._db_available:
            from agents.db.crud import save_test_run as db_save_run

            run = self._db_call(
                lambda session: db_save_run(
                    session,
                    project_uuid,
                    status,
                    tests_run,
                    tests_passed,
                    output,
                    pipeline_run_id=uuid.UUID(pipeline_run_id)
                    if pipeline_run_id
                    else None,
                    pipeline_task_id=uuid.UUID(pipeline_task_id)
                    if pipeline_task_id
                    else None,
                    artifact_revision=artifact_revision,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    exit_code=exit_code,
                    trace_id=trace_id,
                )
            )

        testing = self.get_agent_state("testing")
        testing["last_test_status"] = status
        if run:
            testing["last_run_id"] = str(run.id)
            testing["last_run"] = {
                "id": str(run.id),
                "status": status,
                "created_at": run.created_at.isoformat(),
                "stdout_path": run.stdout_path,
                "stderr_path": run.stderr_path,
                "exit_code": run.exit_code,
            }
        self.set_agent_state("testing", testing)

    def get_last_test_run(self) -> dict | None:
        """Get the most recent test run result from Neon."""
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return None

        from agents.db.crud import get_last_test_run as db_get_run

        run = self._db_call(lambda session: db_get_run(session, project_uuid))
        if not run:
            return None
        return {
            "status": run.status,
            "tests_run": run.tests_run,
            "tests_passed": run.tests_passed,
            "output": run.output,
            "pipeline_run_id": str(run.pipeline_run_id) if run.pipeline_run_id else None,
            "pipeline_task_id": str(run.pipeline_task_id)
            if run.pipeline_task_id
            else None,
            "artifact_revision": run.artifact_revision,
            "stdout_path": run.stdout_path,
            "stderr_path": run.stderr_path,
            "exit_code": run.exit_code,
            "trace_id": run.trace_id,
            "created_at": run.created_at.isoformat(),
        }

    def save_deployment(
        self,
        status: str,
        contract_name: str | None = None,
        plan_contract_id: str | None = None,
        deployed_address: str | None = None,
        tx_hash: str | None = None,
        snowtrace_url: str | None = None,
        network: str = "avalanche_fuji",
        pipeline_run_id: str | None = None,
        pipeline_task_id: str | None = None,
        artifact_revision: int = 0,
        stdout_path: str | None = None,
        stderr_path: str | None = None,
        exit_code: int | None = None,
        trace_id: str | None = None,
        deployed_contracts: list[dict] | None = None,
        executed_calls: list[dict] | None = None,
    ) -> None:
        """
        Save a deployment record.
        Full record → Neon deployments table.
        Letta block updated with deployed_address + tx_hash only.
        """
        project_uuid = self._project_uuid()
        dep = None

        if project_uuid and self._db_available:
            from agents.db.crud import save_deployment as db_save_dep

            dep = self._db_call(
                lambda session: db_save_dep(
                    session,
                    project_uuid,
                    status=status,
                    contract_name=contract_name,
                    plan_contract_id=plan_contract_id,
                    deployed_address=deployed_address,
                    tx_hash=tx_hash,
                    snowtrace_url=snowtrace_url,
                    network=network,
                    pipeline_run_id=uuid.UUID(pipeline_run_id)
                    if pipeline_run_id
                    else None,
                    pipeline_task_id=uuid.UUID(pipeline_task_id)
                    if pipeline_task_id
                    else None,
                    artifact_revision=artifact_revision,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    exit_code=exit_code,
                    trace_id=trace_id,
                    deployed_contracts=deployed_contracts,
                    executed_calls=executed_calls,
                )
            )
            if dep is None:
                raise RuntimeError(
                    "Authoritative deployment record could not be persisted."
                )

        deployment = self.get_agent_state("deployment")
        deployment["last_deploy_status"] = status
        if deployed_address:
            deployment["deployed_address"] = deployed_address
        if tx_hash:
            deployment["tx_hash"] = tx_hash
        if snowtrace_url:
            deployment["snowtrace_url"] = snowtrace_url
        if deployed_contracts:
            deployment["deployed_contracts"] = list(deployed_contracts)
        if executed_calls:
            deployment["executed_calls"] = list(executed_calls)
        deployment["last_deploy_ref"] = {
            "status": status,
            "contract_name": contract_name,
            "plan_contract_id": plan_contract_id,
            "network": network,
            "deployed_address": deployed_address,
            "tx_hash": tx_hash,
            "deployed_contracts": list(deployed_contracts or []),
            "executed_calls": list(executed_calls or []),
        }
        self.set_agent_state("deployment", deployment)

    def get_last_deployment(self) -> dict | None:
        """Get the most recent deployment record from Neon."""
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return None

        from agents.db.crud import get_last_deployment as db_get_dep

        dep = self._db_call(lambda session: db_get_dep(session, project_uuid))
        if not dep:
            return None
        return {
            "status": dep.status,
            "contract_name": dep.contract_name,
            "plan_contract_id": dep.plan_contract_id,
            "deployed_address": dep.deployed_address,
            "tx_hash": dep.tx_hash,
            "snowtrace_url": dep.snowtrace_url,
            "network": dep.network,
            "pipeline_run_id": str(dep.pipeline_run_id) if dep.pipeline_run_id else None,
            "pipeline_task_id": str(dep.pipeline_task_id)
            if dep.pipeline_task_id
            else None,
            "artifact_revision": dep.artifact_revision,
            "stdout_path": dep.stdout_path,
            "stderr_path": dep.stderr_path,
            "exit_code": dep.exit_code,
            "trace_id": dep.trace_id,
            "deployed_contracts": dep.deployed_contracts or [],
            "executed_calls": dep.executed_calls or [],
            "created_at": dep.created_at.isoformat(),
        }

    def list_test_runs(
        self,
        limit: int = 20,
        *,
        include_output: bool = True,
    ) -> list[dict]:
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return []

        from agents.db.crud import list_test_runs as db_list_test_runs

        rows = self._db_call(
            lambda session: db_list_test_runs(
                session,
                project_uuid,
                limit=limit,
                include_output=include_output,
            )
        )
        return [
            {
                "status": row.status,
                "tests_run": row.tests_run,
                "tests_passed": row.tests_passed,
                "output": row.output if include_output else None,
                "pipeline_run_id": str(row.pipeline_run_id)
                if row.pipeline_run_id
                else None,
                "pipeline_task_id": str(row.pipeline_task_id)
                if row.pipeline_task_id
                else None,
                "artifact_revision": row.artifact_revision,
                "stdout_path": row.stdout_path,
                "stderr_path": row.stderr_path,
                "exit_code": row.exit_code,
                "trace_id": row.trace_id,
                "deployed_contracts": row.deployed_contracts or [],
                "executed_calls": row.executed_calls or [],
                "created_at": row.created_at.isoformat(),
            }
            for row in rows or []
        ]

    def list_deployments(self, limit: int = 20) -> list[dict]:
        project_uuid = self._project_uuid()
        if not project_uuid or not self._db_available:
            return []

        from agents.db.crud import list_deployments as db_list_deployments

        rows = self._db_call(
            lambda session: db_list_deployments(session, project_uuid, limit=limit)
        )
        return [
            {
                "status": row.status,
                "contract_name": row.contract_name,
                "plan_contract_id": row.plan_contract_id,
                "deployed_address": row.deployed_address,
                "tx_hash": row.tx_hash,
                "snowtrace_url": row.snowtrace_url,
                "network": row.network,
                "pipeline_run_id": str(row.pipeline_run_id)
                if row.pipeline_run_id
                else None,
                "pipeline_task_id": str(row.pipeline_task_id)
                if row.pipeline_task_id
                else None,
                "artifact_revision": row.artifact_revision,
                "stdout_path": row.stdout_path,
                "stderr_path": row.stderr_path,
                "exit_code": row.exit_code,
                "trace_id": row.trace_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows or []
        ]

    def save_user_profile(
        self,
        name: str = None,
        experience_level: str = None,
        preferred_language: str = None,
    ) -> None:
        data, block = self._read_user_block()
        profile = data.setdefault("profile", {})
        if name is not None:
            profile["name"] = name
        if experience_level is not None:
            profile["experience_level"] = experience_level
        if preferred_language is not None:
            profile["preferred_language"] = preferred_language
        self._write_user_block(data, block)

    def get_user_profile(self) -> dict:
        data, _ = self._read_user_block()
        return data.get("profile", {})

    def save_user_preference(self, key: str, value: str) -> None:
        data, block = self._read_user_block()
        data.setdefault("preferences", {})[key] = value
        self._write_user_block(data, block)

    def get_user_preferences(self) -> dict:
        data, _ = self._read_user_block()
        return data.get("preferences", {})

    def update_coding_state(
        self,
        artifact_count: int | None = None,
        last_artifact_path: str | None = None,
        latest_artifact_revision: int | None = None,
    ) -> None:
        """Update the coding agent's working state pointer."""
        coding = self.get_agent_state("coding")
        if artifact_count is not None:
            coding["artifact_count"] = artifact_count
        if last_artifact_path is not None:
            coding["last_artifact_path"] = last_artifact_path
        if latest_artifact_revision is not None:
            coding["latest_artifact_revision"] = latest_artifact_revision
        self.set_agent_state("coding", coding)

    def update_audit_state(self, open_issues: int) -> None:
        """Update the audit agent's open issue count."""
        audit = self.get_agent_state("audit")
        audit["open_issues"] = open_issues
        self.set_agent_state("audit", audit)

    def save_session_summary(self, summary: str) -> None:
        """Log session summary as an agent log entry in Neon."""
        self.log_agent_action(
            agent_name="planning_agent",
            action="session_summary",
            why=summary,
        )

    def get_session_history(self) -> list:
        """Get session summaries from the agent log."""
        return [
            e
            for e in self.get_global_log_for_agent("planning_agent")
            if e.get("action") == "session_summary"
        ]

    def get_agent_state(self, agent_name: str) -> dict:
        """Return the working state slice for a given agent."""
        project_uuid = self._project_uuid()
        if project_uuid and self._hot_state_enabled(agent_name):
            from agents.db.crud import get_project_runtime_state

            row = self._db_call(
                lambda session: get_project_runtime_state(session, project_uuid, agent_name)
            )
            if row is not None:
                return self._normalize_agent_state(agent_name, row.state_json)
            default_state = self._default_agent_state(agent_name)
            self.set_agent_state(agent_name, default_state)
            return default_state

        data, _ = self._read_user_block()
        self._ensure_agents_structure(data)
        return self._normalize_agent_state(
            agent_name,
            data.get("agents", {}).get(agent_name, {}),
        )

    def set_agent_state(self, agent_name: str, state: dict) -> None:
        """Replace the working state slice for a given agent."""
        normalized = self._normalize_agent_state(agent_name, state)
        project_uuid = self._project_uuid()
        if project_uuid and self._hot_state_enabled(agent_name):
            from agents.db.crud import upsert_project_runtime_state

            self._db_call(
                lambda session: upsert_project_runtime_state(
                    session,
                    project_id=project_uuid,
                    scope=agent_name,
                    state_json=normalized,
                )
            )
            return

        data, block = self._read_user_block()
        self._ensure_agents_structure(data)
        data.setdefault("agents", {})[agent_name] = normalized
        self._write_user_block(data, block)

    def _normalize_plan_payload(
        self,
        plan: dict | None,
        *,
        previous_plan: dict | None = None,
    ) -> dict | None:
        if not isinstance(plan, dict):
            return plan
        normalized = normalize_plan_contracts(plan, previous_plan=previous_plan)
        if not isinstance(normalized, dict):
            return normalized
        normalized.setdefault("deployment_target", default_deployment_target_payload())
        try:
            normalized["deployment_target"] = DeploymentTarget.model_validate(
                normalized["deployment_target"]
            ).model_dump(exclude_none=True)
        except Exception:
            pass
        return normalized


if __name__ == "__main__":
    mm = MemoryManager(user_id="test-user-123")
    print("MemoryManager ready!")
    print(f"User block label: {mm.user_block_label}")
    print(f"DB available:     {mm._db_available}")
