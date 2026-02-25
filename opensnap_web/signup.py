"""Signup and login flow backed by SQLite accounts."""

from dataclasses import dataclass
import logging
import sqlite3

from opensnap.config import default_app_config
from opensnap.storage.sqlite import SqliteAccountDirectory, SqliteDatabase


@dataclass(frozen=True, slots=True)
class SignupResult:
    """Outcome of signup/login request."""

    ok: bool
    username: str
    created: bool
    error_message: str = ''


class SqliteSignupService:
    """Create-or-login service for PS2 web signup pages."""

    def __init__(self) -> None:
        config = default_app_config()
        self._logger = logging.getLogger('opensnap.web.signup')
        self._database = SqliteDatabase(config.storage.sqlite_path)
        self._database.seed(config.users, config.lobbies)
        self._accounts = SqliteAccountDirectory(self._database)

    def create_or_login(self, *, username: str, password: str) -> SignupResult:
        """Create missing users or authenticate existing users."""

        account = self._accounts.get_by_name(username)
        if account is None:
            try:
                self._accounts.create_user(username, password)
            except sqlite3.IntegrityError:
                # Another request may have created the same username concurrently.
                account = self._accounts.get_by_name(username)
                if account is None:
                    return SignupResult(
                        ok=False,
                        username=username,
                        created=False,
                        error_message='Account creation failed. Please retry.',
                    )
            else:
                self._logger.info('Created account via web signup for user %s.', username)
                return SignupResult(ok=True, username=username, created=True)

        if account is None:
            return SignupResult(
                ok=False,
                username=username,
                created=False,
                error_message='Account lookup failed. Please retry.',
            )

        if not self._accounts.verify_password(account, password):
            self._logger.warning('Rejected web signup/login for user %s due to password mismatch.', username)
            return SignupResult(
                ok=False,
                username=username,
                created=False,
                error_message='Password mismatch for existing user.',
            )
        self._logger.info('Accepted web login for existing user %s.', username)
        return SignupResult(ok=True, username=username, created=False)
