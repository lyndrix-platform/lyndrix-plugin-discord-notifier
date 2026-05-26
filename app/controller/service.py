from datetime import datetime

from .webhook import send_notification_webhook, send_webhook


class DiscordNotifierService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.state = {"notifications_sent": 0}

    @property
    def notifications_sent(self) -> int:
        return self.state["notifications_sent"]

    async def handle_change(self, data: dict) -> None:
        webhook_url = self.ctx.get_secret("webhook_url")
        if not webhook_url:
            return

        send_webhook(
            service=self,
            webhook_url=webhook_url,
            bot_name="Lyndrix Broker",
            entity=data.get("entity_type", "System"),
            action=data.get("action", "UPDATE"),
            payload=data.get("payload", {}),
        )

    async def handle_boot_complete(self, payload: dict) -> None:
        self.ctx.log.info("EVENT: Boot event received. Sending status to Discord...")
        webhook_url = self.ctx.get_secret("webhook_url")
        if not webhook_url:
            self.ctx.log.warning("SKIP: Boot notification skipped: No webhook.")
            return

        send_webhook(
            service=self,
            webhook_url=webhook_url,
            bot_name="Lyndrix System",
            entity="Core Engine",
            action="STARTUP",
            payload={
                "status": "Online",
                "message": "Alle Kernsysteme erfolgreich hochgefahren.",
                "zeitpunkt": datetime.now().strftime("%H:%M:%S"),
            },
        )

    async def handle_notification(self, payload: dict) -> None:
        webhook_url = self.ctx.get_secret("webhook_url")
        if not webhook_url:
            return

        send_notification_webhook(
            service=self,
            webhook_url=webhook_url,
            bot_name="Lyndrix Notifier",
            notif=payload,
        )
