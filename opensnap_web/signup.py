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
  store as on a `COMP-SIGNUP` page. The bytes are copied as they are (also
  Outbreak `0x005ad114`), so the page writes the username alone: the ID is
  sent as the login field and must equal the bootstrap's `0x2d` login echo.
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
- Username: the browser keeps at most 15 characters of the `INPUT-IDS` text
  (10 on a `COMP-SIGNUP` page: AM `0x006d9d04`, Beta1 `0x008c641c`, MH
  `0x0023c070`) and stores it in the 16-byte `receive_login_name` (AM
  `0x006cba88`). Every game logs in with that whole ID: Monster Hunter passes
  its 16-byte ID buffer to `kkLoginClient` (`0x0024a24c`); Auto Modellista
  saves it to `Net_CN_Data` (`net_menu_sega_new` `strcpy`) and
  `lbc_prelogin_00` `strcpy`s it into the login buffer `cw+0x280`. (Choosing
  a memory-card account only rewrites its first 6 bytes,
  `lbc_login_selection_account_03` `strncpy(.., 6)`, which keeps the ID of the
  account in `Net_CN_Data`.) MH and Outbreak keep the same 15 characters
  end to end. No client checks more than a non-empty ID.
- AM Beta1 logs in with at most 7 ID characters: `lbc_prelogin_00` copies
  the ID to `cw+640` (`0x002c7298`) and again to `cw+648` (`0x002c72ac`),
  and `cw+640` is the login name (`0x0035de24`). From 8 characters on, the
  name sent is `ID[:8] + ID`, and the KICS login keeps only its first 16
  bytes. Its signup page caps the username at 7 (`max_username_lengths`),
  so a longer shared account cannot be saved to a Beta1 memory card.
- Password: typed in-game at every login, through a 15-character keyboard (AM
  `net_pass_entry` -> `kb_input_init2(.., 15)`, MH
  `Net_kb_input_init2(76, 312, password, 15)`).

Forms: `<FORM>` and `</FORM>` each start a new line (MH `lobby.bin`
`0x00646d50`, AM `tagoutprintf6` `0x006e2d10`), and a submit button sends
only the fields of the nearest `FORM` before it (MH `0x0063e630`, AM
`FormHandler` `0x006d74e0`) but not its own name. So the recovery form's
button shares a line with the signup button only from a cell of its own.

Logo: `IMG` takes `SRC/WIDTH/HEIGHT/ALT` (MH main `0x35e330`, AM `0x73a940`,
Beta1 `0x8d7d70`) and resolves a relative `SRC` against the page. The decoder
is chosen by the URL extension alone (AM `BsUrlFileExtensionGet` `0x6cf980`):
`.png` or `.bmp`, no GIF or JPEG. AM only decodes 8-bit RGB PNGs (colour type
2, no palette or alpha) of at most 256x256 (`plPNGSetContextFromImage`
`0x6c1150`, Beta1 `0x8d2c40`) and ignores interlacing, so the logo is a plain
RGB PNG flattened on the page background.
"""

from pathlib import Path

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import re

from flask import Blueprint, Response, request

from opensnap.core.accounts import RECOVERY_CODE_ALPHABET, RECOVERY_CODE_LENGTH, verify_password
from opensnap.core.browser_pages import ERROR_COLOR, NOTE_COLOR, TITLE_COLOR, document, panel
from opensnap.storage.interfaces import AccountStore, DuplicateAccountError
from opensnap_web.common import add_routes, html_response, page_view

LOGGER = logging.getLogger('opensnap.web.signup')

# Longest username accepted (see the module docstring).
MAX_USERNAME_LENGTH = 15
MIN_USERNAME_LENGTH = 2
MIN_PASSWORD_LENGTH = 4
MAX_PASSWORD_LENGTH = 15
# Letters, digits and single inner underscores.
USERNAME_PATTERN = re.compile(r'(?!_)(?!.*__)(?!.*_$)[A-Za-z0-9_]+')
RECOVERY_CODE_PATTERN = re.compile(f'[{RECOVERY_CODE_ALPHABET}]{{{RECOVERY_CODE_LENGTH}}}')
# The recovery code on the logged-in page.
RECOVERY_CODE_BACKGROUND = '#000000'
RECOVERY_CODE_COLOR = '#FFFFFF'
# The recovery form, next to the signup form.
RECOVER_PAGE = 'recover.html'

# Served next to every signup page (see the module docstring).
LOGO_NAME = 'opensnap_logo.png'
LOGO_PNG = (Path(__file__).resolve().parents[1] / 'data' / 'logo' / 'opensnap_logo_ps2.png').read_bytes()
LOGO_HTML = f'<img src="{LOGO_NAME}" width="240" height="42" alt="openSN@P"><br>\n<br>\n'


def _page(title: str, body: str, *, title_color: str = TITLE_COLOR, rows: tuple[str, ...] = (), columns: int = 1) -> str:
    """One signup page: the logo over a centered panel (shared browser page style)."""

    return document(LOGO_HTML + panel(title, body, title_color=title_color, rows=rows, columns=columns))


def _field(label: str, kind: str, name: str, length: int) -> str:
    return f'{label}: <input type="{kind}" name="{name}" size="{length}" maxlength="{length}"><br>\n'


def signup_index_page(max_username_length: int = MAX_USERNAME_LENGTH) -> str:
    """The signup form, for usernames of at most `max_username_length` characters."""

    # The recovery form sits in the button row's second cell (see the module docstring).
    return _page(
        'openSNAP signup service',
        'Create an account with a new username<br>\n'
        'and password, or log in with an existing one.<br>\n'
        '<br>\n'
        '<form action="create_id.html" method="post">\n'
        + _field('Username', 'text', 'username', max_username_length)
        + _field('Password', 'password', 'password', MAX_PASSWORD_LENGTH)
        + '<br>\n'
        f'<font color="{NOTE_COLOR}">'
        f'Username: {MIN_USERNAME_LENGTH}-{max_username_length} letters, digits or _.<br>\n'
        f'Password: {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.</font><br>\n',
        rows=(
            '<tr><td align="center"><input type="submit" value="Create / Log in"></form></td>'
            f'<td align="center"><form action="{RECOVER_PAGE}"><input type="submit" value="Recover account"></form>'
            '</td></tr>',
        ),
        columns=2,
    )


def recover_index_page(directory: str, max_username_length: int = MAX_USERNAME_LENGTH) -> str:
    """The recovery form of the signup `directory`: username, recovery code and a new password."""

    return _page(
        'Recover account',
        'Enter your username, your recovery code<br>\n'
        'and a new password for the account.<br>\n'
        '<br>\n'
        '<form action="recover_id.html" method="post">\n'
        + _field('Username', 'text', 'username', max_username_length)
        + _field('Recovery code', 'text', 'recovery_code', RECOVERY_CODE_LENGTH)
        + _field('New password', 'password', 'password', MAX_PASSWORD_LENGTH)
        + '<br>\n'
        f'<font color="{NOTE_COLOR}">'
        f'Recovery code: {RECOVERY_CODE_LENGTH} letters or digits.<br>\n'
        f'Password: {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters.</font><br>\n'
        '<br>\n'
        'Your recovery code changes once it is used.<br>\n'
        '<br>\n'
        '<center><input type="submit" value="Recover account"></center>\n'
        '</form>\n'
        '<br>\n'
        f'<center><a href="{directory}">Back</a></center>\n',
    )


@dataclass(frozen=True, slots=True)
class SignupResult:
    """Outcome of one signup, login or recovery request."""

    ok: bool
    username: str
    created: bool
    error_message: str = ''
    recovery_code: str = ''


class SignupService:
    """Create missing accounts, log in existing ones or recover them."""

    def __init__(self, accounts: AccountStore) -> None:
        self._accounts = accounts

    def create_or_login(self, *, username: str, password: str, store_kage_login_key: bool = False) -> SignupResult:
        """Create or log in; on KAGE-SDK signup pages also store the KAGE login key."""

        account = self._accounts.get_by_name(username)
        created = False
        if account is None:
            try:
                account = self._accounts.create_user(username, password)
                created = True
            except DuplicateAccountError:
                # Another request may have created the same username concurrently.
                account = self._accounts.get_by_name(username)
                if account is None:
                    return SignupResult(False, username, False, 'Account creation failed. Please retry.')

        if not created and not verify_password(account, password):
            LOGGER.warning('Rejected web signup/login for user %s due to password mismatch.', username)
            return SignupResult(False, username, False, 'Password mismatch for existing user.')
        if store_kage_login_key:
            self._accounts.set_kage_login_key(account, password.encode('utf-8'))
        LOGGER.info('%s account via web signup for user %s.', 'Created' if created else 'Accepted', username)
        return SignupResult(True, username, created, recovery_code=account.recovery_code)

    def recover(
        self, *, username: str, recovery_code: str, password: str, store_kage_login_key: bool = False
    ) -> SignupResult:
        """Set a new password and recovery code; a success is also a login."""

        account = self._accounts.recover(username, recovery_code, password)
        if account is None:
            LOGGER.warning('Rejected web account recovery for user %s.', username)
            return SignupResult(False, username, False, 'Wrong username or recovery code.')
        if store_kage_login_key:
            self._accounts.set_kage_login_key(account, password.encode('utf-8'))
        LOGGER.info('Recovered account via web signup for user %s.', username)
        return SignupResult(True, username, False, recovery_code=account.recovery_code)


def is_valid_username(username: str, max_length: int = MAX_USERNAME_LENGTH) -> bool:
    return MIN_USERNAME_LENGTH <= len(username) <= max_length and USERNAME_PATTERN.fullmatch(username) is not None


def is_valid_password(password: str) -> bool:
    return MIN_PASSWORD_LENGTH <= len(password.encode('utf-8')) <= MAX_PASSWORD_LENGTH


def is_valid_recovery_code(recovery_code: str) -> bool:
    return RECOVERY_CODE_PATTERN.fullmatch(recovery_code) is not None


# Return link of the games without their own entry in `return_links`.
DEFAULT_RETURN_LINK = 'AMUSA_MENU_BACK'


@dataclass(frozen=True, slots=True)
class _Directory:
    """What one signup directory's pages need."""

    return_link: str
    store_kage_login_key: bool
    max_username_length: int


def add_signup_routes(
    blueprint: Blueprint,
    service: SignupService,
    prefixes: tuple[str, ...],
    *,
    root_aliases: bool = False,
    return_links: Mapping[str, str] | None = None,
    kage_prefixes: tuple[str, ...] = (),
    max_username_lengths: Mapping[str, int] | None = None,
) -> None:
    """Serve the signup and recovery pages under each prefix.

    `return_links` maps a prefix to its game's browser exit link when that
    game lacks `DEFAULT_RETURN_LINK` (see the module docstring). Logins on
    `kage_prefixes` (KAGE-SDK games) also store the KAGE login key.
    `max_username_lengths` maps a prefix to a shorter username limit.
    """

    # The forms post to relative pages, so every directory that serves the
    # forms also serves the pages they post to.
    max_username_lengths = max_username_lengths or {}
    directories = [f'/{prefix}/' for prefix in prefixes]
    for prefix in prefixes:
        max_username_length = max_username_lengths.get(prefix, MAX_USERNAME_LENGTH)
        add_routes(blueprint, [f'/{prefix}/', f'/{prefix}/index.jsp'], page_view(signup_index_page(max_username_length)))
        add_routes(blueprint, [f'/{prefix}/{RECOVER_PAGE}'], page_view(recover_index_page(f'/{prefix}/', max_username_length)))
    if root_aliases:
        directories.append('/')
        add_routes(blueprint, ['/', '/login.php'], page_view(signup_index_page()))
        add_routes(blueprint, [f'/{RECOVER_PAGE}'], page_view(recover_index_page('/')))

    def current_directory() -> _Directory:
        prefix = request.path.rsplit('/', 1)[0].strip('/')
        return _Directory(
            (return_links or {}).get(prefix, DEFAULT_RETURN_LINK),
            prefix in kage_prefixes,
            max_username_lengths.get(prefix, MAX_USERNAME_LENGTH),
        )

    def create_id(username: str | None = None, **_kwargs: str) -> Response:
        if username is None:
            username = request.values.get('username') or ''
        password = (request.values.get('password') or '').strip()
        return _signup_response(service, current_directory(), username.strip(), password)

    def recover_id() -> Response:
        return _recover_response(
            service,
            current_directory(),
            (request.values.get('username') or '').strip(),
            # The code is upper case; the browser keyboard may type it in lower case.
            (request.values.get('recovery_code') or '').strip().upper(),
            (request.values.get('password') or '').strip(),
        )

    add_routes(blueprint, [f'{directory}create_id.html' for directory in directories], create_id, methods=('GET', 'POST'))
    add_routes(blueprint, [f'{directory}create_id_<username>.html' for directory in directories], create_id)
    add_routes(blueprint, [f'{directory}recover_id.html' for directory in directories], recover_id, methods=('GET', 'POST'))
    add_routes(
        blueprint,
        [f'{directory}{LOGO_NAME}' for directory in directories],
        lambda **_kwargs: Response(LOGO_PNG, mimetype='image/png'),
    )


def _signup_response(service: SignupService, directory: _Directory, username: str, password: str) -> Response:
    if not is_valid_username(username, directory.max_username_length):
        return _error_page('Invalid username.')
    if not is_valid_password(password):
        return _error_page('Invalid password.')
    result = service.create_or_login(
        username=username, password=password, store_kage_login_key=directory.store_kage_login_key
    )
    if not result.ok:
        return _error_page(result.error_message)
    return _account_ready(
        'Account ready',
        f'Welcome, <b>{result.username}</b>!<br>\n'
        '<br>\n'
        'Note down your recovery code and keep it<br>\n'
        'safe: it sets a new password if you forget<br>\n'
        'yours.<br>\n',
        result,
        directory,
    )


def _recover_response(
    service: SignupService, directory: _Directory, username: str, recovery_code: str, password: str
) -> Response:
    if not is_valid_username(username, directory.max_username_length):
        return _error_page('Invalid username.', RECOVER_PAGE)
    if not is_valid_recovery_code(recovery_code):
        return _error_page('Invalid recovery code.', RECOVER_PAGE)
    if not is_valid_password(password):
        return _error_page('Invalid password.', RECOVER_PAGE)
    result = service.recover(
        username=username,
        recovery_code=recovery_code,
        password=password,
        store_kage_login_key=directory.store_kage_login_key,
    )
    if not result.ok:
        return _error_page(result.error_message, RECOVER_PAGE)
    return _account_ready(
        'Password changed',
        f'Welcome back, <b>{result.username}</b>!<br>\n'
        '<br>\n'
        'Your recovery code has changed. Note down<br>\n'
        'the new one and keep it safe: the old one<br>\n'
        'no longer works.<br>\n',
        result,
        directory,
    )


def _account_ready(title: str, body: str, result: SignupResult, directory: _Directory) -> Response:
    """The logged-in page: `body`, the recovery code, and the ID the browser saves."""

    return html_response(
        _page(
            title,
            body,
            rows=(
                f'<tr><td align="center" bgcolor="{RECOVERY_CODE_BACKGROUND}">'
                f'<font color="{RECOVERY_CODE_COLOR}"><b>{result.recovery_code}</b></font></td></tr>',
                '<tr><td>\n'
                'Return to the game to save your username<br>\n'
                'to the memory card.<br>\n'
                '<br>\n'
                f'<center><a href="{directory.return_link}">Return to the game</a></center>\n'
                '</td></tr>',
            ),
        )
        + '<!--AM-USA-COMP-SIGNUP-->\n'
        f'<!--INPUT-IDS-->{result.username}<!--INPUT-IDE-->\n'
    )


def _error_page(message: str, retry_page: str = '') -> Response:
    # Back to a form of the same directory (`/<prefix>/create_id*.html` -> `/<prefix>/`).
    retry = request.path.rsplit('/', 1)[0] + '/' + retry_page
    return html_response(
        _page(
            'Login error',
            f'{message}<br>\n'
            '<br>\n'
            f'<center><a href="{retry}">Try again</a></center>\n',
            title_color=ERROR_COLOR,
        )
    )
