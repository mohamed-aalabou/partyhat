import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api
from agents import telegram_service


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_telegram_link_rejects_default_user():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            api.telegram_link(
                ctx=api.RequestContext(project_id="default", user_id="default"),
                session=object(),
            )
        )

    assert exc.value.status_code == 400


def test_telegram_link_returns_deep_link(monkeypatch):
    user_id = uuid.uuid4()
    captured = {}

    async def fake_get_user_by_id(session, requested_user_id):
        assert requested_user_id == user_id
        return SimpleNamespace(id=user_id)

    async def fake_get_telegram_user_link(session, requested_user_id):
        assert requested_user_id == user_id
        return None

    async def fake_delete_unused_tokens(session, requested_user_id):
        assert requested_user_id == user_id
        captured["deleted"] = True
        return 1

    async def fake_create_link_token(session, *, user_id, token_hash, expires_at):
        captured["token_hash"] = token_hash
        captured["expires_at"] = expires_at
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(api, "db_get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(api, "get_telegram_user_link", fake_get_telegram_user_link)
    monkeypatch.setattr(api, "delete_unused_telegram_link_tokens", fake_delete_unused_tokens)
    monkeypatch.setattr(api, "create_telegram_link_token", fake_create_link_token)
    monkeypatch.setattr(api, "generate_telegram_connect_token", lambda: "token-123")
    monkeypatch.setattr(api, "hash_telegram_connect_token", lambda token: f"hash:{token}")
    monkeypatch.setattr(api, "get_telegram_connect_token_ttl_seconds", lambda: 900)
    monkeypatch.setattr(
        api,
        "build_telegram_deep_link",
        lambda token: f"https://t.me/zap_thebot?start={token}",
    )
    monkeypatch.setattr(api, "get_telegram_bot_username", lambda: "zap_thebot")
    monkeypatch.setattr(api, "get_telegram_bot_display_name", lambda: "Zap from PartyHat")

    result = asyncio.run(
        api.telegram_link(
            ctx=api.RequestContext(project_id="default", user_id=str(user_id)),
            session=object(),
        )
    )

    assert result.linked is False
    assert result.bot_username == "zap_thebot"
    assert result.bot_display_name == "Zap from PartyHat"
    assert result.deep_link_url == "https://t.me/zap_thebot?start=token-123"
    assert captured["deleted"] is True
    assert captured["token_hash"] == "hash:token-123"
    assert isinstance(datetime.fromisoformat(result.expires_at), datetime)


def test_telegram_status_returns_link_state(monkeypatch):
    user_id = uuid.uuid4()

    async def fake_get_user_by_id(session, requested_user_id):
        assert requested_user_id == user_id
        return SimpleNamespace(id=user_id)

    async def fake_get_telegram_user_link(session, requested_user_id):
        assert requested_user_id == user_id
        return SimpleNamespace(
            enabled=True,
            chat_username="mohamed",
            linked_at=datetime(2026, 4, 10, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(api, "db_get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(api, "get_telegram_user_link", fake_get_telegram_user_link)
    monkeypatch.setattr(api, "get_telegram_bot_username", lambda: "zap_thebot")
    monkeypatch.setattr(api, "get_telegram_bot_display_name", lambda: "Zap from PartyHat")

    result = asyncio.run(
        api.telegram_status(
            ctx=api.RequestContext(project_id="default", user_id=str(user_id)),
            session=object(),
        )
    )

    assert result.linked is True
    assert result.enabled is True
    assert result.chat_username == "mohamed"
    assert result.bot_username == "zap_thebot"


def test_telegram_unlink_removes_link(monkeypatch):
    user_id = uuid.uuid4()
    captured = {}

    async def fake_get_user_by_id(session, requested_user_id):
        assert requested_user_id == user_id
        return SimpleNamespace(id=user_id)

    async def fake_delete_unused_tokens(session, requested_user_id):
        captured["tokens_deleted"] = requested_user_id
        return 1

    async def fake_delete_link(session, requested_user_id):
        captured["link_deleted"] = requested_user_id
        return True

    monkeypatch.setattr(api, "db_get_user_by_id", fake_get_user_by_id)
    monkeypatch.setattr(api, "delete_unused_telegram_link_tokens", fake_delete_unused_tokens)
    monkeypatch.setattr(api, "delete_telegram_user_link", fake_delete_link)

    result = asyncio.run(
        api.telegram_unlink(
            ctx=api.RequestContext(project_id="default", user_id=str(user_id)),
            session=object(),
        )
    )

    assert result.success is True
    assert captured["tokens_deleted"] == user_id
    assert captured["link_deleted"] == user_id


def test_telegram_webhook_rejects_wrong_secret():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            api.telegram_webhook(
                update={},
                x_telegram_bot_api_secret_token="wrong",
                session=object(),
            )
        )

    assert exc.value.status_code == 403


def test_telegram_webhook_accepts_valid_secret(monkeypatch):
    async def fake_handle_update(update):
        assert update == {"message": {"text": "/start token"}}
        return {"handled": True, "linked": True}

    monkeypatch.setattr(api, "get_telegram_webhook_secret", lambda: "secret")
    monkeypatch.setattr(api, "handle_telegram_webhook_update", fake_handle_update)

    result = asyncio.run(
        api.telegram_webhook(
            update={"message": {"text": "/start token"}},
            x_telegram_bot_api_secret_token="secret",
            session=object(),
        )
    )

    assert result == {"ok": True, "handled": True, "linked": True}


def test_build_terminal_notification_message_includes_contracts_and_snowtrace(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_DISPLAY_NAME", "Zap from PartyHat")
    monkeypatch.setenv("PARTYHAT_APP_BASE_URL", "https://partyhat.app")

    payload = telegram_service.build_terminal_notification_payload(
        pipeline_run_id="run-123",
        project_id="project-123",
        project_name="PartyToken",
        terminal_status="completed",
        deployment_target={
            "network": "avalanche_fuji",
            "name": "Avalanche Fuji",
            "chain_id": 43113,
        },
        terminal_deployment=SimpleNamespace(
            tx_hash="0xabc",
            deployed_contracts=[
                {
                    "contract_name": "PartyToken",
                    "deployed_address": "0x1111111111111111111111111111111111111111",
                },
                {
                    "contract_name": "PartyTreasury",
                    "deployed_address": "0x2222222222222222222222222222222222222222",
                },
            ],
        ),
    )
    message = telegram_service.format_terminal_notification_message(payload)

    assert payload["tx_url"] == "https://testnet.snowtrace.io/tx/0xabc"
    assert "PartyToken" in message
    assert "PartyTreasury" in message
    assert "https://testnet.snowtrace.io/address/0x1111111111111111111111111111111111111111" in message
    assert "Open in PartyHat: https://partyhat.app?project_id=project-123&pipeline_run_id=run-123" in message


def test_handle_telegram_webhook_update_links_private_chat(monkeypatch):
    user_id = uuid.uuid4()
    sent_messages = []

    async def fake_consume_token(session, token_hash):
        assert token_hash == telegram_service.hash_telegram_connect_token("connect-token")
        return SimpleNamespace(user_id=user_id)

    async def fake_upsert_link(
        session,
        *,
        user_id,
        chat_id,
        chat_type,
        telegram_user_id,
        chat_username,
        first_name,
        enabled,
    ):
        return SimpleNamespace(user_id=user_id, chat_id=chat_id)

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(telegram_service, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(telegram_service, "consume_telegram_link_token", fake_consume_token)
    monkeypatch.setattr(telegram_service, "upsert_telegram_user_link", fake_upsert_link)
    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)
    monkeypatch.setattr(telegram_service, "get_telegram_bot_display_name", lambda: "Zap from PartyHat")

    result = asyncio.run(
        telegram_service.handle_telegram_webhook_update(
            {
                "message": {
                    "text": "/start connect-token",
                    "chat": {"id": 42, "type": "private", "username": "mohamed"},
                    "from": {"id": 99, "first_name": "Mohamed"},
                }
            }
        )
    )

    assert result["handled"] is True
    assert result["linked"] is True
    assert sent_messages[0][0] == 42
    assert "connected" in sent_messages[0][1].lower()


def test_handle_telegram_webhook_update_rejects_missing_token(monkeypatch):
    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)

    result = asyncio.run(
        telegram_service.handle_telegram_webhook_update(
            {
                "message": {
                    "text": "/start",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
    )

    assert result == {"handled": True, "linked": False, "reason": "missing_token"}
    assert sent_messages[0][0] == 42


def test_handle_telegram_webhook_update_rejects_invalid_token(monkeypatch):
    sent_messages = []

    async def fake_consume_token(session, token_hash):
        return None

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))
        return {"ok": True}

    monkeypatch.setattr(telegram_service, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(telegram_service, "consume_telegram_link_token", fake_consume_token)
    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)

    result = asyncio.run(
        telegram_service.handle_telegram_webhook_update(
            {
                "message": {
                    "text": "/start invalid-token",
                    "chat": {"id": 42, "type": "private"},
                }
            }
        )
    )

    assert result == {"handled": True, "linked": False, "reason": "invalid_token"}
    assert "invalid or expired" in sent_messages[0][1].lower()


def test_dispatch_next_notification_once_marks_sent(monkeypatch):
    notification_id = uuid.uuid4()
    user_id = uuid.uuid4()
    captured = {}

    async def fake_claim(session, *, channel, stale_after_seconds):
        assert channel == telegram_service.TELEGRAM_CHANNEL
        return SimpleNamespace(
            id=notification_id,
            user_id=user_id,
            payload_json={
                "project_name": "PartyToken",
                "status_label": "Deployed",
                "network": {"name": "Avalanche Fuji"},
                "contracts": [],
            },
        )

    async def fake_get_link(session, requested_user_id):
        assert requested_user_id == user_id
        return SimpleNamespace(chat_id=123, enabled=True)

    async def fake_send_message(chat_id, text):
        captured["chat_id"] = chat_id
        captured["text"] = text
        return {"ok": True}

    async def fake_mark_sent(session, requested_notification_id):
        captured["sent_id"] = requested_notification_id
        return SimpleNamespace(id=requested_notification_id)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram_service, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(telegram_service, "claim_next_notification_outbox", fake_claim)
    monkeypatch.setattr(telegram_service, "get_telegram_user_link", fake_get_link)
    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)
    monkeypatch.setattr(telegram_service, "mark_notification_outbox_sent", fake_mark_sent)
    monkeypatch.setattr(telegram_service, "get_telegram_bot_display_name", lambda: "Zap from PartyHat")

    handled = asyncio.run(telegram_service.dispatch_next_notification_once())

    assert handled is True
    assert captured["chat_id"] == 123
    assert captured["sent_id"] == notification_id


def test_dispatch_next_notification_once_retries_transient_failures(monkeypatch):
    notification_id = uuid.uuid4()
    user_id = uuid.uuid4()
    captured = {}

    async def fake_claim(session, *, channel, stale_after_seconds):
        return SimpleNamespace(
            id=notification_id,
            user_id=user_id,
            payload_json={"project_name": "PartyToken", "status_label": "Deployed"},
        )

    async def fake_get_link(session, requested_user_id):
        return SimpleNamespace(chat_id=123, enabled=True)

    async def fake_send_message(chat_id, text):
        raise telegram_service.TelegramApiError("rate limited", permanent=False)

    async def fake_mark_pending(session, requested_notification_id, *, last_error=None):
        captured["pending_id"] = requested_notification_id
        captured["last_error"] = last_error
        return SimpleNamespace(id=requested_notification_id)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram_service, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(telegram_service, "claim_next_notification_outbox", fake_claim)
    monkeypatch.setattr(telegram_service, "get_telegram_user_link", fake_get_link)
    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)
    monkeypatch.setattr(telegram_service, "mark_notification_outbox_pending", fake_mark_pending)

    handled = asyncio.run(telegram_service.dispatch_next_notification_once())

    assert handled is True
    assert captured["pending_id"] == notification_id
    assert "rate limited" in captured["last_error"]


def test_dispatch_next_notification_once_disables_blocked_links(monkeypatch):
    notification_id = uuid.uuid4()
    user_id = uuid.uuid4()
    captured = {}

    async def fake_claim(session, *, channel, stale_after_seconds):
        return SimpleNamespace(
            id=notification_id,
            user_id=user_id,
            payload_json={"project_name": "PartyToken", "status_label": "Failed"},
        )

    async def fake_get_link(session, requested_user_id):
        return SimpleNamespace(chat_id=123, enabled=True)

    async def fake_send_message(chat_id, text):
        raise telegram_service.TelegramApiError(
            "bot was blocked by the user",
            permanent=True,
            disable_link=True,
        )

    async def fake_set_enabled(session, requested_user_id, *, enabled):
        captured["disabled_user"] = requested_user_id
        captured["enabled"] = enabled
        return SimpleNamespace(user_id=requested_user_id, enabled=enabled)

    async def fake_mark_failed(session, requested_notification_id, *, last_error=None):
        captured["failed_id"] = requested_notification_id
        captured["last_error"] = last_error
        return SimpleNamespace(id=requested_notification_id)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(telegram_service, "async_session_factory", lambda: DummySessionManager())
    monkeypatch.setattr(telegram_service, "claim_next_notification_outbox", fake_claim)
    monkeypatch.setattr(telegram_service, "get_telegram_user_link", fake_get_link)
    monkeypatch.setattr(telegram_service, "send_telegram_message", fake_send_message)
    monkeypatch.setattr(telegram_service, "set_telegram_user_link_enabled", fake_set_enabled)
    monkeypatch.setattr(telegram_service, "mark_notification_outbox_failed", fake_mark_failed)

    handled = asyncio.run(telegram_service.dispatch_next_notification_once())

    assert handled is True
    assert captured["disabled_user"] == user_id
    assert captured["enabled"] is False
    assert captured["failed_id"] == notification_id
