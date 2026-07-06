"""
Discord Notifier — entrypoint.

Registers as a GatewayAdapter in the Messaging Gateway.  Supports multiple
independent instances for different Discord channels/servers, configured via:

    LYNDRIX_GATEWAY_PROVIDERS=discord:ops,discord:alerts
    LYNDRIX_GATEWAY_DISCORD_OPS_WEBHOOK_URL=https://discord.com/api/webhooks/…
    LYNDRIX_GATEWAY_DISCORD_ALERTS_WEBHOOK_URL=https://discord.com/api/webhooks/…

When LYNDRIX_GATEWAY_PROVIDERS is unset, a single default instance is
registered and reads its webhook URL from Vault (backward compat with v0.1).

Configuration is managed exclusively through the generic provider-config
surface (``ProviderConfigField`` → ``GET/PATCH /api/notifications/providers/
discord/config``), rendered by both the notification-router settings card and
the React NotificationsSection — this plugin ships no bespoke settings UI.
"""
import time

from core.api import ModuleManifest, PluginHealthStatus

from .app.api.router import build_interaction_router
from .app.logic.adapter import DiscordGatewayAdapter
from .app.logic.service import DiscordNotifierService

manifest = ModuleManifest(
    id="lyndrix.plugin.discord",
    name="Discord Notifier",
    version="1.1.1",
    description=(
        "Two-way Discord integration via the Lyndrix Messaging Gateway. "
        "Supports multiple channel instances via env vars."
    ),
    author="Lyndrix",
    icon="notifications_active",
    type="PLUGIN",
    min_core_version="0.3.0",
    auto_enable_on_install=False,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier",
    permissions={
        "subscribe": ["system:boot_complete"],
        "emit": [],
    },
    # Identity & Permissions 2.0 roles. Permissions are fully qualified
    # (plugin:<id>:api:*) rather than the bare global api:read/write, so
    # granting these roles only ever affects this plugin.
    roles=[
        {"id": "viewer", "label": "Discord Notifier Viewer",
         "permissions": ["plugin:lyndrix.plugin.discord:api:read"],
         "auto_map_groups": [],
         "description": "Read-only access to Discord notifier status and config."},
        {"id": "operator", "label": "Discord Notifier Operator",
         "permissions": ["plugin:lyndrix.plugin.discord:api:read", "plugin:lyndrix.plugin.discord:api:write"],
         "auto_map_groups": [],
         "description": "Manage Discord notifier configuration and adapters."},
        {"id": "admin", "label": "Discord Notifier Administrator",
         "permissions": ["plugin:lyndrix.plugin.discord:api:read", "plugin:lyndrix.plugin.discord:api:write"],
         "auto_map_groups": ["INT_ADMIN"],
         "description": "Full control over the Discord notifier plugin."},
    ],
)

# Module-level state — populated in setup()
_adapters: list[DiscordGatewayAdapter] = []
_service:  DiscordNotifierService | None = None


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
        # public=True: this is Discord's unauthenticated Interactions webhook
        # endpoint — it must be reachable without the core's require_api_auth
        # dependency. The handler self-authenticates via Ed25519 signature
        # verification (app/api/router.py:118), which fails closed on any
        # missing/invalid signature, so this is not an open route.
        try:
            ctx.register_routes(build_interaction_router(_adapters[0]), public=True)
        except TypeError:
            # Older core (< 1.1.0) has no public-route parameter. The Interactions
            # webhook only works unauthenticated, so mounting it behind
            # require_api_auth would just 401 Discord — skip it instead of aborting
            # setup. Outbound notifications keep working; slash-command/button
            # interactions need core >= 1.1.0.
            ctx.log.warning(
                "Discord Notifier: core lacks public-route support (needs >= 1.1.0); "
                "Interactions webhook NOT mounted. Outbound notifications still work."
            )

    _service = DiscordNotifierService(ctx)

    @ctx.subscribe("system:boot_complete")
    async def _on_boot(payload):
        ctx.log.info("Discord Notifier: system boot complete, %d adapter(s) ready.", len(_adapters))

    ctx.log.info(
        "Discord Notifier: setup complete (%d adapter(s) registered).",
        len(_adapters),
    )


async def health(ctx) -> PluginHealthStatus:
    """Functional health probe.

    A notifier with no usable transport is dead weight, so this checks the
    thing that actually matters: does each registered Discord adapter resolve a
    working transport (Bot API credentials *or* a webhook URL)? It calls each
    live adapter's own ``health()`` (which reads env/Vault off-thread), so the
    result reflects real configuration, not just that ``setup()`` registered an
    adapter object.
    """
    start = time.perf_counter()

    if not _adapters or _service is None:
        return PluginHealthStatus(
            status="error",
            details={"reason": "not_initialized", "adapters": len(_adapters)},
        )

    results: list[tuple[str, bool]] = []
    for adapter in _adapters:
        try:
            ok = bool(await adapter.health())
        except Exception:
            ok = False
        results.append((adapter.provider_id, ok))

    configured = [pid for pid, ok in results if ok]
    unconfigured = [pid for pid, ok in results if not ok]
    latency = round((time.perf_counter() - start) * 1000, 1)
    details = {
        "adapters_total": len(_adapters),
        "adapters_configured": len(configured),
        "unconfigured": unconfigured,
        "notifications_sent": _service.state.get("notifications_sent", 0),
    }

    # No adapter has a usable transport yet. This is an *unconfigured* state
    # (no token/webhook set), not a broken runtime — keep it degraded so it does
    # not trip the post-install verification gate (which reverts only on error).
    if not configured:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "no_transport_configured"},
            latency_ms=latency,
        )
    # Some instances configured, some not → partial delivery only.
    if unconfigured:
        return PluginHealthStatus(
            status="degraded",
            details={**details, "reason": "some_adapters_unconfigured"},
            latency_ms=latency,
        )
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)


def teardown(ctx):
    global _adapters, _service
    from core.api import messaging_gateway
    for adapter in _adapters:
        messaging_gateway.unregister(adapter.provider_id)
    _adapters = []
    _service  = None
