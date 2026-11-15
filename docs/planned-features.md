# Planned features

Features that are designed but deliberately **not active** in the current release.

## Interactive buttons (acknowledge / take over)

**Status:** planned, switched off since 1.3.0. Tracked in the core feature index
(`lyndrix-core/features/discord-interactive-buttons.md`).

### What it will do

A notification posted to Discord can carry action buttons, for example
*Acknowledge* or *Take over* on an incident. A click is sent to Lyndrix through the
public `/interactions` endpoint, mapped to the Lyndrix user behind the Discord
account, checked against that user's permissions and then forwarded to the plugin
that owns the action.

### Why it is off today

The Ed25519 signature on an interaction proves only that the request comes from
Discord. It says nothing about who clicked: every member who can see the message
could trigger the action, and no Discord user is mapped to a Lyndrix identity.
The 2026-11 audit (finding **DN-03**) therefore closed the path:

- A button click is answered with an ephemeral "not linked / not authorized"
  message and nothing is forwarded to the bus.
- The interaction token and the Discord member object are never copied into the
  event metadata.
- `PING`, signature verification and the replay window still work, so the
  endpoint can stay registered in the Discord developer portal.

No plugin sends action buttons today, so nothing that worked stops working.

### What has to exist before it is switched on again

1. **Account linking** — a mapping Discord user id → Lyndrix user, created by the
   user while logged in to Lyndrix (not by trusting a name from Discord), stored
   in the core, revocable by the user and by an administrator.
2. **Permission check per action** — the resolved Lyndrix user must hold the
   permission the action needs in the owning plugin (for example ITSM
   acknowledge) at the moment of the click. Being a member of the Discord
   channel is never enough.
3. **Audit trail** — every accepted and every refused click is recorded with the
   Lyndrix user, the Discord user id, the action, the target record and the
   outcome.
4. **Core/gateway support** — the provider-user lookup and the permission check
   belong in the messaging gateway, before the resume topic is emitted, so every
   provider plugin gets the same guarantee.

The hook for point 1 already exists in the code:
`DiscordGatewayAdapter.lyndrix_user_for(discord_user_id)` returns `None` for
everyone until a mapping is available (fail closed).
