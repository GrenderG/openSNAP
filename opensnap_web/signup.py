"""Account signup pages shared by every SN@P game's in-game browser.

The client browser saves the ID from the `AM-USA-COMP-SIGNUP` / `INPUT-IDS`
special tags of the result page to the memory card; games only differ in the
URL prefixes they open. Accounts are shared by every game.

Result page tag (same browser code in `SLUS_206.42` `browser.bin`,
`SLUS_204.98` `browser.bin`, `SLUS_208.96` main and `SLUS_207.65` `netwk.bin`,
whose tag table `0x0060dbc0` and handler `0x005ace5c` match MH's):
- `COMP-SIGNUP` (page type 4) also sets the browser's close-button flag
  (`+400`, MH `0x0023c028`): the browser adds a `閉じる` button whose element
  always opens `https://www01.kddi-mmbb.jp/<n>/CRS-top.jsp` (MH `lobby.bin`
  `0x00628960` creates it with `+93` = 0 -> `0x0063ccac`), which fails here.
- `AM-USA-COMP-SIGNUP` (page type 37) only records the page type (MH
  `0x0023c170`, AM `0x006d9e20`, Beta1 `0x008c6528`), so no button; its
  `INPUT-IDS` text ends at `<!--INPUT-IDE-->` (at most 15 characters, MH
  `0x0023c0b0`, AM `0x006d9d44`, Beta1 `0x008c644c`) and goes to the same ID
  store as on a `COMP-SIGNUP` page. The username is followed by a newline
  inside that text, as on the original pages: the ID is sent as the login
  field, and the bootstrap's `0x2d` login echo (with its newline) must match it.
Return link: a link whose `HREF` starts with one of the browser's exit names
ends the browser like its pad menu does (the `HREF` is kept raw, MH
`0x00641630`; link press `0x0063dfa0` -> exit `0x0063f5b0`), after which the
game saves the stored ID. Names and the exit reason they leave the game:
- MH NA/EU (`lobby.bin` `0x0063dfa0`), Outbreak (`netwk.bin` `0x00596fd0`) and
  AM release (`browser.bin` `0x006d7040`): `AMUSA_MENU_BACK` = 1,
  `AMUSA_GAME_BACK` = 2. The pad
  menu's exit leaves 0. MH ends reasons 0/1 the same way and only 2 differently
  (main `0x00249520`); AM release only branches on 2 after `SaveNetFile`
  (`net_menu_sega_new` `0x0020a368`). Outbreak saves the ID on both and only
  makes it the active login on 2 (`netwk.bin` `0x00608290` state 7). So these
  games get `AMUSA_MENU_BACK`.
- AM Beta1 (`browser.bin` `0x008c3cc0`): only `AMUSA_GAME_BACK`, which exits
  without a reason (`0x008c4da0`), like its pad menu.

Binary limits:
- Username: the browser keeps at most 15 characters of the `INPUT-IDS` text,
  newline included
  (10 on a `COMP-SIGNUP` page: AM `0x006d9d04`, Beta1 `0x008c641c`, MH
  `0x0023c070`) and stores it in the 16-byte `receive_login_name` (AM
  `0x006cba88`). Every game logs in with that whole ID: Monster Hunter passes
  its 16-byte ID buffer to `kkLoginClient` (`0x0024a24c`); Auto Modellista
  saves it to `Net_CN_Data` (`net_menu_sega_new` `strcpy`) and
  `lbc_prelogin_00` `strcpy`s it into the login buffer `cw+0x280`. (Choosing
  a memory-card account only rewrites its first 6 bytes,
  `lbc_login_selection_account_03` `strncpy(.., 6)`, which keeps the ID of the
  account in `Net_CN_Data`.) Usernames stay within the 10 characters every
  existing account was created with.
- Password: typed in-game at every login, through a 15-character keyboard (AM
  `net_pass_entry` -> `kb_input_init2(.., 15)`, MH
  `Net_kb_input_init2(76, 312, password, 15)`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import re

from flask import Blueprint, Response, request

from opensnap.core.accounts import verify_password
from opensnap.core.browser_pages import ERROR_COLOR, NOTE_COLOR, TITLE_COLOR, document, panel
from opensnap.storage.interfaces import AccountStore, DuplicateAccountError
from opensnap_web.common import add_routes, html_response, page_view

LOGGER = logging.getLogger('opensnap.web.signup')

# Longest username accepted (see the module docstring).
MAX_USERNAME_LENGTH = 10
MIN_USERNAME_LENGTH = 4
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 15
# Letters, digits and single inner underscores.
USERNAME_PATTERN = re.compile(
    rf'(?!_)(?!.*__)(?!.*_$)[A-Za-z0-9_]{{{MIN_USERNAME_LENGTH},{MAX_USERNAME_LENGTH}}}'
)

def _page(title: str, body: str, *, title_color: str = TITLE_COLOR) -> str:
    """One signup page: a centered panel (shared browser page style)."""

    return document(panel(title, body, title_color=title_color))


SIGNUP_INDEX_PAGE = _page(
    'openSNAP signup service',
    'Create an account with a new username<br>\n'
    'and password, or log in with an existing one.<br>\n'
    '<br>\n'
    '<form action="create_id.html" method="post">\n'
    'Username: '
    f'<input type="text" name="username" size="{MAX_USERNAME_LENGTH}" maxlength="{MAX_USERNAME_LENGTH}">'
    '<br>\n'
    'Password: '
    f'<input type="password" name="password" size="{MAX_PASSWORD_LENGTH}" maxlength="{MAX_PASSWORD_LENGTH}">'
    '<br>\n'
    '<br>\n'
    f'<font color="{NOTE_COLOR}">'
    f'Username: {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} letters, digits or _.<br>\n'
    f'Password: {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.</font><br>\n'
    '<br>\n'
    '<center><input type="submit" value="Create / Log in"></center>\n'
    '</form>\n',
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


# Return link of the games without their own entry in `return_links`.
DEFAULT_RETURN_LINK = 'AMUSA_MENU_BACK'


def add_signup_routes(
    blueprint: Blueprint,
    service: SignupService,
    prefixes: tuple[str, ...],
    *,
    root_aliases: bool = False,
    return_links: Mapping[str, str] | None = None,
) -> None:
    """Serve the signup index and create-id pages under each prefix.

    `return_links` maps a prefix to its game's browser exit link when that
    game lacks `DEFAULT_RETURN_LINK` (see the module docstring).
    """

    # The form posts to the relative `create_id.html`, so every directory that
    # serves the form also serves the create-id pages.
    directories = [f'/{prefix}/' for prefix in prefixes]
    index_paths = [path for directory in directories for path in (directory, f'{directory}index.jsp')]
    if root_aliases:
        directories.append('/')
        index_paths += ['/', '/login.php']
    add_routes(blueprint, index_paths, page_view(SIGNUP_INDEX_PAGE))

    def create_id(username: str | None = None, **_kwargs: str) -> Response:
        if username is None:
            username = request.values.get('username') or ''
        password = (request.values.get('password') or '').strip()
        prefix = request.path.rsplit('/', 1)[0].strip('/')
        return_link = (return_links or {}).get(prefix, DEFAULT_RETURN_LINK)
        return _signup_response(service, username.strip(), password, return_link)

    add_routes(blueprint, [f'{directory}create_id.html' for directory in directories], create_id, methods=('GET', 'POST'))
    add_routes(blueprint, [f'{directory}create_id_<username>.html' for directory in directories], create_id)


def _signup_response(service: SignupService, username: str, password: str, return_link: str) -> Response:
    if not is_valid_username(username):
        return _error_page('Invalid username.')
    if not is_valid_password(password):
        return _error_page('Invalid password.')
    result = service.create_or_login(username=username, password=password)
    if not result.ok:
        return _error_page(result.error_message)
    return html_response(
        _page(
            'Account ready',
            f'Welcome, <b>{result.username}</b>!<br>\n'
            '<br>\n'
            'Return to the game to save your username<br>\n'
            'to the memory card.<br>\n'
            '<br>\n'
            f'<center><a href="{return_link}">Return to the game</a></center>\n',
        )
        + '<!--AM-USA-COMP-SIGNUP-->\n'
        # The saved ID keeps the newline: it is the login field the client
        # sends, and `kkBootStrapLoginSuccess` `strcmp`s it with the `0x2d`
        # login echo, which ends with a newline (bootstrap handlers).
        f'<!--INPUT-IDS-->{result.username}\n<!--INPUT-IDE-->\n'
    )


def _error_page(message: str) -> Response:
    # Back to the signup form of the same directory (`/<prefix>/create_id*.html` -> `/<prefix>/`).
    retry = request.path.rsplit('/', 1)[0] + '/'
    return html_response(
        _page(
            'Login error',
            f'{message}<br>\n'
            '<br>\n'
            f'<center><a href="{retry}">Try again</a></center>\n',
            title_color=ERROR_COLOR,
        )
    )
