"""Neon Postgres DB layer: async engine, session, and table creation."""

import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agents.db.models import Base

load_dotenv()

T = TypeVar("T")

# Neon connection string: use postgresql+asyncpg for SQLAlchemy async
# If DATABASE_URL is postgresql://..., convert to postgresql+asyncpg://
# asyncpg uses connect_args["ssl"], not URL params like sslmode.
def _get_async_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url or "postgresql+asyncpg://localhost/partyhat"


def _is_remote_ssl_host(url: str) -> bool:
    """True if URL points to a host that requires SSL (e.g. Neon)."""
    if not url or "localhost" in url or "127.0.0.1" in url:
        return False
    return "neon.tech" in url or ".aws.neon.tech" in url


_db_url = _get_async_url()
_engine = create_async_engine(
    _db_url,
    echo=os.getenv("SQL_ECHO", "").lower() in ("1", "true", "yes"),
    pool_pre_ping=True,  # check connection before use (avoids "connection is closed")
    pool_recycle=300,    # recycle connections before Neon idle timeout
    pool_use_lifo=True,  # prefer hot connections to reduce churn against the pooler
    connect_args={"ssl": True} if _is_remote_ssl_host(_db_url) else {},
)

async_session_factory = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


_TRANSIENT_DB_ERROR_TOKENS = (
    "connection was closed in the middle of operation",
    "the underlying connection is closed",
    "cannot call transaction.rollback()",
    "connection reset by peer",
    "connection is closed",
    "connection refused",
    "server closed the connection unexpectedly",
    "terminating connection due to administrator command",
    "could not connect to server",
    "ssl syscall error",
    "connection reseterror",
)


def _iter_exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        next_exc = current.__cause__ or current.__context__
        current = next_exc if isinstance(next_exc, BaseException) else None


def is_transient_db_disconnect(exc: BaseException) -> bool:
    for current in _iter_exception_chain(exc):
        if isinstance(current, (ConnectionError, ConnectionResetError, TimeoutError, OSError)):
            return True
        if isinstance(current, DBAPIError) and getattr(current, "connection_invalidated", False):
            return True
        message = str(current).lower()
        if any(token in message for token in _TRANSIENT_DB_ERROR_TOKENS):
            return True
    return False


async def invalidate_session_safely(session: AsyncSession | None) -> None:
    if session is None:
        return
    try:
        await session.invalidate()
    except Exception:
        try:
            await session.close()
        except Exception:
            pass


async def rollback_session_safely(session: AsyncSession | None) -> None:
    if session is None:
        return
    try:
        await session.rollback()
    except Exception as exc:
        if is_transient_db_disconnect(exc):
            await invalidate_session_safely(session)
            return
        raise


async def run_with_retry(
    session: AsyncSession,
    operation: Callable[[AsyncSession], Awaitable[T]],
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> T:
    try:
        return await operation(session)
    except Exception as exc:
        if not is_transient_db_disconnect(exc):
            raise
        await invalidate_session_safely(session)
        factory = session_factory or async_session_factory
        async with factory() as retry_session:
            return await operation(retry_session)


async def get_session():
    """Yield an async session for dependency injection. Yields None when DATABASE_URL is not set."""
    if not os.getenv("DATABASE_URL"):
        yield None
        return
    session = async_session_factory()
    try:
        yield session
    except Exception as exc:
        if is_transient_db_disconnect(exc):
            await invalidate_session_safely(session)
        else:
            await rollback_session_safely(session)
        raise
    finally:
        try:
            await session.close()
        except Exception as exc:
            if not is_transient_db_disconnect(exc):
                raise


async def _migrate_users_email_to_wallet(conn) -> None:
    """Rename users.email -> users.wallet if the email column exists (one-off migration)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'email'
              ) THEN
                ALTER TABLE users RENAME COLUMN email TO wallet;
              END IF;
            END $$;
            """
        )
    )


async def _migrate_projects_add_screenshot_base64(conn) -> None:
    """Add projects.screenshot_base64 column if missing (one-off migration)."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'projects'
                  AND column_name = 'screenshot_base64'
              ) THEN
                ALTER TABLE projects ADD COLUMN screenshot_base64 TEXT;
              END IF;
            END $$;
            """
        )
    )


async def _migrate_pipeline_tasks_add_hierarchy(conn) -> None:
    """Add hierarchy and dispatch fields to pipeline_tasks if missing."""
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'pipeline_tasks'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'task_type'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN task_type TEXT;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'parent_task_id'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN parent_task_id UUID;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'sequence_index'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN sequence_index INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'artifact_revision'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN artifact_revision INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'depends_on_task_ids'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN depends_on_task_ids JSONB;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'claimed_at'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN claimed_at TIMESTAMPTZ;
                END IF;

                UPDATE pipeline_tasks
                SET task_type = COALESCE(task_type, assigned_to || '.legacy');

                UPDATE pipeline_tasks
                SET sequence_index = 0
                WHERE sequence_index IS NULL;

                UPDATE pipeline_tasks
                SET artifact_revision = 0
                WHERE artifact_revision IS NULL;

                ALTER TABLE pipeline_tasks
                  ALTER COLUMN task_type SET DEFAULT 'unknown';
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN task_type SET NOT NULL;
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN sequence_index SET DEFAULT 0;
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN sequence_index SET NOT NULL;
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN artifact_revision SET DEFAULT 0;
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN artifact_revision SET NOT NULL;
              END IF;
            END $$;
            """
        )
    )

    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'pipeline_tasks'
              ) AND NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'pipeline_tasks_parent_task_id_fkey'
              ) THEN
                ALTER TABLE pipeline_tasks
                  ADD CONSTRAINT pipeline_tasks_parent_task_id_fkey
                  FOREIGN KEY (parent_task_id)
                  REFERENCES pipeline_tasks(id)
                  ON DELETE SET NULL;
              END IF;
            END $$;
            """
        )
    )

    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_parent_task_id
            ON pipeline_tasks (parent_task_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_dispatch_status_created
            ON pipeline_tasks (pipeline_run_id, status, created_at, sequence_index, id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_dispatch_revision
            ON pipeline_tasks (pipeline_run_id, status, artifact_revision, created_at, sequence_index, id);
            """
        )
    )


async def _migrate_pipeline_tasks_add_runtime_fields(conn) -> None:
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'pipeline_tasks'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'retry_budget_key'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN retry_budget_key TEXT;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'retry_attempt'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN retry_attempt INTEGER DEFAULT 0;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'failure_class'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN failure_class TEXT;
                END IF;

                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_tasks'
                    AND column_name = 'gate_id'
                ) THEN
                  ALTER TABLE pipeline_tasks ADD COLUMN gate_id UUID;
                END IF;

                UPDATE pipeline_tasks
                SET retry_attempt = 0
                WHERE retry_attempt IS NULL;

                ALTER TABLE pipeline_tasks
                  ALTER COLUMN retry_attempt SET DEFAULT 0;
                ALTER TABLE pipeline_tasks
                  ALTER COLUMN retry_attempt SET NOT NULL;
              END IF;
            END $$;
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_retry_budget_key
            ON pipeline_tasks (retry_budget_key);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_gate_id
            ON pipeline_tasks (gate_id);
            """
        )
    )


async def _migrate_test_runs_runtime_fields(conn) -> None:
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'test_runs'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'pipeline_run_id'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN pipeline_run_id UUID;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'pipeline_task_id'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN pipeline_task_id UUID;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'artifact_revision'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN artifact_revision INTEGER DEFAULT 0;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'stdout_path'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN stdout_path TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'stderr_path'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN stderr_path TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'exit_code'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN exit_code INTEGER;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'test_runs'
                    AND column_name = 'trace_id'
                ) THEN
                  ALTER TABLE test_runs ADD COLUMN trace_id TEXT;
                END IF;

                UPDATE test_runs
                SET artifact_revision = 0
                WHERE artifact_revision IS NULL;

                ALTER TABLE test_runs
                  ALTER COLUMN artifact_revision SET DEFAULT 0;
                ALTER TABLE test_runs
                  ALTER COLUMN artifact_revision SET NOT NULL;
              END IF;
            END $$;
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_test_runs_pipeline_run_id
            ON test_runs (pipeline_run_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_test_runs_pipeline_task_id
            ON test_runs (pipeline_task_id);
            """
        )
    )


async def _migrate_deployments_runtime_fields(conn) -> None:
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'deployments'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'pipeline_run_id'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN pipeline_run_id UUID;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'pipeline_task_id'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN pipeline_task_id UUID;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'plan_contract_id'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN plan_contract_id TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'artifact_revision'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN artifact_revision INTEGER DEFAULT 0;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'stdout_path'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN stdout_path TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'stderr_path'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN stderr_path TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'exit_code'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN exit_code INTEGER;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'trace_id'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN trace_id TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'deployed_contracts'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN deployed_contracts JSONB;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'deployments'
                    AND column_name = 'executed_calls'
                ) THEN
                  ALTER TABLE deployments ADD COLUMN executed_calls JSONB;
                END IF;

                UPDATE deployments
                SET artifact_revision = 0
                WHERE artifact_revision IS NULL;

                ALTER TABLE deployments
                  ALTER COLUMN artifact_revision SET DEFAULT 0;
                ALTER TABLE deployments
                  ALTER COLUMN artifact_revision SET NOT NULL;
              END IF;
            END $$;
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_deployments_pipeline_run_id
            ON deployments (pipeline_run_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_deployments_pipeline_task_id
            ON deployments (pipeline_task_id);
            """
        )
    )


async def _migrate_plans_add_default_deployment_target(conn) -> None:
    await conn.execute(
        text(
            """
            UPDATE plans
            SET plan_data = jsonb_set(
              COALESCE(plan_data::jsonb, '{}'::jsonb),
              '{deployment_target}',
              jsonb_build_object(
                'network', 'avalanche_fuji',
                'name', 'Avalanche Fuji',
                'description', 'Default Avalanche Fuji deployment target.',
                'chain_id', 43113,
                'rpc_url_env_var', 'FUJI_RPC_URL',
                'private_key_env_var', 'FUJI_PRIVATE_KEY'
              ),
              true
            )::json
            WHERE plan_data IS NOT NULL
              AND NOT ((plan_data::jsonb) ? 'deployment_target');
            """
        )
    )


async def _migrate_pipeline_runs_detached_execution(conn) -> None:
    await conn.execute(
        text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'pipeline_runs'
              ) THEN
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_runs'
                    AND column_name = 'runner_token'
                ) THEN
                  ALTER TABLE pipeline_runs ADD COLUMN runner_token TEXT;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_runs'
                    AND column_name = 'runner_started_at'
                ) THEN
                  ALTER TABLE pipeline_runs ADD COLUMN runner_started_at TIMESTAMPTZ;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_runs'
                    AND column_name = 'runner_heartbeat_at'
                ) THEN
                  ALTER TABLE pipeline_runs ADD COLUMN runner_heartbeat_at TIMESTAMPTZ;
                END IF;
                IF NOT EXISTS (
                  SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public'
                    AND table_name = 'pipeline_runs'
                    AND column_name = 'next_event_seq'
                ) THEN
                  ALTER TABLE pipeline_runs ADD COLUMN next_event_seq INTEGER DEFAULT 1;
                END IF;

                UPDATE pipeline_runs
                SET next_event_seq = 1
                WHERE next_event_seq IS NULL;

                ALTER TABLE pipeline_runs
                  ALTER COLUMN next_event_seq SET DEFAULT 1;
                ALTER TABLE pipeline_runs
                  ALTER COLUMN next_event_seq SET NOT NULL;
              END IF;
            END $$;
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_runs_runner_token
            ON pipeline_runs (runner_token);
            """
        )
    )
    await conn.execute(
        text(
            """
            WITH event_state AS (
              SELECT pipeline_run_id, COALESCE(MAX(seq), 0) + 1 AS next_seq
              FROM pipeline_run_events
              GROUP BY pipeline_run_id
            )
            UPDATE pipeline_runs pr
            SET next_event_seq = event_state.next_seq
            FROM event_state
            WHERE pr.id = event_state.pipeline_run_id
              AND pr.next_event_seq < event_state.next_seq;
            """
        )
    )


async def _migrate_project_runtime_states(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS project_runtime_states (
              id UUID PRIMARY KEY,
              project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              scope TEXT NOT NULL,
              state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              version INTEGER NOT NULL DEFAULT 1,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_project_runtime_states_project_scope
            ON project_runtime_states (project_id, scope);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_project_runtime_states_project_scope
            ON project_runtime_states (project_id, scope);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_project_runtime_states_project_updated
            ON project_runtime_states (project_id, updated_at);
            """
        )
    )


async def _migrate_pipeline_run_events(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS pipeline_run_events (
              id UUID PRIMARY KEY,
              project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
              pipeline_task_id UUID,
              seq INTEGER NOT NULL,
              event_type TEXT NOT NULL,
              stage TEXT,
              payload JSONB NOT NULL,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_run_events_run_seq
            ON pipeline_run_events (pipeline_run_id, seq);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_run_events_run_seq
            ON pipeline_run_events (pipeline_run_id, seq);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_run_events_project_created
            ON pipeline_run_events (project_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_run_events_pipeline_task_id
            ON pipeline_run_events (pipeline_task_id);
            """
        )
    )


async def _migrate_telegram_notifications(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS telegram_user_links (
              id UUID PRIMARY KEY,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              chat_id BIGINT NOT NULL,
              chat_type TEXT NOT NULL DEFAULT 'private',
              telegram_user_id BIGINT,
              chat_username TEXT,
              first_name TEXT,
              enabled BOOLEAN NOT NULL DEFAULT TRUE,
              linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_user_links_user_id
            ON telegram_user_links (user_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_user_links_chat_id
            ON telegram_user_links (chat_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_telegram_user_links_enabled
            ON telegram_user_links (enabled);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_telegram_user_links_updated
            ON telegram_user_links (updated_at);
            """
        )
    )

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS telegram_link_tokens (
              id UUID PRIMARY KEY,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              token_hash TEXT NOT NULL,
              expires_at TIMESTAMPTZ NOT NULL,
              used_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_link_tokens_token_hash
            ON telegram_link_tokens (token_hash);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_telegram_link_tokens_user_created
            ON telegram_link_tokens (user_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_telegram_link_tokens_expires
            ON telegram_link_tokens (expires_at);
            """
        )
    )

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
              id UUID PRIMARY KEY,
              user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              pipeline_run_id UUID NOT NULL REFERENCES pipeline_runs(id) ON DELETE CASCADE,
              channel TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
              dedupe_key TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              last_error TEXT,
              claimed_at TIMESTAMPTZ,
              sent_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_outbox_dedupe_key
            ON notification_outbox (dedupe_key);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notification_outbox_channel_status_created
            ON notification_outbox (channel, status, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notification_outbox_pipeline_run_id
            ON notification_outbox (pipeline_run_id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notification_outbox_claimed_at
            ON notification_outbox (claimed_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_notification_outbox_user_id
            ON notification_outbox (user_id);
            """
        )
    )
async def _migrate_pipeline_status_read_indexes(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_tasks_run_created
            ON pipeline_tasks (pipeline_run_id, created_at, sequence_index, id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_evaluations_run_created
            ON pipeline_evaluations (pipeline_run_id, created_at, id);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_test_runs_project_created
            ON test_runs (project_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_deployments_project_created
            ON deployments (project_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_messages_project_session_created
            ON messages (project_id, session_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_plans_project_created
            ON plans (project_id, created_at);
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_pipeline_run_snapshots_project_updated
            ON pipeline_run_snapshots (project_id, updated_at);
            """
        )
    )



async def create_tables() -> None:
    """Create all tables. Call on app startup or migrations."""
    if not os.getenv("DATABASE_URL"):
        return  # Skip when DATABASE_URL not configured (e.g. local dev without Neon)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_users_email_to_wallet(conn)
        await _migrate_projects_add_screenshot_base64(conn)
        await _migrate_pipeline_tasks_add_hierarchy(conn)
        await _migrate_pipeline_tasks_add_runtime_fields(conn)
        await _migrate_test_runs_runtime_fields(conn)
        await _migrate_deployments_runtime_fields(conn)
        await _migrate_plans_add_default_deployment_target(conn)
        await _migrate_pipeline_runs_detached_execution(conn)
        await _migrate_project_runtime_states(conn)
        await _migrate_pipeline_run_events(conn)
        await _migrate_telegram_notifications(conn)
        await _migrate_pipeline_status_read_indexes(conn)


async def drop_tables() -> None:
    """Drop all tables. For tests or reset only."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
