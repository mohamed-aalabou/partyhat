# Telegram Frontend Integration

This document explains how the frontend should integrate the existing Telegram connection and notification flow in PartyHat.

The backend already supports:

- generating a one-time Telegram deep link
- linking a PartyHat user to a private Telegram chat
- checking current Telegram connection status
- unlinking Telegram
- sending terminal pipeline notifications automatically

The frontend does **not** send Telegram messages directly. The frontend only manages the user's Telegram connection state and UX around connecting, reconnecting, and unlinking.

## What Exists Already

Backend implementation already lives here:

- `agents/api.py`
- `agents/agents/telegram_service.py`
- `agents/agents/db/crud.py`

Current notification behavior:

- notifications are sent automatically when a pipeline reaches a terminal status
- supported terminal states are `completed`, `failed`, and `cancelled`
- delivery happens through the backend outbox/dispatcher, not through the frontend

## Frontend Responsibility

The frontend should do 4 things:

1. resolve and persist a real `user_id`
2. show whether Telegram is connected
3. let the user connect or reconnect Telegram with a deep link
4. let the user unlink Telegram

That is the entire frontend scope for this feature.

## Required Backend Context

Telegram endpoints require a real PartyHat `user_id`. They do **not** work with the placeholder `"default"` user.

Resolve the user first:

### `POST /users?wallet=<WALLET_ADDRESS>`

Example response:

```json
{
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

Persist that `user_id` in the app session and send it on Telegram requests.

## Headers

For Telegram endpoints, the important header is:

```http
X-User-Id: <USER_ID>
```

Recommended app convention:

```http
X-User-Id: <USER_ID>
X-Project-Id: <PROJECT_ID>
Content-Type: application/json
```

`X-Project-Id` is not required for the Telegram endpoints themselves, but sending the normal header pair keeps frontend behavior consistent with the rest of the app.

## Endpoints The Frontend Should Use

### 1. Get current Telegram status: `GET /integrations/telegram/status`

Response shape:

```json
{
  "linked": true,
  "enabled": true,
  "bot_username": "zap_thebot",
  "bot_display_name": "Zap from PartyHat",
  "chat_username": "mohamed",
  "linked_at": "2026-04-10T00:00:00+00:00"
}
```

Meaning of the flags:

- `linked = false`: no Telegram link exists for this user
- `linked = true` and `enabled = true`: Telegram is connected and notifications can be delivered
- `linked = true` and `enabled = false`: a link record exists, but delivery is currently disabled and the user should reconnect

That last case matters. The backend can disable a link after a permanent Telegram delivery failure, for example if the bot was blocked.

### 2. Start connect / reconnect flow: `POST /integrations/telegram/link`

Response shape:

```json
{
  "linked": false,
  "bot_username": "zap_thebot",
  "bot_display_name": "Zap from PartyHat",
  "deep_link_url": "https://t.me/zap_thebot?start=token-123",
  "expires_at": "2026-04-11T12:34:56+00:00"
}
```

Important behavior:

- this endpoint generates a fresh one-time deep link token
- it deletes older unused tokens for the same user
- it is safe to call again if the user wants a new link
- token TTL defaults to 900 seconds unless backend config overrides it
- `expires_at` is the token expiry time, not the lifetime of the Telegram connection
- the `linked` field here is only the current pre-existing active state; it does **not** mean the new connect flow is complete

Completion of the connect flow is confirmed only by polling `GET /integrations/telegram/status`.

### 3. Unlink Telegram: `DELETE /integrations/telegram/link`

Response shape:

```json
{
  "success": true
}
```

Use this when the user clicks Disconnect / Unlink.

## Recommended Frontend Flow

### Initial page load

When the settings page or notification settings UI loads:

1. ensure wallet/user resolution already happened
2. call `GET /integrations/telegram/status`
3. render the UI from `linked` and `enabled`

Recommended UI states:

- `Not connected`
- `Connected`
- `Reconnect required`
- `Connecting...`
- `Disconnecting...`
- `Error`

### Connect or reconnect flow

When the user clicks `Connect Telegram` or `Reconnect Telegram`:

1. call `POST /integrations/telegram/link`
2. open `deep_link_url`
3. start polling `GET /integrations/telegram/status`
4. stop polling when `linked && enabled` becomes `true`
5. also stop polling once `expires_at` is reached
6. if polling expires first, show `Link expired` and offer `Generate new link`

Polling every 2 to 3 seconds is sufficient.

Notes:

- Telegram linking only works in a private chat with the bot
- the user must press `Start` in Telegram after opening the deep link
- the frontend should treat the deep link as an external navigation

### Disconnect flow

When the user clicks `Disconnect Telegram`:

1. call `DELETE /integrations/telegram/link`
2. optimistically set local UI to disconnected, or refetch `GET /integrations/telegram/status`

## Notification semantics

There is currently no separate frontend API for notification preferences.

Behavior today is:

- if the Telegram link exists and is enabled, backend terminal notifications are sent automatically
- if the Telegram link does not exist, nothing is sent
- if the Telegram link becomes disabled, the frontend should prompt the user to reconnect

The frontend does not need to call a separate `enable notifications` endpoint.

## What Notifications The User Will Receive

The current backend sends Telegram messages for terminal pipeline outcomes:

- deployment completed
- deployment failed
- deployment cancelled

The message may include:

- PartyHat bot display name
- project name
- status label
- network name
- transaction hash
- Snowtrace transaction URL for Avalanche networks
- deployed contract names and addresses
- Snowtrace address links for deployed contracts
- deep link back into PartyHat with `project_id` and `pipeline_run_id`

The frontend does not build this message. The backend builds and sends it.

## Error Handling

Expected error cases the frontend should handle:

- `400`: `X-User-Id` missing or invalid
- `404`: user not found
- `503`: Telegram is not configured on the backend, or database access is unavailable

Recommended UI behavior:

- show a normal inline error for `400` and `404`
- show `Telegram notifications are temporarily unavailable` for `503`

## Recommended Frontend State Model

Suggested state shape:

```ts
type TelegramStatus = {
  linked: boolean;
  enabled: boolean;
  bot_username: string;
  bot_display_name: string;
  chat_username: string | null;
  linked_at: string | null;
};

type TelegramLinkResponse = {
  linked: boolean;
  bot_username: string;
  bot_display_name: string;
  deep_link_url: string;
  expires_at: string;
};
```

Suggested derived UI states:

```ts
function deriveTelegramUiState(status: TelegramStatus | null) {
  if (!status) return "not_connected";
  if (!status.linked) return "not_connected";
  if (status.enabled) return "connected";
  return "reconnect_required";
}
```

## Example API Helpers

```ts
const apiBase = process.env.NEXT_PUBLIC_API_BASE_URL!;

function partyhatHeaders(userId: string, projectId?: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "X-User-Id": userId,
    ...(projectId ? { "X-Project-Id": projectId } : {}),
  };
}

async function getTelegramStatus(userId: string, projectId?: string) {
  const res = await fetch(`${apiBase}/integrations/telegram/status`, {
    method: "GET",
    headers: partyhatHeaders(userId, projectId),
  });
  if (!res.ok) throw await res.json();
  return (await res.json()) as TelegramStatus;
}

async function createTelegramLink(userId: string, projectId?: string) {
  const res = await fetch(`${apiBase}/integrations/telegram/link`, {
    method: "POST",
    headers: partyhatHeaders(userId, projectId),
  });
  if (!res.ok) throw await res.json();
  return (await res.json()) as TelegramLinkResponse;
}

async function unlinkTelegram(userId: string, projectId?: string) {
  const res = await fetch(`${apiBase}/integrations/telegram/link`, {
    method: "DELETE",
    headers: partyhatHeaders(userId, projectId),
  });
  if (!res.ok) throw await res.json();
  return (await res.json()) as { success: boolean };
}
```

## Example Connect Logic

```ts
async function connectTelegram(userId: string, projectId?: string) {
  const link = await createTelegramLink(userId, projectId);

  window.open(link.deep_link_url, "_blank", "noopener,noreferrer");

  const expiresAt = new Date(link.expires_at).getTime();

  return await new Promise<TelegramStatus>((resolve, reject) => {
    const interval = window.setInterval(async () => {
      try {
        if (Date.now() >= expiresAt) {
          window.clearInterval(interval);
          reject(new Error("Telegram link expired"));
          return;
        }

        const status = await getTelegramStatus(userId, projectId);
        if (status.linked && status.enabled) {
          window.clearInterval(interval);
          resolve(status);
        }
      } catch (error) {
        window.clearInterval(interval);
        reject(error);
      }
    }, 2500);
  });
}
```

## UX Copy Guidance

Recommended labels:

- `Connect Telegram`
- `Reconnect Telegram`
- `Disconnect Telegram`
- `Connected`
- `Reconnect required`
- `Open Telegram`
- `Generate new link`

Recommended helper text:

- `Connect your Telegram account to receive deployment updates.`
- `After opening Telegram, press Start in the bot chat to finish linking.`
- `Your previous Telegram connection is no longer active. Reconnect to resume notifications.`

## Backend-Only Details The Frontend Should Not Implement

The frontend should **not** call or reproduce any of this logic:

- `POST /integrations/telegram/webhook`
- Telegram bot API calls
- webhook secret handling
- notification queueing
- outbox retries
- message formatting

Those are already implemented on the backend.

## Deployment / QA Checklist

Before frontend QA, confirm backend env is set correctly:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME` or `TELEGRAM_BOT_NAME`
- `TELEGRAM_BOT_DISPLAY_NAME`
- `TELEGRAM_WEBHOOK_BASE_URL`
- `TELEGRAM_WEBHOOK_SECRET`
- `DATABASE_URL`

Optional but useful:

- `TELEGRAM_CONNECT_TOKEN_TTL_SECONDS`
- `PARTYHAT_APP_BASE_URL`

Frontend QA checklist:

1. Connect wallet and obtain a real `user_id`
2. Open Telegram settings UI and verify disconnected state
3. Click `Connect Telegram`
4. Confirm Telegram opens with the bot deep link
5. Press `Start` in Telegram
6. Confirm frontend status changes to connected
7. Run a pipeline to terminal completion and confirm a Telegram message arrives
8. Block the bot or simulate delivery failure and confirm status becomes reconnect-required
9. Click `Disconnect Telegram` and confirm status returns to disconnected

## Bottom Line

The frontend integration is simple:

- use `GET /integrations/telegram/status` to render state
- use `POST /integrations/telegram/link` to start connect/reconnect
- poll `GET /integrations/telegram/status` until connected
- use `DELETE /integrations/telegram/link` to disconnect
- do not build notification delivery in the frontend
