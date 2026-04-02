import asyncio
import os
import sys
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from letta_client import Letta

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

# Thread pool for running async Neon CRUD from sync tool context
_db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="partyhat-db")


def _run_db(coro):
    """
    Run an async coroutine from a sync context safely.
    Uses a dedicated thread so asyncio.run() always gets a fresh event loop,
    avoiding conflicts with FastAPI's running loop.
    """
    future = _db_executor.submit(asyncio.run, coro)
    return future.result()


class MemoryManager:
    def __init__(self, user_id: str = "default", project_id: str | None = None):
        """
        Args:
            user_id:    The authenticated user's ID.
                        Defaults to "default" for local testing only.
            project_id: When set, all memory is scoped to this project.
        """
        self.client = Letta(api_key=os.getenv("LETTA_API_KEY"))
        self.user_id = user_id
        self.project_id = project_id

        # Block labels
        if project_id:
            self.user_block_label = f"project:{project_id}"
        else:
            self.user_block_label = f"user:{user_id}"

        # Block ID cache that are populated on first _get_or_create call
        # Avoids repeated blocks.list() calls within the same instance
        self._block_id_cache: dict[str, str] = {}

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
                    "note_count": 0,  # how many reasoning notes exist in Neon
                    # Letta-local copies of the current and previous plan JSON.
                    # Only the most recent two versions are kept to avoid unbounded growth.
                    "current_plan": None,
                    "previous_plan": None,
                },
                "coding": {
                    "artifact_count": 0,
                    "last_artifact_path": None,
                },
                "testing": {
                    "last_test_status": None,  # passed | failed | error
                    "last_run_id": None,  # Neon test_runs.id
                },
                "deployment": {
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
        planning.setdefault("note_count", 0)
        planning.setdefault("current_plan", None)
        planning.setdefault("previous_plan", None)

        coding = agents.setdefault("coding", {})
        coding.setdefault("artifact_count", 0)
        coding.setdefault("last_artifact_path", None)
        coding.setdefault("artifacts", [])
        coding.setdefault("notes", [])

        testing = agents.setdefault("testing", {})
        testing.setdefault("last_test_status", None)
        testing.setdefault("last_run_id", None)

        deployment = agents.setdefault("deployment", {})
        deployment.setdefault("deployed_address", None)
        deployment.setdefault("tx_hash", None)
        deployment.setdefault("snowtrace_url", None)

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

    def _db_call(self, coro_factory):
        """
        Run an async DB operation from sync context.
        coro_factory is a callable that accepts a session and returns a coroutine.
        Returns the result or None if DB is unavailable.
        """
        if not self._db_available:
            return None

        async def _run():
            """
            Run the DB coroutine using a per-call async engine bound to this
            event loop, to avoid sharing asyncpg connections across loops.
            """
            from agents.db import _get_async_url, _is_remote_ssl_host  # type: ignore[attr-defined]
            from sqlalchemy.ext.asyncio import (
                AsyncSession,
                async_sessionmaker,
                create_async_engine,
            )

            db_url = _get_async_url()
            if not db_url:
                return None

            connect_args = (
                {"ssl": True} if _is_remote_ssl_host(db_url) else {}
            )

            engine = create_async_engine(
                db_url,
                echo=False,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args=connect_args,
            )
            session_factory = async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            )

            try:
                async with session_factory() as session:
                    return await coro_factory(session)
            finally:
                await engine.dispose()

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
        project_uuid = self._project_uuid()
        status = plan.get("status", "draft")

        # Writing the full plan to Neon
        saved_plan = None
        if project_uuid:
            from agents.db.crud import upsert_plan as db_upsert_plan

            saved_plan = self._db_call(
                lambda session: db_upsert_plan(session, project_uuid, plan, status)
            )

        # Updating Letta planning slice in Letta
        data, block = self._read_user_block()
        planning = self._get_agent_slice(data, "planning")

        # Shift current_plan to previous_plan and store the new plan as current_plan.
        # This keeps a one-step history in Letta while Neon stores the full history.
        if planning.get("current_plan") is not None:
            planning["previous_plan"] = planning.get("current_plan")
        planning["current_plan"] = plan

        planning["plan_status"] = status
        if saved_plan:
            planning["plan_id"] = str(saved_plan.id)
        self._write_user_block(data, block)

    def get_plan(self) -> dict | None:
        """
        Retrieve the current plan.
        Reads from Neon if available, falls back to Letta copy when Neon is
        unavailable or has no row.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import get_current_plan as db_get_plan

            plan_row = self._db_call(lambda session: db_get_plan(session, project_uuid))
            if plan_row:
                return plan_row.plan_data

        # Fallback: read from Letta planning slice (current_plan).
        data, _ = self._read_user_block()
        planning = self._get_agent_slice(data, "planning")
        return planning.get("current_plan") or None

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

        # Updating Letta pointer
        data, block = self._read_user_block()
        planning = self._get_agent_slice(data, "planning")
        planning["plan_status"] = status
        self._write_user_block(data, block)

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

        # Incrementing counter in Letta
        data, block = self._read_user_block()
        planning = self._get_agent_slice(data, "planning")
        planning["note_count"] = planning.get("note_count", 0) + 1
        self._write_user_block(data, block)

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
    ) -> None:
        """
        Save a test run result.
        Full output → Neon test_runs table.
        Letta block updated with last_test_status pointer only.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import save_test_run as db_save_run

            run = self._db_call(
                lambda session: db_save_run(
                    session, project_uuid, status, tests_run, tests_passed, output
                )
            )
            # Updating Letta pointer
            data, block = self._read_user_block()
            testing = self._get_agent_slice(data, "testing")
            testing["last_test_status"] = status
            if run:
                testing["last_run_id"] = str(run.id)
            self._write_user_block(data, block)

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
            "created_at": run.created_at.isoformat(),
        }

    def save_deployment(
        self,
        status: str,
        contract_name: str | None = None,
        deployed_address: str | None = None,
        tx_hash: str | None = None,
        snowtrace_url: str | None = None,
        network: str = "avalanche_fuji",
    ) -> None:
        """
        Save a deployment record.
        Full record → Neon deployments table.
        Letta block updated with deployed_address + tx_hash only.
        """
        project_uuid = self._project_uuid()

        if project_uuid and self._db_available:
            from agents.db.crud import save_deployment as db_save_dep

            self._db_call(
                lambda session: db_save_dep(
                    session,
                    project_uuid,
                    status=status,
                    contract_name=contract_name,
                    deployed_address=deployed_address,
                    tx_hash=tx_hash,
                    snowtrace_url=snowtrace_url,
                    network=network,
                )
            )

        # Updating Letta pointer with only the small strings agents actually need
        data, block = self._read_user_block()
        deployment = self._get_agent_slice(data, "deployment")
        if deployed_address:
            deployment["deployed_address"] = deployed_address
        if tx_hash:
            deployment["tx_hash"] = tx_hash
        if snowtrace_url:
            deployment["snowtrace_url"] = snowtrace_url
        self._write_user_block(data, block)

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
            "deployed_address": dep.deployed_address,
            "tx_hash": dep.tx_hash,
            "snowtrace_url": dep.snowtrace_url,
            "network": dep.network,
            "created_at": dep.created_at.isoformat(),
        }

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
    ) -> None:
        """Update the coding agent's working state pointer in Letta."""
        data, block = self._read_user_block()
        coding = self._get_agent_slice(data, "coding")
        if artifact_count is not None:
            coding["artifact_count"] = artifact_count
        if last_artifact_path is not None:
            coding["last_artifact_path"] = last_artifact_path
        self._write_user_block(data, block)

    def update_audit_state(self, open_issues: int) -> None:
        """Update the audit agent's open issue count in Letta."""
        data, block = self._read_user_block()
        audit = self._get_agent_slice(data, "audit")
        audit["open_issues"] = open_issues
        self._write_user_block(data, block)

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
        """Return the working state slice for a given agent from Letta."""
        data, _ = self._read_user_block()
        return data.get("agents", {}).get(agent_name, {})

    def set_agent_state(self, agent_name: str, state: dict) -> None:
        """Replace the working state slice for a given agent in Letta."""
        data, block = self._read_user_block()
        data.setdefault("agents", {})[agent_name] = state
        self._write_user_block(data, block)


if __name__ == "__main__":
    mm = MemoryManager(user_id="test-user-123")
    print("MemoryManager ready!")
    print(f"User block label: {mm.user_block_label}")
    print(f"DB available:     {mm._db_available}")
