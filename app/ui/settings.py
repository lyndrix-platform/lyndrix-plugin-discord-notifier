from nicegui import ui
from ui.theme import UIStyles


def render_settings_ui(ctx, service) -> None:
    current_state = {"enabled": True, "bot_name": "Lyndrix Event Broker"}
    vault_state = {
        "bot_token":   ctx.get_secret("bot_token")   or "",
        "channel_id":  ctx.get_secret("channel_id")  or "",
        "webhook_url": ctx.get_secret("webhook_url") or "",
    }

    def _save(key: str, label: str):
        value = vault_state[key]
        if not value:
            ui.notify(f"{label} ist leer.", type="warning")
            return
        success = ctx.set_secret(key, value)
        if success:
            ui.notify(f"{label} sicher im Vault gespeichert!", type="positive")
        else:
            ui.notify(f"Fehler beim Speichern von {label} im Vault", type="negative")

    def save_bot_api():
        _save("bot_token",  "Bot Token")
        _save("channel_id", "Channel ID")

    def save_webhook():
        _save("webhook_url", "Webhook URL")

    with ui.column().classes("w-full gap-4 pt-2"):
        with ui.card().classes(f"{UIStyles.CARD_GLASS} w-full").style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(
                "h-1 w-full bg-gradient-to-r from-indigo-400 via-violet-400 to-purple-400"
            )
            with ui.column().classes("w-full flex-grow p-5 gap-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("notifications_active", size="18px").classes(
                        "text-indigo-400"
                    )
                    ui.label("Benachrichtigungen").classes(
                        "text-sm font-bold uppercase tracking-widest text-slate-300"
                    )
                ui.label("Konfiguration für System-Benachrichtigungen.").classes(
                    UIStyles.TEXT_MUTED
                )
                with ui.row().classes("w-full items-center gap-4"):
                    ui.switch("Benachrichtigungen aktivieren").bind_value(
                        current_state, "enabled"
                    ).props("color=primary")
                    ui.input("Bot Name").bind_value(current_state, "bot_name").classes(
                        "flex-grow"
                    ).props("outlined dense")

        # --- Bot API (recommended) ---
        with ui.card().classes(f"{UIStyles.CARD_GLASS} w-full").style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(
                "h-1 w-full bg-gradient-to-r from-emerald-400 via-teal-400 to-cyan-400"
            )
            with ui.column().classes("w-full flex-grow p-5 gap-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("smart_toy", size="18px").classes("text-emerald-400")
                    ui.label("Bot API (empfohlen)").classes(
                        "text-sm font-bold uppercase tracking-widest text-slate-300"
                    )
                ui.label(
                    "Aktiviert In-Place-Edits von Nachrichten "
                    "(z. B. „Pipeline läuft…“ → „Erfolgreich“ in derselben Nachricht)."
                ).classes(UIStyles.TEXT_MUTED)
                ui.input("Bot Token (Vault)").bind_value(
                    vault_state, "bot_token"
                ).classes("w-full").props("outlined dense type=password")
                ui.input("Channel ID (Vault)").bind_value(
                    vault_state, "channel_id"
                ).classes("w-full").props("outlined dense")
                with ui.row().classes("w-full justify-end mt-2"):
                    ui.button(
                        "Bot API speichern",
                        on_click=save_bot_api,
                        icon="save",
                        color="primary",
                    ).props("unelevated rounded size=sm")

        # --- Webhook (fallback) ---
        with ui.card().classes(f"{UIStyles.CARD_GLASS} w-full").style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(
                "h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-teal-400"
            )
            with ui.column().classes("w-full flex-grow p-5 gap-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("webhook", size="18px").classes("text-sky-400")
                    ui.label("Webhook (Fallback)").classes(
                        "text-sm font-bold uppercase tracking-widest text-slate-300"
                    )
                ui.label(
                    "Wird nur verwendet, wenn keine Bot API konfiguriert ist. "
                    "Webhooks unterstützen keine Nachrichtenbearbeitung."
                ).classes(UIStyles.TEXT_MUTED)
                ui.input("Discord Webhook URL (Vault)").bind_value(
                    vault_state, "webhook_url"
                ).classes("w-full").props("outlined dense type=password")
                with ui.row().classes("w-full justify-end mt-2"):
                    ui.button(
                        "Webhook speichern",
                        on_click=save_webhook,
                        icon="save",
                        color="primary",
                    ).props("unelevated rounded size=sm")

        if service is not None:
            ui.label(
                f"Versendete Benachrichtigungen: {service.notifications_sent}"
            ).classes(UIStyles.TEXT_MUTED)
