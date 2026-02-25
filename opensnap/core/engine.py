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
            if message.command == 0 and (message.type_flags & 0x6000) == 0x6000:
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

    def _handle_echo(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Echo packets receive an empty ACK-style response."""

        return [
            context.reply(
                message,
                type_flags=CHANNEL_ROOM | FLAG_RESPONSE,
                command=commands.CMD_SEND_ECHO,
            )
        ]
