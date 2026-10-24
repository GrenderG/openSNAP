"""Build the retail NA (`SLUS_208.96`) patch: EU quest records and EU text.

Run `python3 -m tools.monsterhunter.build_na_patch` from the repository root;
it needs the MIPS binutils (`mipsel-linux-gnu-as`, `-ld`, `-objcopy`, `-nm`)
and writes `data/monsterhunter/patches/na_custom_<version>.bin` and its
`patches.json` entry, disabled: the blob has no signed DNAS install header,
so retail NA cannot install it (`opensnap_app/capcom/monsterhunter/patch.py`).
The code is in `na/patch.s`: key-0 pokes plus one resident block (key
`0xffffffff`) at `0x3d99a0..0x3d9faf` of the NA resident blob, the fold stub
first, then A. Interpreter-tested only, not run in game.

- A: quest clear time and per-quest kill counts are kept in the resident block
  and sent after 6306 as EU's 6307 (kind 1 counts, kind 2 weapon class and
  seconds), cleared on each ack; nothing is saved on the card.
- B: EU players' UTF-8 text (accents, ß, ¡, ¿) is folded to ASCII wherever NA
  prints or measures it, since the NA font has no accented letters; 3-byte
  characters print as `?`. NA's own text (ASCII and Shift-JIS symbol pairs) is
  unchanged.
"""

from pathlib import Path
import struct
import subprocess
import tempfile

from tools.monsterhunter.patchblob import RESIDENT_DATA_KEY, blob, entry, resident_address, write_patch

NA_STOCK_VERSION = b'0407251750'
# `YYMMDDhhmm` like the stamp it replaces, so the game's version reads alike.
PATCH_NAME = b'2610241900'
PATCH_VERSION = b'1002'
CHANGELOG = (
    'Quest clear times and kill counts are sent like EU, for the rankings.',
    'EU players\' accented text is shown as plain ASCII.',
)
NA_RESIDENT_BLOB = 0x003D9980
SOURCE = Path(__file__).resolve().parent / 'na' / 'patch.s'


def _jal(target: int) -> bytes:
    return struct.pack('<I', 0x0C000000 | (target >> 2) & 0x03FFFFFF)


def _j(target: int) -> bytes:
    return struct.pack('<I', 0x08000000 | (target >> 2) & 0x03FFFFFF)


def _word(value: int) -> bytes:
    return struct.pack('<I', value)


# (key, dest, original word, new bytes from the symbols, purpose). The applier
# copies byte by byte in this order: the reply-table entry is complete before
# the loop bound that makes it reachable changes.
def _pokes(symbols: dict[str, int]) -> list[tuple[int, int, int, bytes]]:
    s = symbols
    return [
        # A: reply entry 67 = 6307 (dir 2) handled by h6307; 6306's handler 0x2b3ba0 wrapped by h6306.
        (0x00, 0x00369933, 0x00, b'\x63'),
        (0x00, 0x00369983, 0x00, b'\x07'),
        (0x00, 0x003699D3, 0x00, b'\x01'),
        (0x00, 0x00369A23, 0x00, b'\x02'),
        (0x00, 0x00369B3C, 0x00000000, _word(s['h6307'])),
        (0x00, 0x00369B08, 0x002B3BA0, _word(s['h6306'])),
        # A: quest bookkeeping (Quest_enemy_die, Quest_start, clear branch, Quest_retire_set).
        (0x00, 0x001F1138, 0x0C07D68C, _jal(s['quest_kill'])),
        (0x00, 0x001F0A3C, 0x3C01003F, _jal(s['quest_start'])),
        (0x00, 0x001F16F8, 0x3C01003F, _jal(s['quest_clear'])),
        (0x00, 0x001F0C78, 0x2402FFFF, _jal(s['quest_retire'])),
        # B: both printers format through the fold (flfntPrintf, font_print_sp).
        (0x00, 0x001E5810, 0x0C05D4F4, _jal(s['fold_vsprintf'])),
        (0x00, 0x0012F7BC, 0x0C05D4F4, _jal(s['fold_vsprintf'])),
        # B: every place that measures or lays out player text gets the folded text.
        (0x00, 0x0011C918, 0x0C05BFCE, _jal(s['fold_strlen'])),   # name label centring
        (0x00, 0x001185A0, 0x0C046130, _jal(s['w_name'])),        # hunter name print
        (0x00, 0x00235624, 0x00A0982D, _jal(s['w_chat'])),        # chat log line
        (0x00, 0x00234F40, 0x27BDFF90, _j(s['w_msg'])),           # message window (folds in place)
        (0x23, 0x00602408, 0x0C05BFCE, _jal(s['fold_strlen'])),   # lobby Town name centring
        (0x23, 0x005FB348, 0x0C1827E0, _jal(s['w_panel'])),       # lobby player info panel
        (0x23, 0x005FB400, 0x0C1827E0, _jal(s['w_panel'])),
        (0x23, 0x005FC2A0, 0x0C04C43C, _jal(s['w_para'])),        # lobby paragraph wrap
        (0x23, 0x005FCDF8, 0x0C04C43C, _jal(s['w_para'])),
        (0x23, 0x0064F87C, 0x0C04C438, _jal(s['w_para_e0'])),
        (0x11, 0x0055C150, 0x0C05BFCE, _jal(s['fold_strlen'])),   # sub_main hunter name width
        # A: last, the reply match loop takes 68 entries (slti v0,t2,67 -> 68).
        (0x00, 0x002B4C4C, 0x29420043, _word(0x29420044)),
    ]


def assemble(link_address: int) -> tuple[bytes, dict[str, int]]:
    """Assemble and link `na/patch.s` at `link_address`; return the code and its symbols."""

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        commands = [
            ['mipsel-linux-gnu-as', '-EL', '-march=r5900', '-mabi=eabi', '-o', 'patch.o', str(SOURCE)],
            ['mipsel-linux-gnu-ld', '-EL', '-m', 'elf32ltsmip', '-e', '_start', '-N',
             f'-Ttext=0x{link_address:x}', '-o', 'patch.elf', 'patch.o'],
            ['mipsel-linux-gnu-objcopy', '-O', 'binary', '-j', '.text', 'patch.elf', 'patch.bin'],
        ]
        for command in commands:
            subprocess.run(command, cwd=work, check=True, capture_output=True)
        listing = subprocess.run(
            ['mipsel-linux-gnu-nm', 'patch.elf'], cwd=work, check=True, capture_output=True, text=True
        ).stdout
        symbols = {name: int(value, 16) for value, _kind, name in (line.split() for line in listing.splitlines())}
        return (work / 'patch.bin').read_bytes(), symbols


def build() -> bytes:
    # ld places .text on a 16-byte boundary; zero bytes in front of the code
    # put it exactly there inside the resident entry.
    data_address = resident_address(NA_RESIDENT_BLOB)
    link_address = (data_address + 15) & ~15
    code, symbols = assemble(link_address)
    if symbols['_start'] != link_address or symbols['QDATA'] % 8:
        raise ValueError('the resident block is misplaced')
    pokes = [entry(key, dest, data) for key, dest, _original, data in _pokes(symbols)]
    resident = bytes(link_address - data_address) + code
    return blob(b'M-HUNTER', [entry(RESIDENT_DATA_KEY, 0, resident), *pokes])


def original_words() -> list[tuple[int, int, bytes]]:
    """(key, dest, original bytes) of every poke, to check against the game images."""

    pokes = _pokes({name: 0 for name in ('h6306', 'h6307', 'quest_kill', 'quest_start', 'quest_clear',
                                          'quest_retire', 'fold_vsprintf', 'fold_strlen', 'w_name', 'w_chat',
                                          'w_msg', 'w_panel', 'w_para', 'w_para_e0')})
    return [
        (key, dest, original.to_bytes(len(data), 'little')) for key, dest, original, data in pokes
    ]


if __name__ == '__main__':
    print(write_patch(
        'na_custom', PATCH_NAME, PATCH_VERSION, [NA_STOCK_VERSION], build(),
        enabled=False, changelog=CHANGELOG,
    ))
