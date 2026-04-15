from agents import planning_tools
from schemas.deployment_schema import DeploymentTarget
from schemas.plan_schema import (
    Constructor,
    ContractFunction,
    ContractPlan,
    FunctionInput,
    PostDeployCall,
    SmartContractPlan,
)


class FakeMemoryManager:
    def __init__(self):
        self.agent_states = {"planning": {}}

    def get_agent_state(self, agent_name: str) -> dict:
        return self.agent_states.setdefault(agent_name, {})

    def set_agent_state(self, agent_name: str, state: dict) -> None:
        self.agent_states[agent_name] = state


def test_send_question_batch_persists_structured_questions(monkeypatch):
    mm = FakeMemoryManager()
    monkeypatch.setattr(planning_tools, "_get_memory_manager", lambda: mm)

    result = planning_tools.send_question_batch.invoke(
        {
            "questions": [
                {
                    "question": "What ERC standard do you want?",
                    "answer_recommendations": [
                        {"text": "ERC-20", "recommended": True},
                        {"text": "ERC-721"},
                    ],
                },
                {
                    "question": "Do you need owner-only minting?",
                    "answer_recommendations": [
                        {"text": "Yes, owner only", "recommended": True},
                        {"text": "No, anyone can mint"},
                    ],
                },
            ]
        }
    )

    assert result["success"] is True
    assert result["count"] == 2
    assert planning_tools.get_pending_questions() == result["pending_questions"]
    assert planning_tools.get_answer_recommendations() == [
        {"text": "ERC-20", "recommended": True},
        {"text": "ERC-721"},
    ]


def test_send_question_batch_rejects_more_than_five_questions(monkeypatch):
    mm = FakeMemoryManager()
    monkeypatch.setattr(planning_tools, "_get_memory_manager", lambda: mm)

    result = planning_tools.send_question_batch.invoke(
        {
            "questions": [
                {"question": f"Question {idx}?"}
                for idx in range(1, 7)
            ]
        }
    )

    assert result == {
        "error": "A question batch may contain at most 5 questions."
    }


def test_send_answer_recommendations_updates_first_pending_question(monkeypatch):
    mm = FakeMemoryManager()
    mm.set_agent_state(
        "planning",
        {
            "pending_questions": [
                {"question": "Who can mint?", "answer_recommendations": []}
            ]
        },
    )
    monkeypatch.setattr(planning_tools, "_get_memory_manager", lambda: mm)

    planning_tools.send_answer_recommendations.invoke(
        {
            "recommendations": [
                {"text": "Only the owner", "recommended": True},
                {"text": "Addresses with MINTER_ROLE"},
            ]
        }
    )

    assert planning_tools.get_pending_questions() == [
        {
            "question": "Who can mint?",
            "answer_recommendations": [
                {"text": "Only the owner", "recommended": True},
                {"text": "Addresses with MINTER_ROLE"},
            ],
        }
    ]

    planning_tools.clear_pending_questions()

    assert planning_tools.get_pending_questions() == []
    assert planning_tools.get_answer_recommendations() == []


def test_request_plan_verification_persists_structured_indicator(monkeypatch):
    mm = FakeMemoryManager()
    monkeypatch.setattr(planning_tools, "_get_memory_manager", lambda: mm)

    result = planning_tools.request_plan_verification.invoke({})

    assert result == {
        "success": True,
        "approval_request": {
            "type": "plan_verification",
            "required": True,
        },
    }
    assert planning_tools.get_approval_request() == result["approval_request"]


def test_clear_pending_questions_also_clears_approval_request(monkeypatch):
    mm = FakeMemoryManager()
    mm.set_agent_state(
        "planning",
        {
            "approval_request": {
                "type": "plan_verification",
                "required": True,
            }
        },
    )
    monkeypatch.setattr(planning_tools, "_get_memory_manager", lambda: mm)

    planning_tools.clear_pending_questions()

    assert planning_tools.get_approval_request() is None


def _build_plan(*, constructor_inputs, functions=None, post_deploy_calls=None):
    return SmartContractPlan(
        project_name="PartyToken",
        description="Token plan",
        deployment_target=DeploymentTarget(
            network="avalanche_fuji",
            name="Avalanche Fuji",
            chain_id=43113,
            rpc_url_env_var="FUJI_RPC_URL",
            private_key_env_var="FUJI_PRIVATE_KEY",
        ),
        contracts=[
            ContractPlan(
                name="PartyToken",
                description="ERC-20 token",
                erc_template="ERC-20",
                dependencies=["Ownable"],
                constructor=Constructor(
                    description="Initializes the token",
                    inputs=list(constructor_inputs),
                ),
                functions=list(
                    functions
                    or [
                        ContractFunction(
                            name="mint",
                            description="Mint tokens",
                            inputs=[],
                            outputs=[],
                            conditions=["Caller must be owner"],
                        )
                    ]
                ),
            )
        ],
        post_deploy_calls=list(post_deploy_calls or []),
    )


def test_validate_plan_requires_constructor_address_wallet_defaults():
    plan = _build_plan(
        constructor_inputs=[
            FunctionInput(
                name="initialOwner",
                type="address",
                description="Owner wallet",
            )
        ]
    )

    result = planning_tools.validate_plan.func(plan)

    assert result["valid"] is False
    assert any("initialOwner" in issue for issue in result["issues"])
    assert any("default_value='deployer'" in issue for issue in result["issues"])


def test_validate_plan_accepts_constructor_address_deployer_fallback():
    plan = _build_plan(
        constructor_inputs=[
            FunctionInput(
                name="initialOwner",
                type="address",
                description="Owner wallet",
                default_value="deployer",
            )
        ]
    )

    result = planning_tools.validate_plan.func(plan)

    assert result["valid"] is True


def test_validate_plan_rejects_invalid_post_deploy_args_for_single_contract_plan():
    plan = _build_plan(
        constructor_inputs=[],
        functions=[
            ContractFunction(
                name="createEdition",
                description="Create a new edition",
                inputs=[
                    FunctionInput(name="tokenId", type="uint256", description="Token id"),
                    FunctionInput(name="key", type="string", description="Edition key"),
                    FunctionInput(name="maxSupply", type="uint256", description="Cap"),
                    FunctionInput(name="uri", type="string", description="Token URI"),
                ],
                outputs=[],
                conditions=["Caller must be owner"],
            )
        ],
        post_deploy_calls=[
            PostDeployCall(
                target_contract_name="PartyToken",
                function_name="createEdition",
                args=["1", "pass:supporter", "TBD", "TBD"],
                call_order=1,
                description="Create supporter pass",
            )
        ],
    )

    result = planning_tools.validate_plan.func(plan)

    assert result["valid"] is False
    assert any("arg 2" in issue and "quoted string literal" in issue for issue in result["issues"])
    assert any("arg 3" in issue and "unresolved value 'TBD'" in issue for issue in result["issues"])
    assert any("arg 4" in issue and "unresolved value 'TBD'" in issue for issue in result["issues"])
