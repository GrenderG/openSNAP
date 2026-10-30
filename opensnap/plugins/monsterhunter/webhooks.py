"""Monster Hunter announcements (release and NA public beta)."""

from opensnap.webhooks import GameWebhooks


class MonsterHunterWebhooks(GameWebhooks):
    def market_day(self, day: str) -> None:
        self.emit('market_day', f'>>> Today is **{day}** in **Minegarde**! *({self.game})*', day=day)

    def event_quest(self, title: str | None) -> None:
        """Announce today's Event quest, or that there is none."""

        if title is None:
            self.emit('event_quest', f'>>> *No Event quest is available today.* *({self.game})*', title=None)
            return
        self.emit('event_quest', f'>>> **__EVENT QUEST OF THE DAY__**\n\n{title} *({self.game})*', title=title)
