"""Account signup pages shared by every SN@P game's in-game browser.

The client browser saves the ID from the `COMP-SIGNUP` / `INPUT-IDS` special
tags of the result page to the memory card; games only differ in the URL
prefixes they open. Accounts are shared by every game.

Binary limits:
- Username: on a `COMP-SIGNUP` page the browser keeps at most 10 characters of
  the `INPUT-IDS` text (same browser code in `SLUS_206.42` `browser.bin`
  `0x006d9d04`, `SLUS_204.98` `0x008c641c`, `SLUS_208.96` main `0x0023c070`)
  and stores it in the 16-byte `receive_login_name` (AM `0x006cba88`). Every
  game logs in with that whole ID: Monster Hunter passes its 16-byte ID
  buffer to `kkLoginClient` (`0x0024a24c`); Auto Modellista saves it to
  `Net_CN_Data` (`net_menu_sega_new` `strcpy`) and `lbc_prelogin_00`
  `strcpy`s it into the login buffer `cw+0x280`. (Choosing a memory-card
  account only rewrites its first 6 bytes, `lbc_login_selection_account_03`
  `strncpy(.., 6)`, which keeps the ID of the account in `Net_CN_Data`.)
- Password: typed in-game at every login, through a 15-character keyboard (AM
  `net_pass_entry` -> `kb_input_init2(.., 15)`, MH
  `Net_kb_input_init2(76, 312, password, 15)`).
"""

from dataclasses import dataclass
import logging
import re

from flask import Blueprint, Response, request

from opensnap.core.accounts import verify_password
from opensnap.storage.interfaces import AccountStore, DuplicateAccountError
from opensnap_web.common import add_routes, html_response, page_view

LOGGER = logging.getLogger('opensnap.web.signup')

# Longest ID the browser keeps, and every game logs in with.
MAX_USERNAME_LENGTH = 10
MIN_USERNAME_LENGTH = 4
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 15
# Letters, digits and single inner underscores.
USERNAME_PATTERN = re.compile(
    rf'(?!_)(?!.*__)(?!.*_$)[A-Za-z0-9_]{{{MIN_USERNAME_LENGTH},{MAX_USERNAME_LENGTH}}}'
)

SIGNUP_INDEX_PAGE = (
    '<html>\n'
    '<body>\n'
    'openSNAP signup service<br>\n'
    '<br>\n'
    'Choose the username to save on your memory card.<br>\n'
    '<br>\n'
    '<form action="create_id.html" method="post">\n'
    'Username: '
    f'<input type="text" name="username" size="{MAX_USERNAME_LENGTH}" maxlength="{MAX_USERNAME_LENGTH}">\n'
    '<br>\n'
    'Password: '
    f'<input type="password" name="password" size="{MAX_PASSWORD_LENGTH}" maxlength="{MAX_PASSWORD_LENGTH}">\n'
    '<br>\n'
    '<input type="submit" value="Create/Login ID">\n'
    '</form>\n'
    '</body>\n'
    '</html>\n'
)


@dataclass(frozen=True, slots=True)
class SignupResult:
    """Outcome of one signup/login request."""

    ok: bool
    username: str
    created: bool
    error_message: str = ''


class SignupService:
    """Create missing accounts or log in existing ones."""

    def __init__(self, accounts: AccountStore) -> None:
        self._accounts = accounts

    def create_or_login(self, *, username: str, password: str) -> SignupResult:
        account = self._accounts.get_by_name(username)
        if account is None:
            try:
                self._accounts.create_user(username, password)
            except DuplicateAccountError:
                # Another request may have created the same username concurrently.
                account = self._accounts.get_by_name(username)
                if account is None:
                    return SignupResult(False, username, False, 'Account creation failed. Please retry.')
            else:
                LOGGER.info('Created account via web signup for user %s.', username)
                return SignupResult(True, username, True)

        if not verify_password(account, password):
            LOGGER.warning('Rejected web signup/login for user %s due to password mismatch.', username)
            return SignupResult(False, username, False, 'Password mismatch for existing user.')
        LOGGER.info('Accepted web login for existing user %s.', username)
        return SignupResult(True, username, False)


def is_valid_username(username: str) -> bool:
    return USERNAME_PATTERN.fullmatch(username) is not None


def is_valid_password(password: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(password.encode('utf-8')) <= MAX_PASSWORD_LENGTH


def add_signup_routes(
    blueprint: Blueprint,
    service: SignupService,
    prefixes: tuple[str, ...],
    *,
    root_aliases: bool = False,
) -> None:
    """Serve the signup index and create-id pages under each prefix."""

    index_paths = [path for prefix in prefixes for path in (f'/{prefix}/', f'/{prefix}/index.jsp')]
    if root_aliases:
        index_paths += ['/', '/login.php']
    add_routes(blueprint, index_paths, page_view(SIGNUP_INDEX_PAGE))

    def create_id(username: str | None = None, **_kwargs: str) -> Response:
        if username is None:
            username = request.values.get('username') or ''
        password = (request.values.get('password') or '').strip()
        return _signup_response(service, username.strip(), password)

    add_routes(blueprint, [f'/{prefix}/create_id.html' for prefix in prefixes], create_id, methods=('GET', 'POST'))
    add_routes(blueprint, [f'/{prefix}/create_id_<username>.html' for prefix in prefixes], create_id)


def _signup_response(service: SignupService, username: str, password: str) -> Response:
    if not is_valid_username(username):
        return _error_page('Invalid username.')
    if not is_valid_password(password):
        return _error_page('Invalid password.')
    result = service.create_or_login(username=username, password=password)
    if not result.ok:
        return _error_page(result.error_message)
    # The browser reads `INPUT-IDS` up to the newline, which is part of the payload.
    return html_response(
        '<html>\n'
        '<body>\n'
        'Profile successfully retrieved.<br>\n'
        'Press the Select button and then "End Browser" to save it to the memory card.\n'
        '</body>\n'
        '</html>\n'
        '<!--COMP-SIGNUP-->\n'
        f'<!--INPUT-IDS-->{result.username}\n'
    )


def _error_page(message: str) -> Response:
    return html_response(
        '<html>\n'
        '<body>\n'
        '<h3>Login error</h3>\n'
        f'{message}<br>\n'
        'Please go back and retry.\n'
        '</body>\n'
        '</html>\n'
    )
