"""Resident Evil Outbreak announcements (File#1, its public beta and File#2)."""

from collections.abc import Sequence

from opensnap.webhooks import GameWebhooks


class OutbreakWebhooks(GameWebhooks):
    def scenario_started(self, area: str, room: str, players: Sequence[str]) -> None:
        self.emit(
            'scenario_started',
            f'>>> **{", ".join(players)}** started a scenario in room **{room}** ({area}).',
            area=area,
            room=room,
            players=list(players),
        )
