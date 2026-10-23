"""Build the retail EU (`SLES_527.07`) description patch.

Run `python3 -m tools.monsterhunter.build_eu_patch` from the repository root;
it writes `data/monsterhunter/patches/eu_<version>.bin` and its `patches.json`
entry, disabled: the blob has no signed DNAS install header, so retail EU
cannot install it (`opensnap_app/capcom/monsterhunter/patch.py`).
"""

import struct

from tools.monsterhunter.patchblob import text_blob, write_patch

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
# `YYMMDDhhmm` like the stamp it replaces, so the game's "Media Version" reads alike.
PATCH_NAME = b'2610221800'
PATCH_VERSION = b'1001'
CHANGELOG = (
    'The Chrome Voucher shows its description in every language.',
    'The GMR Chrome Heart shows its own description instead of the first Great Sword\'s.',
)
EU_RESIDENT_BLOB = 0x0039E300
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


def _eu_description_pokes(addresses: list[int]) -> list[tuple[int, int, bytes]]:
    pokes = [(0, address, _LI_A0_3) for address in _EU_SLOT_4_APPLY]
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
    return text_blob(b'M-HUNTER', EU_RESIDENT_BLOB, texts, _eu_description_pokes)


if __name__ == '__main__':
    print(write_patch(
        'eu', PATCH_NAME, PATCH_VERSION, [EU_STOCK_VERSION], build(),
        enabled=False, changelog=CHANGELOG,
    ))
