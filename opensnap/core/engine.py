"""Protocol engine orchestrating decode, dispatch, and tick."""

from dataclasses import dataclass
from enum import IntEnum
import logging
from typing import Literal

from opensnap.config import AppConfig
from opensnap.core.bootstrap import handlers as bootstrap_handlers
from opensnap.core.context import HandlerContext
from opensnap.core.game import handlers as game_handlers
from opensnap.core.router import CommandRouter
from opensnap.plugins.base import GamePlugin
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram
from opensnap.protocol.constants import (
    BARE_ACK_FLAGS,
    FLAG_CHANNEL_BITS,
    FLAG_MULTI,
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
from opensnap.core.sessions import Session


@dataclass(slots=True)
class EngineResult:
    """Response bundle for one datagram."""

    messages: list[SnapMessage]
    errors: list[str]


class DuplicateAckPolicy(IntEnum):
    """Duplicate reliability policy for inbound SNAP messages."""

    NONE = 0
    DUPLICATE_RELIABLE = 1
    STALE_DUPLICATE_LEAVE = 2


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

        reset_mode: Literal['full', 'game', 'none']
        if role == 'combined':
            reset_mode = 'full'
        elif role == 'game':
            reset_mode = 'game'
        else:
            reset_mode = 'none'

        self._storage = create_storage(config, reset_mode=reset_mode)
        self._accounts = self._storage.accounts
        self._sessions = self._storage.sessions
        self._lobbies = self._storage.lobbies
        self._rooms = self._storage.rooms
        self._router = CommandRouter()

        self._context = HandlerContext(
            config=config,
            accounts=self._accounts,
            sessions=self._sessions,
            lobbies=self._lobbies,
            rooms=self._rooms,
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
        duplicate_reliable_multi_parent = False
        for message in messages:
            self._logger.debug(
                (
                    'Handling command 0x%02x from %s:%d '
                    '(type=0x%04x sess=0x%08x seq=%d ack=%d payload=%d).'
                ),
                message.command,
                message.endpoint.host,
                message.endpoint.port,
                message.type_flags,
                message.session_id,
                message.sequence_number,
                message.acknowledge_number,
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

                self._normalize_session_for_message(message)
                # Track highest inbound sequence per session so direct fanout ACKs can
                # mirror client-side flow control state.
                accepted = self._sessions.accept_incoming(message.session_id, message.sequence_number)
                duplicate_reason = self._duplicate_ack_only_reason(message, accepted)
                if duplicate_reason is not DuplicateAckPolicy.NONE:
                    if (
                        duplicate_reason is DuplicateAckPolicy.DUPLICATE_RELIABLE
                        and self._is_duplicate_reliable_multi_parent(message)
                    ):
                        duplicate_reliable_multi_parent = True
                    ack = self._build_duplicate_reliable_ack(message)
                    if ack is not None:
                        outbound.append(ack)
                    if duplicate_reason is DuplicateAckPolicy.STALE_DUPLICATE_LEAVE:
                        session = self._sessions.get(message.session_id)
                        last_sequence = -1 if session is None else session.last_incoming_sequence
                        self._logger.debug(
                            (
                                'Suppressing stale duplicate reliable leave command from %s:%d '
                                '(sess=0x%08x seq=%d last=%d); returning ACK only.'
                            ),
                            message.endpoint.host,
                            message.endpoint.port,
                            message.session_id,
                            message.sequence_number,
                            last_sequence,
                        )
                    else:
                        self._logger.debug(
                            (
                                'Suppressing duplicate reliable command 0x%02x from %s:%d '
                                '(sess=0x%08x seq=%d); returning ACK only.'
                            ),
                            message.command,
                            message.endpoint.host,
                            message.endpoint.port,
                            message.session_id,
                            message.sequence_number,
                        )
                    continue
                if (
                    duplicate_reliable_multi_parent
                    and self._should_suppress_embedded_send_after_duplicate_multi(message)
                ):
                    self._logger.debug(
                        (
                            'Suppressing embedded duplicate reliable send command 0x%02x '
                            'from %s:%d (sess=0x%08x seq=%d) after duplicate multi parent.'
                        ),
                        message.command,
                        message.endpoint.host,
                        message.endpoint.port,
                        message.session_id,
                        message.sequence_number,
                    )
                    continue

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

        outbound = self._drop_redundant_bare_acks(outbound)
        self._logger.debug(
            'Datagram from %s:%d produced %d outbound message(s) and %d error(s).',
            endpoint.host,
            endpoint.port,
            len(outbound),
            len(errors),
        )
        return EngineResult(messages=outbound, errors=errors)

    def decode_datagram(self, payload: bytes, endpoint: Endpoint) -> list[SnapMessage]:
        """Decode one inbound datagram using the configured game plugin when applicable."""

        return self._decode_datagram(payload, endpoint)

    def encode_messages(self, messages: list[SnapMessage], *, footer_bytes: bytes | None = None) -> bytes:
        """Encode outbound datagrams using the configured game plugin when applicable."""

        if self._role in {'combined', 'game'} and self._plugin is not None:
            return self._plugin.encode_messages(messages, footer_bytes=footer_bytes)
        from opensnap.protocol.codec import encode_messages

        return encode_messages(messages, footer_bytes=footer_bytes)

    def tick(self) -> list[SnapMessage]:
        """Run periodic plugin tasks."""

        if self._plugin is None:
            return []
        return self._plugin.on_tick(self._context)

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

        messages: list[SnapMessage] = []
        if self._plugin is not None:
            messages.extend(self._plugin.on_session_timeout(self._context, session))
        elif session.room_id > 0:
            self._rooms.leave(session.room_id, session.session_id)

        self._sessions.remove(session.session_id)
        return messages

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

        self._router.register(commands.CMD_SEND_ECHO, self._handle_echo)
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

    def _handle_echo(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Respond to keepalive/echo packets by mirroring full payload bytes.

        `SLUS_206.42` `kkSendEchoPacket` copies the caller-provided payload
        length verbatim (observed 8-byte and 64-byte calls), and the echo
        callback variants only clear local state flags.
        """

        payload = message.payload
        channel = message.type_flags & FLAG_CHANNEL_BITS
        if channel == 0:
            channel = FLAG_ROOM

        return [
            context.reply(
                message,
                type_flags=channel | FLAG_RESPONSE,
                command=message.command,
                payload=payload,
            )
        ]

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLogout` as a no-op, matching observed game behavior."""

        del context, message
        return []

    @staticmethod
    def _should_ack_duplicate_only(message: SnapMessage) -> bool:
        """Check whether this reliable duplicate should return transport ACK only."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return False
        # Embedded follow-up messages from multi datagrams can legally carry
        # sequence 0 while the outer reliable entry carries the real sequence.
        # Treating those as duplicates drops valid relays in game-loading flow.
        if message.embedded_in_multi and message.sequence_number == 0:
            return False
        return message.command in {commands.CMD_SEND, commands.CMD_SEND_TARGET}

    def _duplicate_ack_only_reason(self, message: SnapMessage, accepted: bool) -> DuplicateAckPolicy:
        """Return duplicate ACK-only decision reason for one inbound message."""

        if accepted:
            return DuplicateAckPolicy.NONE
        if self._should_ack_duplicate_only(message):
            return DuplicateAckPolicy.DUPLICATE_RELIABLE
        if self._should_ack_stale_duplicate_leave_only(message):
            return DuplicateAckPolicy.STALE_DUPLICATE_LEAVE
        return DuplicateAckPolicy.NONE

    @staticmethod
    def _is_duplicate_reliable_multi_parent(message: SnapMessage) -> bool:
        """Check whether this message is the outer reliable multi duplicate."""

        return (
            not message.embedded_in_multi
            and message.command == commands.CMD_SEND
            and (message.type_flags & FLAG_RELIABLE) != 0
            and (message.type_flags & FLAG_MULTI) != 0
        )

    @staticmethod
    def _should_suppress_embedded_send_after_duplicate_multi(message: SnapMessage) -> bool:
        """Suppress duplicate multi embedded room relays that piggyback sequence 0.

        Duplicate outer reliable multi retransmits can replay embedded `CMD_SEND`
        sequence-zero entries that are semantically tied to the outer transport
        sequence. Re-broadcasting those embedded room relays to peers can
        perturb race-start synchronization. Keep replay behavior for other
        embedded command families (`CMD_SEND_TARGET`, `CMD_CHANGE_ATTRIBUTE`)
        unchanged.
        """

        if not message.embedded_in_multi:
            return False
        if message.command != commands.CMD_SEND:
            return False
        if (message.type_flags & FLAG_RELIABLE) == 0:
            return False
        return message.sequence_number == 0

    def _should_ack_stale_duplicate_leave_only(self, message: SnapMessage) -> bool:
        """ACK-only stale duplicate leave requests once a newer request is accepted.

        Release and Beta1 leave callbacks (`ResultLeaveRoomCallBack*`,
        `ResultLeaveLobbyCallBack*`) run immediate UI state transitions and are
        not keyed by transport sequence. Replaying wrappers for old leave
        sequence numbers after a newer request has already progressed can drive
        redundant callback transitions.
        """

        if message.command != commands.CMD_LEAVE:
            return False
        if (message.type_flags & FLAG_RELIABLE) == 0:
            return False
        # Embedded leave commands inside reliable multi datagrams can legally
        # carry sequence 0 while the outer packet owns transport sequencing.
        if message.embedded_in_multi and message.sequence_number == 0:
            return False

        session = self._sessions.get(message.session_id)
        if session is None:
            return False

        return session.last_incoming_sequence > message.sequence_number

    def _build_duplicate_reliable_ack(self, message: SnapMessage) -> SnapMessage | None:
        """Build a transport ACK for a duplicate reliable command."""

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
        elif message.command == commands.CMD_LEAVE:
            channel = message.type_flags & FLAG_CHANNEL_BITS
            if channel == 0:
                channel = FLAG_ROOM
            ack_type_flags = channel | FLAG_RESPONSE
        else:
            return None

        return self._context.reply(
            message,
            type_flags=ack_type_flags,
            command=commands.CMD_ACK,
            session_id=message.session_id,
        )

    def _drop_redundant_bare_acks(self, outbound: list[SnapMessage]) -> list[SnapMessage]:
        """Drop bare ACKs that are already covered by another response packet.

        `SLUS_204.98` `kkDispatchingPacket` (`0x002e8480`) and
        `SLUS_206.42` `kkDispatchingPacket` (`0x002ee720`) feed every
        `FLAG_RESPONSE` packet through `kkSetRevAck` before command dispatch.
        That makes a standalone `CMD_ACK` redundant whenever the same datagram
        already returns another response packet to the sender with the same
        reverse-ACK number.
        """

        response_keys = {
            (message.endpoint, message.session_id, message.acknowledge_number)
            for message in outbound
            if message.command != commands.CMD_ACK
            and (message.type_flags & FLAG_RESPONSE) != 0
            and message.acknowledge_number > 0
        }
        if not response_keys:
            return outbound

        filtered: list[SnapMessage] = []
        for message in outbound:
            key = (message.endpoint, message.session_id, message.acknowledge_number)
            if (
                message.command == commands.CMD_ACK
                and (message.type_flags & BARE_ACK_FLAGS) == BARE_ACK_FLAGS
                and message.acknowledge_number > 0
                and key in response_keys
            ):
                self._logger.debug(
                    (
                        'Dropping redundant bare ACK to %s:%d '
                        '(sess=0x%08x ack=%d) because another response packet '
                        'in the same datagram already carries the reverse ACK.'
                    ),
                    message.endpoint.host,
                    message.endpoint.port,
                    message.session_id,
                    message.acknowledge_number,
                )
                continue
            filtered.append(message)
        return filtered
