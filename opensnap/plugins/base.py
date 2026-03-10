"""Shared plugin base behavior."""

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.protocol.codec import decode_datagram as decode_snap_datagram, encode_messages as encode_snap_messages
from opensnap.protocol.enums import GameTags, PostGameReportMask, RoomSubcommand
from opensnap.protocol.models import Endpoint, SnapMessage


class GamePlugin:
    """Base class for game-specific SNAP plugins."""

    name = ''

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        """Register game-specific command handlers."""

        del router, context

    def on_tick(self, context: HandlerContext) -> list[SnapMessage]:
        """Run periodic plugin work."""

        del context
        return []

    def on_session_timeout(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Handle one transport-timed-out session."""

        del context, session
        return []

    def decode_datagram(self, payload: bytes, endpoint: Endpoint) -> list[SnapMessage]:
        """Decode one datagram for this plugin."""

        return decode_snap_datagram(payload, endpoint)

    def encode_messages(self, messages: list[SnapMessage], *, footer_bytes: bytes | None = None) -> bytes:
        """Encode one or more outbound messages for this plugin."""

        return encode_snap_messages(messages, footer_bytes=footer_bytes)

    @staticmethod
    def decode_room_game_tag(subcommand: int) -> GameTags | None:
        """Map one raw room subcommand to the normalized room-flow tag."""

        if subcommand == RoomSubcommand.GAME_START:
            return GameTags.GAME_START
        if RoomSubcommand.GAME_OVER_MIN <= subcommand <= RoomSubcommand.GAME_OVER_MAX:
            return GameTags.GAME_OVER
        if RoomSubcommand.RESULT_MIN <= subcommand <= RoomSubcommand.RESULT_MAX:
            return GameTags.RESULT
        if subcommand == RoomSubcommand.RESULT2:
            return GameTags.RESULT2
        return None

    @staticmethod
    def post_game_report_mask(tag: GameTags | None) -> PostGameReportMask:
        """Return the tracked post-game report family for one normalized tag."""

        if tag is GameTags.GAME_OVER:
            return PostGameReportMask.GAME_OVER
        if tag is GameTags.RESULT:
            return PostGameReportMask.RESULT
        return PostGameReportMask.NONE

    @staticmethod
    def should_echo_room_game_tag(tag: GameTags | None) -> bool:
        """Return whether a room game tag is echoed back to the sender.

        The PS2 client expects the start signal to be visible to all room members,
        including the sender. Later post-game packets are peer relays or
        server-originated transitions and should not be reflexively echoed.
        """

        return tag is GameTags.GAME_START
