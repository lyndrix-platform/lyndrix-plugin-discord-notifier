import logging

import pytest

from app.logic.adapter import DiscordGatewayAdapter, _truncate
from app.logic.service import DiscordNotifierService


class StubCtx:
    """Minimal ModuleContext stub for unit tests (no running core)."""

    def __init__(self, secrets=None):
        self.log = logging.getLogger("discord-notifier-test")
        self._secrets = secrets or {}

    def get_secret(self, key):
        return self._secrets.get(key)

    def set_secret(self, key, value):
        self._secrets[key] = value
        return True

    def subscribe(self, event):
        def decorator(func):
            return func

        return decorator

    def create_task(self, coro):
        return coro


def test_service_starts_with_zero_sent():
    service = DiscordNotifierService(StubCtx())
    assert service.state["notifications_sent"] == 0
    assert service.notifications_sent == 0


def test_truncate_appends_ellipsis_when_over_limit():
    assert _truncate("hello", 10) == "hello"
    assert _truncate("hello world", 5) == "hell…"
    assert _truncate("", 5) == ""


def test_provider_id_is_instance_scoped():
    default = DiscordGatewayAdapter(StubCtx(), instance_id="default")
    ops = DiscordGatewayAdapter(StubCtx(), instance_id="ops")
    assert default.provider_id == "discord"
    assert ops.provider_id == "discord:ops"


def test_vault_key_is_prefixed_for_non_default_instances():
    default = DiscordGatewayAdapter(StubCtx(), instance_id="default")
    ops = DiscordGatewayAdapter(StubCtx(), instance_id="ops")
    assert default.vault_key("webhook_url") == "webhook_url"
    assert ops.vault_key("webhook_url") == "ops_webhook_url"


def test_save_config_rejects_invalid_webhook_url():
    adapter = DiscordGatewayAdapter(StubCtx(), instance_id="default")
    assert adapter.save_config("webhook_url", "https://evil.example/hook") is False
    assert (
        adapter.save_config(
            "webhook_url", "https://discord.com/api/webhooks/123/abc"
        )
        is True
    )


@pytest.mark.asyncio
async def test_handle_incoming_routes_component_with_correlation():
    adapter = DiscordGatewayAdapter(StubCtx(), instance_id="ops")
    corr = "123e4567-e89b-12d3-a456-426614174000"
    raw = {
        "type": 3,
        "data": {"custom_id": f"approve:{corr}"},
        "member": {"user": {"id": "42"}},
    }
    inbound = await adapter.handle_incoming(raw)
    assert inbound is not None
    assert inbound.provider_id == "discord:ops"
    assert inbound.action_id == "approve"
    assert str(inbound.correlation_id) == corr
    assert inbound.user_id == "42"


@pytest.mark.asyncio
async def test_handle_incoming_ignores_ping():
    adapter = DiscordGatewayAdapter(StubCtx())
    assert await adapter.handle_incoming({"type": 1}) is None
