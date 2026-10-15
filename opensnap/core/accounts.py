"""Account directory services."""

import base64
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PASSWORD_RECORD_PREFIX = 'v1'
PASSWORD_RECORD_SEPARATOR = '$'


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
) -> Account:
    """Build account model from serialized storage fields."""

    account_seed = normalize_seed(seed)
    encoded_record = normalize_password_record(password_record, account_seed)
    verifier, magic_key = parse_password_record(encoded_record, account_seed)
    return Account(
        user_id=user_id,
        username=username,
        password_verifier=verifier,
        bootstrap_magic_key=magic_key,
        seed=account_seed,
        team=team,
        kage_login_key=kage_login_key,
    )


def normalize_seed(seed: str) -> str:
    """Return configured seed or generate one when missing."""

    cleaned = seed.strip()
    if cleaned:
        return cleaned
    return secrets.token_hex(16)


def normalize_password_record(password_record: str, seed: str) -> str:
    """Convert cleartext credentials to encoded form when needed."""

    if is_encoded_password_record(password_record):
        return password_record

    verifier, magic_key = derive_password_material(password_record, seed)
    return (
        f'{PASSWORD_RECORD_PREFIX}{PASSWORD_RECORD_SEPARATOR}'
        f'{verifier}{PASSWORD_RECORD_SEPARATOR}{magic_key.hex()}'
    )


def parse_password_record(password_record: str, seed: str) -> tuple[str, bytes]:
    """Parse encoded password record or derive from plain value."""

    if is_encoded_password_record(password_record):
        _, verifier, magic_key_hex = password_record.split(PASSWORD_RECORD_SEPARATOR, maxsplit=2)
        return verifier, bytes.fromhex(magic_key_hex)

    return derive_password_material(password_record, seed)


def is_encoded_password_record(password_record: str) -> bool:
    """Check whether password record already uses encoded format."""

    parts = password_record.split(PASSWORD_RECORD_SEPARATOR)
    if len(parts) != 3:
        return False

    if parts[0] != PASSWORD_RECORD_PREFIX:
        return False

    verifier, magic_key_hex = parts[1], parts[2]
    return _is_hex(verifier, 64) and _is_hex(magic_key_hex, 40)


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


def _is_hex(value: str, expected_length: int) -> bool:
    """Return whether value has expected hex shape."""

    if len(value) != expected_length:
        return False

    try:
        int(value, 16)
    except ValueError:
        return False
    return True


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
