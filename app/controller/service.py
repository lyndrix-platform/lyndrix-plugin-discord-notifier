"""DiscordNotifierService — lightweight state container.

Tracks the number of messages delivered via the adapter (used in the
settings UI counter).  The blocking send logic previously in webhook.py
has been removed; all delivery now goes through DiscordGatewayAdapter.send().
"""


class DiscordNotifierService:
    def __init__(self, ctx):
        self.ctx   = ctx
        self.state = {"notifications_sent": 0}

    @property
    def notifications_sent(self) -> int:
        return self.state["notifications_sent"]
