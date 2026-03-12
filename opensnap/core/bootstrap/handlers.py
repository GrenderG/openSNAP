"""Bootstrap-side login, verification, and redirect handlers."""

import logging
import socket
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.core.context import HandlerContext
from opensnap.protocol import commands
from opensnap.protocol.constants import (
    BOOTSTRAP_LOGIN_FAIL_REASON_GENERIC,
    BOOTSTRAP_LOGIN_FAIL_REASON_INVALID_PASSWORD,
    FLAG_CHANNEL_BITS,
    FLAG_RESPONSE,
    FOOTER_BYTES_KAGE,
)
from opensnap.protocol.fields import get_c_string
from opensnap.protocol.models import SnapMessage

LOGGER = logging.getLogger('opensnap.core.bootstrap')


def handle_login_client(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
    """Handle `kkLoginClient` on the bootstrap endpoint."""

    raw_login = _get_login_client_raw_name(message.payload)
    login = _parse_login_client_name(raw_login)
    game_identifier = detect_game_identifier(
        message=message,
        default_game_identifier=context.config.server.default_bootstrap_game_identifier,
    )
    if not login:
        LOGGER.warning(
            'Rejecting login-client from %s:%d: could not parse username from payload len=%d.',
            message.endpoint.host,
            message.endpoint.port,
            len(message.payload),
        )
    account = context.accounts.get_by_name(login)
    if account is None:
        LOGGER.warning(
            'Rejecting login-client from %s:%d: account %r was not found.',
            message.endpoint.host,
            message.endpoint.port,
            login,
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS,
                command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
            )
        ]

    game_target = context.config.server.resolve_game_target(game_identifier)
    if game_target is None:
        LOGGER.error(
            (
                'Rejecting login-client from %s:%d: no bootstrap game target '
                'is configured for %r.'
            ),
            message.endpoint.host,
            message.endpoint.port,
            game_identifier,
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS,
                command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
            )
        ]

    session = context.sessions.create_or_replace(
        message.endpoint,
        account,
        game_identifier=game_identifier,
    )

    if _is_kage_bootstrap_variant(message):
        kage_key = _resolve_kage_bootstrap_key(raw_login=raw_login, account=account)
        if not kage_key:
            return [
                context.reply(
                    message,
                    type_flags=FLAG_CHANNEL_BITS,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                )
            ]
        advertised_game_host = _resolve_game_target_host(
            context=context,
            game_identifier=game_identifier,
            target_host=game_target.host,
            client_host=message.endpoint.host,
        )
        success_payload = _build_kage_login_success_payload(
            login_field=raw_login,
            server_host=advertised_game_host,
            server_port=game_target.port,
            key_material=kage_key,
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_BOOTSTRAP_LOGIN_SUCCESS,
                payload=success_payload,
                session_id=session.session_id,
            )
        ]

    else:
        challenge_payload = _build_bootstrap_login_payload(
            magic_key=account.bootstrap_magic_key,
            seed=account.seed,
            server_secret=context.config.server.server_secret,
            server_port=context.config.server.bootstrap.port,
            bootstrap_key=context.config.server.bootstrap_key,
        )

    return [
        context.reply(
            message,
            type_flags=FLAG_CHANNEL_BITS,
            command=commands.CMD_BOOTSTRAP_LOGIN_SWAN,
            payload=challenge_payload,
            session_id=session.session_id,
        )
    ]


def _resolve_kage_bootstrap_key(*, raw_login: str, account) -> bytes:
    """Resolve key material for `SLUS_204.98` bootstrap success payloads.

    `kkLoginClient` stores runtime `login_password` at `app+0x47c`, and
    `kkBootStrapLoginSuccess` decrypts `0x2d` with that exact byte string.
    """

    if account.bootstrap_login_key:
        return account.bootstrap_login_key

    login_fallback = _parse_login_client_name(raw_login).encode('utf-8')
    if login_fallback:
        LOGGER.debug(
            (
                'KAGE bootstrap key for account %r is unavailable as cleartext; '
                'falling back to login-field key material.'
            ),
            account.username,
        )
        return login_fallback

    LOGGER.error(
        (
            'KAGE bootstrap key for account %r is unavailable: both clear login '
            'key and login-field key material are empty.'
        ),
        account.username,
    )
    return b''


def handle_bootstrap_check(context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
    """Handle `kkBootStrapLoginSWAN_CHECK` on the bootstrap endpoint."""

    valid = _verify_bootstrap_answer(
        payload=message.payload,
        bootstrap_key=context.config.server.bootstrap_key,
        server_secret=context.config.server.server_secret,
    )
    if not valid:
        LOGGER.warning(
            'Rejecting bootstrap check from %s:%d: verifier did not match server secret (payload len=%d).',
            message.endpoint.host,
            message.endpoint.port,
            len(message.payload),
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                payload=_build_login_fail_payload(reason_code=BOOTSTRAP_LOGIN_FAIL_REASON_INVALID_PASSWORD),
            )
        ]

    session = context.sessions.get_by_endpoint(message.endpoint)
    if session is None:
        LOGGER.warning(
            'Rejecting bootstrap check from %s:%d: no session is bound to this endpoint.',
            message.endpoint.host,
            message.endpoint.port,
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                payload=_build_login_fail_payload(),
            )
        ]

    game_target = context.config.server.resolve_game_target(session.game_plugin)
    if game_target is None:
        LOGGER.error(
            (
                'Rejecting bootstrap check from %s:%d: no bootstrap game target '
                'is configured for session game %r.'
            ),
            message.endpoint.host,
            message.endpoint.port,
            session.game_plugin,
        )
        return [
            context.reply(
                message,
                type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
                command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                payload=_build_login_fail_payload(),
            )
        ]

    advertised_host = _resolve_game_target_host(
        context=context,
        game_identifier=session.game_plugin,
        target_host=game_target.host,
        client_host=message.endpoint.host,
    )
    success_payload = _build_login_success_payload(
        login=session.username,
        server_host=advertised_host,
        server_port=game_target.port,
        bootstrap_key=context.config.server.bootstrap_key,
    )
    return [
        context.reply(
            message,
            type_flags=FLAG_CHANNEL_BITS | FLAG_RESPONSE,
            command=commands.CMD_BOOTSTRAP_LOGIN_SUCCESS,
            payload=success_payload,
            session_id=session.session_id,
        )
    ]


def detect_game_identifier(*, message: SnapMessage, default_game_identifier: str) -> str:
    """Return the bootstrap-selected game id for one login attempt.

    Intentionally *not* treated as discriminators:
    - the legacy KAGE footer (`0xBA476610`);
    - the primary footer (`0xBA476611`);
    - the secondary `@cei-auth` login string copy.

    TODO: Recover a game-specific bootstrap discriminator that is not tied to
    footer bytes alone. The KAGE footer currently cannot be assigned directly
    to Auto Modellista beta1 because that variant may be shared by other SN@P
    titles.
    """

    _ = message
    return default_game_identifier


def _get_login_client_raw_name(payload: bytes) -> str:
    """Extract the raw login field from `kkLoginClient` payload offset 0."""

    return get_c_string(payload, 0)


def _parse_login_client_name(raw_login: str) -> str:
    """Parse the account name from the raw `kkLoginClient` login field.

    Some clients send one newline-terminated login string at payload offset 0.
    Another observed variant packs two newline-terminated copies back-to-back
    before the first NUL. The account lookup key is still the first line.
    """

    if not raw_login:
        return ''

    for candidate in raw_login.splitlines():
        if candidate:
            return candidate
    return raw_login.rstrip('\r\n')


def _is_kage_bootstrap_variant(message: SnapMessage) -> bool:
    """Return whether one message uses the KAGE bootstrap payload layout."""

    return message.footer_bytes == FOOTER_BYTES_KAGE


def _encrypt_blowfish_ecb(key: bytes, payload: bytes) -> bytes:
    """Encrypt bytes using Blowfish ECB with zero padding."""

    padded = _pad_block(payload, 8)
    cipher = Cipher(decrepit_algorithms.Blowfish(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _decrypt_blowfish_ecb(key: bytes, payload: bytes) -> bytes:
    """Decrypt bytes using Blowfish ECB."""

    padded = _pad_block(payload, 8)
    cipher = Cipher(decrepit_algorithms.Blowfish(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    return decryptor.update(padded) + decryptor.finalize()


def _pad_block(payload: bytes, block_size: int) -> bytes:
    """Pad payload with null bytes up to the next block boundary."""

    missing = (-len(payload)) % block_size
    if missing == 0:
        return payload
    return payload + (b'\x00' * missing)


def _build_bootstrap_login_payload(
    *,
    magic_key: bytes,
    seed: str,
    server_secret: str,
    server_port: int,
    bootstrap_key: bytes,
) -> bytes:
    """Build the encrypted standard bootstrap challenge payload."""

    seed_bytes = seed.encode('utf-8')
    server_secret_bytes = server_secret.encode('utf-8')

    packed_secret = struct.pack('128s', server_secret_bytes)
    magic = _encrypt_blowfish_ecb(magic_key, packed_secret)
    challenge = struct.pack(
        '>HHL128s3L128sL',
        0,
        server_port,
        len(seed_bytes),
        seed_bytes,
        len(magic),
        len(magic),
        0,
        magic,
        0,
    )
    return _encrypt_blowfish_ecb(bootstrap_key, challenge)


def _build_kage_login_success_payload(
    *,
    login_field: str,
    key_material: bytes,
    server_host: str,
    server_port: int,
) -> bytes:
    """Build direct `0x2d` payload for `SLUS_204.98` bootstrap login success."""

    ip_int = int.from_bytes(socket.inet_aton(server_host), byteorder='big', signed=False)
    plaintext = struct.pack(
        '>40s6L',
        login_field.encode('utf-8'),
        ip_int,
        server_port,
        server_port,
        0,
        0,
        0,
    )
    return _encrypt_blowfish_ecb(key_material, plaintext)


def _verify_bootstrap_answer(*, payload: bytes, bootstrap_key: bytes, server_secret: str) -> bool:
    """Check whether bootstrap verifier proves the expected server secret."""

    clear = _decrypt_blowfish_ecb(bootstrap_key, payload)
    extracted_secret = get_c_string(clear, 8)
    return extracted_secret == server_secret


def _build_login_success_payload(
    *,
    login: str,
    server_host: str,
    server_port: int,
    bootstrap_key: bytes,
) -> bytes:
    """Build the encrypted bootstrap login-success payload."""

    # `SLUS_206.42` `kkLoginClient` stores the raw login field at `app + 1188`
    # (`strncpy` at `0x002ef58c`), and `kkBootStrapLoginSuccess` compares the
    # decrypted `0x2d` login field against that stored buffer with `strcmp`
    # (`0x002ec7b8..0x002ec7d0`). The raw client field is newline-terminated, so
    # this echoed field must preserve that newline byte to satisfy the protocol.
    login_bytes = f'{login}\n'.encode('utf-8')
    ip_int = int.from_bytes(socket.inet_aton(server_host), byteorder='big', signed=False)
    plaintext = struct.pack('>40s6L', login_bytes, ip_int, server_port, server_port, 0, 0, 0)
    return _encrypt_blowfish_ecb(bootstrap_key, plaintext)


def _resolve_game_target_host(
    *,
    context: HandlerContext,
    game_identifier: str,
    target_host: str,
    client_host: str,
) -> str:
    """Resolve the advertised host for the selected game target."""

    configured_host = target_host.strip()
    if configured_host and configured_host not in {'0.0.0.0', '::'}:
        return configured_host

    if game_identifier == context.config.server.game_identifier:
        return _resolve_advertise_host(
            configured_host=context.config.server.game.advertise_host,
            bind_host=context.config.server.game.host,
            client_host=client_host,
        )

    LOGGER.warning(
        (
            'Bootstrap target %r does not define a concrete host; '
            'falling back to 127.0.0.1. Configure OPENSNAP_GAME_SERVER_MAP.'
        ),
        game_identifier,
    )
    return '127.0.0.1'


def _resolve_advertise_host(*, configured_host: str, bind_host: str, client_host: str) -> str:
    """Resolve the host advertised in bootstrap payloads."""

    configured = configured_host.strip()
    if configured and configured not in {'0.0.0.0', '::'}:
        return configured

    bound = bind_host.strip()
    if bound and bound not in {'0.0.0.0', '::'}:
        return bound

    routed = _resolve_local_host_for_client(client_host)
    if routed:
        return routed

    LOGGER.warning(
        (
            'Unable to resolve advertised host from bind=%s for client %s; '
            'falling back to 127.0.0.1. Set the service advertise host to your LAN IP.'
        ),
        bind_host,
        client_host,
    )
    return '127.0.0.1'


def _resolve_local_host_for_client(client_host: str) -> str:
    """Resolve the local IPv4 selected by routing toward one client."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((client_host, 9))
            local_host = str(probe.getsockname()[0]).strip()
    except OSError:
        return ''

    if not local_host or local_host in {'0.0.0.0', '::'}:
        return ''
    return local_host


def _build_login_fail_payload(*, reason_code: int = BOOTSTRAP_LOGIN_FAIL_REASON_GENERIC) -> bytes:
    """Build the fixed bootstrap login-failure payload."""

    return struct.pack('>2L', 0, reason_code)
