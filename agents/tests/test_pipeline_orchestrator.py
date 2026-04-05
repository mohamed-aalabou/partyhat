import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from agents import pipeline_orchestrator as orchestrator


@dataclass
class FakeTask:
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    project_id: uuid.UUID
    assigned_to: str
    created_by: str
    task_type: str
    description: str
    status: str = "pending"
    context: dict | None = None
    parent_task_id: uuid.UUID | None = None
    sequence_index: int = 0
    artifact_revision: int = 0
    depends_on_task_ids: list[str] | None = None
    result_summary: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    claimed_at: datetime | None = None
    completed_at: datetime | None = None


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMemoryManager:
    plan = {
        "project_name": "PartyToken",
        "status": "ready",
        "contracts": [
            {
                "name": "PartyToken",
                "description": "Primary token contract",
                "erc_template": "ERC-20",
                "dependencies": ["Ownable"],
                "constructor": {"inputs": [], "description": "Default constructor"},
                "functions": [{"name": "mint"}],
            }
        ],
    }
    agent_states = {
        "planning": {
            "plan_summary": {
                "project_name": "PartyToken",
                "erc_standard": "ERC-20",
                "contract_names": ["PartyToken"],
                "key_constraints": ["dependency:Ownable"],
            }
        },
        "coding": {
            "latest_artifact_revision": 0,
            "artifacts": [],
        },
        "testing": {"artifacts": [], "last_test_results": []},
        "deployment": {"artifacts": [], "last_deploy_results": []},
    }

    def __init__(self, user_id: str, project_id: str):
        self.user_id = user_id
        self.project_id = project_id

    def get_agent_state(self, agent_name: str) -> dict:
        return self.agent_states.setdefault(agent_name, {})

    def get_plan(self) -> dict:
        return dict(self.plan)

    def update_plan_status(self, status: str) -> None:
        self.agent_states.setdefault("planning", {})["plan_status"] = status


def _build_context(task_type: str, artifact_revision: int, input_artifacts: dict | None = None):
    return {
        "artifact_revision": artifact_revision,
        "plan_summary": {
            "project_name": "PartyToken",
            "erc_standard": "ERC-20",
            "contract_names": ["PartyToken"],
            "key_constraints": ["dependency:Ownable"],
        },
        "input_artifacts": input_artifacts
        or {"coding": [], "testing": [], "deployment": []},
        "upstream_task": None,
        "failure_context": None,
        "expected_outputs": orchestrator.default_expected_outputs(task_type),
    }


def _build_harness(monkeypatch, scenario: str):
    tasks: list[FakeTask] = []
    status_updates: list[str] = []
    thread_ids: list[str] = []
    user_messages: list[str] = []
    created_counter = 0

    FakeMemoryManager.agent_states = {
        "planning": {
            "plan_summary": {
                "project_name": "PartyToken",
                "erc_standard": "ERC-20",
                "contract_names": ["PartyToken"],
                "key_constraints": ["dependency:Ownable"],
            }
        },
        "coding": {"latest_artifact_revision": 0, "artifacts": []},
        "testing": {"artifacts": [], "last_test_results": []},
        "deployment": {"artifacts": [], "last_deploy_results": []},
    }

    def add_task(
        *,
        pipeline_run_id: uuid.UUID,
        project_id: uuid.UUID,
        assigned_to: str,
        created_by: str,
        task_type: str,
        description: str,
        context: dict | None = None,
        parent_task_id: uuid.UUID | None = None,
        sequence_index: int = 0,
        artifact_revision: int = 0,
        depends_on_task_ids: list[str] | None = None,
    ) -> FakeTask:
        nonlocal created_counter
        created_at = datetime(2026, 4, 3, tzinfo=timezone.utc) + timedelta(
            seconds=created_counter
        )
        created_counter += 1
        task = FakeTask(
            id=uuid.uuid4(),
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=assigned_to,
            created_by=created_by,
            task_type=task_type,
            description=description,
            context=context,
            parent_task_id=parent_task_id,
            sequence_index=sequence_index,
            artifact_revision=artifact_revision,
            depends_on_task_ids=depends_on_task_ids,
            created_at=created_at,
        )
        tasks.append(task)
        return task

    async def fake_create_pipeline_task(
        session,
        pipeline_run_id,
        project_id,
        assigned_to,
        created_by,
        task_type,
        description,
        context=None,
        parent_task_id=None,
        sequence_index=0,
        artifact_revision=0,
        depends_on_task_ids=None,
    ):
        return add_task(
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=assigned_to,
            created_by=created_by,
            task_type=task_type,
            description=description,
            context=context,
            parent_task_id=parent_task_id,
            sequence_index=sequence_index,
            artifact_revision=artifact_revision,
            depends_on_task_ids=depends_on_task_ids,
        )

    async def fake_claim_next_pending_task(session, pipeline_run_id):
        completed_ids = {
            str(task.id)
            for task in tasks
            if task.pipeline_run_id == pipeline_run_id and task.status == "completed"
        }
        pending = [
            task
            for task in tasks
            if task.pipeline_run_id == pipeline_run_id and task.status == "pending"
        ]
        for task in sorted(
            pending, key=lambda current: (current.created_at, current.sequence_index, current.id)
        ):
            deps = task.depends_on_task_ids or []
            if any(dep not in completed_ids for dep in deps):
                continue
            task.status = "in_progress"
            task.claimed_at = task.created_at + timedelta(seconds=5)
            return task
        return None

    async def fake_get_pipeline_task(session, task_id):
        for task in tasks:
            if task.id == task_id:
                return task
        return None

    async def fake_get_pipeline_run_tasks(session, pipeline_run_id):
        return sorted(
            [task for task in tasks if task.pipeline_run_id == pipeline_run_id],
            key=lambda current: (current.created_at, current.sequence_index, current.id),
        )

    async def fake_complete_pipeline_task_and_create_next(
        session,
        *,
        pipeline_run_id,
        project_id,
        task_id,
        task_status,
        result_summary,
        next_tasks,
        created_by,
    ):
        current_task = next(task for task in tasks if task.id == task_id)
        current_task.status = task_status
        current_task.result_summary = result_summary
        current_task.completed_at = (current_task.claimed_at or current_task.created_at) + timedelta(minutes=1)

        created = []
        for payload in next_tasks:
            created.append(
                add_task(
                    pipeline_run_id=pipeline_run_id,
                    project_id=project_id,
                    assigned_to=payload["assigned_to"],
                    created_by=created_by,
                    task_type=payload["task_type"],
                    description=payload["description"],
                    context=payload.get("context"),
                    parent_task_id=uuid.UUID(payload["parent_task_id"])
                    if payload.get("parent_task_id")
                    else current_task.id,
                    sequence_index=payload.get("sequence_index", 0),
                    artifact_revision=payload.get(
                        "artifact_revision", current_task.artifact_revision
                    ),
                    depends_on_task_ids=payload.get("depends_on_task_ids"),
                )
            )
        return current_task, created

    def append_result(agent_name: str, history_key: str, task: FakeTask, exit_code: int):
        FakeMemoryManager.agent_states.setdefault(agent_name, {}).setdefault(
            history_key, []
        ).append(
            {
                "pipeline_run_id": str(task.pipeline_run_id),
                "pipeline_task_id": str(task.id),
                "exit_code": exit_code,
                "stdout_path": f"logs/{task.pipeline_run_id}/{task.id}/stdout.log",
                "stderr_path": f"logs/{task.pipeline_run_id}/{task.id}/stderr.log",
            }
        )

    async def fake_stream_chat_with_intent(
        intent,
        session_id,
        user_message,
        project_id=None,
        thread_id_override=None,
    ):
        current_task = next(task for task in tasks if task.status == "in_progress")
        thread_ids.append(thread_id_override or "")
        user_messages.append(user_message)
        yield {"type": "step", "content": current_task.task_type}

        if current_task.task_type == "coding.generate_contracts":
            FakeMemoryManager.agent_states["coding"]["latest_artifact_revision"] = 1
            FakeMemoryManager.agent_states["coding"]["artifacts"] = [
                {"path": "contracts/PartyToken.sol", "contract_names": ["PartyToken"]}
            ]
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=current_task.pipeline_run_id,
                project_id=current_task.project_id,
                task_id=current_task.id,
                task_status="completed",
                result_summary="Generated Solidity contracts.",
                created_by="coding",
                next_tasks=[
                    {
                        "assigned_to": "testing",
                        "task_type": "testing.generate_tests",
                        "description": "Generate Foundry tests for the generated contracts.",
                        "parent_task_id": str(current_task.id),
                        "sequence_index": 0,
                        "artifact_revision": 1,
                        "context": _build_context(
                            "testing.generate_tests",
                            1,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": [],
                                "deployment": [],
                            },
                        ),
                    }
                ],
            )
        elif current_task.task_type == "testing.generate_tests":
            FakeMemoryManager.agent_states["testing"]["artifacts"] = [
                {"path": "test/PartyTokenTest.t.sol", "contract_names": ["PartyTokenTest"]}
            ]
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=current_task.pipeline_run_id,
                project_id=current_task.project_id,
                task_id=current_task.id,
                task_status="completed",
                result_summary="Generated Foundry tests.",
                created_by="testing",
                next_tasks=[
                    {
                        "assigned_to": "testing",
                        "task_type": "testing.run_tests",
                        "description": "Run the generated Foundry tests.",
                        "parent_task_id": str(current_task.id),
                        "sequence_index": 0,
                        "artifact_revision": current_task.artifact_revision,
                        "context": _build_context(
                            "testing.run_tests",
                            current_task.artifact_revision,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                "deployment": [],
                            },
                        ),
                    }
                ],
            )

        yield {"type": "done"}

    async def fake_execute_direct_task(task, project_id, user_id, pipeline_run_id):
        if task.task_type == "testing.run_tests":
            append_result("testing", "last_test_results", task, 0)
            next_tasks = []
            if scenario != "queue_drain_after_non_deploy":
                next_tasks.append(
                    {
                        "assigned_to": "deployment",
                        "task_type": "deployment.prepare_script",
                        "description": "Prepare the Foundry deployment script.",
                        "parent_task_id": str(task.id),
                        "sequence_index": 0,
                        "artifact_revision": task.artifact_revision,
                        "context": _build_context(
                            "deployment.prepare_script",
                            task.artifact_revision,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                "deployment": [],
                            },
                        ),
                    }
                )
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=task.pipeline_run_id,
                project_id=task.project_id,
                task_id=task.id,
                task_status="completed",
                result_summary="Foundry tests passed.",
                created_by="testing",
                next_tasks=next_tasks,
            )
            return [{"type": "tool_call", "stage": "testing", "tool": "run_foundry_tests", "args": "[]"}]

        if task.task_type == "deployment.prepare_script":
            FakeMemoryManager.agent_states["deployment"]["artifacts"] = [
                {"path": "script/DeployPartyToken.s.sol", "contract_names": ["DeployPartyToken"]}
            ]
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=task.pipeline_run_id,
                project_id=task.project_id,
                task_id=task.id,
                task_status="completed",
                result_summary="Prepared deployment script.",
                created_by="deployment",
                next_tasks=[
                    {
                        "assigned_to": "deployment",
                        "task_type": "deployment.execute_deploy",
                        "description": "Execute the prepared deployment script on Fuji.",
                        "parent_task_id": str(task.id),
                        "sequence_index": 0,
                        "artifact_revision": task.artifact_revision,
                        "context": {
                            **_build_context(
                                "deployment.execute_deploy",
                                task.artifact_revision,
                                {
                                    "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                    "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                    "deployment": FakeMemoryManager.agent_states["deployment"]["artifacts"],
                                },
                            ),
                            "script_path": "script/DeployPartyToken.s.sol",
                            "contract_name": "PartyToken",
                        },
                    }
                ],
            )
            return [{"type": "tool_call", "stage": "deployment", "tool": "save_deploy_artifact", "args": "script/DeployPartyToken.s.sol"}]

        if task.task_type == "deployment.execute_deploy":
            if scenario == "deploy_fail_no_followup":
                append_result("deployment", "last_deploy_results", task, 1)
                await fake_complete_pipeline_task_and_create_next(
                    None,
                    pipeline_run_id=task.pipeline_run_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    task_status="failed",
                    result_summary="Forge deploy failed.",
                    created_by="deployment",
                    next_tasks=[],
                )
            elif scenario == "deploy_fail_with_recovery":
                append_result("deployment", "last_deploy_results", task, 1)
                await fake_complete_pipeline_task_and_create_next(
                    None,
                    pipeline_run_id=task.pipeline_run_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    task_status="failed",
                    result_summary="Initial deploy failed.",
                    created_by="deployment",
                    next_tasks=[
                        {
                            "assigned_to": "deployment",
                            "task_type": "deployment.retry_deploy",
                            "description": "Retry deployment with adjusted deployment parameters.",
                            "parent_task_id": str(task.id),
                            "sequence_index": 0,
                            "artifact_revision": task.artifact_revision,
                            "context": {
                                **_build_context(
                                    "deployment.retry_deploy",
                                    task.artifact_revision,
                                    {
                                        "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                        "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                        "deployment": FakeMemoryManager.agent_states["deployment"]["artifacts"],
                                    },
                                ),
                                "script_path": "script/DeployPartyToken.s.sol",
                            },
                        }
                    ],
                )
            else:
                append_result("deployment", "last_deploy_results", task, 0)
                await fake_complete_pipeline_task_and_create_next(
                    None,
                    pipeline_run_id=task.pipeline_run_id,
                    project_id=task.project_id,
                    task_id=task.id,
                    task_status="completed",
                    result_summary="Deployment succeeded.",
                    created_by="deployment",
                    next_tasks=[],
                )
            return [{"type": "tool_call", "stage": "deployment", "tool": "run_foundry_deploy", "args": "{}"}]

        if task.task_type == "deployment.retry_deploy":
            append_result("deployment", "last_deploy_results", task, 0)
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=task.pipeline_run_id,
                project_id=task.project_id,
                task_id=task.id,
                task_status="completed",
                result_summary="Retry deployment succeeded.",
                created_by="deployment",
                next_tasks=[],
            )
            return [{"type": "tool_call", "stage": "deployment", "tool": "run_foundry_deploy", "args": "{}"}]

        return []

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "create_pipeline_task", fake_create_pipeline_task)
    monkeypatch.setattr(orchestrator, "claim_next_pending_task", fake_claim_next_pending_task)
    monkeypatch.setattr(orchestrator, "complete_pipeline_task_and_create_next", fake_complete_pipeline_task_and_create_next)
    monkeypatch.setattr(orchestrator, "get_pipeline_task", fake_get_pipeline_task)
    monkeypatch.setattr(orchestrator, "get_pipeline_run_tasks", fake_get_pipeline_run_tasks)
    monkeypatch.setattr(orchestrator, "stream_chat_with_intent", fake_stream_chat_with_intent)
    monkeypatch.setattr(orchestrator, "_execute_direct_task", fake_execute_direct_task)
    monkeypatch.setattr(orchestrator, "MemoryManager", FakeMemoryManager)
    monkeypatch.setattr(orchestrator, "is_pipeline_cancelled", lambda pipeline_run_id: False)
    monkeypatch.setattr(orchestrator, "clear_cancellation", lambda pipeline_run_id: None)
    monkeypatch.setattr(
        orchestrator,
        "_update_plan_status",
        lambda project_id, user_id, status: status_updates.append(status),
    )

    return tasks, status_updates, thread_ids, user_messages


def _run_pipeline(monkeypatch, scenario: str):
    tasks, status_updates, thread_ids, user_messages = _build_harness(monkeypatch, scenario)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    async def collect():
        return [
            event
            async for event in orchestrator.run_autonomous_pipeline(
                project_id=project_id,
                user_id=user_id,
            )
        ]

    events = asyncio.run(collect())
    return events, tasks, status_updates, thread_ids, user_messages, project_id, user_id


def test_pipeline_fails_when_deploy_fails_without_followups(monkeypatch):
    events, tasks, status_updates, *_ = _run_pipeline(monkeypatch, "deploy_fail_no_followup")

    assert any(
        event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
        and event["task_status"] == "failed"
        for event in events
    )
    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "failed"


def test_pipeline_continues_after_failed_deploy_with_recovery_subtask(monkeypatch):
    events, tasks, status_updates, *_ = _run_pipeline(monkeypatch, "deploy_fail_with_recovery")

    failed_execute = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
    )
    retry_complete = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.retry_deploy"
    )
    retry_task = next(task for task in tasks if task.task_type == "deployment.retry_deploy")
    execute_task = next(task for task in tasks if task.task_type == "deployment.execute_deploy")

    assert failed_execute["task_status"] == "failed"
    assert retry_complete["task_status"] == "completed"
    assert retry_task.parent_task_id == execute_task.id
    assert any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "deployed"


def test_pipeline_completes_only_after_successful_tagged_terminal_deploy(monkeypatch):
    events, tasks, status_updates, *_ = _run_pipeline(monkeypatch, "success")

    deploy_complete = next(
        event
        for event in events
        if event["type"] == "stage_complete"
        and event["task_type"] == "deployment.execute_deploy"
    )

    assert deploy_complete["result_exit_code"] == 0
    assert deploy_complete["queue_duration_ms"] is not None
    assert deploy_complete["execution_duration_ms"] is not None
    assert any(event["type"] == "pipeline_complete" for event in events)
    assert not any(event["type"] == "pipeline_error" for event in events)
    assert status_updates[-1] == "deployed"


def test_pipeline_fails_when_queue_drains_without_successful_deploy(monkeypatch):
    events, tasks, status_updates, *_ = _run_pipeline(monkeypatch, "queue_drain_after_non_deploy")

    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert status_updates[-1] == "failed"
    assert any(task.task_type == "testing.run_tests" for task in tasks)


def test_pipeline_uses_per_task_thread_ids_and_context(monkeypatch):
    events, tasks, _, thread_ids, user_messages, *_ = _run_pipeline(monkeypatch, "success")

    pipeline_run_id = next(event for event in events if event["type"] == "pipeline_start")[
        "pipeline_run_id"
    ]
    agentic_tasks = [
        task for task in tasks if task.task_type in {"coding.generate_contracts", "testing.generate_tests"}
    ]

    assert len(thread_ids) == 2
    assert all(thread_id.startswith(f"pipeline:{pipeline_run_id}:") for thread_id in thread_ids)
    assert all("Pipeline task context:" in message for message in user_messages)
    assert {task.task_type for task in agentic_tasks} == {
        "coding.generate_contracts",
        "testing.generate_tests",
    }


def test_pipeline_status_includes_timings_and_context_metadata(monkeypatch):
    events, tasks, _, _, _, project_id, user_id = _run_pipeline(monkeypatch, "success")
    pipeline_run_id = next(event for event in events if event["type"] == "pipeline_start")[
        "pipeline_run_id"
    ]

    status = asyncio.run(
        orchestrator.get_pipeline_status(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
        )
    )

    first_task = status["tasks"][0]
    run_tests_task = next(task for task in status["tasks"] if task["task_type"] == "testing.run_tests")

    assert first_task["context"]["plan_summary"]["project_name"] == "PartyToken"
    assert first_task["context"]["expected_outputs"]
    assert first_task["claimed_at"] is not None
    assert first_task["queue_duration_ms"] is not None
    assert first_task["execution_duration_ms"] is not None
    assert run_tests_task["artifact_revision"] == 1
    assert run_tests_task["context"]["input_artifacts"]["coding"][0]["path"] == "contracts/PartyToken.sol"


def test_constructor_literals_use_explicit_defaults_and_deployer_fallback():
    contract_plan = {
        "constructor": {
            "inputs": [
                {
                    "name": "initialOwner",
                    "type": "address",
                    "description": "Owner wallet",
                },
                {
                    "name": "treasury",
                    "type": "address",
                    "description": "Treasury wallet",
                    "default_value": "0x1111111111111111111111111111111111111111",
                },
                {
                    "name": "paused",
                    "type": "bool",
                    "description": "Initial pause flag",
                },
            ]
        }
    }

    literals = orchestrator._constructor_literals(contract_plan)
    constraints = orchestrator._deployment_constraints(contract_plan)

    assert literals == [
        "deployer",
        "0x1111111111111111111111111111111111111111",
        "false",
    ]
    assert any(
        "initialOwner" in constraint and "deployer" in constraint
        for constraint in constraints
    )
    assert any(
        "treasury" in constraint
        and "0x1111111111111111111111111111111111111111" in constraint
        for constraint in constraints
    )
