"""Auto Modellista announcements (release and Beta1)."""

from opensnap.webhooks import GameWebhooks


class AutoModellistaWebhooks(GameWebhooks):
    def course_record(self, player: str, team: str, course: str, lap_ms: int) -> None:
        """Announce a new best lap on one ranking board (course letter: names are not mapped yet)."""

        minutes, rest = divmod(lap_ms, 60_000)
        lap = f'{minutes}\'{rest // 1000:02d}"{rest % 1000:03d}'
        driver = f'**{player}** [{team}]' if team else f'**{player}**'
        self.emit(
            'course_record',
            f'>>> New course record on **course {course}**: {driver} with **{lap}**!',
            player=player,
            team=team,
            course=course,
            lap_ms=lap_ms,
        )
