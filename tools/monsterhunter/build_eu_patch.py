"""Build the retail EU (`SLES_527.07`) patch: Capcom's 1001 fixes and description fixes.

Run `python3 -m tools.monsterhunter.build_eu_patch` from the repository root;
it writes `data/monsterhunter/patches/eu_custom_<version>.bin` and its `patches.json`
entry, disabled: the blob has no signed DNAS install header, so retail EU
cannot install it (`opensnap_app/capcom/monsterhunter/patch.py`). It reads
Capcom's own 1001 patch, `eu_official_1001.bin` in the same folder.
"""

import struct

from tools.monsterhunter.patchblob import PATCHES_DIRECTORY, resident_address, text_blob, write_patch

# Retail EU (`SLES_527.07`) English text fixes, also translated into the other
# four languages. Each language is its own text overlay (`str_e/f/g/i/s.bin`,
# keys 0x31-0x35, all loaded at `0x00888000`) with its own tables, whose
# addresses the main ELF's per-language getters return (English `0x002bb0d0`,
# French `0x002b88d0`, German `0x002b92d0`, Italian `0x002b9cd0`, Spanish
# `0x002ba6d0`; item descriptions at +0, weapon descriptions at +0x24).
# - Chrome Voucher (item 309 of the item description table, NA `0x0035b374`):
#   every language points it at a blank string, so it shows no description.
# - The GMR Chrome Heart (weapon description entry at +0x1030, three line
#   pointers): every language repeats its first Great Sword's lines.
# Both get NA's text (`slus_208.96` `0x0037dce0`, `0x00383960`), written the
# EU way: each line ends with a space. The EU font is fixed-width Latin-1 (no
# curly quotes, no euro sign); the boxes hold 3 lines of 28 visible
# characters, and descriptions are printf formats, so `%` is written `%%`.
EU_STOCK_VERSION = b'0502180939'
# Capcom's 1001 patch (name `0509261742`), as the save stores it once decoded.
# A client with it installed reports its name as its version.
OFFICIAL_PATCH = PATCHES_DIRECTORY / 'eu_official_1001.bin'
OFFICIAL_VERSION = b'0509261742'
# `YYMMDDhhmm` like the stamp it replaces, so the game's "Media Version" reads alike.
PATCH_NAME = b'2610241800'
PATCH_VERSION = b'1002'
CHANGELOG = (
    'An unlock that needs Hunter Rank 20 and 50 hunts of two monsters counts Rathian and Rathalos '
    'hunts instead of Fatalis and Aptonoth.',
    'That unlock is checked again as soon as a quest\'s hunts are counted.',
    'The Chrome Voucher shows its description in every language.',
    'The GMR Chrome Heart shows its own description instead of the first Great Sword\'s.',
)
EU_RESIDENT_BLOB = 0x0039E300
# Capcom's 1001 fixes: two replacement functions, kept as Capcom wrote them
# (only PC-relative branches and absolute calls into game code, so they run
# anywhere), and a `j; nop` over the first two words of each original.
# - lobby.bin `0x005784d0`, a lobby unlock check (caller `0x578570`): HR 20 and
#   50 hunts of two monsters, read from the u16 hunt totals at `0x003aea0c`
#   (monster n at +2*(n-1)). The original reads +2 and +0x16 (Fatalis,
#   Aptonoth); the copy reads +0 and +0x14 (Rathian, Rathalos).
# - main ELF `0x002a4050`, the post-quest step that adds the pending hunts
#   into those totals; the copy also calls `0x578570` after adding them.
# (key, dest, offset of the replacement in Capcom's blob), and the end of its code.
_OFFICIAL_HOOKS = ((0x23, 0x005784D0, 0x40), (0x00, 0x002A4050, 0xE0))
_OFFICIAL_CODE_END = 0x1D0
# EU applies a loaded patch for key 0 and the loaded overlay of slots 1, 2 and
# 4, never slot 3, the text overlay (`0x00248858..0x002488bc`, and right after
# an install `0x00246be8..0x00246c4c`). These `li a0,4` delay slots become
# `li a0,3`, so the loaded language's entries apply at once; slot 4 (DNAS)
# has no entries.
_EU_SLOT_4_APPLY = (0x002488A4, 0x002488BC, 0x00246C34, 0x00246C4C)
_LI_A0_3 = struct.pack('<I', 0x24040003)
# Text overlay key, Chrome Voucher description slot, GMR Chrome Heart's first
# line slot, the voucher text and the sword's three lines.
_EU_DESCRIPTION_FIXES = (
    (
        0x31, 0x008DBD04, 0x008E2FD0,
        "For those about to rock. \nGMR salutes you. \nWe're rocking right along. ",
        ('A mighty monster-masher. ', 'Molded in metal ', 'by the Gods of Rock for GMR '),
    ),
    (
        0x32, 0x008E67E4, 0x008EDC10,
        'Pour ceux qui vont rocker. \nGMR vous salue. \nOn continue à rocker ! ',
        ('Une puissante épée longue ', 'forgée dans le métal par ', 'les dieux du rock pour GMR. '),
    ),
    (
        0x33, 0x008DEC14, 0x008E5DC0,
        'Für alle, die rocken wollen. \nGMR grüßt Sie. \nWir rocken weiter! ',
        ('Ein mächtiger Monster- ', 'zermalmer. Geschmiedet von ', 'den Rockgöttern für GMR. '),
    ),
    (
        0x34, 0x008DDDE4, 0x008E52D0,
        'Per chi sta per fare rock. \nGMR vi saluta. \nContinuiamo a rockeggiare. ',
        ('Un possente schiaccia- ', 'mostri, forgiato dagli ', 'dèi del rock per GMR. '),
    ),
    (
        0x35, 0x008DDA24, 0x008E4B50,
        'Para los que van a rockear. \nGMR te saluda. \n¡Seguimos rockeando! ',
        ('Un gran aplastamonstruos ', 'forjado en metal por los ', 'dioses del rock para GMR. '),
    ),
)


def _jump(target: int) -> bytes:
    """`j target; nop`."""

    return struct.pack('<2I', 0x08000000 | (target >> 2) & 0x03FFFFFF, 0)


def _official_code() -> bytes:
    """Capcom's replacement functions, after checking its blob hooks them as `_OFFICIAL_HOOKS` says."""

    official = OFFICIAL_PATCH.read_bytes()
    hooks = [struct.unpack_from('<3I8s', official, 12 + 20 * i) for i in range(len(_OFFICIAL_HOOKS))]
    expected = [(key, dest, 8, _jump(EU_RESIDENT_BLOB + offset)) for key, dest, offset in _OFFICIAL_HOOKS]
    if official[:12] != b'M-HUNTER' + struct.pack('<I', len(_OFFICIAL_HOOKS)) or hooks != expected:
        raise ValueError(f'{OFFICIAL_PATCH} is not Capcom\'s 1001 patch')
    return official[_OFFICIAL_HOOKS[0][2]:_OFFICIAL_CODE_END]


def _eu_description_pokes(addresses: list[int]) -> list[tuple[int, int, bytes]]:
    code = resident_address(EU_RESIDENT_BLOB)
    pokes = [(key, dest, _jump(code + offset - _OFFICIAL_HOOKS[0][2])) for key, dest, offset in _OFFICIAL_HOOKS]
    pokes += [(0, address, _LI_A0_3) for address in _EU_SLOT_4_APPLY]
    texts = iter(addresses)
    for key, voucher_slot, sword_slot, _voucher, _sword in _EU_DESCRIPTION_FIXES:
        pokes.append((key, voucher_slot, struct.pack('<I', next(texts))))
        pokes.append((key, sword_slot, struct.pack('<3I', next(texts), next(texts), next(texts))))
    return pokes


def build() -> bytes:
    texts = [
        text.encode('utf-8')
        for _key, _voucher_slot, _sword_slot, voucher, sword in _EU_DESCRIPTION_FIXES
        for text in (voucher, *sword)
    ]
    return text_blob(b'M-HUNTER', EU_RESIDENT_BLOB, texts, _eu_description_pokes, code=_official_code())


if __name__ == '__main__':
    print(write_patch(
        'eu_custom', PATCH_NAME, PATCH_VERSION, [EU_STOCK_VERSION, OFFICIAL_VERSION], build(),
        enabled=False, changelog=CHANGELOG,
    ))
