"""Account directory services."""

import hashlib
import secrets
from dataclasses import dataclass

from opensnap.config import UserConfig

PASSWORD_RECORD_PREFIX = 'v1'
PASSWORD_RECORD_SEPARATOR = '$'


@dataclass(slots=True)
class Account:
    """User account state."""

    user_id: int
    username: str
    password_verifier: str
    bootstrap_magic_key: bytes
    bootstrap_login_key: bytes
    seed: str
    team: str

    @property
    def session_material(self) -> str:
        """Expose stable material used for session-id derivation."""

        return self.password_verifier


class AccountDirectory:
    """In-memory account lookup service."""

    def __init__(self, users: tuple[UserConfig, ...]) -> None:
        self._by_name: dict[str, Account] = {}
        self._by_id: dict[int, Account] = {}
        for user in users:
            account = build_account(
                user_id=user.user_id,
                username=user.username,
                password_record=user.password,
                seed=user.seed,
                team=user.team,
            )
            self._by_name[account.username] = account
            self._by_id[account.user_id] = account

    def get_by_name(self, username: str) -> Account | None:
        """Get account by username."""

        return self._by_name.get(username)

    def get_by_id(self, user_id: int) -> Account | None:
        """Get account by user id."""

        return self._by_id.get(user_id)

    def set_team(self, user_id: int, team: str) -> None:
        """Set account team string."""

        account = self._by_id.get(user_id)
        if account is not None:
            account.team = team


def build_account(
    *,
    user_id: int,
    username: str,
    password_record: str,
    seed: str,
    team: str,
) -> Account:
    """Build account model from serialized storage fields."""

    raw_password_record = password_record
    account_seed = normalize_seed(seed)
    encoded_record = normalize_password_record(password_record, account_seed)
    verifier, magic_key = parse_password_record(encoded_record, account_seed)
    login_key = _derive_bootstrap_login_key(raw_password_record)
    return Account(
        user_id=user_id,
        username=username,
        password_verifier=verifier,
        bootstrap_magic_key=magic_key,
        bootstrap_login_key=login_key,
        seed=account_seed,
        team=team,
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


def _is_hex(value: str, expected_length: int) -> bool:
    """Return whether value has expected hex shape."""

    if len(value) != expected_length:
        return False

    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _derive_bootstrap_login_key(password_record: str) -> bytes:
    """Return the cleartext bootstrap login key when available.

    `SLUS_204.98` loads the Blowfish login-success key from runtime
    `login_password`; encoded password records do not retain that cleartext.
    """

    if is_encoded_password_record(password_record):
        return b''
    return password_record.encode('utf-8')
