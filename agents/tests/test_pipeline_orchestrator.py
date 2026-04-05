import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agents import pipeline_orchestrator as orchestrator
from agents.pipeline_specs import (
    default_deployment_target_payload,
    retry_budget_key_for_task,
)


@dataclass
class FakeRun:
    id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    plan_id: uuid.UUID | None = None
    status: str = "created"
    current_stage: str | None = None
    current_task_id: uuid.UUID | None = None
    deployment_target: dict | None = None
    cancellation_requested_at: datetime | None = None
    cancellation_reason: str | None = None
    terminal_deployment_id: uuid.UUID | None = None
    failure_class: str | None = None
    failure_reason: str | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    paused_at: datetime | None = None
    resumed_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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
    retry_budget_key: str | None = None
    retry_attempt: int = 0
    failure_class: str | None = None
    gate_id: uuid.UUID | None = None
    result_summary: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    claimed_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class FakeGate:
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    gate_type: str
    status: str = "pending"
    pipeline_task_id: uuid.UUID | None = None
    evaluation_id: uuid.UUID | None = None
    requested_payload: dict | None = None
    resolved_payload: dict | None = None
    requested_reason: str | None = None
    resolved_reason: str | None = None
    requested_by: str | None = None
    resolved_by: str | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None


@dataclass
class FakeEvaluation:
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    stage: str
    evaluation_type: str
    blocking: bool
    status: str
    summary: str
    details_json: dict | None = None
    artifact_revision: int = 0
    pipeline_task_id: uuid.UUID | None = None
    trace_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FakeExecutionRow:
    id: uuid.UUID
    pipeline_run_id: uuid.UUID
    pipeline_task_id: uuid.UUID
    status: str
    exit_code: int
    stdout_path: str
    stderr_path: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMemoryManager:
    plan = {
        "project_name": "PartyToken",
        "status": "ready",
        "description": "Token plan",
        "deployment_target": default_deployment_target_payload(),
        "contracts": [
            {
                "name": "PartyToken",
                "description": "Primary token contract",
                "erc_template": "ERC-20",
                "deployment_role": "primary_deployable",
                "deploy_order": 1,
                "dependencies": ["Ownable"],
                "constructor": {"inputs": [], "description": "Default constructor"},
                "functions": [{"name": "mint"}],
            }
        ],
    }
    agent_states = {}

    def __init__(self, user_id: str, project_id: str):
        self.user_id = user_id
        self.project_id = project_id

    def get_agent_state(self, agent_name: str) -> dict:
        return self.agent_states.setdefault(agent_name, {})

    def get_plan(self) -> dict:
        return dict(self.plan)

    def update_plan_status(self, status: str) -> None:
        self.agent_states.setdefault("planning", {})["plan_status"] = status


def _build_context(
    task_type: str,
    artifact_revision: int,
    input_artifacts: dict | None = None,
) -> dict:
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


def _task_payload(
    state: dict,
    current_task: FakeTask,
    *,
    assigned_to: str,
    task_type: str,
    description: str,
    artifact_revision: int,
    context: dict,
    status: str = "pending",
    gate_id: uuid.UUID | None = None,
) -> dict:
    retry_budget_key = retry_budget_key_for_task(task_type)
    next_attempt = max(
        [
            task.retry_attempt
            for task in state["tasks"]
            if task.pipeline_run_id == current_task.pipeline_run_id
            and task.retry_budget_key == retry_budget_key
        ]
        or [-1]
    ) + 1
    return {
        "assigned_to": assigned_to,
        "task_type": task_type,
        "description": description,
        "parent_task_id": str(current_task.id),
        "sequence_index": 0,
        "artifact_revision": artifact_revision,
        "retry_budget_key": retry_budget_key,
        "retry_attempt": next_attempt,
        "status": status,
        "gate_id": str(gate_id) if gate_id else None,
        "context": context,
    }


def _build_harness(monkeypatch, *, drain_after_tests: bool = False):
    base_time = datetime(2026, 4, 3, tzinfo=timezone.utc)
    created_counter = 0
    state = {
        "run": None,
        "tasks": [],
        "gates": [],
        "evaluations": [],
        "test_runs": {},
        "deployments": {},
        "status_updates": [],
        "thread_ids": [],
        "user_messages": [],
    }

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
        "testing": {"artifacts": []},
        "deployment": {"artifacts": []},
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
        retry_budget_key: str | None = None,
        retry_attempt: int = 0,
        failure_class: str | None = None,
        gate_id: uuid.UUID | None = None,
        status: str = "pending",
    ) -> FakeTask:
        nonlocal created_counter
        created_at = base_time + timedelta(seconds=created_counter)
        created_counter += 1
        task = FakeTask(
            id=uuid.uuid4(),
            pipeline_run_id=pipeline_run_id,
            project_id=project_id,
            assigned_to=assigned_to,
            created_by=created_by,
            task_type=task_type,
            description=description,
            status=status,
            context=context,
            parent_task_id=parent_task_id,
            sequence_index=sequence_index,
            artifact_revision=artifact_revision,
            depends_on_task_ids=depends_on_task_ids,
            retry_budget_key=retry_budget_key,
            retry_attempt=retry_attempt,
            failure_class=failure_class,
            gate_id=gate_id,
            created_at=created_at,
        )
        state["tasks"].append(task)
        return task

    async def fake_create_pipeline_run(
        session,
        *,
        project_id,
        user_id,
        plan_id=None,
        deployment_target=None,
        trace_id=None,
    ):
        run = FakeRun(
            id=uuid.uuid4(),
            project_id=project_id,
            user_id=user_id,
            plan_id=plan_id,
            deployment_target=deployment_target,
            trace_id=trace_id,
            created_at=base_time,
            updated_at=base_time,
        )
        state["run"] = run
        return run

    async def fake_update_pipeline_run(session, pipeline_run_id, **kwargs):
        run = state["run"]
        assert run is not None
        assert run.id == pipeline_run_id
        for key, value in kwargs.items():
            setattr(run, key, value)
        run.updated_at = datetime.now(timezone.utc)
        return run

    async def fake_get_pipeline_run(session, pipeline_run_id):
        run = state["run"]
        if run is not None and run.id == pipeline_run_id:
            return run
        return None

    async def fake_get_current_plan_row(session, project_id):
        return SimpleNamespace(id=uuid.uuid4(), plan_data=FakeMemoryManager.plan)

    async def fake_get_next_retry_attempt(session, pipeline_run_id, retry_budget_key):
        return max(
            [
                task.retry_attempt
                for task in state["tasks"]
                if task.pipeline_run_id == pipeline_run_id
                and task.retry_budget_key == retry_budget_key
            ]
            or [-1]
        ) + 1

    async def fake_create_pipeline_task(
        session,
        *,
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
        retry_budget_key=None,
        retry_attempt=0,
        failure_class=None,
        gate_id=None,
        status="pending",
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
            retry_budget_key=retry_budget_key,
            retry_attempt=retry_attempt,
            failure_class=failure_class,
            gate_id=gate_id,
            status=status,
        )

    async def fake_claim_next_pending_task(session, pipeline_run_id):
        pending = [
            task
            for task in state["tasks"]
            if task.pipeline_run_id == pipeline_run_id and task.status == "pending"
        ]
        for task in sorted(
            pending,
            key=lambda current: (current.created_at, current.sequence_index, current.id),
        ):
            task.status = "in_progress"
            task.claimed_at = task.created_at + timedelta(seconds=5)
            return task
        return None

    async def fake_get_pipeline_task(session, task_id):
        return next((task for task in state["tasks"] if task.id == task_id), None)

    async def fake_get_pipeline_run_tasks(session, pipeline_run_id):
        return sorted(
            [
                task
                for task in state["tasks"]
                if task.pipeline_run_id == pipeline_run_id
            ],
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
        current_task = next(task for task in state["tasks"] if task.id == task_id)
        current_task.status = task_status
        current_task.result_summary = result_summary
        current_task.completed_at = (
            current_task.claimed_at or current_task.created_at
        ) + timedelta(minutes=1)

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
                    retry_budget_key=payload.get("retry_budget_key"),
                    retry_attempt=payload.get("retry_attempt", 0),
                    failure_class=payload.get("failure_class"),
                    gate_id=uuid.UUID(payload["gate_id"])
                    if payload.get("gate_id")
                    else None,
                    status=payload.get("status", "pending"),
                )
            )
        return current_task, created

    async def fake_count_claimed_tasks_for_run(session, pipeline_run_id):
        return len(
            [
                task
                for task in state["tasks"]
                if task.pipeline_run_id == pipeline_run_id and task.claimed_at is not None
            ]
        )

    async def fake_list_pipeline_human_gates(session, pipeline_run_id):
        return [
            gate for gate in state["gates"] if gate.pipeline_run_id == pipeline_run_id
        ]

    async def fake_list_pipeline_evaluations(session, pipeline_run_id):
        return [
            evaluation
            for evaluation in state["evaluations"]
            if evaluation.pipeline_run_id == pipeline_run_id
        ]

    async def fake_get_successful_terminal_deployment(session, pipeline_run_id):
        successful = [
            row
            for row in state["deployments"].values()
            if row.pipeline_run_id == pipeline_run_id
            and row.status == "success"
            and row.exit_code == 0
        ]
        return successful[-1] if successful else None

    async def fake_stream_chat_with_intent(
        intent,
        session_id,
        user_message,
        project_id=None,
        thread_id_override=None,
    ):
        current_task = next(task for task in state["tasks"] if task.status == "in_progress")
        state["thread_ids"].append(thread_id_override or "")
        state["user_messages"].append(user_message)
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
                    _task_payload(
                        state,
                        current_task,
                        assigned_to="testing",
                        task_type="testing.generate_tests",
                        description="Generate Foundry tests for the generated contracts.",
                        artifact_revision=1,
                        context=_build_context(
                            "testing.generate_tests",
                            1,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": [],
                                "deployment": [],
                            },
                        ),
                    )
                ],
            )
        elif current_task.task_type == "testing.generate_tests":
            FakeMemoryManager.agent_states["testing"]["artifacts"] = [
                {
                    "path": "test/PartyTokenTest.t.sol",
                    "contract_names": ["PartyTokenTest"],
                }
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
                    _task_payload(
                        state,
                        current_task,
                        assigned_to="testing",
                        task_type="testing.run_tests",
                        description="Run the generated Foundry tests.",
                        artifact_revision=current_task.artifact_revision,
                        context=_build_context(
                            "testing.run_tests",
                            current_task.artifact_revision,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                "deployment": [],
                            },
                        ),
                    )
                ],
            )

        yield {"type": "done"}

    async def fake_execute_direct_task(task, project_id, user_id, pipeline_run_id):
        if task.task_type == "testing.run_tests":
            state["test_runs"][task.id] = FakeExecutionRow(
                id=uuid.uuid4(),
                pipeline_run_id=task.pipeline_run_id,
                pipeline_task_id=task.id,
                status="passed",
                exit_code=0,
                stdout_path=f"logs/{task.pipeline_run_id}/{task.id}/stdout.log",
                stderr_path=f"logs/{task.pipeline_run_id}/{task.id}/stderr.log",
            )
            next_tasks = []
            if not drain_after_tests:
                next_tasks.append(
                    _task_payload(
                        state,
                        task,
                        assigned_to="deployment",
                        task_type="deployment.prepare_script",
                        description="Prepare the Foundry deployment script.",
                        artifact_revision=task.artifact_revision,
                        context=_build_context(
                            "deployment.prepare_script",
                            task.artifact_revision,
                            {
                                "coding": FakeMemoryManager.agent_states["coding"]["artifacts"],
                                "testing": FakeMemoryManager.agent_states["testing"]["artifacts"],
                                "deployment": [],
                            },
                        ),
                    )
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
            return [
                {
                    "type": "tool_call",
                    "stage": "testing",
                    "tool": "run_foundry_tests",
                    "args": "[]",
                }
            ]

        if task.task_type == "deployment.prepare_script":
            FakeMemoryManager.agent_states["deployment"]["artifacts"] = [
                {
                    "path": "script/DeployPartyToken.s.sol",
                    "contract_names": ["DeployPartyToken"],
                }
            ]
            gate = FakeGate(
                id=uuid.uuid4(),
                pipeline_run_id=task.pipeline_run_id,
                pipeline_task_id=task.id,
                gate_type="pre_deploy",
                requested_reason="Deployment script is ready. Awaiting operator approval before on-chain deployment.",
                requested_payload={
                    "script_path": "script/DeployPartyToken.s.sol",
                    "contract_name": "PartyToken",
                },
                requested_by="system",
            )
            state["gates"].append(gate)
            await fake_complete_pipeline_task_and_create_next(
                None,
                pipeline_run_id=task.pipeline_run_id,
                project_id=task.project_id,
                task_id=task.id,
                task_status="completed",
                result_summary="Prepared deployment script at script/DeployPartyToken.s.sol; awaiting deploy approval.",
                created_by="deployment",
                next_tasks=[
                    _task_payload(
                        state,
                        task,
                        assigned_to="deployment",
                        task_type="deployment.execute_deploy",
                        description="Execute the prepared deployment script on Avalanche Fuji.",
                        artifact_revision=task.artifact_revision,
                        context={
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
                        status="waiting_for_approval",
                        gate_id=gate.id,
                    )
                ],
            )
            await fake_update_pipeline_run(
                None,
                task.pipeline_run_id,
                status="waiting_for_approval",
                paused_at=task.completed_at or task.claimed_at,
                current_stage="deployment",
                current_task_id=task.id,
                failure_class="human_gate",
                failure_reason=gate.requested_reason,
            )
            return [
                {
                    "type": "tool_call",
                    "stage": "deployment",
                    "tool": "save_deploy_artifact",
                    "args": "script/DeployPartyToken.s.sol",
                }
            ]

        if task.task_type == "deployment.execute_deploy":
            state["deployments"][task.id] = FakeExecutionRow(
                id=uuid.uuid4(),
                pipeline_run_id=task.pipeline_run_id,
                pipeline_task_id=task.id,
                status="success",
                exit_code=0,
                stdout_path=f"logs/{task.pipeline_run_id}/{task.id}/stdout.log",
                stderr_path=f"logs/{task.pipeline_run_id}/{task.id}/stderr.log",
            )
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
            return [
                {
                    "type": "tool_call",
                    "stage": "deployment",
                    "tool": "run_foundry_deploy",
                    "args": "{}",
                }
            ]

        return []

    async def fake_validate_execution_result(pipeline_run_id, task):
        if task.task_type == "testing.run_tests":
            row = state["test_runs"].get(task.id)
        elif task.task_type in orchestrator.TERMINAL_SUCCESS_TASK_TYPES:
            row = state["deployments"].get(task.id)
        else:
            return True, None, None

        if row is None:
            return False, "missing execution row", None
        if task.status == "completed" and row.exit_code != 0:
            return False, "completed task has non-zero exit code", None
        if task.status == "failed" and row.exit_code == 0:
            return False, "failed task has zero exit code", None
        return (
            True,
            None,
            {
                "status": row.status,
                "exit_code": row.exit_code,
                "stdout_path": row.stdout_path,
                "stderr_path": row.stderr_path,
            },
        )

    async def fake_postprocess_task(*, project_id, user_id, pipeline_run_id, task):
        return []

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "create_pipeline_run", fake_create_pipeline_run)
    monkeypatch.setattr(orchestrator, "update_pipeline_run", fake_update_pipeline_run)
    monkeypatch.setattr(orchestrator, "get_pipeline_run", fake_get_pipeline_run)
    monkeypatch.setattr(orchestrator, "get_current_plan_row", fake_get_current_plan_row)
    monkeypatch.setattr(orchestrator, "get_next_retry_attempt", fake_get_next_retry_attempt)
    monkeypatch.setattr(orchestrator, "create_pipeline_task", fake_create_pipeline_task)
    monkeypatch.setattr(orchestrator, "claim_next_pending_task", fake_claim_next_pending_task)
    monkeypatch.setattr(
        orchestrator,
        "complete_pipeline_task_and_create_next",
        fake_complete_pipeline_task_and_create_next,
    )
    monkeypatch.setattr(orchestrator, "get_pipeline_task", fake_get_pipeline_task)
    monkeypatch.setattr(orchestrator, "get_pipeline_run_tasks", fake_get_pipeline_run_tasks)
    monkeypatch.setattr(orchestrator, "count_claimed_tasks_for_run", fake_count_claimed_tasks_for_run)
    monkeypatch.setattr(orchestrator, "list_pipeline_human_gates", fake_list_pipeline_human_gates)
    monkeypatch.setattr(orchestrator, "list_pipeline_evaluations", fake_list_pipeline_evaluations)
    monkeypatch.setattr(
        orchestrator,
        "get_successful_terminal_deployment",
        fake_get_successful_terminal_deployment,
    )
    monkeypatch.setattr(orchestrator, "stream_chat_with_intent", fake_stream_chat_with_intent)
    monkeypatch.setattr(orchestrator, "_execute_direct_task", fake_execute_direct_task)
    monkeypatch.setattr(orchestrator, "_validate_execution_result", fake_validate_execution_result)
    monkeypatch.setattr(orchestrator, "_postprocess_task", fake_postprocess_task)
    monkeypatch.setattr(orchestrator, "MemoryManager", FakeMemoryManager)
    monkeypatch.setattr(orchestrator, "is_pipeline_cancelled", lambda pipeline_run_id: False)
    monkeypatch.setattr(
        orchestrator,
        "_update_plan_status",
        lambda project_id, user_id, status: state["status_updates"].append(status),
    )
    return state


def _collect_events(project_id: str, user_id: str, pipeline_run_id: str | None = None):
    async def _run():
        return [
            event
            async for event in orchestrator.run_autonomous_pipeline(
                project_id=project_id,
                user_id=user_id,
                pipeline_run_id=pipeline_run_id,
            )
        ]

    return asyncio.run(_run())


def test_pipeline_pauses_for_pre_deploy_gate_after_prepare_script(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)

    assert any(event["type"] == "pipeline_start" for event in events)
    assert any(event["type"] == "pipeline_waiting_for_approval" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert state["run"].status == "waiting_for_approval"
    assert len(state["gates"]) == 1
    assert state["gates"][0].gate_type == "pre_deploy"
    waiting_task = next(
        task for task in state["tasks"] if task.task_type == "deployment.execute_deploy"
    )
    assert waiting_task.status == "waiting_for_approval"
    assert waiting_task.gate_id == state["gates"][0].id


def test_pipeline_resumes_after_gate_approval_and_completes(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    first_pass = _collect_events(project_id, user_id)
    pipeline_run_id = next(
        event for event in first_pass if event["type"] == "pipeline_start"
    )["pipeline_run_id"]

    gate = state["gates"][0]
    gate.status = "approved"
    gate.resolved_by = "operator"
    gate.resolved_reason = "Approved for deployment."
    gate.resolved_at = datetime.now(timezone.utc)
    waiting_task = next(
        task for task in state["tasks"] if task.task_type == "deployment.execute_deploy"
    )
    waiting_task.status = "pending"

    second_pass = _collect_events(project_id, user_id, pipeline_run_id=pipeline_run_id)

    assert any(event["type"] == "pipeline_resumed" for event in second_pass)
    assert any(event["type"] == "pipeline_complete" for event in second_pass)
    assert state["run"].status == "completed"
    assert state["run"].terminal_deployment_id is not None
    assert state["status_updates"][-1] == "deployed"


def test_pipeline_fails_when_queue_drains_without_successful_terminal_deploy(monkeypatch):
    state = _build_harness(monkeypatch, drain_after_tests=True)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)

    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert state["run"].status == "failed"
    assert state["status_updates"][-1] == "failed"


def test_pipeline_uses_per_task_thread_ids_and_context(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)
    pipeline_run_id = next(
        event for event in events if event["type"] == "pipeline_start"
    )["pipeline_run_id"]

    assert len(state["thread_ids"]) == 2
    assert all(
        thread_id.startswith(f"pipeline:{pipeline_run_id}:")
        for thread_id in state["thread_ids"]
    )
    assert all("Pipeline task context:" in message for message in state["user_messages"])


def test_pipeline_status_includes_run_gates_and_evaluations(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)
    pipeline_run_id = next(
        event for event in events if event["type"] == "pipeline_start"
    )["pipeline_run_id"]
    prepare_task = next(
        task for task in state["tasks"] if task.task_type == "deployment.prepare_script"
    )
    state["evaluations"].append(
        FakeEvaluation(
            id=uuid.uuid4(),
            pipeline_run_id=uuid.UUID(pipeline_run_id),
            pipeline_task_id=prepare_task.id,
            stage="deployment",
            evaluation_type="deployment_prepare",
            blocking=True,
            status="passed",
            summary="Deployment script matches the manifest.",
            artifact_revision=prepare_task.artifact_revision,
            details_json={"script_path": "script/DeployPartyToken.s.sol"},
            trace_id="trace-eval",
        )
    )

    status = asyncio.run(
        orchestrator.get_pipeline_status(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
        )
    )

    first_task = status["tasks"][0]
    run_tests_task = next(
        task for task in status["tasks"] if task["task_type"] == "testing.run_tests"
    )

    assert status["status"] == "waiting_for_approval"
    assert status["run"]["status"] == "waiting_for_approval"
    assert status["gates"][0]["gate_type"] == "pre_deploy"
    assert status["evaluations"][0]["evaluation_type"] == "deployment_prepare"
    assert first_task["context"]["plan_summary"]["project_name"] == "PartyToken"
    assert first_task["claimed_at"] is not None
    assert first_task["queue_duration_ms"] is not None
    assert run_tests_task["artifact_revision"] == 1
    assert (
        run_tests_task["context"]["input_artifacts"]["coding"][0]["path"]
        == "contracts/PartyToken.sol"
    )


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
