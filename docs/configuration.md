# Configuration

> **TODO:** Document the Discord Notifier configuration surface — environment variables,
> Vault-backed secrets, and any operator-provided catalog/config files.

Secrets are stored in the plugin's own Vault namespace via `ctx.get_secret` /
`ctx.set_secret`; see [Plugin development → state and secrets](https://docs.lyndrix.eu/plugins/).

## Interactions endpoint (since 1.3.0)

- **Setting `interactions_public_key`:** Ed25519 public key of the Discord application
  (Developer Portal, General Information, Public Key; 64 hex characters). Not a secret.
  If the field is empty, the Vault secret `discord_public_key` serves as the fallback. If the
  key is missing or is not a valid 64-character hex value, the public endpoint
  `/interactions` rejects every request with HTTP 503.
- **Safeguards:** The request body is limited to 64 KiB (HTTP 413). The key is re-read at
  most every 60 s; saving the plugin settings discards the cache immediately.
- **Button clicks:** They are rejected with an ephemeral "not linked" response as long as
  no Discord user can be mapped to a Lyndrix user (currently always); nothing reaches the
  bus. See [Planned features](planned-features.md).

## Cluster operation (since 1.2.0)

- **Identical environment variables:** Every core instance must carry the same
  `LYNDRIX_GATEWAY_*` variables (`LYNDRIX_GATEWAY_PROVIDERS` and the
  `LYNDRIX_GATEWAY_DISCORD_<INSTANCE>_*` values). Otherwise the instances register
  different adapters and notifications are lost depending on the instance.
- **Message mapping:** The mapping notification → Discord message
  lives in the shared database table `discord_messages`. Each instance
  therefore edits the original message instead of sending a new one.
- **Retention:** Setting `message_retention_days` (default 30). Older
  mappings are deleted hourly by exactly one instance under the lease `discord:prune`
  (since 1.3.0; independent of which instance is currently sending).
- **Rate limits:** The 429 back-off applies per instance and is not coordinated
  across the cluster.
