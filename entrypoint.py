"""
Discord Notifier — entrypoint.

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

from .app.api.router import build_interaction_router
from .app.logic.adapter import DiscordGatewayAdapter
from .app.logic.service import DiscordNotifierService
from .app.ui.nicegui.settings import render_settings_ui as modular_settings_ui

manifest = ModuleManifest(
    id="lyndrix.plugin.discord",
    name="Discord Notifier",
    version="0.1.0",
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
    # The legacy NiceGUI settings page edits the default instance's config.
    modular_settings_ui(ctx, _service, _adapters[0] if _adapters else None)


def setup(ctx):
    global _adapters, _service
    _adapters = []

    # TODO(agent): `settings.gateway_provider_specs` has no `core.api` surface;
    # importing from `config` couples the plugin to core internals. Expose this
    # via core.api (or ctx) so this import can be dropped.
    from config import settings

    # Build adapter list from LYNDRIX_GATEWAY_PROVIDERS, or fall back to a
    # single default instance (Vault-based, backward compat).
    discord_specs = [s for s in settings.gateway_provider_specs if s["type"] == "discord"]

    if discord_specs:
        for spec in discord_specs:
            adapter = DiscordGatewayAdapter(ctx, instance_id=spec["instance_id"])
            ctx.register_gateway_adapter(adapter)
            _adapters.append(adapter)
            ctx.log.info(
                "Discord Notifier: registered instance '%s' (provider_id=%s).",
                spec["instance_id"], adapter.provider_id,
            )
    else:
        # Default single-instance mode — reads webhook_url from Vault
        adapter = DiscordGatewayAdapter(ctx, instance_id="default")
        ctx.register_gateway_adapter(adapter)
        _adapters.append(adapter)
        ctx.log.info("Discord Notifier: registered default instance.")

    # Register the interaction endpoint exactly once. Discord allows only one
    # interactions endpoint URL per application, and correlation resolution is
    # global, so the first adapter is sufficient to parse inbound payloads.
    # TODO(agent): derive the instance from a path/query segment if true
    # per-instance inbound routing is ever required.
    if _adapters:
        ctx.register_routes(build_interaction_router(_adapters[0]))

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
    from core.api import messaging_gateway
    for adapter in _adapters:
        messaging_gateway.unregister(adapter.provider_id)
    _adapters = []
    _service  = None
