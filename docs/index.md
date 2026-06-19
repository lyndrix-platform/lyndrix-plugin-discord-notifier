# Lyndrix Discord Notifier

Two-way Discord integration (bot API + webhook) implemented as a Lyndrix Messaging Gateway adapter; supports multiple channel instances.

- **Repository:** [https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier](https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier)
- **Platform docs:** [Lyndrix Core](https://docs.lyndrix.eu) · [Plugin ecosystem](https://docs.lyndrix.eu/ecosystem/)

This plugin builds on the Lyndrix Core [messaging](https://docs.lyndrix.eu/core-components/messaging/) extension point.

## Features

- Dual-mode delivery: Discord bot API and webhooks
- Multiple independent Discord instances via env vars
- Messaging Gateway adapter with interactive actions

## Installation

Install **Discord Notifier** from the Lyndrix **Plugin Manager**, or declare it for
reconciliation on boot via `LYNDRIX_PLUGINS_DESIRED`:

```text
https://github.com/lyndrix-platform/lyndrix-plugin-discord-notifier
```

See the [Plugin Development Guide](https://docs.lyndrix.eu/plugins/) for the plugin model and
lifecycle, and [Usage](usage.md) / [Configuration](configuration.md) for details.
