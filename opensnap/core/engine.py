"""Protocol engine orchestrating decode, dispatch, and tick."""

from dataclasses import dataclass

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

PRE_AUTH_COMMANDS = {
    commands.CMD_LOGIN_CLIENT,
    commands.CMD_BOOTSTRAP_LOGIN_SWAN_CHECK,
    commands.CMD_LOGIN_TO_KICS,
}


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

        storage = create_storage(config)
        self._accounts = storage.accounts
        self._sessions = storage.sessions
        self._lobbies = storage.lobbies
        self._rooms = storage.rooms
        self._router = CommandRouter()
        self._preauth_sequences: dict[Endpoint, int] = {}

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
            return EngineResult(messages=[], errors=[str(exc)])

        outbound: list[SnapMessage] = []
        errors: list[str] = []
        for message in messages:
            # Ignore bare ACK frames that do not carry command payload.
            if message.command == commands.CMD_ACK and (message.type_flags & 0x6000) == 0x6000:
                continue

            if not self._accept_message_sequence(message):
                continue

            try:
                outbound.extend(self._router.dispatch(self._context, message))
            except Exception as exc:  # noqa: BLE001
                errors.append(f'Handler error for command 0x{message.command:02x}: {exc}')

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

    def _accept_message_sequence(self, message: SnapMessage) -> bool:
        """Reject duplicate or out-of-order messages by sequence number."""

        if message.command == commands.CMD_LOGIN_CLIENT:
            # A fresh login request starts a new bootstrap flow for the endpoint.
            if message.sequence_number == 0:
                self._preauth_sequences[message.endpoint] = 0
                return True
            return self._accept_preauth_sequence(message.endpoint, message.sequence_number)

        session = self._sessions.get(message.session_id)
        if session is None:
            session = self._sessions.get_by_endpoint(message.endpoint)

        if session is not None:
            if message.session_id != session.session_id:
                message.session_id = session.session_id
            self._preauth_sequences.pop(message.endpoint, None)
            return self._sessions.accept_incoming(session.session_id, message.sequence_number)

        if message.command in PRE_AUTH_COMMANDS:
            return self._accept_preauth_sequence(message.endpoint, message.sequence_number)

        return True

    def _accept_preauth_sequence(self, endpoint: Endpoint, sequence_number: int) -> bool:
        """Accept pre-auth sequence numbers for endpoint-scoped bootstrap flow."""

        last = self._preauth_sequences.get(endpoint, -1)
        if sequence_number <= last:
            return False

        self._preauth_sequences[endpoint] = sequence_number
        return True

    def _handle_echo(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Echo packets receive an empty ACK-style response."""

        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_SEND_ECHO,
            )
        ]
