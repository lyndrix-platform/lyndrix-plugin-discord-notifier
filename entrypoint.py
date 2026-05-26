from core.api import ModuleManifest

from .app.controller.service import DiscordNotifierService
from .app.ui.settings import render_settings_ui as modular_settings_ui

manifest = ModuleManifest(
    id="lyndrix.plugin.discord",
    name="Discord Notifier",
    version="0.1.0",
    description="Sendet System-Events und Status-Updates an Discord.",
    author="Lyndrix",
    icon="notifications_active",
    type="PLUGIN",
    min_core_version="0.0.6",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier",
    permissions={
        "subscribe": [
            "change_requested",
            "system:boot_complete",
            "notification:outbound",
        ],
        "emit": [],
    },
)

_service: DiscordNotifierService | None = None


def render_settings_ui(ctx):
    modular_settings_ui(ctx, _service)


def setup(ctx):
    global _service
    _service = DiscordNotifierService(ctx)

    @ctx.subscribe("change_requested")
    async def _on_change(data):
        await _service.handle_change(data)

    @ctx.subscribe("system:boot_complete")
    async def _on_boot(payload):
        await _service.handle_boot_complete(payload)

    @ctx.subscribe("notification:outbound")
    async def _on_notification(payload):
        await _service.handle_notification(payload)

    ctx.log.info("Discord Notifier: connected to event bus.")


def teardown(ctx):
    global _service
    _service = None
