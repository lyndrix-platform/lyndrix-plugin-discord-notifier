# Configuration

> **TODO:** Document the Discord Notifier configuration surface — environment variables,
> Vault-backed secrets, and any operator-provided catalog/config files.

Secrets are stored in the plugin's own Vault namespace via `ctx.get_secret` /
`ctx.set_secret`; see [Plugin development → state and secrets](https://docs.lyndrix.eu/plugins/).

## Interactions-Endpunkt (ab 1.3.0)

- **Einstellung `interactions_public_key`:** Ed25519-Public-Key der Discord-Anwendung
  (Developer Portal, General Information, Public Key; 64 Hexzeichen). Kein Geheimnis.
  Ist das Feld leer, dient das Vault-Secret `discord_public_key` als Fallback. Fehlt der
  Schlüssel oder ist er kein gültiger 64-Zeichen-Hexwert, lehnt der öffentliche Endpunkt
  `/interactions` jede Anfrage mit HTTP 503 ab.
- **Schutzmaßnahmen:** Der Request-Body ist auf 64 KiB begrenzt (HTTP 413). Der Schlüssel
  wird höchstens alle 60 s neu gelesen; Speichern der Plugin-Einstellungen verwirft den
  Cache sofort.
- **Button-Klicks:** Werden mit einer ephemeren Antwort "nicht verknüpft" abgelehnt, solange
  kein Discord-Benutzer einem Lyndrix-Benutzer zugeordnet werden kann (derzeit immer);
  nichts erreicht den Bus. Siehe [Geplante Funktionen](planned-features.md).

## Cluster-Betrieb (ab 1.2.0)

- **Identische Umgebungsvariablen:** Jede Core-Instanz muss dieselben
  `LYNDRIX_GATEWAY_*`-Variablen tragen (`LYNDRIX_GATEWAY_PROVIDERS` und die
  `LYNDRIX_GATEWAY_DISCORD_<INSTANZ>_*`-Werte). Sonst registrieren die Instanzen
  unterschiedliche Adapter und Benachrichtigungen gehen je nach Instanz verloren.
- **Nachrichten-Zuordnung:** Die Zuordnung Benachrichtigung → Discord-Nachricht
  liegt in der gemeinsamen Datenbanktabelle `discord_messages`. Jede Instanz
  bearbeitet daher die ursprüngliche Nachricht, statt eine neue zu senden.
- **Aufbewahrung:** Einstellung `message_retention_days` (Standard 30). Ältere
  Zuordnungen löscht stündlich genau eine Instanz unter dem Lease `discord:prune`
  (ab 1.3.0; unabhängig davon, welche Instanz gerade sendet).
- **Rate-Limits:** Das 429-Back-off gilt pro Instanz und wird nicht über den
  Cluster koordiniert.
