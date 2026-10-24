"""Account directory services."""

import base64
import hashlib
import secrets
import string
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PASSWORD_RECORD_PREFIX = 'v1'
PASSWORD_RECORD_SEPARATOR = '$'
RECOVERY_CODE_LENGTH = 12
RECOVERY_CODE_ALPHABET = string.ascii_uppercase + string.digits


@dataclass(slots=True)
class Account:
    """User account state."""

    user_id: int
    username: str
    password_verifier: str
    bootstrap_magic_key: bytes
    seed: str
    team: str
    # The password as typed in a KAGE-SDK game, which decrypts its bootstrap
    # login reply (`0x2d`) with it; empty until stored by a signup-page login.
    kage_login_key: bytes = b''
    # Sets a new password on the signup page's recovery form. Kept readable
    # because every signup-page login shows it again.
    recovery_code: str = ''

    @property
    def session_material(self) -> str:
        """Expose stable material used for session-id derivation."""

        return self.password_verifier


def build_account(
    *,
    user_id: int,
    username: str,
    password_record: str,
    seed: str,
    team: str,
    kage_login_key: bytes = b'',
    recovery_code: str = '',
) -> Account:
    """Build account model from serialized storage fields."""

    verifier, magic_key = parse_password_record(password_record)
    return Account(
        user_id=user_id,
        username=username,
        password_verifier=verifier,
        bootstrap_magic_key=magic_key,
        seed=seed,
        team=team,
        kage_login_key=kage_login_key,
        recovery_code=recovery_code,
    )


def generate_recovery_code() -> str:
    """Return a new random recovery code."""

    return ''.join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))


def generate_seed() -> str:
    """Return a new random account seed."""

    return secrets.token_hex(16)


def encode_password_record(password: str, seed: str) -> str:
    """Return the stored form of a password: `v1$<verifier>$<bootstrap key hex>`."""

    verifier, magic_key = derive_password_material(password, seed)
    return PASSWORD_RECORD_SEPARATOR.join((PASSWORD_RECORD_PREFIX, verifier, magic_key.hex()))


def parse_password_record(password_record: str) -> tuple[str, bytes]:
    """Return the verifier and bootstrap key of a stored password record."""

    _, verifier, magic_key_hex = password_record.split(PASSWORD_RECORD_SEPARATOR, maxsplit=2)
    return verifier, bytes.fromhex(magic_key_hex)


def derive_password_material(password: str, seed: str) -> tuple[str, bytes]:
    """Derive verifier and bootstrap key from cleartext password."""

    verifier = hashlib.sha256(password.encode('utf-8')).hexdigest()

    digest = hashlib.sha1()
    digest.update(password.encode('utf-8'))
    digest.update(seed.encode('utf-8'))
    return verifier, digest.digest()


def verify_password(account: Account, password: str) -> bool:
    """Check a cleartext password against the account verifier."""

    verifier, _ = derive_password_material(password, account.seed)
    return verifier == account.password_verifier


def seal_kage_login_key(server_secret: str, username: str, key: bytes) -> str:
    """Encrypt a KAGE login key for storage (AES-GCM, bound to the username)."""

    nonce = secrets.token_bytes(12)
    sealed = AESGCM(_kage_sealing_key(server_secret)).encrypt(nonce, key, username.encode('utf-8'))
    return base64.b64encode(nonce + sealed).decode('ascii')


def open_kage_login_key(server_secret: str, username: str, stored: str | None) -> bytes:
    """Decrypt a stored KAGE login key; empty when unset or unreadable.

    It is unreadable after `OPENSNAP_SERVER_SECRET` changes; the player then
    stores it again with one login on the game's signup page.
    """

    if not stored:
        return b''
    try:
        raw = base64.b64decode(stored, validate=True)
        return AESGCM(_kage_sealing_key(server_secret)).decrypt(raw[:12], raw[12:], username.encode('utf-8'))
    except (ValueError, InvalidTag):
        return b''


def _kage_sealing_key(server_secret: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b'openSNAP KAGE login key',
    ).derive(server_secret.encode('utf-8'))
