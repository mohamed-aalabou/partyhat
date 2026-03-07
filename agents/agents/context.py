"""Request-scoped context for project_id and user_id (contextvars)."""

from contextvars import ContextVar

project_id_var: ContextVar[str | None] = ContextVar("project_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)


def set_project_context(project_id: str, user_id: str) -> None:
    """Set project and user context for the current request/task."""
    project_id_var.set(project_id)
    user_id_var.set(user_id)


def get_project_context() -> tuple[str | None, str | None]:
    """Return (project_id, user_id) from context. Either may be None."""
    return project_id_var.get(), user_id_var.get()


def clear_project_context() -> None:
    """Clear context (e.g. after request)."""
    try:
        project_id_var.set(None)
    except LookupError:
        pass
    try:
        user_id_var.set(None)
    except LookupError:
        pass
