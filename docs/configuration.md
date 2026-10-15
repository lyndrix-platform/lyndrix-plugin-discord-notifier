# Configuration

> **TODO:** Document the Discord Notifier configuration surface — environment variables,
> Vault-backed secrets, and any operator-provided catalog/config files.

Secrets are stored in the plugin's own Vault namespace via `ctx.get_secret` /
`ctx.set_secret`; see [Plugin development → state and secrets](https://docs.lyndrix.eu/plugins/).

## Cluster-Betrieb (ab 1.2.0)

- **Identische Umgebungsvariablen:** Jede Core-Instanz muss dieselben
  `LYNDRIX_GATEWAY_*`-Variablen tragen (`LYNDRIX_GATEWAY_PROVIDERS` und die
  `LYNDRIX_GATEWAY_DISCORD_<INSTANZ>_*`-Werte). Sonst registrieren die Instanzen
  unterschiedliche Adapter und Benachrichtigungen gehen je nach Instanz verloren.
- **Nachrichten-Zuordnung:** Die Zuordnung Benachrichtigung → Discord-Nachricht
  liegt in der gemeinsamen Datenbanktabelle `discord_messages`. Jede Instanz
  bearbeitet daher die ursprüngliche Nachricht, statt eine neue zu senden.
- **Aufbewahrung:** Einstellung `message_retention_days` (Standard 30). Ältere
  Zuordnungen löscht die Leader-Instanz höchstens stündlich.
- **Rate-Limits:** Das 429-Back-off gilt pro Instanz und wird nicht über den
  Cluster koordiniert.
