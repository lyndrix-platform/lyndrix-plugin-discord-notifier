"""DiscordNotifierService — owns the shared message store and its lifecycle.

All delivery goes through ``DiscordGatewayAdapter.send()``; this service only
bootstraps the cluster-shared ``discord_messages`` table and reports its size.
(The former per-process ``notifications_sent`` counter was never incremented
and meant nothing behind a load balancer — it was dropped in 1.2.0.)
"""
from __future__ import annotations

import asyncio
import re
import time

from core.api import get_logger

from .message_store import MessageStore

log = get_logger("Plugin:Discord:Service")

# Plugin setting (manifest ``settings_schema``) holding the application's
# Ed25519 public key; the raw Vault secret is the pre-1.3 location, which could
# only be written by hand.
PUBLIC_KEY_SETTING = "interactions_public_key"
LEGACY_PUBLIC_KEY_SECRET = "discord_public_key"
# The interactions endpoint is public: every request, authentic or not, needs
# the key. Cached so an anonymous flood costs no Vault round trips; short enough
# that a peer instance picks up a change even if the settings event is lost.
PUBLIC_KEY_TTL = 60.0
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class PublicKeyCache:
    """The interactions public key, read from Vault at most once per TTL.

    "Not configured" is cached as well (empty string), so the fail-closed 503
    path is just as cheap as the verified one.
    """

    def __init__(self, ctx, ttl: float = PUBLIC_KEY_TTL) -> None:
        self._ctx = ctx
        self._ttl = ttl
        self._value: str | None = None
        self._read_at = 0.0
        self._lock: asyncio.Lock | None = None
        self._generation = 0

    def invalidate(self) -> None:
        """Forget the cached key (``plugin:settings_changed``)."""
        self._value = None
        self._generation += 1

    async def get(self) -> str:
        """The configured key as hex, or ``""`` when none (valid) is configured."""
        if self._value is not None and time.monotonic() - self._read_at < self._ttl:
            return self._value
        if self._lock is None:
            self._lock = asyncio.Lock()
        # One reader at a time: a burst on a cold cache triggers a single Vault
        # read, everybody else waits for it and takes the cached result.
        async with self._lock:
            if self._value is not None and time.monotonic() - self._read_at < self._ttl:
                return self._value
            generation = self._generation
            try:
                # Vault reads are synchronous (hvac) — never on the event loop.
                value = await asyncio.to_thread(self._read)
            except Exception as exc:
                log.warning(f"Discord Notifier: could not read the interactions public key ({exc!s:.120})")
                return self._value or ""
            # A change announced while we were reading wins: do not cache the
            # value that may predate it.
            if generation == self._generation:
                self._value, self._read_at = value, time.monotonic()
            return value

    def _read(self) -> str:
        raw = None
        getter = getattr(self._ctx, "get_setting", None)
        if callable(getter):
            raw = getter(PUBLIC_KEY_SETTING, None)
        if not raw:
            raw = self._ctx.get_secret(LEGACY_PUBLIC_KEY_SECRET)
        key = str(raw or "").strip()
        if key and not _PUBLIC_KEY_RE.match(key):
            log.warning(
                "Discord Notifier: the configured interactions public key is not 64 hex characters — "
                "interactions stay disabled."
            )
            return ""
        return key


class DiscordNotifierService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = MessageStore(ctx)
        self.public_key = PublicKeyCache(ctx)

    async def bootstrap(self) -> bool:
        """Create the table off the event loop; ``False`` (logged) on failure."""
        try:
            await asyncio.to_thread(self.store.bootstrap)
            return True
        except Exception as exc:
            log = getattr(self.ctx, "log", None)
            if log is not None:
                log.error(f"Discord Notifier: DB init failed — {exc}")
            return False

    async def tracked_messages(self) -> int | None:
        """Number of tracked Discord messages (``None`` without a database)."""
        return await asyncio.to_thread(self.store.count)
