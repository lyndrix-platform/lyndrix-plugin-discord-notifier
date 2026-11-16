"""SQLAlchemy models for the Discord Notifier plugin."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


class DiscordMessage(_Base):
    """notification_id → Discord message id, shared by every core instance.

    Lets any instance (or a restarted one) PATCH the message an earlier send
    created instead of posting a duplicate. Keyed by (notification_id,
    provider_id) because one notification can fan out to several Discord
    adapter instances (``discord``, ``discord:ops``, …), each with its own
    message.
    """

    __tablename__ = "discord_messages"

    notification_id = Column(String(128), primary_key=True)
    provider_id = Column(String(64), primary_key=True)
    # Bot API target the message lives in; a changed channel invalidates the id.
    channel_id = Column(String(64), nullable=True)
    discord_message_id = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), index=True)
