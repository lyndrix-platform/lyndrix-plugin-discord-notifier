"""
FastAPI router for Discord Interaction callbacks.

Discord POSTs to this endpoint whenever a user clicks a button (or uses a
slash command) attached to a message this adapter sent.

Security: Discord signs every interaction request with an Ed25519 keypair.
We MUST verify the signature before processing.  Unauthenticated requests
MUST return HTTP 401; missing the verification entirely would allow any
attacker to fake button-click events.

Set up in Discord Developer Portal:
  Interactions Endpoint URL: https://<your-lyndrix-host>/api/plugins/lyndrix.plugin.discord/interactions
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from .adapter import DiscordGatewayAdapter

log = logging.getLogger("Plugin:DiscordRouter")

# Interaction type constants
_TYPE_PING       = 1
_TYPE_COMPONENT  = 3


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
        public_key_hex = adapter._ctx.get_secret("discord_public_key") or ""
        if public_key_hex:
            if not _verify_discord_signature(public_key_hex, timestamp, body, signature):
                log.warning("Discord router: signature verification failed.")
                raise HTTPException(status_code=401, detail="Invalid request signature")
        else:
            log.warning(
                "Discord router: 'discord_public_key' not configured — "
                "signature verification DISABLED (insecure, configure immediately)."
            )

        import json as _json
        try:
            payload = _json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        interaction_type = payload.get("type")

        # Discord PING — must reply with type=1 immediately
        if interaction_type == _TYPE_PING:
            return JSONResponse({"type": 1})

        # Route component interactions through the gateway
        from core.components.messaging.gateway import messaging_gateway
        try:
            await messaging_gateway.handle_incoming("discord", payload)
        except Exception as exc:
            log.error("Discord router: handle_incoming raised: %s", exc)

        # Acknowledge the interaction (deferred update — no new message shown)
        return JSONResponse({"type": 6})

    return router


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
        from nacl.exceptions import BadSignatureError
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
