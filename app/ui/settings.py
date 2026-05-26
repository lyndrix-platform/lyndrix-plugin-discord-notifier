from nicegui import ui
from ui.theme import UIStyles


def render_settings_ui(ctx, service) -> None:
    current_state = {"enabled": True, "bot_name": "Lyndrix Event Broker"}
    vault_state = {"webhook_url": ctx.get_secret("webhook_url") or ""}

    def apply_save():
        if vault_state["webhook_url"]:
            success = ctx.set_secret("webhook_url", vault_state["webhook_url"])
            if success:
                ui.notify("Webhook sicher im Vault gespeichert!", type="positive")
            else:
                ui.notify("Fehler beim Speichern im Vault", type="negative")

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

        with ui.card().classes(f"{UIStyles.CARD_GLASS} w-full").style(
            "padding: 0; flex-wrap: nowrap"
        ):
            ui.element("div").classes(
                "h-1 w-full bg-gradient-to-r from-sky-400 via-cyan-400 to-teal-400"
            )
            with ui.column().classes("w-full flex-grow p-5 gap-4"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.icon("webhook", size="18px").classes("text-sky-400")
                    ui.label("Webhook Konfiguration").classes(
                        "text-sm font-bold uppercase tracking-widest text-slate-300"
                    )
                ui.input("Discord Webhook URL (Vault)").bind_value(
                    vault_state, "webhook_url"
                ).classes("w-full").props("outlined dense type=password")
                with ui.row().classes("w-full justify-end mt-2"):
                    ui.button(
                        "Speichern", on_click=apply_save, icon="save", color="primary"
                    ).props("unelevated rounded size=sm")

        if service is not None:
            ui.label(
                f"Versendete Benachrichtigungen: {service.notifications_sent}"
            ).classes(UIStyles.TEXT_MUTED)
