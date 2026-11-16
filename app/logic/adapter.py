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
Discord webhooks are rate-limited to ~5 requests / 5 seconds per URL.  On HTTP
429 the adapter honours ``Retry-After`` and retries once inside the background
thread — but only if the wait still fits into the send deadline (the gateway's
``send_timeout`` minus a margin).  Otherwise it returns a retryable failure and
the gateway's ``_AdapterRetryWorker`` does the back-off, so exactly one party
retries.

Edits and first sends
---------------------
A failed edit (PATCH) only leads to a new message when Discord says the message
is gone (HTTP 404).  A first send claims the ``discord_messages`` row before the
POST (see ``message_store.py``), so concurrent senders produce one message.

The 429 back-off is per core instance: N instances sharing one bot token / webhook
each see (and wait out) their own rate-limit responses — Discord's limit is not
coordinated across the cluster.

Cluster mode
------------
The notification_id → Discord message id map lives in the shared database
(``discord_messages``, see ``message_store.py``), so whichever instance handles
an update edits the original message. Every instance must carry identical
``LYNDRIX_GATEWAY_*`` env vars, otherwise instances register different adapter
sets and a notification routed to ``discord:ops`` is dropped on an instance
that lacks that adapter.

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
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import ClassVar
from uuid import UUID

import requests

from core.api import (
    GatewayAdapter,
    GatewayCapability,
    InboundMessage,
    MessageSeverity,
    ModuleContext,
    OutboundMessage,
    ProviderConfigField,
)

from .message_store import BUSY, CLAIMED, MessageStore

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
_CONNECT_TIMEOUT       = 5.0

# The worker thread must be done before the gateway's wait_for(send_timeout)
# fires, otherwise the gateway queues a retry while the thread is still sending.
_DEADLINE_MARGIN       = 2.0  # seconds kept free before send_timeout
_MIN_REQUEST_BUDGET    = 2.0  # do not start a request with less time left
_RETRY_AFTER_DEFAULT   = 5.0  # 429 without a usable Retry-After
# While a peer instance is creating the message: poll its claim this often,
# for at most this long, then edit (or give the retry to the gateway).
_CLAIM_POLL            = 0.5
_CLAIM_WAIT_MAX        = 5.0

_EDIT_OK, _EDIT_GONE, _EDIT_RETRY = "ok", "gone", "retry"

# Vault-backed config values are served from a per-adapter cache for this long:
# health() is polled by every dashboard on every instance and send() runs per
# message — neither should cost hvac round trips each time. get_config_fields()
# (called synchronously by the core from async routes) uses the same cache.
# A value saved on this instance is visible at once (save_config), a rejected
# token (401/403) drops the cache, peers follow within the TTL.
_VAULT_CACHE_TTL = 120.0


class _HttpResult:
    """Outcome of one Discord call.

    ``response`` is ``None`` when no answer was received. ``uncertain`` tells
    the two cases apart: ``True`` — the request was sent and may have been
    processed (timeout, connection reset); ``False`` — it was never sent.
    """

    __slots__ = ("response", "uncertain")

    def __init__(self, response, *, uncertain: bool) -> None:
        self.response = response
        self.uncertain = uncertain


def _timeouts(remaining: float) -> tuple[float, float]:
    """``(connect, read)`` for requests so that both together fit *remaining*."""
    connect = min(_CONNECT_TIMEOUT, remaining / 3)
    read = min(float(_HTTP_TIMEOUT), remaining - connect)
    return connect, read


def _retry_after(resp) -> float:
    """Seconds Discord asks us to wait: JSON ``retry_after``, else the header."""
    candidates = []
    try:
        candidates.append(resp.json().get("retry_after"))
    except Exception:
        pass
    candidates.append((getattr(resp, "headers", None) or {}).get("Retry-After"))
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return _RETRY_AFTER_DEFAULT


class _KeyedLocks:
    """One lock per key, created on demand and dropped when nobody uses it."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[object, tuple[threading.Lock, int]] = {}

    @contextmanager
    def hold(self, key, timeout: float):
        """Yield ``True`` once *key* is held, ``False`` if *timeout* ran out."""
        with self._guard:
            lock, users = self._locks.get(key, (threading.Lock(), 0))
            self._locks[key] = (lock, users + 1)
        acquired = lock.acquire(timeout=max(timeout, 0.0))
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
            with self._guard:
                lock, users = self._locks[key]
                if users <= 1:
                    del self._locks[key]
                else:
                    self._locks[key] = (lock, users - 1)


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
    # TODO(agent): FILE_ATTACHMENTS is advertised but no send path performs
    # multipart attachment upload (only embeds, images-by-URL and buttons).
    # Implement multipart upload or drop this flag.
    capabilities        = (
        GatewayCapability.TEXT
        | GatewayCapability.RICH_MEDIA
        | GatewayCapability.FILE_ATTACHMENTS
        | GatewayCapability.INTERACTIVE
        | GatewayCapability.EDIT_MESSAGE
    )
    # Discord webhooks are fast; 20 s is generous but avoids gateway timeout.
    send_timeout: ClassVar[float] = 20.0

    def __init__(
        self,
        ctx: ModuleContext,
        instance_id: str = "default",
        store: MessageStore | None = None,
    ) -> None:
        self._ctx         = ctx
        self._instance_id = instance_id
        self.provider_id  = "discord" if instance_id == "default" else f"discord:{instance_id}"
        # notification_id → Discord message id, shared across core instances
        # (DB-backed). Used by the Bot API path to PATCH a previously created
        # message instead of POSTing a new one (live-updating notifications).
        self._store = store if store is not None else MessageStore(ctx)
        # Vault key → (value, monotonic read time); see _VAULT_CACHE_TTL.
        self._vault_cache: dict[str, tuple[str | None, float]] = {}
        # (provider_id, notification_id) → lock: serializes sends per notification.
        self._inflight = _KeyedLocks()

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

    def vault_key(self, setting: str) -> str:
        """Public accessor for the per-instance Vault key of a config *setting*.

        Lets UIs persist values through ``save_config()`` without reaching into
        the adapter's private key-mapping helper.
        """
        return self._vault_key(setting)

    def _get_config(self, key: str) -> str | None:
        """Read a per-instance config value: env var first, then Vault (cached)."""
        env_val = os.getenv(f"{self._env_prefix()}_{key.upper()}")
        if env_val:
            return env_val
        return self._cached_vault(self._vault_key(key))

    def _read_vault(self, vault_key: str) -> str | None:
        """Blocking Vault read that also refreshes the config-field cache."""
        value = self._ctx.get_secret(vault_key)
        self._vault_cache[vault_key] = (value, time.monotonic())
        return value

    def _cached_vault(self, vault_key: str) -> str | None:
        """Vault value: cached when fresh (``_VAULT_CACHE_TTL``), else read.

        health()/send() call this off-thread and keep the cache warm, so the
        synchronous get_config_fields() read on the core's event loop only
        happens on a cold cache.
        """
        # TODO(agent): the core calls get_config_fields() synchronously from async
        # routes (notification_router/api.py); offload it there and drop this cache.
        entry = self._vault_cache.get(vault_key)
        if entry and time.monotonic() - entry[1] < _VAULT_CACHE_TTL:
            return entry[0]
        return self._read_vault(vault_key)

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
            vault_val = self._cached_vault(self._vault_key(setting)) if not env_val else None
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
        ok = self._ctx.set_secret(key, value)
        if ok:
            self._vault_cache[key] = (value, time.monotonic())
        return ok

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health(self) -> bool:
        """Return True if either Bot API (bot_token + channel_id) or a webhook URL is configured.

        Config resolution may hit Vault (synchronous hvac), so it is offloaded
        to a thread to keep the event loop responsive.
        """
        return await asyncio.to_thread(self._health_sync)

    def _health_sync(self) -> bool:
        # Read every field (not short-circuited) so the config-field cache used
        # by get_config_fields() stays warm — see _cached_vault(). Within the
        # TTL this costs no Vault read at all (audit DN-05).
        cfg = {k: self._get_config(k) for k in ("bot_token", "channel_id", "webhook_url", "bot_name")}
        has_bot_api = bool(cfg["bot_token"] and cfg["channel_id"])
        has_webhook = bool(cfg["webhook_url"])
        return has_bot_api or has_webhook

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, message: OutboundMessage) -> str | None:
        # The gateway waits ``send_timeout`` for this coroutine and then queues
        # a retry — but it cannot stop the worker thread. The deadline is fixed
        # here, on the loop, so time spent queueing for a thread counts too: the
        # worker gives up (returns None) before the gateway does, and exactly
        # one party retries (audit DN-02).
        deadline = time.monotonic() + max(self.send_timeout - _DEADLINE_MARGIN, _MIN_REQUEST_BUDGET)
        # All work below (Vault config reads + the HTTP call) is synchronous, so
        # run the whole dispatch in a worker thread to keep the loop unblocked.
        return await asyncio.to_thread(self._send_sync, message, deadline)

    def _send_sync(self, message: OutboundMessage, deadline: float | None = None) -> str | None:
        if deadline is None:
            deadline = time.monotonic() + max(self.send_timeout - _DEADLINE_MARGIN, _MIN_REQUEST_BUDGET)
        # Prefer the Bot API (supports editing). Fall back to webhook when no
        # bot_token/channel_id is configured.
        bot_token  = self._get_config("bot_token")
        channel_id = self._get_config("channel_id")
        if bot_token and channel_id:
            return self._send_via_bot_api(message, bot_token, channel_id, deadline)

        webhook_url = self._get_config("webhook_url")
        if not webhook_url:
            log.debug(
                "Discord adapter [%s]: no delivery config (bot_token+channel_id or webhook_url) — skipping.",
                self._instance_id,
            )
            return None

        bot_name = self._get_config("bot_name") or "Lyndrix"
        payload  = self._build_payload(message, bot_name)

        result = self._request("POST", str(webhook_url), payload, None, deadline)
        resp = result.response
        if resp is None:
            return None
        if resp.status_code in (200, 204):
            log.debug("Discord adapter [%s]: delivered (HTTP %s).", self._instance_id, resp.status_code)
            return str(resp.status_code)
        log.warning(
            "Discord adapter [%s]: webhook returned %s — %s",
            self._instance_id, resp.status_code, resp.text[:200],
        )
        return None

    # ------------------------------------------------------------------
    # HTTP with a deadline
    # ------------------------------------------------------------------

    def _request(self, method: str, url: str, payload: dict, headers: dict | None, deadline: float) -> "_HttpResult":
        """One Discord call, at most one rate-limit wait, never past *deadline*.

        On HTTP 429 the ``Retry-After`` window is honoured only if the wait plus
        a second attempt still fit into the deadline. Otherwise the 429 response
        is returned as it is: the caller reports a retryable failure and the
        gateway's retry worker (which backs off on its own) tries again —
        sleeping here as well would let both of them send.
        """
        call = requests.patch if method == "PATCH" else requests.post
        attempts = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining < _MIN_REQUEST_BUDGET:
                log.warning(
                    "Discord [%s]: no time left for %s before the send deadline — leaving it to the gateway retry.",
                    self._instance_id, method,
                )
                return _HttpResult(None, uncertain=False)
            kwargs: dict = {"json": payload, "timeout": _timeouts(remaining)}
            if headers:
                kwargs["headers"] = headers
            attempts += 1
            try:
                resp = call(url, **kwargs)
            except Exception as exc:
                # Only the exception type: requests puts the request URL into the
                # message, and a webhook URL carries its token.
                log.error("Discord [%s]: %s failed (%s).", self._instance_id, method, type(exc).__name__)
                # The request may or may not have reached Discord.
                return _HttpResult(None, uncertain=True)

            if resp.status_code != 429 or attempts > 1:
                return _HttpResult(resp, uncertain=False)

            wait = _retry_after(resp)
            if time.monotonic() + wait + _MIN_REQUEST_BUDGET > deadline:
                log.warning(
                    "Discord [%s]: rate limited (429, retry after %.1fs) — does not fit the send deadline, "
                    "leaving the retry to the gateway.",
                    self._instance_id, wait,
                )
                return _HttpResult(resp, uncertain=False)
            log.warning(
                "Discord [%s]: rate limited (429) — sleeping %.1fs before one retry.",
                self._instance_id, wait,
            )
            time.sleep(wait)

    # ------------------------------------------------------------------
    # Bot API delivery (POST/PATCH /channels/{id}/messages)
    # ------------------------------------------------------------------

    def _send_via_bot_api(
        self,
        message: OutboundMessage,
        bot_token: str,
        channel_id: str,
        deadline: float | None = None,
    ) -> str | None:
        if deadline is None:
            deadline = time.monotonic() + max(self.send_timeout - _DEADLINE_MARGIN, _MIN_REQUEST_BUDGET)
        payload         = self._build_api_payload(message)
        notification_id = (message.metadata or {}).get("notification_id")
        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type":  "application/json",
        }
        base_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"

        if not notification_id:
            # Nothing to edit later and nothing to de-duplicate against.
            return self._create(base_url, payload, headers, deadline)

        nid = str(notification_id)
        # One send per notification at a time in this process: an update that
        # arrives (or a gateway retry that fires) while an earlier send is still
        # in flight waits for it and then edits, instead of racing it.
        with self._inflight.hold((self.provider_id, nid), deadline - time.monotonic() - _MIN_REQUEST_BUDGET) as held:
            if not held:
                log.warning(
                    "Discord [%s]: an earlier send for notification %s is still running — retry later.",
                    self._instance_id, nid,
                )
                return None
            return self._upsert_message(nid, str(channel_id), base_url, payload, headers, deadline)

    def _upsert_message(
        self, nid: str, channel_id: str, base_url: str, payload: dict, headers: dict, deadline: float,
    ) -> str | None:
        """Edit the notification's message, or create it if there is none (yet/any more)."""
        # Second pass only after an edit found the message deleted on Discord.
        for _ in range(2):
            existing_id = self._store.get(self.provider_id, nid, channel_id)
            claim = None
            if existing_id is None:
                claim = self._claim(nid, channel_id, deadline)
                if claim.state == BUSY:
                    log.warning(
                        "Discord [%s]: another instance is creating the message for notification %s — retry later.",
                        self._instance_id, nid,
                    )
                    return None
                if claim.state == CLAIMED:
                    return self._create(base_url, payload, headers, deadline, nid=nid, channel_id=channel_id, claim=claim)
                existing_id = claim.message_id

            outcome = self._edit(f"{base_url}/{existing_id}", payload, headers, deadline)
            if outcome == _EDIT_OK:
                log.debug("Discord [%s]: edited message %s.", self._instance_id, existing_id)
                return existing_id
            if outcome == _EDIT_RETRY:
                return None
            # _EDIT_GONE: the message was deleted on Discord. Drop the mapping
            # (only if it still points there) and create a fresh message.
            log.warning(
                "Discord [%s]: message %s no longer exists — creating a new one.",
                self._instance_id, existing_id,
            )
            self._store.forget(self.provider_id, nid, existing_id)
        return None

    def _claim(self, nid: str, channel_id: str, deadline: float):
        """Claim the first send; wait briefly while a peer is creating the message."""
        waited = 0.0
        while True:
            claim = self._store.claim(self.provider_id, nid, channel_id)
            if claim.state != BUSY:
                return claim
            # The peer normally finishes within a second or two; then we edit.
            budget_left = deadline - time.monotonic() - _MIN_REQUEST_BUDGET
            if waited >= _CLAIM_WAIT_MAX or budget_left < _CLAIM_POLL:
                return claim
            time.sleep(_CLAIM_POLL)
            waited += _CLAIM_POLL

    def _edit(self, edit_url: str, payload: dict, headers: dict, deadline: float) -> str:
        """PATCH an existing message → ``_EDIT_OK`` / ``_EDIT_GONE`` / ``_EDIT_RETRY``.

        Only a definitive "this message does not exist" (HTTP 404, Discord code
        10008) may lead to a new message. A rate limit, a 5xx or a timeout says
        nothing about the message: reposting then would leave the old message
        frozen and add a new one per update — under a rate limit that amplifies
        the very condition that caused it (audit DN-01).
        """
        result = self._request("PATCH", edit_url, payload, headers, deadline)
        resp = result.response
        if resp is None:
            return _EDIT_RETRY
        if resp.status_code == 200:
            return _EDIT_OK
        if resp.status_code == 404:
            return _EDIT_GONE
        if resp.status_code in (401, 403):
            # Possibly a rotated token still in the config cache: re-read next time.
            self._vault_cache.clear()
        log.warning(
            "Discord [%s]: edit failed (HTTP %s) — keeping the message, the gateway retries the edit.",
            self._instance_id, resp.status_code,
        )
        return _EDIT_RETRY

    def _create(
        self, base_url: str, payload: dict, headers: dict, deadline: float,
        *, nid: str | None = None, channel_id: str | None = None, claim=None,
    ) -> str | None:
        """POST a new message; settle the claim according to the outcome."""
        nonce = claim.nonce if claim is not None else None
        if nonce:
            # Discord returns the already created message for a repeated nonce
            # instead of creating another one (a few minutes' window) — covers a
            # POST that arrived although we only saw a timeout.
            payload = {**payload, "nonce": nonce, "enforce_nonce": True}

        result = self._request("POST", base_url, payload, headers, deadline)
        resp = result.response
        if resp is not None and resp.status_code == 200:
            try:
                discord_msg_id = str(resp.json()["id"])
            except Exception as exc:
                log.error(
                    "Discord [%s]: created message but response had no id: %s",
                    self._instance_id, exc,
                )
                if nid:
                    self._store.abandon(self.provider_id, nid, nonce)
                return None
            if nid:
                self._store.put(self.provider_id, nid, channel_id, discord_msg_id)
            log.debug("Discord [%s]: created message %s.", self._instance_id, discord_msg_id)
            return discord_msg_id

        if resp is not None:
            if resp.status_code in (401, 403):
                self._vault_cache.clear()
            log.warning(
                "Discord [%s]: bot API create returned %s — %s",
                self._instance_id, resp.status_code, resp.text[:200],
            )
        if nid:
            if result.uncertain:
                self._store.abandon(self.provider_id, nid, nonce)
            else:
                # Discord answered (or was never asked): nothing was created.
                self._store.release(self.provider_id, nid, nonce)
        return None

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

    def lyndrix_user_for(self, discord_user_id: str | None) -> str | None:
        """Lyndrix username linked to a Discord user id — ``None`` = not linked.

        A button click is an action request by *somebody in the Discord
        channel*; it may only be acted on for a known Lyndrix user who is
        allowed to perform that action. No Discord ↔ Lyndrix account link exists
        yet, so nobody is linked and every component interaction is refused
        (audit DN-03) — fail closed rather than trust channel membership.
        """
        # TODO(agent): needs a core/gateway feature — a provider-user → Lyndrix-user
        # mapping (per-user "linked accounts" or an admin-managed allowlist) plus a
        # permission check for the pending action in MessagingGateway.handle_incoming
        # before the resume topic is emitted. Resolve the user here once it exists.
        del discord_user_id
        return None

    @staticmethod
    def interaction_user_id(raw: dict) -> str | None:
        """Discord user id of the member/user who triggered an interaction."""
        member = raw.get("member") or {}
        user = member.get("user") or raw.get("user") or {}
        user_id = user.get("id")
        return str(user_id) if user_id else None

    @staticmethod
    def _public_interaction(raw: dict) -> dict:
        """The part of an interaction that may travel on the event bus.

        Allowlist, not denylist: the raw payload carries the interaction
        ``token`` (lets its holder post follow-ups and edit the original message
        for 15 minutes) and the member object (roles, permissions, nick) — the
        bus delivers payloads to every subscriber and to peer instances.
        """
        data = raw.get("data") or {}
        message = raw.get("message") or {}
        return {
            "id": raw.get("id"),
            "type": raw.get("type"),
            "application_id": raw.get("application_id"),
            "guild_id": raw.get("guild_id"),
            "channel_id": raw.get("channel_id"),
            "message_id": message.get("id"),
            "custom_id": data.get("custom_id"),
            "component_type": data.get("component_type"),
        }

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
        user_id   = self.interaction_user_id(raw)

        # Authorization gate (also enforced by the route, which answers the
        # user): without a linked Lyndrix user nothing reaches the gateway.
        lyndrix_user = self.lyndrix_user_for(user_id)
        if not lyndrix_user:
            log.warning(
                "Discord adapter: component interaction by unlinked Discord user %s refused.",
                user_id or "<unknown>",
            )
            return None

        # custom_id format: "{action_id}:{correlation_id}"
        action_id, _, corr_str = custom_id.partition(":")
        correlation_id: UUID | None = None
        if corr_str:
            try:
                correlation_id = UUID(corr_str)
            except ValueError:
                log.warning("Discord adapter: invalid correlation_id in custom_id: %s", corr_str)

        return InboundMessage(
            provider_id=self.provider_id,
            event_type="component_interaction",
            correlation_id=correlation_id,
            action_id=action_id or None,
            user_id=user_id,
            # Consumers of the resume topic must still check that ``lyndrix_user``
            # may perform ``action_id``. ``raw_interaction`` keeps its name for
            # compatibility but only carries the allowlisted, secret-free fields.
            metadata={
                "lyndrix_user": lyndrix_user,
                "raw_interaction": self._public_interaction(raw),
            },
        )
