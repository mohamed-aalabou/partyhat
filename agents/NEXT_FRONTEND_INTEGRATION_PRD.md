# PartyHat Next.js Frontend Integration PRD

## Document purpose

This document defines the frontend integration contract for a Next.js application that talks to the PartyHat backend.

It is written as a product and engineering handoff for the frontend developer. It describes:

- which backend endpoints to use
- which data to persist in the client
- how the planning and pipeline streams should be handled
- how approval, resume, and cancellation should work
- how to avoid an architecture that relies on many unrelated `useEffect` chains

This is intentionally not a code implementation.

## Primary objective

Build a project-scoped frontend flow where a user can:

1. Resolve or create a user from a wallet address
2. Create a project
3. Update the project name and screenshot
4. Start a planning session
5. Continue planning through `POST /agent/message/stream` with `intent: "planning"`
6. Observe real-time plan, code metadata, and deployment metadata updates
7. Approve the plan
8. Start the autonomous pipeline
9. Observe replayable pipeline timeline events
10. Approve, reject, or override human gates
11. Resume a paused pipeline
12. Cancel a running pipeline
13. View generated artifacts and execution summaries

## Non-goals

- Implementing backend changes
- Inventing a user update API that does not exist
- Loading full artifact file contents eagerly
- Using a separate effect-driven network loop per panel

## Backend constraints the frontend must respect

### Request context

For all project-scoped endpoints, the frontend should send:

```http
X-Project-Id: <PROJECT_ID>
X-User-Id: <USER_ID>
```

The backend also accepts `project_id` and `user_id` in some bodies and query params. The frontend should still standardize on headers for consistency.

### Streaming transport

Do not use browser `EventSource` for PartyHat streams.

Reason:

- `GET /state/stream` requires custom headers
- `GET /pipeline/events` requires custom headers
- `POST /agent/message/stream` is POST-based SSE and cannot use native `EventSource`

Frontend must use `fetch()` plus a streamed response reader.

### Durable vs non-durable channels

Treat the backend as three different channels:

1. `POST /agent/message/stream`
   - transient streamed assistant output for the current planning turn
2. `GET /state/stream`
   - current saved project state
   - not replayable
   - reconnect always starts from a fresh snapshot
3. `GET /pipeline/events` + `GET /pipeline/status`
   - replayable run timeline plus durable authoritative status

The frontend must not confuse them.

## API surface the frontend must support

### User endpoints

#### Create or resolve user

`POST /users?wallet=<WALLET_ADDRESS>`

Behavior:

- If the wallet already exists, returns the existing `user_id`
- Otherwise creates a user and returns the new `user_id`

Response:

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

Important:

- There is currently no user update endpoint in this backend
- Frontend should treat the user record as wallet-derived and effectively immutable for now

### Project endpoints

#### Create project

`POST /projects`

Request body:

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "My Token Project",
  "screenshot_base64": null
}
```

Response:

```json
{
  "project_id": "660e8400-e29b-41d4-a716-446655440001"
}
```

#### Update project

`PATCH /projects/{project_id}`

Request body:

```json
{
  "name": "Updated Project Name",
  "screenshot_base64": "data:image/png;base64,..."
}
```

Behavior:

- Partial update only
- Omitted fields remain unchanged
- `screenshot_base64: null` clears the screenshot

Response:

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440001",
  "user_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Updated Project Name",
  "screenshot_base64": "data:image/png;base64,...",
  "created_at": "2026-04-06T09:00:00+00:00"
}
```

#### Read project list and project detail

Frontend should also support:

- `GET /projects?user_id=<USER_ID>`
- `GET /projects/{project_id}?user_id=<USER_ID>`

These are needed for project picker and reload recovery.

## Planning phase contract

### Start planning

Use `POST /plan/start` once per new planning session.

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

Response:

```json
{
  "session_id": "plan-session-id",
  "message": "Hello, I want to help you plan a new smart contract.",
  "answer_recommendations": [],
  "pending_questions": []
}
```

Frontend responsibilities:

- Persist `session_id`
- Seed the chat UI with the opening assistant message
- Open project-scoped state syncing

### Continue planning through streamed routed chat

Use `POST /agent/message/stream` for planning turns after `session_id` exists.

Request body:

```json
{
  "session_id": "plan-session-id",
  "intent": "planning",
  "message": "Build me an ERC-20 with owner-only minting and a treasury wallet.",
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

### Stream event contract for planning

The backend emits JSON payloads inside SSE `data:` frames.

#### Step event

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

Important note:

- In actual backend behavior, `step.tool_calls` is structured metadata objects
- Frontend should treat `step.tool_calls` as optional display/debug data, not business logic

#### Done event

```json
{
  "type": "done",
  "session_id": "plan-session-id",
  "response": "Understood. I need treasury address behavior and token decimals.",
  "tool_calls": ["get_current_plan", "send_question_batch"],
  "approval_request": null,
  "answer_recommendations": [
    {
      "text": "Use 18 decimals",
      "recommended": true
    }
  ],
  "pending_questions": [
    {
      "question": "What should the treasury wallet do?",
      "answer_recommendations": [
        {
          "text": "Receive minted fees",
          "recommended": true
        }
      ]
    }
  ]
}
```

`approval_request` is `{"type":"plan_verification","required":true}` when the planner wants the frontend to present verification or approval UI. Otherwise it is `null`.

#### Error event

```json
{
  "type": "error",
  "detail": "Unknown intent: planningg"
}
```

### Current plan retrieval

Use `GET /plan/current?project_id=<PROJECT_ID>&user_id=<USER_ID>`:

- on first page load
- on hard refresh
- when state stream is unavailable
- immediately before enabling final approval if the UI needs a durable confirmation

Response shape:

```json
{
  "plan": {
    "project_name": "My Token Project",
    "status": "draft"
  },
  "status": "draft"
}
```

### Plan approval

Use `POST /plan/approve`.

Request body:

```json
{
  "session_id": "plan-session-id",
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

Response:

```json
{
  "session_id": "plan-session-id",
  "success": true,
  "message": "Plan approved. Project 'My Token Project' is ready for code generation."
}
```

## State synchronization contract

### Purpose of `GET /state/stream`

This endpoint is the frontend source of truth for current saved:

- plan
- code artifact metadata
- deployment metadata

It is not the run timeline.

### Event types

- `state_snapshot`
- `plan_updated`
- `code_updated`
- `deployment_updated`
- `error`

### Initial snapshot example

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

### Frontend handling rules

- On connect, replace local current-state cache with the full `state_snapshot`
- On `plan_updated`, `code_updated`, or `deployment_updated`, use the full payload to replace the current-state cache or the relevant branches
- Ignore keepalive comments
- On disconnect, reconnect by reopening the stream
- Do not attempt replay for this stream

### Artifact loading rule

`code_updated` only contains artifact metadata for the code branch, even though the event carries the full current-state payload.

Frontend must load actual file content lazily with:

`GET /artifacts/file?relative_path=<PATH>`

Do not load every artifact file whenever code metadata changes.

## Pipeline execution contract

### Start pipeline

Use `POST /pipeline/run`.

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "user_id": "<USER_ID>"
}
```

Response:

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=project-id&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=project-id&pipeline_run_id=run-id"
}
```

Frontend must immediately:

1. Persist `pipeline_run_id`
2. Reset `last_pipeline_event_seq` to `0`
3. Mark local run state as active
4. Open `GET /pipeline/events`
5. Be ready to call `GET /pipeline/status` on reconnect or refresh

### Replayable run timeline

Use `GET /pipeline/events?project_id=<PROJECT_ID>&pipeline_run_id=<RUN_ID>&after_seq=<LAST_SEQ>`

This endpoint:

- replays missed events after `after_seq`
- then tails new events
- emits keepalive comments
- may disconnect without implying failure

### Common pipeline event types

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

### Authoritative pipeline status

Use `GET /pipeline/status?project_id=<PROJECT_ID>&pipeline_run_id=<RUN_ID>`

This is the durable source of truth for:

- run status
- gate status
- task history
- evaluations
- failure reason
- recovery after reload

Response shape:

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

### Required frontend status rules

- Trust `run.status` and top-level `status` over local guesses
- Use `pipeline/events` for timeline rendering
- Use `pipeline/status` for recovery and decision screens
- After terminal events, still refresh status once

## Human gate flow

### Trigger

When the timeline emits:

```json
{
  "type": "pipeline_waiting_for_approval",
  "pipeline_run_id": "run-id"
}
```

the frontend must immediately call `GET /pipeline/status`.

Do not render approval UI from the event payload alone.

### Gate types

- `pre_deploy`
- `override`

### Approve a pre-deploy gate

`POST /pipeline/gates/{gate_id}/approve`

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Approved to deploy to Fuji"
}
```

### Reject a gate

`POST /pipeline/gates/{gate_id}/reject`

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Rejected by operator"
}
```

### Override a blocking gate

`POST /pipeline/gates/{gate_id}/override`

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "reason": "Operator approves override"
}
```

### Critical rule after approval or override

Gate decision endpoints do not resume the pipeline by themselves.

After a successful `approve` or `override`, frontend must:

1. Call `POST /pipeline/resume`
2. Reopen `GET /pipeline/events` using the saved `last_pipeline_event_seq`
3. Refresh `GET /pipeline/status`

If the gate is rejected:

- do not call resume
- refresh status and show the failed state

## Resume behavior

Use `POST /pipeline/resume`.

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "pipeline_run_id": "<PIPELINE_RUN_ID>"
}
```

Response:

```json
{
  "pipeline_run_id": "run-id",
  "status": "running",
  "events_url": "/pipeline/events?project_id=project-id&pipeline_run_id=run-id",
  "status_url": "/pipeline/status?project_id=project-id&pipeline_run_id=run-id"
}
```

Frontend may also use resume for reconnecting a non-terminal run that stopped making progress, but only after checking status first.

Frontend must not call resume when:

- the run is already `completed`
- the run is already `cancelled`
- a gate is still pending

## Cancellation behavior

Use `POST /pipeline/cancel`.

Request body:

```json
{
  "project_id": "<PROJECT_ID>",
  "pipeline_run_id": "<PIPELINE_RUN_ID>",
  "reason": "User cancelled from frontend"
}
```

Response:

```json
{
  "success": true,
  "message": "Cancellation requested. The pipeline will stop at the next safe point.",
  "pipeline_run_id": "run-id",
  "reason": "User cancelled from frontend"
}
```

Frontend behavior:

- show cancellation as requested immediately
- continue watching status or timeline until terminal cancellation arrives
- expect either `pipeline_cancelled` event or `run.status = "cancelled"`
- leaving the page must not cancel the run

## Artifact and execution result access

Frontend should support:

- `GET /coding/current`
- `GET /testing/current`
- `GET /deployment/current`
- `GET /artifacts/tree`
- `GET /artifacts/file?relative_path=<PATH>`

Recommended usage:

- use `GET /artifacts/tree` to render the file explorer
- use `GET /artifacts/file` only when a file is opened
- use `GET /testing/current` for latest test summaries
- use `GET /deployment/current` for latest deployment summaries

## Recommended frontend state model

The frontend should keep one project-scoped state container.

Recommended top-level shape:

```text
auth
  wallet
  userId

project
  projectId
  name
  screenshotBase64

planning
  sessionId
  messages
  activeAssistantDraft
  answerRecommendations
  pendingQuestions
  currentPlan
  planStatus

liveState
  planSnapshot
  codeMetadata
  deploymentMetadata
  stateStreamConnected

pipeline
  runId
  status
  lastEventSeq
  timeline
  tasks
  gates
  evaluations
  pendingGateId
  failureReason
  eventsStreamConnected

artifacts
  tree
  openFilesByPath
  testingSummary
  deploymentSummary

ui
  loading
  errors
  notices
```

## Required persistence model

Persist these values per project in durable browser storage:

- `user_id`
- `project_id`
- `plan_session_id`
- `pipeline_run_id`
- `last_pipeline_event_seq`

Optional:

- selected artifact file path
- last opened UI tab

Do not persist transient streamed text tokens as the durable source of truth.

## Required frontend architecture

### High-level requirement

Do not build this as many disconnected `useEffect` hooks that each fetch different pieces of state.

### Required pattern

Use one centralized client-side runtime layer based on:

- a reducer-backed React context, or
- a single external store such as Zustand

That runtime layer owns:

- request execution
- stream lifecycle
- durable checkpoint persistence
- event reduction into UI state

### Stream ownership model

Use only these long-lived stream managers:

1. planning send action
   - opens one short-lived `POST /agent/message/stream` request per user message
2. project state stream
   - one long-lived `GET /state/stream` connection per active project
3. pipeline timeline stream
   - one long-lived `GET /pipeline/events` connection per active pipeline run

### Why this matters

This keeps the UI reactive without:

- one effect per artifact panel
- one effect per status field
- one effect per planning response
- one effect per pipeline event type

Instead, network handlers dispatch normalized events into one state container and UI components subscribe to derived slices.

## Required UI decision rules

### Planning screen

- Show planning chat after `session_id` exists
- Render streamed assistant text incrementally during the active planning request
- Replace the active draft with the final assistant message on `done`
- Render `approval_request`, `pending_questions`, and `answer_recommendations` from the final event
- Use durable plan data from `state/stream` or `plan/current`, not from guessed chat text

### Approve button

- Enable only when a durable plan exists
- Prefer confirming `plan.status` from current saved plan state before allowing final approval

### Run pipeline button

- Enable only after plan approval succeeds
- Disable while a run is already active unless the UI is explicitly offering restart/recovery

### Approval modal or panel

- Open only from pending gate records in `pipeline/status.gates`
- Label the gate type clearly
- Require a reason field for auditability

### Cancel action

- Require confirmation
- Keep showing progress until the backend reaches a terminal cancelled state

## Page reload and recovery rules

On project page load:

1. Restore `user_id`, `project_id`, `plan_session_id`, `pipeline_run_id`, and `last_pipeline_event_seq`
2. Fetch current project metadata if needed
3. Fetch `GET /plan/current`
4. Open `GET /state/stream`
5. If `pipeline_run_id` exists:
   - call `GET /pipeline/status`
   - if non-terminal, reconnect `GET /pipeline/events?after_seq=<last_seq>`
   - if waiting for approval, render gate UI from status

If the events stream disconnects unexpectedly:

1. call `GET /pipeline/status`
2. if the run is still non-terminal, reconnect `GET /pipeline/events?after_seq=<last_seq>`

## Recommended end-to-end frontend flow

### New user and project

1. Wallet connects
2. `POST /users`
3. `GET /projects?user_id=...`
4. User creates project with `POST /projects`
5. Optional rename or screenshot update through `PATCH /projects/{project_id}`

### Planning

1. `POST /plan/start`
2. Open `GET /state/stream`
3. User sends prompt through `POST /agent/message/stream`
4. Update chat from streamed planning events
5. Update plan preview from `state/stream`
6. Repeat until plan is ready

### Approval and pipeline

1. `POST /plan/approve`
2. `POST /pipeline/run`
3. Persist `pipeline_run_id`
4. Open `GET /pipeline/events`
5. Periodically or conditionally call `GET /pipeline/status`

### Human gate

1. Receive `pipeline_waiting_for_approval`
2. Fetch `GET /pipeline/status`
3. Read pending gate from `gates`
4. Submit approve, reject, or override
5. If approved or overridden, call `POST /pipeline/resume`
6. Reconnect timeline using saved `last_pipeline_event_seq`

### Completion

1. Refresh `GET /pipeline/status`
2. Refresh `GET /testing/current`
3. Refresh `GET /deployment/current`
4. Load `GET /artifacts/tree`
5. Load individual artifact files on demand

## Acceptance criteria

- User can connect a wallet and receive a stable `user_id`
- User can create a project and later patch its name or screenshot
- Planning uses `POST /agent/message/stream` after the initial `POST /plan/start`
- Current saved plan updates live without refetching every panel manually
- Pipeline timeline resumes after page refresh using `pipeline_run_id` and `last_pipeline_event_seq`
- Pending gates are rendered from durable `pipeline/status` data
- Approved and overridden gates require an explicit resume call
- Cancellation is supported and reflected durably
- Artifact file content is loaded lazily, not eagerly
- Frontend state is coordinated through one central runtime layer rather than many ad hoc effects

## Open implementation note

If a future backend adds a user update endpoint or changes streamed payload shapes, this document should be updated before frontend implementation starts.
