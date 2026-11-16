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

from .message_store import _PRUNE_INTERVAL, MessageStore

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

# Lease that elects the one instance running the retention pass.
PRUNE_LEASE = "discord:prune"
# First pass shortly after start (the DB bootstrap has finished by then), then
# every _PRUNE_INTERVAL.
_PRUNE_FIRST_DELAY = 120.0
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
        self._prune_task: asyncio.Task | None = None

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

    # ── retention (exactly one instance, independent of who sends) ────────────

    async def start_pruner(self) -> None:
        """Lease acquired (or single instance) → prune periodically in the background."""
        task = self._prune_task
        if task is not None and not task.done():
            return
        create_task = getattr(self.ctx, "create_task", None)
        coro = self._prune_loop()
        # ctx.create_task ties the task to the plugin (cancelled on unload).
        self._prune_task = create_task(coro, name=PRUNE_LEASE) if callable(create_task) \
            else asyncio.get_running_loop().create_task(coro)

    async def stop_pruner(self) -> None:
        """Lease lost / plugin unloaded → stop at once; a peer takes over."""
        task, self._prune_task = self._prune_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _prune_loop(self) -> None:
        delay = _PRUNE_FIRST_DELAY
        while True:
            await asyncio.sleep(delay)
            delay = _PRUNE_INTERVAL
            await self.prune_once()

    async def prune_once(self) -> int:
        """One retention pass off the event loop; never raises."""
        try:
            return await asyncio.to_thread(self.store.prune_expired)
        except Exception as exc:  # one failed pass must not end the loop
            log.warning(f"Discord Notifier: retention pass failed ({exc!s:.160})")
            return 0

    async def tracked_messages(self) -> int | None:
        """Number of tracked Discord messages (``None`` without a database)."""
        return await asyncio.to_thread(self.store.count)
