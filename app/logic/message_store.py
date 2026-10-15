"""Shared notification_id → Discord message id store.

The database table ``discord_messages`` is the source of truth so an edit/upsert
of a notification handled by *any* core instance (or after a restart) PATCHes
the message an earlier send created instead of posting a duplicate. A small
per-process cache sits in front to spare the DB on rapid live updates.

All methods are blocking (SQLAlchemy) — the adapter calls them from its
``asyncio.to_thread`` send worker; async callers must offload them too.
Without a database (older core, DB down, tables not bootstrapped yet) the store
degrades to the in-memory cache, i.e. the pre-1.2 per-instance behaviour.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from core.api import get_logger

from ..model.database import db_ready, ensure_tables, get_session, retry_write
from ..model.models import DiscordMessage

log = get_logger("Plugin:Discord:MessageStore")

DEFAULT_RETENTION_DAYS = 30
# Cache entries are trusted this long; after that the DB is asked again, so a
# mapping a peer replaced (edit failed → new message) is picked up quickly.
_CACHE_TTL = 60.0
_CACHE_MAX = 512
# The leader prunes at most this often (per instance, piggy-backed on writes).
_PRUNE_INTERVAL = 3600.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MessageStore:
    def __init__(self, ctx=None) -> None:
        self._ctx = ctx
        self._ready = False
        self._lock = threading.Lock()
        # (provider_id, notification_id) → (channel_id, message_id, cached_at)
        self._cache: OrderedDict[tuple[str, str], tuple[str | None, str, float]] = OrderedDict()
        self._last_prune = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready and db_ready()

    def bootstrap(self) -> None:
        """Create the table (blocking; idempotent; under the bootstrap lock)."""
        ensure_tables()
        self._ready = True

    # ── cache ────────────────────────────────────────────────────────────────

    def _cache_get(self, key: tuple[str, str], channel_id: str | None) -> str | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            cached_channel, msg_id, at = entry
            if time.monotonic() - at > _CACHE_TTL or cached_channel != channel_id:
                self._cache.pop(key, None)
                return None
            return msg_id

    def _cache_put(self, key: tuple[str, str], channel_id: str | None, msg_id: str) -> None:
        with self._lock:
            self._cache[key] = (channel_id, msg_id, time.monotonic())
            self._cache.move_to_end(key)
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)

    # ── API ──────────────────────────────────────────────────────────────────

    def get(self, provider_id: str, notification_id: str, channel_id: str | None) -> str | None:
        """Message id previously created for this notification in *channel_id*."""
        key = (provider_id, notification_id)
        cached = self._cache_get(key, channel_id)
        if cached or not self.is_ready:
            return cached
        try:
            with get_session() as s:
                row = s.get(DiscordMessage, {"notification_id": notification_id, "provider_id": provider_id})
                if row is None or row.channel_id != channel_id:
                    return None
                msg_id = row.discord_message_id
        except Exception as exc:
            log.warning(f"Discord store: lookup failed ({exc!s:.160}) — treating as new message")
            return None
        self._cache_put(key, channel_id, msg_id)
        return msg_id

    def put(self, provider_id: str, notification_id: str, channel_id: str | None, message_id: str) -> None:
        """Remember (insert-or-update) the message id for this notification."""
        key = (provider_id, notification_id)
        self._cache_put(key, channel_id, message_id)
        if not self.is_ready:
            return

        def _upsert() -> None:
            now = _utcnow()
            with get_session() as s:
                row = s.get(DiscordMessage, {"notification_id": notification_id, "provider_id": provider_id})
                if row is None:
                    s.add(DiscordMessage(
                        notification_id=notification_id, provider_id=provider_id,
                        channel_id=channel_id, discord_message_id=message_id,
                        created_at=now, updated_at=now,
                    ))
                else:
                    row.channel_id = channel_id
                    row.discord_message_id = message_id
                    row.updated_at = now
                s.commit()

        try:
            try:
                retry_write(_upsert)
            except IntegrityError:
                # A peer inserted the same key between our read and our insert —
                # the second attempt finds the row and updates it.
                retry_write(_upsert)
        except Exception as exc:
            log.warning(f"Discord store: could not persist message id ({exc!s:.160})")
            return
        self.maybe_prune()

    def forget(self, provider_id: str, notification_id: str) -> None:
        """Drop a mapping whose Discord message no longer accepts edits."""
        key = (provider_id, notification_id)
        with self._lock:
            self._cache.pop(key, None)
        if not self.is_ready:
            return

        def _delete() -> None:
            with get_session() as s:
                s.execute(delete(DiscordMessage).where(
                    DiscordMessage.notification_id == notification_id,
                    DiscordMessage.provider_id == provider_id,
                ))
                s.commit()

        try:
            retry_write(_delete)
        except Exception as exc:
            log.debug(f"Discord store: forget failed ({exc!s:.160})")

    def count(self) -> int | None:
        """Number of tracked messages (``None`` without a database)."""
        if not self.is_ready:
            return None
        with get_session() as s:
            return int(s.scalar(select(func.count()).select_from(DiscordMessage)) or 0)

    # ── retention ────────────────────────────────────────────────────────────

    def _retention_days(self) -> int:
        getter = getattr(self._ctx, "get_setting", None)
        raw = None
        if callable(getter):
            try:
                raw = getter("message_retention_days", DEFAULT_RETENTION_DAYS)
            except Exception:
                raw = None
        try:
            days = int(raw if raw not in (None, "") else DEFAULT_RETENTION_DAYS)
        except (TypeError, ValueError):
            days = DEFAULT_RETENTION_DAYS
        return max(days, 1)

    def maybe_prune(self) -> int:
        """Leader-only, throttled retention pass (piggy-backed on writes)."""
        if time.monotonic() - self._last_prune < _PRUNE_INTERVAL:
            return 0
        # Single mode: the only instance is always the leader.
        if not getattr(self._ctx, "is_leader", True):
            return 0
        self._last_prune = time.monotonic()
        return self.prune(self._retention_days())

    def prune(self, days: int) -> int:
        """Delete mappings not touched for *days* days; returns the row count."""
        if not self.is_ready:
            return 0
        cutoff = _utcnow() - timedelta(days=days)

        def _delete() -> int:
            with get_session() as s:
                res = s.execute(delete(DiscordMessage).where(DiscordMessage.updated_at < cutoff))
                s.commit()
                return int(res.rowcount or 0)

        try:
            removed = retry_write(_delete)
        except Exception as exc:
            log.warning(f"Discord store: prune failed ({exc!s:.160})")
            return 0
        if removed:
            log.info(f"Discord store: pruned {removed} message mapping(s) older than {days} day(s)")
        return removed
