# PartyHat API — Endpoints & Frontend Integration

This document describes the PartyHat API endpoints, how to call them, and how to integrate them into a frontend.

**Base URL (local):** `http://localhost:8000`  
**CORS:** Allowed origins are `http://localhost:3000` and `http://localhost:3001`.

---

## Pushing the schema to the database

The app uses SQLAlchemy with `create_all`: it **creates** missing tables but does **not** alter existing ones.

**Option 1 — Start the API (creates tables on startup)**  
If `DATABASE_URL` is set, tables are created when the server starts:

```bash
cd agents && uv run uvicorn api:app --reload --port 8000
```

**Option 2 — Run the sync script (no server)**  
From the repo root, with `DATABASE_URL` in `agents/.env`:

```bash
cd agents && uv run python sync_schema.py
```

**Existing database with the old `email` column?**  
`create_all` will not rename `email` → `wallet`. Either:

- **Reset (data loss):** use a one-off script that calls `drop_tables()` then `create_tables()` from `agents.db`, or
- **Migrate:** run SQL on your Postgres instance:
  ```sql
  ALTER TABLE users RENAME COLUMN email TO wallet;
  ```

---

## Request context (project & user)

Many endpoints are **project- and user-scoped**. You can pass context in two ways:

1. **Headers (recommended)**
   - `X-Project-Id`: project UUID or `"default"`
   - `X-User-Id`: user UUID or `"default"`

2. **Body or query**
   - Some endpoints accept `project_id` and `user_id` in the request body or as query parameters for backward compatibility.

If you omit both, `project_id` and `user_id` default to `"default"`. For project-scoped memory and sandbox, use real project/user IDs and ensure `DATABASE_URL` is set.

---

## Endpoints overview

| Method | Path                     | Description                                        |
| ------ | ------------------------ | -------------------------------------------------- |
| GET    | `/health`                | Health check                                       |
| POST   | `/users`                 | Create or resolve user by wallet (wallet required) |
| POST   | `/projects`              | Create project                                     |
| GET    | `/projects`              | List projects for a user                           |
| GET    | `/users/{user_id}/projects` | List projects for a user (alias route)          |
| GET    | `/projects/{project_id}` | Get one project                                    |
| PATCH  | `/projects/{project_id}` | Partially update project fields                    |
| GET    | `/messages`              | List persisted chat messages for a project         |
| POST   | `/plan/start`            | Start planning session                             |
| POST   | `/plan/message`          | Send message to planning agent                     |
| GET    | `/plan/current`          | Get current plan                                   |
| POST   | `/plan/approve`          | Approve plan (ready for code gen)                  |
| GET    | `/coding/current`        | Get current code artifacts                         |
| POST   | `/coding/generate`       | Generate Solidity from goal                        |
| GET    | `/deployment/current`    | Get last deploy results                            |
| GET    | `/testing/current`      | Get last test results                              |
| GET    | `/artifacts/tree`        | Artifact directory tree                            |
| GET    | `/artifacts/file`        | Get artifact file content                          |
| GET    | `/memory/full`           | Full memory snapshot (debug)                       |
| POST   | `/agent/message`         | Routed message by intent                           |
| POST   | `/agent/message/stream`  | Streamed routed message (SSE)                      |

---

## Health & status

### `GET /health`

**Response:** `200 OK`

```json
{
	"status": "ok",
	"service": "partyhat-agents"
}
```

**Frontend:** Use for readiness checks and “API connected” indicators.

---

## Users & projects

### `POST /users`

Create or resolve a user by wallet. Requires `wallet`. If the wallet is already linked to a user, returns that user's `user_id`; otherwise creates a new user, links the wallet, and returns the new `user_id`.

**Query (required):** `wallet` (string) — e.g. Ethereum address

**Response:** `200 OK`

```json
{
	"user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Errors:** `503` if `DATABASE_URL` is not configured. `422` if `wallet` is missing.

---

### `POST /projects`

Create a new project for a user.

**Body:**

```json
{
	"user_id": "550e8400-e29b-41d4-a716-446655440000",
	"name": "My Token Project"
}
```

- `user_id` (string, required): UUID of the user.
- `name` (string, optional): Project name.

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

**Query:** `user_id` (string, required) — user UUID.

**Response:** `200 OK`

```json
[
	{
		"id": "660e8400-e29b-41d4-a716-446655440001",
		"user_id": "550e8400-e29b-41d4-a716-446655440000",
		"name": "My Token Project",
		"created_at": "2025-03-07T12:00:00"
	}
]
```

**Errors:** `400` invalid `user_id`, `503` no database.

---

### `GET /users/{user_id}/projects`

Alias route for `GET /projects` that accepts `user_id` in the path instead of query.

**Path:** `user_id` (string, required) - user UUID.

**Response:** `200 OK` (same shape as `GET /projects`)

```json
[
	{
		"id": "660e8400-e29b-41d4-a716-446655440001",
		"user_id": "550e8400-e29b-41d4-a716-446655440000",
		"name": "My Token Project",
		"created_at": "2025-03-07T12:00:00"
	}
]
```

**Errors:** `400` invalid `user_id`, `503` no database.

---

### `GET /projects/{project_id}`

Get a single project. Ownership is validated when `user_id` is provided.

**Path:** `project_id` (UUID).  
**Query:** `user_id` (string, required) — user UUID.

**Response:** `200 OK`

```json
{
	"id": "660e8400-e29b-41d4-a716-446655440001",
	"user_id": "550e8400-e29b-41d4-a716-446655440000",
	"name": "My Token Project",
	"created_at": "2025-03-07T12:00:00"
}
```

**Errors:** `400` invalid IDs, `404` not found, `503` no database.

---

### `PATCH /projects/{project_id}`

Partially update a project by id. Only fields that are explicitly included in the JSON body are updated.

**Path:** `project_id` (UUID).

**Body (all optional):**

```json
{
	"name": "My Updated Project Name",
	"screenshot_base64": "data:image/png;base64,iVBORw0KGgoAAA..."
}
```

- `name` (string or `null`, optional): Project name.
- `screenshot_base64` (string or `null`, optional): Base64 PNG screenshot string.

Behavior notes:

- Uses partial update semantics (`exclude_unset=True`), so omitted fields are left unchanged.
- Sending `screenshot_base64: null` clears the stored screenshot.

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

List persisted chat messages for a project. Supports optional filtering by `session_id`.

**Query:**

- `session_id` (string, optional): Restrict to one chat session.
- `limit` (int, optional, default `200`): Max messages returned.
- `project_id` (optional, default `"default"`): Use explicit query value or `X-Project-Id` header.
- `user_id` (optional, default `"default"`): Use explicit query value or `X-User-Id` header.

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

**Errors:** `400` missing/invalid `project_id`, `503` no database.

**Frontend:** Load chat history on page refresh or when reopening a project/session.

---

## Planning flow

The planning flow is: **start session → send messages → read current plan → approve plan** (then use coding endpoints).

### `POST /plan/start`

Creates a new session and returns the agent’s opening message. The frontend should store `session_id` and use it for all subsequent planning (and optionally agent) calls.

**Body (optional):**

```json
{
	"project_id": "660e8400-e29b-41d4-a716-446655440001",
	"user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

Or rely on `X-Project-Id` / `X-User-Id` headers.

**Response:** `200 OK`

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"message": "Hello! I'm here to help you plan your smart contract...",
	"answer_recommendations": [
		{ "text": "Create an ERC-20 token", "recommended": true },
		{ "text": "Create an ERC-721 NFT collection" },
		{ "text": "Create an ERC-1155 multi-token contract" }
	],
	"pending_questions": [
		{
			"question": "What type of contract do you want to build?",
			"answer_recommendations": [
				{ "text": "ERC-20 token", "recommended": true },
				{ "text": "ERC-721 NFT collection" },
				{ "text": "ERC-1155 multi-token contract" }
			]
		}
	]
}
```

**Frontend:** After calling this, save `session_id` and show `message` as the first bot message. If `pending_questions` is present, render the questions directly; `answer_recommendations` remains as a backward-compatible shortcut for the first question.

---

### `POST /plan/message`

Send a user message to the planning agent.

**Body:**

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"message": "I want an ERC-20 token with mint and burn.",
	"project_id": "660e8400-e29b-41d4-a716-446655440001",
	"user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

- `session_id` (string, required): From `/plan/start`.
- `message` (string, required): User message.
- `project_id` / `user_id` (optional): Override headers if needed.

**Response:** `200 OK`

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"response": "I have enough to start. Please answer these three questions in one reply: 1. Should supply be fixed or mintable? 2. Who can mint? 3. Do you want a cap?",
	"tool_calls": ["send_question_batch", "save_plan_draft"],
	"answer_recommendations": [
		{ "text": "Fixed initial supply (e.g. 1,000,000)", "recommended": true },
		{ "text": "Mintable supply controlled by owner" },
		{ "text": "No cap for now; decide later" }
	],
	"pending_questions": [
		{
			"question": "Should the token supply be fixed or mintable after deployment?",
			"answer_recommendations": [
				{ "text": "Fixed initial supply (e.g. 1,000,000)", "recommended": true },
				{ "text": "Mintable supply controlled by owner" }
			]
		},
		{
			"question": "Who should be allowed to mint new tokens?",
			"answer_recommendations": [
				{ "text": "Only the owner", "recommended": true },
				{ "text": "Addresses with a MINTER_ROLE" }
			]
		},
		{
			"question": "Do you want a maximum token cap?",
			"answer_recommendations": [
				{ "text": "No cap for now; decide later", "recommended": true },
				{ "text": "Yes, set a fixed cap" }
			]
		}
	]
}
```

**Errors:** `400` empty message, `500` agent error.

**Frontend:** Append the user message, then append `response` as the assistant message. Prefer `pending_questions` for rendering a multi-question batch UI. `answer_recommendations` is still returned for older clients and mirrors the first question's quick replies.

---

### `GET /plan/current`

Get the current plan for the user/project context.

**Query:**

- `project_id` (optional, default `"default"`)
- `user_id` (optional, default `"default"`)

**Response:** `200 OK`

```json
{
  "plan": {
    "project_name": "PartyToken",
    "description": "ERC-20 with mint and burn",
    "status": "draft",
    "contracts": [
      {
        "name": "PartyToken",
        "description": "Main token contract",
        "erc_template": "ERC-20",
        "dependencies": ["Ownable"],
        "constructor": { "inputs": [...], "description": "..." },
        "functions": [
          {
            "name": "mint",
            "description": "...",
            "inputs": [...],
            "outputs": [...],
            "conditions": [...]
          }
        ]
      }
    ]
  },
  "status": "draft"
}
```

If there is no plan yet, `plan` and `status` can be `null`.

**Plan statuses:** `draft` → `ready` → `generating` → `testing` → `deployed`. Only non-deployed plans can be edited.

**Frontend:** Call after messages or on “View plan” to show the structured plan; use `status` to drive UI (e.g. enable “Approve” when status is `draft`).

---

### `POST /plan/approve`

Mark the current plan as **ready** for code generation. Fails if there is no plan or if the plan is already `deployed`.

**Body:**

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"project_id": "660e8400-e29b-41d4-a716-446655440001",
	"user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Response:** `200 OK`

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"success": true,
	"message": "Plan approved. Project 'PartyToken' is ready for code generation."
}
```

**Errors:** `404` no plan, `400` plan already deployed, `500` server error.

**Frontend:** After approval, switch to the “Code” or “Generate” step and use `/coding/generate` or `/agent/message` with intent `"coding"`.

---

## Coding

### `GET /coding/current`

List current code artifacts for the project/user (from headers or query).

**Query:** `session_id`, optional `project_id`, `user_id`.

**Response:** `200 OK`

```json
{
	"artifacts": [
		{
			"path": "src/PartyToken.sol",
			"language": "solidity",
			"description": "ERC-20 token",
			"contract_names": ["PartyToken"],
			"related_plan_id": null,
			"created_at": "2025-03-07T12:00:00"
		}
	]
}
```

**Frontend:** Use to show “Generated files” and link to `/artifacts/file` for content.

---

## Deployment

### `GET /deployment/current`

Return the last deploy results for this user/project (from `run_foundry_deploy`). Use to check if deployments are done and successful.

**Query:** optional `project_id`, `user_id` — or use headers `X-Project-Id`, `X-User-Id`.

**Response:** `200 OK`

```json
{
	"last_deploy_results": [
		{
			"timestamp": "2025-03-08T12:00:00.000000+00:00",
			"project_root": "/path/to/project",
			"sandbox_workdir": "/workspace",
			"network": "avalanche_fuji",
			"chain_id": 43113,
			"script_path": "script/Deploy.s.sol",
			"command": "forge script ...",
			"exit_code": 0,
			"stdout": "...",
			"stderr": "",
			"modal_app": "...",
			"tx_hash": "0x...",
			"deployed_address": "0x..."
		}
	]
}
```

**Frontend:** Poll or call after deploy; treat `success === true` as authoritative. Successful deploys require a clean forge exit plus either a deployment `tx_hash` or a confirmed deployed contract address.

---

## Testing

### `GET /testing/current`

Return the last test results for this user/project (from `run_foundry_tests`). Use to check if tests have run and whether they passed.

**Query:** optional `project_id`, `user_id` — or use headers `X-Project-Id`, `X-User-Id`.

**Response:** `200 OK`

```json
{
	"last_test_results": [
		{
			"timestamp": "2025-03-08T12:00:00.000000+00:00",
			"project_root": "/path/to/project",
			"sandbox_workdir": "/workspace",
			"command": "forge test ...",
			"exit_code": 0,
			"stdout": "...",
			"stderr": "",
			"modal_app": "..."
		}
	]
}
```

**Frontend:** Poll or call after test run; treat latest entry with `exit_code === 0` as success.

---

## Coding

### `POST /coding/generate`

Generate Solidity code from a short goal string (standalone, no chat). Good for “quick generate” from a single prompt.

**Body:**

```json
{
	"goal": "ERC-20 token with mint and burn, 18 decimals, Ownable."
}
```

**Query (optional):** `project_id`, `user_id` — or use headers.

**Response:** `200 OK`

```json
{
	"generated_code": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n...",
	"goal": "ERC-20 token with mint and burn, 18 decimals, Ownable."
}
```

**Errors:** `400` empty goal, `500` generation failure.

**Frontend:** Single “Generate” button; show `generated_code` in an editor or diff view, then optionally persist via the coding agent or artifact endpoints.

---

## Artifacts (generated files)

### `GET /artifacts/tree`

Directory tree of generated artifacts, scoped by project when `project_id` is not `"default"`.

**Query:** `project_id`, `user_id` (optional).

**Response:** `200 OK`

```json
{
	"name": "artifacts",
	"path": "artifacts",
	"type": "directory",
	"children": [
		{
			"name": "src",
			"path": "artifacts/src",
			"type": "directory",
			"children": [
				{
					"name": "PartyToken.sol",
					"path": "artifacts/src/PartyToken.sol",
					"type": "file",
					"children": null
				}
			]
		}
	]
}
```

**Frontend:** Render a tree (e.g. collapsible folders); use `path` for `/artifacts/file?relative_path=...`.

---

### `GET /artifacts/file`

Raw content of one artifact file.

**Query:**

- `relative_path` (string, required): e.g. `src/PartyToken.sol`
- `project_id`, `user_id` (optional)

**Response:** `200 OK`

```json
{
	"path": "src/PartyToken.sol",
	"content": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.0;\n..."
}
```

**Errors:** `400` empty path, `404` file not found.

**Frontend:** Use for “Open file” or inline code view; combine with `/artifacts/tree` for a file explorer.

---

## Generic agent (routed by intent)

These endpoints route to different agents by **intent**. Use them when you want one API for planning, coding, and testing.

**Intents:** `planning` | `coding` | `testing`

### `POST /agent/message`

Single request/response.

**Body:**

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"intent": "planning",
	"message": "Add a pause function to the contract.",
	"project_id": "660e8400-e29b-41d4-a716-446655440001",
	"user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

- `session_id` (required)
- `intent` (required): `"planning"` | `"coding"` | `"testing"`
- `message` (required)
- `project_id` / `user_id` (optional)

**Response:** Same shape as `/plan/message`:

```json
{
	"session_id": "770e8400-e29b-41d4-a716-446655440002",
	"response": "I've added a pause function...",
	"tool_calls": ["save_coding_note"],
	"answer_recommendations": [],
	"pending_questions": []
}
```

**Errors:** `400` empty message or unknown intent, `500` agent error.

**Frontend:** One “Send” action per intent; e.g. tabs or mode selector for Planning / Coding / Testing, same `session_id` for the whole flow.

---

### `POST /agent/message/stream`

Same body as `/agent/message`, but the response is **Server-Sent Events (SSE)**.

**Response:** `Content-Type: text/event-stream`

Each event is a JSON line after `data: `:

- **Step (while agent is working):**

  ```json
  { "type": "step", "content": "...", "tool_calls": ["..."] }
  ```

- **Done:**

  ```json
  {
    "type": "done",
    "session_id": "...",
    "response": "...",
    "tool_calls": [...],
    "answer_recommendations": [
      { "text": "Option A", "recommended": true },
      { "text": "Option B" }
    ],
    "pending_questions": [
      {
        "question": "Which ERC standard do you want?",
        "answer_recommendations": [
          { "text": "ERC-20", "recommended": true },
          { "text": "ERC-721" }
        ]
      }
    ]
  }
  ```

- **Error:**

  ```json
  { "type": "error", "detail": "Unknown intent: xyz" }
  ```

**Frontend (JavaScript):**

```javascript
const eventSource = new EventSource(
	"/agent/message/stream?" +
		new URLSearchParams({
			/* not used; body is POST */
		}),
);
// EventSource is GET-only; for POST + stream use fetch + ReadableStream:
async function streamAgentMessage(body) {
	const res = await fetch("http://localhost:8000/agent/message/stream", {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			"X-Project-Id": projectId,
			"X-User-Id": userId,
		},
		body: JSON.stringify(body),
	});
	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = "";
	while (true) {
		const { value, done } = await reader.read();
		if (done) break;
		buffer += decoder.decode(value, { stream: true });
		const lines = buffer.split("\n\n");
		buffer = lines.pop() || "";
		for (const line of lines) {
			if (line.startsWith("data: ")) {
				const data = JSON.parse(line.slice(6));
				if (data.type === "step") appendToUI(data.content);
				else if (data.type === "done") setFinalResponse(data.response);
				else if (data.type === "error") showError(data.detail);
			}
		}
	}
}
```

Use this for typing effect or progressive output while the agent runs.

---

## Debug

### `GET /memory/full`

Returns the full project-scoped user memory and global agent log (Letta blocks). For debugging/observability.

**Query:** `project_id`, `user_id` (optional).

**Response:** `200 OK`

```json
{
  "user_block_label": "user:...",
  "user_memory": { ... },
  "global_block_label": "global:...",
  "global_memory": { ... }
}
```

**Frontend:** Optional “Debug” or “Memory” panel; avoid in production UX.

---

## Frontend integration checklist

1. **Auth / user**
   - Create or resolve user (e.g. `POST /users`), store `user_id`.

2. **Projects**
   - `POST /projects` to create, `GET /projects?user_id=...` to list.
   - Optional alias: `GET /users/{user_id}/projects`.
   - Store current `project_id` and send it (and `user_id`) on every request via headers:  
     `X-Project-Id`, `X-User-Id`.

3. **Planning**
   - `POST /plan/start` → store `session_id`, show first message.
   - Chat: `POST /plan/message` with `session_id` + `message`.
   - Optional history restore: `GET /messages?session_id=...`.
   - Plan view: `GET /plan/current` (uses `project_id`/`user_id` via query or headers).
   - When user is happy: `POST /plan/approve`.

4. **Coding**
   - Option A: `POST /coding/generate` with a `goal` for one-shot generation.
   - Option B: Use `POST /agent/message` with `intent: "coding"` (and same `session_id`) for conversational code gen.
   - List artifacts: `GET /coding/current`.
   - File tree: `GET /artifacts/tree`; file content: `GET /artifacts/file?relative_path=...`.

5. **Streaming**
   - Use `POST /agent/message/stream` with `fetch` + `ReadableStream` (as above) for live typing or long-running replies.

6. **CORS**
   - Frontend must run on `http://localhost:3000` or `http://localhost:3001`, or you need to add your origin to the API’s CORS middleware in `api.py`.

7. **Errors**
   - All errors return JSON `{ "detail": "..." }`. Use `detail` for user-facing or toast messages.

---

## OpenAPI / Swagger

When the server is running, interactive docs are available at:

- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`

Use them to try endpoints and see exact request/response schemas.
