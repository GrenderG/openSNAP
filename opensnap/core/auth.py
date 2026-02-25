"""SNAP bootstrap authentication handlers."""

import hashlib
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
            account_password=account.password,
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

        team = get_c_string(message.payload, 0x128)
        context.accounts.set_team(session.user_id, team)
        payload = struct.pack('>3L', context.config.server.port, 0x01234567, session.session_id)

        return [
            context.reply(
                message,
                type_flags=CHANNEL_LOBBY,
                command=0x29,
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
    account_password: str,
    seed: str,
    server_secret: str,
    server_port: int,
    bootstrap_key: bytes,
) -> bytes:
    """Build encrypted bootstrap challenge payload."""

    seed_bytes = seed.encode('utf-8')
    server_secret_bytes = server_secret.encode('utf-8')

    digest = hashlib.sha1()
    digest.update(account_password.encode('utf-8'))
    digest.update(seed_bytes)
    magic_key = digest.digest()

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

    login_bytes = f'{login}\n'.encode('utf-8')
    ip_int = int.from_bytes(socket.inet_aton(host), byteorder='big', signed=False)
    plaintext = struct.pack('>40s6L', login_bytes, ip_int, server_port, server_port, 0xBB, 0xCC, 0xDD)
    return _encrypt_blowfish_ecb(bootstrap_key, plaintext)


def _build_login_fail_payload() -> bytes:
    """Build login failure payload."""

    return struct.pack('>2L', 0, 0x01)
