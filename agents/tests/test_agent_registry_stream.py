import asyncio
from types import SimpleNamespace

from agents import agent_registry, planning_tools


class FakePlanningAgent:
    def __init__(self, content: str, tool_calls: list[dict] | None = None):
        self._message = SimpleNamespace(
            content=content,
            tool_calls=tool_calls or [],
        )

    async def astream(self, _payload, *, config, stream_mode):
        assert config == {"configurable": {"thread_id": "project-123"}}
        assert stream_mode == "values"
        yield {"messages": [self._message]}


def test_stream_chat_with_intent_includes_approval_request_when_present(monkeypatch):
    cleared = []
    monkeypatch.setattr(
        agent_registry,
        "get_agent_for_intent",
        lambda intent: FakePlanningAgent(
            content="The plan is ready for your review.",
            tool_calls=[{"name": "request_plan_verification", "args": "{}"}],
        ),
    )
    monkeypatch.setattr(
        planning_tools,
        "get_approval_request",
        lambda: {"type": "plan_verification", "required": True},
    )
    monkeypatch.setattr(planning_tools, "get_answer_recommendations", lambda: [])
    monkeypatch.setattr(planning_tools, "get_pending_questions", lambda: [])
    monkeypatch.setattr(
        planning_tools,
        "clear_pending_questions",
        lambda: cleared.append(True),
    )

    async def scenario():
        events = []
        async for event in agent_registry.stream_chat_with_intent(
            intent="planning",
            session_id="session-123",
            user_message="Please review the finished plan.",
            project_id="project-123",
        ):
            events.append(event)
        return events

    events = asyncio.run(scenario())

    assert cleared == [True]
    assert events[-1] == {
        "type": "done",
        "session_id": "session-123",
        "response": "The plan is ready for your review.",
        "tool_calls": ["request_plan_verification"],
        "approval_request": {"type": "plan_verification", "required": True},
        "answer_recommendations": [],
        "pending_questions": [],
    }


def test_stream_chat_with_intent_emits_null_approval_request_when_absent(monkeypatch):
    monkeypatch.setattr(
        agent_registry,
        "get_agent_for_intent",
        lambda intent: FakePlanningAgent(content="I still need one final answer."),
    )
    monkeypatch.setattr(planning_tools, "get_approval_request", lambda: None)
    monkeypatch.setattr(planning_tools, "get_answer_recommendations", lambda: [])
    monkeypatch.setattr(planning_tools, "get_pending_questions", lambda: [])
    monkeypatch.setattr(planning_tools, "clear_pending_questions", lambda: None)

    async def scenario():
        events = []
        async for event in agent_registry.stream_chat_with_intent(
            intent="planning",
            session_id="session-123",
            user_message="Continue planning.",
            project_id="project-123",
        ):
            events.append(event)
        return events

    events = asyncio.run(scenario())

    assert events[-1]["type"] == "done"
    assert events[-1]["approval_request"] is None
