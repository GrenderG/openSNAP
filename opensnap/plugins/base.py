"""Shared plugin base behavior."""

from dataclasses import dataclass

from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.core.sessions import Session
from opensnap.protocol.codec import decode_datagram as decode_snap_datagram, encode_messages as encode_snap_messages
from opensnap.protocol.enums import GameTags, PostGameReportMask, RoomSubcommand
from opensnap.protocol.models import Endpoint, SnapMessage


@dataclass(frozen=True, slots=True)
class SnapTitle:
    """One SN@P client build, as identified by the bootstrap service.

    Every title passes a fixed title code to the SDK's `kkLoginClient`, which
    sends it at login payload offset 100; the footer marker tells the SDK
    generation apart (legacy `0xBA476610`, release `0xBA476611`).
    """

    title_code: int
    footer_marker: int
    name: str


class GamePlugin:
    """Base class for game-specific SNAP plugins."""

    name = ''
    # Client builds served by this plugin; the bootstrap service routes their
    # logins to this plugin's game server.
    snap_titles: tuple[SnapTitle, ...] = ()
    # Companion service (`opensnap_app`) this game also connects to, run as
    # its own process: `python run.py app <companion_app>`.
    companion_app: str | None = None
    # Seconds of client silence after which the server sends the session a
    # reliable empty packet (command 0), or None for none. The client ACKs it,
    # and that outbound ACK keeps its NAT mapping open while the game sends
    # nothing (AM in a lobby or room, MH while it receives Town relays, MH NA
    # beta and Outbreak File#1 always, File#2 for 60 s at a time); otherwise a
    # router reassigns the idle port, and another console behind it logging
    # in from that port supersedes the session. Every served SN@P build takes
    # a reliable packet into its receive window and ACKs it without reading
    # the command (AM `kkReceiveExtentCheck`; MH/Outbreak packet handler, e.g.
    # MH NA 0x001fe778, F2 NA 0x00161d50), and command 0 never runs a callback
    # (AM `kkDispatchingPacket` skips it: Beta1 0x002e84b4, release 0x002ee774,
    # Beta2 0x002ed254; MH/Outbreak dispatch `cmd - 3 < 118` returns: MH NA
    # 0x002035c0, F1 v2 0x001e1600, F2 NA 0x00166b98). 20 s stays under the
    # common 30 s router UDP timeout.
    keepalive_interval_seconds: float | None = 20.0
    # Seconds a client may stay silent while it owes an ACK before the
    # transport ends its session. The client itself gives up after ~5000 ms
    # of silence (`kkReceiveData` refreshes its last-receive clock on every
    # packet and `kkDispatchOperationByPreriod` times out after `0x1388` ms:
    # `SLUS_204.98` `0x002e887c`, `0x002e8700..0x002e8778`; `SLUS_206.42`
    # `0x002eed94`, `0x002eeb14..0x002eebd8`); the server allows a broader
    # 60 s so a client can recover from a network hiccup first.
    reliable_timeout_seconds: float = 60.0

    def register_handlers(self, router: CommandRouter, context: HandlerContext) -> None:
        """Register game-specific command handlers."""

        del router, context

    def start_services(self, context: HandlerContext) -> None:
        """Start plugin-owned auxiliary services that live in the game process."""

        del context

    def stop_services(self) -> None:
        """Stop plugin-owned auxiliary services."""

    def on_tick(self, context: HandlerContext) -> list[SnapMessage]:
        """Run periodic plugin work."""

        del context
        return []

    def on_session_timeout(self, context: HandlerContext, session: Session) -> list[SnapMessage]:
        """Handle one transport-timed-out session."""

        del context, session
        return []

    def session_idle_limit(self, context: HandlerContext, session: Session) -> float | None:
        """Seconds of client silence after which `session` is ended, or None for no limit."""

        del context, session
        return None

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
