import asyncio

import pytest

from agents.db import is_transient_db_disconnect, run_with_retry


class FakeSession:
    def __init__(self, name: str):
        self.name = name
        self.invalidated = False
        self.closed = False

    async def invalidate(self) -> None:
        self.invalidated = True

    async def close(self) -> None:
        self.closed = True


class FakeSessionContext:
    def __init__(self, session: FakeSession):
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


def test_is_transient_db_disconnect_matches_connection_reset() -> None:
    assert is_transient_db_disconnect(ConnectionResetError("socket reset by peer"))
    assert is_transient_db_disconnect(
        RuntimeError("connection was closed in the middle of operation")
    )


def test_run_with_retry_retries_once_with_fresh_session() -> None:
    first_session = FakeSession("first")
    retry_session = FakeSession("retry")
    calls: list[str] = []

    def session_factory():
        return FakeSessionContext(retry_session)

    async def operation(session: FakeSession) -> str:
        calls.append(session.name)
        if session is first_session:
            raise RuntimeError("connection was closed in the middle of operation")
        return "ok"

    result = asyncio.run(
        run_with_retry(
            first_session,
            operation,
            session_factory=session_factory,
        )
    )

    assert result == "ok"
    assert calls == ["first", "retry"]
    assert first_session.invalidated is True


def test_run_with_retry_does_not_retry_non_transient_errors() -> None:
    first_session = FakeSession("first")
    retry_session = FakeSession("retry")
    calls: list[str] = []

    def session_factory():
        return FakeSessionContext(retry_session)

    async def operation(session: FakeSession) -> str:
        calls.append(session.name)
        raise ValueError("invalid payload")

    with pytest.raises(ValueError, match="invalid payload"):
        asyncio.run(
            run_with_retry(
                first_session,
                operation,
                session_factory=session_factory,
            )
        )

    assert calls == ["first"]
    assert first_session.invalidated is False
