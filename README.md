# Lyndrix Discord Notifier

**Plugin ID:** `lyndrix.plugin.discord`

A [Lyndrix](https://github.com/lyndrix-platform/lyndrix-core) plugin providing **two-way
Discord integration** (bot API + webhook), implemented as a
[Messaging Gateway](https://docs.lyndrix.eu/core-components/messaging/) `GatewayAdapter`.
It supports **multiple independent instances** for different Discord channels/servers.

## Features

- **Dual-mode delivery** — Discord bot API and/or incoming webhooks.
- **Multiple instances** — register several Discord targets (e.g. `ops`, `alerts`) at once.
- **Gateway adapter** — outbound messages routed via `messaging:outbound`; supports interactive
  actions/replies through the gateway's correlation mechanism.

## Configuration

Instances are declared via the gateway provider env vars, with a per-instance webhook URL:

```bash
LYNDRIX_GATEWAY_PROVIDERS=discord:ops,discord:alerts
LYNDRIX_GATEWAY_DISCORD_OPS_WEBHOOK_URL=https://discord.com/api/webhooks/…
LYNDRIX_GATEWAY_DISCORD_ALERTS_WEBHOOK_URL=https://discord.com/api/webhooks/…
```

When `LYNDRIX_GATEWAY_PROVIDERS` is unset, a single default instance is registered and reads
its webhook URL from **Vault** (set via the settings UI / `ctx.set_secret("webhook_url", …)`,
read with `ctx.get_secret("webhook_url")`, scoped to this plugin's namespace).

## Installation

Install from the Lyndrix **Plugin Manager**, or via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier
```

## Project structure

```
entrypoint.py          # manifest + lifecycle wiring only
app/controller/        # DiscordGatewayAdapter, webhook sender, DiscordNotifierService
app/ui/settings.py     # NiceGUI settings UI
tests/                 # service smoke tests
```

## Documentation

- Plugin docs: https://discord-notifier.docs.lyndrix.eu
- Platform docs: https://docs.lyndrix.eu — see
  [Messaging Gateway](https://docs.lyndrix.eu/core-components/messaging/).

## License

Apache-2.0 — see [LICENSE](LICENSE).
