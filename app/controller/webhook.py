from datetime import datetime, timezone

import requests

AVATAR_URL = "https://raw.githubusercontent.com/lyndrix-platform/lyndrix-core/main/app/assets/icons/logo.png"


def send_webhook(
    service, webhook_url: str, bot_name: str, entity: str, action: str, payload: dict
) -> bool:
    embed_color = 5763719 if action == "CREATE" else 16753920
    embed_fields = []

    for key, value in payload.items():
        if value != "" and value is not None:
            str_val = str(value)
            if len(str_val) > 100:
                str_val = str_val[:97] + "..."
            embed_fields.append(
                {"name": str(key).capitalize(), "value": f"`{str_val}`", "inline": True}
            )

    embed_fields = embed_fields[:25]

    discord_msg = {
        "username": bot_name,
        "avatar_url": AVATAR_URL,
        "embeds": [
            {
                "title": f"🚀 {entity} Event ausgelöst!",
                "description": f"Ein **{action}** Vorgang wurde registriert.\nDetails:",
                "color": embed_color,
                "fields": embed_fields,
                "footer": {"text": "Lyndrix Plugin Engine"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    try:
        response = requests.post(webhook_url, json=discord_msg, timeout=3)
        if response.status_code == 204:
            service.state["notifications_sent"] += 1
            service.ctx.log.info("SUCCESS: Embed sent to Discord.")
            return True

        service.ctx.log.warning(
            f"WARNING: Unexpected Discord status: {response.status_code}"
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive runtime logging
        service.ctx.log.error(f"ERROR: Failed to send to Discord: {exc}", exc_info=True)
        return False


def send_notification_webhook(
    service, webhook_url: str, bot_name: str, notif: dict
) -> bool:
    type_colors = {
        "positive": 5763719,
        "negative": 15548997,
        "warning": 16753920,
        "info": 3447003,
    }
    embed_color = type_colors.get(notif.get("type", "info"), 3447003)

    discord_msg = {
        "username": bot_name,
        "avatar_url": AVATAR_URL,
        "embeds": [
            {
                "title": f"🔔 {notif.get('title', 'System Notification')}",
                "description": notif.get("message", "No content provided."),
                "color": embed_color,
                "footer": {"text": "Lyndrix Notification Engine"},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }

    try:
        response = requests.post(webhook_url, json=discord_msg, timeout=3)
        if response.status_code == 204:
            service.state["notifications_sent"] += 1
            service.ctx.log.info(
                f"SUCCESS: Notification '{notif.get('title')}' sent to Discord."
            )
            return True

        service.ctx.log.warning(
            f"WARNING: Unexpected Discord status: {response.status_code}"
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive runtime logging
        service.ctx.log.error(
            f"ERROR: Failed to send notification to Discord: {exc}", exc_info=True
        )
        return False
