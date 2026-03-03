"""Protocol engine orchestrating decode, dispatch, and tick."""

from dataclasses import dataclass
import logging

from opensnap.config import AppConfig
from opensnap.core.auth import BootstrapAuthenticator
from opensnap.core.context import HandlerContext
from opensnap.core.router import CommandRouter
from opensnap.plugins.base import GamePlugin
from opensnap.protocol import commands
from opensnap.protocol.codec import PacketDecodeError, decode_datagram
from opensnap.protocol.constants import CHANNEL_LOBBY, CHANNEL_ROOM, FLAG_RELIABLE, FLAG_RESPONSE
from opensnap.protocol.models import Endpoint, SnapMessage
from opensnap.storage.factory import create_storage

@dataclass(slots=True)
class EngineResult:
    """Response bundle for one datagram."""

    messages: list[SnapMessage]
    errors: list[str]


class SnapProtocolEngine:
    """Core protocol engine."""

    def __init__(self, *, config: AppConfig, plugin: GamePlugin) -> None:
        self._config = config
        self._plugin = plugin
        self._logger = logging.getLogger('opensnap.engine')

        storage = create_storage(config)
        self._accounts = storage.accounts
        self._sessions = storage.sessions
        self._lobbies = storage.lobbies
        self._rooms = storage.rooms
        self._router = CommandRouter()

        self._context = HandlerContext(
            config=config,
            accounts=self._accounts,
            sessions=self._sessions,
            lobbies=self._lobbies,
            rooms=self._rooms,
        )

        self._auth = BootstrapAuthenticator()
        self._register_core_handlers()
        self._plugin.register_handlers(self._router, self._context)

    def handle_datagram(self, payload: bytes, endpoint: Endpoint) -> EngineResult:
        """Process one datagram and return outbound messages."""

        try:
            messages = decode_datagram(payload, endpoint)
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
            # Ignore bare ACK frames that do not carry command payload.
            if message.command == commands.CMD_ACK and (message.type_flags & 0x6000) == 0x6000:
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
            if not accepted and self._is_duplicate_reliable_send(message):
                ack = self._build_duplicate_reliable_send_ack(message)
                if ack is not None:
                    outbound.append(ack)
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

        self._logger.debug(
            'Datagram from %s:%d produced %d outbound message(s) and %d error(s).',
            endpoint.host,
            endpoint.port,
            len(outbound),
            len(errors),
        )
        return EngineResult(messages=outbound, errors=errors)

    def tick(self) -> list[SnapMessage]:
        """Run periodic plugin tasks."""

        return self._plugin.on_tick(self._context)

    def _register_core_handlers(self) -> None:
        """Register game-independent handlers."""

        self._router.register(commands.CMD_LOGIN_CLIENT, self._auth.handle_login_client)
        self._router.register(commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK, self._auth.handle_bootstrap_check)
        self._router.register(commands.CMD_LOGIN_TO_KICS, self._auth.handle_login_to_kics)
        self._router.register(commands.CMD_SEND_ECHO, self._handle_echo)
        self._router.register(commands.CMD_LOGOUT_CLIENT, self._handle_logout)

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
        """Respond to keepalive/echo packets by mirroring one payload word."""

        payload = message.payload[:4].ljust(4, b'\x00')
        channel = message.type_flags & 0x3000
        if channel == 0:
            channel = CHANNEL_ROOM

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
    def _is_duplicate_reliable_send(message: SnapMessage) -> bool:
        """Check whether this command should be ACK-only on duplicate sequence."""

        if (message.type_flags & FLAG_RELIABLE) == 0:
            return False
        # Embedded follow-up messages from multi datagrams can legally carry
        # sequence 0 while the outer reliable entry carries the real sequence.
        # Treating those as duplicates drops valid relays in game-loading flow.
        if message.embedded_in_multi and message.sequence_number == 0:
            return False
        return message.command in {commands.CMD_SEND, commands.CMD_SEND_TARGET}

    def _build_duplicate_reliable_send_ack(self, message: SnapMessage) -> SnapMessage | None:
        """Build ACK for duplicate reliable send commands without re-dispatch."""

        if message.command == commands.CMD_SEND_TARGET:
            ack_type_flags = CHANNEL_ROOM | FLAG_RESPONSE
        elif message.command == commands.CMD_SEND:
            if (message.type_flags & 0x3400) == 0x1400:
                ack_type_flags = CHANNEL_LOBBY | FLAG_RESPONSE
            elif (message.type_flags & 0x3400) == 0x2400:
                ack_type_flags = CHANNEL_ROOM | FLAG_RESPONSE
            elif message.type_flags & CHANNEL_ROOM:
                ack_type_flags = CHANNEL_ROOM | FLAG_RESPONSE
            else:
                channel = message.type_flags & 0x3000
                if channel == 0:
                    channel = CHANNEL_ROOM
                ack_type_flags = channel | FLAG_RESPONSE
        else:
            return None

        return self._context.reply(
            message,
            type_flags=ack_type_flags,
            command=commands.CMD_ACK,
            session_id=message.session_id,
        )
