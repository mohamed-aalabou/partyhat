import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agents.deployment_manifest import load_deployment_manifest
from agents import pipeline_orchestrator as orchestrator
from agents import pipeline_evaluations
from agents.pipeline_specs import (
    default_deployment_target_payload,
    retry_budget_key_for_task,
)

PLAN_CONTRACT_ID = "pc_partytoken"


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
    runner_token: str | None = None
    runner_started_at: datetime | None = None
    runner_heartbeat_at: datetime | None = None
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
                "plan_contract_id": PLAN_CONTRACT_ID,
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
            "plan_contracts": [
                {
                    "plan_contract_id": PLAN_CONTRACT_ID,
                    "name": "PartyToken",
                    "deployment_role": "primary_deployable",
                    "deploy_order": 1,
                }
            ],
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
        "snapshot": None,
        "tasks": [],
        "gates": [],
        "evaluations": [],
        "test_runs": {},
        "deployments": {},
        "notifications": [],
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
                "plan_contracts": [
                    {
                        "plan_contract_id": PLAN_CONTRACT_ID,
                        "name": "PartyToken",
                        "deployment_role": "primary_deployable",
                        "deploy_order": 1,
                    }
                ],
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

    async def fake_get_pipeline_run_snapshot(session, pipeline_run_id):
        run = state["run"]
        snapshot = state["snapshot"]
        if run is None or run.id != pipeline_run_id or snapshot is None:
            return None
        return snapshot

    async def fake_refresh_pipeline_run_snapshot(session, pipeline_run_id):
        run = state["run"]
        if run is None or run.id != pipeline_run_id:
            return None
        payload = orchestrator.build_pipeline_status_payload(
            project_id=str(run.project_id),
            pipeline_run_id=str(run.id),
            run=run,
            tasks=[
                task
                for task in state["tasks"]
                if task.pipeline_run_id == pipeline_run_id
            ],
            gates=[
                gate
                for gate in state["gates"]
                if gate.pipeline_run_id == pipeline_run_id
            ],
            evaluations=[
                evaluation
                for evaluation in state["evaluations"]
                if evaluation.pipeline_run_id == pipeline_run_id
            ],
        )
        snapshot = SimpleNamespace(
            pipeline_run_id=pipeline_run_id,
            project_id=run.project_id,
            status=payload["status"],
            failure_reason=payload["failure_reason"],
            snapshot_json=payload,
            version=(getattr(state["snapshot"], "version", 0) or 0) + 1,
            updated_at=run.updated_at,
        )
        state["snapshot"] = snapshot
        return snapshot

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
                {
                    "path": "contracts/PartyToken.sol",
                    "contract_names": ["PartyToken"],
                    "plan_contract_ids": [PLAN_CONTRACT_ID],
                }
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
                    "plan_contract_ids": [PLAN_CONTRACT_ID],
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
                    "plan_contract_ids": [PLAN_CONTRACT_ID],
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
                    "plan_contract_id": PLAN_CONTRACT_ID,
                    "plan_contract_ids": [PLAN_CONTRACT_ID],
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
                            "plan_contract_id": PLAN_CONTRACT_ID,
                            "plan_contract_ids": [PLAN_CONTRACT_ID],
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

    async def fake_finalize_pipeline_terminal_status(
        *,
        project_id,
        user_id,
        pipeline_run_id,
        status,
        terminal_deployment=None,
        failure_class=None,
        failure_reason=None,
    ):
        run = state["run"]
        assert run is not None
        assert str(run.id) == pipeline_run_id
        run.status = status
        run.completed_at = datetime.now(timezone.utc)
        run.failure_class = failure_class
        run.failure_reason = failure_reason
        run.terminal_deployment_id = (
            terminal_deployment.id if terminal_deployment is not None else None
        )
        state["notifications"].append(
            {
                "pipeline_run_id": pipeline_run_id,
                "status": status,
                "failure_reason": failure_reason,
                "terminal_deployment_id": (
                    str(terminal_deployment.id)
                    if terminal_deployment is not None
                    else None
                ),
            }
        )

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "create_pipeline_run", fake_create_pipeline_run)
    monkeypatch.setattr(orchestrator, "update_pipeline_run", fake_update_pipeline_run)
    monkeypatch.setattr(orchestrator, "get_pipeline_run", fake_get_pipeline_run)
    monkeypatch.setattr(orchestrator, "get_pipeline_run_snapshot", fake_get_pipeline_run_snapshot)
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
        "refresh_pipeline_run_snapshot",
        fake_refresh_pipeline_run_snapshot,
    )
    monkeypatch.setattr(
        orchestrator,
        "get_successful_terminal_deployment",
        fake_get_successful_terminal_deployment,
    )
    monkeypatch.setattr(orchestrator, "stream_chat_with_intent", fake_stream_chat_with_intent)
    monkeypatch.setattr(orchestrator, "_execute_direct_task", fake_execute_direct_task)
    monkeypatch.setattr(orchestrator, "_validate_execution_result", fake_validate_execution_result)
    monkeypatch.setattr(orchestrator, "_postprocess_task", fake_postprocess_task)
    monkeypatch.setattr(
        orchestrator,
        "_finalize_pipeline_terminal_status",
        fake_finalize_pipeline_terminal_status,
    )
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
    assert state["gates"][0].requested_payload["plan_contract_id"] == PLAN_CONTRACT_ID
    waiting_task = next(
        task for task in state["tasks"] if task.task_type == "deployment.execute_deploy"
    )
    assert waiting_task.status == "waiting_for_approval"
    assert waiting_task.gate_id == state["gates"][0].id
    assert waiting_task.context["plan_contract_id"] == PLAN_CONTRACT_ID


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
    assert state["notifications"][-1]["status"] == "completed"


def test_pipeline_fails_when_queue_drains_without_successful_terminal_deploy(monkeypatch):
    state = _build_harness(monkeypatch, drain_after_tests=True)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)

    assert any(event["type"] == "pipeline_error" for event in events)
    assert not any(event["type"] == "pipeline_complete" for event in events)
    assert state["run"].status == "failed"
    assert state["status_updates"][-1] == "failed"
    assert state["notifications"][-1]["status"] == "failed"


def test_pipeline_cancellation_enqueues_terminal_notification(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    monkeypatch.setattr(orchestrator, "is_pipeline_cancelled", lambda pipeline_run_id: True)

    events = _collect_events(project_id, user_id)

    assert any(event["type"] == "pipeline_cancelled" for event in events)
    assert state["run"].status == "cancelled"
    assert state["notifications"][-1]["status"] == "cancelled"


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
    assert status["gates"][0]["requested_payload"]["plan_contract_id"] == PLAN_CONTRACT_ID
    assert status["evaluations"][0]["evaluation_type"] == "deployment_prepare"
    assert first_task["context"]["plan_summary"]["project_name"] == "PartyToken"
    assert first_task["claimed_at"] is not None
    assert first_task["queue_duration_ms"] is not None
    assert run_tests_task["artifact_revision"] == 1
    assert (
        run_tests_task["context"]["input_artifacts"]["coding"][0]["path"]
        == "contracts/PartyToken.sol"
    )


def test_pipeline_status_uses_snapshot_fast_path(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)
    pipeline_run_id = next(
        event for event in events if event["type"] == "pipeline_start"
    )["pipeline_run_id"]
    run = state["run"]
    assert run is not None

    snapshot_payload = orchestrator.build_pipeline_status_payload(
        project_id=project_id,
        pipeline_run_id=pipeline_run_id,
        run=run,
        tasks=[
            task
            for task in state["tasks"]
            if task.pipeline_run_id == uuid.UUID(pipeline_run_id)
        ],
        gates=[
            gate
            for gate in state["gates"]
            if gate.pipeline_run_id == uuid.UUID(pipeline_run_id)
        ],
        evaluations=[
            evaluation
            for evaluation in state["evaluations"]
            if evaluation.pipeline_run_id == uuid.UUID(pipeline_run_id)
        ],
    )
    state["snapshot"] = SimpleNamespace(
        pipeline_run_id=uuid.UUID(pipeline_run_id),
        project_id=run.project_id,
        status=snapshot_payload["status"],
        failure_reason=snapshot_payload["failure_reason"],
        snapshot_json=snapshot_payload,
        version=1,
        updated_at=run.updated_at,
    )

    async def fail_get_pipeline_run_tasks(*_args, **_kwargs):
        raise AssertionError("snapshot fast path should not load tasks")

    monkeypatch.setattr(orchestrator, "get_pipeline_run_tasks", fail_get_pipeline_run_tasks)

    status = asyncio.run(
        orchestrator.get_pipeline_status(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
        )
    )

    assert status["pipeline_run_id"] == pipeline_run_id
    assert status["run"]["status"] == state["snapshot"].status
    assert len(status["tasks"]) == len(snapshot_payload["tasks"])


def test_pipeline_status_summary_only_omits_collections(monkeypatch):
    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    events = _collect_events(project_id, user_id)
    pipeline_run_id = next(
        event for event in events if event["type"] == "pipeline_start"
    )["pipeline_run_id"]

    status = asyncio.run(
        orchestrator.get_pipeline_status(
            project_id=project_id,
            user_id=user_id,
            pipeline_run_id=pipeline_run_id,
            include_tasks=False,
            include_gates=False,
            include_evaluations=False,
        )
    )

    assert status["total_tasks"] == len(state["tasks"])
    assert status["tasks"] == []
    assert status["gates"] == []
    assert status["evaluations"] == []


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


def test_pipeline_run_canonicalizes_legacy_fuji_deployment_target(monkeypatch):
    legacy_plan = {
        **FakeMemoryManager.plan,
        "deployment_target": {
            "network": "avalanche",
            "name": "fuji",
            "description": "Avalanche Fuji testnet",
            "chain_id": 43113,
            "rpc_url_env_var": "FUJI_RPC_URL",
            "private_key_env_var": "FUJI_PRIVATE_KEY",
        },
    }
    monkeypatch.setattr(FakeMemoryManager, "plan", legacy_plan, raising=False)

    state = _build_harness(monkeypatch)
    project_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    _collect_events(project_id, user_id)

    assert state["run"].deployment_target["network"] == default_deployment_target_payload()["network"]
    assert state["run"].deployment_target["name"] == default_deployment_target_payload()["name"]
    assert state["run"].deployment_target["chain_id"] == default_deployment_target_payload()["chain_id"]
    assert (
        state["run"].deployment_target["rpc_url_env_var"]
        == default_deployment_target_payload()["rpc_url_env_var"]
    )
    assert (
        state["run"].deployment_target["private_key_env_var"]
        == default_deployment_target_payload()["private_key_env_var"]
    )


def _multi_contract_manifest():
    return load_deployment_manifest(
        {
            "deployment_target": default_deployment_target_payload(),
            "contracts": [
                {
                    "plan_contract_id": "pc_vesting",
                    "name": "AvaVestVesting",
                    "role": "primary_deployable",
                    "deploy_order": 1,
                    "source_path": "contracts/AvaVestVesting.sol",
                    "constructor_args_schema": [],
                },
                {
                    "plan_contract_id": "pc_token",
                    "name": "AvaVestToken",
                    "role": "supporting",
                    "deploy_order": 2,
                    "source_path": "contracts/AvaVestToken.sol",
                    "constructor_args_schema": [
                        {
                            "name": "vesting",
                            "type": "address",
                            "source": "plan_default",
                            "default_value": "<deployed:AvaVestVesting.address>",
                        }
                    ],
                },
            ],
            "post_deploy_calls": [
                {
                    "target_contract_name": "AvaVestVesting",
                    "target_plan_contract_id": "pc_vesting",
                    "function_name": "setToken",
                    "args": ["<deployed:AvaVestToken.address>"],
                    "call_order": 1,
                    "description": "Wire token",
                }
            ],
        }
    )


def test_handle_prepare_script_passes_full_manifest_and_all_contract_ids(monkeypatch):
    manifest = _multi_contract_manifest()
    captured: dict[str, object] = {}

    class MinimalMemoryManager:
        def __init__(self, user_id: str, project_id: str):
            self.user_id = user_id
            self.project_id = project_id

        def get_agent_state(self, agent_name: str) -> dict:
            if agent_name == "coding":
                return {
                    "artifacts": [
                        {
                            "path": "contracts/AvaVestVesting.sol",
                            "contract_names": ["AvaVestVesting"],
                            "plan_contract_ids": ["pc_vesting"],
                        },
                        {
                            "path": "contracts/AvaVestToken.sol",
                            "contract_names": ["AvaVestToken"],
                            "plan_contract_ids": ["pc_token"],
                        },
                    ]
                }
            if agent_name == "planning":
                return {
                    "plan_summary": {
                        "project_name": "AvaVest",
                        "plan_contracts": [
                            {
                                "plan_contract_id": "pc_vesting",
                                "name": "AvaVestVesting",
                                "deployment_role": "primary_deployable",
                                "deploy_order": 1,
                            },
                            {
                                "plan_contract_id": "pc_token",
                                "name": "AvaVestToken",
                                "deployment_role": "supporting",
                                "deploy_order": 2,
                            },
                        ],
                    }
                }
            return {"artifacts": []}

        def get_plan(self) -> dict:
            return {
                "project_name": "AvaVest",
                "contracts": [
                    {"name": "AvaVestVesting"},
                    {"name": "AvaVestToken"},
                ],
            }

    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="testing",
        task_type="deployment.prepare_script",
        description="Prepare deployment script",
        status="in_progress",
        context={},
        artifact_revision=3,
    )

    monkeypatch.setattr(orchestrator, "MemoryManager", MinimalMemoryManager)
    monkeypatch.setattr(orchestrator, "load_saved_manifest", lambda project_id: (manifest, None))
    monkeypatch.setattr(
        orchestrator,
        "load_code_artifact",
        SimpleNamespace(
            func=lambda path: {
                "code": f"contract {path.split('/')[-1].replace('.sol', '')} {{}}"
            }
        ),
    )

    def fake_generate(request):
        captured["request"] = request
        return {"generated_script": "// script"}

    def fake_save_artifact(artifact):
        captured["artifact"] = artifact
        return {"success": True}

    monkeypatch.setattr(orchestrator, "generate_foundry_deploy_script_direct", fake_generate)
    monkeypatch.setattr(
        orchestrator,
        "save_deploy_artifact",
        SimpleNamespace(func=fake_save_artifact),
    )
    monkeypatch.setattr(
        orchestrator,
        "evaluate_deployment_prepare",
        lambda project_id, user_id, script_path: {"status": "passed", "summary": "ok"},
    )

    async def fake_record_pipeline_evaluation(**kwargs):
        return uuid.uuid4()

    async def fake_create_gate_for_task(**kwargs):
        return FakeGate(
            id=uuid.uuid4(),
            pipeline_run_id=uuid.UUID(kwargs["pipeline_run_id"]),
            pipeline_task_id=kwargs["task"].id,
            gate_type="pre_deploy",
        )

    async def fake_next_task_payload(*args, **kwargs):
        return {
            "assigned_to": kwargs["assigned_to"],
            "task_type": kwargs["task_type"],
            "description": kwargs["description"],
            "parent_task_id": str(task.id),
            "sequence_index": 0,
            "artifact_revision": kwargs["artifact_revision"],
            "retry_budget_key": "deploy",
            "retry_attempt": 0,
            "status": kwargs.get("status", "pending"),
            "gate_id": kwargs.get("gate_id"),
            "context": kwargs["context"],
        }

    async def fake_finalize_direct_task(**kwargs):
        captured["finalize"] = kwargs

    monkeypatch.setattr(orchestrator, "_record_pipeline_evaluation", fake_record_pipeline_evaluation)
    monkeypatch.setattr(orchestrator, "_create_gate_for_task", fake_create_gate_for_task)
    monkeypatch.setattr(orchestrator, "_next_task_payload", fake_next_task_payload)
    monkeypatch.setattr(orchestrator, "_finalize_direct_task", fake_finalize_direct_task)

    events = asyncio.run(
        orchestrator._handle_prepare_script(
            task,
            project_id=str(task.project_id),
            user_id=str(uuid.uuid4()),
            pipeline_run_id=str(task.pipeline_run_id),
        )
    )

    assert events[0]["tool"] == "generate_foundry_deploy_script_direct"
    request = captured["request"]
    assert request.deployment_manifest["contracts"][0]["name"] == "AvaVestVesting"
    assert request.deployment_manifest["contracts"][1]["name"] == "AvaVestToken"
    assert "// contracts/AvaVestVesting.sol" in request.contract_sources
    assert "// contracts/AvaVestToken.sol" in request.contract_sources
    artifact = captured["artifact"]
    assert artifact.plan_contract_ids == ["pc_vesting", "pc_token"]
    next_task = captured["finalize"]["next_tasks"][0]
    assert next_task["task_type"] == "deployment.execute_deploy"
    assert next_task["context"]["plan_contract_ids"] == ["pc_vesting", "pc_token"]


def test_handle_prepare_script_compile_preflight_failure_does_not_enqueue_execute(monkeypatch):
    manifest = _multi_contract_manifest()
    captured: dict[str, object] = {}
    stored_scripts: dict[str, str] = {}

    class MinimalMemoryManager:
        def __init__(self, user_id: str, project_id: str):
            self.user_id = user_id
            self.project_id = project_id

        def get_agent_state(self, agent_name: str) -> dict:
            if agent_name == "coding":
                return {
                    "artifacts": [
                        {
                            "path": "contracts/AvaVestVesting.sol",
                            "contract_names": ["AvaVestVesting"],
                            "plan_contract_ids": ["pc_vesting"],
                        },
                        {
                            "path": "contracts/AvaVestToken.sol",
                            "contract_names": ["AvaVestToken"],
                            "plan_contract_ids": ["pc_token"],
                        },
                    ]
                }
            if agent_name == "planning":
                return {
                    "plan_summary": {
                        "project_name": "AvaVest",
                        "plan_contracts": [
                            {
                                "plan_contract_id": "pc_vesting",
                                "name": "AvaVestVesting",
                                "deployment_role": "primary_deployable",
                                "deploy_order": 1,
                            },
                            {
                                "plan_contract_id": "pc_token",
                                "name": "AvaVestToken",
                                "deployment_role": "supporting",
                                "deploy_order": 2,
                            },
                        ],
                    },
                    "latest_artifact_revision": 3,
                }
            return {}

        def get_plan(self) -> dict:
            return {
                "project_name": "AvaVest",
                "contracts": [
                    {"name": "AvaVestVesting"},
                    {"name": "AvaVestToken"},
                ],
            }

    class FakeStorage:
        def load_code(self, path: str) -> str:
            return stored_scripts[path]

    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="testing",
        task_type="deployment.prepare_script",
        description="Prepare deployment script",
        status="in_progress",
        context={},
        artifact_revision=3,
    )

    monkeypatch.setattr(orchestrator, "MemoryManager", MinimalMemoryManager)
    monkeypatch.setattr(
        pipeline_evaluations,
        "_memory_manager",
        lambda project_id, user_id: MinimalMemoryManager(user_id, project_id),
    )
    monkeypatch.setattr(orchestrator, "load_saved_manifest", lambda project_id: (manifest, None))
    monkeypatch.setattr(
        pipeline_evaluations,
        "load_saved_manifest",
        lambda project_id: (manifest, None),
    )
    monkeypatch.setattr(
        orchestrator,
        "load_code_artifact",
        SimpleNamespace(
            func=lambda path: {
                "code": f"contract {path.split('/')[-1].replace('.sol', '')} {{}}"
            }
        ),
    )
    monkeypatch.setattr(
        pipeline_evaluations,
        "get_code_storage",
        lambda project_id: FakeStorage(),
    )
    monkeypatch.setattr(
        pipeline_evaluations,
        "preflight_compile_deploy_script",
        lambda **kwargs: {
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "Error: Undeclared identifier.\n  --> script/DeployAvaVestVesting.s.sol:10:20:",
            "summary": "exit_code=1: Error: Undeclared identifier.",
            "command": "forge script script/DeployAvaVestVesting.s.sol",
        },
    )

    def fake_save_artifact(artifact):
        stored_scripts[artifact.path] = artifact.code or ""
        captured["artifact"] = artifact
        return {"success": True}

    async def fake_record_pipeline_evaluation(**kwargs):
        captured["evaluation"] = kwargs["evaluation"]
        return uuid.uuid4()

    async def fake_create_gate_for_task(**kwargs):
        captured["gate"] = kwargs
        return FakeGate(
            id=uuid.uuid4(),
            pipeline_run_id=uuid.UUID(kwargs["pipeline_run_id"]),
            pipeline_task_id=kwargs["task"].id,
            gate_type="override",
        )

    async def fake_update_pipeline_task(session, task_id, **fields):
        captured["task_update"] = {
            "task_id": task_id,
            **fields,
        }
        return SimpleNamespace(id=task_id, **fields)

    monkeypatch.setattr(
        orchestrator,
        "save_deploy_artifact",
        SimpleNamespace(func=fake_save_artifact),
    )
    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "_record_pipeline_evaluation", fake_record_pipeline_evaluation)
    monkeypatch.setattr(orchestrator, "_create_gate_for_task", fake_create_gate_for_task)
    monkeypatch.setattr(orchestrator, "update_pipeline_task", fake_update_pipeline_task)

    events = asyncio.run(
        orchestrator._handle_prepare_script(
            task,
            project_id=str(task.project_id),
            user_id=str(uuid.uuid4()),
            pipeline_run_id=str(task.pipeline_run_id),
        )
    )

    assert events[0]["tool"] == "generate_foundry_deploy_script_direct"
    assert captured["evaluation"]["status"] == "failed"
    assert "failed compile preflight" in captured["evaluation"]["summary"]
    assert (
        captured["evaluation"]["details"]["compile_preflight"]["stderr"]
        .startswith("Error: Undeclared identifier.")
    )
    assert captured["gate"]["gate_type"] == "override"
    assert captured["task_update"]["status"] == "waiting_for_approval"
    assert captured["task_update"]["failure_class"] == "human_gate"
    assert captured["task_update"]["result_summary"] == captured["evaluation"]["summary"]


def test_handle_prepare_script_pauses_for_unresolved_post_deploy_inputs(monkeypatch):
    captured: dict[str, object] = {"generated": False}
    manifest = load_deployment_manifest(
        {
            "deployment_target": default_deployment_target_payload(),
            "contracts": [
                {
                    "plan_contract_id": "pc_publish",
                    "name": "PublishingEditions1155",
                    "role": "primary_deployable",
                    "deploy_order": 1,
                    "source_path": "contracts/PublishingEditions1155.sol",
                    "constructor_args_schema": [],
                }
            ],
            "post_deploy_calls": [
                {
                    "target_contract_name": "PublishingEditions1155",
                    "target_plan_contract_id": "pc_publish",
                    "function_name": "createEdition",
                    "args": ["1", "pass:supporter", "TBD", "TBD"],
                    "call_order": 1,
                    "description": "Create supporter pass",
                }
            ],
        }
    )

    class MinimalMemoryManager:
        def __init__(self, user_id: str, project_id: str):
            self.user_id = user_id
            self.project_id = project_id

        def get_agent_state(self, agent_name: str) -> dict:
            if agent_name == "planning":
                return {
                    "plan_summary": {
                        "project_name": "Publishing",
                        "plan_contracts": [
                            {
                                "plan_contract_id": "pc_publish",
                                "name": "PublishingEditions1155",
                                "deployment_role": "primary_deployable",
                                "deploy_order": 1,
                            }
                        ],
                    }
                }
            return {"artifacts": []}

        def get_plan(self) -> dict:
            return {
                "project_name": "Publishing",
                "contracts": [
                    {
                        "plan_contract_id": "pc_publish",
                        "name": "PublishingEditions1155",
                        "functions": [
                            {
                                "name": "createEdition",
                                "inputs": [
                                    {"name": "tokenId", "type": "uint256"},
                                    {"name": "key", "type": "string"},
                                    {"name": "maxSupply", "type": "uint256"},
                                    {"name": "uri", "type": "string"},
                                ],
                            }
                        ],
                    }
                ],
            }

    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="testing",
        task_type="deployment.prepare_script",
        description="Prepare deployment script",
        status="in_progress",
        context={},
        artifact_revision=3,
    )

    async def fake_record_pipeline_evaluation(**kwargs):
        captured["evaluation"] = kwargs["evaluation"]
        return uuid.uuid4()

    async def fake_create_gate_for_task(**kwargs):
        captured["gate"] = kwargs
        return FakeGate(
            id=uuid.uuid4(),
            pipeline_run_id=uuid.UUID(kwargs["pipeline_run_id"]),
            pipeline_task_id=kwargs["task"].id,
            gate_type="override",
        )

    async def fake_update_pipeline_task(session, task_id, **fields):
        captured["task_update"] = {"task_id": task_id, **fields}
        return SimpleNamespace(id=task_id, **fields)

    def fake_generate(request):
        captured["generated"] = True
        return {"generated_script": "// should not run"}

    monkeypatch.setattr(orchestrator, "MemoryManager", MinimalMemoryManager)
    monkeypatch.setattr(orchestrator, "load_saved_manifest", lambda project_id: (manifest, None))
    monkeypatch.setattr(orchestrator, "generate_foundry_deploy_script_direct", fake_generate)
    monkeypatch.setattr(orchestrator, "_record_pipeline_evaluation", fake_record_pipeline_evaluation)
    monkeypatch.setattr(orchestrator, "_create_gate_for_task", fake_create_gate_for_task)
    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "update_pipeline_task", fake_update_pipeline_task)

    events = asyncio.run(
        orchestrator._handle_prepare_script(
            task,
            project_id=str(task.project_id),
            user_id=str(uuid.uuid4()),
            pipeline_run_id=str(task.pipeline_run_id),
        )
    )

    assert events == [
        {
            "type": "tool_call",
            "stage": "deployment",
            "tool": "generate_foundry_deploy_script_direct",
            "args": "{}",
        }
    ]
    assert captured["generated"] is False
    assert captured["evaluation"]["status"] == "failed"
    assert "unresolved post-deploy inputs" in captured["evaluation"]["summary"]
    assert any(
        "arg 2" in note and '"pass:supporter"' in note
        for note in captured["evaluation"]["details"]["remediations"]
    )
    assert any(
        "arg 3" in issue and "unresolved value 'TBD'" in issue
        for issue in captured["evaluation"]["details"]["issues"]
    )
    assert captured["gate"]["gate_type"] == "override"
    assert captured["task_update"]["status"] == "waiting_for_approval"
    assert captured["task_update"]["failure_class"] == "human_gate"


def test_next_task_payload_increments_same_retry_key_even_if_db_attempt_is_stale(monkeypatch):
    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="testing",
        task_type="deployment.prepare_script",
        description="Prepare deployment script",
        status="in_progress",
        context={},
        artifact_revision=3,
        retry_budget_key="deployment.prepare",
        retry_attempt=0,
    )

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())

    async def fake_get_next_retry_attempt(session, pipeline_run_id, retry_budget_key):
        return 0

    monkeypatch.setattr(orchestrator, "get_next_retry_attempt", fake_get_next_retry_attempt)

    payload = asyncio.run(
        orchestrator._next_task_payload(
            str(task.pipeline_run_id),
            task,
            assigned_to="deployment",
            task_type="deployment.prepare_script",
            description="Retry prepare script",
            context=None,
            artifact_revision=task.artifact_revision,
            task_status="failed",
            result_summary="compile preflight failed",
            failure_class="evaluation_failed",
        )
    )

    assert payload["retry_budget_key"] == "deployment.prepare"
    assert payload["retry_attempt"] == 1


def test_handle_execute_deploy_records_multi_contract_results(monkeypatch):
    manifest = _multi_contract_manifest()
    captured: dict[str, object] = {}

    class MinimalMemoryManager:
        def __init__(self, user_id: str, project_id: str):
            self.user_id = user_id
            self.project_id = project_id

        def get_agent_state(self, agent_name: str) -> dict:
            return {"artifacts": []}

    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="testing",
        task_type="deployment.execute_deploy",
        description="Execute deployment",
        status="in_progress",
        artifact_revision=4,
        context={
            "script_path": "script/DeployAvaVestVesting.s.sol",
            "contract_name": "AvaVestVesting",
            "plan_contract_id": "pc_vesting",
            "plan_contract_ids": ["pc_vesting", "pc_token"],
        },
    )

    monkeypatch.setattr(orchestrator, "MemoryManager", MinimalMemoryManager)
    monkeypatch.setattr(orchestrator, "load_saved_manifest", lambda project_id: (manifest, None))

    def fake_run_deploy(request):
        captured["request"] = request
        return {
            "success": True,
            "exit_code": 0,
            "command": "forge script script/DeployAvaVestVesting.s.sol --broadcast",
            "stdout_path": "stdout.log",
            "stderr_path": "stderr.log",
            "tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "deployed_address": "0x1111111111111111111111111111111111111111",
            "deployed_contracts": [
                {
                    "contract_name": "AvaVestVesting",
                    "plan_contract_id": "pc_vesting",
                    "deploy_order": 1,
                    "tx_hash": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "deployed_address": "0x1111111111111111111111111111111111111111",
                },
                {
                    "contract_name": "AvaVestToken",
                    "plan_contract_id": "pc_token",
                    "deploy_order": 2,
                    "tx_hash": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "deployed_address": "0x2222222222222222222222222222222222222222",
                },
            ],
            "executed_calls": [
                {
                    "target_contract_name": "AvaVestVesting",
                    "target_plan_contract_id": "pc_vesting",
                    "function_name": "setToken",
                    "args": ["<deployed:AvaVestToken.address>"],
                    "call_order": 1,
                    "tx_hash": "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "status": "success",
                }
            ],
        }

    def fake_record_deployment(record):
        captured["record"] = record
        return {"success": True}

    async def fake_finalize_direct_task(**kwargs):
        captured["finalize"] = kwargs

    monkeypatch.setattr(
        orchestrator,
        "run_foundry_deploy",
        SimpleNamespace(func=fake_run_deploy),
    )
    monkeypatch.setattr(
        orchestrator,
        "record_deployment",
        SimpleNamespace(func=fake_record_deployment),
    )
    monkeypatch.setattr(orchestrator, "_finalize_direct_task", fake_finalize_direct_task)

    events = asyncio.run(
        orchestrator._handle_execute_deploy(
            task,
            project_id=str(task.project_id),
            user_id=str(uuid.uuid4()),
            pipeline_run_id=str(task.pipeline_run_id),
        )
    )

    assert events[0]["tool"] == "run_foundry_deploy"
    assert captured["request"].deployment_manifest["contracts"][1]["name"] == "AvaVestToken"
    assert len(captured["record"].deployed_contracts) == 2
    assert captured["record"].executed_calls[0].function_name == "setToken"
    assert captured["finalize"]["task_status"] == "completed"


def test_validate_execution_result_surfaces_persistence_failure_summary(monkeypatch):
    task = FakeTask(
        id=uuid.uuid4(),
        pipeline_run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        assigned_to="deployment",
        created_by="deployment",
        task_type="deployment.execute_deploy",
        description="Execute deployment",
        status="failed",
        result_summary=(
            "Deployment executed but authoritative deployment record could not be "
            "persisted: db write failed"
        ),
    )

    async def fake_get_deployment_for_task(session, pipeline_run_id, pipeline_task_id):
        return None

    monkeypatch.setattr(orchestrator, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(orchestrator, "get_deployment_for_task", fake_get_deployment_for_task)

    valid, error, execution = asyncio.run(
        orchestrator._validate_execution_result(str(task.pipeline_run_id), task)
    )

    assert valid is False
    assert error == task.result_summary
    assert execution is None
