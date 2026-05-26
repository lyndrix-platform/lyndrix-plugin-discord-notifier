import logging

import pytest

from app.controller.service import DiscordNotifierService


class StubCtx:
    def __init__(self):
        self.log = logging.getLogger("discord-notifier-test")

    def get_secret(self, key):
        return None

    def set_secret(self, key, value):
        return True

    def subscribe(self, event):
        def decorator(func):
            return func

        return decorator

    def create_task(self, coro):
        return coro


@pytest.mark.asyncio
async def test_service_smoke_no_webhook_does_not_increment():
    service = DiscordNotifierService(StubCtx())
    assert service.state["notifications_sent"] == 0

    await service.handle_change(
        {"entity_type": "System", "action": "UPDATE", "payload": {"x": 1}}
    )
    await service.handle_boot_complete({})
    await service.handle_notification({"title": "T", "message": "M"})

    assert service.notifications_sent == 0
