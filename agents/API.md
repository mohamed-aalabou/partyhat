# PartyHat API — Pipeline UX & Frontend Integration

This document describes the PartyHat API endpoints, how to call them, and how to integrate them into a frontend built around the detached planning + pipeline UX.

**Base URL (local):** `http://localhost:8000`  
**CORS:** Allowed origins currently include `http://localhost:3000`, `http://localhost:3001`, `https://partyhat-app.vercel.app`, and `https://partyhat-backend.onrender.com`.

---

## Pushing the schema to the database

The app uses SQLAlchemy with `create_all`: it creates missing tables but does not alter existing ones.

**Option 1 - Start the API (creates tables on startup)**

If `DATABASE_URL` is set, tables are created when the server starts:

```bash
cd agents && uv run uvicorn api:app --reload --port 8000
```

**Option 2 - Run the sync script (no server)**

From the repo root, with `DATABASE_URL` in `agents/.env`:

```bash
cd agents && uv run python sync_schema.py
```

**Existing database with the old `email` column?**

`create_all` will not rename `email` to `wallet`. Either:

- Reset with data loss: use a one-off script that calls `drop_tables()` then `create_tables()` from `agents.db`
- Migrate in Postgres:

```sql
ALTER TABLE users RENAME COLUMN email TO wallet;
```

---

## Request context

Many endpoints are project- and user-scoped. You can pass context in two ways:

1. Headers (recommended)
   - `X-Project-Id`: project UUID or `"default"`
   - `X-User-Id`: user UUID or `"default"`
2. Body or query
   - Some endpoints accept `project_id` and `user_id` in the request body or query for backward compatibility

If you omit both, `project_id` and `user_id` default to `"default"`. For project-scoped memory, persisted chat, pipeline state, and artifact storage, use real project and user IDs and ensure `DATABASE_URL` is set.

Pipeline execution endpoints require real project and user IDs. Do not rely on `"default"` there.

---

## Recommended frontend flow

The recommended frontend flow is:

1. Resolve or create the user with `POST /users`
2. Create or select a project with `POST /projects` or `GET /projects`
3. Generate a planning `session_id` in the frontend, for example with `crypto.randomUUID()`
4. Open `GET /state/stream` early and keep it open while the project is active
5. Start and continue planning through `POST /agent/message/stream` with `intent: "planning"`
6. Read durable plan state from `GET /state/stream` or `GET /plan/current`
7. If needed, explicitly mark the current plan ready with `POST /plan/approve`
8. Start detached execution with `POST /pipeline/run`
9. Open `GET /pipeline/events` for the replayable timeline and use `GET /pipeline/status` for durable run state and recovery
10. Resolve any human gate with a gate decision endpoint, then call `POST /pipeline/resume`
11. Read generated outputs from `GET /coding/current`, `GET /testing/current`, `GET /deployment/current`, `GET /artifacts/tree`, and `GET /artifacts/file`

Treat the backend as three complementary channels:

- `GET /state/stream` is the best way to stay up to date on the latest saved plan, code artifact metadata, and deployment metadata
- `GET /pipeline/events` is the replayable pipeline timeline for progress rendering and run logs
- `GET /pipeline/status` is the authoritative durable state for the current or latest pipeline run

Persist at least:

- `user_id`
- `project_id`
- `session_id`
- `pipeline_run_id`
- `last_pipeline_event_seq`
- `pending_gate_id` when a gate is open

---

## Endpoints overview

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/health` | Health check |
| POST | `/users` | Create or resolve a user by wallet |
| POST | `/projects` | Create a project |
| GET | `/projects` | List projects for a user |
| GET | `/users/{user_id}/projects` | List projects for a user (alias route) |
| GET | `/projects/{project_id}` | Get one project |
| PATCH | `/projects/{project_id}` | Partially update project fields |
| GET | `/messages` | List persisted chat messages for a project or session |
| POST | `/agent/message/stream` | Primary streamed routed chat endpoint for planning and agent interactions |
| GET | `/plan/current` | Get the current saved plan |
| GET | `/state/stream` | Best live feed for saved plan, code artifact metadata, and deployment metadata |
| POST | `/plan/approve` | Explicitly mark the current plan ready |
| POST | `/pipeline/run` | Start a detached pipeline run |
| GET | `/pipeline/events` | Replayable SSE timeline for a pipeline run |
| GET | `/pipeline/status` | Authoritative durable pipeline status, tasks, gates, and evaluations |
| POST | `/pipeline/resume` | Resume a paused or gate-resolved pipeline run |
| POST | `/pipeline/cancel` | Request pipeline cancellation |
| POST | `/pipeline/gates/{gate_id}/approve` | Approve a pending `pre_deploy` gate |
| POST | `/pipeline/gates/{gate_id}/reject` | Reject a pending gate |
| POST | `/pipeline/gates/{gate_id}/override` | Override a pending `override` gate |
| GET | `/coding/current` | Get current code artifact metadata |
| POST | `/coding/generate` | One-shot Solidity generation helper |
| GET | `/testing/current` | Get the latest compact test result history |
| GET | `/deployment/current` | Get the latest compact deployment result history |
| GET | `/artifacts/tree` | Artifact directory tree |
| GET | `/artifacts/file` | Artifact file content |
| POST | `/plan/message` | Legacy non-stream planning fallback |
| POST | `/agent/message` | Legacy non-stream routed chat. Do not use for new frontend work |
| GET | `/memory/full` | Full memory snapshot for debugging |

---

## Health

### `GET /health`

**Response:** `200 OK`

```json
{
  "status": "ok",
  "service": "partyhat-agents"
}
```

**Frontend:** Use for readiness checks and API connectivity indicators.

---

## Users and projects

### `POST /users`

Create or resolve a user by wallet. If the wallet is already linked to a user, this returns the existing `user_id`. Otherwise it creates a new user and links the wallet.

**Query**

- `wallet` (required): wallet address string

**Response:** `200 OK`

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Errors:** `503` when `DATABASE_URL` is not configured, `422` when `wallet` is missing.

---

### `POST /projects`

Create a new project for a user.

**Body**

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Token Project",
  "screenshot_base64": null
}
```

- `user_id` is required
- `name` is optional
- `screenshot_base64` is optional

**Response:** `200 OK`

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

**Errors:** `400` invalid `user_id`, `503` no database.

---

### `GET /projects`

List all projects for a user.

**Query**

- `user_id` (required)

**Response:** `200 OK`

```json
[
  {
    "id": "660e8400-e29b-41d4-a716-446655440001",
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "My Token Project",
    "screenshot_base64": null,
    "created_at": "2025-03-07T12:00:00"
  }
]
```

**Errors:** `400` invalid `user_id`, `503` no database.

---

### `GET /users/{user_id}/projects`

Alias route for `GET /projects`.

**Path**

- `user_id` (required)

**Response:** `200 OK`

```json
[
  {
    "id": "660e8400-e29b-41d4-a716-446655440001",
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "My Token Project",
    "screenshot_base64": null,
    "created_at": "2025-03-07T12:00:00"
  }
]
```

**Errors:** `400` invalid `user_id`, `503` no database.

---

### `GET /projects/{project_id}`

Get a single project. Ownership is validated when `user_id` is provided.

**Path**

- `project_id` (required UUID)

**Query**

- `user_id` (required UUID)

**Response:** `200 OK`

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Token Project",
  "screenshot_base64": null,
  "created_at": "2025-03-07T12:00:00"
}
```

**Errors:** `400` invalid IDs, `404` not found, `503` no database.

---

### `PATCH /projects/{project_id}`

Partially update a project. Only fields included in the JSON body are changed.

**Path**

- `project_id` (required UUID)

**Body**

```json
{
  "name": "My Updated Project Name",
  "screenshot_base64": "data:image/png;base64,iVBORw0KGgoAAA..."
}
```

- Sending `screenshot_base64: null` clears the stored screenshot
- Omitted fields are left unchanged

**Response:** `200 OK`

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Updated Project Name",
  "screenshot_base64": "data:image/png;base64,iVBORw0KGgoAAA...",
  "created_at": "2025-03-07T12:00:00"
}
```

**Errors:** `400` invalid `project_id`, `404` project not found, `503` no database.

---

## Messages

### `GET /messages`

List persisted chat messages for a project. This includes stored user and agent messages for planning and routed agent sessions when project-scoped persistence is available. You can filter to a single `session_id`.

**Query**

- `session_id` (optional)
- `limit` (optional, default `200`)
- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "messages": [
    {
      "id": "6f2c3e7c-6fa1-4cb5-9e52-9ad1132c67d4",
      "project_id": "660e8400-e29b-41d4-a716-446655440001",
      "session_id": "770e8400-e29b-41d4-a716-446655440002",
      "sender": "user",
      "content": "I want an ERC-20 token with mint and burn.",
      "created_at": "2025-03-07T12:00:00.000000"
    }
  ]
}
```

**Errors:** `400` missing or invalid `project_id`, `503` no database.

**Frontend:** Use for refresh or restore flows when reopening a project or session.

---

## Planning and routed chat

### `POST /agent/message/stream`

This is the primary chat endpoint for new frontend work.

Use it to start planning, continue planning, and finalize planning by sending routed messages with `intent: "planning"`. The same endpoint also supports other routed agent intents for non-planning workflows.

**Supported intents**

- `planning`
- `coding`
- `testing`
- `deployment`
- `audit`

**Important frontend rule**

Generate `session_id` in the frontend and persist it per project. The recommended planning UX does not depend on a server-created session bootstrap.

**Body**

```json
{
  "session_id": "plan-session-id",
  "intent": "planning",
  "message": "Build me an ERC-20 with owner-only minting and a treasury wallet.",
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `200 OK`, `Content-Type: text/event-stream`

Each SSE frame contains JSON after `data:`.

**Step event**

`step` events are transient progress updates. `tool_calls` here is optional structured metadata and should be treated as display or debug information, not as business logic.

```json
{
  "type": "step",
  "content": "I need a few more details before I can finalize the plan.",
  "tool_calls": [
    {
      "name": "get_current_plan",
      "args": "{}"
    }
  ]
}
```

**Done event**

For `intent: "planning"`, the final `done` event includes the assistant response plus structured planning UI data such as `approval_request`, `answer_recommendations`, and `pending_questions`.

```json
{
  "type": "done",
  "session_id": "plan-session-id",
  "response": "Understood. I still need token decimals and the treasury wallet behavior.",
  "tool_calls": [
    "get_current_plan",
    "send_question_batch",
    "save_plan_draft"
  ],
  "approval_request": null,
  "answer_recommendations": [
    {
      "text": "Use 18 decimals",
      "recommended": true
    }
  ],
  "pending_questions": [
    {
      "question": "How many decimals should the token use?",
      "answer_recommendations": [
        {
          "text": "18",
          "recommended": true
        },
        {
          "text": "6"
        }
      ]
    }
  ]
}
```

`approval_request` is `{"type":"plan_verification","required":true}` when the planning agent wants the frontend to show a verify or approve affordance. Otherwise it is `null`.

**Error event**

```json
{
  "type": "error",
  "detail": "Unknown intent: planningg"
}
```

**Frontend**

- Use `fetch()` plus a streamed response, not browser `EventSource`, because this endpoint is POST-based
- Render `step.content` incrementally while the request is active
- Replace transient streamed text with the `done.response` payload when the turn finishes
- Use `approval_request`, `pending_questions`, and `answer_recommendations` from the final event for planning UI controls
- Do not treat streamed chat text as the durable source of truth for plan state; use `GET /state/stream` or `GET /plan/current` for saved plan data

**Example JavaScript**

```javascript
async function streamAgentMessage(body, projectId, userId) {
  const res = await fetch("http://localhost:8000/agent/message/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Project-Id": projectId,
      "X-User-Id": userId
    },
    body: JSON.stringify(body)
  });

  if (!res.ok || !res.body) {
    const text = await res.text();
    throw new Error(text || `Request failed: ${res.status}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";

    for (const chunk of chunks) {
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data: ")) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === "step") {
          appendTransientText(event.content || "");
        } else if (event.type === "done") {
          finalizeAssistantTurn(event);
        } else if (event.type === "error") {
          showError(event.detail);
        }
      }
    }
  }
}
```

---

### `GET /state/stream`

This is the best way to stay up to date on saved project state.

Use it for the latest:

- plan state
- code artifact metadata
- deployment metadata

Do not use it for replayable pipeline history. It is a current-state stream, not a run timeline.

**Query**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

Headers are still recommended.

**Response:** `200 OK`, `Content-Type: text/event-stream`

This stream always starts with a fresh snapshot, then emits updates only when the saved state changes. Each update event now carries the full current `plan`, `code`, and `deployment` branches so clients can replace entire local state without merging partial payloads.

**Event types**

- `state_snapshot`
- `plan_updated`
- `code_updated`
- `deployment_updated`
- `error`

**Initial snapshot example**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
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

**Incremental plan update example**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "plan": {
    "plan": {
      "project_name": "My Token Project",
      "status": "ready"
    },
    "status": "ready",
    "version": "8123..."
  },
  "code": {
    "artifacts": [],
    "version": "1f4a..."
  },
  "deployment": {
    "last_deploy_results": [],
    "version": "8b9c..."
  },
  "emitted_at": "2026-04-06T09:01:00+00:00"
}
```

**Frontend**

- Keep this stream open for the active project whenever possible
- On connect, replace local current-state cache with the full `state_snapshot`
- On `plan_updated`, `code_updated`, or `deployment_updated`, replace the full current-state cache or the relevant branches from the full payload
- Ignore keepalive comment lines
- Reconnect by simply reopening the endpoint; the new `state_snapshot` is the recovery mechanism
- Use `GET /artifacts/file` to fetch actual source text lazily

Because frontends usually send `X-Project-Id` and `X-User-Id`, use `fetch()` with a streamed response here instead of native `EventSource`.

---

### `GET /plan/current`

Get the current saved plan for the project and user context.

**Query**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "plan": {
    "project_name": "PartyToken",
    "description": "ERC-20 with owner-only minting",
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

If there is no saved plan yet, both `plan` and `status` may be `null`.

**Plan lifecycle statuses**

- `draft`
- `ready`
- `generating`
- `testing`
- `deploying`
- `deployed`
- `failed`

**Frontend:** Use this for page load recovery, explicit refresh, or situations where you need a one-shot plan read. For ongoing sync, prefer `GET /state/stream`.

---

### `POST /plan/approve`

Explicitly flip the current saved plan into the `ready` state.

This is useful when you already have a draft plan and want a deliberate UI action to mark it ready for the pipeline. It is not the primary transport for the planning chat itself.

**Body**

```json
{
  "session_id": "plan-session-id",
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `200 OK`

```json
{
  "session_id": "plan-session-id",
  "success": true,
  "message": "Plan approved. Project 'PartyToken' is ready for code generation."
}
```

**Errors:** `404` no plan, `400` plan already deployed, `500` server error.

**Frontend:** Enable this only when a durable plan exists and the UI wants an explicit ready-state action.

---

## Pipeline execution

### `POST /pipeline/run`

Start a detached autonomous pipeline run after planning is complete.

The backend returns control metadata immediately. Use `GET /pipeline/events` for the replayable timeline and `GET /pipeline/status` for durable state.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `202 Accepted`

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=660e8400-e29b-41d4-a716-446655440001&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=660e8400-e29b-41d4-a716-446655440001&pipeline_run_id=run-id"
}
```

**Frontend**

- Persist `pipeline_run_id` immediately
- Set `last_pipeline_event_seq` to `0`
- Open `GET /pipeline/events`
- Keep `GET /pipeline/status` available for recovery and gate rendering

---

### `GET /pipeline/events`

Replayable SSE stream for one pipeline run.

This endpoint replays any missed events after `after_seq`, then tails new events until the run ends or the client disconnects.

**Query**

- `project_id` (required)
- `pipeline_run_id` (required)
- `after_seq` (optional, default `0`)

**Response:** `200 OK`, `Content-Type: text/event-stream`

Each event uses SSE `id`, `event`, and `data` fields. The JSON payload in `data` always includes the event body, and usually includes the same sequence in `seq`.

**Common event types**

- `pipeline_start`
- `pipeline_resumed`
- `stage_start`
- `tool_call`
- `agent_message`
- `evaluation`
- `stage_complete`
- `pipeline_waiting_for_approval`
- `pipeline_complete`
- `pipeline_error`
- `pipeline_cancelled`

**Example early events**

```json
{
  "type": "pipeline_start",
  "seq": 1,
  "pipeline_run_id": "run-id",
  "project_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

```json
{
  "type": "stage_start",
  "seq": 2,
  "stage": "coding",
  "task_id": "task-id",
  "task_type": "coding.generate_contracts",
  "description": "Generate Solidity contracts from the approved plan.",
  "retry_budget_key": "coding",
  "retry_attempt": 0
}
```

**Frontend**

- This stream may replay backlog first if `after_seq` is behind
- The stream may emit keepalive comments; ignore lines that start with `:`
- Use the SSE `id` or JSON `seq` as the replay cursor
- Update `last_pipeline_event_seq` after each parsed event
- When `pipeline_waiting_for_approval` arrives, immediately call `GET /pipeline/status`
- When `pipeline_complete`, `pipeline_error`, or `pipeline_cancelled` arrives, still call `GET /pipeline/status` once to confirm final durable state
- Because most frontends still send custom headers, use `fetch()` with a streamed GET response instead of native `EventSource`

---

### `GET /pipeline/status`

Authoritative durable state for a pipeline run.

Use this endpoint for:

- page reload recovery
- disconnected timeline recovery
- rendering tasks, evaluations, and human gates
- confirming final status after a terminal event

If `pipeline_run_id` is omitted, the backend returns the latest run for the project. If no runs exist for the project, it returns:

```json
{
  "error": "No pipeline runs found for this project"
}
```

**Query**

- `project_id` (required)
- `pipeline_run_id` (optional)

**Response shape**

```json
{
  "pipeline_run_id": "run-id",
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "status": "waiting_for_approval",
  "failure_reason": "Deployment script is ready. Awaiting operator approval before on-chain deployment.",
  "run": {
    "id": "run-id",
    "project_id": "660e8400-e29b-41d4-a716-446655440001",
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "waiting_for_approval",
    "current_stage": "deployment",
    "current_task_id": "task-id",
    "deployment_target": {},
    "failure_class": "human_gate",
    "created_at": "2026-04-06T09:00:00+00:00",
    "started_at": "2026-04-06T09:00:05+00:00",
    "paused_at": "2026-04-06T09:04:00+00:00",
    "resumed_at": null,
    "completed_at": null,
    "updated_at": "2026-04-06T09:04:00+00:00"
  },
  "total_tasks": 4,
  "tasks": [],
  "gates": [],
  "evaluations": []
}
```

**Top-level fields to rely on**

- `status`: authoritative run state
- `failure_reason`: top-level human-readable failure or waiting reason
- `run`: full durable run record
- `tasks`: serialized pipeline tasks
- `gates`: pending and resolved human gates
- `evaluations`: pipeline evaluations

**Common run statuses**

- `created`
- `running`
- `waiting_for_approval`
- `cancellation_requested`
- `cancelled`
- `completed`
- `failed`

---

### Human gates

When a run pauses for human input, `GET /pipeline/events` emits `pipeline_waiting_for_approval`. The frontend should then read the durable pending gate from `GET /pipeline/status`.

The frontend should render approval UI from `status.gates`, not from the event payload alone.

**Gate types**

- `pre_deploy`: deployment script is ready and needs explicit operator approval before on-chain execution
- `override`: a blocking evaluation or retry-budget condition needs explicit override

**Important**

Gate decision endpoints do not resume execution by themselves. After `approve` or `override`, call `POST /pipeline/resume`.

---

### `POST /pipeline/gates/{gate_id}/approve`

Approve a pending `pre_deploy` gate.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "reason": "Approved to deploy to Avalanche Fuji"
}
```

**Response:** `200 OK`

```json
{
  "success": true,
  "gate_id": "gate-id",
  "action": "approve",
  "pipeline_run_id": "run-id",
  "reason": "Approved to deploy to Avalanche Fuji",
  "run_status": "running"
}
```

---

### `POST /pipeline/gates/{gate_id}/reject`

Reject a pending gate.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "reason": "Rejected by operator"
}
```

**Response:** `200 OK`

```json
{
  "success": true,
  "gate_id": "gate-id",
  "action": "reject",
  "pipeline_run_id": "run-id",
  "reason": "Rejected by operator",
  "run_status": "failed"
}
```

---

### `POST /pipeline/gates/{gate_id}/override`

Override a pending `override` gate.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "reason": "Grant one extra retry"
}
```

**Response:** `200 OK`

```json
{
  "success": true,
  "gate_id": "gate-id",
  "action": "override",
  "pipeline_run_id": "run-id",
  "reason": "Grant one extra retry",
  "run_status": "running"
}
```

**Frontend gate flow**

1. Receive `pipeline_waiting_for_approval`
2. Call `GET /pipeline/status`
3. Read the pending gate from `gates`
4. Submit `approve`, `reject`, or `override`
5. If the result is approval or override, call `POST /pipeline/resume`
6. Reopen `GET /pipeline/events` using the saved `last_pipeline_event_seq`

---

### `POST /pipeline/resume`

Resume a paused or gate-resolved pipeline run.

This returns detached control metadata immediately.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "pipeline_run_id": "run-id"
}
```

**Response:** `202 Accepted`

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=660e8400-e29b-41d4-a716-446655440001&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=660e8400-e29b-41d4-a716-446655440001&pipeline_run_id=run-id"
}
```

**Notes**

- This returns `400` if the run is already terminal
- This returns `409` if a gate is still pending
- The reopened timeline may begin with a `pipeline_resumed` event

---

### `POST /pipeline/cancel`

Request cancellation for a running pipeline. Cancellation is best-effort and takes effect at the next safe point.

**Body**

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "pipeline_run_id": "run-id",
  "reason": "User cancelled from frontend"
}
```

**Response:** `200 OK`

```json
{
  "success": true,
  "message": "Cancellation requested. The pipeline will stop at the next safe point.",
  "pipeline_run_id": "run-id",
  "reason": "User cancelled from frontend"
}
```

**Frontend:** Keep showing progress until `GET /pipeline/events` or `GET /pipeline/status` reaches a terminal cancelled state.

---

## Outputs and artifacts

### `GET /coding/current`

Return current code artifact metadata for the project and user.

This is metadata only. For actual file contents, use `GET /artifacts/file`.

**Query**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "artifacts": [
    {
      "path": "contracts/PartyToken.sol",
      "language": "solidity",
      "description": "Main ERC-20 token contract",
      "contract_names": [
        "PartyToken"
      ],
      "plan_contract_ids": [
        "pc_partytoken"
      ],
      "related_plan_id": null,
      "created_at": "2025-03-07T12:00:00"
    }
  ]
}
```

**Frontend:** Use this for results views or artifact lists. For live updates, prefer `GET /state/stream`.

---

### `POST /coding/generate`

One-shot Solidity generation helper.

This remains supported, but it is a standalone utility endpoint rather than the primary path for the pipeline UX.

**Body**

```json
{
  "goal": "ERC-20 token with mint and burn, 18 decimals, Ownable."
}
```

**Query**

- `project_id` (optional)
- `user_id` (optional)

Headers can also carry project and user context.

**Response:** `200 OK`

```json
{
  "generated_code": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n...",
  "goal": "ERC-20 token with mint and burn, 18 decimals, Ownable."
}
```

**Errors:** `400` empty goal, `500` generation failure.

---

### `GET /testing/current`

Return the latest compact test result history for the project and user.

These entries intentionally omit large `stdout` and `stderr` blobs. They may include pipeline metadata such as `pipeline_run_id`, `pipeline_task_id`, and `trace_id`.

**Query**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "last_test_results": [
    {
      "timestamp": "2025-03-08T12:00:00.000000+00:00",
      "command": "forge test ...",
      "exit_code": 0,
      "modal_app": "...",
      "pipeline_run_id": "run-123",
      "pipeline_task_id": "task-789",
      "trace_id": "trace-456"
    }
  ]
}
```

**Frontend:** Use for results screens or explicit refresh. For live state changes, prefer `GET /state/stream`.

---

### `GET /deployment/current`

Return the latest compact deployment result history for the project and user.

These entries intentionally omit large `stdout` and `stderr` blobs. They may include pipeline metadata such as `pipeline_run_id`, `pipeline_task_id`, `plan_contract_id`, and `trace_id`.

**Query**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "last_deploy_results": [
    {
      "timestamp": "2025-03-08T12:00:00.000000+00:00",
      "network": "avalanche_fuji",
      "chain_id": 43113,
      "script_path": "script/DeployPartyToken.s.sol",
      "command": "forge script ...",
      "exit_code": 0,
      "tx_hash": "0x...",
      "deployed_address": "0x...",
      "pipeline_run_id": "run-123",
      "pipeline_task_id": "task-456",
      "plan_contract_id": "pc_partytoken",
      "trace_id": "trace-123"
    }
  ]
}
```

**Frontend:** Use for results screens or explicit refresh. For live state changes, prefer `GET /state/stream`.

---

### `GET /artifacts/tree`

Return the directory tree of generated artifacts for the project.

**Query**

- `project_id` (optional)
- `user_id` (optional)

**Response:** `200 OK`

```json
{
  "name": "generated_contracts",
  "path": "",
  "type": "directory",
  "children": [
    {
      "name": "contracts",
      "path": "contracts",
      "type": "directory",
      "children": [
        {
          "name": "PartyToken.sol",
          "path": "contracts/PartyToken.sol",
          "type": "file",
          "children": null
        }
      ]
    }
  ]
}
```

**Frontend:** Render a file explorer and use the `path` value with `GET /artifacts/file`.

---

### `GET /artifacts/file`

Load the raw contents of one artifact file.

**Query**

- `relative_path` (required), for example `contracts/PartyToken.sol`
- `project_id` (optional)
- `user_id` (optional)

**Response:** `200 OK`

```json
{
  "path": "contracts/PartyToken.sol",
  "content": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n..."
}
```

**Errors:** `400` empty path, `404` file not found.

---

## Legacy endpoints

### `POST /plan/message`

Legacy non-stream planning fallback.

This endpoint is still implemented, but new frontend work should prefer `POST /agent/message/stream` for planning because it supports the pipeline UX and streamed responses.

**Body**

```json
{
  "session_id": "plan-session-id",
  "message": "I want an ERC-20 token with mint and burn.",
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `200 OK`

```json
{
  "session_id": "plan-session-id",
  "response": "I need a few more details before I can finalize the plan.",
  "tool_calls": [
    "send_question_batch",
    "save_plan_draft"
  ],
  "answer_recommendations": [],
  "pending_questions": []
}
```

---

### `POST /agent/message`

Legacy non-stream routed chat.

Do not use this endpoint for new frontend work. Use `POST /agent/message/stream` instead.

It supports the same intent set as the stream endpoint:

- `planning`
- `coding`
- `testing`
- `deployment`
- `audit`

**Body**

```json
{
  "session_id": "plan-session-id",
  "intent": "coding",
  "message": "Explain the current artifacts.",
  "project_id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `200 OK`

```json
{
  "session_id": "plan-session-id",
  "response": "Here is the current artifact summary...",
  "tool_calls": [
    "get_current_artifacts"
  ],
  "answer_recommendations": [],
  "pending_questions": []
}
```

**Errors:** `400` empty message or unknown intent, `500` agent error.

---

## Debug

### `GET /memory/full`

Return the full project-scoped user memory block and global memory block for debugging or observability.

**Query**

- `project_id` (optional)
- `user_id` (optional)

**Response:** `200 OK`

```json
{
  "user_block_label": "user:...",
  "user_memory": {},
  "global_block_label": "global:...",
  "global_memory": {}
}
```

**Frontend:** Keep this behind internal or debug tooling rather than normal product UX.

---

## Frontend integration checklist

1. Resolve or create the user with `POST /users`
2. Create or select the project with `POST /projects`, `GET /projects`, or `GET /projects/{project_id}`
3. Persist `project_id` and `user_id`, and send them on every request through `X-Project-Id` and `X-User-Id`
4. Generate and persist a `session_id` in the frontend
5. Open `GET /state/stream` for the active project
6. Start planning with `POST /agent/message/stream` and `intent: "planning"`
7. Render streamed assistant text during the active request
8. Drive plan UI from durable state in `GET /state/stream` or `GET /plan/current`
9. If needed, use `POST /plan/approve` to explicitly mark the plan ready
10. Start execution with `POST /pipeline/run`
11. Persist `pipeline_run_id` and open `GET /pipeline/events`
12. Persist `last_pipeline_event_seq` as timeline events arrive
13. On refresh or reconnect, call `GET /pipeline/status` first, then reconnect `GET /pipeline/events?after_seq=<last_seq>` if the run is non-terminal
14. When a gate is pending, render it from `pipeline/status.gates`, submit the gate decision, then call `POST /pipeline/resume` if the decision was approval or override
15. Use `GET /coding/current`, `GET /testing/current`, `GET /deployment/current`, `GET /artifacts/tree`, and `GET /artifacts/file` for results and outputs

All error responses use JSON with a `detail` field when raised through FastAPI exceptions. For example:

```json
{
  "detail": "Message cannot be empty"
}
```

---

## OpenAPI and Swagger

When the server is running, interactive docs are available at:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

Use them to inspect live schemas and try requests interactively.
