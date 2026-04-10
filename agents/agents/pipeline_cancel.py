"""
Durable pipeline cancellation helpers backed by Postgres.

Cancellation is keyed by pipeline_run_id and is monotonic: once
cancellation_requested_at is set, it is never cleared.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from agents.db.models import PipelineRun, PipelineRunSnapshot
from agents.pipeline_status import serialize_run


def _get_sync_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if not url.startswith("postgresql://"):
        url = "postgresql://localhost/partyhat"
    return url


def _is_remote_ssl_host(url: str) -> bool:
    if not url or "localhost" in url or "127.0.0.1" in url:
        return False
    return "neon.tech" in url or ".aws.neon.tech" in url


_sync_url = _get_sync_url()
_engine = (
    create_engine(
        _sync_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"sslmode": "require"} if _is_remote_ssl_host(_sync_url) else {},
    )
    if os.getenv("DATABASE_URL")
    else None
)
_SyncSession = sessionmaker(bind=_engine) if _engine else None


def _session() -> Session | None:
    if _SyncSession is None:
        return None
    return _SyncSession()


def cancel_pipeline_run(pipeline_run_id: str, reason: str | None = None) -> bool:
    session = _session()
    if session is None:
        return False
    try:
        run = session.execute(
            select(PipelineRun).where(PipelineRun.id == uuid.UUID(pipeline_run_id))
        ).scalar_one_or_none()
        if run is None:
            return False
        if run.cancellation_requested_at is None:
            run.cancellation_requested_at = datetime.now(timezone.utc)
            run.cancellation_reason = reason
        if run.status not in {"completed", "failed", "cancelled"}:
            run.status = "cancellation_requested"
        run.updated_at = datetime.now(timezone.utc)
        snapshot = session.execute(
            select(PipelineRunSnapshot).where(
                PipelineRunSnapshot.pipeline_run_id == run.id
            )
        ).scalar_one_or_none()
        if snapshot is not None:
            payload = dict(snapshot.snapshot_json or {})
            payload["status"] = run.status
            payload["failure_reason"] = payload.get("failure_reason")
            payload["run"] = serialize_run(run)
            snapshot.status = run.status
            snapshot.failure_reason = payload.get("failure_reason")
            snapshot.snapshot_json = payload
            snapshot.version = int(snapshot.version or 0) + 1
            snapshot.updated_at = run.updated_at
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_pipeline_cancellation(pipeline_run_id: str) -> dict | None:
    session = _session()
    if session is None:
        return None
    try:
        run = session.execute(
            select(PipelineRun).where(PipelineRun.id == uuid.UUID(pipeline_run_id))
        ).scalar_one_or_none()
        if run is None:
            return None
        return {
            "status": run.status,
            "cancellation_requested_at": (
                run.cancellation_requested_at.isoformat()
                if run.cancellation_requested_at
                else None
            ),
            "cancellation_reason": run.cancellation_reason,
        }
    finally:
        session.close()


def is_pipeline_cancelled(pipeline_run_id: str) -> bool:
    details = get_pipeline_cancellation(pipeline_run_id)
    if not details:
        return False
    return details.get("cancellation_requested_at") is not None
