# PartyHat Pipeline API — Frontend Integration Guide

This document covers only the endpoints needed for the detached pipeline frontend flow, starting with project creation and ending with deployment approval, resume, cancellation, event replay, and output retrieval.

**Base URL (local):** `http://localhost:8000`

## Recommended Frontend Flow

1. Resolve the user with `POST /users`
2. Create a project with `POST /projects`
3. Start planning with `POST /plan/start`
4. Continue planning with `POST /plan/message`
5. Read the latest plan with `GET /plan/current`
6. Open `GET /state/stream` if you want live current-state updates for plan, code, and deployment
7. Approve the plan with `POST /plan/approve`
8. Start the pipeline with `POST /pipeline/run`
9. Open `GET /pipeline/events` for timeline updates and poll `GET /pipeline/status` for authoritative run state
10. If the run pauses on `pipeline_waiting_for_approval`, approve/reject/override the pending gate
11. Resume execution with `POST /pipeline/resume` and reopen `GET /pipeline/events`
12. Read outputs from `GET /coding/current`, `GET /testing/current`, `GET /deployment/current`, `GET /artifacts/tree`, and `GET /artifacts/file`

## Frontend Model

Treat the pipeline as two separate channels:

- `GET /pipeline/status` is the authoritative state for whether a run is `running`, `waiting_for_approval`, `completed`, `failed`, or `cancelled`
- `GET /pipeline/events` is a replayable timeline for UI updates, logs, and progress rendering
- `GET /state/stream` is a current-state stream for the latest plan, code metadata, and deployment metadata across planning and pipeline phases

The frontend should persist at least:

- `project_id`
- `user_id`
- `pipeline_run_id`
- `last_pipeline_event_seq`

Important behavior:

- Leaving the page does not cancel the pipeline run
- Closing the SSE connection only stops live updates in that tab
- On return, the frontend should call `/pipeline/status`, then reconnect to `/pipeline/events?after_seq=<last seen seq>`

## Request Context

For all project-scoped endpoints, send these headers:

```http
X-Project-Id: <PROJECT_ID>
X-User-Id: <USER_ID>
```

The backend also accepts `project_id` and `user_id` in request bodies or query parameters on some endpoints, but headers are the cleanest option for frontend integration.

Important note for `GET /pipeline/events`:

- Use `fetch()` with a streamed response, not browser `EventSource`, because the frontend still needs to send `X-User-Id` / `X-Project-Id` headers
- If authentication later moves to cookies or another header-free mechanism, native `EventSource` becomes viable

The same `fetch()` guidance applies to `GET /state/stream`.

## 1. Resolve or Create the User

### `POST /users?wallet=<WALLET_ADDRESS>`

Use this once when the wallet connects. It returns an existing `user_id` if the wallet is already known.

**Response**

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

Persist `user_id` in your app session.

## 2. Create the Project

### `POST /projects`

**Body**

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Token Project",
  "screenshot_base64": null
}
```

**Response**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

Persist `project_id` and send it in `X-Project-Id` on all later calls.

## 3. Start the Planning Session

### `POST /plan/start`

This creates the planning `session_id` and returns the assistant's first message.

**Headers**

```http
X-Project-Id: <PROJECT_ID>
X-User-Id: <USER_ID>
Content-Type: application/json
```

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

**Response**

```json
{
  "session_id": "plan-session-id",
  "message": "Hello, I want to help you plan a new smart contract.",
  "answer_recommendations": [],
  "pending_questions": []
}
```

Persist `session_id` in the project state.

## 4. Continue Planning

### `POST /plan/message`

Use this for the planning chat loop until the plan is ready.

**Body**

```json
{
  "session_id": "plan-session-id",
  "message": "Build me an ERC-20 with owner-only minting and a treasury wallet.",
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

**Response**

```json
{
  "session_id": "plan-session-id",
  "response": "Understood. I need a few more details...",
  "tool_calls": ["get_current_plan", "send_question_batch"],
  "answer_recommendations": [],
  "pending_questions": []
}
```

## 5. Read the Current Plan

### `GET /plan/current?project_id=<PROJECT_ID>&user_id=<USER_ID>`

Usually you call this after each planning turn or on page refresh.

**Response**

```json
{
  "plan": {
    "project_name": "My Token Project",
    "status": "draft",
    "deployment_target": {
      "network": "avalanche_fuji",
      "name": "Avalanche Fuji",
      "chain_id": 43113,
      "rpc_url_env_var": "FUJI_RPC_URL",
      "private_key_env_var": "FUJI_PRIVATE_KEY"
    },
    "contracts": []
  },
  "status": "draft"
}
```

The important frontend check is whether the plan exists and whether its `status` is ready to approve or run.

## 6. Approve the Plan

### `POST /plan/approve`

This flips the plan into the `ready` state so the pipeline can start.

**Body**

```json
{
  "session_id": "plan-session-id",
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

**Response**

```json
{
  "session_id": "plan-session-id",
  "success": true,
  "message": "Plan approved. Project 'My Token Project' is ready for code generation."
}
```

## 7. Start the Pipeline

### `POST /pipeline/run`

This endpoint starts a detached run and returns control metadata immediately.

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

**Response**

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=project-id&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=project-id&pipeline_run_id=run-id"
}
```

Persist `pipeline_run_id` in frontend state immediately.

Recommended immediate frontend actions after `POST /pipeline/run` succeeds:

1. Save `pipeline_run_id`
2. Set local UI state to `running`
3. Set `last_pipeline_event_seq = 0`
4. Open `GET /pipeline/events`
5. Start a status poll loop or at least call `GET /pipeline/status` on reconnect / page reload

## 8. Stream the Timeline

### `GET /pipeline/events?project_id=<PROJECT_ID>&pipeline_run_id=<PIPELINE_RUN_ID>&after_seq=<LAST_SEEN_SEQ>`

This endpoint is an SSE stream. It replays any missed events after `after_seq`, then tails new events until the run ends or the client disconnects.

Frontend expectations:

- The stream may replay older events first if `after_seq` is behind
- The stream may send keepalive comments; ignore lines that do not start with `id:`, `event:`, or `data:`
- The stream ending does not mean failure by itself; always confirm terminal state via `GET /pipeline/status`

### Common Stream Event Types

- `pipeline_start`
- `stage_start`
- `tool_call`
- `agent_message`
- `evaluation`
- `stage_complete`
- `pipeline_waiting_for_approval`
- `pipeline_complete`
- `pipeline_error`
- `pipeline_cancelled`

### Example Early Events

```json
{
  "type": "pipeline_start",
  "seq": 1,
  "pipeline_run_id": "run-id",
  "project_id": "project-id"
}
```

```json
{
  "seq": 2,
  "type": "stage_start",
  "stage": "coding",
  "task_id": "task-id",
  "task_type": "coding.generate_contracts",
  "description": "Generate Solidity contracts from the approved plan.",
  "retry_budget_key": "coding",
  "retry_attempt": 0
}
```

Use the SSE `id` field or the JSON `seq` field to remember the last rendered event. On reconnect, pass that value as `after_seq`.

Recommended event handling rules:

- Append `agent_message`, `tool_call`, `evaluation`, `stage_start`, and `stage_complete` to the run timeline
- Update `last_pipeline_event_seq` after each successfully parsed event
- When `pipeline_waiting_for_approval` arrives, immediately fetch `/pipeline/status` to load the pending gate details
- When `pipeline_complete`, `pipeline_error`, or `pipeline_cancelled` arrives, still call `/pipeline/status` once to confirm final durable state

## 9. Stream Current Plan, Code, and Deployment State

### `GET /state/stream?project_id=<PROJECT_ID>&user_id=<USER_ID>`

This endpoint is an SSE stream for current project state. It emits one initial snapshot immediately, then only emits updates when the current saved plan, code artifact metadata, or deployment metadata change.

Use it for:

- live planning UIs that need the latest saved draft or approved plan
- artifact panels that should refresh when generated file metadata changes
- deployment panels that should refresh when authoritative deployment records change

Do not use it for replayable pipeline history. `GET /pipeline/events` remains the run timeline endpoint.

Important behavior:

- every reconnect starts with a fresh `state_snapshot`
- there is no replay cursor or persisted event sequence for this endpoint
- every `plan_updated`, `code_updated`, and `deployment_updated` event carries the full current `plan`, `code`, and `deployment` branches
- code payloads are metadata-only; use `GET /artifacts/file?relative_path=...` to load actual source text
- keepalive comments may appear while nothing changes

### State Stream Event Types

- `state_snapshot`
- `plan_updated`
- `code_updated`
- `deployment_updated`
- `error`

### Example Initial Snapshot

```json
{
  "project_id": "project-id",
  "plan": {
    "plan": {
      "project_name": "My Token Project",
      "status": "draft"
    },
    "status": "draft",
    "version": "4b7f..."
  },
  "code": {
    "artifacts": [],
    "version": "1f4a..."
  },
  "deployment": {
    "last_deploy_results": [],
    "version": "8b9c..."
  },
  "emitted_at": "2026-04-06T09:00:00+00:00"
}
```

### Example Incremental Events

```json
{
  "project_id": "project-id",
  "plan": {
    "plan": {
      "project_name": "My Token Project",
      "status": "ready"
    },
    "status": "ready",
    "version": "8123..."
  },
  "code": {
    "artifacts": [
      {
        "path": "contracts/PartyToken.sol"
      }
    ],
    "version": "9abc..."
  },
  "deployment": {
    "last_deploy_results": [
      {
        "status": "success",
        "tx_hash": "0x123"
      }
    ],
    "version": "7def..."
  },
  "emitted_at": "2026-04-06T09:01:00+00:00"
}
```

Frontend handling rules:

- replace the full current-state cache or the relevant branches whenever a `plan_updated`, `code_updated`, or `deployment_updated` event arrives
- ignore keepalive comments
- reconnect by reopening `GET /state/stream`; the new initial snapshot is the recovery mechanism
- fetch source file contents lazily with `GET /artifacts/file`

## 10. Poll the Authoritative Pipeline Status

### `GET /pipeline/status?project_id=<PROJECT_ID>&pipeline_run_id=<PIPELINE_RUN_ID>`

Use this to restore the UI after refresh, or poll while the SSE stream is disconnected.

If `pipeline_run_id` is omitted, the backend returns the latest run for the project.

**Response Shape**

```json
{
  "pipeline_run_id": "run-id",
  "project_id": "project-id",
  "status": "waiting_for_approval",
  "failure_reason": "Deployment script is ready. Awaiting operator approval before on-chain deployment.",
  "run": {
    "id": "run-id",
    "status": "waiting_for_approval",
    "current_stage": "deployment",
    "current_task_id": "task-id",
    "deployment_target": {},
    "failure_class": "human_gate"
  },
  "total_tasks": 4,
  "tasks": [],
  "gates": [],
  "evaluations": []
}
```

### Run Status Values

- `created`
- `running`
- `waiting_for_approval`
- `cancellation_requested`
- `cancelled`
- `completed`
- `failed`

Frontend should treat `run.status` as the authoritative run state.

Recommended usage:

- Call on page load if you already have a saved `pipeline_run_id`
- Call whenever the events stream disconnects unexpectedly
- Call after any gate decision
- Call once after any terminal event to refresh the full final state

## 11. Handle Human Gates

When the pipeline hits a human gate, the stream emits:

```json
{
  "type": "pipeline_waiting_for_approval",
  "pipeline_run_id": "run-id"
}
```

Then call `GET /pipeline/status` and inspect `gates` for the pending gate.

The frontend should render approval UI from `status.gates`, not from the event payload alone.

### Gate Types

- `pre_deploy`: the deploy script passed checks and now needs operator approval before on-chain deploy
- `override`: a blocking evaluation or retry budget exhaustion needs explicit operator override

### Approve a `pre_deploy` Gate

`POST /pipeline/gates/{gate_id}/approve`

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Approved to deploy to Fuji"
}
```

### Reject a Gate

`POST /pipeline/gates/{gate_id}/reject`

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Rejected by operator"
}
```

### Override an `override` Gate

`POST /pipeline/gates/{gate_id}/override`

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Grant one extra retry"
}
```

### Important Frontend Note

The gate decision endpoint updates durable state in Postgres, but it does not resume execution by itself. After `approve` or `override`, call `/pipeline/resume`, then reopen `/pipeline/events`.

Recommended gate flow:

1. Receive `pipeline_waiting_for_approval`
2. Call `GET /pipeline/status`
3. Find the pending gate in `gates`
4. Render approve / reject / override UI from that durable gate record
5. Submit the decision endpoint
6. If approved or overridden, call `POST /pipeline/resume`
7. Reconnect `GET /pipeline/events` using the current `last_pipeline_event_seq`

## 12. Resume the Pipeline

### `POST /pipeline/resume`

This endpoint returns control metadata immediately.

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "pipeline_run_id": "<PIPELINE_RUN_ID>"
}
```

**Response**

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=project-id&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=project-id&pipeline_run_id=run-id"
}
```

The reopened stream may start with:

```json
{
  "type": "pipeline_resumed",
  "seq": 8,
  "pipeline_run_id": "run-id",
  "project_id": "project-id"
}
```

Then `GET /pipeline/events` continues with the usual stage and terminal events.

Operational notes:

- `/pipeline/resume` should only be called after a human gate is resolved or when reconnecting a non-terminal run that is no longer making progress
- If the run is already `completed` or `cancelled`, `/pipeline/resume` returns an error
- If a gate is still pending, `/pipeline/resume` returns an error

## 13. Cancel the Pipeline

### `POST /pipeline/cancel`

**Body**

```json
{
  "project_id": "<PROJECT_ID>",
  "pipeline_run_id": "<PIPELINE_RUN_ID>",
  "reason": "User cancelled from frontend"
}
```

**Response**

```json
{
  "success": true,
  "message": "Cancellation requested. The pipeline will stop at the next safe point.",
  "pipeline_run_id": "run-id",
  "reason": "User cancelled from frontend"
}
```

After this, the stream or status endpoint should eventually show `pipeline_cancelled` or `run.status = "cancelled"`.

## 14. Read Artifacts and Execution Results

These endpoints are useful for the pipeline results screens.

### `GET /coding/current`

Returns current code artifact metadata.

### `GET /testing/current`

Returns the latest authoritative test runs for the project. This is Postgres-backed.

### `GET /deployment/current`

Returns the latest authoritative deployment records for the project. This is Postgres-backed.

### `GET /artifacts/tree`

Returns the artifact directory tree. Use this to build a file explorer in the UI.

### `GET /artifacts/file?relative_path=<PATH>`

Returns a single artifact file, such as:

- `contracts/PartyToken.sol`
- `test/PartyTokenTest.t.sol`
- `script/DeployPartyToken.s.sol`
- `manifests/deployment.json`

The deployment manifest is especially important for the new flow because it is the explicit source of deploy intent.

## Recommended Frontend State

Persist these values per project:

- `user_id`
- `project_id`
- `plan_session_id`
- `pipeline_run_id`
- `pipeline_status`
- `last_pipeline_event_seq`
- `pending_gate_id`

## Minimal Events Reader Example

```ts
async function streamGetSse(
  url: string,
  headers: Record<string, string>,
  onEvent: (event: any) => void,
) {
  const response = await fetch(url, {
    method: "GET",
    headers: {
      ...headers,
    },
  });

  if (!response.ok || !response.body) {
    throw new Error(`Request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      let eventId: string | null = null;
      let eventData = "";

      for (const line of chunk.split("\n")) {
        if (line.startsWith(":")) continue;
        if (line.startsWith("id: ")) eventId = line.slice(4);
        if (line.startsWith("data: ")) eventData += line.slice(6);
      }

      if (!eventData) continue;
      const parsed = JSON.parse(eventData);
      if (eventId && parsed.seq == null) {
        parsed.seq = Number(eventId);
      }
      onEvent(parsed);
    }
  }
}
```

Use it for:

- `GET /state/stream`
- `GET /pipeline/events`

## Recommended UI Logic

- Show planning UI until `/plan/approve` succeeds
- Start pipeline and switch to a run timeline view
- Persist `pipeline_run_id` from `/pipeline/run`
- Persist `last_pipeline_event_seq` as events arrive
- Open `GET /pipeline/events` for live updates and use `GET /pipeline/status` for reload recovery
- On page load, if `pipeline_run_id` exists, call `GET /pipeline/status` first and reconnect `GET /pipeline/events?after_seq=<last seq>`
- If `run.status === "waiting_for_approval"`, show the pending gate and approval buttons
- After approval or override, immediately call `/pipeline/resume`, then reopen `GET /pipeline/events`
- If the user leaves the page, do nothing special on the backend; just reconnect on return
- For completed runs, show artifact browser plus test and deployment summaries
- For failed runs, render `run.failure_reason`, `evaluations`, and recent `tasks`
