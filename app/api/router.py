"""
FastAPI router for Discord Interaction callbacks.

Discord POSTs to this endpoint whenever a user clicks a button (or uses a
slash command) attached to a message this adapter sent.

Security: Discord signs every interaction request with an Ed25519 keypair.
We MUST verify the signature before processing.  Unauthenticated requests
MUST return HTTP 401; missing the verification entirely would allow any
attacker to fake button-click events.  When no public key is configured the
endpoint fails closed (HTTP 503) and refuses to process interactions.

Set up in Discord Developer Portal:
  Interactions Endpoint URL: https://<your-lyndrix-host>/api/plugins/lyndrix.plugin.discord/interactions
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from core.api import messaging_gateway

if TYPE_CHECKING:
    from ..logic.adapter import DiscordGatewayAdapter

log = logging.getLogger("Plugin:DiscordRouter")

# Interaction type constants
_TYPE_PING       = 1
_TYPE_COMPONENT  = 3

# Reject interactions whose signed timestamp is older than this (replay window).
_MAX_TIMESTAMP_SKEW = 300  # seconds


def build_interaction_router(adapter: "DiscordGatewayAdapter") -> APIRouter:
    """Return a FastAPI router pre-bound to *adapter*."""
    router = APIRouter()

    @router.post("/interactions")
    async def discord_interactions(request: Request) -> Response:
        """Receive and route Discord Component Interactions."""
        body      = await request.body()
        signature = request.headers.get("X-Signature-Ed25519", "")
        timestamp = request.headers.get("X-Signature-Timestamp", "")

        # Verify Ed25519 signature ----------------------------------------
        # Vault reads are synchronous (hvac) — offload to a thread so we never
        # block the event loop.
        public_key_hex = await asyncio.to_thread(
            adapter._ctx.get_secret, "discord_public_key"
        ) or ""
        # Fail closed: without a configured public key we cannot authenticate
        # the request, so refuse to process it rather than trusting it blindly.
        # TODO(agent): make the public key required config for inbound and mark
        # the adapter inbound-disabled/unhealthy while it is absent.
        if not public_key_hex:
            log.warning(
                "Discord router: 'discord_public_key' not configured — "
                "refusing interaction (signature verification unavailable)."
            )
            raise HTTPException(
                status_code=503,
                detail="Discord interactions disabled: public key not configured",
            )
        if not _verify_discord_signature(public_key_hex, timestamp, body, signature):
            log.warning("Discord router: signature verification failed.")
            raise HTTPException(status_code=401, detail="Invalid request signature")

        # Replay protection: reject stale (but validly-signed) interactions.
        if not _timestamp_is_fresh(timestamp):
            log.warning("Discord router: rejecting stale interaction timestamp.")
            raise HTTPException(status_code=401, detail="Stale request timestamp")

        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        interaction_type = payload.get("type")

        # Discord PING — must reply with type=1 immediately
        if interaction_type == _TYPE_PING:
            return JSONResponse({"type": 1})

        # Route component interactions through the gateway using this adapter's
        # actual provider_id (e.g. "discord" or "discord:ops").
        try:
            await messaging_gateway.handle_incoming(adapter.provider_id, payload)
        except Exception as exc:
            log.error("Discord router: handle_incoming raised: %s", exc)

        # Acknowledge the interaction (deferred update — no new message shown)
        return JSONResponse({"type": 6})

    return router


def _timestamp_is_fresh(timestamp: str) -> bool:
    """Return True if the signed Discord timestamp is within the replay window.

    Discord sends ``X-Signature-Timestamp`` as a Unix epoch (seconds). A
    captured, validly-signed request can otherwise be replayed indefinitely.
    """
    try:
        sent = float(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(time.time() - sent) <= _MAX_TIMESTAMP_SKEW


def _verify_discord_signature(
    public_key_hex: str,
    timestamp: str,
    body: bytes,
    signature_hex: str,
) -> bool:
    """Verify an Ed25519 request signature from Discord.

    Returns True when the signature is valid, False otherwise.
    Requires the ``PyNaCl`` library (``pip install PyNaCl``).

    Security: if PyNaCl is not installed the function returns False and denies
    the request rather than allowing unsigned interactions through.
    """
    try:
        from nacl.signing import VerifyKey
        key = VerifyKey(bytes.fromhex(public_key_hex))
        key.verify((timestamp.encode() + body), bytes.fromhex(signature_hex))
        return True
    except ImportError:
        log.error(
            "SECURITY: PyNaCl is not installed — Discord interaction denied. "
            "Install with: pip install PyNaCl"
        )
        return False   # Deny rather than allow unsigned requests
    except Exception:
        return False
