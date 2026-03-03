"""SNAP bootstrap authentication handlers."""

from dataclasses import dataclass
import logging
import socket
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.core.context import HandlerContext
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY, FLAG_RESPONSE, FOOTER_BYTES_KAGE
from opensnap.protocol.fields import get_c_string
from opensnap.protocol.models import SnapMessage

LOGGER = logging.getLogger('opensnap.auth')


class BootstrapAuthenticator:
    """Implements bootstrap and KICS login handlers."""

    def handle_login_client(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLoginClient`."""

        raw_login = _get_login_client_raw_name(message.payload)
        login = _parse_login_client_name(raw_login)
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
                    type_flags=CHANNEL_LOBBY,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                )
            ]

        session = context.sessions.create_or_replace(message.endpoint, account)
        if _uses_kage_bootstrap_variant(message):
            advertised_host = _resolve_advertise_host(
                configured_host=context.config.server.advertise_host,
                bind_host=context.config.server.host,
                client_host=message.endpoint.host,
            )
            challenge_payload = _build_kage_bootstrap_payload(
                login_field=raw_login,
                key_material=account.bootstrap_magic_key.hex().encode('ascii'),
                server_host=advertised_host,
                server_port=context.config.server.port,
            )
        else:
            challenge_payload = _build_bootstrap_login_payload(
                magic_key=account.bootstrap_magic_key,
                seed=account.seed,
                server_secret=context.config.server.server_secret,
                server_port=context.config.server.port,
                bootstrap_key=context.config.server.bootstrap_key,
            )

        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY,
                command=commands.CMD_BOOTSTRAP_LOGIN_SWAN,
                payload=challenge_payload,
                session_id=session.session_id,
            )
        ]

    def handle_bootstrap_check(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkBootStrapLoginSWAN_CHECK`."""

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
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                    payload=_build_login_fail_payload(),
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
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                    payload=_build_login_fail_payload(),
                )
            ]

        advertised_host = _resolve_advertise_host(
            configured_host=context.config.server.advertise_host,
            bind_host=context.config.server.host,
            client_host=message.endpoint.host,
        )
        success_payload = _build_login_success_payload(
            login=session.username,
            server_host=advertised_host,
            server_port=context.config.server.port,
            bootstrap_key=context.config.server.bootstrap_key,
        )

        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                command=commands.CMD_BOOTSTRAP_LOGIN_SUCCESS,
                payload=success_payload,
                session_id=session.session_id,
            )
        ]

    def handle_login_to_kics(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLoginToKICS`."""

        session = context.sessions.get_by_endpoint(message.endpoint)
        if session is None:
            LOGGER.warning(
                'Ignoring login-to-kics from %s:%d: no authenticated session is bound to this endpoint.',
                message.endpoint.host,
                message.endpoint.port,
            )
            return []

        if len(message.payload) < 0x130:
            LOGGER.warning(
                (
                    'Received short login-to-kics payload from %s:%d '
                    '(len=%d, expected>=304); parsing available fields only.'
                ),
                message.endpoint.host,
                message.endpoint.port,
                len(message.payload),
            )

        parsed = _parse_kics_login_payload(message.payload)
        context.accounts.set_team(session.user_id, parsed.team)
        payload = struct.pack('>3L', context.config.server.port, 0x01234567, session.session_id)

        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY,
                command=commands.CMD_RESULT_LOGIN_TO_KICS,
                payload=payload,
                session_id=session.session_id,
            )
        ]


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


def _uses_kage_bootstrap_variant(message: SnapMessage) -> bool:
    """Return whether one message uses the KAGE bootstrap variant."""

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
    """Pad payload with null bytes up to block boundary."""

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
    """Build encrypted bootstrap challenge payload."""

    seed_bytes = seed.encode('utf-8')
    server_secret_bytes = server_secret.encode('utf-8')

    packed_secret = struct.pack('128s', server_secret_bytes)
    magic = _encrypt_blowfish_ecb(magic_key, packed_secret)

    # The challenge payload matches the observed struct layout.
    # Fields include server port, seed length and data, and encrypted magic blob.
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


def _build_kage_bootstrap_payload(
    *,
    login_field: str,
    key_material: bytes,
    server_host: str,
    server_port: int,
) -> bytes:
    """Build the KAGE bootstrap payload for the KAGE `0x2c` variant."""

    ip_int = int.from_bytes(socket.inet_aton(server_host), byteorder='big', signed=False)
    login_bytes = login_field.encode('utf-8')
    # KAGE documents the last metadata word as the size of the data appended after
    # the packed header ("size of following data, sent back when logging to lobby").
    # This client variant decrypts a fixed 0x118-byte blob, so the trailing size must
    # describe the zero-padded bytes we append after this header.
    trailing_data = b'\x00' * (0x118 - struct.calcsize('>40s6L'))
    plaintext = struct.pack(
        '>40s6L',
        login_bytes,
        ip_int,
        server_port,
        server_port,
        0,
        0,
        len(trailing_data),
    )
    padded_plaintext = plaintext + trailing_data
    return _encrypt_blowfish_ecb(key_material, padded_plaintext)


def _verify_bootstrap_answer(*, payload: bytes, bootstrap_key: bytes, server_secret: str) -> bool:
    """Check decrypted challenge answer secret."""

    clear = _decrypt_blowfish_ecb(bootstrap_key, payload)
    extracted_secret = get_c_string(clear, 8)
    if extracted_secret == server_secret:
        return True
    return _matches_wrapped_bootstrap_verifier(clear)


def _matches_wrapped_bootstrap_verifier(clear: bytes) -> bool:
    """Check the structured wrapped-verifier shape seen in release captures.

    Some release-client captures send the `0x41` verifier as a fixed-width
    wrapper: `0x00000080`, `0x00000000`, then 128 bytes whose first 32 bytes
    are variable and whose remaining 96 bytes are the same 8-byte block
    repeated. That matches a Blowfish-ECB encrypted short secret padded with
    zeros, and is materially more constrained than accepting any non-empty blob.
    """

    if len(clear) < 136:
        return False

    declared_size, reserved = struct.unpack_from('>2L', clear, 0)
    if declared_size != 0x80 or reserved != 0:
        return False

    wrapped = clear[8:136]
    repeated_block = wrapped[32:40]
    if len(repeated_block) != 8:
        return False
    if repeated_block == b'\x00' * 8:
        return False

    return wrapped[32:] == repeated_block * 12


def _build_login_success_payload(
    *,
    login: str,
    server_host: str,
    server_port: int,
    bootstrap_key: bytes,
) -> bytes:
    """Build encrypted login success payload."""

    login_bytes = login.encode('utf-8')
    ip_int = int.from_bytes(socket.inet_aton(server_host), byteorder='big', signed=False)
    # No trailing blob is appended to this packet, so the "size of following data"
    # metadata remains zero.
    plaintext = struct.pack('>40s6L', login_bytes, ip_int, server_port, server_port, 0, 0, 0)
    return _encrypt_blowfish_ecb(bootstrap_key, plaintext)


def _resolve_advertise_host(*, configured_host: str, bind_host: str, client_host: str) -> str:
    """Resolve host advertised in bootstrap login-success payloads."""

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
            'falling back to 127.0.0.1. Set OPENSNAP_ADVERTISE_HOST to your LAN IP.'
        ),
        bind_host,
        client_host,
    )
    return '127.0.0.1'


def _resolve_local_host_for_client(client_host: str) -> str:
    """Resolve local IPv4 selected by kernel routing toward one client."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # No packets are sent for UDP connect(), but kernel picks a source address.
            probe.connect((client_host, 9))
            local_host = str(probe.getsockname()[0]).strip()
    except OSError:
        return ''

    if not local_host or local_host in {'0.0.0.0', '::'}:
        return ''
    return local_host


def _build_login_fail_payload() -> bytes:
    """Build login failure payload."""

    return struct.pack('>2L', 0, 0x01)


@dataclass(frozen=True, slots=True)
class KicsLoginPayload:
    """Structured view of observed `kkLoginToKICS` payload fields."""

    client_ip: str
    mtu_hint: int
    client_flags: int
    version_code: int
    login: str
    region_code: int
    marker_bb: int
    marker_dd: int
    auth_blob: bytes
    team: str


def _parse_kics_login_payload(payload: bytes) -> KicsLoginPayload:
    """Parse known `kkLoginToKICS` payload offsets from packet captures."""

    return KicsLoginPayload(
        client_ip=_read_ipv4(payload, 0),
        mtu_hint=_read_u32(payload, 4),
        client_flags=_read_u32(payload, 8),
        version_code=_read_u32(payload, 12),
        login=get_c_string(payload, 16).rstrip('\n'),
        region_code=_read_u32(payload, 0x20),
        marker_bb=_read_u32(payload, 0x24),
        marker_dd=_read_u32(payload, 0x28),
        auth_blob=_slice(payload, 0x80, 0x80),
        team=get_c_string(payload, 0x128),
    )


def _read_u32(payload: bytes, offset: int) -> int:
    """Read uint32 from payload offset with zero fallback."""

    if offset + 4 > len(payload):
        return 0
    return struct.unpack_from('>L', payload, offset)[0]


def _read_ipv4(payload: bytes, offset: int) -> str:
    """Read IPv4 address from payload offset with loopback fallback."""

    raw_ip = _read_u32(payload, offset)
    try:
        return socket.inet_ntoa(struct.pack('>L', raw_ip))
    except OSError:
        return '127.0.0.1'


def _slice(payload: bytes, offset: int, length: int) -> bytes:
    """Return bounded payload slice."""

    if offset >= len(payload):
        return b''
    end = min(len(payload), offset + length)
    return payload[offset:end]
