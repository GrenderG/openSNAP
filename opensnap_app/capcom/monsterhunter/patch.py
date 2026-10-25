"""Monster Hunter online code patches.

How a server installs a code patch in every Monster Hunter build openSNAP
serves, and how to write one. Patches are built by `tools/monsterhunter` into
`data/monsterhunter/patches` (`patches.json` names each blob, its 6001 name
and version, the client versions it is offered to, whether it is enabled and
its changelog); the service only loads them and offers the enabled ones.
Builds: retail NA (`SLUS_208.96`), retail EU (`SLES_527.07`), the EU demo
(`SLED_530.83`), the EU test build (`TLES_527.07`) and the NA public beta
(`SLUS_291.10`). Integers on the APP
wire are big-endian (`protocol.py`), everything inside the patch blob is
little-endian.

Blob (applier `PatchExecCS`, NA `0x258400`)
-------------------------------------------
    char magic[8]     "M-HUNTER" (retail NA/EU) or "MH-PUB-B" (demo, test
                      build, NA beta)
    u32  count
    count entries:
        u32 key       module the entry patches (below)
        u32 dest      absolute RAM address
        u32 len       bit 31 clear: `len` data bytes follow
                      bit 31 set: a `u32 src` follows; `len & 0x7fffffff`
                      bytes are copied from absolute address `src`
        data          padded to 4 bytes

The applier is called with one key and copies only the entries with that key,
byte by byte, then calls `FlushCache(0)` (D-cache writeback only). There is
no checksum or signature: only the magic is checked. An entry whose key
matches nothing (e.g. `0xffffffff`) is never copied, so it is a free block of
code or data that stays in RAM inside the resident blob (below). The blob may
be up to 0x10000 bytes; its DNAS-encrypted form must also fit in 0x10000, so
leave some room.

Keys are `slot << 4 | overlay id`. Overlays are loaded with their 0x40-byte
`MWo3` header at the slot address, so `dest = slot + offset in the unpacked
file`. Every overlay load applies its key.

    key        module                        NA, NA beta, test   EU, demo
    0x00       main ELF                      -                   -
    0x11       sub_main.bin                  0x00552e00          0x00512480
    0x21-0x24  select, game, lobby, yn       0x005be900          0x0056bb00
    0x31-0x32  dnas_net, dnas_ins            0x00a86200          -
    0x31-0x35  str_e, f, g, i, s (language)  -                   0x00888000
    0x41-0x42  dnas_net, dnas_ins            -                   0x00a86200

When a patch is loaded or installed the applier runs for key 0 and the
loaded overlay of each slot (EU skips the language slot, so EU text overlays
are only patched when they load again). Main-ELF code that has already run
may still sit in the I-cache after `FlushCache(0)`; a hook that must take
effect at once can issue `FlushCache(2)` itself.

Retail NA and EU, and the EU demo: delivery and install
-------------------------------------------------------
1. On the first APP connection (APP1, mode 0) push 6001 `u8 1, field
   (name[10] + ver[4]), u32 size, u32 0`. `name` is the patch version (10
   ASCII characters), `ver` a display version ("1010" shows as 1.010),
   `size` the blob size, at most 0x10000. The client asks 6002 `field name,
   u32 offset, u16 length`; answer each with `field name, u32 offset, field
   data` holding exactly that slice. A name that differs from the offer
   fails the connection.
2. After the connection the client compares `name` with its version string
   over 10 characters; equal skips the install. It does so only after the
   whole download, so the server must not offer an installed patch again.
3. The client asks to save (message 61, yes/no). It needs an existing game
   save; declining aborts going online. Yes: DNAS install authentication
   (`InetDNAS2ConnectInstall`, must succeed), DNAS encryption of the blob
   (`InetDNAS2PersonalEncode`), the game save is written, and the blob is
   decrypted and applied.
4. Load-back: NA when its game save is loaded (`0x2407b0`, `0x242514`), EU
   and the demo only on entering online mode (EU `0x2307c0`, `0x232510`). The
   stored blob is applied only when its name sorts above the version string.

DNAS install header. `InetDNAS2PersonalEncode` (EU `0xaafbd0` in
`dnas_ins`) only accepts a download that starts with a 0x580-byte DNAS
install header, whose records are RSA-1024 signatures checked with public
keys in `dnas_ins` (key 83 first; the worker `0xaa3430`). The private keys
are not on the client, so openSNAP cannot make the header, and the blobs
built by `tools/monsterhunter` cannot be distributed as they are: a download
under 0x580 bytes makes the client poll forever (the "100%" install hang),
a longer one fails the signature check with a DNAS error. Both outcomes were
reproduced by emulating the client's own code on retail EU and NA (the
record check is `c^65537 mod n` with key 83's modulus, then PKCS#1 type-1
unpadding, `0xaacc48`). Their `patches.json` entries are therefore disabled.

RAM without an install. `OPENSNAP_MH_RAM_PATCH` (EU) and
`OPENSNAP_MH_RAM_PATCH_NA` (NA) make the APP service deliver a raw blob during
the online session instead: `ram_patch.py` uses World-list and download
handlers as arbitrary RAM writers, plants a small shellcode in the resident
patch buffer and calls this applier with key 0 and the loaded overlay slots
(1-4 on EU, 1-3 on NA). Nothing is stored and the patch lasts for the
game process.
EU works on a stock client and on one carrying Capcom's official 1001 patch
(`0509261742`) and replaces the 6001 offer; the EU demo and TLES are excluded.
NA uses a different route and blob and only reaches the stock stamp
`0407251750`, never the public beta, TLES or an already patched client. The
routes, the 1-16 World requirement, the 768-byte priming-page limit and the
shellcode are documented there.

Version string and naming. The version field of the client's 1002 answer is
its build stamp (`YYMMDDhhmm` of `__DATE__`/`__TIME__`) or the installed patch
name: when `count != 0` and the stamp sorts below the name (10 characters),
the applier copies the name over it. Name a patch with 10 ASCII characters
above the build's stamp and give it at least one entry; the stamp's own
`YYMMDDhhmm` form (e.g. "2610221800") keeps the game's "Media Version" looking
alike; push 6001 `u8 1` only while the 1002 version differs from it, and route
by the 1002 build byte (2 = NA, 3 = EU and demo) since patches are
build-specific. A newer patch needs a name above the installed one.

Storage. The stored form is DNAS-encrypted, so a patch can only be installed
over the network. It lives in the game save (`BASLUS-20896MH`,
`BESLES-52707MH`, `BESLED-53083MH`): `u16 0x0100, u16 seed, u16 sum,
u16 0x5963`, then 16-bit words XORed with a key stream (`k = seed`; per word:
`w ^= k; k = k or 1; k = k * 176 % 0xff53`; `sum` is the 16-bit sum of the
plain words). In the descrambled file the encrypted blob is at 0x208
(0x10000 bytes) and the header at 0x10208: `char name[10], char ver[4],
u32 length @0x14` (0 = no patch). Ordinary saves keep both.

    item                   NA            EU            demo
    magic                  "M-HUNTER"    "M-HUNTER"    "MH-PUB-B"
    build stamp            "0407251750"  "0502180939"  "0412241846"
    applier                0x00258400    0x00248350    0x002482c0
    resident blob          0x003d9980    0x0039e300    0x0039bf00
    encrypted copy         0x003c9980    0x0038e300    0x0038bf00
    patch header (0x40)    0x003e9980    0x003ae300    0x003abf00
    save size, words       0x11b88, 0x8dbc  0x11c0c, 0x8dfe  0x11c0c, 0x8dfe
    6001 / 6002 handlers   0x2b4150 / 0x2b43e0  0x2a46f8 / 0x2a4980  -

The resident blob holds 0x10000 bytes and is written only at load, install
and boot (cleared); put hook code there, never in the encrypted copy, the
header or the download buffer (heap).

NA public beta (`SLUS_291.10`)
------------------------------
Plaintext, no prompt, no DNAS, nothing stored: the patch lasts for the
session. Magic "MH-PUB-B", build stamp "0405171956", same wire format and
keys as NA. The 6002 replies are written to `0x003bbf80`; when the announced
size has arrived and the name differs from the stamp, the client copies the
blob to the resident buffer `0x003cbf80` (header `0x003dbf80`) and applies
all slots. Offer it on every session; it can't be saved.

EU test build (`TLES_527.07`)
-----------------------------
Cannot be patched over the network: it has NA's APP client and accepts the
download, but the install step (`0x247d28`) never reads the downloaded data
and the prompt/DNAS path is dead code. Magic "MH-PUB-B", stamp "0408131008".
"""

from dataclasses import dataclass
import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger('opensnap_app.capcom.monsterhunter')

PATCHES_DIRECTORY = 'patches'
MANIFEST_NAME = 'patches.json'


@dataclass(frozen=True, slots=True)
class ClientPatch:
    """One built patch: its 6001 name and display version, the client versions it is offered to, and the blob."""

    name: bytes
    version: bytes
    offered_to: frozenset[bytes]
    blob: bytes


def load_patches(data_directory: Path) -> dict[bytes, ClientPatch]:
    """Read the enabled patches of `patches/patches.json` into {client version: patch}."""

    directory = data_directory / PATCHES_DIRECTORY
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    by_version: dict[bytes, ClientPatch] = {}
    for build, entry in json.loads(manifest_path.read_text()).items():
        if not entry['enabled']:
            _LOGGER.info('Client patch %s (%s) is disabled.', build, entry['name'])
            continue
        patch = ClientPatch(
            name=entry['name'].encode('ascii'),
            version=entry['version'].encode('ascii'),
            offered_to=frozenset(stamp.encode('ascii') for stamp in entry['offered_to']),
            blob=(directory / entry['file']).read_bytes(),
        )
        if len(patch.name) != 10 or len(patch.version) != 4 or not 0 < len(patch.blob) <= 0x10000:
            raise ValueError(f'{build} patch in {manifest_path} is malformed')
        for stamp in patch.offered_to:
            by_version[stamp] = patch
        _LOGGER.info('Client patch %s (%s) for versions %s.', build, entry['name'], ', '.join(entry['offered_to']))
    return by_version
