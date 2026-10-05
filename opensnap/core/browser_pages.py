"""Page style shared by the pages openSNAP serves to the games' browsers.

The game browsers have no CSS; pages are styled only with the attributes they
parse (same tag table in `SLUS_208.96` main `0x0035e0a0` and both Auto
Modellista `browser.bin`): `BODY BGCOLOR/TEXT/LINK`, `TABLE BGCOLOR/BORDER/
BORDERCOLOR/CELLPADDING/CELLSPACING/WIDTH`, `TD BGCOLOR/ALIGN`, `FONT COLOR`.
The browser wraps at any character (mid-word), so text lines are broken by
hand to fit a panel (about 48 characters). Tables are never nested.
"""

PAGE_BACKGROUND = '#101820'
PAGE_TEXT = '#E0E6EE'
PANEL_BACKGROUND = '#1C2A3A'
PANEL_BORDER = '#4F78A0'
TITLE_COLOR = '#FFC864'
ERROR_COLOR = '#FF8080'
NOTE_COLOR = '#9FB4C8'
PANEL_WIDTH = 520


def document(content: str) -> str:
    """Wrap centered `content` in the dark page body."""

    return (
        '<html>\n'
        f'<body bgcolor="{PAGE_BACKGROUND}" text="{PAGE_TEXT}" link="{TITLE_COLOR}">\n'
        '<center>\n'
        f'{content}'
        '</center>\n'
        '</body>\n'
        '</html>\n'
    )


def panel(title: str, body: str, *, title_color: str = TITLE_COLOR) -> str:
    """One panel: a title bar over a body cell."""

    return (
        f'{_table_open(cellpadding=8)}\n'
        f'{_title_row(title, title_color)}\n'
        f'<tr><td>\n{body}</td></tr>\n'
        '</table>\n'
    )


def data_table(title: str, header: tuple[str, ...], rows: list[tuple[str, ...]], *, empty: str) -> str:
    """One panel-styled table: a title bar, a header row, then `rows` (already escaped)."""

    columns = len(header)
    lines = [
        _table_open(cellpadding=4),
        _title_row(title, TITLE_COLOR, columns=columns),
        '<tr>' + ''.join(f'<td><font color="{NOTE_COLOR}">{cell}</font></td>' for cell in header) + '</tr>',
    ]
    lines += ['<tr>' + ''.join(f'<td>{cell}</td>' for cell in row) + '</tr>' for row in rows]
    if not rows:
        lines.append(f'<tr><td colspan="{columns}">{empty}</td></tr>')
    lines.append('</table>')
    return '\n'.join(lines) + '\n'


def _table_open(*, cellpadding: int) -> str:
    return (
        f'<table width="{PANEL_WIDTH}" border="1" bordercolor="{PANEL_BORDER}" cellspacing="0"'
        f' cellpadding="{cellpadding}" bgcolor="{PANEL_BACKGROUND}">'
    )


def _title_row(title: str, color: str, *, columns: int = 1) -> str:
    span = f' colspan="{columns}"' if columns > 1 else ''
    return f'<tr><td align="center"{span}><b><font color="{color}">{title}</font></b></td></tr>'
