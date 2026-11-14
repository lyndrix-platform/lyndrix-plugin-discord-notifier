"""Outbound delivery rules (audit DN-01, DN-02, DN-07): a failed edit never
turns into a new message unless Discord says the message is gone, a rate-limit
wait never outlives the gateway's send deadline, and concurrent first sends of
one notification create exactly one message. In-memory SQLite, no network."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import update

from app.logic import adapter as adapter_mod
from app.logic import message_store as store_mod
from app.logic.adapter import DiscordGatewayAdapter
from app.logic.message_store import BUSY, CLAIMED, EXISTING, PENDING_PREFIX, MessageStore
from app.model.models import DiscordMessage
from core.api import OutboundMessage

from .test_message_store import _Ctx, _no_gateway_env, _store, db  # noqa: F401  (shared fixtures)

_SECRETS = {"bot_token": "dummy-token-not-real", "channel_id": "chan"}
_MSG_URL = "https://discord.com/api/v10/channels/chan/messages"


class _Resp:
    def __init__(self, status, data=None, headers=None):
        self.status_code = status
        self._data = data or {}
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._data


class _Clock:
    """Replaces the adapter's ``time``: sleeping advances the clock instantly."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Discord:
    """Scripted Discord: records calls, answers from per-method queues."""

    def __init__(self, monkeypatch, *, post=None, patch=None):
        self.calls = []
        self._answers = {"POST": list(post or []), "PATCH": list(patch or [])}
        self._next_id = 100
        monkeypatch.setattr(adapter_mod.requests, "post", lambda url, **kw: self._call("POST", url, kw))
        monkeypatch.setattr(adapter_mod.requests, "patch", lambda url, **kw: self._call("PATCH", url, kw))

    def _call(self, method, url, kw):
        self.calls.append((method, url, kw))
        queue = self._answers[method]
        answer = queue.pop(0) if queue else None
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if method == "PATCH":
            return _Resp(200)
        self._next_id += 1
        return _Resp(200, {"id": str(self._next_id)})

    @property
    def methods(self):
        return [c[0] for c in self.calls]


def _message(nid="n-1"):
    return OutboundMessage(title="t", body="b", metadata={"notification_id": nid})


def _adapter(store=None):
    return DiscordGatewayAdapter(_Ctx(_SECRETS), store=store or _store())


@pytest.fixture()
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(adapter_mod, "time", fake)
    return fake


# --- DN-01: edit failures ------------------------------------------------------------

@pytest.mark.parametrize(
    "failure",
    [_Resp(429, {"retry_after": 60}), _Resp(500), _Resp(502), _Resp(503), _Resp(403),
     TimeoutError("read timed out")],
    ids=["429", "500", "502", "503", "403", "timeout"],
)
def test_failed_edit_never_posts_a_new_message(db, monkeypatch, clock, failure):
    adapter = _adapter()
    discord = _Discord(monkeypatch)
    first = adapter._send_sync(_message())
    assert first is not None

    discord._answers["PATCH"].append(failure)
    assert adapter._send_sync(_message()) is None           # retryable failure
    assert discord.methods == ["POST", "PATCH"]             # no second POST
    assert _store().get("discord", "n-1", "chan") == first  # mapping kept

    # The gateway's retry then edits the original message.
    assert adapter._send_sync(_message()) == first
    assert discord.methods == ["POST", "PATCH", "PATCH"]


def test_edit_of_a_deleted_message_reposts_once(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch, patch=[_Resp(404, {"code": 10008})])
    first = adapter._send_sync(_message())
    second = adapter._send_sync(_message())
    assert discord.methods == ["POST", "PATCH", "POST"]
    assert second not in (None, first)
    assert _store().get("discord", "n-1", "chan") == second


def test_rate_limited_edit_is_retried_in_place_when_it_fits(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch, patch=[_Resp(429, {"retry_after": 1.5})])
    first = adapter._send_sync(_message())
    assert adapter._send_sync(_message()) == first
    assert discord.methods == ["POST", "PATCH", "PATCH"]
    assert clock.slept == [1.5]


# --- DN-02: the 429 wait is bounded by the send deadline -----------------------------

def test_retry_after_beyond_the_deadline_returns_without_sleeping(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch, post=[_Resp(429, {"retry_after": 30})])
    assert adapter._send_sync(_message()) is None
    assert clock.slept == []                    # the gateway's retry worker backs off instead
    assert discord.methods == ["POST"]
    # Nothing was created → the claim is released and the retry posts normally.
    assert adapter._send_sync(_message()) is not None
    assert discord.methods == ["POST", "POST"]


def test_retry_after_header_is_honoured_within_the_deadline(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch, post=[_Resp(429, headers={"Retry-After": "3"})])
    assert adapter._send_sync(_message()) is not None
    assert clock.slept == [3.0] and discord.methods == ["POST", "POST"]


def test_webhook_path_honours_the_deadline_too(db, monkeypatch, clock):
    adapter = DiscordGatewayAdapter(
        _Ctx({"webhook_url": "https://discord.com/api/webhooks/1/dummy-token-not-real"}), store=_store())
    discord = _Discord(monkeypatch, post=[_Resp(429, {"retry_after": 600})])
    assert adapter._send_sync(OutboundMessage(title="t", body="b")) is None
    assert clock.slept == [] and discord.methods == ["POST"]


def test_no_request_is_started_after_the_deadline(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch)
    assert adapter._send_sync(_message(), deadline=clock.now + 0.5) is None
    assert discord.calls == []
    assert _store().count() == 0     # claim released: nothing was sent


def test_request_timeouts_fit_the_remaining_budget(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch)
    adapter._send_sync(_message(), deadline=clock.now + 6.0)
    connect, read = discord.calls[0][2]["timeout"]
    assert connect + read <= 6.0


def test_send_deadline_lies_before_the_gateway_timeout(db, monkeypatch):
    import asyncio

    adapter = _adapter()
    seen = {}
    monkeypatch.setattr(adapter, "_send_sync", lambda message, deadline: seen.setdefault("d", deadline))
    before = adapter_mod.time.monotonic()
    asyncio.run(adapter.send(_message()))
    assert seen["d"] - before < adapter.send_timeout


def test_request_errors_are_logged_without_the_url(db, monkeypatch, clock, caplog):
    secret_url = "https://discord.com/api/webhooks/1/dummy-token-not-real"
    adapter = DiscordGatewayAdapter(_Ctx({"webhook_url": secret_url}), store=_store())
    _Discord(monkeypatch, post=[ConnectionError(f"Max retries exceeded with url: {secret_url}")])
    with caplog.at_level("DEBUG"):
        assert adapter._send_sync(OutboundMessage(title="t", body="b")) is None
    assert "dummy-token-not-real" not in caplog.text


# --- DN-07 / DN-02: one message per notification -------------------------------------

def test_claim_lets_exactly_one_instance_create(db):
    a, b = _store(), _store()
    mine = a.claim("discord", "n-1", "chan")
    assert mine.state == CLAIMED and mine.nonce
    assert b.claim("discord", "n-1", "chan").state == BUSY
    assert b.get("discord", "n-1", "chan") is None           # a placeholder is not a message id

    a.put("discord", "n-1", "chan", "111")
    theirs = b.claim("discord", "n-1", "chan")
    assert (theirs.state, theirs.message_id) == (EXISTING, "111")
    assert a.count() == 1


def test_released_claim_can_be_claimed_again_and_never_removes_a_real_id(db):
    a, b = _store(), _store()
    mine = a.claim("discord", "n-1", "chan")
    a.release("discord", "n-1", mine.nonce)
    again = b.claim("discord", "n-1", "chan")
    assert again.state == CLAIMED
    b.put("discord", "n-1", "chan", "222")
    a.release("discord", "n-1", mine.nonce)      # late/stale release: no effect
    a.abandon("discord", "n-1", again.nonce)
    assert _store().get("discord", "n-1", "chan") == "222"


def test_stale_or_abandoned_claim_is_taken_over_with_the_same_nonce(db):
    a, b = _store(), _store()
    mine = a.claim("discord", "n-1", "chan")
    a.abandon("discord", "n-1", mine.nonce)      # outcome unknown (timeout)
    taken = b.claim("discord", "n-1", "chan")
    assert (taken.state, taken.nonce) == (CLAIMED, mine.nonce)
    assert a.claim("discord", "n-1", "chan").state == BUSY   # b's claim is fresh again

    # An owner that crashed: the claim simply ages out.
    crashed = a.claim("discord", "n-2", "chan")
    with db.SessionLocal() as s:
        s.execute(update(DiscordMessage).where(DiscordMessage.notification_id == "n-2")
                  .values(updated_at=datetime.utcnow() - timedelta(seconds=store_mod.CLAIM_TTL + 5)))
        s.commit()
    assert b.claim("discord", "n-2", "chan").nonce == crashed.nonce


def test_simultaneous_first_send_on_two_instances_posts_once(db, monkeypatch):
    """Instance B sends while A's POST is still in flight (the DN-07 double post)."""
    monkeypatch.setattr(adapter_mod, "_CLAIM_POLL", 0.01)
    a, b = _adapter(), _adapter()
    in_post, let_go = threading.Event(), threading.Event()
    posts, patches = [], []

    def _post(url, **kw):
        posts.append(kw["json"])
        in_post.set()
        assert let_go.wait(5)
        return _Resp(200, {"id": "111"})

    monkeypatch.setattr(adapter_mod.requests, "post", _post)
    monkeypatch.setattr(adapter_mod.requests, "patch", lambda url, **kw: patches.append(url) or _Resp(200))

    results = {}
    first = threading.Thread(target=lambda: results.setdefault("a", a._send_sync(_message())))
    first.start()
    assert in_post.wait(5)
    second = threading.Thread(target=lambda: results.setdefault("b", b._send_sync(_message())))
    second.start()
    second.join(0.2)
    assert second.is_alive() and len(posts) == 1     # B waits on A's claim instead of posting
    let_go.set()
    first.join(5), second.join(5)

    assert results == {"a": "111", "b": "111"}
    assert len(posts) == 1 and patches == [f"{_MSG_URL}/111"]
    assert posts[0]["enforce_nonce"] is True and 0 < len(posts[0]["nonce"]) <= 25


def test_busy_claim_that_does_not_clear_is_a_retryable_failure(db, monkeypatch, clock):
    peer = _store()
    peer.claim("discord", "n-1", "chan")             # a peer is (still) creating the message
    adapter = _adapter()
    discord = _Discord(monkeypatch)
    assert adapter._send_sync(_message()) is None
    assert discord.calls == []
    assert sum(clock.slept) <= adapter_mod._CLAIM_WAIT_MAX


def test_post_with_unknown_outcome_is_repeated_with_the_same_nonce(db, monkeypatch, clock):
    adapter = _adapter()
    discord = _Discord(monkeypatch, post=[TimeoutError("read timed out")])
    assert adapter._send_sync(_message()) is None
    msg_id = adapter._send_sync(_message())          # the gateway retry
    assert msg_id is not None
    first, second = (c[2]["json"]["nonce"] for c in discord.calls)
    assert first == second                           # Discord de-duplicates on its side
    assert _store().get("discord", "n-1", "chan") == msg_id


def test_gateway_retry_while_the_worker_is_still_sending_does_not_post_twice(db, monkeypatch):
    """Same process: the retry waits for the running send, then edits."""
    adapter = _adapter()
    in_post, let_go = threading.Event(), threading.Event()
    posts, patches = [], []

    def _post(url, **kw):
        posts.append(url)
        in_post.set()
        assert let_go.wait(5)
        return _Resp(200, {"id": "111"})

    monkeypatch.setattr(adapter_mod.requests, "post", _post)
    monkeypatch.setattr(adapter_mod.requests, "patch", lambda url, **kw: patches.append(url) or _Resp(200))

    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("worker", adapter._send_sync(_message())))
    worker.start()
    assert in_post.wait(5)
    retry = threading.Thread(target=lambda: results.setdefault("retry", adapter._send_sync(_message())))
    retry.start()
    retry.join(0.2)
    assert retry.is_alive() and len(posts) == 1
    let_go.set()
    worker.join(5), retry.join(5)
    assert results == {"worker": "111", "retry": "111"} and len(posts) == 1 and len(patches) == 1
    assert adapter._inflight._locks == {}            # no lock left behind


def test_store_without_db_still_serves_the_adapter(monkeypatch, clock):
    from types import SimpleNamespace

    from app.model import database

    monkeypatch.setattr(database, "db_instance",
                        SimpleNamespace(engine=None, SessionLocal=None, is_connected=False))
    adapter = DiscordGatewayAdapter(_Ctx(_SECRETS), store=MessageStore(_Ctx()))
    discord = _Discord(monkeypatch)
    first = adapter._send_sync(_message())
    assert adapter._send_sync(_message()) == first
    assert discord.methods == ["POST", "PATCH"]


def test_pending_placeholder_fits_the_column_and_discords_nonce_limit(db):
    claim = _store().claim("discord", "n-1", "chan")
    assert len(PENDING_PREFIX + claim.nonce) <= DiscordMessage.discord_message_id.type.length
    assert len(claim.nonce) <= 25


# --- DN-05: config reads are cached --------------------------------------------------

def test_health_and_send_do_not_read_vault_every_time(db, monkeypatch, clock):
    ctx = _Ctx(_SECRETS)
    adapter = DiscordGatewayAdapter(ctx, store=_store())
    _Discord(monkeypatch)

    assert adapter._health_sync() is True
    cold = ctx.reads                    # bot_token, channel_id, webhook_url, bot_name
    assert cold == 4
    for _ in range(20):
        assert adapter._health_sync() is True
        assert adapter._send_sync(_message()) is not None
    assert ctx.reads == cold

    clock.now += adapter_mod._VAULT_CACHE_TTL + 1
    adapter._health_sync()
    assert ctx.reads == cold + 4        # refreshed after the TTL


def test_rejected_token_drops_the_config_cache(db, monkeypatch, clock):
    ctx = _Ctx(_SECRETS)
    adapter = DiscordGatewayAdapter(ctx, store=_store())
    _Discord(monkeypatch, post=[_Resp(401)])
    assert adapter._send_sync(_message()) is None
    ctx.secrets["bot_token"] = "dummy-rotated-token-not-real"
    reads = ctx.reads
    assert adapter._send_sync(_message()) is not None
    assert ctx.reads > reads            # the rotated token was re-read


def test_saved_config_is_visible_immediately(db, clock):
    ctx = _Ctx({})
    adapter = DiscordGatewayAdapter(ctx, store=_store())
    assert adapter._health_sync() is False
    assert adapter.save_config("webhook_url", "https://discord.com/api/webhooks/1/dummy-token-not-real")
    assert adapter._health_sync() is True
