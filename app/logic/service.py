"""DiscordNotifierService — owns the shared message store and its lifecycle.

All delivery goes through ``DiscordGatewayAdapter.send()``; this service only
bootstraps the cluster-shared ``discord_messages`` table and reports its size.
(The former per-process ``notifications_sent`` counter was never incremented
and meant nothing behind a load balancer — it was dropped in 1.2.0.)
"""
from __future__ import annotations

import asyncio

from .message_store import MessageStore


class DiscordNotifierService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.store = MessageStore(ctx)

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
