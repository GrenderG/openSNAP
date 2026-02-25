"""SNAP bootstrap authentication handlers."""

from dataclasses import dataclass
import socket
import struct

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.decrepit.ciphers import algorithms as decrepit_algorithms
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from opensnap.core.context import HandlerContext
from opensnap.protocol import commands
from opensnap.protocol.constants import CHANNEL_LOBBY, FLAG_RESPONSE
from opensnap.protocol.fields import get_c_string
from opensnap.protocol.models import SnapMessage


class BootstrapAuthenticator:
    """Implements bootstrap and KICS login handlers."""

    def handle_login_client(self, context: HandlerContext, message: SnapMessage) -> list[SnapMessage]:
        """Handle `kkLoginClient`."""

        # Clients often include a trailing newline in login strings.
        login = get_c_string(message.payload, 0).rstrip('\n')
        account = context.accounts.get_by_name(login)
        if account is None:
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_LOBBY,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                )
            ]

        session = context.sessions.create_or_replace(message.endpoint, account)
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
            return [
                context.reply(
                    message,
                    type_flags=CHANNEL_LOBBY | FLAG_RESPONSE,
                    command=commands.CMD_BOOTSTRAP_LOGIN_FAIL,
                    payload=_build_login_fail_payload(),
                )
            ]

        success_payload = _build_login_success_payload(
            login=session.username,
            server_host=context.config.server.host,
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
            return []

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


def _verify_bootstrap_answer(*, payload: bytes, bootstrap_key: bytes, server_secret: str) -> bool:
    """Check decrypted challenge answer secret."""

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
    """Build encrypted login success payload."""

    host = server_host
    if host == '0.0.0.0':
        host = '127.0.0.1'

    # Clients expect a newline after the username in this payload.
    login_bytes = f'{login}\n'.encode('utf-8')
    ip_int = int.from_bytes(socket.inet_aton(host), byteorder='big', signed=False)
    plaintext = struct.pack('>40s6L', login_bytes, ip_int, server_port, server_port, 0xBB, 0xCC, 0xDD)
    return _encrypt_blowfish_ecb(bootstrap_key, plaintext)


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
