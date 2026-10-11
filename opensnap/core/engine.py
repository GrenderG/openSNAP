"""Protocol engine orchestrating decode, dispatch, and tick."""

from dataclasses import dataclass, field, replace
import logging
import time
from typing import Literal

from opensnap.config import AppConfig
from opensnap.core.bootstrap import handlers as bootstrap_handlers
from opensnap.core.context import HandlerContext
from opensnap.core.game import handlers as game_handlers
from opensnap.core.router import CommandRouter
from opensnap.plugins.base import GamePlugin
from opensnap.plugins.registry import identify_snap_title
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram
from opensnap.protocol.constants import (
    BARE_ACK_FLAGS,
    FLAG_CHANNEL_BITS,
    FLAG_ROOM,
    FLAG_RESPONSE,
    FLAG_RELIABLE,
    RELAY_CONTEXT_MASK,
    TYPE_LOBBY_RELAY,
    TYPE_LOBBY_RELAY_REQUEST,
    TYPE_ROOM_RELAY,
)
from opensnap.protocol.models import Endpoint, SnapMessage, WIRE_FORMAT_SNAP
from opensnap.storage.factory import create_storage
from opensnap.core.lobbies import LobbyRegistry
from opensnap.core.rooms import RoomRegistry
from opensnap.core.sessions import ReliableReceipt, Session, SessionRegistry


# A gap in a client's reliable sequences older than this is given up (checked
# on the engine tick): the client resends every 200 ms and stops after four
# retransmits.
RELIABLE_GAP_LIMIT_SECONDS = 2.0
# Reliable requests whose answers are kept for replay: the client has at most
# 16 reliable packets in flight, so a retry is never more than 16 behind the
# newest; 32 leaves a margin.
RELIABLE_REPLY_HISTORY = 32


@dataclass(slots=True)
class EngineResult:
    """Response bundle for one datagram."""

    messages: list[SnapMessage]
    errors: list[str]
    room_pending_clears: list[tuple[Endpoint, int]] = field(default_factory=list)


class SnapProtocolEngine:
    """Core protocol engine."""

    def __init__(
        self,
        *,
        config: AppConfig,
        plugin: GamePlugin | None = None,
        role: Literal['combined', 'bootstrap', 'game'] = 'combined',
    ) -> None:
        if role not in {'combined', 'bootstrap', 'game'}:
            raise ValueError(f'Unsupported engine role: {role}.')

        self._config = config
        self._plugin = plugin
        self._role = role
        self._logger = logging.getLogger('opensnap.engine')
        self._closed = False
        self._clock = time.monotonic

        # Accounts, session handoffs, records and online players are shared with other processes (and
        # possibly other machines); sessions, lobbies and rooms are this
        # process's connection state and stay in memory.
        self._storage = create_storage(config)
        self._accounts = self._storage.accounts
        self._handoffs = self._storage.handoffs
        self._sessions = SessionRegistry()
        self._lobbies = LobbyRegistry(config.lobbies)
        self._rooms = RoomRegistry()
        self._router = CommandRouter()

        self._context = HandlerContext(
            config=config,
            accounts=self._accounts,
            handoffs=self._handoffs,
            records=self._storage.records,
            online_players=self._storage.online_players,
            sessions=self._sessions,
            lobbies=self._lobbies,
            rooms=self._rooms,
            identify_snap_title=identify_snap_title,
            end_session=self._end_session,
        )

        self._register_core_handlers()
        if self._role in {'combined', 'game'} and self._plugin is not None:
            self._plugin.register_handlers(self._router, self._context)

    def handle_datagram(self, payload: bytes, endpoint: Endpoint) -> EngineResult:
        """Process one datagram and return outbound messages."""

        try:
            messages = self._decode_datagram(payload, endpoint)
        except PacketDecodeError as exc:
            self._logger.warning(
                'Decode error from %s:%d: %s',
                endpoint.host,
                endpoint.port,
                exc,
            )
            return EngineResult(messages=[], errors=[str(exc)])

        self._logger.debug(
            'Decoded %d message(s) from %s:%d.',
            len(messages),
            endpoint.host,
            endpoint.port,
        )
        outbound: list[SnapMessage] = []
        errors: list[str] = []
        room_pending_clears: list[tuple[Endpoint, int]] = []
        self._handle_in_order(messages, outbound, errors, room_pending_clears)

        self._logger.debug(
            'Datagram from %s:%d produced %d outbound message(s) and %d error(s).',
            endpoint.host,
            endpoint.port,
            len(outbound),
            len(errors),
        )
        return EngineResult(
            messages=outbound,
            errors=errors,
            room_pending_clears=list(dict.fromkeys(room_pending_clears)),
        )

    def _handle_in_order(
        self,
        messages: list[SnapMessage],
        outbound: list[SnapMessage],
        errors: list[str],
        room_pending_clears: list[tuple[Endpoint, int]],
    ) -> None:
        """Handle one datagram, then every held datagram it puts back in order."""

        pending = [messages]
        while pending:
            for session_id in self._handle_datagram_messages(pending.pop(0), outbound, errors, room_pending_clears):
                session = self._sessions.get(session_id)
                if session is not None and session.last_incoming_sequence + 1 in session.held_reliable:
                    pending.append(session.held_reliable.pop(session.last_incoming_sequence + 1)[1])

    def _handle_datagram_messages(
        self,
        messages: list[SnapMessage],
        outbound: list[SnapMessage],
        errors: list[str],
        room_pending_clears: list[tuple[Endpoint, int]],
    ) -> set[int]:
        """Handle the messages of one datagram; return the sessions they belong to."""

        session_ids: set[int] = set()
        replies_start = len(outbound)
        reliable_request: SnapMessage | None = None
        skip_embedded = False
        for index, message in enumerate(messages):
            if skip_embedded and message.embedded_in_multi:
                continue
            skip_embedded = False
            if message.wire_format == WIRE_FORMAT_SNAP:
                self._normalize_session_for_message(message)
            session_ids.add(message.session_id)
            # Any datagram, bare ACKs included, shows the client is alive.
            active = self._sessions.get(message.session_id)
            if active is not None:
                active.last_inbound_at = self._clock()
            prior_room_id = self._room_id_for_session(message.session_id)
            self._logger.debug(
                (
                    'Handling command 0x%02x from %s:%d '
                    '(type=0x%04x sess=0x%08x seq=%d ack=%s payload=%d).'
                ),
                message.command,
                message.endpoint.host,
                message.endpoint.port,
                message.type_flags,
                message.session_id,
                message.sequence_number,
                self._ack_for_log(message),
                len(message.payload),
            )
            if message.wire_format == WIRE_FORMAT_SNAP:
                # Ignore bare ACK frames that do not carry command payload.
                if message.command == commands.CMD_ACK and (message.type_flags & BARE_ACK_FLAGS) == BARE_ACK_FLAGS:
                    self._logger.debug(
                        'Ignoring bare ACK frame from %s:%d.',
                        message.endpoint.host,
                        message.endpoint.port,
                    )
                    continue

                # Top-level reliable packets go through the session's receive
                # window (`accept_incoming`). Only top-level packets carry a
                # transport sequence (embedded children
                # decode as 0, see the codec), and unreliable packets are numbered
                # by a separate counter: `kkReceiveExtentCheck` keeps reliable
                # packets in the receive window (`SLUS_206.42` `0x002f4d5c`) and
                # unreliable ones against their own last number (`app+16`,
                # `0x002f5028`), so neither touches the session sequence.
                if not message.embedded_in_multi and message.type_flags & FLAG_RELIABLE:
                    receipt = self._sessions.accept_incoming(message.session_id, message.sequence_number)
                    if receipt is ReliableReceipt.HOLD:
                        # Ahead of a missing sequence: ACK it, as the client
                        # does for a stored packet, and handle the datagram
                        # once the gap is filled.
                        outbound.append(self._transport_ack(message))
                        active.held_reliable[message.sequence_number] = (self._clock(), messages[index:])
                        return session_ids
                    held = active is not None and message.sequence_number in active.held_reliable
                    if receipt is ReliableReceipt.DUPLICATE and active is not None:
                        # A resend of a held datagram is handled once, in order;
                        # a resend of a handled request gets the same answers
                        # again (the client's window drops them).
                        # Embedded children are not run again either.
                        answers = () if held else active.reply_history.get(message.sequence_number, ())
                        outbound.append(self._transport_ack(message))
                        outbound.extend(replace(answer, endpoint=message.endpoint) for answer in answers)
                        self._logger.debug(
                            'Duplicate reliable command 0x%02x seq=%d from %s:%d: %s.',
                            message.command,
                            message.sequence_number,
                            message.endpoint.host,
                            message.endpoint.port,
                            'held' if held else f'replayed {len(answers)} answer(s)',
                        )
                        skip_embedded = True
                        continue
                    if receipt is ReliableReceipt.DELIVER:
                        reliable_request = message

            if not self._router.has_handler(message.command):
                payload_preview = message.payload[:32].hex(' ')
                if len(message.payload) > 32:
                    payload_preview = f'{payload_preview} ...'
                detail = (
                    'Unhandled command '
                    f'0x{message.command:02x} '
                    f'(type=0x{message.type_flags:04x} '
                    f'sess=0x{message.session_id:08x} '
                    f'seq={message.sequence_number} '
                    f'ack={message.acknowledge_number} '
                    f'payload_len={len(message.payload)} '
                    f'payload_hex={payload_preview or "<empty>"})'
                )
                self._logger.warning(
                    '%s from %s:%d.',
                    detail,
                    message.endpoint.host,
                    message.endpoint.port,
                )
                errors.append(detail)
                continue

            try:
                produced = self._router.dispatch(self._context, message)
                outbound.extend(produced)
                if self._should_clear_room_pending_after_message(message, prior_room_id):
                    room_pending_clears.append((message.endpoint, message.session_id))
                self._logger.debug(
                    'Handler for command 0x%02x produced %d outbound message(s).',
                    message.command,
                    len(produced),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.exception(
                    'Handler failure for command 0x%02x from %s:%d.',
                    message.command,
                    message.endpoint.host,
                    message.endpoint.port,
                )
                errors.append(f'Handler error for command 0x{message.command:02x}: {exc}')

        if reliable_request is not None:
            self._remember_reliable_replies(reliable_request, outbound[replies_start:])
        return session_ids

    def _remember_reliable_replies(self, request: SnapMessage, produced: list[SnapMessage]) -> None:
        """Keep the reliable answers one reliable request (and its embedded children) got.

        Only the requester's answers are kept: callbacks to other players are
        not replayed (MH room join `0x00619430` counts a member again).
        """

        session = self._sessions.get(request.session_id)
        if session is None:
            return
        session.reply_history[request.sequence_number] = tuple(
            reply for reply in produced
            if reply.session_id == request.session_id and reply.type_flags & FLAG_RELIABLE
        )
        oldest = request.sequence_number - RELIABLE_REPLY_HISTORY
        for sequence in [sequence for sequence in session.reply_history if sequence <= oldest]:
            del session.reply_history[sequence]

    def _release_stalled_reliable(self) -> list[SnapMessage]:
        """Hand on held datagrams whose missing sequence will not come any more.

        The client resends a reliable packet every 200 ms and stops after four
        retransmits (`kkSendOperation`), so a gap older than
        `RELIABLE_GAP_LIMIT_SECONDS` at an engine tick is given up.
        """

        now = self._clock()
        outbound: list[SnapMessage] = []
        errors: list[str] = []
        for session in self._sessions.all():
            if not session.held_reliable:
                continue
            if now - min(arrival for arrival, _ in session.held_reliable.values()) < RELIABLE_GAP_LIMIT_SECONDS:
                continue
            first_held = min(session.held_reliable)
            self._logger.warning(
                'Session 0x%08x: reliable sequence(s) %d..%d never arrived; handling held packets from %d.',
                session.session_id,
                session.last_incoming_sequence + 1,
                first_held - 1,
                first_held,
            )
            session.last_incoming_sequence = first_held - 1
            # A released LEAVE's room-pending clear is not applied: the tick
            # returns messages only.
            self._handle_in_order(session.held_reliable.pop(first_held)[1], outbound, errors, [])
        for error in errors:
            self._logger.error('Held packet error: %s', error)
        return outbound

    def decode_datagram(self, payload: bytes, endpoint: Endpoint) -> list[SnapMessage]:
        """Decode one inbound datagram using the configured game plugin when applicable."""

        return self._decode_datagram(payload, endpoint)

    def encode_messages(self, messages: list[SnapMessage], *, footer_bytes: bytes | None = None) -> bytes:
        """Encode outbound datagrams using the configured game plugin when applicable."""

        if self._role in {'combined', 'game'} and self._plugin is not None:
            return self._plugin.encode_messages(messages, footer_bytes=footer_bytes)
        from opensnap.protocol.codec import encode_messages

        return encode_messages(messages, footer_bytes=footer_bytes)

    def start_plugin_services(self) -> None:
        """Start auxiliary services owned by the configured game plugin."""

        if self._role in {'combined', 'game'} and self._plugin is not None:
            self._plugin.start_services(self._context)

    def tick(self) -> list[SnapMessage]:
        """Run periodic plugin tasks and end sessions idle past their plugin's limit."""

        released = self._release_stalled_reliable()
        if self._plugin is None:
            return released
        return released + self._end_idle_sessions() + self._plugin.on_tick(self._context)

    def _end_idle_sessions(self) -> list[SnapMessage]:
        assert self._plugin is not None
        now = self._clock()
        messages: list[SnapMessage] = []
        for session in self._sessions.all():
            limit = self._plugin.session_idle_limit(self._context, session)
            if limit is None or now - session.last_inbound_at < limit:
                continue
            self._logger.warning(
                'Ending session 0x%08x of %s:%d: no datagram for %.0f s.',
                session.session_id,
                session.endpoint.host,
                session.endpoint.port,
                now - session.last_inbound_at,
            )
            messages.extend(self._end_session(session))
        return messages

    def handle_transport_timeout(self, endpoint: Endpoint, session_id: int) -> list[SnapMessage]:
        """Tear down one timed-out session and return any cleanup callbacks."""

        session = self.resolve_session(endpoint, session_id)
        if session is None:
            return []

        self._logger.warning(
            'Timing out session 0x%08x for %s:%d.',
            session.session_id,
            session.endpoint.host,
            session.endpoint.port,
        )

        return self._end_session(session)

    def _end_session(self, session: Session) -> list[SnapMessage]:
        """Remove one session with its plugin, room and handoff state."""

        messages: list[SnapMessage] = []
        if self._plugin is not None:
            messages.extend(self._plugin.on_session_timeout(self._context, session))
        elif session.room_id > 0:
            self._rooms.leave(session.room_id, session.session_id)

        self._sessions.remove(session.session_id)
        self._handoffs.remove(session.session_id)
        return messages

    def _room_id_for_session(self, session_id: int) -> int:
        """Return the current room id for one session, or `0` if unknown."""

        session = self._sessions.get(session_id)
        if session is None:
            return 0
        return session.room_id

    def _should_clear_room_pending_after_message(self, message: SnapMessage, prior_room_id: int) -> bool:
        """Report whether one handled message moved a session out of a room."""

        if message.command not in {commands.CMD_LEAVE, commands.CMD_LOGIN_TO_KICS}:
            return False
        if prior_room_id <= 0:
            return False
        return self._room_id_for_session(message.session_id) == 0

    def resolve_session(self, endpoint: Endpoint, session_id: int) -> Session | None:
        """Resolve one session by id first, then by bound endpoint."""

        session = self._sessions.get(session_id)
        if session is not None:
            return session
        return self._sessions.get_by_endpoint(endpoint)

    def close(self) -> None:
        """Close engine-owned resources."""

        if self._closed:
            return
        if self._plugin is not None:
            self._plugin.stop_services()
        self._storage.close()
        self._closed = True

    def __del__(self) -> None:
        """Best-effort cleanup for engine-owned resources."""

        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    def _register_core_handlers(self) -> None:
        """Register game-independent handlers."""

        self._router.register(commands.CMD_SEND_ECHO, game_handlers.handle_send_echo)
        if self._role in {'combined', 'bootstrap'}:
            self._router.register(commands.CMD_LOGIN_CLIENT, bootstrap_handlers.handle_login_client)
            self._router.register(
                commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
                bootstrap_handlers.handle_bootstrap_check,
            )
        if self._role in {'combined', 'game'}:
            self._router.register(commands.CMD_LOGIN_TO_KICS, game_handlers.handle_login_to_kics)
            self._router.register(commands.CMD_LOGOUT_CLIENT, self._handle_logout)

    def _decode_datagram(self, payload: bytes, endpoint: Endpoint) -> list[SnapMessage]:
        """Decode inbound datagrams with the configured game plugin if present."""

        if self._role in {'combined', 'game'} and self._plugin is not None:
            return self._plugin.decode_datagram(payload, endpoint)
        return decode_datagram(payload, endpoint)

    def _normalize_session_for_message(self, message: SnapMessage) -> None:
        """Resolve a session by endpoint when incoming headers use stale session ids."""

        session = self._sessions.get(message.session_id)
        if session is None:
            session = self._sessions.get_by_endpoint(message.endpoint)
        if session is None:
            return

        if message.session_id != session.session_id:
            self._logger.debug(
                (
                    'Normalizing incoming session id from 0x%08x to 0x%08x '
                    'for %s:%d command 0x%02x.'
                ),
                message.session_id,
                session.session_id,
                message.endpoint.host,
                message.endpoint.port,
                message.command,
            )
            message.session_id = session.session_id

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLogout` as a no-op, matching observed game behavior."""

        del context, message
        return []

    def _transport_ack(self, message: SnapMessage) -> SnapMessage:
        """Bare transport ACK for one reliable request that is not handled now."""

        if message.command == commands.CMD_SEND_TARGET:
            ack_type_flags = FLAG_ROOM | FLAG_RESPONSE
        elif message.command == commands.CMD_SEND:
            callback_flags = message.type_flags & RELAY_CONTEXT_MASK
            if callback_flags in (TYPE_LOBBY_RELAY, TYPE_LOBBY_RELAY_REQUEST):
                ack_type_flags = FLAG_CHANNEL_BITS | FLAG_RESPONSE
            elif callback_flags == TYPE_ROOM_RELAY:
                ack_type_flags = FLAG_ROOM | FLAG_RESPONSE
            elif message.type_flags & FLAG_ROOM:
                ack_type_flags = FLAG_ROOM | FLAG_RESPONSE
            else:
                channel = message.type_flags & FLAG_CHANNEL_BITS
                if channel == 0:
                    channel = FLAG_ROOM
                ack_type_flags = channel | FLAG_RESPONSE
        else:
            ack_type_flags = (message.type_flags & FLAG_CHANNEL_BITS or FLAG_ROOM) | FLAG_RESPONSE

        return self._context.reply(
            message,
            type_flags=ack_type_flags,
            command=commands.CMD_ACK,
            session_id=message.session_id,
        )

    @staticmethod
    def _ack_for_log(message: SnapMessage) -> str:
        """Return a readable ACK field for one decoded message log line."""

        if message.embedded_in_multi:
            return '<embedded>'
        return str(message.acknowledge_number)
