"""Cluster readiness (1.2.0): the notification → Discord message map is shared
through the database, so an update handled by another instance (or after a
restart) edits the original message. Runs against in-memory SQLite."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.logic import adapter as adapter_mod
from app.logic.adapter import DiscordGatewayAdapter
from app.logic.message_store import MessageStore
from app.model import database
from app.model.models import DiscordMessage
from core.api import OutboundMessage


class _Ctx:
    def __init__(self, secrets=None, *, leader=True, settings=None):
        self.log = logging.getLogger("discord-test")
        self.secrets = dict(secrets or {})
        self.settings = dict(settings or {})
        self.is_leader = leader
        self.reads = 0

    def get_secret(self, key):
        self.reads += 1
        return self.secrets.get(key)

    def set_secret(self, key, value):
        self.secrets[key] = value
        return True

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


class _Resp:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data or {}
        self.text = ""

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def _no_gateway_env(monkeypatch):
    """The dev container carries real LYNDRIX_GATEWAY_* vars — never use them."""
    import os

    for key in list(os.environ):
        if key.startswith("LYNDRIX_GATEWAY_"):
            monkeypatch.delenv(key)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    taken: list[str] = []

    @contextmanager
    def _lock(name):
        taken.append(name)
        yield True

    fake = SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine),
                           is_connected=True, advisory_lock=_lock, taken=taken)
    monkeypatch.setattr(database, "db_instance", fake)
    return fake


def _store(ctx=None) -> MessageStore:
    store = MessageStore(ctx or _Ctx())
    store.bootstrap()
    return store


def test_bootstrap_under_lock_and_idempotent(db):
    _store()
    _store()
    assert db.taken == ["lyndrix_bootstrap", "lyndrix_bootstrap"]


def test_mapping_is_shared_between_instances(db):
    a, b = _store(), _store()
    a.put("discord", "n-1", "chan", "111")
    assert b.get("discord", "n-1", "chan") == "111"
    assert b.get("discord", "n-1", "other-chan") is None  # channel changed
    assert b.get("discord:ops", "n-1", "chan") is None    # other adapter instance
    b.put("discord", "n-1", "chan", "222")                # update, no duplicate row
    assert a.count() == 1
    a.forget("discord", "n-1")
    assert _store().get("discord", "n-1", "chan") is None  # b may serve its cache up to 60 s


def test_store_without_db_degrades_to_cache(monkeypatch):
    monkeypatch.setattr(database, "db_instance",
                        SimpleNamespace(engine=None, SessionLocal=None, is_connected=False))
    store = MessageStore(_Ctx())
    store.put("discord", "n-1", "chan", "111")
    assert store.get("discord", "n-1", "chan") == "111"
    assert store.count() is None


def test_upsert_retries_snapshot_conflict(db, monkeypatch):
    store = _store()
    store._last_prune = float("inf")  # keep the retention pass out of the count
    calls = {"n": 0}
    real = database.get_session

    @contextmanager
    def _flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("(1020, 'Record has changed since last read')")
        with real() as s:
            yield s

    monkeypatch.setattr("app.logic.message_store.get_session", _flaky)
    store.put("discord", "n-1", "chan", "111")
    monkeypatch.setattr("app.logic.message_store.get_session", real)
    assert calls["n"] == 2
    assert _store().get("discord", "n-1", "chan") == "111"


def test_prune_is_leader_only_and_respects_retention(db):
    leader = _store(_Ctx(settings={"message_retention_days": "7"}))
    standby = _store(_Ctx(leader=False))
    leader.put("discord", "old", "chan", "1")
    leader.put("discord", "new", "chan", "2")
    with db.SessionLocal() as s:
        s.execute(update(DiscordMessage).where(DiscordMessage.notification_id == "old")
                  .values(updated_at=datetime.utcnow() - timedelta(days=8)))
        s.commit()
    standby._last_prune = 0.0
    assert standby.maybe_prune() == 0
    leader._last_prune = 0.0
    assert leader.maybe_prune() == 1
    assert leader.maybe_prune() == 0  # throttled
    assert leader.count() == 1


def _message(nid: str) -> OutboundMessage:
    return OutboundMessage(title="t", body="b", metadata={"notification_id": nid})


def test_update_from_other_instance_edits_instead_of_reposting(db, monkeypatch):
    secrets = {"bot_token": "tok", "channel_id": "chan"}
    a = DiscordGatewayAdapter(_Ctx(secrets), store=_store())
    b = DiscordGatewayAdapter(_Ctx(secrets), store=_store())
    posts, patches = [], []
    monkeypatch.setattr(adapter_mod.requests, "post",
                        lambda url, **kw: posts.append(url) or _Resp(200, {"id": "111"}))
    monkeypatch.setattr(adapter_mod.requests, "patch",
                        lambda url, **kw: patches.append(url) or _Resp(200))

    assert a._send_sync(_message("n-1")) == "111"
    assert b._send_sync(_message("n-1")) == "111"
    assert len(posts) == 1
    assert patches == ["https://discord.com/api/v10/channels/chan/messages/111"]


def test_failed_edit_reposts_and_replaces_mapping(db, monkeypatch):
    secrets = {"bot_token": "tok", "channel_id": "chan"}
    store = _store()
    store.put("discord", "n-1", "chan", "gone")
    adapter = DiscordGatewayAdapter(_Ctx(secrets), store=_store())
    monkeypatch.setattr(adapter_mod.requests, "patch", lambda url, **kw: _Resp(404))
    monkeypatch.setattr(adapter_mod.requests, "post", lambda url, **kw: _Resp(200, {"id": "333"}))
    assert adapter._send_sync(_message("n-1")) == "333"
    assert _store().get("discord", "n-1", "chan") == "333"


def test_config_fields_use_warm_cache(db):
    ctx = _Ctx({"webhook_url": "https://discord.com/api/webhooks/1/x"})
    adapter = DiscordGatewayAdapter(ctx, store=_store())
    assert adapter._health_sync() is True
    reads = ctx.reads
    fields = {f.key: f for f in adapter.get_config_fields()}
    assert ctx.reads == reads  # served from the cache warmed off-thread
    assert fields["webhook_url"].current_value == "https://discord.com/api/webhooks/1/x"
    adapter.save_config("bot_name", "Ops")
    assert {f.key: f for f in adapter.get_config_fields()}["bot_name"].current_value == "Ops"
