import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import requests

from agents.db import async_session_factory
from agents.db.crud import (
    claim_next_notification_outbox,
    consume_telegram_link_token,
    get_telegram_user_link,
    mark_notification_outbox_failed,
    mark_notification_outbox_pending,
    mark_notification_outbox_sent,
    set_telegram_user_link_enabled,
    upsert_telegram_user_link,
)

TELEGRAM_CHANNEL = "telegram"
TELEGRAM_EVENT_COMPLETED = "pipeline.completed"
TELEGRAM_EVENT_FAILED = "pipeline.failed"
TELEGRAM_EVENT_CANCELLED = "pipeline.cancelled"
TELEGRAM_CONNECT_DEFAULT_TTL_SECONDS = 900
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 10
TELEGRAM_DISPATCH_STALE_AFTER_SECONDS = 300
TELEGRAM_DISPATCH_IDLE_SECONDS = 2.0
TELEGRAM_DISPATCH_ERROR_SECONDS = 5.0

_dispatcher_task: asyncio.Task[Any] | None = None


@dataclass(slots=True)
class TelegramApiError(Exception):
    message: str
    permanent: bool = False
    disable_link: bool = False
    retry_after: int | None = None

    def __str__(self) -> str:
        return self.message


def _clean_env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def get_telegram_bot_token() -> str:
    return _clean_env("TELEGRAM_BOT_TOKEN")


def get_telegram_bot_username() -> str:
    return (_clean_env("TELEGRAM_BOT_USERNAME") or _clean_env("TELEGRAM_BOT_NAME")).lstrip(
        "@"
    )


def get_telegram_bot_display_name() -> str:
    return _clean_env("TELEGRAM_BOT_DISPLAY_NAME") or get_telegram_bot_username() or "PartyHat"


def get_telegram_webhook_base_url() -> str:
    return _clean_env("TELEGRAM_WEBHOOK_BASE_URL").rstrip("/")


def get_telegram_webhook_secret() -> str:
    return _clean_env("TELEGRAM_WEBHOOK_SECRET")


def get_telegram_connect_token_ttl_seconds() -> int:
    raw = _clean_env("TELEGRAM_CONNECT_TOKEN_TTL_SECONDS")
    try:
        ttl = int(raw) if raw else TELEGRAM_CONNECT_DEFAULT_TTL_SECONDS
    except ValueError:
        ttl = TELEGRAM_CONNECT_DEFAULT_TTL_SECONDS
    return max(60, ttl)


def _telegram_api_url(method: str) -> str:
    token = get_telegram_bot_token()
    if not token:
        raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured.", permanent=False)
    return f"https://api.telegram.org/bot{token}/{method}"


def build_telegram_webhook_url() -> str | None:
    base_url = get_telegram_webhook_base_url()
    if not base_url:
        return None
    return f"{base_url}/integrations/telegram/webhook"


def generate_telegram_connect_token() -> str:
    return secrets.token_urlsafe(24)


def hash_telegram_connect_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_telegram_deep_link(token: str) -> str:
    username = get_telegram_bot_username()
    if not username:
        raise TelegramApiError(
            "Telegram bot username is not configured.",
            permanent=True,
        )
    return f"https://t.me/{username}?start={token}"


def terminal_event_type_for_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized == "completed":
        return TELEGRAM_EVENT_COMPLETED
    if normalized == "cancelled":
        return TELEGRAM_EVENT_CANCELLED
    return TELEGRAM_EVENT_FAILED


def terminal_notification_dedupe_key(pipeline_run_id: str, status: str) -> str:
    return f"{TELEGRAM_CHANNEL}:{pipeline_run_id}:{status.strip().lower()}"


def _snowtrace_base_url(*, chain_id: int | None, network: str | None) -> str | None:
    if chain_id == 43113 or (network or "").strip().lower() == "avalanche_fuji":
        return "https://testnet.snowtrace.io"
    if chain_id == 43114 or (network or "").strip().lower() == "avalanche_mainnet":
        return "https://snowtrace.io"
    return None


def snowtrace_tx_url(
    *,
    chain_id: int | None,
    network: str | None,
    tx_hash: str | None,
) -> str | None:
    if not tx_hash:
        return None
    base = _snowtrace_base_url(chain_id=chain_id, network=network)
    if not base:
        return None
    return f"{base}/tx/{tx_hash}"


def snowtrace_address_url(
    *,
    chain_id: int | None,
    network: str | None,
    address: str | None,
) -> str | None:
    if not address:
        return None
    base = _snowtrace_base_url(chain_id=chain_id, network=network)
    if not base:
        return None
    return f"{base}/address/{address}"


def _partyhat_open_url(project_id: str, pipeline_run_id: str) -> str | None:
    base = _clean_env("PARTYHAT_APP_BASE_URL").rstrip("/")
    if not base:
        return None
    query = urlencode(
        {
            "project_id": project_id,
            "pipeline_run_id": pipeline_run_id,
        }
    )
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}"


def build_terminal_notification_payload(
    *,
    pipeline_run_id: str,
    project_id: str,
    project_name: str | None,
    terminal_status: str,
    deployment_target: dict[str, Any] | None,
    terminal_deployment: Any | None = None,
    failure_reason: str | None = None,
    cancelled_reason: str | None = None,
) -> dict[str, Any]:
    target = deployment_target or {}
    chain_id = target.get("chain_id")
    network = target.get("network")
    network_name = (
        target.get("name")
        or target.get("description")
        or target.get("network")
        or "Deployment target"
    )
    tx_hash = getattr(terminal_deployment, "tx_hash", None)
    tx_url = snowtrace_tx_url(chain_id=chain_id, network=network, tx_hash=tx_hash)
    deployed_contracts = getattr(terminal_deployment, "deployed_contracts", None) or []

    contracts: list[dict[str, Any]] = []
    for contract in deployed_contracts:
        if not isinstance(contract, dict):
            continue
        address = contract.get("deployed_address")
        contracts.append(
            {
                "name": contract.get("contract_name") or "Contract",
                "address": address,
                "url": snowtrace_address_url(
                    chain_id=chain_id,
                    network=network,
                    address=address,
                ),
            }
        )

    if not contracts and terminal_deployment is not None:
        address = getattr(terminal_deployment, "deployed_address", None)
        if address or getattr(terminal_deployment, "contract_name", None):
            contracts.append(
                {
                    "name": getattr(terminal_deployment, "contract_name", None)
                    or "Contract",
                    "address": address,
                    "url": snowtrace_address_url(
                        chain_id=chain_id,
                        network=network,
                        address=address,
                    ),
                }
            )

    return {
        "pipeline_run_id": pipeline_run_id,
        "project_id": project_id,
        "project_name": project_name or "PartyHat project",
        "terminal_status": terminal_status,
        "status_label": {
            "completed": "Deployed",
            "failed": "Failed",
            "cancelled": "Cancelled",
        }.get((terminal_status or "").strip().lower(), "Updated"),
        "network": {
            "network": network,
            "name": network_name,
            "chain_id": chain_id,
        },
        "tx_hash": tx_hash,
        "tx_url": tx_url,
        "contracts": contracts,
        "failure_reason": failure_reason,
        "cancelled_reason": cancelled_reason,
        "partyhat_url": _partyhat_open_url(project_id, pipeline_run_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def format_terminal_notification_message(payload: dict[str, Any]) -> str:
    status_label = str(payload.get("status_label") or "Updated")
    project_name = str(payload.get("project_name") or "PartyHat project")
    network_name = (
        ((payload.get("network") or {}).get("name"))
        if isinstance(payload.get("network"), dict)
        else None
    )
    lines = [
        get_telegram_bot_display_name(),
        "",
        f"Project: {project_name}",
        f"Status: {status_label}",
    ]
    if network_name:
        lines.append(f"Network: {network_name}")

    failure_reason = payload.get("failure_reason")
    if failure_reason:
        lines.append(f"Reason: {failure_reason}")

    cancelled_reason = payload.get("cancelled_reason")
    if cancelled_reason and cancelled_reason != failure_reason:
        lines.append(f"Cancellation: {cancelled_reason}")

    tx_hash = payload.get("tx_hash")
    if tx_hash:
        lines.append(f"Tx: {tx_hash}")
    tx_url = payload.get("tx_url")
    if tx_url:
        lines.append(str(tx_url))

    contracts = payload.get("contracts") if isinstance(payload.get("contracts"), list) else []
    if contracts:
        lines.append("Contracts:")
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            name = contract.get("name") or "Contract"
            address = contract.get("address")
            url = contract.get("url")
            if address:
                lines.append(f"- {name}: {address}")
            else:
                lines.append(f"- {name}")
            if url:
                lines.append(f"  {url}")

    partyhat_url = payload.get("partyhat_url")
    if partyhat_url:
        lines.extend(["", f"Open in PartyHat: {partyhat_url}"])

    return "\n".join(lines).strip()


def _classify_telegram_api_failure(response: requests.Response) -> TelegramApiError:
    message = f"Telegram API request failed with status {response.status_code}."
    retry_after = None
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    description = str(payload.get("description") or "").strip()
    if description:
        message = description
    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        raw_retry_after = parameters.get("retry_after")
        if raw_retry_after is not None:
            try:
                retry_after = int(raw_retry_after)
            except (TypeError, ValueError):
                retry_after = None

    lower_message = message.lower()
    permanent = response.status_code >= 400 and response.status_code < 500 and response.status_code != 429
    disable_link = any(
        token in lower_message
        for token in (
            "bot was blocked by the user",
            "chat not found",
            "user is deactivated",
            "forbidden: bot was kicked",
        )
    )
    return TelegramApiError(
        message=message,
        permanent=permanent,
        disable_link=disable_link,
        retry_after=retry_after,
    )


def _telegram_post_sync(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        _telegram_api_url(method),
        json=payload,
        timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        raise _classify_telegram_api_failure(response)

    data = response.json()
    if not data.get("ok", False):
        message = str(data.get("description") or "Telegram API returned ok=false.")
        parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        retry_after = parameters.get("retry_after")
        try:
            retry_after_value = int(retry_after) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after_value = None
        lower_message = message.lower()
        raise TelegramApiError(
            message=message,
            permanent=True,
            disable_link=(
                "bot was blocked by the user" in lower_message or "chat not found" in lower_message
            ),
            retry_after=retry_after_value,
        )
    return data


async def send_telegram_message(chat_id: int, text: str) -> dict[str, Any]:
    payload = {
        "chat_id": int(chat_id),
        "text": text,
        "disable_web_page_preview": False,
    }
    return await asyncio.to_thread(_telegram_post_sync, "sendMessage", payload)


async def configure_telegram_webhook() -> dict[str, Any]:
    webhook_url = build_telegram_webhook_url()
    secret = get_telegram_webhook_secret()
    token = get_telegram_bot_token()
    if not token or not webhook_url or not secret:
        return {"configured": False, "reason": "missing_env"}

    payload = {
        "url": webhook_url,
        "secret_token": secret,
        "allowed_updates": ["message"],
        "drop_pending_updates": False,
    }
    return await asyncio.to_thread(_telegram_post_sync, "setWebhook", payload)


async def handle_telegram_webhook_update(update: dict[str, Any]) -> dict[str, Any]:
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return {"handled": False, "reason": "unsupported_update"}

    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = chat.get("id")
    chat_type = str(chat.get("type") or "")
    text = str(message.get("text") or "").strip()
    if not chat_id or not text.startswith("/start"):
        return {"handled": False, "reason": "unsupported_message"}

    if chat_type != "private":
        return {"handled": False, "reason": "non_private_chat"}

    token = text.partition(" ")[2].strip()
    if not token:
        await send_telegram_message(
            int(chat_id),
            "Return to PartyHat and use Connect Telegram to link this chat.",
        )
        return {"handled": True, "linked": False, "reason": "missing_token"}

    token_hash = hash_telegram_connect_token(token)
    user_row = message.get("from") if isinstance(message.get("from"), dict) else {}

    async with async_session_factory() as session:
        link_token = await consume_telegram_link_token(session, token_hash)
        if link_token is None:
            await send_telegram_message(
                int(chat_id),
                "That PartyHat link is invalid or expired. Return to PartyHat and generate a new link.",
            )
            return {"handled": True, "linked": False, "reason": "invalid_token"}

        link = await upsert_telegram_user_link(
            session,
            user_id=link_token.user_id,
            chat_id=int(chat_id),
            chat_type=chat_type,
            telegram_user_id=(
                int(user_row["id"])
                if user_row.get("id") is not None
                else None
            ),
            chat_username=str(chat.get("username") or "") or None,
            first_name=str(user_row.get("first_name") or "") or None,
            enabled=True,
        )

    await send_telegram_message(
        int(chat_id),
        f"{get_telegram_bot_display_name()} is connected. You'll receive PartyHat deployment updates here.",
    )
    return {
        "handled": True,
        "linked": True,
        "user_id": str(link.user_id),
    }


async def dispatch_next_notification_once() -> bool:
    if not get_telegram_bot_token() or not os.getenv("DATABASE_URL"):
        return False

    async with async_session_factory() as session:
        row = await claim_next_notification_outbox(
            session,
            channel=TELEGRAM_CHANNEL,
            stale_after_seconds=TELEGRAM_DISPATCH_STALE_AFTER_SECONDS,
        )
    if row is None:
        return False

    payload = dict(row.payload_json or {})
    async with async_session_factory() as session:
        link = await get_telegram_user_link(session, row.user_id)

    if link is None or not link.enabled:
        async with async_session_factory() as session:
            await mark_notification_outbox_failed(
                session,
                row.id,
                last_error="Telegram link is unavailable or disabled.",
            )
        return True

    try:
        await send_telegram_message(int(link.chat_id), format_terminal_notification_message(payload))
    except TelegramApiError as exc:
        async with async_session_factory() as session:
            if exc.disable_link:
                await set_telegram_user_link_enabled(
                    session,
                    row.user_id,
                    enabled=False,
                )
            if exc.permanent:
                await mark_notification_outbox_failed(
                    session,
                    row.id,
                    last_error=str(exc),
                )
            else:
                await mark_notification_outbox_pending(
                    session,
                    row.id,
                    last_error=str(exc),
                )
        if exc.retry_after:
            await asyncio.sleep(max(1, exc.retry_after))
        return True
    except Exception as exc:
        async with async_session_factory() as session:
            await mark_notification_outbox_pending(
                session,
                row.id,
                last_error=f"Unexpected Telegram delivery error: {exc}",
            )
        return True

    async with async_session_factory() as session:
        await mark_notification_outbox_sent(session, row.id)
    return True


async def _notification_dispatcher_loop() -> None:
    while True:
        try:
            handled = await dispatch_next_notification_once()
            if not handled:
                await asyncio.sleep(TELEGRAM_DISPATCH_IDLE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Telegram] Dispatcher error: {exc}")
            await asyncio.sleep(TELEGRAM_DISPATCH_ERROR_SECONDS)


async def start_notification_dispatcher() -> None:
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return
    if not os.getenv("DATABASE_URL"):
        return
    _dispatcher_task = asyncio.create_task(_notification_dispatcher_loop())


async def stop_notification_dispatcher() -> None:
    global _dispatcher_task
    if _dispatcher_task is None:
        return
    _dispatcher_task.cancel()
    try:
        await _dispatcher_task
    except asyncio.CancelledError:
        pass
    _dispatcher_task = None
