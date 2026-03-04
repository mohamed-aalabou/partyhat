"""
Lightweight smoke tests for agent wiring.

These tests are intentionally minimal and avoid real network calls. They
check that agent factories and routing helpers can be imported and that
the registry exposes the expected intents.
"""

from agents.agents.agent_registry import AGENTS, get_agent_for_intent


def test_registry_contains_expected_agents():
    for intent in ["planning", "coding", "testing", "deployment", "audit"]:
        assert intent in AGENTS
        assert AGENTS[intent] is not None


def test_get_agent_for_intent_round_trip():
    for intent in ["planning", "coding", "testing", "deployment", "audit"]:
        agent = get_agent_for_intent(intent)
        assert agent is AGENTS[intent]

