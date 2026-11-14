"""Shared notification_id → Discord message id store.

The database table ``discord_messages`` is the source of truth so an edit/upsert
of a notification handled by *any* core instance (or after a restart) PATCHes
the message an earlier send created instead of posting a duplicate. A small
per-process cache sits in front to spare the DB on rapid live updates.

First sends are serialized through the same row (:meth:`MessageStore.claim`):
before an instance POSTs it inserts a *pending* placeholder
(``discord_message_id = "pending:<nonce>"``). The unique key lets exactly one
instance win; the others see the claim, wait for the real id and edit instead of
posting a second message. A claim whose owner died is taken over after
``CLAIM_TTL`` seconds, reusing its nonce so Discord can de-duplicate the POST.

All methods are blocking (SQLAlchemy) — the adapter calls them from its
``asyncio.to_thread`` send worker; async callers must offload them too.
Without a database (older core, DB down, tables not bootstrapped yet) the store
degrades to the in-memory cache, i.e. the pre-1.2 per-instance behaviour.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
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
# Retention pass interval (the service's leased background task, see
# DiscordNotifierService.start_pruner).
_PRUNE_INTERVAL = 3600.0

# Placeholder value of ``discord_message_id`` while an instance is creating the
# message. The column is 32 chars wide and Discord accepts a nonce of up to 25:
# 8 (prefix) + 24 (nonce) fits both.
PENDING_PREFIX = "pending:"
_NONCE_LEN = 24
# A pending claim is honoured this long. It must outlive one complete send
# (the adapter's send deadline is ~20 s); after that the owner is presumed dead
# and the next sender takes the claim over.
CLAIM_TTL = 60.0

CLAIMED = "claimed"    # caller owns the slot → POST, then put() / release() / abandon()
EXISTING = "existing"  # a message exists → PATCH ``message_id``
BUSY = "busy"          # another sender is creating it right now → retry later


@dataclass(frozen=True)
class Claim:
    state: str
    message_id: str | None = None
    # Sent to Discord as the message nonce (with ``enforce_nonce``): a repeated
    # POST for the same claim returns the first message instead of a new one.
    nonce: str | None = None


def _new_nonce() -> str:
    return uuid.uuid4().hex[:_NONCE_LEN]


def is_pending(message_id: str | None) -> bool:
    return bool(message_id) and str(message_id).startswith(PENDING_PREFIX)


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
                # A pending placeholder is not a message id — see claim().
                if row is None or row.channel_id != channel_id or is_pending(row.discord_message_id):
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

    def claim(self, provider_id: str, notification_id: str, channel_id: str | None) -> Claim:
        """Reserve the right to create the message for this notification.

        Claim-before-send: the placeholder row is inserted *before* the POST, so
        two senders (two instances, or a gateway retry racing a worker that is
        still sending) cannot both create a message — the unique key decides.

        Without a database the store cannot coordinate with peers and behaves
        like the pre-1.2 per-instance map (``CLAIMED`` unless cached).
        """
        key = (provider_id, notification_id)
        cached = self._cache_get(key, channel_id)
        if cached:
            return Claim(EXISTING, message_id=cached)
        if not self.is_ready:
            return Claim(CLAIMED, nonce=_new_nonce())

        def _tx() -> Claim:
            now = _utcnow()
            with get_session() as s:
                row = s.get(DiscordMessage, {"notification_id": notification_id, "provider_id": provider_id})
                if row is None:
                    nonce = _new_nonce()
                    s.add(DiscordMessage(
                        notification_id=notification_id, provider_id=provider_id,
                        channel_id=channel_id, discord_message_id=PENDING_PREFIX + nonce,
                        created_at=now, updated_at=now,
                    ))
                    s.commit()  # IntegrityError → a peer claimed first (handled below)
                    return Claim(CLAIMED, nonce=nonce)

                current, seen_at = row.discord_message_id, row.updated_at
                same_channel = row.channel_id == channel_id
                pending = is_pending(current)
                if same_channel and not pending:
                    return Claim(EXISTING, message_id=current)
                if same_channel and pending and (now - seen_at).total_seconds() < CLAIM_TTL:
                    return Claim(BUSY)
                # Take over: an expired claim (owner crashed or never learned the
                # outcome — keep its nonce so Discord de-duplicates a POST that
                # did arrive) or a message that lives in a channel no longer used.
                nonce = current[len(PENDING_PREFIX):] if (pending and same_channel) else _new_nonce()
                taken = s.execute(
                    update(DiscordMessage)
                    .where(
                        DiscordMessage.notification_id == notification_id,
                        DiscordMessage.provider_id == provider_id,
                        # Conditional on what we read: only one of several
                        # concurrent takers changes the row.
                        DiscordMessage.discord_message_id == current,
                        DiscordMessage.updated_at == seen_at,
                    )
                    .values(channel_id=channel_id, discord_message_id=PENDING_PREFIX + nonce, updated_at=now)
                )
                s.commit()
                return Claim(CLAIMED, nonce=nonce) if taken.rowcount == 1 else Claim(BUSY)

        try:
            try:
                return retry_write(_tx)
            except IntegrityError:
                # Lost the insert race — the second pass reads the winner's row.
                return retry_write(_tx)
        except Exception as exc:
            # A notification is worth more than de-duplication: with the DB
            # unusable (often exactly what the alert is about) post anyway.
            log.warning(f"Discord store: claim failed ({exc!s:.160}) — sending without coordination")
            return Claim(CLAIMED, nonce=_new_nonce())

    def release(self, provider_id: str, notification_id: str, nonce: str | None) -> None:
        """Give a claim back after a POST that definitely created nothing."""
        self._settle_claim(provider_id, notification_id, nonce, expire=False)

    def abandon(self, provider_id: str, notification_id: str, nonce: str | None) -> None:
        """Expire a claim whose POST has an unknown outcome (timeout, reset).

        The row stays, already stale: the next sender takes it over at once and
        repeats the POST with the *same* nonce, so a message Discord did create
        is returned instead of duplicated.
        """
        self._settle_claim(provider_id, notification_id, nonce, expire=True)

    def _settle_claim(self, provider_id: str, notification_id: str, nonce: str | None, *, expire: bool) -> None:
        if not nonce or not self.is_ready:
            return
        mine = (
            DiscordMessage.notification_id == notification_id,
            DiscordMessage.provider_id == provider_id,
            # Only our own placeholder — never a peer's claim or a real id.
            DiscordMessage.discord_message_id == PENDING_PREFIX + nonce,
        )

        def _tx() -> None:
            with get_session() as s:
                if expire:
                    stale = _utcnow() - timedelta(seconds=CLAIM_TTL + 1)
                    s.execute(update(DiscordMessage).where(*mine).values(updated_at=stale))
                else:
                    s.execute(delete(DiscordMessage).where(*mine))
                s.commit()

        try:
            retry_write(_tx)
        except Exception as exc:  # the claim then simply expires after CLAIM_TTL
            log.debug(f"Discord store: could not settle claim ({exc!s:.160})")

    def forget(self, provider_id: str, notification_id: str, message_id: str | None = None) -> None:
        """Drop a mapping whose Discord message no longer exists.

        With *message_id* the row is only removed while it still points at that
        message, so a mapping (or claim) a peer wrote in the meantime survives.
        """
        key = (provider_id, notification_id)
        with self._lock:
            self._cache.pop(key, None)
        if not self.is_ready:
            return

        def _delete() -> None:
            with get_session() as s:
                stmt = delete(DiscordMessage).where(
                    DiscordMessage.notification_id == notification_id,
                    DiscordMessage.provider_id == provider_id,
                )
                if message_id is not None:
                    stmt = stmt.where(DiscordMessage.discord_message_id == message_id)
                s.execute(stmt)
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

    def prune_expired(self) -> int:
        """One retention pass with the configured retention (blocking: Vault + DB)."""
        self._last_prune = time.monotonic()
        return self.prune(self._retention_days())

    def maybe_prune(self) -> int:
        """Leader-only, throttled retention pass.

        No longer called from put(): pruning piggy-backed on the leader's own
        sends never ran when another instance did the sending. Kept for callers
        without the leased task (see DiscordNotifierService.start_pruner).
        """
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
