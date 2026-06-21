"""
Discord Notifier — entrypoint (v0.3).

Registers as a GatewayAdapter in the Messaging Gateway.  Supports multiple
independent instances for different Discord channels/servers, configured via:

    LYNDRIX_GATEWAY_PROVIDERS=discord:ops,discord:alerts
    LYNDRIX_GATEWAY_DISCORD_OPS_WEBHOOK_URL=https://discord.com/api/webhooks/…
    LYNDRIX_GATEWAY_DISCORD_ALERTS_WEBHOOK_URL=https://discord.com/api/webhooks/…

When LYNDRIX_GATEWAY_PROVIDERS is unset, a single default instance is
registered and reads its webhook URL from Vault (backward compat with v0.1).

Backward compatibility
----------------------
The ``notification:outbound`` subscription is kept so that code that has not
yet been migrated to ``messaging:outbound`` continues to deliver messages to
the first registered Discord instance.  It will be removed in v0.4.
"""
from core.api import ModuleManifest

from .app.controller.adapter import DiscordGatewayAdapter
from .app.controller.router import build_interaction_router
from .app.controller.service import DiscordNotifierService
from .app.ui.settings import render_settings_ui as modular_settings_ui

manifest = ModuleManifest(
    id="lyndrix.plugin.discord",
    name="Discord Notifier",
    version="0.0.9",
    description=(
        "Two-way Discord integration via the Lyndrix Messaging Gateway. "
        "Supports multiple channel instances via env vars."
    ),
    author="Lyndrix",
    icon="notifications_active",
    type="PLUGIN",
    min_core_version="0.1.1",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier",
    permissions={
        "subscribe": [
            "system:boot_complete",
            # Deprecated — remove in v0.4 once all callers use messaging:outbound
            "notification:outbound",
        ],
        "emit": [],
    },
)

# Module-level state — populated in setup()
_adapters: list[DiscordGatewayAdapter] = []
_service:  DiscordNotifierService | None = None


def render_settings_ui(ctx):
    modular_settings_ui(ctx, _service)


def setup(ctx):
    global _adapters, _service
    _adapters = []

    from config import settings

    # Build adapter list from LYNDRIX_GATEWAY_PROVIDERS, or fall back to a
    # single default instance (Vault-based, backward compat).
    discord_specs = [s for s in settings.gateway_provider_specs if s["type"] == "discord"]

    if discord_specs:
        for spec in discord_specs:
            adapter = DiscordGatewayAdapter(ctx, instance_id=spec["instance_id"])
            ctx.register_gateway_adapter(adapter)
            ctx.register_routes(build_interaction_router(adapter))
            _adapters.append(adapter)
            ctx.log.info(
                "Discord Notifier: registered instance '%s' (provider_id=%s).",
                spec["instance_id"], adapter.provider_id,
            )
    else:
        # Default single-instance mode — reads webhook_url from Vault
        adapter = DiscordGatewayAdapter(ctx, instance_id="default")
        ctx.register_gateway_adapter(adapter)
        ctx.register_routes(build_interaction_router(adapter))
        _adapters.append(adapter)
        ctx.log.info("Discord Notifier: registered default instance.")

    _service = DiscordNotifierService(ctx)

    @ctx.subscribe("system:boot_complete")
    async def _on_boot(payload):
        ctx.log.info("Discord Notifier: system boot complete, %d adapter(s) ready.", len(_adapters))

    # Deprecated: route notification:outbound to the first adapter.
    @ctx.subscribe("notification:outbound")
    async def _on_legacy_notification(payload):
        if _adapters:
            ctx.log.debug("Discord Notifier: bridging legacy notification:outbound (deprecated path).")
            await _adapters[0].legacy_send_notification(payload)
            _service.state["notifications_sent"] = (
                _service.state.get("notifications_sent", 0) + 1
            )

    ctx.log.info(
        "Discord Notifier: setup complete (%d adapter(s) registered).",
        len(_adapters),
    )


def teardown(ctx):
    global _adapters, _service
    from core.components.messaging.gateway import messaging_gateway
    for adapter in _adapters:
        messaging_gateway.unregister(adapter.provider_id)
    _adapters = []
    _service  = None
