"""
DiscordGatewayAdapter — production-hardened GatewayAdapter implementation.

Translates OutboundMessage objects into Discord webhook payloads (embeds +
action buttons via components) and parses incoming Discord Interaction
payloads back into InboundMessage objects.

Incoming interactions require a publicly reachable URL:
  POST /api/plugins/lyndrix.plugin.discord/interactions

Discord sends Component Interactions (type=3) here when a user clicks a
button.  The ``custom_id`` of each button is set to ``str(action.correlation_id)``
so the MessagingGateway can look up and resolve the pending action.

Rate limiting
-------------
Discord webhooks are rate-limited to ~5 requests / 5 seconds per URL.  The
adapter inspects the ``Retry-After`` response header on HTTP 429 and sleeps
inside the background thread before retrying once.  Further back-off is
handled by the gateway's ``_AdapterRetryWorker``.

Payload limits (enforced via _truncate())
------------------------------------------
- Embed title:       256 chars
- Embed description: 4096 chars
- Button label:      80 chars
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID

import requests

from core.api import (
    ActionButton,
    GatewayAdapter,
    GatewayCapability,
    InboundMessage,
    MessageSeverity,
    OutboundMessage,
    ProviderConfigField,
)

if TYPE_CHECKING:
    from core.components.plugins.logic.context import ModuleContext

log = logging.getLogger("Plugin:DiscordAdapter")

# Discord embed colour codes keyed by MessageSeverity
_SEVERITY_COLOR: dict[MessageSeverity, int] = {
    MessageSeverity.SUCCESS: 0x57F287,  # green
    MessageSeverity.ERROR:   0xED4245,  # red
    MessageSeverity.WARNING: 0xFEE75C,  # yellow
    MessageSeverity.INFO:    0x5865F2,  # blurple
}

# Discord button style codes keyed by ActionButton.style
_BUTTON_STYLE: dict[str, int] = {
    "primary":   1,
    "secondary": 2,
    "danger":    4,
}

_LYNDRIX_AVATAR = (
    "https://raw.githubusercontent.com/lyndrix-platform/"
    "lyndrix-core/main/app/assets/icons/favicon-32x32.png"
)

# Discord hard limits
_EMBED_TITLE_MAX       = 256
_EMBED_DESCRIPTION_MAX = 4096
_EMBED_FIELD_NAME_MAX  = 256
_EMBED_FIELD_VALUE_MAX = 1024
_EMBED_FIELDS_MAX      = 25
_BUTTON_LABEL_MAX      = 80
_HTTP_TIMEOUT          = 15   # seconds per request inside asyncio.to_thread

# In-memory message ID store retention: long enough for a pipeline run, but
# bounded so the store does not grow without bound across reboots.
_MSG_STORE_TTL = 86400  # 24 hours


def _truncate(s: str, limit: int) -> str:
    """Trim *s* to *limit* characters, appending '…' if truncated."""
    if not s:
        return s
    return s if len(s) <= limit else s[:limit - 1] + "…"


class DiscordGatewayAdapter(GatewayAdapter):
    """Gateway adapter that delivers messages to Discord via webhooks.

    Supports outbound rich embeds and inbound button interactions.

    Multi-instance support
    ----------------------
    Pass ``instance_id`` to create multiple independent adapters, each with
    its own webhook URL (e.g. separate ops / alerts channels).  The
    ``provider_id`` is then ``discord:{instance_id}`` so the Gateway can
    route to each independently.

    Config is read from environment variables first, then falls back to
    Vault secrets via ``ctx.get_secret()``:

    - Env var: ``LYNDRIX_GATEWAY_DISCORD_{INSTANCE_ID}_{SETTING_KEY}``
    - Vault  : ``{instance_id}_{setting_key}`` (e.g. ``ops_webhook_url``)
      Except for the default instance (``instance_id="default"``), where the
      Vault key is just ``webhook_url`` / ``bot_name`` for backward compat.
    """

    # provider_id is set dynamically in __init__; ClassVar annotation kept for ABC.
    provider_id:  str = "discord"
    display_name: str = "Discord"
    capabilities        = (
        GatewayCapability.TEXT
        | GatewayCapability.RICH_MEDIA
        | GatewayCapability.FILE_ATTACHMENTS
        | GatewayCapability.INTERACTIVE
        | GatewayCapability.EDIT_MESSAGE
    )
    # Discord webhooks are fast; 20 s is generous but avoids gateway timeout.
    send_timeout: ClassVar[float] = 20.0

    def __init__(self, ctx: "ModuleContext", instance_id: str = "default") -> None:
        self._ctx         = ctx
        self._instance_id = instance_id
        self.provider_id  = "discord" if instance_id == "default" else f"discord:{instance_id}"
        # notification_id → (discord_message_id, monotonic_created_time).
        # Used by the Bot API path to PATCH a previously created message
        # instead of POSTing a new one (live-updating notifications).
        self._message_store: dict[str, tuple[str, float]] = {}

    # ------------------------------------------------------------------
    # Bot API message ID store (per-instance, in-memory)
    # ------------------------------------------------------------------

    def _get_stored_message_id(self, notification_id: str) -> str | None:
        entry = self._message_store.get(notification_id)
        if entry and (time.monotonic() - entry[1]) < _MSG_STORE_TTL:
            return entry[0]
        # Either missing or expired — drop and report miss.
        self._message_store.pop(notification_id, None)
        return None

    def _store_message_id(self, notification_id: str, discord_message_id: str) -> None:
        self._message_store[notification_id] = (discord_message_id, time.monotonic())
        # Opportunistic cleanup of expired entries to keep the dict bounded.
        now = time.monotonic()
        expired = [k for k, (_, t) in self._message_store.items() if now - t > _MSG_STORE_TTL]
        for k in expired:
            del self._message_store[k]

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _env_prefix(self) -> str:
        return f"LYNDRIX_GATEWAY_DISCORD_{self._instance_id.upper().replace('-', '_')}"

    def _vault_key(self, setting: str) -> str:
        return (
            setting.lower()
            if self._instance_id == "default"
            else f"{self._instance_id}_{setting.lower()}"
        )

    def _get_config(self, key: str) -> str | None:
        """Read a per-instance config value: env var first, then Vault."""
        env_val = os.getenv(f"{self._env_prefix()}_{key.upper()}")
        if env_val:
            return env_val
        return self._ctx.get_secret(self._vault_key(key))

    # ------------------------------------------------------------------
    # ProviderConfigField API (used by notification router settings UI)
    # ------------------------------------------------------------------

    def get_config_fields(self) -> list[ProviderConfigField]:
        prefix = self._env_prefix()
        fields = []
        for setting, label, sensitive, placeholder in [
            ("BOT_TOKEN",   "Bot Token",              True,  "MTQ5NTcwNTEzND…"),
            ("CHANNEL_ID",  "Channel ID",             False, "1511417154287960386"),
            ("WEBHOOK_URL", "Webhook URL (fallback)", True,  "https://discord.com/api/webhooks/…"),
            ("BOT_NAME",    "Bot Name",               False, "Lyndrix"),
        ]:
            env_var   = f"{prefix}_{setting}"
            env_val   = os.getenv(env_var)
            vault_val = self._ctx.get_secret(self._vault_key(setting)) if not env_val else None
            fields.append(ProviderConfigField(
                key=self._vault_key(setting),
                label=label,
                env_var=env_var,
                current_value=env_val or vault_val,
                is_env_locked=bool(env_val),
                sensitive=sensitive,
                placeholder=placeholder,
            ))
        return fields

    def save_config(self, key: str, value: str) -> bool:
        """Persist a config value to Vault. Validates webhook URLs before saving."""
        if key.endswith("webhook_url") and value:
            if not value.startswith("https://discord.com/api/webhooks/"):
                log.warning(
                    "Discord adapter [%s]: rejecting invalid webhook URL (must start with "
                    "https://discord.com/api/webhooks/).",
                    self._instance_id,
                )
                return False
        return self._ctx.set_secret(key, value)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return True if either Bot API (bot_token + channel_id) or a webhook URL is configured."""
        has_bot_api = bool(self._get_config("bot_token") and self._get_config("channel_id"))
        has_webhook = bool(self._get_config("webhook_url"))
        return has_bot_api or has_webhook

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> str | None:
        # Prefer the Bot API (supports editing). Fall back to webhook when no
        # bot_token/channel_id is configured.
        bot_token  = self._get_config("bot_token")
        channel_id = self._get_config("channel_id")
        if bot_token and channel_id:
            return await asyncio.to_thread(
                self._send_via_bot_api, message, bot_token, channel_id
            )

        webhook_url = self._get_config("webhook_url")
        if not webhook_url:
            log.debug(
                "Discord adapter [%s]: no delivery config (bot_token+channel_id or webhook_url) — skipping.",
                self._instance_id,
            )
            return None

        bot_name = self._get_config("bot_name") or "Lyndrix"
        payload  = self._build_payload(message, bot_name)
        instance = self._instance_id   # capture for closure

        def _post() -> str | None:
            """Synchronous HTTP call wrapped in asyncio.to_thread for non-blocking dispatch."""
            try:
                resp = requests.post(str(webhook_url), json=payload, timeout=_HTTP_TIMEOUT)

                # Rate-limited: wait for the Retry-After window, then retry once.
                if resp.status_code == 429:
                    retry_after = 5.0
                    try:
                        retry_after = float(resp.json().get("retry_after", 5.0))
                    except Exception:
                        pass
                    log.warning(
                        "Discord adapter [%s]: rate limited (429) — sleeping %.1fs before retry.",
                        instance, retry_after,
                    )
                    time.sleep(retry_after)
                    resp = requests.post(str(webhook_url), json=payload, timeout=_HTTP_TIMEOUT)

                if resp.status_code in (200, 204):
                    log.debug("Discord adapter [%s]: delivered (HTTP %s).", instance, resp.status_code)
                    return str(resp.status_code)

                log.warning(
                    "Discord adapter [%s]: webhook returned %s — %s",
                    instance, resp.status_code, resp.text[:200],
                )
                return None
            except Exception as exc:
                log.error("Discord adapter [%s]: send failed: %s", instance, exc)
                return None

        return await asyncio.to_thread(_post)

    # ------------------------------------------------------------------
    # Bot API delivery (POST/PATCH /channels/{id}/messages)
    # ------------------------------------------------------------------

    def _send_via_bot_api(
        self,
        message: OutboundMessage,
        bot_token: str,
        channel_id: str,
    ) -> str | None:
        payload         = self._build_api_payload(message)
        notification_id = (message.metadata or {}).get("notification_id")
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type":  "application/json",
        }
        base_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

        # Try to edit an existing message first.
        if notification_id:
            existing_id = self._get_stored_message_id(str(notification_id))
            if existing_id:
                edit_url = f"{base_url}/{existing_id}"
                try:
                    resp = requests.patch(
                        edit_url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT,
                    )
                except Exception as exc:
                    log.error(
                        "Discord [%s]: edit request failed: %s",
                        self._instance_id, exc,
                    )
                    return None
                if resp.status_code == 200:
                    log.debug(
                        "Discord [%s]: edited message %s.",
                        self._instance_id, existing_id,
                    )
                    return existing_id
                log.warning(
                    "Discord [%s]: edit failed (HTTP %s) — creating new message instead.",
                    self._instance_id, resp.status_code,
                )
                # Drop the stale id so we don't keep PATCHing it.
                self._message_store.pop(str(notification_id), None)

        # POST a new message (and remember its id if we have a notification_id).
        resp = self._post_with_rate_limit(base_url, payload, headers)
        if resp is None:
            return None
        if resp.status_code == 200:
            try:
                discord_msg_id = str(resp.json()["id"])
            except Exception as exc:
                log.error(
                    "Discord [%s]: created message but response had no id: %s",
                    self._instance_id, exc,
                )
                return None
            if notification_id:
                self._store_message_id(str(notification_id), discord_msg_id)
            log.debug(
                "Discord [%s]: created message %s.",
                self._instance_id, discord_msg_id,
            )
            return discord_msg_id
        log.warning(
            "Discord [%s]: bot API create returned %s — %s",
            self._instance_id, resp.status_code, resp.text[:200],
        )
        return None

    def _post_with_rate_limit(self, url: str, payload: dict, headers: dict):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT)
        except Exception as exc:
            log.error("Discord [%s]: post failed: %s", self._instance_id, exc)
            return None
        if resp.status_code == 429:
            retry_after = 5.0
            try:
                retry_after = float(resp.json().get("retry_after", 5.0))
            except Exception:
                pass
            log.warning(
                "Discord [%s]: rate limited (429) — sleeping %.1fs before retry.",
                self._instance_id, retry_after,
            )
            time.sleep(retry_after)
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=_HTTP_TIMEOUT)
            except Exception as exc:
                log.error("Discord [%s]: retry post failed: %s", self._instance_id, exc)
                return None
        return resp

    # ------------------------------------------------------------------
    # Payload builders
    # ------------------------------------------------------------------

    def _embed_fields_from_metadata(self, msg: OutboundMessage) -> list[dict]:
        """Pull rich embed fields out of metadata.lyndrix_payload.embed_fields, truncated to Discord limits."""
        lp     = (msg.metadata or {}).get("lyndrix_payload") or {}
        raw    = lp.get("embed_fields") or []
        result = []
        for f in raw[:_EMBED_FIELDS_MAX]:
            result.append({
                "name":   _truncate(str(f.get("name", "")), _EMBED_FIELD_NAME_MAX),
                "value":  _truncate(str(f.get("value", "")), _EMBED_FIELD_VALUE_MAX),
                "inline": bool(f.get("inline", False)),
            })
        return result

    def _build_api_payload(self, msg: OutboundMessage) -> dict:
        """Payload for the Discord Bot API (POST/PATCH /channels/{id}/messages).

        Unlike the webhook path, the Bot API does not accept top-level
        ``username`` / ``avatar_url`` — those are properties of the bot user.
        """
        color = _SEVERITY_COLOR.get(msg.severity, _SEVERITY_COLOR[MessageSeverity.INFO])

        embed: dict = {
            "title":       _truncate(msg.title or "", _EMBED_TITLE_MAX),
            "description": _truncate(msg.body  or "", _EMBED_DESCRIPTION_MAX),
            "color":       color,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        fields = self._embed_fields_from_metadata(msg)
        if fields:
            embed["fields"] = fields
        if msg.image_url:
            embed["image"] = {"url": str(msg.image_url)}

        payload: dict = {"embeds": [embed]}

        if msg.actions and self.supports(GatewayCapability.INTERACTIVE):
            buttons = []
            for btn in msg.actions:
                buttons.append({
                    "type":      2,
                    "label":     _truncate(btn.label, _BUTTON_LABEL_MAX),
                    "style":     _BUTTON_STYLE.get(btn.style, 1),
                    "custom_id": f"{btn.action_id}:{btn.correlation_id}",
                })
            payload["components"] = [{"type": 1, "components": buttons}]

        return payload

    def _build_payload(self, msg: OutboundMessage, bot_name: str) -> dict:
        """Build the Discord webhook JSON payload from an OutboundMessage.

        All user-supplied string fields are truncated to Discord's hard limits
        to prevent silent drops caused by oversized payloads.
        """
        color = _SEVERITY_COLOR.get(msg.severity, _SEVERITY_COLOR[MessageSeverity.INFO])

        embed: dict = {
            "title":       _truncate(msg.title or "", _EMBED_TITLE_MAX),
            "description": _truncate(msg.body  or "", _EMBED_DESCRIPTION_MAX),
            "color":       color,
        }
        fields = self._embed_fields_from_metadata(msg)
        if fields:
            embed["fields"] = fields
        if msg.image_url:
            embed["image"] = {"url": str(msg.image_url)}

        payload: dict = {
            "username":   bot_name,
            "avatar_url": _LYNDRIX_AVATAR,
            "embeds":     [embed],
        }

        # Add interactive buttons as a Discord action-row component.
        if msg.actions and self.supports(GatewayCapability.INTERACTIVE):
            buttons = []
            for btn in msg.actions:
                buttons.append({
                    "type":      2,   # BUTTON
                    "label":     _truncate(btn.label, _BUTTON_LABEL_MAX),
                    "style":     _BUTTON_STYLE.get(btn.style, 1),
                    # Encode the correlation_id into custom_id for round-trip resolution.
                    "custom_id": f"{btn.action_id}:{btn.correlation_id}",
                })
            payload["components"] = [{"type": 1, "components": buttons}]

        return payload

    # ------------------------------------------------------------------
    # Inbound (Discord Component Interactions, type=3)
    # ------------------------------------------------------------------

    async def handle_incoming(self, raw: dict) -> InboundMessage | None:
        interaction_type = raw.get("type")

        # Type 1 = PING (Discord verification handshake — handled in the route)
        if interaction_type == 1:
            return None

        # Type 3 = MESSAGE_COMPONENT (button click)
        if interaction_type != 3:
            log.debug("Discord adapter: ignoring interaction type %s.", interaction_type)
            return None

        data      = raw.get("data") or {}
        custom_id = data.get("custom_id") or ""
        member    = raw.get("member") or {}
        user      = member.get("user") or raw.get("user") or {}
        user_id   = user.get("id")

        # custom_id format: "{action_id}:{correlation_id}"
        action_id, _, corr_str = custom_id.partition(":")
        correlation_id: UUID | None = None
        if corr_str:
            try:
                correlation_id = UUID(corr_str)
            except ValueError:
                log.warning("Discord adapter: invalid correlation_id in custom_id: %s", corr_str)

        return InboundMessage(
            provider_id="discord",
            event_type="component_interaction",
            correlation_id=correlation_id,
            action_id=action_id or None,
            user_id=user_id,
            metadata={"raw_interaction": raw},
        )

    # ------------------------------------------------------------------
    # Legacy bridge (used by entrypoint during migration period)
    # ------------------------------------------------------------------

    async def legacy_send_notification(self, payload: dict) -> None:
        """Translate a ``notification:outbound`` payload and send it directly.

        Deprecated. Kept for backward compatibility until all callers have
        been migrated to ``messaging:outbound``.
        """
        severity_map = {
            "positive": MessageSeverity.SUCCESS,
            "negative": MessageSeverity.ERROR,
            "warning":  MessageSeverity.WARNING,
        }
        msg = OutboundMessage(
            title=payload.get("title") or "Lyndrix",
            body=payload.get("message") or "",
            severity=severity_map.get(payload.get("type", "info"), MessageSeverity.INFO),
            source_plugin_id="lyndrix.plugin.discord",
        )
        await self.send(msg)
