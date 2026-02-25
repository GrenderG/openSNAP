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
from opensnap.protocol.constants import CHANNEL_ROOM, FLAG_RESPONSE
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
        """Echo packets receive an empty ACK-style response."""

        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_SEND_ECHO,
            )
        ]

    def _handle_logout(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLogout` as a no-op, matching snapsi behavior."""

        del context, message
        return []
