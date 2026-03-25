"""
Contextvars won't work for cancellation because the cancel request
arrives on a different HTTP request (different async context) than
the one running the pipeline. This module uses a simple in-memory
set of cancelled pipeline_run_ids that both the cancel endpoint
and the orchestrator can access.
"""

_cancelled_runs: set[str] = set()


def cancel_pipeline_run(pipeline_run_id: str) -> None:
    """Mark a pipeline run as cancelled."""
    _cancelled_runs.add(pipeline_run_id)


def is_pipeline_cancelled(pipeline_run_id: str) -> bool:
    """Check if a pipeline run has been cancelled."""
    return pipeline_run_id in _cancelled_runs


def clear_cancellation(pipeline_run_id: str) -> None:
    """Remove a pipeline run from the cancellation set (cleanup)."""
    _cancelled_runs.discard(pipeline_run_id)
