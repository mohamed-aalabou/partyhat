"""
Lightweight smoke tests for agent wiring.

These tests are intentionally minimal and avoid real network calls. They
check that agent factories and routing helpers can be imported and that
the registry exposes the expected intents.
"""

from agents.agents.agent_registry import AGENTS, get_agent_for_intent


def test_coding_tools_can_be_imported():
    # Sanity check that coding tools and storage abstractions import cleanly.
    from agents.agents import coding_tools  # noqa: F401
    from agents.agents.code_storage import LocalCodeStorage  # noqa: F401


def test_deployment_tools_can_be_imported():
    from agents.agents import deployment_tools  # noqa: F401


def test_deployment_tools_include_fuji_workflow():
    from agents.agents.deployment_tools import DEPLOYMENT_TOOLS

    tool_names = {t.name for t in DEPLOYMENT_TOOLS}
    assert "generate_foundry_deploy_script" in tool_names
    assert "save_deploy_artifact" in tool_names
    assert "run_foundry_deploy" in tool_names
    assert "verify_contract_on_snowtrace" in tool_names
    assert "record_deployment" in tool_names


def test_registry_contains_expected_agents():
    for intent in ["planning", "coding", "testing", "deployment", "audit"]:
        assert intent in AGENTS
        assert AGENTS[intent] is not None


def test_get_agent_for_intent_round_trip():
    for intent in ["planning", "coding", "testing", "deployment", "audit"]:
        agent = get_agent_for_intent(intent)
        assert agent is AGENTS[intent]

