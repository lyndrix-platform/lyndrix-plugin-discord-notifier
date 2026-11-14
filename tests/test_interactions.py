"""Inbound Discord interactions: signature handling stays as it was, component
interactions are refused without a linked Lyndrix user and nothing secret is
handed to the gateway/bus (audit DN-03).

PyNaCl is a plugin dependency that is not installed on every test host, so the
Ed25519 primitive is replaced by a keyed stand-in here; one test at the end runs
the real verification where PyNaCl is available."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import router as router_mod
from app.logic.adapter import DiscordGatewayAdapter

_CORR = "123e4567-e89b-12d3-a456-426614174000"
_INTERACTION_TOKEN = "dummy-interaction-token-not-real"


class _Ctx:
    def __init__(self, secrets=None):
        self.log = logging.getLogger("discord-interactions-test")
        self.secrets = dict(secrets or {})
        self.reads = 0

    def get_secret(self, key):
        self.reads += 1
        return self.secrets.get(key)

    def set_secret(self, key, value):
        self.secrets[key] = value
        return True


class _Gateway:
    def __init__(self):
        self.forwarded = []

    async def handle_incoming(self, provider_id, raw):
        self.forwarded.append((provider_id, raw))


def _component(user_id="42"):
    return {
        "id": "9001", "type": 3, "token": _INTERACTION_TOKEN, "application_id": "77",
        "guild_id": "1", "channel_id": "2", "message": {"id": "555"},
        "data": {"custom_id": f"approve:{_CORR}", "component_type": 2},
        "member": {"user": {"id": user_id}, "roles": ["900"], "permissions": "8"},
    }


def _fake_signature(public_key_hex, timestamp, body):
    import hashlib

    return hashlib.sha256(public_key_hex.encode() + timestamp.encode() + body).hexdigest()


@pytest.fixture()
def bench(monkeypatch):
    """(client, adapter, ctx, gateway, sign) around the real interaction router."""
    key = "aa" * 32
    ctx = _Ctx({"discord_public_key": key})

    def _fake_verify(public_key_hex, timestamp, body, signature_hex):
        return signature_hex == _fake_signature(public_key_hex, timestamp, body)

    monkeypatch.setattr(router_mod, "_verify_discord_signature", _fake_verify)
    adapter = DiscordGatewayAdapter(ctx)
    gateway = _Gateway()
    monkeypatch.setattr(router_mod, "messaging_gateway", gateway)
    app = FastAPI()
    app.include_router(router_mod.build_interaction_router(adapter))

    def sign(payload, *, timestamp=None, key=key):
        body = json.dumps(payload).encode()
        ts = str(int(timestamp if timestamp is not None else time.time()))
        return {
            "content": body,
            "headers": {"X-Signature-Ed25519": _fake_signature(key, ts, body),
                        "X-Signature-Timestamp": ts, "Content-Type": "application/json"},
        }

    return TestClient(app), adapter, ctx, gateway, sign


# --- unchanged: signature verification and PING --------------------------------------

def test_ping_is_answered(bench):
    client, _, _, gateway, sign = bench
    reply = client.post("/interactions", **sign({"type": 1}))
    assert reply.status_code == 200 and reply.json() == {"type": 1}
    assert gateway.forwarded == []


def test_bad_signature_and_stale_timestamp_are_rejected(bench):
    client, _, _, gateway, sign = bench
    forged = sign(_component(), key="bb" * 32)
    assert client.post("/interactions", **forged).status_code == 401
    assert client.post("/interactions", content=b"{}").status_code == 401
    assert client.post("/interactions", **sign({"type": 1}, timestamp=time.time() - 3600)).status_code == 401
    assert gateway.forwarded == []


def test_missing_public_key_fails_closed(bench):
    client, _, ctx, gateway, sign = bench
    ctx.secrets.clear()
    assert client.post("/interactions", **sign({"type": 1})).status_code == 503
    assert gateway.forwarded == []


# --- DN-03: authorization ------------------------------------------------------------

def test_component_interaction_by_unlinked_user_is_refused_ephemerally(bench):
    client, _, _, gateway, sign = bench
    reply = client.post("/interactions", **sign(_component()))
    assert reply.status_code == 200
    body = reply.json()
    assert body["type"] == 4 and body["data"]["flags"] == 64     # ephemeral message
    assert "not linked" in body["data"]["content"]
    assert gateway.forwarded == []                                # no action is triggered


def test_no_discord_user_is_linked_by_default(bench):
    _, adapter, _, _, _ = bench
    assert adapter.lyndrix_user_for("42") is None
    # Defence in depth: the adapter refuses as well if something bypasses the route.
    assert asyncio.run(adapter.handle_incoming(_component())) is None


def test_linked_user_is_forwarded_without_token_or_member(bench, monkeypatch):
    client, adapter, _, gateway, sign = bench
    monkeypatch.setattr(adapter, "lyndrix_user_for", lambda uid: "alice" if uid == "42" else None)

    assert client.post("/interactions", **sign(_component())).json() == {"type": 6}
    assert client.post("/interactions", **sign(_component(user_id="666"))).json()["type"] == 4
    assert len(gateway.forwarded) == 1

    inbound = asyncio.run(adapter.handle_incoming(_component()))
    on_the_bus = json.dumps(inbound.model_dump(mode="json"))
    assert _INTERACTION_TOKEN not in on_the_bus
    assert "roles" not in on_the_bus and "permissions" not in on_the_bus
    assert inbound.user_id == "42" and inbound.metadata["lyndrix_user"] == "alice"
    assert inbound.metadata["raw_interaction"] == {
        "id": "9001", "type": 3, "application_id": "77", "guild_id": "1", "channel_id": "2",
        "message_id": "555", "custom_id": f"approve:{_CORR}", "component_type": 2,
    }


def test_real_ed25519_verification():
    signing = pytest.importorskip("nacl.signing")
    key = signing.SigningKey.generate()
    public = key.verify_key.encode().hex()
    body, ts = b'{"type":1}', "1700000000"
    good = key.sign(ts.encode() + body).signature.hex()
    assert router_mod._verify_discord_signature(public, ts, body, good) is True
    assert router_mod._verify_discord_signature(public, ts, body + b" ", good) is False
    assert router_mod._verify_discord_signature("zz", ts, body, good) is False
